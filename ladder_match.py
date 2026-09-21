"""
Ladder synthesis: Kalshi cumulative thresholds vs Polymarket range buckets.

The two venues quote margin-of-victory (and similar) races in different shapes:

    Kalshi      "Republicans, 26+ pts"      cumulative: YES iff margin >= 26
    Polymarket  "Republican 25-30%"         disjoint buckets that partition the race

So no single Polymarket market is the counterpart of a Kalshi rung. The
counterpart is a SUM: P(margin >= 26) equals the total of every bucket lying at
or above 26. That makes this a multi-leg relationship, which is why the pair
matcher (strictly 1-to-1) can never find it - Kalshi's 4,552 midterm MOV markets
are the largest matchable class it leaves behind (docs/EXPANSION_PROPOSAL.md,
pass 15).

Thresholds do not always land on a bucket edge (Kalshi 7 vs bucket "5-10"), so
the sum is not always the same contract. That does NOT make the relationship
unusable - it makes it one-sided, and WHICH basket you use depends on the
direction you trade:

  * ABOVE-only basket (buckets entirely at or above the threshold) pays 1 only
    when the Kalshi rung also pays 1. It is DOMINATED by the rung. So you may
    buy the rung and sell that basket: the rung covers every state the basket
    owes in.
  * ABOVE + STRADDLING basket (adding the bucket the threshold falls inside)
    pays 1 whenever the rung does, and sometimes more. It DOMINATES the rung.
    So you may sell the rung and buy that basket.

Picking the right basket per direction makes both legs of the comparison an
exact settlement dominance, not an estimate - a positive edge holds however the
straddling bucket resolves. When the threshold does coincide with a bucket edge
the straddle set is empty and the two baskets are the same contract; `exact`
records that, because those rungs replicate in both directions at once.

Prices come from `catalog_bid`/`catalog_ask` in `extra`, which are Gamma's real
top-of-book. The snapshot's main orderbook is deliberately a single mid used on
both sides (see `discover._catalog_bid_ask`) and must NOT be used for edges.

Output is still review-only: executing these needs N-leg support in the
executor, and the legs are thin. Nothing here feeds the alerter.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

_MOV_TITLE_RE = re.compile(r"\s*(?:election\s+)?margin of victory.*$", re.IGNORECASE)
_PARTY = (("republican", r"\brepublicans?\b|\bgop\b"),
          ("democrat", r"\bdemocrats?\b|\bdemocratic\b|\bdem\b"))


def race_key(title: str) -> str:
    """Normalised race name shared by both venues ("Wyoming Senate")."""
    t = _MOV_TITLE_RE.sub("", (title or "").lower()).strip()
    t = re.sub(r"\b(the|20\d\d)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _side(label: str) -> str:
    """Party when the label names one, else the bare name ("Hageman")."""
    low = (label or "").lower()
    for name, pat in _PARTY:
        if re.search(pat, low):
            return name
    return re.sub(r"[^a-z ]", "", low).strip()


def parse_kalshi_rung(snap) -> tuple[str, float] | None:
    """("republican", 26.0) from "Republicans, 26+ pts" / "Hageman, 26+ pts"."""
    sub = (snap.extra or {}).get("yes_sub_title") or snap.title or ""
    m = re.match(r"^(?P<side>.+?),\s*(?P<n>\d+(?:\.\d+)?)\s*\+", sub)
    if not m:
        return None
    strike = (snap.extra or {}).get("floor_strike")
    value = float(strike) if strike is not None else float(m.group("n"))
    return _side(m.group("side")), value


def parse_poly_bucket(snap) -> tuple[str, float, float] | None:
    """("democrat", 10.0, 15.0) from "Democrat 10-15%"; "R 5%+" -> (5, inf);
    "Lula <5%" -> (0, 5); "Republican Wins" -> (0, inf)."""
    label = (snap.title or "").strip()
    m = re.match(r"^(?P<side>.+?)\s*(?P<lo>\d+(?:\.\d+)?)\s*[-–]\s*(?P<hi>\d+(?:\.\d+)?)\s*%?$", label)
    if m:
        return _side(m.group("side")), float(m.group("lo")), float(m.group("hi"))
    m = re.match(r"^(?P<side>.+?)\s*(?P<lo>\d+(?:\.\d+)?)\s*%?\s*\+$", label)
    if m:
        return _side(m.group("side")), float(m.group("lo")), math.inf
    m = re.match(r"^(?P<side>.+?)\s*<\s*(?P<hi>\d+(?:\.\d+)?)\s*%?$", label)
    if m:
        return _side(m.group("side")), 0.0, float(m.group("hi"))
    m = re.match(r"^(?P<side>.+?)\s+wins?$", label, re.IGNORECASE)
    if m:
        return _side(m.group("side")), 0.0, math.inf
    return None


def _quotes(snap) -> tuple[float | None, float | None]:
    """Real (bid, ask) for a Polymarket leg.

    NOT snap.orderbook - that holds `outcomePrices`, one mid written to both
    sides so the matcher's price_sim keeps working. Trading off it would book a
    spread that does not exist.
    """
    ex = snap.extra or {}
    return ex.get("catalog_bid"), ex.get("catalog_ask")


def _sum(legs, idx) -> float | None:
    """Total of one side of the book over legs; None if any leg is unquoted."""
    total = 0.0
    for _, s in legs:
        q = _quotes(s)[idx]
        if q is None:
            return None
        total += q
    return total


def _covers_tail(legs) -> bool:
    """Do these buckets cover every outcome at or above their own floor?

    The dominating basket only dominates if it pays in EVERY state the Kalshi
    rung pays in, so the buckets must run upward with no gap and the top one
    must be unbounded. Polymarket mixes shapes within a race (a Kansas ladder
    for one candidate, a bare "Hamilton Wins" for the other), so a race that
    looks like a ladder can still have a hole or a capped top - buying such a
    basket would leave the short Kalshi rung uncovered in the tail.
    """
    if not legs:
        return False
    ranges = sorted(((b[1], b[2]) for b, _ in legs))
    reach = ranges[0][1]
    for lo, hi in ranges[1:]:
        if lo > reach:            # gap: nothing pays between reach and lo
            return False
        reach = max(reach, hi)
    return reach == math.inf


def synthesize(k_snaps, p_snaps) -> list[dict]:
    """Pair each Kalshi rung with the Polymarket buckets that replicate it.

    Returns one record per rung carrying BOTH baskets: `sub_legs` (above-only,
    dominated by the rung) and `sup_legs` (above + straddling, dominating it),
    each with the side of the book you would actually trade. `exact` is True
    when the threshold lands on a bucket edge, where the two coincide.
    """
    k_races: dict[str, list] = defaultdict(list)
    for s_ in k_snaps:
        if "MIDTERMMOV" not in (s_.event_id or "") and "margin of victory" not in (
                (s_.extra or {}).get("event_title") or "").lower():
            continue
        rung = parse_kalshi_rung(s_)
        if rung:
            k_races[race_key((s_.extra or {}).get("event_title") or "")].append((rung, s_))

    p_races: dict[str, list] = defaultdict(list)
    for s_ in p_snaps:
        ev = (s_.extra or {}).get("event_title") or ""
        if "margin of victory" not in ev.lower():
            continue
        bucket = parse_poly_bucket(s_)
        if bucket:
            p_races[race_key(ev)].append((bucket, s_))

    out: list[dict] = []
    for race, rungs in k_races.items():
        buckets = p_races.get(race)
        if not buckets:
            continue
        for (k_side, threshold), ks in rungs:
            legs = [(b, s_) for (b, s_) in buckets if b[0] == k_side]
            if not legs:
                continue
            above = [(b, s_) for (b, s_) in legs if b[1] >= threshold]
            straddle = [(b, s_) for (b, s_) in legs if b[1] < threshold < b[2]]
            if not above and not straddle:
                continue
            # Sell the dominated basket (collect bids); buy the dominating one
            # (pay asks). Either may be unquoted, which kills only that side.
            sub_bid = _sum(above, 0) if above else None
            # Selling the dominated basket is safe whatever the rest of the
            # race looks like (every bucket in it sits entirely above the
            # threshold). Buying the dominating one is not - it has to cover
            # the whole tail.
            sup = above + straddle
            sup_ask = _sum(sup, 1) if _covers_tail(sup) else None
            if sub_bid is None and sup_ask is None:
                continue
            out.append({
                "race": race,
                "side": k_side,
                "threshold": threshold,
                "kalshi_ticker": ks.market_id,
                "kalshi_title": ks.title,
                "kalshi_bid": ks.orderbook.best_bid if ks.orderbook else None,
                "kalshi_ask": ks.orderbook.best_ask if ks.orderbook else None,
                "exact": not straddle,
                "sub_bid": None if sub_bid is None else round(sub_bid, 4),
                "sup_ask": None if sup_ask is None else round(sup_ask, 4),
                "sub_legs": [s_.market_id for _, s_ in above],
                "sup_legs": [s_.market_id for _, s_ in above + straddle],
                "leg_labels": [s_.title for _, s_ in above + straddle],
            })
    return out


def edges(records: list[dict], fee: float = 0.07) -> list[dict]:
    """Attach the best settlement-safe edge to each synthesis.

    Both directions are exact dominance relations, so neither result is an
    estimate:

      long rung / short ABOVE-only basket   sum(bids) - k_ask  - fee(k_ask)
      short rung / long ABOVE+STRADDLE      k_bid - sum(asks)  - fee(1-k_bid)

    The rung pays whenever the dominated basket does, and the dominating basket
    pays whenever the rung does, so a positive number here survives however the
    straddling bucket resolves. Kalshi charges the taker fee; PM's CLOB does not.
    """
    out = []
    for r in records:
        k_bid, k_ask = r.get("kalshi_bid"), r.get("kalshi_ask")
        f = lambda q: fee * q * (1 - q)  # Kalshi taker fee
        cands = []
        if k_ask is not None and r["sub_bid"] is not None:
            cands.append((r["sub_bid"] - k_ask - f(k_ask),
                          "buy rung on Kalshi + sell above-only basket on PM",
                          r["sub_legs"]))
        if k_bid is not None and r["sup_ask"] is not None:
            cands.append((k_bid - r["sup_ask"] - f(1 - k_bid),
                          "sell rung on Kalshi + buy above+straddle basket on PM",
                          r["sup_legs"]))
        if not cands:
            continue
        edge, direction, legs = max(cands, key=lambda c: c[0])
        out.append({**r, "edge": round(edge, 4), "direction": direction, "legs": legs})
    return sorted(out, key=lambda r: -r["edge"])

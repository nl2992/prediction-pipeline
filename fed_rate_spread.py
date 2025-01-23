"""
Fed Rate Spread Analysis
========================
Compares Fed funds rate expectations across Kalshi and Polymarket.

Kalshi : KXFED series — "Will the upper bound of the federal funds rate be
         above X%?" threshold ladder, one contract per 25 bps level.
         Markets are meeting-specific (e.g. KXFED-26JUN closes on meeting day).

Polymarket: two complementary ladders across a full-year horizon (before 2027):
  - "Will no / 1 / 2 / … Fed rate cuts happen in 2026?" — count-based
  - "Will the Fed's upper bound reach X%+ before 2027?" — level-based (hikes)
  - "Will the Fed's lower bound reach X% or lower before 2027?" — level-based (cuts)

Because the two exchanges use different structures and time horizons this script:
  1. Fetches the Kalshi KXFED ladder and derives an implied probability
     distribution over rate levels at the next meeting.
  2. Fetches all Polymarket Fed markets via full-catalog pagination.
  3. Compares implied cut probabilities between the two exchanges.
  4. Flags monotonicity violations within either exchange's ladder
     (prices that violate basic probability ordering are cross-market spreads).
  5. Runs matcher + arb on any directly equivalent markets found.

Usage:
    python fed_rate_spread.py                        # next meeting auto-detected
    python fed_rate_spread.py --event KXFED-26JUN    # specify Kalshi event ticker
    python fed_rate_spread.py --poly-timeout 30      # longer Polymarket timeout
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return round((bid + ask) / 2, 4)
    return bid or ask


def _rate_from_ticker(ticker: str) -> float | None:
    try:
        return float(ticker.split("-T")[-1])
    except (IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Kalshi: fetch and parse the KXFED ladder
# ---------------------------------------------------------------------------


def fetch_kalshi_fed_ladder(
    event_ticker: str | None = None,
    timeout: int = 20,
) -> tuple[str, list[dict]]:
    from kalshi.client import KalshiClient

    client = KalshiClient(timeout=timeout)

    if event_ticker:
        resp = client.get_markets(event_ticker=event_ticker, status="open", limit=200)
        mkts = resp.get("markets", [])
    else:
        all_mkts = client.get_all_markets(series_ticker="KXFED", max_pages=10, status="open")
        if not all_mkts:
            return ("", [])
        events: dict[str, list[dict]] = {}
        for m in all_mkts:
            ev = m.get("event_ticker", "")
            events.setdefault(ev, []).append(m)
        event_ticker = min(
            events,
            key=lambda e: min(m.get("close_time", "9999") for m in events[e]),
        )
        mkts = events[event_ticker]

    ladder = [m for m in mkts if _rate_from_ticker(m.get("ticker", "")) is not None]
    ladder.sort(key=lambda m: _rate_from_ticker(m["ticker"]))
    return (event_ticker or "", ladder)


def build_kalshi_distribution(ladder: list[dict]) -> list[dict]:
    """
    Convert a sorted KXFED threshold ladder into an implied probability distribution.

    "Will upper bound be ABOVE X?" YES = P(rate > X).
    P(rate = X) = P(above X_prev) - P(above X).
    """
    from kalshi.client import KalshiClient

    above: list[tuple[float, float]] = []
    for m in ladder:
        rate = _rate_from_ticker(m["ticker"])
        top = KalshiClient.parse_top_of_book(m)
        p = _mid(top["yes_bid"], top["yes_ask"])
        if rate is not None and p is not None:
            above.append((rate, p))

    if not above:
        return []

    dist: list[dict] = []
    for i, (rate, p_above) in enumerate(above):
        p_prev = above[i - 1][1] if i > 0 else 1.0
        p_exact = round(p_prev - p_above, 4)
        top = KalshiClient.parse_top_of_book(ladder[i])
        dist.append({
            "threshold": rate,
            "implied_rate": rate,
            "p_above": round(p_above, 4),
            "p_this_level": max(0.0, p_exact),
            "yes_bid": _f(top.get("yes_bid")),
            "yes_ask": _f(top.get("yes_ask")),
            "ticker": ladder[i]["ticker"],
        })

    # Residual tail below the lowest rung
    dist.insert(0, {
        "threshold": None,
        "implied_rate": above[0][0] - 0.25,
        "p_above": None,
        "p_this_level": round(1.0 - above[0][1], 4),
        "yes_bid": None,
        "yes_ask": None,
        "ticker": f"<{above[0][0]:.2f}%",
    })

    dist.sort(key=lambda x: -x["p_this_level"])
    return dist


# ---------------------------------------------------------------------------
# Polymarket: full-catalog pagination for Fed markets
# ---------------------------------------------------------------------------

_FED_KW = [
    "fed rate cut", "fed rate cuts", "no fed rate",
    "upper bound reach", "lower bound reach",   # catches "Fed's upper/lower bound reach X%"
    "upper bound", "lower bound",               # broader fallback
    "federal funds", "fomc meeting", "fed emergency",
    "rate cut happen", "rate cuts happen",
    "fed abolished", "emergency rate cut",
]


def fetch_polymarket_fed_markets(
    poly_timeout: int = 10,
) -> list[dict]:
    """
    Paginate through all active Polymarket markets and return Fed-related ones.
    Returns [] on network failure.
    """
    found: list[dict] = []
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    print("  Paginating Polymarket catalog (this may take ~20 s)…", end="", flush=True)
    try:
        for offset in range(0, 3000, 100):
            r = session.get(
                "https://gamma-api.polymarket.com/markets",
                params={"limit": 100, "offset": offset, "active": "true", "closed": "false"},
                timeout=poly_timeout,
            )
            r.raise_for_status()
            mkts = r.json()
            if isinstance(mkts, dict):
                mkts = mkts.get("markets", [])
            if not mkts:
                break
            for m in mkts:
                t = (m.get("question") or m.get("title") or "").lower()
                if any(kw in t for kw in _FED_KW):
                    found.append(m)
            if offset % 500 == 0:
                print(".", end="", flush=True)
        print(f" done. {len(found)} markets found.")
    except Exception as exc:
        print(f"\n  [WARN] Polymarket unreachable: {exc.__class__.__name__}")
        return []

    return found


def _parse_poly_price(m: dict) -> float | None:
    prices_raw = m.get("outcomePrices")
    if isinstance(prices_raw, str):
        try:
            prices_raw = json.loads(prices_raw)
        except Exception:
            return None
    if prices_raw and len(prices_raw) > 0:
        return _f(prices_raw[0])
    return None


# ---------------------------------------------------------------------------
# Classify and structure Polymarket Fed markets
# ---------------------------------------------------------------------------


def classify_polymarket_fed(markets: list[dict]) -> dict:
    """
    Group Polymarket Fed markets into:
      - count_cuts: "Will N Fed rate cuts happen in 2026?"
      - upper_reach: "Will upper bound reach X%+ before 2027?" (hike)
      - lower_reach: "Will lower bound reach X% or lower before 2027?" (cut)
      - other: anything else
    """
    count_cuts: list[dict] = []
    upper_reach: list[dict] = []
    lower_reach: list[dict] = []
    other: list[dict] = []

    _count_re = re.compile(r"(\d+)\s+fed rate cut")
    _no_cut_re = re.compile(r"no fed rate cut")
    _upper_re = re.compile(r"upper bound reach ([\d.]+)%")
    _lower_re = re.compile(r"lower bound reach ([\d.]+)%")

    for m in markets:
        t = (m.get("question") or m.get("title") or "").lower()
        p = _parse_poly_price(m)
        row = {"title": m.get("question") or m.get("title", ""), "price": p, "raw": m}

        if _no_cut_re.search(t):
            row["n_cuts"] = 0
            count_cuts.append(row)
        elif (mo := _count_re.search(t)):
            row["n_cuts"] = int(mo.group(1))
            count_cuts.append(row)
        elif (mo := _upper_re.search(t)):
            row["rate"] = float(mo.group(1))
            upper_reach.append(row)
        elif (mo := _lower_re.search(t)):
            row["rate"] = float(mo.group(1))
            lower_reach.append(row)
        else:
            other.append(row)

    count_cuts.sort(key=lambda x: x["n_cuts"])
    upper_reach.sort(key=lambda x: x["rate"])
    lower_reach.sort(key=lambda x: x["rate"], reverse=True)

    return {
        "count_cuts": count_cuts,
        "upper_reach": upper_reach,
        "lower_reach": lower_reach,
        "other": other,
    }


# ---------------------------------------------------------------------------
# Monotonicity violation checker (intra-exchange arb flag)
# ---------------------------------------------------------------------------


def check_monotonicity(
    ladder: list[dict],
    direction: str,  # "ascending" (higher threshold → lower P) or "descending"
) -> list[dict]:
    """
    For a ladder of (rate, price) pairs, flag adjacent pairs where the
    probability ordering is violated.

    ascending:  P(reach X%) should DECREASE as X increases (it's harder to reach higher levels)
    descending: P(reach X%) should INCREASE as X decreases (easier to fall below lower levels)

    Returns list of violation dicts.
    """
    violations = []
    prices = [r["price"] for r in ladder if r["price"] is not None]
    rates = [r["rate"] for r in ladder if r["price"] is not None]

    for i in range(1, len(prices)):
        if direction == "ascending":
            # P should decrease as rate increases
            if prices[i] > prices[i - 1]:
                violations.append({
                    "rate_low": rates[i - 1],
                    "rate_high": rates[i],
                    "p_low": prices[i - 1],
                    "p_high": prices[i],
                    "excess": round(prices[i] - prices[i - 1], 4),
                })
        else:
            # P should increase as rate decreases
            if prices[i] > prices[i - 1]:
                violations.append({
                    "rate_low": rates[i],
                    "rate_high": rates[i - 1],
                    "p_low": prices[i - 1],
                    "p_high": prices[i],
                    "excess": round(prices[i] - prices[i - 1], 4),
                })
    return violations


def compute_monotonicity_arb(
    violation: dict,
    fee_rate: float = 0.02,
) -> dict:
    """
    For a monotonicity violation where P(high_threshold) > P(low_threshold),
    compute the two-leg arb:
      - Buy YES on low_threshold (cheaper, must pay out if high_threshold pays out)
      - Buy NO on high_threshold (since high is overpriced)

    All outcomes:
      A) Neither reached:  YES(low)=$0, NO(high)=$1 → payout $1
      B) Low reached, not high: YES(low)=$1, NO(high)=$1 → payout $2
      C) Both reached: YES(low)=$1, NO(high)=$0 → payout $1

    Minimum payout = $1 in all cases.
    """
    cost_yes_low = violation["p_low"]       # buy YES on lower threshold (cheaper)
    cost_no_high = 1.0 - violation["p_high"]  # buy NO on higher threshold (overpriced YES)
    gross_cost = round(cost_yes_low + cost_no_high, 4)
    # Minimum payout = $1 (scenario A or C); scenario B pays $2
    min_payout = 1.0
    gross_profit = round(min_payout - gross_cost, 4)
    net_profit = round(gross_profit - fee_rate, 4)

    return {
        "buy_yes": f"reach {violation['rate_low']:.2f}%",
        "buy_yes_cost": cost_yes_low,
        "buy_no": f"reach {violation['rate_high']:.2f}%",
        "buy_no_cost": round(cost_no_high, 4),
        "gross_cost": gross_cost,
        "min_payout": min_payout,
        "gross_profit": gross_profit,
        "net_profit_after_fee": net_profit,
        "profitable": net_profit > 0,
    }


# ---------------------------------------------------------------------------
# Cross-exchange implied probability comparison
# ---------------------------------------------------------------------------


def cross_exchange_summary(
    kalshi_dist: list[dict],
    poly_groups: dict,
    current_lower_bound: float = 3.50,  # today's Fed lower bound
    current_upper_bound: float = 3.75,  # today's Fed upper bound
) -> None:
    """Print a side-by-side comparison of implied cut probabilities."""

    # Kalshi: P(any cut at next meeting) = P(rate ≠ current at next meeting)
    current_level_row = next(
        (r for r in kalshi_dist if abs(r["implied_rate"] - current_upper_bound) < 0.01),
        None,
    )
    kalshi_hold_prob = current_level_row["p_this_level"] if current_level_row else None

    # Polymarket: P(no cuts in 2026)
    no_cut_row = next(
        (r for r in poly_groups["count_cuts"] if r.get("n_cuts") == 0), None
    )
    poly_no_cut_prob = no_cut_row["price"] if no_cut_row else None

    # Implied H2 cut probability: if Kalshi gives P(hold at June) and Polymarket gives P(no cuts all year)
    if kalshi_hold_prob and poly_no_cut_prob:
        # P(no cut June) ≈ kalshi_hold_prob
        # P(no cut all year) = poly_no_cut_prob
        # P(at least 1 cut after June | no cut by June) ≈ (poly_no_cut_prob) implies cuts
        p_cut_by_june = 1.0 - kalshi_hold_prob
        p_no_cut_all_year = poly_no_cut_prob
        p_cut_after_june_given_hold_june = (
            (kalshi_hold_prob - p_no_cut_all_year) / kalshi_hold_prob
            if kalshi_hold_prob > 0 else None
        )
    else:
        p_cut_by_june = p_cut_after_june_given_hold_june = None

    print(f"\n{'='*70}")
    print("  CROSS-EXCHANGE CUT PROBABILITY COMPARISON")
    print(f"{'='*70}")
    print(f"  Current rate:  lower={current_lower_bound:.2f}%  upper={current_upper_bound:.2f}%")
    print()
    print(f"  {'Metric':<48}  {'Kalshi':>8}  {'Polymarket':>10}")
    print(f"  {'-'*48}  {'-'*8}  {'-'*10}")

    def _pct(v):
        return f"{v*100:.1f}%" if v is not None else "    n/a"

    print(f"  {'P(hold at next meeting / no cuts in 2026)':<48}  "
          f"{_pct(kalshi_hold_prob):>8}  {_pct(poly_no_cut_prob):>10}")
    print(f"  {'P(≥1 cut at next meeting / ≥1 cut in 2026)':<48}  "
          f"{_pct(p_cut_by_june):>8}  {_pct(1-poly_no_cut_prob if poly_no_cut_prob else None):>10}")

    if p_cut_after_june_given_hold_june is not None:
        print()
        print(f"  Implied P(cut in H2 2026 | hold at June) ≈ "
              f"{p_cut_after_june_given_hold_june*100:.1f}%")
        print(f"  (Derived: P(no cut all year) / P(hold at June) → "
              f"{poly_no_cut_prob*100:.1f}% / {kalshi_hold_prob*100:.1f}%)")

    # Count-based cut distribution
    if poly_groups["count_cuts"]:
        print(f"\n  Polymarket 2026 cut-count distribution:")
        cum = 0.0
        for r in sorted(poly_groups["count_cuts"], key=lambda x: x.get("n_cuts", 99)):
            p = r["price"] or 0
            cum += p
            label = f"  Exactly {r['n_cuts']} cut(s):" if r["n_cuts"] > 0 else "  No cuts:"
            print(f"    {label:<18} {p*100:5.1f}%   (cumulative ≥0: {cum*100:.1f}%)")


# ---------------------------------------------------------------------------
# Monotonicity check and arb printer
# ---------------------------------------------------------------------------


def verify_clob_leg(
    market_raw: dict,
    side: str,                 # "YES" or "NO"
    min_bid: float = 0.01,
    max_ask: float = 0.99,
    min_depth_usd: float = 100.0,
) -> dict:
    """
    Resolve the correct token for `side` and call verify_clob_liquidity().
    Returns the liquidity dict, plus `token_id` used.
    """
    from polymarket.client import PolymarketClient
    import json as _json

    raw_ids = market_raw.get("clobTokenIds")
    if isinstance(raw_ids, str):
        try:
            raw_ids = _json.loads(raw_ids)
        except Exception:
            raw_ids = []
    outcomes_raw = market_raw.get("outcomes")
    if isinstance(outcomes_raw, str):
        try:
            outcomes_raw = _json.loads(outcomes_raw)
        except Exception:
            outcomes_raw = []

    token_id = None
    for i, outcome in enumerate(outcomes_raw or []):
        if isinstance(outcome, str) and outcome.strip().lower() == side.lower() and i < len(raw_ids or []):
            token_id = raw_ids[i]
            break
    if token_id is None and raw_ids:
        token_id = raw_ids[0] if side.upper() == "YES" else (raw_ids[1] if len(raw_ids) > 1 else raw_ids[0])

    if not token_id:
        return {"token_id": None, "is_liquid": False, "reason": "no token ID found"}

    client = PolymarketClient(timeout=10)
    result = client.verify_clob_liquidity(
        token_id,
        min_bid=min_bid,
        max_ask=max_ask,
        min_depth_usd=min_depth_usd,
    )
    return result


def print_monotonicity_analysis(
    poly_groups: dict,
    raw_markets: list[dict],
    fee_rate: float = 0.02,
    verify_liquidity: bool = True,
    min_depth_usd: float = 100.0,
) -> None:
    print(f"\n{'='*70}")
    print("  INTRA-POLYMARKET MONOTONICITY CHECK")
    print(f"{'='*70}")
    print("  (A ladder violates monotonicity if P(harder event) > P(easier event).")
    print("   All signals are verified against live CLOB depth before being flagged.)")

    # Build a lookup from rate → raw market dict for CLOB verification
    _upper_re = re.compile(r"upper bound reach ([\d.]+)%")
    _lower_re = re.compile(r"lower bound reach ([\d.]+)%")
    upper_mkt_by_rate: dict[float, dict] = {}
    lower_mkt_by_rate: dict[float, dict] = {}
    for m in raw_markets:
        t = (m.get("question") or m.get("title") or "").lower()
        if (mo := _upper_re.search(t)):
            upper_mkt_by_rate[float(mo.group(1))] = m
        elif (mo := _lower_re.search(t)):
            lower_mkt_by_rate[float(mo.group(1))] = m

    any_real_violation = False

    def _check_ladder(ladder, direction, label, mkt_by_rate, side_low, side_high):
        nonlocal any_real_violation
        violations = check_monotonicity(ladder, direction)
        if not violations:
            print(f"\n  {label}: no violations ✓")
            return
        print(f"\n  {label} — {len(violations)} candidate violation(s):")
        for v in violations:
            arb = compute_monotonicity_arb(v, fee_rate)
            print(f"    P({v['rate_low']:.2f}%) = {v['p_low']*100:.2f}%  "
                  f"< P({v['rate_high']:.2f}%) = {v['p_high']*100:.2f}%  "
                  f"[excess: {v['excess']*100:.2f} pp]  "
                  f"→ theoretical net: {arb['net_profit_after_fee']:.4f}")

            if not verify_liquidity:
                if arb["profitable"]:
                    print(f"      *** PROFITABLE (unverified) ***")
                continue

            # Verify live CLOB liquidity on both legs
            low_mkt = mkt_by_rate.get(v["rate_low"])
            high_mkt = mkt_by_rate.get(v["rate_high"])
            if not low_mkt or not high_mkt:
                print(f"      [SKIP] cannot find raw market dict for rate level — skipping CLOB check")
                continue

            print(f"      Verifying live CLOB liquidity…")
            liq_yes_low  = verify_clob_leg(low_mkt,  side_low,  min_depth_usd=min_depth_usd)
            liq_no_high  = verify_clob_leg(high_mkt, side_high, min_depth_usd=min_depth_usd)

            print(f"        {side_low} leg  (rate {v['rate_low']:.2f}%):  "
                  f"bid={liq_yes_low['best_bid']}  ask={liq_yes_low['best_ask']}  "
                  f"depth=${liq_yes_low['ask_size']}  → {'LIQUID ✓' if liq_yes_low['is_liquid'] else 'ILLIQUID ✗ (' + liq_yes_low['reason'] + ')'}")
            print(f"        {side_high} leg (rate {v['rate_high']:.2f}%):  "
                  f"bid={liq_no_high['best_bid']}  ask={liq_no_high['best_ask']}  "
                  f"depth=${liq_no_high['bid_size']}  → {'LIQUID ✓' if liq_no_high['is_liquid'] else 'ILLIQUID ✗ (' + liq_no_high['reason'] + ')'}")

            if liq_yes_low["is_liquid"] and liq_no_high["is_liquid"]:
                # Recompute arb with real prices
                real_yes_ask = liq_yes_low["best_ask"]
                real_no_ask  = round(1.0 - liq_no_high["best_bid"], 6) if liq_no_high["best_bid"] else None
                if real_yes_ask and real_no_ask:
                    real_cost = round(real_yes_ask + real_no_ask, 6)
                    real_profit = round(1.0 - real_cost - fee_rate, 6)
                    if real_profit > 0:
                        any_real_violation = True
                        print(f"        *** REAL ARB: cost={real_cost:.4f}  net_profit={real_profit:.4f} ***")
                    else:
                        print(f"        Real cost={real_cost:.4f} → net_profit={real_profit:.4f} (not profitable at live prices)")
            else:
                print(f"        Ghost market — signal rejected.")

    _check_ladder(
        poly_groups["upper_reach"], "ascending",
        "Upper-bound reach (hike) ladder",
        upper_mkt_by_rate, "YES", "NO",
    )
    _check_ladder(
        poly_groups["lower_reach"], "descending",
        "Lower-bound reach (cut) ladder",
        lower_mkt_by_rate, "YES", "NO",
    )

    if not any_real_violation:
        print("\n  No live-verified profitable arb found.")


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------


def print_kalshi_distribution(dist: list[dict], event_ticker: str) -> None:
    print(f"\n{'='*70}")
    print(f"  KALSHI IMPLIED RATE DISTRIBUTION  —  {event_ticker}")
    print(f"{'='*70}")
    print(f"  {'Upper bound':>12}  {'P(this level)':>14}  {'P(above)':>9}  "
          f"{'YES bid':>8}  {'YES ask':>8}  Ticker")
    print(f"  {'-'*12}  {'-'*14}  {'-'*9}  {'-'*8}  {'-'*8}  {'-'*36}")
    for row in dist:
        rate = f"{row['implied_rate']:.2f}%" if row["implied_rate"] is not None else "?"
        p_level = f"{row['p_this_level']*100:6.2f}%"
        p_above = f"{row['p_above']*100:6.2f}%" if row["p_above"] is not None else "    n/a"
        bid = f"{row['yes_bid']:.4f}" if row["yes_bid"] else "    —"
        ask = f"{row['yes_ask']:.4f}" if row["yes_ask"] else "    —"
        print(f"  {rate:>12}  {p_level:>14}  {p_above:>9}  {bid:>8}  {ask:>8}  {row['ticker']}")


def print_polymarket_ladders(poly_groups: dict) -> None:
    print(f"\n{'='*70}")
    print("  POLYMARKET FED LADDERS  (full-year 2026 / before 2027)")
    print(f"{'='*70}")

    # Upper bound (hikes)
    ub = poly_groups.get("upper_reach", [])
    if ub:
        print("\n  Upper-bound reach (hike scenario) — P(rate rises to X%+ before 2027):")
        prev_p = 1.0
        for r in ub:
            p = r["price"]
            flag = "  ← VIOLATION" if p and p > prev_p else ""
            print(f"    P(reach {r['rate']:.2f}%+) = {p*100:5.2f}%{flag}"
                  if p else f"    P(reach {r['rate']:.2f}%+) = n/a")
            if p:
                prev_p = p

    # Lower bound (cuts)
    lb = poly_groups.get("lower_reach", [])
    if lb:
        print("\n  Lower-bound reach (cut scenario) — P(rate falls to ≤ X% before 2027):")
        prev_p = 1.0
        for r in lb:
            p = r["price"]
            flag = "  ← VIOLATION" if p and p > prev_p else ""
            print(f"    P(≤{r['rate']:.2f}%) = {p*100:5.2f}%{flag}"
                  if p else f"    P(≤{r['rate']:.2f}%) = n/a")
            if p:
                prev_p = p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Fed rate spread analysis across Kalshi and Polymarket.")
    parser.add_argument("--event", default=None,
                        help="Kalshi event ticker, e.g. KXFED-26JUN (auto-detected if omitted)")
    parser.add_argument("--poly-timeout", type=int, default=15)
    parser.add_argument("--kalshi-timeout", type=int, default=20)
    parser.add_argument("--lower-bound", type=float, default=3.50,
                        help="Current Fed lower bound rate (default: 3.50)")
    parser.add_argument("--upper-bound", type=float, default=3.75,
                        help="Current Fed upper bound rate (default: 3.75)")
    parser.add_argument("--fee", type=float, default=0.02,
                        help="Fee rate for arb net profit calculation (default: 0.02)")
    args = parser.parse_args()

    print(f"\nFed Rate Spread Analysis  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Current rate target: {args.lower_bound:.2f}% – {args.upper_bound:.2f}%")

    # --- Kalshi ---
    print("\n[1] Fetching Kalshi KXFED ladder…")
    event_ticker, ladder = fetch_kalshi_fed_ladder(
        event_ticker=args.event,
        timeout=args.kalshi_timeout,
    )
    if not ladder:
        print("  ERROR: No Kalshi KXFED markets found.")
        sys.exit(1)
    print(f"  {len(ladder)} threshold markets for event: {event_ticker}")

    dist = build_kalshi_distribution(ladder)
    print_kalshi_distribution(dist, event_ticker)

    modal = dist[0]
    ev = sum(
        row["implied_rate"] * row["p_this_level"]
        for row in dist if row["implied_rate"] is not None
    )
    print(f"\n  Modal outcome:   {modal['implied_rate']:.2f}%  (P = {modal['p_this_level']*100:.1f}%)")
    print(f"  Expected upper:  {ev:.3f}%")

    # --- Polymarket ---
    print("\n[2] Fetching Polymarket Fed markets…")
    poly_mkts = fetch_polymarket_fed_markets(poly_timeout=args.poly_timeout)

    if not poly_mkts:
        print("\n  Kalshi-only run complete. Run with VPN / on a reachable network for cross-exchange data.")
        return

    poly_groups = classify_polymarket_fed(poly_mkts)
    print_polymarket_ladders(poly_groups)

    # --- Cross-exchange comparison ---
    cross_exchange_summary(
        dist, poly_groups,
        current_lower_bound=args.lower_bound,
        current_upper_bound=args.upper_bound,
    )

    # --- Intra-Polymarket monotonicity (internal spread check) ---
    print_monotonicity_analysis(poly_groups, raw_markets=poly_mkts, fee_rate=args.fee)


if __name__ == "__main__":
    main()

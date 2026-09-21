"""
FastAPI backend for the prediction market arb dashboard.

Run:
    python server.py
or:
    uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import book_arb
from arb import kalshi_taker_fee
from pipeline import OrderBook, PriceLevel

logging.disable(logging.WARNING)

app = FastAPI(title="Pred-Arb Monitor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SIGNALS_FILE = Path(__file__).parent / "signals.jsonl"
STATIC_DIR   = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_signals(n: int = 200, _block: int = 1_000_000) -> list[dict]:
    """Last ``n`` signals, newest first. Reads only the final ``_block`` bytes —
    signals.jsonl is monitor-written append-only with no cap, so reading the whole
    file each request scales badly (same fix as health.py #23, issue #31)."""
    try:
        with open(SIGNALS_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _block))
            data = f.read()
    except Exception:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > _block and lines:
        lines = lines[1:]                 # first line likely partial — drop it
    out = []
    for line in reversed(lines[-n:]):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Book-arb: depth-walked executable arb, exposed for the dashboard's BOOK SCAN
# panel. book_arb.py already computes all of this for the alerter's email; the
# dashboard just never surfaced it. One helper (``_compute_book_arb``) is
# shared by the POST (scan-payload ladders, no network) and GET-live (fresh
# fetch) endpoints below, so the maths lives in exactly one place.
# ---------------------------------------------------------------------------

_BOOK_ARB_BUDGETS = (1000, 2000, 2500, 5000)
_CURVE_POINT_CAP = 40
_LADDER_LEVEL_CAP = 15
_BODY_ELLIPSIS = Body(...)  # module-level singleton — ruff B008 forbids a call default


def _book_from_lists(raw: dict | None) -> OrderBook:
    """``{"bids": [[price,size],...], "asks": [...]}`` -> OrderBook. Used for the
    ladders discover() already embeds in the scan payload (top 30 levels/side)."""
    raw = raw or {}

    def _levels(key: str) -> list[PriceLevel]:
        out = []
        for item in raw.get(key) or []:
            try:
                price, size = item
                out.append(PriceLevel(price=float(price), size=float(size)))
            except (TypeError, ValueError):
                continue
        return out

    return OrderBook(bids=_levels("bids"), asks=_levels("asks"))


def _fill_to_dict(fill: "book_arb.Fill") -> dict:
    return {
        "contracts": fill.contracts,
        "vwap_a":    fill.vwap_a,
        "vwap_b":    fill.vwap_b,
        "cost_a":    fill.cost_a,
        "cost_b":    fill.cost_b,
        "profit":    fill.profit,
        "roi":       fill.roi,
    }


def _breakeven_contracts(curve: list[tuple[float, float, float, float]]) -> float:
    """Cumulative contracts at the last curve point still under $1.00 combined
    cost — i.e. exactly how deep the book can be walked before the arb dies.
    0.0 when even the first (best-priced) chunk is already >= $1.00."""
    breakeven = 0.0
    for cum_contracts, _price_a, _price_b, combined_cost_with_fee in curve:
        if combined_cost_with_fee < 1.0:
            breakeven = cum_contracts
    return breakeven


def _direction_result(poly_book: OrderBook, kalshi_book: OrderBook, direction: str,
                       budgets: tuple[float, ...]) -> dict:
    side_a, side_b, kalshi_leg = book_arb._DIRECTIONS[direction]
    result = book_arb.executable_arb(poly_book, kalshi_book, direction, budgets=budgets)
    leg_a, leg_b = result["ladders"]

    top_of_book_net = None
    if leg_a and leg_b:
        # Same derivation build_buy_ladder already applies (NO price = 1 - yes
        # bid) — comparing this to ``max.profit``/``by_budget`` is the whole
        # point of the panel: top-of-book overstates what actually fills.
        k_top_price = leg_a[0][0] if kalshi_leg == "A" else leg_b[0][0]
        top_of_book_net = round(1.0 - leg_a[0][0] - leg_b[0][0] - kalshi_taker_fee(k_top_price), 6)

    curve = book_arb.cumulative_curve(leg_a, leg_b, kalshi_leg)
    breakeven_contracts = _breakeven_contracts(curve)
    step = max(1, -(-len(curve) // _CURVE_POINT_CAP)) if curve else 1  # ceil div
    curve_points = [
        {"cum_contracts": c, "price_a": pa, "price_b": pb, "combined_cost_with_fee": cost}
        for c, pa, pb, cost in curve[::step][:_CURVE_POINT_CAP]
    ]

    return {
        "direction":           direction,
        "kalshi_leg":          kalshi_leg,
        "max":                 _fill_to_dict(result["max"]),
        "by_budget":           {str(b): _fill_to_dict(f) for b, f in result["by_budget"].items()},
        "top_of_book_net":     top_of_book_net,
        "breakeven_contracts": breakeven_contracts,
        "curve":               curve_points,
        "ladders": {
            "leg_a": [{"price": p, "size": s} for p, s in leg_a[:_LADDER_LEVEL_CAP]],
            "leg_b": [{"price": p, "size": s} for p, s in leg_b[:_LADDER_LEVEL_CAP]],
        },
    }


def _compute_book_arb(poly_book: OrderBook, kalshi_book: OrderBook,
                      budgets: tuple[float, ...] | None = None) -> dict:
    """Both directions' full executable picture — the shared computation behind
    /api/book-arb and /api/book-arb/live. Never raises for empty/thin books
    (book_arb's Fill defaults to zeros); callers still guard venue fetch errors."""
    budgets = tuple(budgets) if budgets else _BOOK_ARB_BUDGETS
    directions = {
        d: _direction_result(poly_book, kalshi_book, d, budgets)
        for d in book_arb._DIRECTIONS
    }
    best_direction = max(directions, key=lambda d: directions[d]["max"]["profit"])
    return {"directions": directions, "best_direction": best_direction}


# ---------------------------------------------------------------------------
# Scan summary — WHICH PAIRS HAVE ARBITRAGE and where the alpha is.
#
# The dashboard historically showed "ARB 0/1" on scans of 8,000+ pairs for two
# COMPOUNDING reasons, not one:
#   1. discover.py's legacy arb_net_profit/arb_eligible path used a flat 7c
#      fee (today's real Kalshi taker fee, 0.07*p*(1-p), peaks at 1.75c and
#      is usually much less) — see arb.kalshi_taker_fee.
#   2. arb_eligible (the field the dashboard's ARB column has always used) is
#      a much STRICTER filter than the one that actually drives alerter
#      emails. The alerter (alerter.compute_signals) gates on
#      ``v2_match is True and not settlement_risk`` — it does not consult
#      arb_eligible at all. On a real 7,903-pair scan: arb_eligible passed
#      only 1,193 pairs (11 positive under the accurate fee, 1 under flat7),
#      while the alerter gate passed 7,430 (628 positive accurate, 54 flat7).
#
# So the summary below reports the funnel PER GATE — all pairs, arb_eligible
# (labelled as the dashboard's historical ARB column), and the alerter gate
# (labelled as the population that actually drives alerts) — rather than a
# single chain, so the UI can show why the old on-screen number looked tiny
# without implying there was no arbitrage.
# ---------------------------------------------------------------------------

_EDGE_THRESHOLDS = (("above_1c", 0.01), ("above_3c", 0.03), ("above_5c", 0.05), ("above_10c", 0.10))

# A net edge above ~10c on a $1 binary means the two venues are pricing the
# SAME contract ~10+ cents apart after fees — on liquid, correctly-matched
# markets that gap gets arbed away in minutes. In practice an edge this large
# almost always means a stale/dead book (top-of-book hasn't moved in a while)
# or a mismatched pair (the matcher paired two different contracts), not a
# real opportunity. A real 7,903-pair scan measured 58 alerter-gate positive
# pairs above this threshold accounting for 73% of raw "executable" dollars
# (net edges up to +0.96, i.e. a ~35c combined cost for a $1 payout) — headline
# executable figures must exclude these or they actively mislead.
PLAUSIBLE_EDGE_MAX = 0.10


def is_alerter_gate(pair: dict) -> bool:
    """True iff this pair would survive alerter.compute_signals's first two
    filters (independent v2 match confirmed, no settlement ambiguity). Kept
    as its own function — rather than inlined in build_summary — so a test
    can pin it against alerter.compute_signals's own gating and catch drift."""
    return pair.get("v2_match") is True and not pair.get("settlement_risk")


def _funnel_bucket(pairs: list[dict]) -> dict:
    """Priced/positive/edge-threshold counts for one population of pairs.

    ``priced`` = has a non-None arb_net_accurate (see discover.py's comment on
    why None means "unpriced", not "unprofitable"). All threshold counts are
    on the ACCURATE fee number, per the brief.
    """
    priced = [p for p in pairs if p.get("arb_net_accurate") is not None]
    bucket = {
        "total": len(pairs),
        "priced": len(priced),
        "positive_accurate": sum(1 for p in priced if p["arb_net_accurate"] > 0),
        "positive_flat7": sum(
            1 for p in pairs
            if p.get("arb_net_flat7") is not None and p["arb_net_flat7"] > 0
        ),
    }
    for key, threshold in _EDGE_THRESHOLDS:
        bucket[key] = sum(1 for p in priced if p["arb_net_accurate"] > threshold)
    return bucket


def _group_stats(pairs: list[dict], key_fn) -> dict:
    """{group_key: {pairs, priced, positive, positive_plausible, best_edge,
    exec_profit}} for one grouping of an already-priced population (used for
    by_category/by_source). ``key_fn`` derives the group key from a pair —
    never mutates the pairs.

    ``exec_profit`` is summed over the PLAUSIBLE band only (0 < net <=
    PLAUSIBLE_EDGE_MAX) — same population as the headline executable.profit —
    so summing this column across groups reproduces the headline number
    instead of contradicting it with the raw (implausible-inflated) total.
    ``positive`` stays a raw count of net > 0 (a different question: "how many
    pairs look positive" vs "how many dollars are plausibly real"); a group
    can show many raw positives and few plausible dollars — e.g. a category
    dominated by stale-book artifacts — which is exactly what
    ``positive_plausible`` next to it is meant to surface.
    """
    out: dict = {}
    for p in pairs:
        group = key_fn(p)
        stats = out.setdefault(group, {
            "pairs": 0, "priced": 0, "positive": 0, "positive_plausible": 0,
            "best_edge": None, "exec_profit": 0.0,
        })
        stats["pairs"] += 1
        net = p.get("arb_net_accurate")
        if net is not None:
            stats["priced"] += 1
            if net > 0:
                stats["positive"] += 1
                if net <= PLAUSIBLE_EDGE_MAX:
                    stats["positive_plausible"] += 1
                    stats["exec_profit"] += p.get("exec_profit") or 0.0
            if stats["best_edge"] is None or net > stats["best_edge"]:
                stats["best_edge"] = net
    for stats in out.values():
        stats["exec_profit"] = round(stats["exec_profit"], 4)
    return out


def _plausibility_bucket(pairs: list[dict]) -> dict:
    """{pairs, with_depth, exec_profit} for one plausibility band. with_depth
    counts pairs where the book_arb walk actually ran (exec_profit is not
    None) — i.e. live ladders were available, not just a top-of-book quote."""
    return {
        "pairs":      len(pairs),
        "with_depth": sum(1 for p in pairs if p.get("exec_profit") is not None),
        "exec_profit": round(sum(p.get("exec_profit") or 0.0 for p in pairs), 4),
    }


def _plausibility_split(positive_pairs: list[dict]) -> dict:
    """Splits an already-positive (arb_net_accurate > 0) population into
    PLAUSIBLE (<= PLAUSIBLE_EDGE_MAX) and IMPLAUSIBLE (above it) — see the
    module comment above PLAUSIBLE_EDGE_MAX for why the split exists."""
    plausible   = [p for p in positive_pairs if p["arb_net_accurate"] <= PLAUSIBLE_EDGE_MAX]
    implausible = [p for p in positive_pairs if p["arb_net_accurate"] > PLAUSIBLE_EDGE_MAX]
    return {
        "plausible":   _plausibility_bucket(plausible),
        "implausible": _plausibility_bucket(implausible),
    }


def _top_rows(pairs: list[dict], sort_key) -> list[dict]:
    top = sorted(pairs, key=sort_key)[:15]
    return [{
        "poly_title":    p.get("poly_title"),
        "kalshi_title":  p.get("kalshi_title"),
        "category":      p.get("category"),
        "arb_net_accurate": p.get("arb_net_accurate"),
        "arb_net_flat7":    p.get("arb_net_flat7"),
        "exec_contracts":   p.get("exec_contracts"),
        "exec_profit":      p.get("exec_profit"),
        "settlement_risk":  p.get("settlement_risk"),
        # Identity fields (not display data) so the dashboard can jump a
        # clicked top-15 row to the matching row in LIVE PAIRS.
        "poly_id":       p.get("poly_id"),
        "kalshi_ticker": p.get("kalshi_ticker"),
    } for p in top]


def build_summary(pairs: list[dict]) -> dict:
    """Scan-payload summary: which pairs have arbitrage and where the alpha
    is. Pure function of the discover() row list — testable without a scan."""
    all_pairs = pairs
    arb_eligible_pairs = [p for p in pairs if p.get("arb_eligible")]
    alerter_gate_pairs = [p for p in pairs if is_alerter_gate(p)]

    funnel = {
        "all":          _funnel_bucket(all_pairs),
        "arb_eligible": _funnel_bucket(arb_eligible_pairs),
        "alerter_gate": _funnel_bucket(alerter_gate_pairs),
    }
    fee_model_delta = {
        gate: {"flat7_positive": b["positive_flat7"], "accurate_positive": b["positive_accurate"]}
        for gate, b in funnel.items()
    }

    # by_category / by_source computed over the alerter-gate population — the
    # population that actually drives alerts, per the brief's headline call.
    by_category = _group_stats(alerter_gate_pairs, lambda p: p.get("category") or "?")
    by_source = _group_stats(
        alerter_gate_pairs,
        lambda p: "sports" if p.get("match_source") == "sports" else "text",
    )

    positive_alerter = [p for p in alerter_gate_pairs
                         if p.get("arb_net_accurate") is not None and p["arb_net_accurate"] > 0]

    plausibility = _plausibility_split(positive_alerter)
    plausible_pairs   = [p for p in positive_alerter if p["arb_net_accurate"] <= PLAUSIBLE_EDGE_MAX]
    implausible_pairs = [p for p in positive_alerter if p["arb_net_accurate"] > PLAUSIBLE_EDGE_MAX]

    # HEADLINE executable figures cover the PLAUSIBLE band only — a net edge
    # above PLAUSIBLE_EDGE_MAX is a stale-book/mismatch signal, not alpha, and
    # summing it into "executable profit" would actively mislead (a real scan
    # put 73% of raw exec dollars in 58 such pairs). The implausible total is
    # still reported, explicitly labelled, never silently dropped.
    executable = {
        "contracts": round(sum(p.get("exec_contracts") or 0.0 for p in plausible_pairs), 4),
        "profit":    round(sum(p.get("exec_profit") or 0.0 for p in plausible_pairs), 4),
        "implausible_contracts": round(sum(p.get("exec_contracts") or 0.0 for p in implausible_pairs), 4),
        "implausible_profit":    round(sum(p.get("exec_profit") or 0.0 for p in implausible_pairs), 4),
    }

    # TOP 15: within the plausible band, ranked by realized exec_profit (what
    # can actually be deployed), not raw net edge — raw-edge ranking is
    # exactly what surfaced stale-book artifacts (Gears of War/CoD/GTA VI at
    # ~0.92-0.96 net) at the top of the list. A second short list keeps the
    # implausible pairs visible as a matcher review queue, per how this repo
    # has always treated them, ranked by edge (the thing that flagged them).
    top_rows = _top_rows(plausible_pairs, lambda p: -(p.get("exec_profit") or 0.0))
    top_implausible_rows = _top_rows(implausible_pairs, lambda p: -p["arb_net_accurate"])

    books_live_n = sum(1 for p in all_pairs if p.get("books_live"))
    settlement_flagged = sum(1 for p in all_pairs if p.get("settlement_risk"))
    ae = funnel["arb_eligible"]
    ag = funnel["alerter_gate"]
    caveats = [
        f"Live order books were fetched for {books_live_n} of {len(all_pairs)} pairs this scan "
        "(the rest kept catalog snapshot prices).",
        "Settlement verification (contract-level, per-venue) is separate from this summary; "
        "a pair with no settlement_risk flag can still fail it.",
        f"{settlement_flagged} pairs carry a non-empty settlement_risk flag and are excluded "
        "from the alerter-gate population above.",
        f"ARB-ELIGIBLE (the dashboard's historical ARB column) showed only "
        f"{ae['positive_accurate']} positive pairs out of {ae['total']} — a strict eligibility "
        "filter plus (previously) a conservative flat 7c fee, not an absence of arbitrage. "
        f"ALERTER-GATE, the population that actually drives alerts, shows {ag['positive_accurate']} "
        f"positive pairs out of {ag['total']}.",
    ]
    impl = plausibility["implausible"]
    plaus = plausibility["plausible"]
    total_raw_exec = round(plaus["exec_profit"] + impl["exec_profit"], 4)
    if impl["pairs"]:
        impl_share_pct = round(impl["exec_profit"] / total_raw_exec * 100) if total_raw_exec else 0
        caveats.append(
            f"{impl['pairs']} alerter-gate positive pairs have an implausible net edge above "
            f"{PLAUSIBLE_EDGE_MAX*100:.0f}c and account for ${impl['exec_profit']:.2f} "
            f"({impl_share_pct}%) of the ${total_raw_exec:.2f} raw executable total — these read as "
            "stale books or mismatched pairs, not arbitrage, and are excluded from the headline "
            f"executable figure (${plaus['exec_profit']:.2f}, {plaus['pairs']} pairs)."
        )

    return {
        "funnel": funnel,
        "fee_model_delta": fee_model_delta,
        "by_category": by_category,
        "by_source": by_source,
        "plausibility": plausibility,
        "executable": executable,
        "top": top_rows,
        "top_implausible": top_implausible_rows,
        "caveats": caveats,
    }


# discover() ingests the full Kalshi/Polymarket open-market catalogs by
# default (100% coverage — see discover.ingest_kalshi / ingest_polymarket),
# each in a handful of seconds via cursor pagination, so no event cap or
# horizon is needed here. market_sweep=False below skips the ~290s Polymarket
# orphan sweep for interactive dashboard requests; run discover.py directly
# (or the alerter, which caches the sweep) to include the ~480 orphan markets.
_DEFAULT_SCAN_DAYS = None
_DEFAULT_MAX_EVENTS = None


def _run_scan(
    category: str = "all",
    min_sim: float = 0.30,
    max_events: int | None = _DEFAULT_MAX_EVENTS,
    show_prices: bool = True,
    days: int | None = _DEFAULT_SCAN_DAYS,
) -> dict:
    t0 = time.time()
    try:
        from discover import discover
        pairs = discover(
            category=category,
            days=days,
            min_sim=min_sim,
            show_prices=show_prices,
            max_events_to_search=max_events,
            market_sweep=False,
        )
    except Exception as exc:
        return {"error": str(exc), "pairs": [], "elapsed": 0}

    elapsed = round(time.time() - t0, 1)
    return {
        "pairs": pairs,
        "elapsed": elapsed,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "count": len(pairs),
        "arb_count": sum(1 for p in pairs if (p.get("arb_net_profit") or 0) > 0),
        "summary": build_summary(pairs),
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/scan")
def api_scan(
    category: str = "all",
    min_sim: float = 0.30,
    max_events: int | None = _DEFAULT_MAX_EVENTS,
    days: int | None = _DEFAULT_SCAN_DAYS,
):
    """Run a full organic discover scan and return matched pairs."""
    return JSONResponse(_run_scan(category=category, min_sim=min_sim,
                                  max_events=max_events, show_prices=True, days=days))


@app.get("/api/scan/fast")
def api_scan_fast(
    category: str = "all",
    max_events: int | None = _DEFAULT_MAX_EVENTS,
    days: int | None = _DEFAULT_SCAN_DAYS,
):
    """Quick scan — no live orderbook enrichment, uses catalog mid-prices only."""
    return JSONResponse(_run_scan(category=category, show_prices=False,
                                  max_events=max_events, days=days))


@app.get("/api/signals")
def api_signals(n: int = 100):
    """Return the last n entries from signals.jsonl."""
    return JSONResponse({"signals": _load_signals(n)})


@app.post("/api/book-arb")
def api_book_arb(payload: dict = _BODY_ELLIPSIS):
    """Depth-walked executable arb for one matched pair, using the ladders the
    scan payload already carries (discover.py, ``poly_book``/``kalshi_book``,
    top 30 levels/side) — no network call, so this returns instantly. This is
    the calc book_arb.py already does for the alerter's email; the dashboard
    never showed it."""
    poly_book = _book_from_lists(payload.get("poly_book"))
    kalshi_book = _book_from_lists(payload.get("kalshi_book"))
    budgets = payload.get("budgets") or None
    return JSONResponse(_compute_book_arb(poly_book, kalshi_book, budgets))


@app.get("/api/book-arb/live")
def api_book_arb_live(kalshi_ticker: str, poly_token_id: str):
    """Same computation as /api/book-arb, but sourced from FRESH order books —
    needed because a FAST SCAN (/api/scan/fast) fetches no books at all, so the
    scan payload has nothing for /api/book-arb to walk. Venue errors come back
    as {"error": ...} rather than a 500, matching _run_scan's error shape."""
    try:
        from kalshi.client import KalshiClient
        from pipeline import _parse_kalshi_full_book, _parse_polymarket_book
        from polymarket.client import PolymarketClient

        kalshi_raw = KalshiClient().get_orderbook(kalshi_ticker)
        poly_raw = PolymarketClient().get_orderbook(poly_token_id)
        poly_book = _parse_polymarket_book(poly_raw)
        kalshi_book = _parse_kalshi_full_book(kalshi_raw)
    except Exception as exc:
        return JSONResponse({"error": str(exc)})

    return JSONResponse(_compute_book_arb(poly_book, kalshi_book))


@app.get("/api/status")
def api_status():
    """Connectivity check for both exchanges."""
    results: dict[str, Any] = {}

    try:
        from kalshi.client import KalshiClient
        kc = KalshiClient(timeout=8)
        resp = kc.get_markets(limit=1)
        results["kalshi"] = "ok" if resp.get("markets") else "empty"
    except Exception as exc:
        results["kalshi"] = f"error: {exc}"

    try:
        from polymarket.client import PolymarketClient
        pc = PolymarketClient(timeout=8)
        mkts = pc.get_markets(limit=1)
        results["polymarket"] = "ok" if mkts else "empty"
    except Exception as exc:
        results["polymarket"] = f"error: {exc}"

    results["ts"] = datetime.now(timezone.utc).isoformat()
    return JSONResponse(results)


# ---------------------------------------------------------------------------
# Serve the frontend
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

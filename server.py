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

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import ask
import ask_provider
import ask_tools
import book_arb
import evidence
import review_queue
import rule_search
import scan_jobs
import store
from arb import kalshi_taker_fee
from pipeline import OrderBook, PriceLevel
from rule_diff import compare_rules

logging.disable(logging.WARNING)
logger = logging.getLogger(__name__)

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

_PRUNE_KEEP_SCANS = 10


@app.on_event("startup")
def _startup() -> None:
    store.init_db()


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


def _execute_scan(
    scan_id: int | None,
    category: str = "all",
    min_sim: float = 0.30,
    max_events: int | None = _DEFAULT_MAX_EVENTS,
    show_prices: bool = True,
    days: int | None = _DEFAULT_SCAN_DAYS,
) -> dict:
    """Runs discover() and persists the result against an ALREADY-CREATED
    scan row (``scan_id``, from ``store.start_scan`` — or None if that failed
    / isn't wanted). Split out of ``_run_scan`` (Phase 2c) so the background
    job runner (scan_jobs.py) and the legacy synchronous endpoints
    (``/api/scan``, ``/api/scan/fast``) share one code path: the job runner
    creates its own scan row up front (so it can report the id before the
    scan finishes) and calls this directly; ``_run_scan`` below still does
    both steps itself for callers that don't need a job."""
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
        # Note: scan_jobs.ScanCancelled (the job runner's cooperative-
        # cancellation signal, raised from its stdout proxy while discover()
        # is running — see scan_jobs.py) is a BaseException, NOT an Exception
        # subclass, specifically so this generic handler can't catch it: it
        # must propagate all the way up to the job worker uncaught, the same
        # way KeyboardInterrupt would, so the job/store row land in
        # 'cancelled' rather than being caught here and reported as 'failed'.
        # discover.py has ~19 bare `except Exception:` blocks around its own
        # internal calls; a plain Exception subclass would risk being
        # swallowed by one of those on its way out.
        if scan_id is not None:
            try:
                store.fail_scan(scan_id, str(exc))
            except Exception:
                logger.warning("store.fail_scan failed", exc_info=True)
        return {"error": str(exc), "pairs": [], "elapsed": 0}

    elapsed = round(time.time() - t0, 1)
    summary = build_summary(pairs)
    result = {
        "pairs": pairs,
        "elapsed": elapsed,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "count": len(pairs),
        "arb_count": sum(1 for p in pairs if (p.get("arb_net_profit") or 0) > 0),
        "summary": summary,
    }

    persisted = False
    if scan_id is not None:
        try:
            store.finish_scan(scan_id, pairs, summary)
            store.prune_scans(keep=_PRUNE_KEEP_SCANS)
            persisted = True
        except Exception:
            logger.warning("store.finish_scan failed; scan result not persisted", exc_info=True)

    result["scan_id"] = scan_id if persisted else None
    result["persisted"] = persisted
    return result


def _run_scan(
    category: str = "all",
    min_sim: float = 0.30,
    max_events: int | None = _DEFAULT_MAX_EVENTS,
    show_prices: bool = True,
    days: int | None = _DEFAULT_SCAN_DAYS,
) -> dict:
    mode = "full" if show_prices else "fast"
    params = {
        "category": category, "min_sim": min_sim, "max_events": max_events,
        "show_prices": show_prices, "days": days,
    }

    scan_id: int | None = None
    try:
        scan_id = store.start_scan(mode, params)
    except Exception:
        logger.warning("store.start_scan failed; scan will not be persisted", exc_info=True)

    return _execute_scan(scan_id, category=category, min_sim=min_sim,
                          max_events=max_events, show_prices=show_prices, days=days)


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


def _ensure_summary(scan: dict) -> None:
    """A stored scan without a usable summary (older rows, failed summary
    build) gets one rebuilt from its pairs, so the dashboard can always render."""
    if not (scan.get("summary") or {}).get("funnel"):
        scan["summary"] = build_summary(scan["pairs"])


@app.get("/api/scans/latest")
def api_scans_latest(mode: str | None = None):
    """Latest completed scan, with pairs and a reviews map keyed by pair_key
    for every pair in that scan."""
    scan = store.latest_scan(mode=mode)
    if scan is None:
        return JSONResponse({"error": "no completed scan"}, status_code=404)
    _ensure_summary(scan)
    keys = [store.pair_key(p) for p in scan["pairs"]]
    scan["reviews"] = store.current_reviews(keys)
    return JSONResponse(scan)


@app.get("/api/scans")
def api_scans(limit: int = 20):
    """Scan metadata only (no pairs) for the scan history list."""
    return JSONResponse({"scans": store.list_scans(limit=limit)})


@app.post("/api/scans")
def api_scans_start(payload: dict = _BODY_ELLIPSIS):
    """Starts a background scan job (Phase 2c, docs/FRONTEND_REVIEW.md
    finding 6) instead of blocking the request for the 10-20 minutes a full
    scan takes. Single-flight: if a job is already running, that job is
    returned unchanged with ``started: False`` rather than starting a
    second concurrent scan — 202 when a new job was started, 200 when an
    existing one was returned, 422 on an invalid mode."""
    mode = payload.get("mode")
    if mode not in ("full", "fast"):
        return JSONResponse({"error": "mode must be 'full' or 'fast'"}, status_code=422)
    params = {
        "category": payload.get("category", "all"),
        "min_sim": payload.get("min_sim", 0.30),
        "max_events": payload.get("max_events"),
        "days": payload.get("days"),
    }
    result = scan_jobs.start(mode, params)
    return JSONResponse(result, status_code=202 if result["started"] else 200)


@app.get("/api/scans/job")
def api_scans_job():
    """Current or most-recently-finished background scan job. 404 if no job
    has been started since process startup. Registered ahead of
    ``/api/scans/{scan_id}`` so the literal path 'job' is matched here first
    rather than being parsed as a scan id."""
    job = scan_jobs.status()
    if job is None:
        return JSONResponse({"error": "no scan job"}, status_code=404)
    return JSONResponse(job)


@app.post("/api/scans/job/cancel")
def api_scans_job_cancel():
    """Requests cancellation of the currently-running scan job (cooperative —
    see scan_jobs.py). 409 if no job is currently running."""
    job = scan_jobs.cancel()
    if job is None:
        return JSONResponse({"error": "no scan running"}, status_code=409)
    return JSONResponse(job)


@app.get("/api/scans/{scan_id}")
def api_scan_by_id(scan_id: int):
    scan = store.get_scan(scan_id)
    if scan is None:
        return JSONResponse({"error": "scan not found"}, status_code=404)
    _ensure_summary(scan)
    return JSONResponse(scan)


@app.post("/api/reviews")
def api_add_review(payload: dict = _BODY_ELLIPSIS):
    """Records a human review verdict for a pair. Append-only: this always
    inserts a new row, never overwrites a prior review of the same pair."""
    pair_key_ = payload.get("pair_key")
    verdict = payload.get("verdict")
    reason = payload.get("reason")
    if not pair_key_ or not isinstance(pair_key_, str):
        return JSONResponse({"error": "pair_key is required"}, status_code=422)
    if verdict not in ("match", "mismatch", "uncertain"):
        return JSONResponse(
            {"error": "verdict must be one of match, mismatch, uncertain"},
            status_code=422,
        )
    # Phase 2d: stamp the review with the rule-evidence hashes current at
    # review time (if any evidence has ever been cached for this pair's two
    # markets), so a later rules change can be detected via
    # store.review_is_stale without re-fetching anything. Behavior is
    # unchanged when no evidence is cached -- both hashes stay None, exactly
    # as store.add_review already defaults them.
    kalshi_rules_hash = None
    poly_rules_hash = None
    pair = store.find_pair(pair_key_)
    if pair is not None:
        if pair.get("kalshi_ticker"):
            kalshi_evidence = store.latest_evidence("kalshi", pair["kalshi_ticker"])
            if kalshi_evidence is not None:
                kalshi_rules_hash = kalshi_evidence["content_hash"]
        if pair.get("poly_id"):
            poly_evidence = store.latest_evidence("polymarket", pair["poly_id"])
            if poly_evidence is not None:
                poly_rules_hash = poly_evidence["content_hash"]
    review = store.add_review(
        pair_key_, verdict, reason,
        kalshi_rules_hash=kalshi_rules_hash, poly_rules_hash=poly_rules_hash,
    )
    return JSONResponse(review)


def _with_stale(review: dict) -> dict:
    """Adds a purely additive "stale" field (Phase 2d) -- True when the
    review's stored rule-evidence hash no longer matches the latest cached
    evidence for that pair's markets. Never changes any existing field."""
    return {**review, "stale": store.review_is_stale(review)}


@app.get("/api/reviews")
def api_reviews(pair_key: str | None = None):
    """History for one pair (``pair_key`` given) or the current-verdict map
    for up to 500 pairs (no ``pair_key``)."""
    if pair_key is not None:
        history = [_with_stale(r) for r in store.review_history(pair_key)]
        return JSONResponse({"reviews": history})
    current = {k: _with_stale(r) for k, r in store.current_reviews().items()}
    return JSONResponse(current)


@app.get("/api/review-queue")
def api_review_queue(top_n: int = 25, min_size: float = 20.0, mode: str | None = "full"):
    """Ranks the latest completed scan's pairs with the same economics and
    sort key as ``tools.audit_ranked_signals.run()`` (Phase 2e, see
    docs/IMPLEMENTATION_PLAN.md's "Review Queue tab" / "Yield columns"
    tasks). No network calls -- this is a pure re-rank of pairs the scan
    already fetched and priced. 404 if no completed scan of ``mode`` exists;
    422 for a non-positive ``top_n``/``min_size``.
    """
    if top_n <= 0 or min_size <= 0:
        return JSONResponse(
            {"error": "top_n and min_size must both be positive"}, status_code=422,
        )
    scan = store.latest_scan(mode=mode)
    if scan is None:
        return JSONResponse({"error": "no completed scan"}, status_code=404)

    rows = review_queue.build_review_queue_rows(
        scan["pairs"], top_n=top_n, min_size=min_size,
    )
    reviews = store.current_reviews([row["pair_key"] for row in rows])
    full_index = review_queue._full_pair_index(scan["pairs"])
    for row in rows:
        review = reviews.get(row["pair_key"])
        pair = full_index.get(f"{row.get('poly_id')}|{row.get('kalshi_ticker')}")
        stale = review_queue.review_is_stale(review, pair)
        if review is not None:
            review = _with_stale(review)
            stale = stale or bool(review["stale"])
        row["review"] = review
        row["review_stale"] = stale

    return JSONResponse({
        "scan_id": scan["id"],
        "scan_finished_at": scan.get("finished_at"),
        "parameters": {"top_n": top_n, "min_size": min_size, "mode": mode},
        "rows": rows,
    })


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
# Phase 2d: settlement-rule evidence + rule diff. NEW routes only (additive) --
# see evidence.py / rule_diff.py for the fetch/cache and comparison logic.
# ---------------------------------------------------------------------------

_RULE_FLAGS_BATCH_CAP = 25


def _venue_evidence_payload(evidence_row: dict | None) -> dict | None:
    if evidence_row is None:
        return None
    return {
        "rules_text": evidence_row.get("rules_text"),
        "source_url": evidence_row.get("source_url"),
        "fetched_at": evidence_row.get("fetched_at"),
        "hash": evidence_row.get("content_hash"),
        "stale": bool(evidence_row.get("stale", False)),
    }


def pair_evidence_payload(pair_key: str, refresh: bool = False) -> dict | None:
    """Shared body of the evidence route (also used by ask_tools). None when
    the pair_key is unknown."""
    pair = store.find_pair(pair_key)
    if pair is None:
        return None

    kalshi_ticker = pair.get("kalshi_ticker")
    poly_id = pair.get("poly_id")
    max_age_s = 0 if refresh else evidence.DEFAULT_MAX_AGE_S
    kalshi_evidence = (
        evidence.get_evidence("kalshi", kalshi_ticker, max_age_s=max_age_s, fetch=True)
        if kalshi_ticker else None
    )
    poly_evidence = (
        evidence.get_evidence("polymarket", poly_id, max_age_s=max_age_s, fetch=True)
        if poly_id else None
    )

    kalshi_text = kalshi_evidence.get("rules_text") if kalshi_evidence else None
    poly_text = poly_evidence.get("rules_text") if poly_evidence else None
    rule_flags = compare_rules(kalshi_text, poly_text)

    return {
        "pair_key": pair_key,
        "kalshi": _venue_evidence_payload(kalshi_evidence),
        "poly": _venue_evidence_payload(poly_evidence),
        "rule_flags": rule_flags,
    }


@app.get("/api/pairs/{pair_key:path}/evidence")
def api_pair_evidence(pair_key: str, refresh: bool = False):
    """Settlement-rule evidence for both legs of a pair, plus rule_diff
    conflict flags. Fetches from the venue APIs only if nothing is cached
    yet (or ``refresh=true``); otherwise serves the cached rows."""
    payload = pair_evidence_payload(pair_key, refresh)
    if payload is None:
        return JSONResponse({"error": "pair not found"}, status_code=404)
    return JSONResponse(payload)


@app.get("/api/evidence/search")
def api_evidence_search(q: str = "", limit: int = 20, venue: str | None = None):
    """Full-text search over cached settlement-rule text (Phase 4a)."""
    if len(q) > rule_search.MAX_QUERY_CHARS:
        raise HTTPException(status_code=422, detail="q too long (max 200 chars)")
    limit = max(1, min(int(limit), 50))
    try:
        results = rule_search.search(q, limit=limit, venue=venue)
    except rule_search.SearchUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"query": q, "results": results})


@app.post("/api/evidence/rule-flags")
def api_evidence_rule_flags(payload: dict = _BODY_ELLIPSIS):
    """Batch rule-flag lookup for the dashboard's pair list. For each
    ``pair_key``: null means evidence isn't cached for it yet (unknown, not
    "no conflict"); an empty list means evidence is cached and compared with
    no flags found. With ``fetch: true``, fetches evidence (live) for up to
    ``limit`` (capped at 25) pairs missing a cache entry before comparing."""
    pair_keys = payload.get("pair_keys") or []
    should_fetch = bool(payload.get("fetch"))
    limit = min(int(payload.get("limit") or _RULE_FLAGS_BATCH_CAP), _RULE_FLAGS_BATCH_CAP)

    flags: dict[str, list[dict] | None] = {}
    fetched_so_far = 0
    for pair_key in pair_keys:
        pair = store.find_pair(pair_key)
        if pair is None:
            flags[pair_key] = None
            continue
        kalshi_ticker = pair.get("kalshi_ticker")
        poly_id = pair.get("poly_id")

        kalshi_cached = store.latest_evidence("kalshi", kalshi_ticker) if kalshi_ticker else None
        poly_cached = store.latest_evidence("polymarket", poly_id) if poly_id else None
        missing = kalshi_cached is None or poly_cached is None

        if missing and should_fetch and fetched_so_far < limit:
            if kalshi_ticker and kalshi_cached is None:
                kalshi_cached = evidence.get_evidence("kalshi", kalshi_ticker)
            if poly_id and poly_cached is None:
                poly_cached = evidence.get_evidence("polymarket", poly_id)
            fetched_so_far += 1
            missing = kalshi_cached is None or poly_cached is None

        if missing:
            flags[pair_key] = None
            continue

        kalshi_text = kalshi_cached.get("rules_text") if kalshi_cached else None
        poly_text = poly_cached.get("rules_text") if poly_cached else None
        flags[pair_key] = compare_rules(kalshi_text, poly_text)

    return JSONResponse({"flags": flags})


# ---------------------------------------------------------------------------
# Phase 3: Ask assistant (tool-calling, read-only). See ask.py / ask_tools.py.
# ---------------------------------------------------------------------------

@app.get("/api/ask/tools")
def api_ask_tools():
    return JSONResponse({"tools": ask_tools.TOOLS})


@app.post("/api/ask")
def api_ask(payload: Any = _BODY_ELLIPSIS):
    provider = ask_provider.get_provider()
    if provider is None:
        return JSONResponse({"error": "Ask is not configured"}, status_code=503)
    try:
        question, history = ask.validate_request(payload)
    except ask.AskValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse(ask.run_ask(question, history, provider))


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

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

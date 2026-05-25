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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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

def _load_signals(n: int = 200) -> list[dict]:
    if not SIGNALS_FILE.exists():
        return []
    lines = SIGNALS_FILE.read_text().strip().splitlines()
    out = []
    for line in reversed(lines[-n:]):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _run_scan(
    category: str = "all",
    min_sim: float = 0.30,
    max_events: int | None = None,
    show_prices: bool = True,
) -> dict:
    t0 = time.time()
    try:
        from discover import discover
        pairs = discover(
            category=category,
            days=None,
            min_sim=min_sim,
            show_prices=show_prices,
            max_events_to_search=max_events,
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
    max_events: int | None = None,
):
    """Run a full organic discover scan and return matched pairs."""
    return JSONResponse(_run_scan(category=category, min_sim=min_sim,
                                  max_events=max_events, show_prices=True))


@app.get("/api/scan/fast")
def api_scan_fast(category: str = "all"):
    """Quick scan — no live orderbook enrichment, uses catalog mid-prices only."""
    return JSONResponse(_run_scan(category=category, show_prices=False))


@app.get("/api/signals")
def api_signals(n: int = 100):
    """Return the last n entries from signals.jsonl."""
    return JSONResponse({"signals": _load_signals(n)})


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

"""Shared seeding for the Ask tests (tmp store via the conftest autouse fixture)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import store

FIXTURES = Path(__file__).parent / "fixtures" / "rule_texts"
MLB = json.loads((FIXTURES / "mlb.json").read_text())

BOOK_POLY = {"bids": [[0.40, 500], [0.39, 500]], "asks": [[0.45, 500], [0.46, 500]]}
BOOK_KALSHI = {"bids": [[0.50, 500], [0.49, 500]], "asks": [[0.55, 500], [0.56, 500]]}


def _iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def make_pair(ticker, poly_id, net, *, title="T", category="sports", contracts=100,
              exec_profit=5.0, kalshi_days=10, poly_days=20, books=True):
    p = {
        "kalshi_ticker": ticker, "poly_id": poly_id, "poly_token_id": poly_id + "-tok",
        "kalshi_title": f"K {title}", "poly_title": f"P {title}", "category": category,
        "arb_net_accurate": net, "exec_profit": exec_profit, "exec_contracts": contracts,
        "kalshi_close": _iso(kalshi_days), "poly_close": _iso(poly_days),
    }
    if books:
        p["poly_book"], p["kalshi_book"] = BOOK_POLY, BOOK_KALSHI
    return p


def seed():
    """Two completed fast scans. Returns (scan1_id, scan2_id, pair dicts)."""
    store.init_db()
    p1 = make_pair(MLB["kalshi"]["market_id"], MLB["polymarket"]["market_id"], 0.05, title="MLB")
    p2 = make_pair("K2", "poly2", 0.03, title="Two", category="politics", kalshi_days=3, contracts=10)
    p3 = make_pair("K3", "poly3", 0.01, title="Three", category="sports")
    s1 = store.start_scan("fast")
    store.finish_scan(s1, [p1, p2, p3], {})
    p1b = dict(p1, arb_net_accurate=0.08)
    p2b = make_pair("K2", "poly2", 0.03, title="Two", category="politics", kalshi_days=3,
                    contracts=10, books=False)
    p4 = make_pair("K4", "poly4", 0.02, title="Four", category="sports", contracts=300)
    s2 = store.start_scan("fast")
    store.finish_scan(s2, [p1b, p2b, p4], {})
    # evidence for p1 (conflicting tie-break rules) only
    store.save_evidence("kalshi", p1["kalshi_ticker"], MLB["kalshi"]["rules_text"], MLB["kalshi"]["source_url"])
    store.save_evidence("polymarket", p1["poly_id"], MLB["polymarket"]["rules_text"], MLB["polymarket"]["source_url"])
    store.add_review(store.pair_key(p1), "mismatch", "tie rules differ")
    store.add_review(store.pair_key(p4), "match")
    return s1, s2, {"p1": p1b, "p2": p2b, "p4": p4}

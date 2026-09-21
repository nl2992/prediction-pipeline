"""Multi-leg ladder candidates: Kalshi cumulative rungs vs Polymarket buckets.

The pair matcher is strictly 1-to-1, so it can never reach Kalshi's midterm
margin-of-victory ladders: their counterpart on Polymarket is a SUM of buckets,
not a market. This report runs that synthesis (`ladder_match`) against the live
catalogs and ranks the settlement-safe edges.

REVIEW ONLY - these are not alerts, for three reasons:

  * the executor has no N-leg support, and legging into a 4-bucket basket one
    order at a time is not the arb that was priced;
  * Gamma's catalog quotes carry no DEPTH, so a fat edge may sit on a book with
    a few dollars in it (the spread column is the only hint you get here);
  * a rung and a bucket can define the margin differently (two-party share vs
    all votes), which no amount of price data will tell you.

Usage:
    python -m tools.ladder_report              # top candidates
    python -m tools.ladder_report --min-edge 0.05 --json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone


def _pools():
    import discover as D
    from kalshi.client import KalshiClient
    from polymarket.client import PolymarketClient

    fa = datetime.now(timezone.utc).isoformat()
    kev = KalshiClient().get_all_events(status="open", with_nested_markets=True)
    pev = PolymarketClient().get_all_events()
    k = [D._k_snap(m, fa, ev.get("title", ""), ev.get("series_ticker", ""))
         for ev in kev for m in ev.get("markets") or []
         if m.get("status") in (None, "", "active", "open")]
    p = [D._p_snap_from_event(m, ev.get("title", ""), ev.get("slug") or ev.get("id", ""), fa)
         for ev in pev for m in ev.get("markets") or []
         if m.get("active", True) and not m.get("closed")]
    return k, p


def build(min_edge: float = 0.0) -> dict:
    from ladder_match import edges, synthesize

    k, p = _pools()
    rec = synthesize(k, p)
    ed = [r for r in edges(rec) if r["edge"] >= min_edge]
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rungs_synthesised": len(rec),
        "races": len({r["race"] for r in rec}),
        "exact_rungs": sum(1 for r in rec if r["exact"]),
        "priced": sum(1 for r in rec if r["sub_bid"] is not None or r["sup_ask"] is not None),
        "candidates": ed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--min-edge", type=float, default=0.02)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    logging.disable(logging.WARNING)

    out = build(a.min_edge)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"Ladder report {out['at']}")
    print(f"  {out['rungs_synthesised']} Kalshi rungs synthesised across {out['races']} races "
          f"({out['exact_rungs']} land on a bucket edge); {out['priced']} priced")
    print(f"  {len(out['candidates'])} candidates at >= {a.min_edge:.0%} edge  [REVIEW ONLY - "
          f"no depth data, no N-leg executor]\n")
    for r in out["candidates"][:a.top]:
        print(f"  {r['edge']:+.3f}  {r['race']} - {r['side']} {r['threshold']:g}+ pts "
              f"({'edge-aligned' if r['exact'] else 'straddled'})")
        print(f"          Kalshi {r['kalshi_ticker']}  bid {r['kalshi_bid']} / ask {r['kalshi_ask']}")
        print(f"          {r['direction']}  ({len(r['legs'])} PM legs)")
        print(f"          basket: sell-side {r['sub_bid']}, buy-side {r['sup_ask']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Ingestion / blocking RECALL probe — Kalshi event-cap dimension.

`validate_recall.py` covers recall lost at the similarity GATE, but only for
pairs already in the pool. It cannot see pairs that never entered the pool at all.
Production `discover()` blocks Kalshi to the first `max_events_to_search` events
(200 in the loop). A true cross-platform pair whose Kalshi event falls outside
that cap is silently dropped before matching ever runs.

This probe widens the cap and diffs the matched pairs: pairs that appear only at
the wider cap, and that the independent v2 `contract_spec` engine endorses, are
candidate ingestion misses caused by the event-count block.

Scope & honesty (so results are not over-read):
  * Covers ONLY the Kalshi event-count cap dimension of ingestion recall. It does
    NOT cover Polymarket keyword-derivation misses (a true PM counterpart that the
    derived keyword search never surfaced) — that dimension is still open.
  * v2 is an imperfect referee (it has endorsed e.g. "New York Liberty" vs
    "New York Jets" on a shared token), so endorsements are review candidates,
    not confirmed misses.
  * CLEAN = widening the cap surfaces no v2-endorsed new pairs — i.e. the 200-cap
    is not provably dropping v2-acceptable pairs in this scan. Not an absolute
    "zero missed" claim.

Usage:
    python validate_ingestion.py --json
    python validate_ingestion.py --prod-cap 200 --wide-cap 500
"""
from __future__ import annotations

import argparse
import json
import sys

from discover import discover


def _key(r: dict) -> tuple:
    return (r.get("poly_id"), r.get("kalshi_ticker"))


def run(prod_cap: int = 200, wide_cap: int = 500) -> dict:
    prod = discover(category="all", days=730, min_sim=0.30, show_prices=False,
                    max_events_to_search=prod_cap, catalog_cache_ttl=1200)
    wide = discover(category="all", days=730, min_sim=0.30, show_prices=False,
                    max_events_to_search=wide_cap, catalog_cache_ttl=1200)

    prod_keys = {_key(r) for r in prod}
    new_only = [r for r in wide if _key(r) not in prod_keys]
    endorsed = [r for r in new_only if r.get("v2_match") is True]

    return {
        "prod_cap": prod_cap, "wide_cap": wide_cap,
        "prod_pairs": len(prod), "wide_pairs": len(wide),
        "new_only_pairs": len(new_only),
        "v2_endorsed_ingestion_misses": len(endorsed),
        "candidates": [{
            "poly_title": r.get("poly_title"), "kalshi_title": r.get("kalshi_title"),
            "poly_id": r.get("poly_id"), "kalshi_ticker": r.get("kalshi_ticker"),
            "category": r.get("category"), "confidence": r.get("confidence"),
        } for r in endorsed],
        "clean": not endorsed,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingestion recall probe (event-cap sensitivity + v2 referee)")
    ap.add_argument("--prod-cap", type=int, default=200)
    ap.add_argument("--wide-cap", type=int, default=500)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # discover() prints to stdout; keep --json clean (see validate_live).
    if args.json:
        real = sys.stdout
        sys.stdout = sys.stderr
        try:
            res = run(args.prod_cap, args.wide_cap)
        finally:
            sys.stdout = real
        print(json.dumps(res, indent=2))
        return
    res = run(args.prod_cap, args.wide_cap)
    print(f"\nINGESTION probe | prod(cap={res['prod_cap']})={res['prod_pairs']} "
          f"wide(cap={res['wide_cap']})={res['wide_pairs']} "
          f"new-only={res['new_only_pairs']} "
          f"v2-endorsed misses={res['v2_endorsed_ingestion_misses']} "
          f"=> {'CLEAN' if res['clean'] else 'REVIEW'}")
    for c in res["candidates"]:
        print(f"  CANDIDATE INGESTION MISS [{c['category']}]: "
              f"{c['poly_title'][:42]!r} <-> {c['kalshi_title'][:42]!r}")


if __name__ == "__main__":
    main()

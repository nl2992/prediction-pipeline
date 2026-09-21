"""Coverage ledger: for every ingested market, is it matched — and if not, why?

`coverage_report` answers "of the pairs that exist, how many do we find?".
This answers the blunter question: "we ingest ~100k Kalshi and ~150k Polymarket
markets — what happens to each one?" It splits the unmatched into

  * held out of matching on purpose (stats-only, parlay, ended), and
  * markets whose SERIES/CLASS never matches anything, which is the honest
    signature of "the other venue does not list this contract" (Kalshi has no
    corner markets; Polymarket has no vote-percent ladders).

A class that never matches is not necessarily a bug, and a class that matches
partially usually is a recall gap worth a look — the output separates the two so
the next pass can start from evidence.

Usage:
    python -m tools.coverage_ledger            # ~4 min (no order books)
    python -m tools.coverage_ledger --json
    python -m tools.coverage_ledger --top 20
"""
from __future__ import annotations

import argparse
import collections
import json
import sys


def build(top: int = 12) -> dict:
    from discover import discover

    pairs, k_snaps, p_snaps = discover(category="all", days=None, min_sim=0.30,
                                       show_prices=False, return_pools=True)
    matched_k = {x["kalshi_ticker"] for x in pairs}
    matched_p = {x["poly_id"] for x in pairs}

    def family(s) -> str:
        return (s.extra.get("series_ticker")
                or (s.event_id or "").split("-")[0] or "?")

    k_tot, k_hit, k_held = collections.Counter(), collections.Counter(), collections.Counter()
    for s in k_snaps:
        f = family(s)
        k_tot[f] += 1
        if s.extra.get("match_excluded"):
            k_held[f] += 1
        elif s.market_id in matched_k:
            k_hit[f] += 1

    p_tot, p_hit, p_held = collections.Counter(), collections.Counter(), collections.Counter()
    for s in p_snaps:
        t = s.extra.get("sports_market_type") or "non-sports"
        p_tot[t] += 1
        if s.extra.get("match_excluded"):
            p_held[t] += 1
        elif s.market_id in matched_p:
            p_hit[t] += 1

    def split(tot, hit, held):
        never = {f: n for f, n in tot.items() if hit[f] == 0}
        partial = {f: (hit[f], n) for f, n in tot.items() if hit[f]}
        return {
            "markets": sum(tot.values()),
            "matched": sum(hit.values()),
            "held_out": sum(held.values()),
            "classes": len(tot),
            "classes_matching": len(partial),
            "classes_never_matching": len(never),
            "markets_in_never_matching_classes": sum(never.values()),
            "top_never_matching": sorted(never.items(), key=lambda kv: -kv[1])[:top],
            "top_partially_matching": sorted(
                ((f, h, n) for f, (h, n) in partial.items()), key=lambda t: -(t[2] - t[1])
            )[:top],
        }

    return {"kalshi": split(k_tot, k_hit, k_held),
            "polymarket": split(p_tot, p_hit, p_held),
            "pairs": len(pairs)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()
    import logging
    logging.disable(logging.WARNING)

    led = build(a.top)
    if a.json:
        print(json.dumps(led, indent=2))
        return 0
    for venue in ("kalshi", "polymarket"):
        v = led[venue]
        print(f"\n{venue.upper()}: {v['markets']:,} markets in {v['classes']:,} classes — "
              f"{v['matched']:,} matched, {v['held_out']:,} held out of matching")
        print(f"  classes that match something: {v['classes_matching']:,}; "
              f"classes that never match: {v['classes_never_matching']:,} "
              f"({v['markets_in_never_matching_classes']:,} markets — usually means the other "
              f"venue does not list the contract)")
        print("  largest never-matching classes:")
        for name, n in v["top_never_matching"]:
            print(f"     {n:7,}  {name}")
        print("  largest recall gaps inside classes that DO match (matched/total):")
        for name, h, n in v["top_partially_matching"]:
            print(f"     {h:5,}/{n:<7,} {name}")
    print(f"\npairs: {led['pairs']:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

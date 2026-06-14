"""Live validation of the matching engine against current Kalshi+Polymarket data.

Runs the organic `discover` scan to surface real, current cross-platform pairs
(real tickers/slugs/categories), then judges each engine-returned pair with the
**independent v2 `contract_spec` engine** that already rides along in shadow mode.
A v2 rejection of a v1 live match is a candidate FALSE POSITIVE; a v2 polarity
flag that v1 missed is a candidate polarity error. Both fail the run.

Scope & honesty: this measures live **precision** (are the engine's returned
pairs correct?) and polarity, using v2 as an independent referee. It does NOT
measure live **recall** (missed matches), which would require ground-truth
extraction independent of the engine — that remains an open backlog item in
MATCHER_VALIDATION_LOG.md. Do not read a pass here as "no live matches were
missed."

Usage:
    python validate_live.py            # human summary
    python validate_live.py --json     # machine-readable
    python validate_live.py --min-pairs 20
"""
from __future__ import annotations

import argparse
import json
import sys

from discover import discover


def run(min_pairs: int = 20, max_events: int = 200) -> dict:
    pairs = discover(category="all", days=730, min_sim=0.30,
                     show_prices=False, max_events_to_search=max_events,
                     catalog_cache_ttl=1200)

    judged, agree, disagree, unjudged, inverted = [], [], [], [], []
    for r in pairs:
        v2 = r.get("v2_match")
        rec = {
            "poly_id": r.get("poly_id"), "poly_slug": r.get("poly_slug"),
            "poly_title": r.get("poly_title"),
            "kalshi_ticker": r.get("kalshi_ticker"),
            "kalshi_series": r.get("kalshi_series_ticker"),
            "kalshi_title": r.get("kalshi_title"),
            "category": r.get("category"), "confidence": r.get("confidence"),
            "v2_match": v2, "v2_inverted": r.get("v2_inverted"),
            "v2_reasons": r.get("v2_reasons"),
        }
        judged.append(rec)
        if v2 is True:
            agree.append(rec)
        elif v2 is False:
            disagree.append(rec)            # candidate false positive
        else:
            unjudged.append(rec)            # v2 errored — cannot referee
        if r.get("v2_inverted"):
            inverted.append(rec)

    # categories covered
    cats = sorted({r.get("category", "?") for r in pairs})

    passed = (len(pairs) >= min_pairs
              and not disagree
              and not unjudged)

    return {
        "engine_match_count": len(pairs),
        "min_pairs": min_pairs,
        "categories": cats,
        "v2_agree": len(agree),
        "v2_disagree": len(disagree),
        "v2_unjudged": len(unjudged),
        "v2_inverted_flagged": len(inverted),
        "false_positive_candidates": disagree,
        "unjudged_pairs": unjudged,
        "passed": passed,
        "pairs": judged,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Live matcher validation via discover + v2 referee")
    ap.add_argument("--min-pairs", type=int, default=20)
    ap.add_argument("--max-events", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    # discover() prints scan progress to stdout. In --json mode that pollutes the
    # payload and breaks downstream json.load, so route progress to stderr (still
    # visible / logged) and keep stdout a clean JSON document.
    if args.json:
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            res = run(min_pairs=args.min_pairs, max_events=args.max_events)
        finally:
            sys.stdout = real_stdout
        print(json.dumps(res, indent=2))
        return
    res = run(min_pairs=args.min_pairs, max_events=args.max_events)
    print(f"\nLIVE: engine pairs={res['engine_match_count']} (need >= {res['min_pairs']}) | "
          f"v2 agree={res['v2_agree']} disagree={res['v2_disagree']} "
          f"unjudged={res['v2_unjudged']} inverted={res['v2_inverted_flagged']} "
          f"=> {'PASS' if res['passed'] else 'FAIL'}")
    print(f"categories: {', '.join(res['categories'])}")
    if res["false_positive_candidates"]:
        print("CANDIDATE FALSE POSITIVES (v2 rejects v1):")
        for r in res["false_positive_candidates"]:
            print(f"  {r['poly_title'][:44]!r} <-> {r['kalshi_title'][:44]!r}")
            for reason in (r["v2_reasons"] or [])[:2]:
                print(f"     - {reason}")
    if res["unjudged_pairs"]:
        print(f"UNJUDGED (v2 errored) x{len(res['unjudged_pairs'])}")


if __name__ == "__main__":
    main()

"""Live RECALL probe for the matching engine.

The curated harness and ``validate_live.py`` measure *precision* (are returned
pairs correct?). They cannot see *missed* matches. This probe takes a first,
clearly-scoped step at recall, against the REAL production matcher
(`_match_groups_then_individual`, the two-level group matcher `discover` uses):

  1. Pull the live snapshot pools `discover()` builds once (`return_pools=True`).
  2. Run the production group matcher at the PRODUCTION similarity gate (0.30)
     and at a RELAXED gate (default 0.20) over the same pools.
  3. The pairs that appear only under the relaxed gate are candidates the
     production gate dropped. Referee each with the independent v2
     `contract_spec` engine; a v2-endorsed relaxed-only pair is a candidate
     **missed match** (recall gap attributable to the similarity threshold).

Scope & honesty (stated so results are not over-read):
  * This measures recall gaps caused by the production matcher's SIMILARITY GATE
    over discover's candidate universe. It does NOT detect markets that were
    never fetched/blocked into the pools (ingestion/blocking recall), nor does it
    prove the relaxed-gate pairs are truly correct — v2 is an imperfect referee
    (it has endorsed e.g. "New York Liberty" vs "New York Jets" on a shared-token
    score), so its endorsements are *candidates for human review*, not confirmed
    misses.
  * A clean result ("no v2-endorsed relaxed-only pairs") means the gate is not
    obviously dropping v2-acceptable pairs in this scan — not "zero missed
    matches" in any absolute sense.

Usage:
    python validate_recall.py --json
    python validate_recall.py --relaxed 0.18 --max-events 100
"""
from __future__ import annotations

import argparse
import json
import sys

from discover import discover, _match_groups_then_individual

try:
    from contract_spec import explain as _v2_explain
except Exception:  # pragma: no cover
    _v2_explain = None


def _key(mp) -> tuple[str, str]:
    return (mp.poly.market_id, mp.kalshi.market_id)


def run(production: float = 0.30, relaxed: float = 0.20,
        max_events: int = 200) -> dict:
    out = discover(category="all", days=730, min_sim=production,
                   show_prices=False, max_events_to_search=max_events,
                   catalog_cache_ttl=1200, return_pools=True)
    _results, k_snaps, p_snaps = out

    # Same production matcher discover uses, at two gates over one fetched pool set.
    prod_pairs = _match_groups_then_individual(k_snaps, p_snaps, min_sim=production)
    relax_pairs = _match_groups_then_individual(k_snaps, p_snaps, min_sim=relaxed)

    prod_keys = {_key(mp) for mp in prod_pairs}
    relaxed_only = [mp for mp in relax_pairs if _key(mp) not in prod_keys]

    candidate_misses = []
    v2_unavailable = _v2_explain is None
    for mp in relaxed_only:
        endorsed, reasons = None, None
        if _v2_explain is not None:
            try:
                v2 = _v2_explain(mp.poly, mp.kalshi)
                endorsed, reasons = v2.match, v2.reasons
            except Exception as exc:
                endorsed, reasons = None, [f"v2 error: {exc}"]
        if endorsed:
            candidate_misses.append({
                "poly_id": mp.poly.market_id, "poly_title": mp.poly.title,
                "kalshi_ticker": mp.kalshi.market_id, "kalshi_title": mp.kalshi.title,
                "title_sim": round(mp.title_similarity, 3),
                "confidence": round(mp.confidence, 3),
                "v2_reasons": reasons,
            })

    return {
        "pool_poly": len(p_snaps), "pool_kalshi": len(k_snaps),
        "production_gate": production, "relaxed_gate": relaxed,
        "production_pairs": len(prod_pairs),
        "relaxed_pairs": len(relax_pairs),
        "relaxed_only_pairs": len(relaxed_only),
        "v2_endorsed_candidate_misses": len(candidate_misses),
        "v2_unavailable": v2_unavailable,
        "candidate_misses": candidate_misses,
        "recall_clean": (not candidate_misses) and not v2_unavailable,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Live recall probe (threshold sensitivity + v2 referee)")
    ap.add_argument("--production", type=float, default=0.30)
    ap.add_argument("--relaxed", type=float, default=0.20)
    ap.add_argument("--max-events", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # discover() prints to stdout; keep --json payload clean (see validate_live).
    if args.json:
        real = sys.stdout
        sys.stdout = sys.stderr
        try:
            res = run(args.production, args.relaxed, args.max_events)
        finally:
            sys.stdout = real
        print(json.dumps(res, indent=2))
        return
    res = run(args.production, args.relaxed, args.max_events)
    print(f"\nRECALL probe | pools poly={res['pool_poly']} kalshi={res['pool_kalshi']} | "
          f"prod({res['production_gate']})={res['production_pairs']} "
          f"relaxed({res['relaxed_gate']})={res['relaxed_pairs']} "
          f"relaxed-only={res['relaxed_only_pairs']} "
          f"v2-endorsed misses={res['v2_endorsed_candidate_misses']} "
          f"=> {'CLEAN' if res['recall_clean'] else 'REVIEW'}")
    for c in res["candidate_misses"]:
        print(f"  CANDIDATE MISS: {c['poly_title'][:44]!r} <-> {c['kalshi_title'][:44]!r} "
              f"(sim={c['title_sim']})")


if __name__ == "__main__":
    main()

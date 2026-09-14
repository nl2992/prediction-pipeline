"""Full-coverage verifier — diffs discover()'s ingestion against ground truth.

discover() (via ingest_kalshi / ingest_polymarket) is supposed to ingest 100%
of both venues' OPEN markets. This probe checks that claim independently:

  1. Records t_start, then runs ingest_kalshi + ingest_polymarket (sweep on)
     WITHOUT running the matcher — so a slow/changing matcher never affects
     this probe.
  2. Independently walks Kalshi ``/markets?status=open&mve_filter=exclude``
     (cursor pagination) and Gamma ``/markets/keyset?closed=false`` — the
     ground-truth open-market id sets for each venue.
  3. Diffs both ways: ids in truth but not ingested ("miss"), and ids ingested
     but not in truth ("closed_since" — the market closed between the two
     scans, which is fine).

Each miss is classified:
  * "drift"  — the market (or its parent event) was created/opened AFTER
               t_start, so ingest_* genuinely could not have seen it yet.
  * "gap"    — created before t_start and still missing: a real ingestion bug.

Exit code is 0 iff there are zero "gap" misses on both venues, so this is
CI-safe as a live smoke check (though it hits both live APIs and can take
several minutes with the Polymarket orphan sweep on).

Usage:
    python -m tools.validate_coverage --json
    python -m tools.validate_coverage --no-sweep
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from discover import ingest_kalshi, ingest_polymarket, _parse_dt


def _kalshi_truth_ids(kc) -> tuple[set[str], dict[str, dict]]:
    """Ground truth: every OPEN Kalshi market ticker, via
    /markets?status=open&mve_filter=exclude (mve_filter isn't exposed by
    KalshiClient.get_markets, so call the underlying /markets path directly)."""
    ids: set[str] = set()
    by_id: dict[str, dict] = {}
    cursor = None
    seen_cursors: set[str] = set()
    while True:
        params = {"limit": 1000, "status": "open", "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        resp = kc._get("/markets", params=params)
        batch = resp.get("markets", [])
        for m in batch:
            t = m.get("ticker", "")
            ids.add(t)
            by_id[t] = m
        cursor = resp.get("cursor")
        if not batch or not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    return ids, by_id


def _polymarket_truth_ids(pc) -> tuple[set[str], dict[str, dict]]:
    """Ground truth: every market with active and not closed, via /markets/keyset."""
    ids: set[str] = set()
    by_id: dict[str, dict] = {}
    for m in pc.get_all_markets_keyset(closed=False):
        if not m.get("active"):
            continue
        cid = m.get("conditionId") or m.get("id", "")
        ids.add(cid)
        by_id[cid] = m
    return ids, by_id


def _classify_miss(row: dict, t_start: datetime, created_keys: tuple[str, ...]) -> str:
    """'drift' if the row (or its parent event, for Polymarket) was created
    after t_start; else 'gap' (a real ingestion miss)."""
    for key in created_keys:
        created = _parse_dt(row.get(key))
        if created:
            return "drift" if created > t_start else "gap"
    events = row.get("events") or []
    if events:
        created = _parse_dt(events[0].get("createdAt"))
        if created:
            return "drift" if created > t_start else "gap"
    # No creation timestamp available — cannot prove it's drift, so treat it
    # conservatively as a gap (visible, not silently waved through).
    return "gap"


def _diff(venue: str, ingested_ids: set[str], truth_ids: set[str],
         truth_by_id: dict[str, dict], t_start: datetime,
         created_keys: tuple[str, ...]) -> dict:
    miss_ids = truth_ids - ingested_ids
    closed_since_ids = ingested_ids - truth_ids

    drift, gap = [], []
    for mid in miss_ids:
        row = truth_by_id.get(mid, {})
        cls = _classify_miss(row, t_start, created_keys)
        (drift if cls == "drift" else gap).append(mid)

    coverage_pct = 100.0 * len(ingested_ids & truth_ids) / len(truth_ids) if truth_ids else 100.0
    return {
        "venue": venue,
        "truth_count": len(truth_ids),
        "ingested_count": len(ingested_ids),
        "coverage_pct": round(coverage_pct, 2),
        "drift_count": len(drift),
        "gap_count": len(gap),
        "closed_since_count": len(closed_since_ids),
        "gap_examples": sorted(gap)[:10],
        "drift_examples": sorted(drift)[:10],
    }


def run(sweep: bool = True) -> dict:
    from kalshi.client import KalshiClient
    from polymarket.client import PolymarketClient

    t_start = datetime.now(timezone.utc)
    kc = KalshiClient()
    pc = PolymarketClient()

    _filtered, k_snaps = ingest_kalshi(kc, now=t_start, market_sweep=sweep)
    p_snaps = ingest_polymarket(pc, fetched_at=t_start.isoformat(), market_sweep=sweep)

    k_ingested_ids = {s.market_id for s in k_snaps}
    p_ingested_ids = {s.market_id for s in p_snaps}

    k_truth_ids, k_truth_by_id = _kalshi_truth_ids(kc)
    p_truth_ids, p_truth_by_id = _polymarket_truth_ids(pc)

    k_result = _diff("kalshi", k_ingested_ids, k_truth_ids, k_truth_by_id, t_start,
                     created_keys=("open_time", "created_time"))
    p_result = _diff("polymarket", p_ingested_ids, p_truth_ids, p_truth_by_id, t_start,
                     created_keys=("createdAt",))

    return {
        "t_start": t_start.isoformat(),
        "sweep": sweep,
        "kalshi": k_result,
        "polymarket": p_result,
        "clean": k_result["gap_count"] == 0 and p_result["gap_count"] == 0,
    }


def _print_venue(r: dict) -> None:
    print(f"  {r['venue']}: {r['ingested_count']:,} ingested vs {r['truth_count']:,} truth "
          f"({r['coverage_pct']:.1f}% coverage) · gaps={r['gap_count']} drift={r['drift_count']} "
          f"closed_since={r['closed_since_count']}")
    for mid in r["gap_examples"]:
        print(f"    GAP: {mid}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Full-coverage ingestion verifier (ground-truth diff)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-sweep", dest="sweep", action="store_false", default=True,
                    help="Skip both orphan sweeps — Kalshi's /markets?status=open&"
                         "mve_filter=exclude walk and Polymarket's /markets/keyset walk "
                         "(faster, undercounts both venues)")
    args = ap.parse_args()

    if args.json:
        real = sys.stdout
        sys.stdout = sys.stderr
        try:
            res = run(sweep=args.sweep)
        finally:
            sys.stdout = real
        print(json.dumps(res, indent=2))
    else:
        res = run(sweep=args.sweep)
        print(f"\nCOVERAGE probe | t_start={res['t_start']} sweep={res['sweep']}")
        _print_venue(res["kalshi"])
        _print_venue(res["polymarket"])
        print(f"=> {'CLEAN' if res['clean'] else 'GAPS FOUND'}")

    sys.exit(0 if res["clean"] else 1)


if __name__ == "__main__":
    main()

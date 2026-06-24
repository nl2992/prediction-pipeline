"""Validate the matching engine against a curated cross-platform pair set.

Unlike ``tests/test_fixture_pairs.py`` (which scores pairs one at a time), this
harness feeds an entire curated set of Polymarket + Kalshi snapshots into
``match_markets`` *together*, then checks that the engine recovers the intended
1-to-1 pairing. Doing it jointly is what surfaces the three failure modes the
validation loop cares about at once:

  * missed match     — an intended pair the engine did not return
  * false positive   — a returned pair that is not an intended pair
  * wrong counterpart — poly_i matched to kalshi_j (i != j)

It also checks polarity (``inverted`` flag) against the fixture ground truth.

Usage:
    python validate_matcher.py                 # diverse 20-pair slice, offset 0
    python validate_matcher.py --offset 20     # rotate to a different slice
    python validate_matcher.py --n 42 --json   # all true pairs, JSON output
"""
from __future__ import annotations

import argparse
import json
import os

from pipeline import MarketSnapshot, OrderBook, PriceLevel
from matcher import match_markets

FIXTURE = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "pairs_fixture.json")
CATEGORIES = ["politics", "econ", "sports", "crypto", "tech", "culture", "misc"]


def _ob(yb: float, ya: float) -> OrderBook:
    return OrderBook(bids=[PriceLevel(yb, 100.0)], asks=[PriceLevel(ya, 100.0)])


def _poly(p: dict) -> MarketSnapshot:
    return MarketSnapshot("polymarket", p["market_id"], p.get("slug", ""),
                          p["question"], "open", p.get("end_date_iso"), "",
                          _ob(p.get("yes_bid", 0.4), p.get("yes_ask", 0.5)), extra={})


def _kalshi(k: dict) -> MarketSnapshot:
    return MarketSnapshot("kalshi", k["ticker"], k.get("ticker", ""),
                          k["title"], "open", k.get("close_time_iso"), "",
                          _ob(k.get("yes_bid", 0.4), k.get("yes_ask", 0.5)), extra={})


def select_pairs(all_true: list[dict], n: int, offset: int) -> list[dict]:
    """Pick a category-diverse slice of ``n`` true pairs, rotated by ``offset``.

    Round-robins across categories so every slice spans the full spread, and the
    offset rotates the starting point so successive loop iterations test
    different subsets of the labelled pool.
    """
    by_cat: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for p in all_true:
        by_cat.setdefault(p["category"], []).append(p)
    ordered: list[dict] = []
    i = 0
    while len(ordered) < len(all_true):
        for c in CATEGORIES:
            bucket = by_cat.get(c, [])
            if i < len(bucket):
                ordered.append(bucket[i])
        i += 1
    if not ordered:
        return []
    start = offset % len(ordered)
    rotated = ordered[start:] + ordered[:start]
    return rotated[:n]


def run(n: int = 20, offset: int = 0, min_sim: float = 0.30,
        max_delta: float = 99999.0) -> dict:
    with open(FIXTURE, encoding="utf-8") as _f:
        data = json.load(_f)
    all_true = [p for p in data["pairs"] if p["ground_truth"]["should_match"]]
    chosen = select_pairs(all_true, n, offset)

    poly_snaps = [_poly(p["polymarket"]) for p in chosen]
    kalshi_snaps = [_kalshi(p["kalshi"]) for p in chosen]
    intended = {p["polymarket"]["market_id"]: p["kalshi"]["ticker"] for p in chosen}
    inverted_truth = {p["polymarket"]["market_id"]: p["ground_truth"].get("inverted", False)
                      for p in chosen}
    pid_by_poly = {p["polymarket"]["market_id"]: p["pair_id"] for p in chosen}

    returned = match_markets(poly_snaps, kalshi_snaps,
                             min_title_similarity=min_sim,
                             max_close_delta_hours=max_delta)
    ret_by_poly = {mp.poly.market_id: mp for mp in returned}

    per_pair = []
    exact = 0
    for p in chosen:
        poly_id = p["polymarket"]["market_id"]
        exp_k = intended[poly_id]
        mp = ret_by_poly.get(poly_id)
        got_k = mp.kalshi.market_id if mp else None
        score = round(mp.confidence, 3) if mp else None
        polarity_ok = (mp.inverted == inverted_truth[poly_id]) if mp else None
        if mp is None:
            label = "Missed match"
        elif got_k != exp_k:
            label = "Incorrect counterpart"
        elif not polarity_ok:
            label = "Incorrect polarity"
        else:
            label = "Exact match"
            exact += 1
        per_pair.append({
            "pair_id": pid_by_poly[poly_id], "category": p["category"],
            "expected_poly": poly_id, "expected_kalshi": exp_k,
            "engine_kalshi": got_k, "score": score,
            "exact": label == "Exact match", "label": label,
        })

    intended_set = {(pid, k) for pid, k in intended.items()}
    returned_set = {(mp.poly.market_id, mp.kalshi.market_id) for mp in returned}
    false_positives = [{"poly": a, "kalshi": b} for (a, b) in returned_set - intended_set]
    missed = [pid_by_poly[a] for (a, _) in intended_set
              if a not in {mp.poly.market_id for mp in returned}]

    return {
        "offset": offset, "n_requested": n,
        "expected_match_count": len(chosen),
        "engine_match_count": len(returned),
        "exact_matches": exact,
        "false_positives": false_positives,
        "missed": missed,
        "passed": (exact == len(chosen)
                   and len(returned) == len(chosen)
                   and not false_positives),
        "per_pair": per_pair,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate matcher against curated pairs")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--min-sim", type=float, default=0.30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = run(n=args.n, offset=args.offset, min_sim=args.min_sim)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"slice offset={result['offset']} | expected={result['expected_match_count']} "
          f"engine={result['engine_match_count']} exact={result['exact_matches']} "
          f"FP={len(result['false_positives'])} missed={len(result['missed'])} "
          f"=> {'PASS' if result['passed'] else 'FAIL'}")
    for r in result["per_pair"]:
        flag = "OK " if r["exact"] else "XX "
        print(f"  {flag}{r['pair_id']:<10}{r['category']:<10}{r['label']:<22}"
              f"score={r['score']} exp_k={r['expected_kalshi']} got_k={r['engine_kalshi']}")
    if result["false_positives"]:
        print("  False positives:", result["false_positives"])
    if result["missed"]:
        print("  Missed:", result["missed"])


if __name__ == "__main__":
    main()

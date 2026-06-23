#!/usr/bin/env python3
"""
Test the matcher against the fixture (pairs_fixture.json).

Primary evaluation is PAIRWISE: the fixture's own description says
"ground_truth.should_match is the matcher label" — each of the 50 entries is a
candidate (polymarket, kalshi) pair the matcher must accept or reject. A global
50x50 assignment is also reported as a secondary view, but it contains
deliberately unwinnable traps (duplicate Kalshi titles like FED-26JUL-CUT vs
FED-26JUL-CUTX) that make 1-1 assignment ambiguous.
"""

import json
from dataclasses import dataclass
from matcher import match_markets


@dataclass
class MockSnapshot:
    market_id: str
    title: str
    close_time: str | None = None
    extra: dict = None
    event_id: str = ""


def load_fixture():
    with open(r"C:\Users\nigel\Downloads\pairs_fixture.json") as f:
        return json.load(f)


def to_snaps(pair):
    pm = pair["polymarket"]
    k = pair["kalshi"]
    return (
        MockSnapshot(market_id=pm["market_id"], title=pm["question"], close_time=pm["end_date_iso"]),
        MockSnapshot(market_id=k["ticker"], title=k["title"], close_time=k["close_time_iso"]),
    )


def main():
    fixture = load_fixture()
    pairs = fixture["pairs"]

    # ---------------- PAIRWISE EVALUATION (primary) ----------------
    print("=" * 80)
    print("PAIRWISE EVALUATION (fixture semantics: should_match is the matcher label)")
    print("=" * 80)

    tp = tn = fp = fn = 0
    failures = []

    for pair in pairs:
        pm_snap, k_snap = to_snaps(pair)
        gt = pair["ground_truth"]
        matched = bool(match_markets([pm_snap], [k_snap], min_title_similarity=0.30, max_close_delta_hours=9999))

        if gt["should_match"] and matched:
            tp += 1
        elif gt["should_match"] and not matched:
            fn += 1
            failures.append((pair["pair_id"], gt["match_type"], "MISSED MATCH", pair))
        elif not gt["should_match"] and not matched:
            tn += 1
        else:
            fp += 1
            failures.append((pair["pair_id"], gt["match_type"], "FALSE POSITIVE", pair))

    for pair_id, mtype, kind, pair in failures:
        print(f"[FAIL] {pair_id} ({mtype}): {kind}")
        print(f"    PM: {pair['polymarket']['question']}")
        print(f"    K:  {pair['kalshi']['title']}")

    total = len(pairs)
    print()
    print(f"Should-match correct:     {tp} / {tp + fn}")
    print(f"Should-NOT-match correct: {tn} / {tn + fp}")
    print(f"False positives: {fp}   False negatives: {fn}")
    print(f"PAIRWISE ACCURACY: {(tp + tn) / total * 100:.1f}%  ({tp + tn}/{total})")

    # ---------------- GLOBAL ASSIGNMENT (secondary) ----------------
    print()
    print("=" * 80)
    print("GLOBAL 1-1 ASSIGNMENT (secondary; duplicate-title traps make this ambiguous)")
    print("=" * 80)

    poly_snaps, kalshi_snaps = [], []
    expected = {}
    k_title_close = {}  # kalshi id -> (title, close) for equivalence scoring
    for pair in pairs:
        pm_snap, k_snap = to_snaps(pair)
        poly_snaps.append(pm_snap)
        kalshi_snaps.append(k_snap)
        expected[pair["pair_id"]] = (pm_snap.market_id, k_snap.market_id, pair["ground_truth"]["should_match"])
        k_title_close[k_snap.market_id] = (k_snap.title, k_snap.close_time)

    matched_pairs = match_markets(poly_snaps, kalshi_snaps, min_title_similarity=0.30, max_close_delta_hours=9999)
    matched_by_poly = {mp.poly.market_id: mp.kalshi.market_id for mp in matched_pairs}

    g_tp = g_tn = g_fp = g_fn = 0
    for _pair_id, (pm_id, k_id, should) in sorted(expected.items()):
        got_k = matched_by_poly.get(pm_id)
        # Equivalence: matched K counts as correct if it is title+close identical
        # to the expected K (the fixture contains indistinguishable duplicates).
        hit = got_k == k_id or (got_k is not None and k_title_close.get(got_k) == k_title_close[k_id])
        if should and hit:
            g_tp += 1
        elif should and not hit:
            g_fn += 1
        elif not should and not hit:
            g_tn += 1
        else:
            g_fp += 1

    print(f"Should-match correct:     {g_tp} / {g_tp + g_fn}")
    print(f"Should-NOT-match correct: {g_tn} / {g_tn + g_fp}")
    print(f"GLOBAL ACCURACY: {(g_tp + g_tn) / total * 100:.1f}%  ({g_tp + g_tn}/{total})")


if __name__ == "__main__":
    main()

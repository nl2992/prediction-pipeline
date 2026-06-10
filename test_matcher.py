#!/usr/bin/env python3
"""
Test the matcher against the fixture.
Compare ground truth labels with what the matcher outputs.
"""

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from matcher import (
    match_markets,
    is_compatible_match,
    is_close_time_compatible,
    _tokens,
    _jaccard,
    _close_delta_hours,
    _confidence,
)

# Mock MarketSnapshot for testing
@dataclass
class MockSnapshot:
    market_id: str
    title: str
    close_time: str | None = None
    extra: dict = None
    event_id: str = ""

# Load the fixture
with open(r"C:\Users\nigel\Downloads\pairs_fixture.json") as f:
    fixture = json.load(f)

# Convert fixture pairs to MockSnapshots
poly_snaps = []
kalshi_snaps = []
expected_matches = {}

for pair in fixture["pairs"]:
    pair_id = pair["pair_id"]
    pm = pair["polymarket"]
    k = pair["kalshi"]
    gt = pair["ground_truth"]

    poly_snap = MockSnapshot(
        market_id=pm["market_id"],
        title=pm["question"],
        close_time=pm["end_date_iso"],
    )
    kalshi_snap = MockSnapshot(
        market_id=k["ticker"],
        title=k["title"],
        close_time=k["close_time_iso"],
    )

    poly_snaps.append(poly_snap)
    kalshi_snaps.append(kalshi_snap)

    expected_matches[pair_id] = {
        "should_match": gt["should_match"],
        "match_type": gt["match_type"],
        "inverted": gt["inverted"],
        "poly_id": pm["market_id"],
        "kalshi_id": k["ticker"],
        "pm_question": pm["question"],
        "k_title": k["title"],
    }

# Run matcher
matched_pairs = match_markets(
    poly_snaps,
    kalshi_snaps,
    min_title_similarity=0.30,
    max_close_delta_hours=9999,  # Don't filter by time
)

# Build matched set for comparison
matched_set = {}
for mp in matched_pairs:
    # Find the pair_id that corresponds to this match
    for pair_id, expected in expected_matches.items():
        if expected["poly_id"] == mp.poly.market_id and expected["kalshi_id"] == mp.kalshi.market_id:
            matched_set[pair_id] = {
                "matched": True,
                "confidence": mp.confidence,
                "title_similarity": mp.title_similarity,
                "close_delta_hours": mp.close_delta_hours,
            }

# Analyze results
print("=" * 80)
print("MATCHER TEST RESULTS")
print("=" * 80)

correct_matches = 0
correct_non_matches = 0
false_positives = 0  # Should NOT match but did
false_negatives = 0  # Should match but didn't

print("\n### SHOULD MATCH ###\n")
for pair_id, expected in sorted(expected_matches.items()):
    if expected["should_match"]:
        if pair_id in matched_set:
            print(f"[OK] {pair_id} ({expected['match_type']}): MATCHED (confidence={matched_set[pair_id]['confidence']:.3f})")
            correct_matches += 1
        else:
            print(f"[FAIL] {pair_id} ({expected['match_type']}): NOT MATCHED (should have matched)")
            print(f"    PM: {expected['pm_question']}")
            print(f"    K:  {expected['k_title']}")
            false_negatives += 1

print("\n### SHOULD NOT MATCH ###\n")
for pair_id, expected in sorted(expected_matches.items()):
    if not expected["should_match"]:
        if pair_id not in matched_set:
            print(f"[OK] {pair_id} ({expected['match_type']}): Correctly NOT matched")
            correct_non_matches += 1
        else:
            print(f"[FAIL] {pair_id} ({expected['match_type']}): INCORRECTLY MATCHED (confidence={matched_set[pair_id]['confidence']:.3f})")
            print(f"    PM: {expected['pm_question']}")
            print(f"    K:  {expected['k_title']}")
            false_positives += 1

# Summary
print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"Correct matches: {correct_matches} / {sum(1 for p in expected_matches.values() if p['should_match'])}")
print(f"Correct non-matches: {correct_non_matches} / {sum(1 for p in expected_matches.values() if not p['should_match'])}")
print(f"False positives (wrong matches): {false_positives}")
print(f"False negatives (missed matches): {false_negatives}")
print(f"Accuracy: {(correct_matches + correct_non_matches) / len(expected_matches) * 100:.1f}%")

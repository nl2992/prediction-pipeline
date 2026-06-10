#!/usr/bin/env python3
"""Debug threshold-led acceptance for failing pairs."""

import json
from matcher import (
    _numeric_threshold,
    _threshold_equal,
    _named_entities,
    _close_delta_hours,
)

# Load fixture
with open(r"C:\Users\nigel\Downloads\pairs_fixture.json") as f:
    fixture = json.load(f)

# Check threshold-led acceptance for specific pairs
test_pairs = [11, 14, 16, 21, 24]  # 0-indexed, so pair 11 = PAIR-011

for idx in test_pairs:
    pair = fixture["pairs"][idx]
    pm = pair["polymarket"]
    k = pair["kalshi"]
    pair_id = pair["pair_id"]

    print(f"\n{'='*80}")
    print(f"{pair_id}")
    print(f"{'='*80}")

    pm_q = pm["question"]
    k_t = k["title"]
    pm_close = pm["end_date_iso"]
    k_close = k["close_time_iso"]

    print(f"PM: {pm_q}")
    print(f"K:  {k_t}")

    # Check thresholds
    pm_thr = _numeric_threshold(pm_q)
    k_thr = _numeric_threshold(k_t)
    print(f"\nThresholds:")
    print(f"  PM: {pm_thr}")
    print(f"  K:  {k_thr}")

    if pm_thr and k_thr:
        thr_equal = _threshold_equal(pm_thr, k_thr)
        print(f"  Thresholds equal: {thr_equal}")

    # Check entities
    pm_ent = _named_entities(pm_q)
    k_ent = _named_entities(k_t)
    shared = pm_ent & k_ent
    print(f"\nEntities:")
    print(f"  PM: {pm_ent}")
    print(f"  K:  {k_ent}")
    print(f"  Shared: {shared}")

    # Check close times
    dh = _close_delta_hours(pm_close, k_close)
    print(f"\nClose times:")
    print(f"  PM: {pm_close}")
    print(f"  K:  {k_close}")
    print(f"  Delta hours: {dh}")
    if dh is not None:
        print(f"  Within 72h: {dh <= 72.0}")

    # Check all conditions for threshold-led acceptance
    print(f"\nThreshold-led acceptance conditions:")
    print(f"  1. Both thresholds exist: {bool(pm_thr and k_thr)}")
    if pm_thr and k_thr:
        print(f"  2. Thresholds equal: {_threshold_equal(pm_thr, k_thr)}")
    else:
        print(f"  2. Thresholds equal: N/A (missing threshold)")
    print(f"  3. Shared entity: {bool(shared)}")
    print(f"  4. Delta time exists and <= 72h: {dh is not None and (dh <= 72.0 if dh is not None else False)}")

    if pm_thr and k_thr and _threshold_equal(pm_thr, k_thr) and shared and dh is not None and dh <= 72.0:
        print(f"  => WOULD PASS threshold-led acceptance")
    else:
        print(f"  => WOULD FAIL threshold-led acceptance")

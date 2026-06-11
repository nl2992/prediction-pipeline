"""Regression gate on the frozen live top-20 cross-platform fixture.

tests/fixtures/live_top20.json is a static snapshot of the top 20 genuine
Polymarket↔Kalshi pairs hand-extracted from live data on 2026-06-11 (top-50 PM
events by 24h volume × full Kalshi open-event catalog). All 20 are TRUE pairs:
both v1 (production) and v2 (contract_spec) must match every one, pairwise,
with zero misalignments in pooled assignment.
"""

from __future__ import annotations

import json
import os
import unittest

from contract_spec import explain
from matcher import match_markets
from pipeline import MarketSnapshot, OrderBook, PriceLevel

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "live_top20.json")


def _snap(source: str, mid: str, title: str, close: str) -> MarketSnapshot:
    return MarketSnapshot(
        source=source, market_id=mid, event_id="", title=title, status="open",
        close_time=close, fetched_at="x",
        orderbook=OrderBook(bids=[PriceLevel(0.4, 9.0)], asks=[PriceLevel(0.5, 9.0)]),
        extra={},
    )


def _load():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["pairs"]


class LiveTop20Gate(unittest.TestCase):
    def test_v1_matches_all_pairs_pairwise(self) -> None:
        misses = []
        for i, p in enumerate(_load()):
            pm = _snap("polymarket", f"pm-{i}", p["polymarket"]["question"], p["polymarket"]["end_date"])
            k = _snap("kalshi", p["kalshi"]["ticker"], p["kalshi"]["title"], p["kalshi"]["close"])
            if not match_markets([pm], [k], min_title_similarity=0.30, max_close_delta_hours=9999):
                misses.append(p["label"])
        self.assertEqual(misses, [], msg=f"v1 missed live pairs: {misses}")

    def test_v2_matches_all_pairs_pairwise(self) -> None:
        misses = []
        for i, p in enumerate(_load()):
            pm = _snap("polymarket", f"pm-{i}", p["polymarket"]["question"], p["polymarket"]["end_date"])
            k = _snap("kalshi", p["kalshi"]["ticker"], p["kalshi"]["title"], p["kalshi"]["close"])
            d = explain(pm, k)
            if not d.match:
                misses.append((p["label"], d.reasons[-1] if d.reasons else "?"))
        self.assertEqual(misses, [], msg=f"v2 missed live pairs: {misses}")

    def test_v1_pooled_assignment_no_misalignment(self) -> None:
        pms, ks, expect = [], [], {}
        for i, p in enumerate(_load()):
            pm = _snap("polymarket", f"pm-{i}", p["polymarket"]["question"], p["polymarket"]["end_date"])
            k = _snap("kalshi", p["kalshi"]["ticker"], p["kalshi"]["title"], p["kalshi"]["close"])
            pms.append(pm)
            ks.append(k)
            expect[pm.market_id] = k.market_id
        pairs = match_markets(pms, ks, min_title_similarity=0.30, max_close_delta_hours=9999)
        misaligned = [
            (mp.poly.title[:40], mp.kalshi.title[:40])
            for mp in pairs
            if expect[mp.poly.market_id] != mp.kalshi.market_id
        ]
        self.assertEqual(misaligned, [], msg=f"misaligned: {misaligned}")
        self.assertEqual(len(pairs), len(pms), msg="every live pair must be matched in pooled mode")


if __name__ == "__main__":
    unittest.main()

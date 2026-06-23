"""Score the matcher against the labelled pairs fixture (tests/fixtures/).

The fixture carries ground-truth should_match labels across exact, paraphrase,
inverted, and several mismatch-trap types. This test:
  * locks in pairs that are known-correct today (regression guard), and
  * asserts overall compatibility accuracy stays at or above the baseline,
so progress on the remaining hard paraphrase cases is tracked without flakiness.
"""
from __future__ import annotations

import json
import os
import unittest

from pipeline import MarketSnapshot, OrderBook, PriceLevel
from matcher import is_compatible_match, match_markets

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pairs_fixture.json")


def _ob(yb, ya):
    return OrderBook(bids=[PriceLevel(yb, 100.0)], asks=[PriceLevel(ya, 100.0)])


def _poly(p):
    return MarketSnapshot("polymarket", p.get("market_id", "pm"), p.get("slug", ""),
                          p["question"], "open", p.get("end_date_iso"), "",
                          _ob(p.get("yes_bid", 0.4), p.get("yes_ask", 0.5)), extra={})


def _kalshi(k):
    return MarketSnapshot("kalshi", k.get("ticker", "k"), k.get("ticker", ""),
                          k["title"], "open", k.get("close_time_iso"), "",
                          _ob(k.get("yes_bid", 0.4), k.get("yes_ask", 0.5)), extra={})


class FixturePairsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as f:   # explicit: file is UTF-8 (CI is Linux)
            cls.pairs = {pr["pair_id"]: pr for pr in json.load(f)["pairs"]}

    def _compat(self, pid):
        pr = self.pairs[pid]
        return is_compatible_match(_poly(pr["polymarket"]), _kalshi(pr["kalshi"]))

    def _returned(self, pid):
        pr = self.pairs[pid]
        return len(match_markets([_poly(pr["polymarket"])], [_kalshi(pr["kalshi"])],
                                 max_close_delta_hours=99999, min_title_similarity=0.30)) == 1

    def test_fixed_pairs_now_match(self):
        # Locked in by iteration 20 — must not regress.
        for pid in ("PAIR-001", "PAIR-002", "PAIR-003", "PAIR-027", "PAIR-034"):
            self.assertTrue(self._compat(pid), f"{pid} should be compatible")
        for pid in ("PAIR-011", "PAIR-013"):  # crypto threshold-led, via synonyms
            self.assertTrue(self._returned(pid), f"{pid} should be returned by match_markets")

    def test_clear_mismatches_rejected(self):
        # Traps the matcher already handles correctly — must stay rejected.
        for pid in ("PAIR-006", "PAIR-010", "PAIR-012"):
            self.assertFalse(self._compat(pid), f"{pid} must be rejected")

    def test_compat_accuracy_baseline(self):
        # Overall compatibility accuracy must not drop below the current level.
        correct = 0
        for pr in self.pairs.values():
            should = pr["ground_truth"]["should_match"]
            got = is_compatible_match(_poly(pr["polymarket"]), _kalshi(pr["kalshi"]))
            if got == should:
                correct += 1
        self.assertGreaterEqual(correct, 40, f"compat accuracy regressed: {correct}/50")


if __name__ == "__main__":
    unittest.main()

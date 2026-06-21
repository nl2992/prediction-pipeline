"""Tests for arb_charts: executable summary mapping + PNG generation / degradation."""
from __future__ import annotations

import unittest

from arb_charts import executable_summary, make_arb_chart


def signal_with_books():
    return {
        "direction": "buy YES on Polymarket + buy NO on Kalshi",
        "poly_book": {"bids": [[0.30, 100]], "asks": [[0.40, 100], [0.45, 100]]},
        "kalshi_book": {"bids": [[0.50, 150], [0.42, 100]], "asks": [[0.95, 100]]},
    }


def deep_book_signal():
    # Super-cheap PM leg + deep Kalshi book: unbounded max is ~1M contracts, but
    # the $5k/market tier is far smaller. Mirrors the Rahm-Emanuel longshot case.
    return {
        "key": "deep|x|buy YES on Polymarket + buy NO on Kalshi",
        "direction": "buy YES on Polymarket + buy NO on Kalshi",
        "poly_book": {"bids": [[0.01, 1000000]], "asks": [[0.02, 1000000]]},
        "kalshi_book": {"bids": [[0.04, 1000000]], "asks": [[0.99, 100]]},
    }


class ExecutableSummary(unittest.TestCase):
    def test_maps_direction_and_computes_profit(self):
        summ = executable_summary(signal_with_books())
        self.assertIsNotNone(summ)
        direction, res = summ
        self.assertEqual(direction, "poly_yes__kalshi_no")
        self.assertAlmostEqual(res["max"].contracts, 150.0, places=3)
        self.assertAlmostEqual(res["max"].profit, 9.875, places=3)

    def test_missing_books_returns_none(self):
        self.assertIsNone(executable_summary({"direction": "x"}))
        self.assertIsNone(executable_summary(
            {"direction": "buy YES on Polymarket + buy NO on Kalshi"}))

    def test_tolerates_book_levels_with_extra_fields(self):
        # Book rows that carry a 3rd field (e.g. order count) must not crash with
        # "too many values to unpack (expected 2)" — issue #7.
        s = {
            "direction": "buy YES on Polymarket + buy NO on Kalshi",
            "poly_book": {"bids": [[0.30, 100, 5]], "asks": [[0.40, 100, 3]]},
            "kalshi_book": {"bids": [[0.50, 150, 9]], "asks": [[0.95, 100, 1]]},
        }
        direction, res = executable_summary(s)
        self.assertEqual(direction, "poly_yes__kalshi_no")
        self.assertGreater(res["max"].contracts, 0)

    def test_budget_tier_far_below_unbounded_max(self):
        # The deep-book longshot: unbounded max is huge; the $5k/market tier is a
        # small, realistic fraction. The email headline must use the latter.
        from arb_charts import BUDGETS
        _, res = executable_summary(deep_book_signal())
        cap = res["by_budget"][max(BUDGETS)]
        self.assertGreater(res["max"].contracts, 100000)
        self.assertLess(cap.contracts, 10000)
        self.assertLessEqual(cap.cost_b, 5000 + 1)  # never exceeds $5k/market


class MakeChart(unittest.TestCase):
    def test_returns_png_bytes_when_books_present(self):
        png = make_arb_chart(signal_with_books())
        if png is None:
            self.skipTest("matplotlib not available")
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 1000)

    def test_returns_none_without_books(self):
        self.assertIsNone(make_arb_chart({"direction": "x"}))


if __name__ == "__main__":
    unittest.main()

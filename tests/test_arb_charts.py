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

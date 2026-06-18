"""Unit tests for the executable-arb / VWAP engine (book_arb.py).

Values are hand-computed from the constructed ladders so the test doubles as a
spec: this is what executing against the displayed book actually yields.
"""
from __future__ import annotations

import unittest

from pipeline import OrderBook, PriceLevel
from book_arb import build_buy_ladder, executable_arb, cumulative_curve


def ob(bids, asks):
    return OrderBook(bids=[PriceLevel(p, s) for p, s in bids],
                     asks=[PriceLevel(p, s) for p, s in asks])


class BuyLadder(unittest.TestCase):
    def test_yes_uses_asks_ascending(self):
        book = ob(bids=[(0.5, 10)], asks=[(0.45, 100), (0.40, 50)])
        self.assertEqual(build_buy_ladder(book, "yes"), [(0.40, 50), (0.45, 100)])

    def test_no_is_one_minus_yes_bid_cheapest_first(self):
        # YES bids 0.50/0.42 -> NO prices 0.50/0.58, cheapest NO first.
        book = ob(bids=[(0.50, 150), (0.42, 100)], asks=[(0.6, 10)])
        self.assertEqual(build_buy_ladder(book, "no"), [(0.50, 150), (0.58, 100)])


class ExecutableArb(unittest.TestCase):
    def setUp(self):
        # PM YES asks: 0.40x100, 0.45x100
        self.poly = ob(bids=[(0.30, 100)], asks=[(0.40, 100), (0.45, 100)])
        # Kalshi YES bids 0.50x150, 0.42x100 -> NO 0.50x150, 0.58x100
        self.kalshi = ob(bids=[(0.50, 150), (0.42, 100)], asks=[(0.95, 100)])

    def test_max_depth_vwap_and_profit(self):
        r = executable_arb(self.poly, self.kalshi, "poly_yes__kalshi_no")["max"]
        # 100 @ (0.40 + 0.50) then 50 @ (0.45 + 0.50); 3rd level (0.58 NO) unprofitable
        self.assertAlmostEqual(r.contracts, 150.0, places=3)
        self.assertAlmostEqual(r.vwap_a, 62.5 / 150, places=5)   # PM VWAP
        self.assertAlmostEqual(r.vwap_b, 0.50, places=5)          # Kalshi NO VWAP
        self.assertAlmostEqual(r.cost_a, 62.5, places=2)
        self.assertAlmostEqual(r.cost_b, 75.0, places=2)
        self.assertAlmostEqual(r.profit, 9.875, places=3)

    def test_budget_caps_deployment_per_market(self):
        r = executable_arb(self.poly, self.kalshi, "poly_yes__kalshi_no",
                           budgets=(20,))["by_budget"][20]
        # $20/market: Kalshi NO @0.50 caps at 40 contracts ($20); PM costs $16.
        self.assertAlmostEqual(r.contracts, 40.0, places=3)
        self.assertAlmostEqual(r.cost_b, 20.0, places=2)
        self.assertAlmostEqual(r.cost_a, 16.0, places=2)
        self.assertAlmostEqual(r.profit, 3.30, places=2)

    def test_no_arb_returns_zero(self):
        # Efficient books: PM ask 0.60 + Kalshi NO 0.55 = 1.15 > 1 -> nothing.
        poly = ob(bids=[(0.55, 100)], asks=[(0.60, 100)])
        kalshi = ob(bids=[(0.45, 100)], asks=[(0.55, 100)])   # NO = 0.55
        r = executable_arb(poly, kalshi, "poly_yes__kalshi_no")["max"]
        self.assertEqual(r.contracts, 0.0)
        self.assertEqual(r.profit, 0.0)


class CumulativeCurve(unittest.TestCase):
    def setUp(self):
        # PM YES asks: 0.40x100, 0.45x100
        self.poly = ob(bids=[(0.30, 100)], asks=[(0.40, 100), (0.45, 100)])
        # Kalshi YES bids 0.50x150, 0.42x100 -> NO 0.50x150, 0.58x100
        self.kalshi = ob(bids=[(0.50, 150), (0.42, 100)], asks=[(0.95, 100)])

    def test_curve_walks_full_overlap_and_crosses_breakeven(self):
        legA = build_buy_ladder(self.poly, "yes")        # PM YES
        legB = build_buy_ladder(self.kalshi, "no")        # Kalshi NO
        curve = cumulative_curve(legA, legB, "B")         # Kalshi is leg B
        # Lockstep chunks: 100 @ (0.40/0.50), 50 @ (0.45/0.50), 50 @ (0.45/0.58)
        self.assertEqual([round(c[0], 1) for c in curve], [100.0, 150.0, 200.0])
        # First two chunks profitable (<1), last chunk crosses break-even (>1)
        self.assertLess(curve[0][3], 1.0)
        self.assertLess(curve[1][3], 1.0)
        self.assertGreater(curve[2][3], 1.0)

    def test_empty_book_gives_empty_curve(self):
        empty = ob(bids=[], asks=[])
        legA = build_buy_ladder(empty, "yes")
        legB = build_buy_ladder(self.kalshi, "no")
        self.assertEqual(cumulative_curve(legA, legB, "B"), [])


if __name__ == "__main__":
    unittest.main()

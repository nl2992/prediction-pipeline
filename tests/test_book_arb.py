"""Unit tests for the executable-arb / VWAP engine (book_arb.py).

Values are hand-computed from the constructed ladders so the test doubles as a
spec: this is what executing against the displayed book actually yields.
"""
from __future__ import annotations

import unittest

from pipeline import OrderBook, PriceLevel
from book_arb import (
    build_buy_ladder, executable_arb, cumulative_curve, _fill,
    vwap_for_quantity, executable_edge_at_size,
)


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


class Fill(unittest.TestCase):
    """Depth-walk fill simulator: numbers are hand-computed (fee(0.5)=0.0175,
    so single-rung pair profit = 1-0.40-0.50-0.0175 = 0.0825) (#110)."""

    def test_single_rung(self):
        f = _fill([(0.40, 100.0)], [(0.50, 100.0)], "B", None)
        self.assertEqual(f.contracts, 100.0)
        self.assertAlmostEqual(f.profit, 8.25)
        self.assertAlmostEqual(f.vwap_a, 0.40)
        self.assertAlmostEqual(f.vwap_b, 0.50)

    def test_budget_caps_contracts(self):
        # budget 20 on the 0.50 leg -> 40 contracts (20/0.50).
        f = _fill([(0.40, 100.0)], [(0.50, 100.0)], "B", 20.0)
        self.assertEqual(f.contracts, 40.0)
        self.assertAlmostEqual(f.profit, 3.3)

    def test_stops_at_unprofitable_rung(self):
        # Rung 2 (0.49) makes the pair unprofitable, so only rung 1 fills.
        f = _fill([(0.40, 50.0), (0.49, 50.0)], [(0.50, 100.0)], "B", None)
        self.assertEqual(f.contracts, 50.0)
        self.assertAlmostEqual(f.profit, 4.125)

    def test_zero_when_no_profitable_overlap(self):
        f = _fill([(0.60, 100.0)], [(0.50, 100.0)], "B", None)
        self.assertEqual(f.contracts, 0.0)
        self.assertEqual(f.profit, 0.0)


class VwapForQuantity(unittest.TestCase):
    """Depth-walked VWAP to fill an exact quantity (iteration 4: min_size
    economics), as distinct from _fill's profitability-gated walk."""

    def test_fills_within_top_level(self):
        vwap, filled = vwap_for_quantity([(0.40, 100.0), (0.45, 50.0)], 20.0)
        self.assertAlmostEqual(vwap, 0.40)
        self.assertAlmostEqual(filled, 20.0)

    def test_walks_multiple_levels(self):
        # 10 @ 0.40 + 10 @ 0.45 -> vwap = (10*0.40 + 10*0.45)/20 = 0.425
        vwap, filled = vwap_for_quantity([(0.40, 10.0), (0.45, 50.0)], 20.0)
        self.assertAlmostEqual(vwap, 0.425)
        self.assertAlmostEqual(filled, 20.0)

    def test_short_fill_when_book_too_thin(self):
        vwap, filled = vwap_for_quantity([(0.40, 10.0)], 20.0)
        self.assertAlmostEqual(filled, 10.0)      # can't reach 20
        self.assertAlmostEqual(vwap, 0.40)

    def test_empty_book(self):
        vwap, filled = vwap_for_quantity([], 20.0)
        self.assertEqual(filled, 0.0)
        self.assertEqual(vwap, 0.0)


class ExecutableEdgeAtSize(unittest.TestCase):
    """book_arb.executable_edge_at_size: the depth-walked economics behind
    alerter.compute_signals' min_size gate (iteration 4)."""

    def test_matches_fill_when_top_level_covers_qty(self):
        poly = ob(bids=[(0.30, 100)], asks=[(0.40, 100)])
        kalshi = ob(bids=[(0.50, 150)], asks=[(0.95, 100)])
        res = executable_edge_at_size(poly, kalshi, "poly_yes__kalshi_no", 20.0)
        self.assertIsNotNone(res)
        # 20 contracts fully at top-of-book on both legs -> vwap == top price,
        # and net == gross minus the Kalshi fee on that price (same as _fill's
        # single-rung net for this book, since 20 < the 100-contract top level).
        self.assertAlmostEqual(res["vwap_a"], 0.40)
        self.assertAlmostEqual(res["vwap_b"], 0.50)
        single_rung = _fill([(0.40, 20.0)], [(0.50, 20.0)], "B", None)
        self.assertAlmostEqual(res["net"], round(single_rung.profit / single_rung.contracts, 6))

    def test_none_when_depth_insufficient(self):
        poly = ob(bids=[(0.30, 100)], asks=[(0.40, 5)])   # only 5 contracts total
        kalshi = ob(bids=[(0.50, 150)], asks=[(0.95, 100)])
        res = executable_edge_at_size(poly, kalshi, "poly_yes__kalshi_no", 20.0)
        self.assertIsNone(res)

    def test_thin_top_level_walks_into_worse_price(self):
        # 1 contract at a great price, then depth at a much worse price.
        poly = ob(bids=[(0.30, 100)], asks=[(0.05, 1), (0.60, 100)])
        kalshi = ob(bids=[(0.65, 1), (0.10, 100)], asks=[(0.95, 100)])
        res = executable_edge_at_size(poly, kalshi, "poly_yes__kalshi_no", 25)
        self.assertIsNotNone(res)
        # VWAP over 25 contracts is dominated by the deep (worse) level, not
        # the single-contract stale top level.
        self.assertGreater(res["vwap_a"], 0.5)
        self.assertLess(res["net"], 0)   # no real edge once walked

    def test_zero_qty_is_none(self):
        poly = ob(bids=[(0.30, 100)], asks=[(0.40, 100)])
        kalshi = ob(bids=[(0.50, 150)], asks=[(0.95, 100)])
        self.assertIsNone(executable_edge_at_size(poly, kalshi, "poly_yes__kalshi_no", 0))


if __name__ == "__main__":
    unittest.main()

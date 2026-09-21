"""Ladder synthesis: Kalshi cumulative rungs vs Polymarket range buckets."""
from __future__ import annotations

import math
import unittest

from ladder_match import (
    edges,
    parse_kalshi_rung,
    parse_poly_bucket,
    race_key,
    synthesize,
)
from pipeline import MarketSnapshot, OrderBook, PriceLevel


def k(ticker, sub, event, price=0.30):
    return MarketSnapshot("kalshi", ticker, "KXMIDTERMMOV-WYSENR",
                          f"Will the margin of victory be...? {sub}", "active", None, "",
                          OrderBook(bids=[PriceLevel(price, 100)], asks=[PriceLevel(price + 0.02, 100)]),
                          extra={"event_title": event, "yes_sub_title": sub,
                                 "floor_strike": float(sub.split(",")[1].strip().split("+")[0])})


def p(mid, label, event, price, ask=None):
    """A PM bucket. `price` is the real bid; the snapshot orderbook deliberately
    holds a single mid on both sides, exactly as discover() builds it, so a test
    that priced off it would see the wrong number."""
    return MarketSnapshot("polymarket", mid, "pe", label, "open", None, "",
                          OrderBook(bids=[PriceLevel(price, 100)], asks=[PriceLevel(price, 100)]),
                          extra={"event_title": event, "catalog_bid": price,
                                 "catalog_ask": price if ask is None else ask})


class Parsing(unittest.TestCase):
    def test_race_key_matches_across_venues(self):
        self.assertEqual(race_key("Wyoming Senate margin of victory"),
                         race_key("Wyoming Senate Election Margin of Victory"))

    def test_kalshi_rung(self):
        s = k("K-P26", "Republicans, 26+ pts", "Wyoming Senate margin of victory")
        self.assertEqual(parse_kalshi_rung(s), ("republican", 26.0))

    def test_candidate_named_rung(self):
        s = k("K-P26", "Hageman, 26+ pts", "Wyoming Senate margin of victory")
        self.assertEqual(parse_kalshi_rung(s), ("hageman", 26.0))

    def test_bucket_forms(self):
        ev = "Wyoming Senate Election Margin of Victory"
        self.assertEqual(parse_poly_bucket(p("a", "Republican 25-30%", ev, .1)), ("republican", 25.0, 30.0))
        self.assertEqual(parse_poly_bucket(p("b", "Republican 30%+", ev, .1)), ("republican", 30.0, math.inf))
        self.assertEqual(parse_poly_bucket(p("c", "Lula da Silva <5%", ev, .1)), ("lula da silva", 0.0, 5.0))
        self.assertEqual(parse_poly_bucket(p("d", "Republican Wins", ev, .1)), ("republican", 0.0, math.inf))


class Synthesis(unittest.TestCase):
    KEV = "Wyoming Senate margin of victory"
    PEV = "Wyoming Senate Election Margin of Victory"

    def _buckets(self):
        ev = "Wyoming Senate Election Margin of Victory"
        # a complete ladder: contiguous, unbounded at the top
        return [p("b1", "Republican 0-5%", ev, 0.10), p("b2", "Republican 5-10%", ev, 0.15),
                p("b3", "Republican 10%+", ev, 0.20), p("b4", "Democrat 0-5%", ev, 0.30)]

    def test_threshold_on_a_bucket_edge_is_exact(self):
        rec = synthesize([k("K1", "Republicans, 10+ pts", "Wyoming Senate margin of victory")],
                         self._buckets())
        self.assertEqual(len(rec), 1)
        self.assertTrue(rec[0]["exact"])
        # only the 10-15 bucket is at or above 10, and both baskets agree
        self.assertAlmostEqual(rec[0]["sub_bid"], 0.20)
        self.assertAlmostEqual(rec[0]["sup_ask"], 0.20)

    def test_threshold_inside_a_bucket_gives_two_different_baskets(self):
        rec = synthesize([k("K1", "Republicans, 7+ pts", "Wyoming Senate margin of victory")],
                         self._buckets())
        self.assertFalse(rec[0]["exact"])
        # dominated basket = 10-15 only; dominating basket adds the straddling 5-10
        self.assertAlmostEqual(rec[0]["sub_bid"], 0.20)
        self.assertAlmostEqual(rec[0]["sup_ask"], 0.35)
        self.assertEqual(rec[0]["sub_legs"], ["b3"])
        self.assertEqual(sorted(rec[0]["sup_legs"]), ["b2", "b3"])

    def test_prices_come_from_the_real_book_not_the_snapshot_mid(self):
        ev = "Wyoming Senate Election Margin of Victory"
        bucket = p("b3", "Republican 10%+", ev, 0.20, ask=0.26)
        bucket.orderbook = OrderBook(bids=[PriceLevel(0.99, 1)], asks=[PriceLevel(0.99, 1)])
        rec = synthesize([k("K1", "Republicans, 10+ pts", "Wyoming Senate margin of victory")],
                         [bucket])
        self.assertAlmostEqual(rec[0]["sub_bid"], 0.20)
        self.assertAlmostEqual(rec[0]["sup_ask"], 0.26)

    def test_unquoted_leg_kills_only_the_side_that_needs_it(self):
        ev = "Wyoming Senate Election Margin of Victory"
        straddle = p("b2", "Republican 5-10%", ev, 0.15)
        straddle.extra["catalog_ask"] = None          # cannot buy the dominating basket
        rec = synthesize([k("K1", "Republicans, 7+ pts", "Wyoming Senate margin of victory")],
                         [straddle, p("b3", "Republican 10%+", ev, 0.20)])
        self.assertAlmostEqual(rec[0]["sub_bid"], 0.20)
        self.assertIsNone(rec[0]["sup_ask"])

    def test_capped_top_bucket_has_no_dominating_basket(self):
        # "10-15%" pays nothing on a 20-point win, so buying it would not cover
        # a short Kalshi "10+ pts" rung.
        ev = "Wyoming Senate Election Margin of Victory"
        rec = synthesize([k("K1", "Republicans, 10+ pts", "Wyoming Senate margin of victory")],
                         [p("b3", "Republican 10-15%", ev, 0.20)])
        self.assertIsNone(rec[0]["sup_ask"])
        self.assertAlmostEqual(rec[0]["sub_bid"], 0.20)       # selling it is still safe

    def test_gap_in_the_ladder_has_no_dominating_basket(self):
        ev = "Wyoming Senate Election Margin of Victory"
        rec = synthesize([k("K1", "Republicans, 5+ pts", "Wyoming Senate margin of victory")],
                         [p("b2", "Republican 5-10%", ev, 0.15),
                          p("b3", "Republican 15%+", ev, 0.20)])   # 10-15 missing
        self.assertIsNone(rec[0]["sup_ask"])

    def test_bare_wins_bucket_dominates(self):
        ev = "Wyoming Senate Election Margin of Victory"
        rec = synthesize([k("K1", "Republicans, 5+ pts", "Wyoming Senate margin of victory")],
                         [p("b9", "Republican Wins", ev, 0.55, ask=0.60)])
        self.assertAlmostEqual(rec[0]["sup_ask"], 0.60)       # loose, but it does dominate
        self.assertIsNone(rec[0]["sub_bid"])

    def test_other_side_buckets_are_not_summed(self):
        rec = synthesize([k("K1", "Republicans, 10+ pts", "Wyoming Senate margin of victory")],
                         self._buckets())
        self.assertNotIn("b4", rec[0]["sup_legs"])
        self.assertAlmostEqual(rec[0]["sub_bid"], 0.20)

    def test_no_counterpart_race_is_skipped(self):
        self.assertEqual(
            synthesize([k("K1", "Republicans, 10+ pts", "Ohio Senate margin of victory")],
                       self._buckets()), [])


class Edges(unittest.TestCase):
    def test_each_direction_uses_the_basket_that_dominates_correctly(self):
        # Selling the rung (bid 0.30) and buying the dominating basket (0.20)
        # is the profitable side here.
        rec = [{"race": "r", "side": "republican", "threshold": 7.0,
                "kalshi_ticker": "K1", "kalshi_title": "t",
                "kalshi_bid": 0.30, "kalshi_ask": 0.32, "exact": False,
                "sub_bid": 0.10, "sup_ask": 0.20,
                "sub_legs": ["b3"], "sup_legs": ["b2", "b3"], "leg_labels": []}]
        out = edges(rec, fee=0.0)
        self.assertAlmostEqual(out[0]["edge"], 0.10)          # 0.30 - 0.20
        self.assertIn("buy above+straddle", out[0]["direction"])
        self.assertEqual(out[0]["legs"], ["b2", "b3"])

    def test_long_rung_short_dominated_basket(self):
        rec = [{"race": "r", "side": "republican", "threshold": 10.0,
                "kalshi_ticker": "K1", "kalshi_title": "t",
                "kalshi_bid": 0.20, "kalshi_ask": 0.22, "exact": True,
                "sub_bid": 0.40, "sup_ask": 0.40,
                "sub_legs": ["b3"], "sup_legs": ["b3"], "leg_labels": []}]
        out = edges(rec, fee=0.0)
        self.assertAlmostEqual(out[0]["edge"], 0.18)          # 0.40 - 0.22
        self.assertIn("buy rung on Kalshi", out[0]["direction"])

    def test_fee_is_charged_on_the_kalshi_leg(self):
        rec = [{"race": "r", "side": "republican", "threshold": 10.0,
                "kalshi_ticker": "K1", "kalshi_title": "t",
                "kalshi_bid": 0.20, "kalshi_ask": 0.50, "exact": True,
                "sub_bid": 0.55, "sup_ask": 0.55,
                "sub_legs": ["b3"], "sup_legs": ["b3"], "leg_labels": []}]
        out = edges(rec)
        self.assertAlmostEqual(out[0]["edge"], round(0.55 - 0.50 - 0.07 * 0.25, 4))


if __name__ == "__main__":
    unittest.main()

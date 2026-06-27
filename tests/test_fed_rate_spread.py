"""Tests for the Fed-rate monotonicity-arb logic (fed_rate_spread.py, #41)."""
from __future__ import annotations

import unittest

from fed_rate_spread import (
    check_monotonicity, compute_monotonicity_arb, build_kalshi_distribution,
    _f, _mid, _rate_from_ticker, _parse_poly_price,
)


class FedParseHelpers(unittest.TestCase):
    """Raw venue data -> prices/rates for the distribution + monotonicity math (#139)."""

    def test_f(self):
        self.assertAlmostEqual(_f("0.5"), 0.5)
        self.assertIsNone(_f("0"))      # 0 = no resting order
        self.assertIsNone(_f("x"))
        self.assertIsNone(_f(None))

    def test_mid(self):
        self.assertAlmostEqual(_mid(0.40, 0.42), 0.41)
        self.assertAlmostEqual(_mid(0.40, None), 0.40)   # bid-only
        self.assertAlmostEqual(_mid(None, 0.42), 0.42)   # ask-only
        self.assertIsNone(_mid(None, None))

    def test_rate_from_ticker(self):
        self.assertAlmostEqual(_rate_from_ticker("KXFED-26JUN-T3.50"), 3.5)
        self.assertIsNone(_rate_from_ticker("NOPE"))

    def test_parse_poly_price(self):
        self.assertAlmostEqual(_parse_poly_price({"outcomePrices": '["0.62", "0.38"]'}), 0.62)
        self.assertAlmostEqual(_parse_poly_price({"outcomePrices": [0.55, 0.45]}), 0.55)
        self.assertIsNone(_parse_poly_price({"outcomePrices": "notjson"}))
        self.assertIsNone(_parse_poly_price({}))


class BuildKalshiDistribution(unittest.TestCase):
    def test_implied_probability_arithmetic(self):
        ladder = [
            {"ticker": "KXFED-T3.50", "yes_bid_dollars": "0.78", "yes_ask_dollars": "0.82"},  # mid .80
            {"ticker": "KXFED-T3.75", "yes_bid_dollars": "0.48", "yes_ask_dollars": "0.52"},  # mid .50
        ]
        dist = {round(d["implied_rate"], 2): d for d in build_kalshi_distribution(ladder)}
        self.assertAlmostEqual(dist[3.50]["p_above"], 0.80)
        self.assertAlmostEqual(dist[3.50]["p_this_level"], 0.20)   # 1.0 - 0.80
        self.assertAlmostEqual(dist[3.75]["p_this_level"], 0.30)   # 0.80 - 0.50 (p_prev - p_above)
        self.assertAlmostEqual(dist[3.25]["p_this_level"], 0.20)   # residual tail = 1 - 0.80
        self.assertIsNone(dist[3.25]["p_above"])                   # tail has no p_above

    def test_empty_ladder(self):
        self.assertEqual(build_kalshi_distribution([]), [])


class CheckMonotonicity(unittest.TestCase):
    def test_ascending_flags_price_increasing_with_rate(self):
        # P(reach >= rate) should DECREASE as rate rises; rising price is a violation.
        ladder = [{"rate": 4.00, "price": 0.50}, {"rate": 4.25, "price": 0.60}]
        v = check_monotonicity(ladder, "ascending")
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["rate_low"], 4.00)
        self.assertEqual(v[0]["rate_high"], 4.25)
        self.assertAlmostEqual(v[0]["excess"], 0.10)

    def test_ascending_monotone_ladder_no_violation(self):
        ladder = [{"rate": 4.00, "price": 0.60}, {"rate": 4.25, "price": 0.50}]
        self.assertEqual(check_monotonicity(ladder, "ascending"), [])

    def test_descending_on_reverse_sorted_ladder(self):
        # lower_reach is reverse-sorted (rate descending); price rising along the list
        # is the violation, and rate_low/high are labelled accordingly.
        ladder = [{"rate": 4.25, "price": 0.50}, {"rate": 4.00, "price": 0.60}]
        v = check_monotonicity(ladder, "descending")
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["rate_low"], 4.00)
        self.assertEqual(v[0]["rate_high"], 4.25)

    def test_skips_none_prices(self):
        ladder = [{"rate": 4.00, "price": None}, {"rate": 4.25, "price": 0.60}]
        self.assertEqual(check_monotonicity(ladder, "ascending"), [])


class ComputeMonotonicityArb(unittest.TestCase):
    def _violation(self, p_low, p_high):
        return {"rate_low": 4.00, "rate_high": 4.25, "p_low": p_low, "p_high": p_high,
                "excess": round(p_high - p_low, 4)}

    def test_arb_math(self):
        arb = compute_monotonicity_arb(self._violation(0.30, 0.40), fee_rate=0.02)
        self.assertAlmostEqual(arb["buy_yes_cost"], 0.30)            # p_low
        self.assertAlmostEqual(arb["buy_no_cost"], 0.60)            # 1 - p_high
        self.assertAlmostEqual(arb["gross_cost"], 0.90)
        self.assertAlmostEqual(arb["gross_profit"], 0.10)          # p_high - p_low
        self.assertAlmostEqual(arb["net_profit_after_fee"], 0.08)
        self.assertTrue(arb["profitable"])

    def test_not_profitable_when_excess_below_fee(self):
        arb = compute_monotonicity_arb(self._violation(0.40, 0.41), fee_rate=0.02)
        self.assertFalse(arb["profitable"])                         # 0.01 edge < 0.02 fee


if __name__ == "__main__":
    unittest.main()

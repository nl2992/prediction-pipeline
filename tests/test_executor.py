"""Tests for the executor pre-flight combined live-edge recheck (#33)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from executor import TradeIntent, TradeResult, ArbExecution, pre_flight_checks


def _tr(success: bool):
    intent = TradeIntent("kalshi", "YES", 0.5, 1, "KX", None, "x")
    return TradeResult(intent=intent, success=success, dry_run=False)


class NakedExposure(unittest.TestCase):
    def _exec(self, a_ok, b_ok, cancelled):
        return ArbExecution(leg_a=_tr(a_ok), leg_b=_tr(b_ok), dry_run=False,
                            cancelled_leg_a=cancelled)

    def test_true_when_legA_filled_legB_failed_not_cancelled(self):
        self.assertTrue(self._exec(True, False, False).naked_exposure)

    def test_false_when_both_succeed(self):
        self.assertFalse(self._exec(True, True, False).naked_exposure)

    def test_false_when_legA_cancelled(self):
        self.assertFalse(self._exec(True, False, True).naked_exposure)

    def test_false_when_legA_never_filled(self):
        self.assertFalse(self._exec(False, False, False).naked_exposure)


def _legs():
    # Stale prices show a profitable edge: gross 0.97, net (fee 0.02) = +0.01.
    a = TradeIntent("polymarket", "YES", 0.47, 5, "PMID", "tok", "PM YES")
    b = TradeIntent("kalshi", "NO", 0.50, 5, "KX", None, "Kalshi NO")
    return a, b


class PreflightHedgeStructure(unittest.TestCase):
    def test_same_exchange_aborts(self):
        a = TradeIntent("kalshi", "YES", 0.40, 5, "KX1", None, "a")
        b = TradeIntent("kalshi", "NO", 0.50, 5, "KX2", None, "b")
        with patch("executor.check_price_still_valid", return_value=(True, "ok", 0.4)):
            go, msgs = pre_flight_checks(a, b, max_position_usd=500, fee_a=0.0, fee_b=0.0)
        self.assertFalse(go)
        self.assertTrue(any("same exchange" in m for m in msgs))

    def test_same_side_aborts(self):
        a = TradeIntent("polymarket", "YES", 0.40, 5, "PM", "tok", "a")
        b = TradeIntent("kalshi", "YES", 0.50, 5, "KX", None, "b")
        with patch("executor.check_price_still_valid", return_value=(True, "ok", 0.4)):
            go, msgs = pre_flight_checks(a, b, max_position_usd=500, fee_a=0.0, fee_b=0.0)
        self.assertFalse(go)
        self.assertTrue(any("same side" in m for m in msgs))

    def test_valid_hedge_passes_structure(self):
        a = TradeIntent("polymarket", "YES", 0.45, 5, "PM", "tok", "a")
        b = TradeIntent("kalshi", "NO", 0.45, 5, "KX", None, "b")
        with patch("executor.check_price_still_valid", return_value=(True, "ok", 0.45)):
            go, msgs = pre_flight_checks(a, b, max_position_usd=500, fee_a=0.0, fee_b=0.0)
        self.assertTrue(go, msgs)
        self.assertFalse(any("same exchange" in m or "same side" in m for m in msgs))


class PreflightCombinedEdge(unittest.TestCase):
    def test_combined_live_edge_negative_aborts(self):
        a, b = _legs()
        # Each leg drifts within per-leg tolerance, but combined live gross = 1.03
        # -> live net = 1 - 1.03 - 0.02 = -0.05. Per-leg checks pass; 3b must FAIL.
        def fake(intent, price_tolerance=0.02):
            return (True, "within tolerance", 0.51 if intent.exchange == "polymarket" else 0.52)
        with patch("executor.check_price_still_valid", side_effect=fake):
            go, msgs = pre_flight_checks(a, b, max_position_usd=500, fee_a=0.0, fee_b=0.02)
        self.assertFalse(go)
        self.assertTrue(any("live combined edge" in m and "FAIL" in m for m in msgs))

    def test_combined_live_edge_positive_passes(self):
        a, b = _legs()
        # Live gross 0.95 -> live net = 1 - 0.95 - 0.02 = +0.03 -> 3b OK.
        def fake(intent, price_tolerance=0.02):
            return (True, "ok", 0.47 if intent.exchange == "polymarket" else 0.48)
        with patch("executor.check_price_still_valid", side_effect=fake):
            go, msgs = pre_flight_checks(a, b, max_position_usd=500, fee_a=0.0, fee_b=0.02)
        self.assertTrue(go, msgs)
        self.assertTrue(any("live combined edge" in m and "OK" in m for m in msgs))


if __name__ == "__main__":
    unittest.main()

"""Tests for the executor pre-flight combined live-edge recheck (#33)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from executor import TradeIntent, pre_flight_checks


def _legs():
    # Stale prices show a profitable edge: gross 0.97, net (fee 0.02) = +0.01.
    a = TradeIntent("polymarket", "YES", 0.47, 5, "PMID", "tok", "PM YES")
    b = TradeIntent("kalshi", "NO", 0.50, 5, "KX", None, "Kalshi NO")
    return a, b


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

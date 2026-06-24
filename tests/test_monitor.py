"""Tests for monitor's live-price re-check best-level selection (#63)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from monitor import _verify_kalshi_clob, _detect_pair_arb


class DetectPairArb(unittest.TestCase):
    def test_poly_yes_direction_only(self):
        # pyk profitable (0.25), kyp unprofitable -> picks pyk.
        d, profit, _cp, _ck = _detect_pair_arb(pa=0.40, pb=0.30, ka=0.60, kb=0.65, worst_fee=0.0)
        self.assertEqual(d, "poly_yes__kalshi_no")
        self.assertAlmostEqual(profit, 0.25)

    def test_kalshi_yes_direction_when_better(self):
        # pyk = 0 (not >0 -> None), kyp = 0.30 -> picks kyp.
        d, profit, _cp, _ck = _detect_pair_arb(pa=0.50, pb=0.60, ka=0.30, kb=0.50, worst_fee=0.0)
        self.assertEqual(d, "kalshi_yes__poly_no")
        self.assertAlmostEqual(profit, 0.30)

    def test_picks_higher_of_two_profitable(self):
        # pyk = 0.50, kyp = 0.30 -> keeps pyk (the higher).
        d, profit, _cp, _ck = _detect_pair_arb(pa=0.30, pb=0.70, ka=0.40, kb=0.80, worst_fee=0.0)
        self.assertEqual(d, "poly_yes__kalshi_no")
        self.assertAlmostEqual(profit, 0.50)

    def test_no_arb_yields_nonpositive_profit(self):
        # neither direction profitable -> arb_profit <= 0 (caller filters).
        _d, profit, _cp, _ck = _detect_pair_arb(pa=0.60, pb=0.50, ka=0.60, kb=0.50, worst_fee=0.0)
        self.assertLessEqual(profit, 0)


class VerifyKalshiClob(unittest.TestCase):
    def test_live_ask_uses_highest_no_bid_regardless_of_order(self):
        # Unsorted no_dollars: best (highest) NO bid 0.35 -> YES ask = 1-0.35 = 0.65.
        book = {"orderbook_fp": {"no_dollars": [["0.30", "5"], ["0.35", "5"], ["0.32", "5"]],
                                 "yes_dollars": [["0.60", "5"]]}}
        with patch("kalshi.client.KalshiClient") as KC:
            KC.return_value.get_orderbook.return_value = book
            ok, details = _verify_kalshi_clob("KX", expected_price=0.65, side="ask", price_tolerance=0.02)
        self.assertEqual(details["live_yes_ask"], 0.65)
        self.assertTrue(ok)

    def test_live_bid_uses_highest_yes_bid_regardless_of_order(self):
        book = {"orderbook_fp": {"yes_dollars": [["0.40", "5"], ["0.45", "5"], ["0.42", "5"]],
                                 "no_dollars": [["0.50", "5"]]}}
        with patch("kalshi.client.KalshiClient") as KC:
            KC.return_value.get_orderbook.return_value = book
            ok, details = _verify_kalshi_clob("KX", expected_price=0.45, side="bid", price_tolerance=0.02)
        self.assertEqual(details["live_yes_bid"], 0.45)
        self.assertTrue(ok)


class MainExitCode(unittest.TestCase):
    """--once must surface a scan failure in the exit code (#127)."""

    def test_once_scan_error_exits_nonzero(self):
        import monitor
        with patch("sys.argv", ["monitor", "--once"]), \
             patch("monitor._setup_logging"), \
             patch("monitor.run_one_scan", side_effect=RuntimeError("boom")):
            with self.assertRaises(SystemExit) as cm:
                monitor.main()
        self.assertEqual(cm.exception.code, 1)

    def test_once_success_exits_zero(self):
        import monitor
        from types import SimpleNamespace
        ok_summary = SimpleNamespace(signals=[], verified_signals=0)
        with patch("sys.argv", ["monitor", "--once"]), \
             patch("monitor._setup_logging"), \
             patch("monitor.print_scan_summary"), \
             patch("monitor.run_one_scan", return_value=ok_summary):
            monitor.main()  # no SystemExit -> clean exit 0


if __name__ == "__main__":
    unittest.main()

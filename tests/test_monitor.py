"""Tests for monitor's live-price re-check best-level selection (#63)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from monitor import _verify_kalshi_clob


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


if __name__ == "__main__":
    unittest.main()

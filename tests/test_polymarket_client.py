"""Tests for verify_clob_liquidity best-level selection (polymarket.client, #61).

It must pick the true best by price (max bid / min ask), not trust raw list order
— the CLOB book isn't guaranteed best-first. Constructed without creds; get_orderbook
mocked, so no network."""
from __future__ import annotations

import unittest

from polymarket.client import PolymarketClient


class VerifyClobLiquidity(unittest.TestCase):
    def test_picks_best_regardless_of_list_order(self):
        c = PolymarketClient()
        # Deliberately UNSORTED: best bid (0.45) and best ask (0.55) are not first.
        c.get_orderbook = lambda tid: {
            "bids": [{"price": "0.40", "size": "100"}, {"price": "0.45", "size": "50"},
                     {"price": "0.30", "size": "20"}],
            "asks": [{"price": "0.60", "size": "80"}, {"price": "0.55", "size": "30"},
                     {"price": "0.70", "size": "10"}],
        }
        r = c.verify_clob_liquidity("tok")
        self.assertEqual(r["best_bid"], 0.45)
        self.assertEqual(r["bid_size"], 50.0)
        self.assertEqual(r["best_ask"], 0.55)
        self.assertEqual(r["ask_size"], 30.0)
        self.assertTrue(r["is_liquid"])

    def test_empty_book_is_none_and_illiquid(self):
        c = PolymarketClient()
        c.get_orderbook = lambda tid: {"bids": [], "asks": []}
        r = c.verify_clob_liquidity("tok")
        self.assertIsNone(r["best_bid"])
        self.assertIsNone(r["best_ask"])
        self.assertFalse(r["is_liquid"])

    def test_skips_malformed_levels(self):
        c = PolymarketClient()
        c.get_orderbook = lambda tid: {
            "bids": [{"price": "x", "size": "1"}, {"price": "0.42", "size": "5"}],
            "asks": [{"price": "0.58", "size": "7"}],
        }
        r = c.verify_clob_liquidity("tok")
        self.assertEqual(r["best_bid"], 0.42)   # malformed level skipped
        self.assertEqual(r["best_ask"], 0.58)


if __name__ == "__main__":
    unittest.main()

"""Tests for order-book parsing level validation (pipeline.py, #42)."""
from __future__ import annotations

import unittest

from pipeline import _parse_polymarket_book, _parse_kalshi_full_book, _is_valid_level


class IsValidLevel(unittest.TestCase):
    def test_accepts_in_range(self):
        self.assertTrue(_is_valid_level(0.5, 10))

    def test_rejects_boundaries_and_nonpositive_size(self):
        for p in (0.0, 1.0, -0.1, 1.5):
            self.assertFalse(_is_valid_level(p, 10))
        self.assertFalse(_is_valid_level(0.5, 0))
        self.assertFalse(_is_valid_level(0.5, -1))


class PolymarketBook(unittest.TestCase):
    def test_drops_degenerate_levels(self):
        raw = {
            "bids": [{"price": "0.40", "size": "100"}, {"price": "0", "size": "50"},
                     {"price": "1.0", "size": "20"}, {"price": "0.30", "size": "0"}],
            "asks": [{"price": "0.55", "size": "80"}, {"price": "1.2", "size": "5"}],
        }
        ob = _parse_polymarket_book(raw)
        self.assertEqual([(l.price, l.size) for l in ob.bids], [(0.40, 100.0)])
        self.assertEqual([(l.price, l.size) for l in ob.asks], [(0.55, 80.0)])

    def test_valid_book_preserved_and_sorted(self):
        raw = {"bids": [{"price": "0.40", "size": "1"}, {"price": "0.45", "size": "1"}],
               "asks": [{"price": "0.60", "size": "1"}, {"price": "0.55", "size": "1"}]}
        ob = _parse_polymarket_book(raw)
        self.assertEqual(ob.best_bid, 0.45)   # highest bid first
        self.assertEqual(ob.best_ask, 0.55)   # lowest ask first


class KalshiBook(unittest.TestCase):
    def test_drops_degenerate_levels(self):
        raw = {"orderbook_fp": {
            "yes_dollars": [["0.40", "100"], ["0", "50"], ["1.0", "20"]],
            "no_dollars": [["0.31", "32"], ["0", "10"]],
        }}
        ob = _parse_kalshi_full_book(raw)
        self.assertEqual([(l.price, l.size) for l in ob.bids], [(0.40, 100.0)])
        # ask derived from the one valid no-bid (0.31 -> 1-0.31 = 0.69)
        self.assertEqual([round(l.price, 2) for l in ob.asks], [0.69])


if __name__ == "__main__":
    unittest.main()

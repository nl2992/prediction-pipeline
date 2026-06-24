"""Tests for verify_clob_liquidity best-level selection (polymarket.client, #61).

It must pick the true best by price (max bid / min ask), not trust raw list order
— the CLOB book isn't guaranteed best-first. Constructed without creds; get_orderbook
mocked, so no network."""
from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from unittest.mock import patch

from polymarket.client import PolymarketClient

# unpadded urlsafe-b64 of b"abcd"; the client appends "==" before decoding.
_SECRET = "YWJjZA"
_KEY = base64.urlsafe_b64decode(_SECRET + "==")


def _expected_sig(ts: str, method: str, path: str, body: str) -> str:
    msg = ts + method.upper() + path + body.replace("'", '"')
    return base64.urlsafe_b64encode(
        hmac.new(_KEY, msg.encode("utf-8"), hashlib.sha256).digest()).decode()


class L2AuthSigning(unittest.TestCase):
    def test_signature_matches_recomputation(self):
        c = PolymarketClient(api_key="addr", api_secret=_SECRET, api_passphrase="pass")
        h = c._clob_auth_headers("GET", "/order", body="")
        self.assertEqual(h["POLY_API_KEY"], "addr")
        self.assertEqual(h["POLY_PASSPHRASE"], "pass")
        self.assertEqual(h["POLY_SIGNATURE"], _expected_sig(h["POLY_TIMESTAMP"], "GET", "/order", ""))

    def test_body_single_quotes_normalized(self):
        c = PolymarketClient(api_key="a", api_secret=_SECRET, api_passphrase="p")
        h = c._clob_auth_headers("POST", "/order", body="{'x': 1}")
        self.assertEqual(h["POLY_SIGNATURE"],
                         _expected_sig(h["POLY_TIMESTAMP"], "POST", "/order", "{'x': 1}"))

    def test_unauthenticated_raises(self):
        with patch.dict("os.environ", {}, clear=True):     # no POLY_* creds
            c = PolymarketClient()
            with self.assertRaises(RuntimeError):
                c._clob_auth_headers("GET", "/order")


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


class SearchMarketsPagination(unittest.TestCase):
    def test_stuck_page_terminates(self):
        c = PolymarketClient()
        calls = {"n": 0}
        def fake_get_markets(**kw):
            calls["n"] += 1
            if calls["n"] > 50:                       # broken guard would hang — fail fast
                raise AssertionError("search_markets did not terminate on a stuck page")
            return [{"conditionId": "same", "question": "alpha market"}]  # same page forever
        c.get_markets = fake_get_markets
        out = c.search_markets(["alpha"])
        self.assertLessEqual(calls["n"], 2)           # page 1, then no-new -> stop
        self.assertEqual(len(out), 1)                 # the one matching market, deduped

    def test_normal_pagination_finds_matches_then_ends(self):
        c = PolymarketClient()
        pages = [
            [{"conditionId": "1", "question": "alpha one"}, {"conditionId": "2", "question": "beta"}],
            [{"conditionId": "3", "question": "alpha three"}],
            [],   # end of catalog
        ]
        seq = iter(pages)
        c.get_markets = lambda **kw: next(seq, [])
        out = c.search_markets(["alpha"])
        self.assertEqual(sorted(m["conditionId"] for m in out), ["1", "3"])


class IsAuthenticated(unittest.TestCase):
    """Signed-request gate: needs api_key AND api_secret AND api_passphrase (#116)."""

    def test_no_creds(self):
        self.assertFalse(PolymarketClient()._is_authenticated)

    def test_partial_creds(self):
        self.assertFalse(PolymarketClient(api_key="a", api_secret="b")._is_authenticated)

    def test_full_creds(self):
        self.assertTrue(
            PolymarketClient(api_key="a", api_secret="b", api_passphrase="c")._is_authenticated)


if __name__ == "__main__":
    unittest.main()

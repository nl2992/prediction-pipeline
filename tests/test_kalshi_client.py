"""Tests for the Kalshi order-request construction (kalshi.client.place_order, #60).

The executor mocks the whole client, so the actual request body sent to place an
order is otherwise uncovered. KalshiClient is constructable without credentials and
place_order builds the body then calls _post, so mocking _post tests it with no
network or keys."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from kalshi.client import KalshiClient


def _client() -> KalshiClient:
    c = KalshiClient(api_key="test", private_key_path=None)
    c._post = MagicMock(return_value={"order_id": "o1", "status": "executed", "remaining_count": 0})
    return c


class PlaceOrderBody(unittest.TestCase):
    def test_request_path_and_fields(self):
        c = _client()
        c.place_order(ticker="KXTEST", side="bid", price=0.97, count=10,
                      time_in_force="fill_or_kill", client_order_id="cid-1")
        args, kwargs = c._post.call_args
        self.assertEqual(args[0], "/portfolio/orders")
        self.assertTrue(kwargs["authenticated"])
        b = kwargs["json_body"]
        self.assertEqual(b["ticker"], "KXTEST")
        self.assertEqual(b["side"], "bid")
        self.assertEqual(b["count"], "10.00")
        self.assertEqual(b["price"], "0.97")
        self.assertEqual(b["time_in_force"], "fill_or_kill")
        self.assertEqual(b["client_order_id"], "cid-1")

    def test_price_formatting_trims_trailing_zeros(self):
        for price, expected in [(0.97, "0.97"), (0.5, "0.5"), (0.055, "0.055"),
                                (0.4, "0.4"), (0.123456, "0.123456")]:
            c = _client()
            c.place_order(ticker="KX", side="ask", price=price, count=1)
            self.assertEqual(c._post.call_args.kwargs["json_body"]["price"], expected,
                             msg=f"price {price}")

    def test_auto_client_order_id_when_omitted(self):
        c = _client()
        c.place_order(ticker="KX", side="bid", price=0.5, count=1)
        self.assertTrue(c._post.call_args.kwargs["json_body"]["client_order_id"])  # uuid generated


class AuthSigning(unittest.TestCase):
    def test_signature_verifies_against_public_key(self):
        import base64
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
        from cryptography.hazmat.primitives import hashes
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        c = KalshiClient(api_key="my-key", private_key_path=None)
        c._private_key = priv                              # bypass PEM load
        headers = c._auth_headers("GET", "/trade-api/v2/markets")
        self.assertEqual(headers["KALSHI-ACCESS-KEY"], "my-key")
        self.assertTrue(headers["KALSHI-ACCESS-TIMESTAMP"])
        ts = headers["KALSHI-ACCESS-TIMESTAMP"]
        sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        message = f"{ts}GET/trade-api/v2/markets".encode()
        # verify() raises InvalidSignature on a bad signature; no raise == correct.
        priv.public_key().verify(
            sig, message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def test_unauthenticated_raises(self):
        c = KalshiClient(api_key=None, private_key_path=None)   # no key
        with self.assertRaises(RuntimeError):
            c._auth_headers("GET", "/trade-api/v2/markets")


class GetRetry(unittest.TestCase):
    def _ok(self):
        r = MagicMock(status_code=200)
        r.json.return_value = {"ok": True}
        r.raise_for_status.return_value = None
        return r

    def test_retries_on_request_exception_then_succeeds(self):
        import requests
        c = KalshiClient(api_key=None, private_key_path=None)
        c.session = MagicMock()
        c.session.get.side_effect = [requests.RequestException("blip"), self._ok()]
        with patch("time.sleep"):                     # no real backoff delay
            out = c._get("/markets")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(c.session.get.call_count, 2)

    def test_retries_on_429_then_succeeds(self):
        c = KalshiClient(api_key=None, private_key_path=None)
        c.session = MagicMock()
        c.session.get.side_effect = [MagicMock(status_code=429), self._ok()]
        with patch("time.sleep"):
            out = c._get("/markets")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(c.session.get.call_count, 2)


class Pagination(unittest.TestCase):
    def _client(self):
        return KalshiClient(api_key="test", private_key_path=None)

    def test_stuck_cursor_terminates(self):
        c = self._client()
        calls = {"n": 0}
        def fake_get_markets(**kw):
            calls["n"] += 1
            if calls["n"] > 50:                       # broken guard would hang — fail fast
                raise AssertionError("pagination did not terminate on a repeating cursor")
            return {"markets": [{"ticker": f"m{calls['n']}"}], "cursor": "STUCK"}  # never empties
        c.get_markets = fake_get_markets
        out = c.get_all_markets()
        self.assertLessEqual(calls["n"], 2)           # page 1, then repeat detected -> stop
        self.assertTrue(out)

    def test_normal_pagination_walks_all_pages(self):
        c = self._client()
        pages = [
            {"markets": [{"ticker": "a"}], "cursor": "c1"},
            {"markets": [{"ticker": "b"}], "cursor": "c2"},
            {"markets": [{"ticker": "d"}], "cursor": ""},   # end
        ]
        seq = iter(pages)
        c.get_markets = lambda **kw: next(seq)
        out = c.get_all_markets()
        self.assertEqual([m["ticker"] for m in out], ["a", "b", "d"])


class ParseTopOfBook(unittest.TestCase):
    """Raw market dict -> normalised top-of-book; 0.0/malformed/missing -> None (#115)."""

    def test_full_market_parsed(self):
        m = {
            "ticker": "KXFOO-T1", "event_ticker": "KXFOO", "title": "Foo?", "status": "active",
            "yes_bid_dollars": "0.59", "yes_ask_dollars": "0.62",
            "no_bid_dollars": "0.38", "no_ask_dollars": "0.41",
            "last_price_dollars": "0.60", "volume_24h_fp": "1234",
            "close_time": "2026-12-31T00:00:00Z",
        }
        r = KalshiClient.parse_top_of_book(m)
        self.assertAlmostEqual(r["yes_bid"], 0.59)
        self.assertAlmostEqual(r["no_ask"], 0.41)
        self.assertAlmostEqual(r["volume_24h"], 1234.0)
        self.assertEqual(r["ticker"], "KXFOO-T1")
        self.assertEqual(r["event_ticker"], "KXFOO")
        self.assertEqual(r["status"], "active")
        self.assertEqual(r["close_time"], "2026-12-31T00:00:00Z")

    def test_zero_malformed_missing_become_none(self):
        r = KalshiClient.parse_top_of_book(
            {"ticker": "X", "yes_bid_dollars": "0.0", "yes_ask_dollars": "bad"})
        self.assertIsNone(r["yes_bid"])   # 0.0 = no resting order
        self.assertIsNone(r["yes_ask"])   # malformed
        self.assertIsNone(r["no_bid"])    # missing


if __name__ == "__main__":
    unittest.main()

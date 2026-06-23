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


if __name__ == "__main__":
    unittest.main()

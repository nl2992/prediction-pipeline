"""Tests for the Kalshi order-request construction (kalshi.client.place_order, #60).

The executor mocks the whole client, so the actual request body sent to place an
order is otherwise uncovered. KalshiClient is constructable without credentials and
place_order builds the body then calls _post, so mocking _post tests it with no
network or keys."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

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


if __name__ == "__main__":
    unittest.main()

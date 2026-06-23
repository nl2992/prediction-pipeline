"""Tests for the executor pre-flight combined live-edge recheck (#33)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from executor import (
    TradeIntent, TradeResult, ArbExecution, Executor, pre_flight_checks, check_price_still_valid,
)


def _live_executor():
    """An Executor on the live path with a mocked Kalshi client (bypasses creds)."""
    ex = Executor(dry_run=True)
    ex.dry_run = False
    ex._kalshi_client = MagicMock()
    return ex


class KalshiLivePriceBestBid(unittest.TestCase):
    def test_yes_ask_uses_highest_no_bid_regardless_of_order(self):
        # Unsorted no_dollars: best (highest) NO bid is 0.35, not last. YES ask must
        # be 1 - 0.35 = 0.65, not 1 - 0.32 (the [-1] the old code would have used).
        book = {"orderbook_fp": {"no_dollars": [["0.30", "5"], ["0.35", "5"], ["0.32", "5"]]}}
        intent = TradeIntent("kalshi", "YES", 0.66, 10, "KX", None, "x")
        with patch("kalshi.client.KalshiClient") as KC:
            KC.return_value.get_orderbook.return_value = book
            ok, msg, live = check_price_still_valid(intent, price_tolerance=0.02)
        self.assertEqual(live, 0.65)
        self.assertTrue(ok)        # drift 0.01 <= 0.02

    def test_no_ask_uses_highest_yes_bid_regardless_of_order(self):
        book = {"orderbook_fp": {"yes_dollars": [["0.40", "5"], ["0.45", "5"], ["0.42", "5"]]}}
        intent = TradeIntent("kalshi", "NO", 0.55, 10, "KX", None, "x")  # NO ask = 1-0.45 = 0.55
        with patch("kalshi.client.KalshiClient") as KC:
            KC.return_value.get_orderbook.return_value = book
            ok, msg, live = check_price_still_valid(intent, price_tolerance=0.02)
        self.assertEqual(live, 0.55)


class KalshiSideMapping(unittest.TestCase):
    """The NO-side mapping (buy NO @ p == sell YES @ 1-p) is money-critical (#64)."""
    def _exec_capturing(self):
        ex = _live_executor()
        ex._kalshi_client.place_order.return_value = {
            "order_id": "o", "status": "executed", "remaining_count": 0}
        return ex

    def test_yes_maps_to_bid_at_same_price(self):
        ex = self._exec_capturing()
        ex._place_kalshi(TradeIntent("kalshi", "YES", 0.40, 10, "KX", None, "buy YES"))
        kw = ex._kalshi_client.place_order.call_args.kwargs
        self.assertEqual(kw["side"], "bid")
        self.assertEqual(kw["price"], 0.40)

    def test_no_maps_to_ask_at_complement_price(self):
        ex = self._exec_capturing()
        ex._place_kalshi(TradeIntent("kalshi", "NO", 0.30, 10, "KX", None, "buy NO"))
        kw = ex._kalshi_client.place_order.call_args.kwargs
        self.assertEqual(kw["side"], "ask")
        self.assertEqual(kw["price"], 0.70)        # 1 - 0.30


class KalshiFokFill(unittest.TestCase):
    def _intent(self):
        return TradeIntent("kalshi", "YES", 0.40, 10, "KX", None, "Buy YES")

    def test_filled_when_remaining_zero(self):
        ex = _live_executor()
        ex._kalshi_client.place_order.return_value = {
            "order_id": "o1", "status": "executed", "remaining_count": 0}
        r = ex._place_kalshi(self._intent())
        self.assertTrue(r.success)

    def test_not_filled_when_killed(self):
        ex = _live_executor()
        ex._kalshi_client.place_order.return_value = {
            "order_id": "o2", "status": "canceled", "remaining_count": 10}
        r = ex._place_kalshi(self._intent())
        self.assertFalse(r.success)
        self.assertIn("not filled", (r.error or ""))

    def test_falls_back_to_get_order_when_remaining_absent(self):
        ex = _live_executor()
        ex._kalshi_client.place_order.return_value = {"order_id": "o3", "status": "executed"}
        ex._kalshi_client.get_order.return_value = {"remaining_count": 0}
        r = ex._place_kalshi(self._intent())
        self.assertTrue(r.success)
        ex._kalshi_client.get_order.assert_called_once()

    def test_unconfirmable_treated_as_not_filled(self):
        ex = _live_executor()
        ex._kalshi_client.place_order.return_value = {"order_id": "o4", "status": "unknown"}
        ex._kalshi_client.get_order.return_value = {}        # still no remaining_count
        r = ex._place_kalshi(self._intent())
        self.assertFalse(r.success)


def _tr(success: bool):
    intent = TradeIntent("kalshi", "YES", 0.5, 1, "KX", None, "x")
    return TradeResult(intent=intent, success=success, dry_run=False)


class CancelConfirmed(unittest.TestCase):
    def test_kalshi_confirmed_only_when_reduced(self):
        ex = _live_executor()
        ex._kalshi_client.cancel_order.return_value = {"reduced_by": 5}
        self.assertTrue(ex._cancel_confirmed("kalshi", "o1"))
        ex._kalshi_client.cancel_order.return_value = {"reduced_by": 0}  # filled -> nothing cancelled
        self.assertFalse(ex._cancel_confirmed("kalshi", "o1"))

    def test_poly_confirmed_only_when_in_canceled_list(self):
        ex = Executor(dry_run=True); ex.dry_run = False; ex._poly_client = MagicMock()
        ex._poly_client.cancel_order.return_value = {"canceled": ["o2"]}
        self.assertTrue(ex._cancel_confirmed("polymarket", "o2"))
        ex._poly_client.cancel_order.return_value = {"canceled": [], "not_canceled": {"o2": "filled"}}
        self.assertFalse(ex._cancel_confirmed("polymarket", "o2"))


class ExecuteNakedPath(unittest.TestCase):
    def test_filled_legA_unfillable_legB_flags_naked(self):
        ex = Executor(dry_run=True); ex.dry_run = False
        ex._kalshi_client = MagicMock(); ex._poly_client = MagicMock()
        # leg A (kalshi) fills; leg B (poly) does not match; cancel of filled leg A reduces nothing.
        ex._kalshi_client.place_order.return_value = {"order_id": "a", "status": "executed", "remaining_count": 0}
        ex._poly_client.place_order.return_value = {"orderID": "b", "success": True, "status": "unmatched"}
        ex._kalshi_client.cancel_order.return_value = {"order_id": "a", "reduced_by": 0}
        leg_a = TradeIntent("kalshi", "YES", 0.40, 10, "KX", None, "A")
        leg_b = TradeIntent("polymarket", "NO", 0.55, 10, "PM", "tok", "B")
        res = ex.execute(leg_a, leg_b, skip_preflight=True)
        self.assertTrue(res.leg_a.success)
        self.assertFalse(res.leg_b.success)
        self.assertFalse(res.cancelled_leg_a)     # filled -> nothing cancelled
        self.assertTrue(res.naked_exposure)        # correctly surfaced, not masked


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


def _live_poly_executor():
    ex = Executor(dry_run=True)
    ex.dry_run = False
    ex._poly_client = MagicMock()
    return ex


class PolymarketFokFill(unittest.TestCase):
    def _intent(self):
        return TradeIntent("polymarket", "YES", 0.45, 10, "PM", "tok123", "Buy YES")

    def test_filled_when_matched(self):
        ex = _live_poly_executor()
        ex._poly_client.place_order.return_value = {"orderID": "x", "success": True, "status": "matched"}
        self.assertTrue(ex._place_polymarket(self._intent()).success)

    def test_not_filled_when_unmatched(self):
        ex = _live_poly_executor()
        ex._poly_client.place_order.return_value = {"orderID": "x", "success": True, "status": "unmatched"}
        self.assertFalse(ex._place_polymarket(self._intent()).success)

    def test_not_filled_when_rejected(self):
        ex = _live_poly_executor()
        ex._poly_client.place_order.return_value = {"success": False, "errorMsg": "insufficient balance"}
        r = ex._place_polymarket(self._intent())
        self.assertFalse(r.success)
        self.assertIn("insufficient balance", (r.error or ""))

    def test_resting_order_not_treated_as_fill(self):
        ex = _live_poly_executor()
        ex._poly_client.place_order.return_value = {"orderID": "x", "success": True, "status": "live"}
        self.assertFalse(ex._place_polymarket(self._intent()).success)


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


class PreflightLegSanity(unittest.TestCase):
    def test_missing_quote_price_zero_aborts(self):
        a = TradeIntent("polymarket", "YES", 0.0, 5, "PM", "tok", "a")  # missing quote -> 0.0
        b = TradeIntent("kalshi", "NO", 0.50, 5, "KX", None, "b")
        with patch("executor.check_price_still_valid", return_value=(True, "ok", 0.5)):
            go, msgs = pre_flight_checks(a, b, max_position_usd=500, fee_a=0.0, fee_b=0.0)
        self.assertFalse(go)
        self.assertTrue(any("not in (0,1)" in m for m in msgs))

    def test_nonpositive_size_aborts(self):
        a = TradeIntent("polymarket", "YES", 0.45, 0, "PM", "tok", "a")
        b = TradeIntent("kalshi", "NO", 0.45, 5, "KX", None, "b")
        with patch("executor.check_price_still_valid", return_value=(True, "ok", 0.45)):
            go, msgs = pre_flight_checks(a, b, max_position_usd=500, fee_a=0.0, fee_b=0.0)
        self.assertFalse(go)
        self.assertTrue(any("size" in m and "0" in m for m in msgs))

    def test_valid_legs_pass_sanity(self):
        a = TradeIntent("polymarket", "YES", 0.45, 5, "PM", "tok", "a")
        b = TradeIntent("kalshi", "NO", 0.45, 5, "KX", None, "b")
        with patch("executor.check_price_still_valid", return_value=(True, "ok", 0.45)):
            go, msgs = pre_flight_checks(a, b, max_position_usd=500, fee_a=0.0, fee_b=0.0)
        self.assertTrue(go, msgs)
        self.assertFalse(any("not in (0,1)" in m for m in msgs))


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

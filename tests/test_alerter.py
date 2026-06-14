"""Unit tests for the arb alerter: signal math, de-dup, and email body."""

from __future__ import annotations

import time
import unittest

from alerter import build_email, compute_signals, filter_new, signals_to_send


def pair(pb, pa, kb, ka, **extra):
    base = {
        "poly_bid": pb, "poly_ask": pa, "kalshi_bid": kb, "kalshi_ask": ka,
        "poly_title": "PM market?", "kalshi_title": "Kalshi market?",
        "poly_id": "pm1", "kalshi_ticker": "KX1", "poly_slug": "pm-market",
        "kalshi_series_ticker": "KXSER", "kalshi_event_title": "Kalshi market?",
        "confidence": 0.9, "v2_match": True,
    }
    base.update(extra)
    return base


class ComputeSignals(unittest.TestCase):
    def test_clear_arb_detected_with_accurate_fee(self) -> None:
        # PM YES ask 0.40, Kalshi NO ask = 1-0.65 = 0.35 -> gross 0.25.
        sigs = compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertGreater(s["net_accurate"], 0.20)
        self.assertIn("Polymarket", s["direction"])
        self.assertTrue(s["kalshi_url"].startswith("https://kalshi.com/markets/kxser/"))
        self.assertEqual(s["poly_url"], "https://polymarket.com/event/pm-market")

    def test_no_arb_filtered_out(self) -> None:
        # Tight, efficient books (the live Musk pair): no signal.
        sigs = compute_signals([pair(0.952, 0.963, 0.95, 0.96)], min_edge=0.005)
        self.assertEqual(sigs, [])

    def test_dust_below_threshold_filtered(self) -> None:
        # ~0.17c dust edge must NOT clear the default 0.5c bar.
        sigs = compute_signals([pair(0.006, 0.007, 0.003, 0.004)], min_edge=0.005)
        self.assertEqual(sigs, [])

    def test_best_direction_chosen(self) -> None:
        # Direction B (Kalshi YES + PM NO) is the profitable one here:
        # k_ask 0.30 + (1-pm_bid 0.74)=0.26 -> gross 0.44.
        sigs = compute_signals([pair(0.74, 0.76, 0.28, 0.30)], min_edge=0.005)
        self.assertEqual(len(sigs), 1)
        self.assertIn("Kalshi", sigs[0]["direction"].split("+")[0])


class FilterNew(unittest.TestCase):
    def _sig(self, net=0.05):
        return compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)[0]

    def test_new_signal_passes(self) -> None:
        self.assertEqual(len(filter_new([self._sig()], {}, realert_hours=6)), 1)

    def test_unchanged_recent_signal_suppressed(self) -> None:
        s = self._sig()
        state = {s["key"]: {"net": s["net_accurate"], "ts": time.time()}}
        self.assertEqual(filter_new([s], state, realert_hours=6), [])

    def test_stale_signal_realerted(self) -> None:
        s = self._sig()
        state = {s["key"]: {"net": s["net_accurate"], "ts": time.time() - 7 * 3600}}
        self.assertEqual(len(filter_new([s], state, realert_hours=6)), 1)

    def test_improved_edge_realerted(self) -> None:
        s = self._sig()
        state = {s["key"]: {"net": s["net_accurate"] - 0.01, "ts": time.time()}}
        self.assertEqual(len(filter_new([s], state, realert_hours=6)), 1)


class SignalsToSend(unittest.TestCase):
    """Trigger on change, but email EVERY positive-net pair."""

    def _sig(self, key, net):
        return {"key": key, "net_accurate": net,
                "poly_title": "p", "kalshi_title": "k"}

    def test_emails_all_positive_when_one_changed(self) -> None:
        # A is already known/unchanged, B is brand new. The trigger fires (B is
        # fresh) and BOTH positive pairs are emailed — not just B.
        a = self._sig("A", 0.05)
        b = self._sig("B", 0.03)
        state = {"A": {"net": 0.05, "ts": time.time()}}
        to_email, fresh = signals_to_send([a, b], state, realert_hours=6)
        self.assertEqual({s["key"] for s in to_email}, {"A", "B"})
        self.assertEqual({s["key"] for s in fresh}, {"B"})
        # sorted by net desc
        self.assertEqual([s["key"] for s in to_email], ["A", "B"])

    def test_no_email_when_nothing_changed(self) -> None:
        a = self._sig("A", 0.05)
        state = {"A": {"net": 0.05, "ts": time.time()}}
        to_email, fresh = signals_to_send([a], state, realert_hours=6)
        self.assertEqual(to_email, [])
        self.assertEqual(fresh, [])

    def test_non_positive_net_excluded(self) -> None:
        # Net <= 0 (not runnable after fees) is never emailed, even if "fresh".
        a = self._sig("A", 0.04)            # positive, new
        z = self._sig("Z", -0.01)           # negative net — excluded
        to_email, fresh = signals_to_send([a, z], {}, realert_hours=6)
        self.assertEqual({s["key"] for s in to_email}, {"A"})
        self.assertNotIn("Z", {s["key"] for s in fresh})


class EmailBody(unittest.TestCase):
    def test_subject_and_links_present(self) -> None:
        sigs = compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)
        subject, html = build_email(sigs)
        self.assertIn("[Pred-Arb]", subject)
        self.assertIn("net edge", subject)
        self.assertIn("https://polymarket.com/event/pm-market", html)
        self.assertIn("https://kalshi.com/markets/kxser/", html)
        self.assertIn("per $1", html)


if __name__ == "__main__":
    unittest.main()

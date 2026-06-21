"""Unit tests for the arb alerter: signal math, de-dup, and email body."""

from __future__ import annotations

import time
import unittest

from alerter import build_email, compute_signals, filter_new, signals_to_send, _exec_block, _settle_horizon


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
        # Direction B (Kalshi YES + PM NO) is the profitable one here, with a
        # realistic sub-25c edge: k_ask 0.66 + (1-pm_bid 0.74)=0.26 -> gross 0.08.
        # (Direction A is negative.) Edge stays under the phantom-edge guard.
        sigs = compute_signals([pair(0.74, 0.76, 0.64, 0.66)], min_edge=0.005)
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


class PrecisionGuards(unittest.TestCase):
    """compute_signals must drop v2-rejected and implausible-edge phantom arbs."""

    def test_v2_rejected_pair_excluded(self) -> None:
        rejected = pair(0.38, 0.40, 0.65, 0.68, v2_match=False)
        self.assertEqual(compute_signals([rejected], min_edge=0.005), [])
        # identical prices but v2-endorsed -> kept
        ok = pair(0.38, 0.40, 0.65, 0.68)  # v2_match True by default
        self.assertEqual(len(compute_signals([ok], min_edge=0.005)), 1)

    def test_phantom_huge_edge_excluded(self) -> None:
        # PM YES bid 0.98 vs Kalshi YES ask 0.03 -> ~95c "edge": a mismatch
        # (e.g. "Player X" vs "Player X: 2+ assists"), not a real arb.
        phantom = pair(0.98, 0.99, 0.02, 0.03)
        self.assertEqual(compute_signals([phantom], min_edge=0.005), [])

    def test_require_v2_can_be_disabled(self) -> None:
        rejected = pair(0.38, 0.40, 0.65, 0.68, v2_match=False)
        self.assertEqual(len(compute_signals([rejected], min_edge=0.005, require_v2=False)), 1)


class LiquidityFilter(unittest.TestCase):
    """min_size drops illiquid/one-sided books; default (0) changes nothing."""

    def test_thin_book_filtered(self) -> None:
        thin = pair(0.38, 0.40, 0.65, 0.68, poly_ask_size=2, kalshi_bid_size=2,
                    poly_bid_size=2, kalshi_ask_size=2)
        self.assertEqual(compute_signals([thin], min_edge=0.005, min_size=20), [])

    def test_deep_book_passes(self) -> None:
        deep = pair(0.38, 0.40, 0.65, 0.68, poly_ask_size=100, kalshi_bid_size=100,
                    poly_bid_size=100, kalshi_ask_size=100)
        self.assertEqual(len(compute_signals([deep], min_edge=0.005, min_size=20)), 1)

    def test_default_min_size_unchanged(self) -> None:
        # No size fields + default min_size=0 -> still produces the signal.
        self.assertEqual(len(compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)), 1)


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

    def test_top_n_caps_the_email(self) -> None:
        # 5 positive signals, top_n=3 -> email the 3 richest by net.
        sigs = [self._sig(f"K{i}", net) for i, net in enumerate([0.02, 0.10, 0.05, 0.01, 0.08])]
        to_email, fresh = signals_to_send(sigs, {}, realert_hours=6, top_n=3)
        self.assertEqual([round(s["net_accurate"], 2) for s in to_email], [0.10, 0.08, 0.05])

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


class AdaptiveScan(unittest.TestCase):
    """Progressive event-cap widening until >= target survivable arbs."""

    def _patch(self, per_cap):
        """Patch discover.discover to return ``per_cap[cap]`` profitable pairs."""
        import discover as discmod
        calls = []

        def fake_discover(**kw):
            cap = kw["max_events_to_search"]
            calls.append(cap)
            n = per_cap[cap]
            return [pair(0.38, 0.40, 0.65, 0.68,
                         poly_id=f"p{cap}_{i}", kalshi_ticker=f"K{cap}_{i}")
                    for i in range(n)]

        self._orig = discmod.discover
        discmod.discover = fake_discover
        self.addCleanup(lambda: setattr(discmod, "discover", self._orig))
        return calls

    def test_stops_at_first_cap_meeting_target(self) -> None:
        import alerter
        calls = self._patch({1000: 4, 1500: 9})
        pairs, sigs, cap = alerter.adaptive_scan(min_edge=0.005, target=3, caps=(1000, 1500), min_size=0)
        self.assertEqual(cap, 1000)
        self.assertEqual(calls, [1000])          # did NOT escalate
        self.assertEqual(len(sigs), 4)

    def test_escalates_when_short(self) -> None:
        import alerter
        calls = self._patch({1000: 2, 1500: 6})
        pairs, sigs, cap = alerter.adaptive_scan(min_edge=0.005, target=5, caps=(1000, 1500), min_size=0)
        self.assertEqual(cap, 1500)
        self.assertEqual(calls, [1000, 1500])    # escalated to last rung
        self.assertEqual(len(sigs), 6)


class AnnualisedRanking(unittest.TestCase):
    def test_annualised_uses_later_close_and_net_edge(self) -> None:
        from datetime import datetime, timezone
        yr = datetime.now(timezone.utc).year + 1
        s = {"net_accurate": 0.06, "poly_close": f"{yr}-01-01",
             "kalshi_close": f"{yr}-07-01"}  # later leg = July
        ann, days, settle = _settle_horizon(s)
        self.assertTrue(settle.endswith("-07-01"))     # later of the two dates
        self.assertAlmostEqual(ann, 0.06 * 365.0 / days, places=6)

    def test_near_dated_smaller_edge_outranks_far_dated_bigger(self) -> None:
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        far = (datetime.now(timezone.utc) + timedelta(days=540)).date().isoformat()
        near = {"net_accurate": 0.05, "poly_close": soon, "kalshi_close": soon}
        big = {"net_accurate": 0.20, "poly_close": far, "kalshi_close": far}
        self.assertGreater(_settle_horizon(near)[0], _settle_horizon(big)[0])

    def test_signals_to_send_ranks_by_annualised(self) -> None:
        from datetime import datetime, timedelta, timezone
        soon = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        far = (datetime.now(timezone.utc) + timedelta(days=540)).date().isoformat()
        near = {"net_accurate": 0.05, "key": "a", "poly_close": soon, "kalshi_close": soon}
        big = {"net_accurate": 0.20, "key": "b", "poly_close": far, "kalshi_close": far}
        out, _ = signals_to_send([big, near], state={}, realert_hours=6.0, min_net=0.03)
        self.assertEqual(out[0]["key"], "a")  # near-dated 5% ranks above far-dated 20%

    def test_missing_close_falls_back_without_crash(self) -> None:
        ann, days, settle = _settle_horizon({"net_accurate": 0.07})
        self.assertEqual((days, settle), (None, None))
        self.assertAlmostEqual(ann, 0.07)


class EmailBody(unittest.TestCase):
    def test_subject_and_links_present(self) -> None:
        sigs = compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)
        subject, html, images = build_email(sigs)
        self.assertIsInstance(images, list)
        self.assertIn("[Pred-Arb]", subject)
        self.assertIn(">3% net", subject)
        self.assertIn("https://polymarket.com/event/pm-market", html)
        self.assertIn("https://kalshi.com/markets/kxser/", html)
        self.assertIn("net edge", html)

    def test_books_produce_exec_block_and_inline_chart(self) -> None:
        p = pair(0.38, 0.40, 0.65, 0.68)
        p["poly_book"] = {"bids": [[0.30, 100]], "asks": [[0.40, 100], [0.45, 100]]}
        p["kalshi_book"] = {"bids": [[0.65, 200], [0.50, 100]], "asks": [[0.95, 100]]}
        sigs = compute_signals([p], min_edge=0.005)
        _, html, images = build_email(sigs)
        self.assertIn("Execute now", html)
        self.assertIn("Net profit by stake", html)
        if images:  # matplotlib present
            self.assertTrue(html.count("cid:") == len(images))
            self.assertTrue(images[0][1].startswith(b"\x89PNG"))

    def test_exec_block_headline_uses_budget_tier_not_unbounded_max(self) -> None:
        # Deep-book longshot: unbounded max ~1M contracts, but the headline must
        # report the realistic $5k/market tier (run: email-number sanity check).
        from arb_charts import executable_summary, BUDGETS
        s = {
            "key": "deep|x|buy YES on Polymarket + buy NO on Kalshi",
            "direction": "buy YES on Polymarket + buy NO on Kalshi",
            "poly_book": {"bids": [[0.01, 1000000]], "asks": [[0.02, 1000000]]},
            "kalshi_book": {"bids": [[0.04, 1000000]], "asks": [[0.99, 100]]},
        }
        _, res = executable_summary(s)
        cap = res["by_budget"][max(BUDGETS)]
        html, _png, _cid = _exec_block(s)
        self.assertIn(f"{cap.contracts:,.0f}", html)          # the $5k-tier figure
        self.assertNotIn(f"{res['max'].contracts:,.0f}", html)  # NOT the unbounded max
        self.assertIn("Execute now", html)

    def test_exec_block_vwap_labels_match_exchange_for_kalshi_yes_dir(self) -> None:
        # Direction "buy YES on Kalshi + buy NO on Polymarket": leg A is KALSHI,
        # leg B is PM. The headline must attribute each VWAP to the right exchange
        # (a hardcoded PM=vwap_a swapped them for this direction — run 58).
        from arb_charts import executable_summary, BUDGETS
        s = {
            "key": "k|x|buy YES on Kalshi + buy NO on Polymarket",
            "direction": "buy YES on Kalshi + buy NO on Polymarket",
            # Buy Kalshi YES @ 0.30 (ask) + PM NO @ 0.63 (1 - 0.37 bid) = 0.93 < 1.
            "poly_book": {"bids": [[0.37, 5000]], "asks": [[0.40, 5000]]},
            "kalshi_book": {"bids": [[0.25, 100]], "asks": [[0.30, 5000]]},
        }
        direction, res = executable_summary(s)
        cap = res["by_budget"][max(BUDGETS)]
        html, _p, _c = _exec_block(s)
        # leg A (vwap_a) is the Kalshi leg here -> must appear after "Kalshi",
        # leg B (vwap_b) is the PM leg -> must appear after "PM".
        self.assertIn(f"PM {cap.vwap_b:.2f}", html)
        self.assertIn(f"Kalshi {cap.vwap_a:.2f}", html)
        self.assertNotEqual(round(cap.vwap_a, 2), round(cap.vwap_b, 2))  # distinct, so the test is meaningful


if __name__ == "__main__":
    unittest.main()

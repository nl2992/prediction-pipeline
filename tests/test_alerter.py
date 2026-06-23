"""Unit tests for the arb alerter: signal math, de-dup, and email body."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

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

    def test_ai_settlement_date_preferred_over_contractual(self) -> None:
        from datetime import datetime, timezone
        yr = datetime.now(timezone.utc).year + 1
        # Contractual close is Dec; AI says it really resolves in March (earlier).
        s = {"net_accurate": 0.06, "poly_close": f"{yr}-12-31", "kalshi_close": f"{yr}-12-31",
             "ai_poly_close": f"{yr}-03-01", "ai_kalshi_close": f"{yr}-03-01"}
        _ann, _days, settle = _settle_horizon(s)
        self.assertTrue(settle.endswith("-03-01"))  # used the AI's earlier date

    def test_ai_date_ignored_if_garbage(self) -> None:
        from datetime import datetime, timezone
        yr = datetime.now(timezone.utc).year + 1
        s = {"net_accurate": 0.06, "poly_close": f"{yr}-09-01", "kalshi_close": f"{yr}-09-01",
             "ai_poly_close": "not-a-date", "ai_kalshi_close": "1999-01-01"}  # unparseable / past
        _ann, _days, settle = _settle_horizon(s)
        self.assertTrue(settle.endswith("-09-01"))  # fell back to contractual


class CapJsonl(unittest.TestCase):
    def _file(self, n):
        import tempfile, pathlib
        fd, p = tempfile.mkstemp(suffix=".jsonl"); import os; os.close(fd)
        p = pathlib.Path(p)
        p.write_text("".join(f'{{"i":{i}}}\n' for i in range(n)), encoding="utf-8")
        return p

    def test_noop_when_under_byte_threshold(self):
        from alerter import _cap_jsonl
        p = self._file(100)
        try:
            _cap_jsonl(p, max_bytes=10_000_000, keep_rows=10)   # big threshold -> no trim
            self.assertEqual(len(p.read_text().splitlines()), 100)
        finally:
            p.unlink()

    def test_trims_to_keep_rows_when_over_threshold(self):
        from alerter import _cap_jsonl
        p = self._file(5000)
        try:
            _cap_jsonl(p, max_bytes=1, keep_rows=50)            # tiny threshold -> trim
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 50)
            self.assertEqual(lines[-1], '{"i":4999}')           # keeps the most recent
            self.assertFalse(p.with_suffix(p.suffix + ".tmp").exists())  # atomic, no leftover
        finally:
            p.unlink()

    def test_missing_file_is_noop(self):
        from alerter import _cap_jsonl
        import pathlib
        _cap_jsonl(pathlib.Path("does-not-exist.jsonl"), max_bytes=1, keep_rows=10)  # no raise


class PruneState(unittest.TestCase):
    def test_drops_old_keeps_recent_and_specials(self):
        from alerter import _prune_state
        now = time.time()
        state = {
            "fresh|k|d": {"net": 0.05, "ts": now - 3600},          # 1h old -> keep
            "stale|k|d": {"net": 0.04, "ts": now - 8 * 24 * 3600},  # 8d old -> drop
            "_last_alert_ts": now - 30 * 24 * 3600,                  # special -> keep
        }
        out = _prune_state(state)
        self.assertIn("fresh|k|d", out)
        self.assertNotIn("stale|k|d", out)
        self.assertIn("_last_alert_ts", out)                        # non-dict preserved

    def test_just_updated_keys_survive(self):
        from alerter import _prune_state
        now = time.time()
        out = _prune_state({"k|x|d": {"net": 0.05, "ts": now}})
        self.assertIn("k|x|d", out)


class FreshnessLabels(unittest.TestCase):
    def test_classifies_new_improved_persistent(self):
        from alerter import _freshness_labels
        sigs = [
            {"key": "new1", "net_accurate": 0.05},
            {"key": "imp1", "net_accurate": 0.08},      # was 0.05 -> +3c improved
            {"key": "same1", "net_accurate": 0.0505},   # was 0.05 -> within step
        ]
        state = {"imp1": {"net": 0.05}, "same1": {"net": 0.05}}
        labels = _freshness_labels(sigs, state)
        self.assertEqual(labels.get("new1"), "new")
        self.assertEqual(labels.get("imp1"), "improved")
        self.assertNotIn("same1", labels)               # persistent -> no badge

    def test_badge_rendered_in_email(self):
        sigs = compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)
        key = sigs[0]["key"]
        _, html, _ = build_email(sigs, {key: "new"})
        self.assertIn(">NEW<", html)
        _, html2, _ = build_email(sigs, {})              # no label -> no badge
        self.assertNotIn(">NEW<", html2)


class SendEmail(unittest.TestCase):
    _cfg = {"recipients": ["a@b.com", "c@d.com"], "from_addr": "x@y.com",
            "smtp_host": "smtp.test", "smtp_port": 587, "smtp_user": "u", "smtp_pass": "p"}

    def test_handshake_and_mime_with_inline_image(self):
        from alerter import send_email
        with patch("smtplib.SMTP") as SMTP:
            srv = SMTP.return_value.__enter__.return_value
            send_email(self._cfg, "Subj", "<p>hi</p>", [("chartA", b"\x89PNG\r\n\x1a\nDATA")])
        srv.starttls.assert_called_once()
        srv.login.assert_called_once_with("u", "p")
        frm, rcpts, msg = srv.sendmail.call_args.args
        self.assertEqual(frm, "x@y.com")
        self.assertEqual(rcpts, ["a@b.com", "c@d.com"])
        self.assertIn("multipart/related", msg)
        self.assertIn("text/html", msg)
        self.assertIn("image/png", msg)
        self.assertIn("Content-ID: <chartA>", msg)

    def test_no_images_has_no_image_part(self):
        from alerter import send_email
        with patch("smtplib.SMTP") as SMTP:
            srv = SMTP.return_value.__enter__.return_value
            send_email(self._cfg, "Subj", "<p>hi</p>", [])
        msg = srv.sendmail.call_args.args[2]
        self.assertNotIn("image/png", msg)


class DegradationAlert(unittest.TestCase):
    _cfg = {"recipients": ["a@b.com"], "from_addr": "x@y.com", "smtp_host": "h",
            "smtp_port": 1, "smtp_user": "u", "smtp_pass": "p"}

    def test_sends_then_suppressed_within_cooldown(self):
        from alerter import _alert_operator
        st = {}
        with patch("alerter.send_email") as send:
            _alert_operator(self._cfg, "CYCLE ERROR: boom", state=st)
            _alert_operator(self._cfg, "CYCLE ERROR: boom again", state=st)  # within cooldown
        self.assertEqual(send.call_count, 1)               # second suppressed
        self.assertIn("_last_alert_ts", st)

    def test_resends_after_cooldown(self):
        from alerter import _alert_operator, _ALERT_COOLDOWN_S
        st = {"_last_alert_ts": time.time() - _ALERT_COOLDOWN_S - 1}
        with patch("alerter.send_email") as send:
            _alert_operator(self._cfg, "CYCLE ERROR: boom", state=st)
        self.assertEqual(send.call_count, 1)

    def test_noop_when_email_not_configured(self):
        from alerter import _alert_operator
        with patch("alerter.email_configured", return_value=False), \
             patch("alerter.send_email") as send:
            _alert_operator(self._cfg, "boom", state={})
        send.assert_not_called()

    def test_never_raises_on_send_failure(self):
        from alerter import _alert_operator
        with patch("alerter.send_email", side_effect=OSError("smtp down")):
            _alert_operator(self._cfg, "boom", state={})    # must not raise

    def test_subject_is_single_line(self):
        from alerter import _alert_operator
        with patch("alerter.send_email") as send:
            _alert_operator(self._cfg, "CYCLE ERROR: boom\nsecond line\r\nthird", state={})
        subject = send.call_args[0][1]                       # (cfg, subject, body)
        self.assertNotIn("\n", subject)
        self.assertNotIn("\r", subject)

    def test_reason_html_escaped_in_alert_body(self):
        from alerter import _alert_operator
        with patch("alerter.send_email") as send:
            _alert_operator(self._cfg, "CYCLE ERROR: bad <tag> & 'x'", state={})
        body = send.call_args[0][2]                          # (cfg, subject, body)
        self.assertIn("&lt;tag&gt;", body)
        self.assertNotIn("<tag>", body)


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

    def test_titles_html_escaped(self) -> None:
        p = pair(0.38, 0.40, 0.65, 0.68, poly_title="AT&T <b>hack</b>?")
        sigs = compute_signals([p], min_edge=0.005)
        _, html_out, _ = build_email(sigs)
        self.assertNotIn("<b>hack</b>", html_out)        # raw markup not injected
        self.assertIn("AT&amp;T", html_out)              # & escaped
        self.assertIn("&lt;b&gt;hack", html_out)         # < > escaped

    def test_missing_market_url_renders_no_dead_link(self) -> None:
        p = pair(0.38, 0.40, 0.65, 0.68)
        p["poly_slug"] = ""                       # -> _poly_url == "" -> no anchor
        sigs = compute_signals([p], min_edge=0.005)
        _, html, _ = build_email(sigs)
        self.assertNotIn('href=""', html)         # no dead anchor anywhere
        self.assertIn("link unavailable", html)   # graceful note instead

    def test_present_urls_render_anchors(self) -> None:
        sigs = compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)
        _, html, _ = build_email(sigs)
        self.assertIn('<a href="https://polymarket.com/event/', html)
        self.assertNotIn("link unavailable", html)

    def test_subject_tags_new_vs_resend(self) -> None:
        sigs = compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)
        key = sigs[0]["key"]
        subj_new, _, _ = build_email(sigs, {key: "new"})
        self.assertTrue(subj_new.endswith("· 1 new"))
        subj_resend, _, _ = build_email(sigs, {})
        self.assertTrue(subj_resend.endswith("· re-send"))

    def test_threshold_is_configurable_in_subject_and_intro(self) -> None:
        sigs = compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)
        subject, html, _ = build_email(sigs, min_net=0.05)
        self.assertIn(">5% net", subject)
        self.assertIn("above 5%", html)
        # default unchanged
        subject_d, html_d, _ = build_email(sigs)
        self.assertIn(">3% net", subject_d)

    def test_portfolio_totals_aggregates_and_renders(self) -> None:
        from alerter import _portfolio_totals
        p = pair(0.38, 0.40, 0.65, 0.68)
        p["poly_book"] = {"bids": [[0.30, 100]], "asks": [[0.40, 100]]}
        p["kalshi_book"] = {"bids": [[0.65, 200]], "asks": [[0.95, 100]]}
        sigs = compute_signals([p], min_edge=0.005)
        n, deploy, profit = _portfolio_totals(sigs)
        self.assertEqual(n, 1)
        self.assertGreater(deploy, 0)
        self.assertGreater(profit, 0)
        _, html, _ = build_email(sigs)
        self.assertIn("Portfolio:", html)

    def test_portfolio_summary_omitted_without_books(self) -> None:
        sigs = compute_signals([pair(0.38, 0.40, 0.65, 0.68)], min_edge=0.005)  # no books
        _, html, _ = build_email(sigs)
        self.assertNotIn("Portfolio:", html)

    def test_one_bad_book_does_not_crash_whole_email(self) -> None:
        # A signal whose book makes exec rendering raise must degrade to text-only
        # (no exec block) WITHOUT crashing build_email and losing every alert (#7).
        good = pair(0.38, 0.40, 0.65, 0.68)
        good["poly_book"] = {"bids": [[0.30, 100]], "asks": [[0.40, 100]]}
        good["kalshi_book"] = {"bids": [[0.65, 200]], "asks": [[0.95, 100]]}
        bad = pair(0.30, 0.45, 0.60, 0.58, poly_id="bad", kalshi_ticker="bad",
                   poly_title="BAD pair?", poly_slug="bad-market")
        bad["poly_book"] = {"bids": [None], "asks": [None]}  # raises inside _ob
        bad["kalshi_book"] = {"bids": [[0.60, 100]], "asks": [[0.95, 100]]}
        sigs = compute_signals([good, bad], min_edge=0.005)
        self.assertEqual(len(sigs), 2)
        subject, html, images = build_email(sigs)        # must not raise
        self.assertIn("[Pred-Arb]", subject)
        for s in sigs:                                    # both pairs still present
            self.assertIn(s["poly_title"], html)

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

    def _book_sig(self) -> dict:
        return {
            "key": "k|x|buy YES on Kalshi + buy NO on Polymarket",
            "direction": "buy YES on Kalshi + buy NO on Polymarket",
            "poly_book": {"bids": [[0.37, 5000]], "asks": [[0.40, 5000]]},
            "kalshi_book": {"bids": [[0.25, 100]], "asks": [[0.30, 5000]]},
        }

    def test_exec_block_shows_ai_verified_tick_when_confirmed(self) -> None:
        s = self._book_sig(); s["ai_same"] = True; s["ai_reason"] = "same race"
        html, _p, _c = _exec_block(s)
        self.assertIn("AI check", html)
        self.assertIn("verified identical", html)

    def test_exec_block_shows_ai_flag_when_not_same(self) -> None:
        s = self._book_sig(); s["ai_same"] = False; s["ai_reason"] = "different windows"
        html, _p, _c = _exec_block(s)
        self.assertIn("flagged: different windows", html)

    def test_exec_block_omits_ai_row_when_unchecked(self) -> None:
        html, _p, _c = _exec_block(self._book_sig())  # no ai_same -> no key, graceful
        self.assertNotIn("AI check", html)


if __name__ == "__main__":
    unittest.main()

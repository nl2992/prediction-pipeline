"""Tests for the read-only production health summary (health.py)."""
from __future__ import annotations

import unittest

import health


class SummarizeLog(unittest.TestCase):
    def test_extracts_lifecycle_markers(self):
        lines = [
            "[alerter] scan done in 800s — cap=1500, 700 pairs, 100 survivable arb(s)",
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 15 arbs >3% net — best 44% annualised (15 pairs)",
            "[alerter] AI verify: mode=enforce, key=present, 15 checked, 14 confirmed, 1 flagged",
            "[alerter] scan done in 810s — cap=1500, 700 pairs, 99 survivable arb(s)",
        ]
        s = health.summarize_log(lines)
        self.assertIn("[Pred-Arb] 15 arbs", s["last_email_subject"])
        self.assertEqual(s["scans_since_email"], 1)          # one scan after the email
        self.assertIn("mode=enforce", s["last_heartbeat"])
        self.assertIsNone(s["recent_cycle_error"])

    def test_silent_no_op_detected(self):
        # Email went out, but the only heartbeat is BEFORE it -> flag possible no-op.
        lines = [
            "[alerter] AI verify: mode=enforce, key=present, 15 checked, 15 confirmed, 0 flagged",
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 15 arbs >3% net (15 pairs)",
            "[alerter] scan done in 800s — 100 survivable arb(s)",
        ]
        s = health.summarize_log(lines)
        self.assertFalse(s["heartbeat_after_email"])
        self.assertIsNotNone(s["last_heartbeat"])

    def test_recent_cycle_error_flagged(self):
        lines = ["[alerter] CYCLE ERROR: too many values to unpack (expected 2)"]
        s = health.summarize_log(lines)
        self.assertIn("too many values", s["recent_cycle_error"])

    def test_old_cycle_error_not_recent(self):
        lines = ["[alerter] CYCLE ERROR: boom"] + ["filler"] * 250
        s = health.summarize_log(lines)
        self.assertIsNone(s["recent_cycle_error"])           # >200 lines back -> not recent

    def test_empty_log_safe(self):
        s = health.summarize_log([])
        self.assertIsNone(s["last_scan"])
        self.assertIsNone(s["scans_since_email"])


class FormatHealth(unittest.TestCase):
    def test_renders_ok_and_warn(self):
        s = health.summarize_log([
            "[alerter] scan done in 800s — 100 survivable arb(s)",
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 9 arbs (9 pairs)",
            "[alerter] AI verify: mode=enforce, key=present, 9 checked, 9 confirmed, 0 flagged",
        ])
        out = health.format_health(s, 194, "2026-06-22 03:11:58")
        self.assertIn("health", out)
        self.assertIn("verifier:", out)
        self.assertIn("ai_verify.jsonl: 194 rows", out)

    def test_warns_when_no_heartbeat(self):
        s = health.summarize_log(["[alerter] scan done in 800s — 100 survivable arb(s)"])
        out = health.format_health(s, 0, None)
        self.assertIn("no verifier heartbeat", out)


if __name__ == "__main__":
    unittest.main()

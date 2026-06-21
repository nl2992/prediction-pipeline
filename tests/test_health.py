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

    def test_healthy_cycle_is_not_flagged(self):
        # Heartbeat (key=present) precedes EMAILED in the same cycle — must NOT WARN
        # (the old "heartbeat after email" heuristic false-WARNed here, #10).
        lines = [
            "[alerter] AI verify: mode=enforce, key=present, 15 checked, 15 confirmed, 0 flagged",
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 15 arbs >3% net (15 pairs)",
            "[alerter] scan done in 800s — 100 survivable arb(s)",
        ]
        s = health.summarize_log(lines)
        self.assertFalse(s["key_absent"])
        self.assertFalse(s["emails_without_heartbeat"])
        self.assertNotIn("WARN", health.format_health(s, 1, "t").split("verifier")[1].split("\n")[0])

    def test_key_absent_heartbeat_warns(self):
        lines = [
            "[alerter] AI verify: mode=enforce, key=ABSENT — verification SKIPPED (12 pairs kept as-is)",
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 12 arbs (12 pairs)",
        ]
        s = health.summarize_log(lines)
        self.assertTrue(s["key_absent"])
        self.assertIn("key not resolving", health.format_health(s, 1, "t"))

    def test_emails_without_any_heartbeat_warns(self):
        lines = [
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 12 arbs (12 pairs)",
            "[alerter] scan done in 800s — 100 survivable arb(s)",
        ]
        s = health.summarize_log(lines)
        self.assertTrue(s["emails_without_heartbeat"])
        self.assertIn("no verifier heartbeat in window", health.format_health(s, 1, "t"))

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

    def test_idle_when_no_heartbeat_and_no_email(self):
        # No email-worthy cycle in the window -> verifier idle, NOT a warning.
        s = health.summarize_log(["[alerter] scan done in 800s — 100 survivable arb(s)"])
        out = health.format_health(s, 0, None)
        self.assertIn("verifier idle", out)


if __name__ == "__main__":
    unittest.main()

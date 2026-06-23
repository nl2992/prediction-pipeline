"""Tests for the read-only production health summary (health.py)."""
from __future__ import annotations

import unittest

import health


class Tail(unittest.TestCase):
    def _write(self, lines):
        import os, tempfile
        fd, p = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def test_small_file_returns_all_lines(self):
        import os
        p = self._write([f"line{i}" for i in range(10)])
        try:
            self.assertEqual(health._tail(p, 100), [f"line{i}" for i in range(10)])
            self.assertEqual(health._tail(p, 3), ["line7", "line8", "line9"])
        finally:
            os.unlink(p)

    def test_large_file_reads_only_tail_block(self):
        import os
        p = self._write([f"line{i}" for i in range(5000)])
        try:
            out = health._tail(p, 5, block=2000)  # tiny block forces partial-first-line path
            self.assertEqual(out, [f"line{i}" for i in range(4995, 5000)])
        finally:
            os.unlink(p)


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

    def test_overall_ok_on_healthy(self):
        s = health.summarize_log([
            "[alerter] scan done in 800s — 100 survivable arb(s)",
            "[alerter] AI verify: mode=enforce, key=present, 9 checked, 9 confirmed, 0 flagged",
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 9 arbs (9 pairs)",
        ])
        self.assertTrue(health.overall_ok(s))
        self.assertIn("STATUS: OK", health.format_health(s, 1, "t"))

    def test_overall_ok_on_normal_quiet(self):
        # A scan but no email/heartbeat in window (quiet realert period) is NOT degraded.
        s = health.summarize_log(["[alerter] scan done in 800s — 100 survivable arb(s)"])
        self.assertTrue(health.overall_ok(s))

    def test_degraded_on_key_absent(self):
        s = health.summarize_log([
            "[alerter] scan done in 800s — 100 survivable arb(s)",
            "[alerter] AI verify: mode=enforce, key=ABSENT — verification SKIPPED",
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 9 arbs (9 pairs)",
        ])
        self.assertFalse(health.overall_ok(s))
        self.assertIn("STATUS: DEGRADED", health.format_health(s, 1, "t"))

    def test_scan_pairs_collapse_flagged(self):
        normal = ["[alerter] scan done in 800s — cap=1500, 790 pairs, 200 survivable arb(s)"] * 4
        collapsed = ["[alerter] scan done in 800s — cap=1500, 30 pairs, 2 survivable arb(s)"]
        s = health.summarize_log(normal + collapsed)
        self.assertEqual(s["last_scan_pairs"], 30)
        self.assertTrue(s["scan_pairs_low"])
        self.assertFalse(health.overall_ok(s))
        self.assertIn("COLLAPSED", health.format_health(s, 1, "t"))

    def test_normal_scan_variation_ok(self):
        lines = [f"[alerter] scan done in 800s — cap=1500, {n} pairs, 50 survivable arb(s)"
                 for n in (790, 800, 770, 810)]
        s = health.summarize_log(lines)
        self.assertFalse(s["scan_pairs_low"])
        self.assertTrue(health.overall_ok(s))

    def test_scan_pairs_not_confused_with_v1_pairs_shadow_line(self):
        # 'v2 shadow: agrees on 771/795 v1 pairs, 24 ...' must NOT be parsed as a count.
        lines = [
            "[alerter] scan done in 800s — cap=1500, 790 pairs, 200 survivable arb(s)",
            "      v2 shadow: agrees on 771/795 v1 pairs, 24 disagreement(s):",
        ]
        s = health.summarize_log(lines)
        self.assertEqual(s["last_scan_pairs"], 790)   # only the scan-done line counted

    def test_too_few_scans_not_flagged(self):
        s = health.summarize_log(["[alerter] scan done in 800s — cap=1500, 5 pairs, 0 survivable arb(s)"])
        self.assertFalse(s["scan_pairs_low"])         # 1 sample -> no baseline, no alarm

    def test_degraded_when_verifier_api_failing(self):
        s = health.summarize_log([
            "[alerter] scan done in 800s — 100 survivable arb(s)",
            "[alerter] AI verify: mode=enforce, key=present, 14 checked, 0 confirmed, 0 flagged",
            "[alerter] EMAILED ['a@b.com']: [Pred-Arb] 14 arbs (14 pairs)",
        ])
        self.assertTrue(s["verifier_api_failing"])
        self.assertFalse(health.overall_ok(s))
        self.assertIn("all checks failed", health.format_health(s, 1, "t"))

    def test_normal_heartbeat_not_flagged_api_failing(self):
        s = health.summarize_log([
            "[alerter] AI verify: mode=enforce, key=present, 14 checked, 13 confirmed, 1 flagged",
        ])
        self.assertFalse(s["verifier_api_failing"])

    def test_degraded_on_recent_email_failure(self):
        s = health.summarize_log([
            "[alerter] scan done in 800s — 100 survivable arb(s)",
            "[alerter] AI verify: mode=enforce, key=present, 9 checked, 9 confirmed, 0 flagged",
            "[alerter] EMAIL FAILED: (535, b'auth failed') — signal logged to alert_signals.jsonl",
        ])
        self.assertIsNotNone(s["recent_email_failure"])
        self.assertFalse(health.overall_ok(s))
        out = health.format_health(s, 1, "t")
        self.assertIn("STATUS: DEGRADED", out)
        self.assertIn("email delivery: FAILED", out)

    def test_old_email_failure_not_flagged(self):
        lines = ["[alerter] EMAIL FAILED: boom"] + ["filler"] * 250
        s = health.summarize_log(lines)
        self.assertIsNone(s["recent_email_failure"])

    def test_email_failed_not_confused_with_emailed(self):
        # "EMAIL FAILED" must not be parsed as a successful "EMAILED".
        s = health.summarize_log(["[alerter] EMAIL FAILED: smtp down"])
        self.assertIsNone(s["last_email_subject"])
        self.assertIsNotNone(s["recent_email_failure"])

    def test_degraded_on_recent_cycle_error_and_no_scan(self):
        self.assertFalse(health.overall_ok(health.summarize_log(["[alerter] CYCLE ERROR: boom"])))
        self.assertFalse(health.overall_ok(health.summarize_log([])))  # no scan at all

    def test_idle_when_no_heartbeat_and_no_email(self):
        # No email-worthy cycle in the window -> verifier idle, NOT a warning.
        s = health.summarize_log(["[alerter] scan done in 800s — 100 survivable arb(s)"])
        out = health.format_health(s, 0, None)
        self.assertIn("verifier idle", out)


if __name__ == "__main__":
    unittest.main()

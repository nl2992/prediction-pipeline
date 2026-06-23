"""Tests for the unified operator dashboard (ops.py, #79)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import ops


class OpsDashboard(unittest.TestCase):
    def test_composes_all_three_sections_and_exit_ok(self):
        with patch("health.build_report", return_value=("STATUS: OK\nhealth body", True)), \
             patch("signal_report.load_signals", return_value=[]), \
             patch("ai_verify_report.load_verdicts", return_value=[]), \
             patch("builtins.print") as pr:
            rc = ops.main()
        out = "\n".join(str(c.args[0]) for c in pr.call_args_list if c.args)
        self.assertEqual(rc, 0)
        self.assertIn("STATUS: OK", out)                 # health section
        self.assertIn("Emitted-signal log digest", out)  # opportunities section
        self.assertIn("verdict-log digest", out)         # matcher-QA section

    def test_exit_1_when_degraded(self):
        with patch("health.build_report", return_value=("STATUS: DEGRADED", False)), \
             patch("signal_report.load_signals", return_value=[]), \
             patch("ai_verify_report.load_verdicts", return_value=[]), \
             patch("builtins.print"):
            self.assertEqual(ops.main(), 1)


if __name__ == "__main__":
    unittest.main()

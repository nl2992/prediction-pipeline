"""Tests for the emitted-signal digest (signal_report.py, #76)."""
from __future__ import annotations

import unittest

import signal_report as r


def _sig(key, net, ts, poly="P", kalshi="K"):
    return {"key": key, "net_accurate": net, "ts": ts, "poly_title": poly, "kalshi_title": kalshi}


class LoadSignals(unittest.TestCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(r.load_signals("does-not-exist.jsonl"), [])


class Summarize(unittest.TestCase):
    def test_groups_counts_and_max_net(self):
        sigs = [
            _sig("A", 0.05, "2026-06-01", "Race A", "RaceA k"),
            _sig("A", 0.08, "2026-06-02", "Race A", "RaceA k"),   # A: 2x, max 0.08, latest 0.08
            _sig("B", 0.12, "2026-06-01", "Race B", "RaceB k"),   # B: 1x, max 0.12
        ]
        s = r.summarize(sigs)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["unique_pairs"], 2)
        # most recurring: A (2x) first
        self.assertEqual(s["most_recurring"][0]["poly"], "Race A")
        self.assertEqual(s["most_recurring"][0]["count"], 2)
        self.assertAlmostEqual(s["most_recurring"][0]["max_net"], 0.08)
        self.assertAlmostEqual(s["most_recurring"][0]["latest_net"], 0.08)  # latest by ts
        # richest: B (0.12) first
        self.assertEqual(s["richest"][0]["poly"], "Race B")
        self.assertAlmostEqual(s["richest"][0]["max_net"], 0.12)

    def test_empty_is_safe(self):
        s = r.summarize([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["most_recurring"], [])
        self.assertEqual(s["richest"], [])

    def test_malformed_net_does_not_crash(self):
        s = r.summarize([{"key": "A", "net_accurate": None, "ts": "t", "poly_title": "x", "kalshi_title": "y"}])
        self.assertEqual(s["richest"][0]["max_net"], 0.0)


class FormatReport(unittest.TestCase):
    def test_renders(self):
        s = r.summarize([_sig("A", 0.05, "t", "Race A", "RaceA k")])
        out = r.format_report(s)
        self.assertIn("Emitted-signal log digest", out)
        self.assertIn("Most recurring", out)
        self.assertIn("Richest", out)
        self.assertIn("Race A", out)


if __name__ == "__main__":
    unittest.main()

"""Tests for the read-only verdict-log digest (ai_verify_report.py)."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest

import ai_verify_report as r


def _write(rows: list[dict]) -> pathlib.Path:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for x in rows:
            f.write(json.dumps(x) + "\n")
    return pathlib.Path(path)


class LoadVerdicts(unittest.TestCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(r.load_verdicts("does-not-exist.jsonl"), [])

    def test_skips_malformed_lines(self):
        p = _write([{"same": True}])
        p.write_text(p.read_text(encoding="utf-8") + "{not json}\n\n", encoding="utf-8")
        try:
            self.assertEqual(len(r.load_verdicts(p)), 1)
        finally:
            p.unlink()


class Summarize(unittest.TestCase):
    def test_counts_and_pct(self):
        s = r.summarize([{"same": True}, {"same": True}, {"same": False, "reason": "x"}])
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["n_confirmed"], 2)
        self.assertEqual(s["n_flagged"], 1)
        self.assertAlmostEqual(s["pct_confirmed"], 200 / 3, places=3)

    def test_recurring_flagged_pairs_ranked_with_latest_reason(self):
        rows = [
            {"ts": "2026-06-01T00:00:00", "poly": "A", "kalshi": "Ak", "same": False, "reason": "old"},
            {"ts": "2026-06-02T00:00:00", "poly": "A", "kalshi": "Ak", "same": False, "reason": "new"},
            {"ts": "2026-06-02T00:00:00", "poly": "B", "kalshi": "Bk", "same": False, "reason": "once"},
            {"ts": "2026-06-03T00:00:00", "poly": "A", "kalshi": "Ak", "same": True},
        ]
        s = r.summarize(rows)
        fp = s["flagged_pairs"]
        self.assertEqual(fp[0]["poly"], "A")          # flagged twice -> ranked first
        self.assertEqual(fp[0]["count"], 2)
        self.assertEqual(fp[0]["reason"], "new")       # latest reason wins
        self.assertEqual(fp[1]["count"], 1)

    def test_recent_flags_newest_first(self):
        rows = [
            {"ts": "2026-06-01T00:00:00", "poly": "A", "kalshi": "Ak", "same": False},
            {"ts": "2026-06-05T00:00:00", "poly": "B", "kalshi": "Bk", "same": False},
        ]
        s = r.summarize(rows)
        self.assertEqual(s["recent_flags"][0]["poly"], "B")

    def test_empty_is_safe(self):
        s = r.summarize([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["pct_confirmed"], 0.0)
        self.assertEqual(s["flagged_pairs"], [])


class FormatReport(unittest.TestCase):
    def test_renders_without_error(self):
        s = r.summarize([{"poly": "A", "kalshi": "Ak", "same": False, "reason": "diff windows", "ts": "2026-06-02T00:00:00"}])
        out = r.format_report(s)
        self.assertIn("verdict-log digest", out)
        self.assertIn("Recurring flagged pairs", out)
        self.assertIn("diff windows", out)


if __name__ == "__main__":
    unittest.main()

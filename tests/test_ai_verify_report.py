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

    def test_resolved_pair_excluded_currently_flagged_listed(self):
        # Race1 was flagged twice then re-confirmed same (resolved, e.g. by v3) ->
        # must NOT appear; Race2's latest verdict is still flagged -> must appear.
        rows = [
            {"ts": "2026-06-01T00:00:00", "poly": "Race1", "kalshi": "R1k", "same": False, "reason": "old"},
            {"ts": "2026-06-02T00:00:00", "poly": "Race1", "kalshi": "R1k", "same": False, "reason": "new"},
            {"ts": "2026-06-02T00:00:00", "poly": "Race2", "kalshi": "R2k", "same": False, "reason": "once"},
            {"ts": "2026-06-03T00:00:00", "poly": "Race1", "kalshi": "R1k", "same": True},
        ]
        s = r.summarize(rows)
        fp = s["flagged_pairs"]
        self.assertEqual([e["poly"] for e in fp], ["Race2"])   # Race1 resolved -> dropped
        self.assertEqual(fp[0]["reason"], "once")

    def test_test_sentinels_excluded(self):
        rows = [
            {"ts": "2026-06-02T00:00:00", "poly": "PHANTOM", "kalshi": "PHANTOM K", "same": False, "reason": "r"},
            {"ts": "2026-06-02T00:00:00", "poly": "Real Race", "kalshi": "RRk", "same": False, "reason": "real"},
        ]
        s = r.summarize(rows)
        self.assertEqual([e["poly"] for e in s["flagged_pairs"]], ["Real Race"])
        self.assertEqual([v["poly"] for v in s["recent_flags"]], ["Real Race"])

    def test_recent_flags_newest_first(self):
        rows = [
            {"ts": "2026-06-01T00:00:00", "poly": "Race1", "kalshi": "R1k", "same": False},
            {"ts": "2026-06-05T00:00:00", "poly": "Race2", "kalshi": "R2k", "same": False},
        ]
        s = r.summarize(rows)
        self.assertEqual(s["recent_flags"][0]["poly"], "Race2")

    def test_empty_is_safe(self):
        s = r.summarize([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["pct_confirmed"], 0.0)
        self.assertEqual(s["flagged_pairs"], [])


class FormatReport(unittest.TestCase):
    def test_renders_without_error(self):
        s = r.summarize([{"poly": "Real Race", "kalshi": "RRk", "same": False, "reason": "diff windows", "ts": "2026-06-02T00:00:00"}])
        out = r.format_report(s)
        self.assertIn("verdict-log digest", out)
        self.assertIn("Currently flagged pairs", out)
        self.assertIn("diff windows", out)


if __name__ == "__main__":
    unittest.main()

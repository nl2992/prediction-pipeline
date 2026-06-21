"""Tests for the read-only verdict-log digest (ai_verify_report.py)."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import tempfile
import unittest

import ai_verify_report as r

_NOW = dt.datetime(2026, 6, 22, 12, 0, 0, tzinfo=dt.timezone.utc)


def _ago(hours: float) -> str:
    return (_NOW - dt.timedelta(hours=hours)).isoformat()


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
            {"ts": _ago(5), "poly": "Race1", "kalshi": "R1k", "same": False, "reason": "old"},
            {"ts": _ago(4), "poly": "Race1", "kalshi": "R1k", "same": False, "reason": "new"},
            {"ts": _ago(4), "poly": "Race2", "kalshi": "R2k", "same": False, "reason": "once"},
            {"ts": _ago(3), "poly": "Race1", "kalshi": "R1k", "same": True},
        ]
        s = r.summarize(rows, now=_NOW)
        fp = s["flagged_pairs"]
        self.assertEqual([e["poly"] for e in fp], ["Race2"])   # Race1 resolved -> dropped
        self.assertEqual(fp[0]["reason"], "once")

    def test_stale_flag_dropped_recent_kept(self):
        rows = [
            {"ts": _ago(2), "poly": "Fresh", "kalshi": "Fk", "same": False,
             "same_event": False, "reason": "active"},
            {"ts": _ago(48), "poly": "Ghost", "kalshi": "Gk", "same": False,
             "same_event": False, "reason": "fixed upstream, gone quiet"},
        ]
        s = r.summarize(rows, now=_NOW, stale_hours=12)
        polys = [e["poly"] for e in s["flagged_pairs"]]
        self.assertIn("Fresh", polys)            # 2h ago -> active
        self.assertNotIn("Ghost", polys)         # 48h ago -> stale ghost, dropped
        fresh = s["flagged_pairs"][0]
        self.assertAlmostEqual(fresh["hours_ago"], 2, delta=0.1)

    def test_unparseable_ts_kept(self):
        rows = [{"ts": "", "poly": "NoTs", "kalshi": "Nk", "same": False,
                 "same_event": False, "reason": "r"}]
        s = r.summarize(rows, now=_NOW, stale_hours=12)
        self.assertEqual([e["poly"] for e in s["flagged_pairs"]], ["NoTs"])

    def test_partition_by_failure_mode(self):
        rows = [
            {"ts": _ago(2), "poly": "BRICS q", "kalshi": "OPEC q",
             "same": False, "same_event": False, "settlement_same": True, "reason": "diff orgs"},
            {"ts": _ago(2), "poly": "Bill X", "kalshi": "Bill X k",
             "same": False, "same_event": True, "settlement_same": False, "reason": "diff dates"},
        ]
        s = r.summarize(rows, now=_NOW)
        self.assertEqual([e["poly"] for e in s["matcher_false_positives"]], ["BRICS q"])
        self.assertEqual([e["poly"] for e in s["settlement_mismatches"]], ["Bill X"])
        out = r.format_report(s)
        self.assertIn("MATCHER false-positives", out)
        self.assertIn("correct enforce drops", out)

    def test_test_sentinels_excluded(self):
        rows = [
            {"ts": _ago(2), "poly": "PHANTOM", "kalshi": "PHANTOM K", "same": False, "same_event": False, "reason": "r"},
            {"ts": _ago(2), "poly": "Real Race", "kalshi": "RRk", "same": False, "same_event": False, "reason": "real"},
        ]
        s = r.summarize(rows, now=_NOW)
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
        s = r.summarize([{"poly": "Real Race", "kalshi": "RRk", "same": False,
                          "same_event": False, "reason": "diff windows", "ts": _ago(2)}], now=_NOW)
        out = r.format_report(s)
        self.assertIn("verdict-log digest", out)
        self.assertIn("MATCHER false-positives", out)
        self.assertIn("diff windows", out)


if __name__ == "__main__":
    unittest.main()

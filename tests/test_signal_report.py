"""Tests for the emitted-signal digest (signal_report.py, #76)."""
from __future__ import annotations

import datetime as dt
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


class Annualised(unittest.TestCase):
    import datetime as _dt
    _NOW = _dt.datetime(2026, 6, 24, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def _close(self, days):
        return (self._NOW + self._dt.timedelta(days=days)).date().isoformat()

    def _recent_ts(self, hours):
        return (self._NOW - self._dt.timedelta(hours=hours)).isoformat()

    def test_near_dated_smaller_edge_outranks_far_dated_bigger(self):
        sigs = [
            {"key": "near", "net_accurate": 0.04, "ts": self._recent_ts(1),
             "poly_title": "Near", "kalshi_title": "n",
             "poly_close": self._close(30), "kalshi_close": self._close(30)},      # ~30d -> high ann
            {"key": "far", "net_accurate": 0.10, "ts": self._recent_ts(1),
             "poly_title": "Far", "kalshi_title": "f",
             "poly_close": self._close(900), "kalshi_close": self._close(900)},    # ~900d -> low ann
        ]
        s = r.summarize(sigs, now=self._NOW)
        self.assertEqual(s["best_annualised"][0]["poly"], "Near")   # annualised wins
        self.assertEqual(s["richest"][0]["poly"], "Far")            # raw edge differs

    def test_stale_pair_excluded_from_actionable_annualised(self):
        sigs = [
            {"key": "stale", "net_accurate": 0.04, "ts": self._recent_ts(48),     # 48h ago -> stale
             "poly_title": "Stale", "kalshi_title": "s",
             "poly_close": self._close(20), "kalshi_close": self._close(20)},
            {"key": "fresh", "net_accurate": 0.04, "ts": self._recent_ts(2),      # 2h ago -> current
             "poly_title": "Fresh", "kalshi_title": "f",
             "poly_close": self._close(40), "kalshi_close": self._close(40)},
        ]
        s = r.summarize(sigs, now=self._NOW, recent_hours=24)
        polys = [e["poly"] for e in s["best_annualised"]]
        self.assertIn("Fresh", polys)
        self.assertNotIn("Stale", polys)        # excluded despite a valid annualised


class FormatReport(unittest.TestCase):
    def test_renders(self):
        s = r.summarize([_sig("A", 0.05, "t", "Race A", "RaceA k")])
        out = r.format_report(s)
        self.assertIn("Emitted-signal log digest", out)
        self.assertIn("Most recurring", out)
        self.assertIn("Richest", out)
        self.assertIn("Race A", out)


class DigestHelpers(unittest.TestCase):
    """Recency + ranking helpers behind the digest (#117)."""

    _NOW = dt.datetime(2026, 6, 24, 12, 0, 0, tzinfo=dt.timezone.utc)

    def test_age_hours_z_and_naive(self):
        self.assertAlmostEqual(r._age_hours("2026-06-23T12:00:00Z", self._NOW), 24.0)
        self.assertAlmostEqual(r._age_hours("2026-06-23T12:00:00", self._NOW), 24.0)

    def test_age_hours_missing_and_bad(self):
        self.assertIsNone(r._age_hours("", self._NOW))
        self.assertIsNone(r._age_hours("xyz", self._NOW))

    def test_net_value_missing_bad(self):
        self.assertAlmostEqual(r._net({"net_accurate": 0.07}), 0.07)
        self.assertEqual(r._net({}), 0.0)
        self.assertEqual(r._net({"net_accurate": "x"}), 0.0)

    def test_annualised_one_year_horizon(self):
        # net 0.10 with closes ~1 year out -> ~0.10 annualised.
        a = r._annualised({"net_accurate": 0.10, "kalshi_close": "2027-06-24", "poly_close": "2027-06-24"})
        self.assertAlmostEqual(a, 0.10, places=2)

    def test_annualised_unknown_close_falls_back_to_net(self):
        self.assertAlmostEqual(r._annualised({"net_accurate": 0.10}), 0.10, places=3)


if __name__ == "__main__":
    unittest.main()

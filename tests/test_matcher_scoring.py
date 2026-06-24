"""Tests for matcher match-quality scoring (#95)."""
from __future__ import annotations

import unittest

from matcher import _confidence, _close_delta_hours


class Confidence(unittest.TestCase):
    """0.70*title + 0.30*time_score, time_score = max(0, 1 - delta/max_delta)."""

    def test_unknown_delta_uses_title_only(self):
        self.assertEqual(_confidence(0.8, None, 100), 0.8)

    def test_nonpositive_max_delta_uses_title_only(self):
        self.assertEqual(_confidence(0.8, 10, 0), 0.8)

    def test_perfect_proximity(self):
        # delta 0 -> time_score 1 -> 0.7*1 + 0.3*1
        self.assertAlmostEqual(_confidence(1.0, 0, 100), 1.0)

    def test_partial_proximity_blend(self):
        # delta 50 of 100 -> time_score 0.5 -> 0.7 + 0.15
        self.assertAlmostEqual(_confidence(1.0, 50, 100), 0.85)

    def test_delta_beyond_max_clamps_time_score_to_zero(self):
        # delta 200 > max 100 -> time_score max(0, -1) = 0 -> 0.7*sim
        self.assertAlmostEqual(_confidence(1.0, 200, 100), 0.70)


class CloseDeltaHours(unittest.TestCase):
    def test_absolute_hour_delta(self):
        self.assertAlmostEqual(
            _close_delta_hours("2027-01-01T00:00:00Z", "2027-01-02T00:00:00Z"), 24.0
        )

    def test_order_independent(self):
        self.assertAlmostEqual(
            _close_delta_hours("2027-01-02T00:00:00Z", "2027-01-01T00:00:00Z"), 24.0
        )

    def test_none_when_either_missing_or_unparseable(self):
        self.assertIsNone(_close_delta_hours(None, "2027-01-01"))
        self.assertIsNone(_close_delta_hours("garbage", "2027-01-01"))


if __name__ == "__main__":
    unittest.main()

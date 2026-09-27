"""Tests for rule_diff.compare_rules (Phase 2d): pure text-comparison, no I/O.

Parametrized over real rule-text fixtures captured from the live Kalshi/
Polymarket APIs (tests/fixtures/rule_texts/*.json) plus small synthetic
positive/negative cases per kind.
"""
from __future__ import annotations

import glob
import json
import os
import unittest

from rule_diff import compare_rules

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "rule_texts")


class FixturePairs(unittest.TestCase):
    """Each fixture is a real pair captured once from the live APIs during
    development. See the module docstring in rule_diff.py for why these
    matter: title-level guards can't see a settlement-rule difference."""

    def test_fixtures_directory_is_not_empty(self):
        paths = glob.glob(os.path.join(FIXTURES_DIR, "*.json"))
        self.assertTrue(paths, "expected at least one captured rule-text fixture")

    def _load(self, name: str) -> dict:
        with open(os.path.join(FIXTURES_DIR, f"{name}.json"), encoding="utf-8") as f:
            return json.load(f)

    def _kinds(self, name: str) -> tuple[list[str], dict]:
        data = self._load(name)
        flags = compare_rules(data["kalshi"]["rules_text"], data["polymarket"]["rules_text"])
        return [f["kind"] for f in flags], data

    def test_mlb_flags_tie_break(self):
        kinds, data = self._kinds("mlb")
        self.assertIn("tie_break", kinds)
        self.assertEqual(data["expected_kind"], "tie_break")

    def test_quebec_flags_ranking_basis(self):
        kinds, data = self._kinds("quebec")
        self.assertIn("ranking_basis", kinds)
        self.assertEqual(data["expected_kind"], "ranking_basis")

    def test_trump_xi_putin_flags_event_definition(self):
        kinds, data = self._kinds("trump_xi_putin")
        self.assertIn("event_definition", kinds)
        self.assertEqual(data["expected_kind"], "event_definition")

    def test_cpi_core_flags_bucket_vs_threshold(self):
        kinds, data = self._kinds("cpi_core")
        self.assertIn("bucket_vs_threshold", kinds)
        self.assertEqual(data["expected_kind"], "bucket_vs_threshold")

    def test_measles_negative_has_no_high_severity_flags(self):
        """Both sides are CDC US counts; 'above 6000' vs 'at least 6000' is a
        harmless boundary phrasing difference here, not a real conflict."""
        data = self._load("measles")
        flags = compare_rules(data["kalshi"]["rules_text"], data["polymarket"]["rules_text"])
        high = [f for f in flags if f["severity"] == "high"]
        self.assertEqual(high, [])

    def test_flag_shape(self):
        data = self._load("mlb")
        flags = compare_rules(data["kalshi"]["rules_text"], data["polymarket"]["rules_text"])
        self.assertTrue(flags)
        for flag in flags:
            self.assertEqual(
                set(flag.keys()),
                {"kind", "severity", "detail", "kalshi_excerpt", "poly_excerpt"},
            )
            self.assertIn(flag["severity"], ("high", "medium", "low"))
            self.assertLessEqual(len(flag["kalshi_excerpt"]), 200)
            self.assertLessEqual(len(flag["poly_excerpt"]), 200)
            self.assertTrue(flag["detail"])


class MissingText(unittest.TestCase):
    def test_no_flags_when_kalshi_text_missing(self):
        self.assertEqual(compare_rules(None, "some poly text about ties"), [])

    def test_no_flags_when_poly_text_missing(self):
        self.assertEqual(compare_rules("some kalshi text about ties", None), [])

    def test_no_flags_when_both_empty_strings(self):
        self.assertEqual(compare_rules("", ""), [])


class TieBreakSynthetic(unittest.TestCase):
    def test_both_proportional_no_flag(self):
        a = "In case of a tie, this market resolves proportionally among tied entrants."
        b = "Ties resolve to a proportional payout across all tied outcomes."
        kinds = [f["kind"] for f in compare_rules(a, b)]
        self.assertNotIn("tie_break", kinds)

    def test_both_tiebreaker_no_flag(self):
        a = "In the event of a tie, this market resolves to the tied entrant ranked first alphabetically."
        b = "Ties are broken using a tiebreaker: the lower earned run average wins."
        kinds = [f["kind"] for f in compare_rules(a, b)]
        self.assertNotIn("tie_break", kinds)

    def test_tiebreaker_vs_proportional_flags(self):
        a = "Ties are broken alphabetically among tied entrants."
        b = "In case of exact ties, tied participants receive a proportional payout."
        flags = compare_rules(a, b)
        kinds = [f["kind"] for f in flags]
        self.assertIn("tie_break", kinds)
        flag = next(f for f in flags if f["kind"] == "tie_break")
        self.assertEqual(flag["severity"], "medium")

    def test_ambiguous_side_does_not_flag(self):
        # This side mentions both policies -- ambiguous, so no flag.
        a = "Ties are broken alphabetically, unless a proportional payout applies instead."
        b = "In case of exact ties, tied participants receive a proportional payout."
        kinds = [f["kind"] for f in compare_rules(a, b)]
        self.assertNotIn("tie_break", kinds)

    def test_missing_policy_on_one_side_does_not_flag(self):
        a = "This market resolves based on the official government report."
        b = "In case of exact ties, tied participants receive a proportional payout."
        kinds = [f["kind"] for f in compare_rules(a, b)]
        self.assertNotIn("tie_break", kinds)


class RankingBasisSynthetic(unittest.TestCase):
    def test_seats_vs_votes_flags_high(self):
        a = "The winner is ranked by the number of seats won in the legislature."
        b = "Ranking is based on province-wide valid-vote count, not seats won."
        flags = compare_rules(a, b)
        kinds = [f["kind"] for f in flags]
        self.assertIn("ranking_basis", kinds)
        flag = next(f for f in flags if f["kind"] == "ranking_basis")
        self.assertEqual(flag["severity"], "high")

    def test_both_seats_no_flag(self):
        a = "The winner is ranked by the number of seats won."
        b = "This resolves based on seat count in the assembly."
        kinds = [f["kind"] for f in compare_rules(a, b)]
        self.assertNotIn("ranking_basis", kinds)


class EventDefinitionSynthetic(unittest.TestCase):
    def test_photo_vs_meeting_flags_high(self):
        a = "This resolves Yes if the two leaders are seen together in the same frame."
        b = "This resolves Yes only if the two leaders meet in person at a trilateral meeting."
        flags = compare_rules(a, b)
        kinds = [f["kind"] for f in flags]
        self.assertIn("event_definition", kinds)
        flag = next(f for f in flags if f["kind"] == "event_definition")
        self.assertEqual(flag["severity"], "high")

    def test_both_photo_no_flag(self):
        a = "Resolves Yes if photographed or videotaped together."
        b = "Resolves Yes if the leaders are seen together in public."
        kinds = [f["kind"] for f in compare_rules(a, b)]
        self.assertNotIn("event_definition", kinds)


class BucketVsThresholdSynthetic(unittest.TestCase):
    def test_exact_vs_open_flags_high(self):
        a = "This market resolves Yes if the reading will be exactly 0.2%."
        b = "This market resolves Yes if the reading comes in above 0.2%."
        flags = compare_rules(a, b)
        kinds = [f["kind"] for f in flags]
        self.assertIn("bucket_vs_threshold", kinds)
        flag = next(f for f in flags if f["kind"] == "bucket_vs_threshold")
        self.assertEqual(flag["severity"], "high")

    def test_both_open_threshold_no_flag(self):
        """Mirrors the measles fixture: 'above' vs 'at least' are both open
        thresholds -- a harmless phrasing difference, not a real conflict."""
        a = "Resolves Yes if the count is above 6000."
        b = "Resolves Yes if the count is at least 6000."
        kinds = [f["kind"] for f in compare_rules(a, b)]
        self.assertNotIn("bucket_vs_threshold", kinds)


if __name__ == "__main__":
    unittest.main()

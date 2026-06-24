"""Tests for discover.py pure helpers (#89)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from discover import _parse_dt, _is_parlay, _is_parlay_market, _category, _derive_keywords


class ParseDt(unittest.TestCase):
    """_parse_dt must always yield a tz-aware UTC datetime or None — the result
    feeds days-to-close in the annualised-return ranking, so a tz slip would
    silently distort edge."""

    def test_z_suffix_is_utc(self):
        self.assertEqual(
            _parse_dt("2027-01-15T12:30:00Z"),
            datetime(2027, 1, 15, 12, 30, tzinfo=timezone.utc),
        )

    def test_explicit_utc_offset(self):
        self.assertEqual(
            _parse_dt("2027-01-15T12:30:00+00:00"),
            datetime(2027, 1, 15, 12, 30, tzinfo=timezone.utc),
        )

    def test_non_utc_offset_converted_to_utc(self):
        # 12:30 at +05:00 is 07:30 UTC — conversion, not truncation.
        self.assertEqual(
            _parse_dt("2027-01-15T12:30:00+05:00"),
            datetime(2027, 1, 15, 7, 30, tzinfo=timezone.utc),
        )

    def test_naive_datetime_assumed_utc(self):
        r = _parse_dt("2027-01-15T12:30:00")
        self.assertEqual(r, datetime(2027, 1, 15, 12, 30, tzinfo=timezone.utc))
        self.assertEqual(r.tzinfo, timezone.utc)

    def test_date_only_is_midnight_utc(self):
        self.assertEqual(
            _parse_dt("2027-01-15"),
            datetime(2027, 1, 15, 0, 0, tzinfo=timezone.utc),
        )

    def test_none_empty_and_malformed_return_none(self):
        for bad in (None, "", "garbage", "2027-13-99"):
            self.assertIsNone(_parse_dt(bad), bad)

    def test_result_is_always_tz_aware(self):
        for s in ("2027-01-15T12:30:00Z", "2027-01-15T12:30:00", "2027-01-15"):
            self.assertIsNotNone(_parse_dt(s).tzinfo, s)


class IsParlayEvent(unittest.TestCase):
    def test_kxmve_event_prefix(self):
        self.assertTrue(_is_parlay({"event_ticker": "KXMVE-1", "title": "x"}))

    def test_stats_only_title(self):
        self.assertTrue(_is_parlay({"event_ticker": "AAA", "title": "What will voter turnout be?"}))

    def test_yes_no_combo_title(self):
        self.assertTrue(_is_parlay({"event_ticker": "AAA", "title": "yes Chiefs,yes Lakers"}))

    def test_normal_single_event_is_not_parlay(self):
        self.assertFalse(_is_parlay({"event_ticker": "AAA", "title": "Will the Fed cut rates?"}))


class IsParlayMarket(unittest.TestCase):
    def test_three_or_more_win_legs(self):
        self.assertTrue(_is_parlay_market(
            {"title": "Will the Chiefs win the AFC, win the Super Bowl, and win game 1?"}))

    def test_and_will_conjunctive_parlay(self):
        self.assertTrue(_is_parlay_market(
            {"title": "Will ACA credits not be extended and will the GOP win the House?"}))

    def test_yes_no_combo_title(self):
        self.assertTrue(_is_parlay_market({"title": "yes A,no B"}))

    def test_single_leg_market_is_not_parlay(self):
        self.assertFalse(_is_parlay_market({"title": "Will Trump win the 2028 election?"}))


class Category(unittest.TestCase):
    def test_election(self):
        self.assertEqual(_category({"title": "Will a Republican win the Senate race in Ohio?"}), "election")

    def test_election_priority_over_sports(self):
        # "race" / team-name substrings must not pull a Senate event into sports.
        self.assertEqual(_category({"title": "Who wins the House race in district 3?"}), "election")

    def test_economic(self):
        self.assertEqual(_category({"title": "Will the Fed cut interest rates in March?"}), "economic")

    def test_political(self):
        self.assertEqual(_category({"title": "Will SCOTUS overturn the ruling?"}), "political")

    def test_sports(self):
        self.assertEqual(_category({"title": "Will the Lakers win game 4?"}), "sports")

    def test_pop_fallback(self):
        self.assertEqual(_category({"title": "Will Taylor Swift release an album?"}), "pop")


class DeriveKeywords(unittest.TestCase):
    """Keyword extraction drives the cross-venue search query, so the
    highest-priority proper-noun branch must not capture the leading question
    word as part of a name pair (#91)."""

    def test_will_prefix_not_captured_in_name_pair(self):
        self.assertEqual(_derive_keywords("Will Donald Trump win the 2028 election?"), ["Donald Trump"])

    def test_who_will_prefix_stripped(self):
        self.assertEqual(_derive_keywords("Who will Joe Biden endorse?"), ["Joe Biden"])

    def test_league_game_branch_unchanged(self):
        self.assertEqual(_derive_keywords("Will the Lakers win NBA Finals game 4?"), ["NBA game 4"])

    def test_fallback_longest_tokens(self):
        # No proper pair / league / district -> longest non-stopword tokens.
        self.assertIn("consumption", _derive_keywords("What is the cheese consumption forecast?"))


if __name__ == "__main__":
    unittest.main()

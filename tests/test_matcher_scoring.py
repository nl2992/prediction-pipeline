"""Tests for matcher match-quality scoring (#95)."""
from __future__ import annotations

import unittest

from matcher import (
    _confidence, _close_delta_hours, _settlement_type, _monetary_direction,
    is_inverted_pair, _known_orgs, _known_products, _matchup_signature,
    _names_overlap, _name_anchor_tokens,
    _is_ou_or_spread, _has_over_under, _stat_thresholds,
    _is_win_market, _is_player_prop,
    _offices, _parties, _jurisdictions, _years,
    _sports_league, _legislative_scope,
    _time_scopes, _comparison_bounds, _month_names,
    _arb_signature, is_arb_eligible,
)
from pipeline import MarketSnapshot, OrderBook


def _snap(title: str, full: str = "") -> MarketSnapshot:
    return MarketSnapshot(
        source="x", market_id="m", event_id="e", title=title, status="open",
        close_time=None, fetched_at="t", orderbook=OrderBook(bids=[], asks=[]),
        extra={"full_question": full},
    )


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


class SettlementType(unittest.TestCase):
    """Path-dependence classifier; the point-vs-touch split is the PAIR-015
    trap (different contracts at the same strike) (#96)."""

    def test_hold(self):
        self.assertEqual(_settlement_type("Will BTC stay above $80k for all of 2026?"), "hold")

    def test_touch_verb(self):
        self.assertEqual(_settlement_type("Will BTC hit $175k?"), "touch")

    def test_touch_at_any_point(self):
        self.assertEqual(_settlement_type("Will BTC dip below $80k at any point?"), "touch")

    def test_reach_scoped_to_whole_year_is_touch(self):
        self.assertEqual(_settlement_type("Will BTC be above $150k in 2026?"), "touch")

    def test_point_in_time_on_date_is_none(self):
        # PAIR-015: "close above ON Dec 31" is a point read, NOT a touch.
        self.assertIsNone(_settlement_type("Will BTC close above $150k on Dec 31?"))

    def test_no_signal_is_none(self):
        self.assertIsNone(_settlement_type("Who wins the election?"))


class MonetaryDirection(unittest.TestCase):
    def test_hike(self):
        self.assertEqual(_monetary_direction("Will the Fed hike rates?"), {"up"})

    def test_cut(self):
        self.assertEqual(_monetary_direction("Will the Fed cut rates?"), {"down"})

    def test_both(self):
        self.assertEqual(_monetary_direction("Will the Fed raise then cut?"), {"up", "down"})

    def test_neither(self):
        self.assertEqual(_monetary_direction("Who wins?"), set())


class IsInvertedPair(unittest.TestCase):
    """Poly-YES == Kalshi-NO detection; a wrong verdict fabricates or misses
    an arb. Text-only, never price-based (#97)."""

    def test_threshold_flip_touch_vs_hold(self):
        p = _snap("Will BTC dip below $80k at any point?")
        k = _snap("Will BTC stay above $80k for all of 2026?")
        self.assertTrue(is_inverted_pair(p, k))

    def test_antonym_state_cue(self):
        self.assertTrue(is_inverted_pair(
            _snap("Will TikTok be banned?"), _snap("Will TikTok be operating legally?")))

    def test_same_direction_not_inverted(self):
        self.assertFalse(is_inverted_pair(
            _snap("Will BTC be above $80k?"), _snap("Will BTC be above $80k?")))

    def test_unrelated_not_inverted(self):
        self.assertFalse(is_inverted_pair(
            _snap("Who wins the election?"), _snap("Who wins the cup?")))

    def test_both_mixed_antonym_is_not_inversion(self):
        # Both sides mention banned AND legal -> not a clean one-neg/one-pos flip.
        mixed = "Will TikTok be banned or stay legal?"
        self.assertFalse(is_inverted_pair(_snap(mixed), _snap(mixed)))


class KnownOrgsProducts(unittest.TestCase):
    """Org/product mismatch gate: BRICS != OPEC (#13), Claude != GPT (#98)."""

    def test_orgs_distinct(self):
        self.assertEqual(_known_orgs("Will BRICS expand in 2026?"), {"brics"})
        self.assertEqual(_known_orgs("Will OPEC cut output?"), {"opec"})

    def test_orgs_empty_when_none_present(self):
        self.assertEqual(_known_orgs("Who wins the race?"), set())

    def test_products_distinct(self):
        self.assertEqual(_known_products("Will Claude 6 launch?"), {"claude"})
        self.assertEqual(_known_products("Will GPT-6 launch?"), {"gpt"})


class MatchupSignature(unittest.TestCase):
    def test_order_independent(self):
        # The whole point: "A vs B" and "B vs A" are the same game.
        self.assertEqual(
            _matchup_signature("Lakers vs Celtics winner"),
            _matchup_signature("Celtics vs. Lakers"),
        )

    def test_at_form_parsed(self):
        self.assertEqual(
            _matchup_signature("Yankees at Red Sox"),
            frozenset((frozenset({"yankees"}), frozenset({"red", "sox"}))),
        )

    def test_none_when_no_matchup(self):
        self.assertIsNone(_matchup_signature("Who wins the election?"))


class NamesOverlap(unittest.TestCase):
    """Entity matching across differing abbreviations; must over-match neither
    via qualifiers nor lose the ticker-clip rule (#100)."""

    def test_substring_containment(self):
        self.assertTrue(_names_overlap({"donald trump"}, {"trump"}))

    def test_ticker_prefix_clip(self):
        # "sol" clips to "solana", "etf" to "etfs".
        self.assertTrue(_names_overlap({"solana etf"}, {"sol etfs"}))

    def test_shared_real_anchor(self):
        self.assertTrue(_names_overlap({"james bond"}, {"new bond"}))

    def test_distinct_names_do_not_overlap(self):
        self.assertFalse(_names_overlap({"biden"}, {"trump"}))

    def test_qualifier_is_not_a_shared_anchor(self):
        # "New York" vs "New Jersey" must NOT match on "new".
        self.assertFalse(_names_overlap({"new york"}, {"new jersey"}))


class NameAnchorTokens(unittest.TestCase):
    def test_drops_qualifier_words(self):
        self.assertEqual(_name_anchor_tokens("new bond"), {"bond"})

    def test_all_qualifiers_yields_empty(self):
        self.assertEqual(_name_anchor_tokens("the will jan"), set())

    def test_drops_short_tokens(self):
        self.assertEqual(_name_anchor_tokens("xi"), set())


class OuSpreadAndStats(unittest.TestCase):
    """Bet-type gates: a totals/spread line must never match a moneyline (#101)."""

    def test_ou_line_is_ou_or_spread(self):
        self.assertTrue(_is_ou_or_spread("Sweden 1st Half O/U 0.5"))

    def test_over_n_is_ou_or_spread(self):
        self.assertTrue(_is_ou_or_spread("France over 1.5 goals"))

    def test_spread_paren_is_ou_or_spread(self):
        self.assertTrue(_is_ou_or_spread("Bosnia and Herzegovina (-1.5)"))

    def test_moneyline_is_not_ou_or_spread(self):
        self.assertFalse(_is_ou_or_spread("Will the Lakers win?"))

    def test_has_over_under(self):
        self.assertTrue(_has_over_under("Total over 1.5"))
        self.assertFalse(_has_over_under("Lakers win"))

    def test_stat_threshold_maps_keyword_to_numbers(self):
        self.assertEqual(_stat_thresholds("LeBron over 25.5 points"), {"points": {25.5}})

    def test_any_point_idiom_is_not_a_points_prop(self):
        # "at any point" is stripped, so a price-touch title yields no stat.
        self.assertEqual(_stat_thresholds("Will BTC dip below $80k at any point in 2026?"), {})


class WinMarketAndPlayerProp(unittest.TestCase):
    """Moneyline vs player-prop discrimination (#102)."""

    def test_win_and_beat_are_win_markets(self):
        self.assertTrue(_is_win_market("Will the Lakers win?"))
        self.assertTrue(_is_win_market("Will the Lakers beat the Celtics?"))

    def test_ou_line_is_not_a_win_market(self):
        self.assertFalse(_is_win_market("Lakers O/U 215.5"))

    def test_colon_stat_is_player_prop(self):
        self.assertTrue(_is_player_prop("Cody Gakpo: 2+ assists"))

    def test_goalscorer_is_player_prop(self):
        self.assertTrue(_is_player_prop("Mitch Marner: First Goalscorer"))

    def test_n_plus_stat_is_player_prop(self):
        self.assertTrue(_is_player_prop("Aaron Judge 3+ total bases"))

    def test_plain_win_is_not_player_prop(self):
        self.assertFalse(_is_player_prop("Will the Lakers win?"))

    def test_team_margin_is_not_player_prop(self):
        # "win by over 1.5 goals" is a margin/total, not a "N+ stat" prop.
        self.assertFalse(_is_player_prop("Lakers win by over 1.5 goals"))


class PoliticalGeoYearGates(unittest.TestCase):
    """Office/party/jurisdiction/year scoping with documented traps (#103)."""

    def test_president_office(self):
        self.assertIn("president", _offices("Will the presidential election be close?"))

    def test_vice_president_is_not_president(self):
        found = _offices("Who will be the vice presidential nominee?")
        self.assertIn("vice_president", found)
        self.assertNotIn("president", found)

    def test_party_aliases(self):
        self.assertEqual(_parties("Will the GOP win?"), {"republican"})
        self.assertEqual(_parties("Will Labour win the seat?"), {"democratic"})
        self.assertEqual(_parties("Will the Tories hold?"), {"conservative"})

    def test_indiana_does_not_register_india(self):
        found = _jurisdictions("Indiana Senate race")
        self.assertIn("indiana", found)
        self.assertNotIn("india", found)

    def test_india_registers_when_standalone(self):
        self.assertIn("india", _jurisdictions("India general election"))

    def test_years_four_digit_and_two_digit_normalization(self):
        self.assertEqual(_years("2026 election outcome"), {"2026"})
        self.assertEqual(_years("26 election"), {"2026"})


class SportsLeagueAndLegislativeScope(unittest.TestCase):
    """Disjointness gates against shared-token phantom matches (#104)."""

    def test_nba(self):
        self.assertEqual(_sports_league("NBA Finals: New York"), {"nba"})

    def test_wnba_is_not_nba(self):
        self.assertEqual(_sports_league("WNBA championship: New York"), {"wnba"})

    def test_mls_and_nhl_shared_city_are_disjoint(self):
        self.assertEqual(_sports_league("Los Angeles FC (MLS Cup)"), {"mls"})
        self.assertEqual(_sports_league("Los Angeles Kings (Stanley Cup)"), {"nhl"})

    def test_no_league(self):
        self.assertEqual(_sports_league("Will it rain tomorrow?"), set())

    def test_legislative_seat(self):
        self.assertEqual(_legislative_scope("Will the Republican Party win the IN-01 House seat?"), "seat")

    def test_legislative_chamber(self):
        self.assertEqual(_legislative_scope("Which party will win the U.S. House?"), "chamber")

    def test_legislative_none(self):
        self.assertIsNone(_legislative_scope("Who wins the presidency?"))


class TimeScopesAndBounds(unittest.TestCase):
    """Resolution-granularity and bound extraction (#105)."""

    def test_specific_day(self):
        self.assertEqual(_time_scopes("Will it resolve on Jan 15?"), {"day:jan-15"})

    def test_month_scope(self):
        self.assertEqual(_time_scopes("Will it happen in March?"), {"month:mar"})

    def test_year_phrases(self):
        self.assertEqual(_time_scopes("Will it happen this year?"), {"year"})
        self.assertEqual(_time_scopes("Will it happen in 2026?"), {"year"})

    def test_day_suppresses_year_scope(self):
        # A concrete date is more specific than the surrounding year.
        self.assertEqual(_time_scopes("Will BTC hit 150k on Dec 31 in 2026?"), {"day:dec-31"})

    def test_no_time_scope(self):
        self.assertEqual(_time_scopes("Who wins?"), set())

    def test_comparison_bounds_lt_only(self):
        self.assertEqual(_comparison_bounds("less than 5%"), {"lt": {5.0}, "gt": set()})

    def test_comparison_bounds_both(self):
        self.assertEqual(_comparison_bounds("more than 3 and less than 5"), {"lt": {5.0}, "gt": {3.0}})

    def test_month_names_multi(self):
        self.assertEqual(_month_names("between March and July"), {"mar", "jul"})


class ArbSignatureAndEligibility(unittest.TestCase):
    """The strict final gate before a pair becomes a trade signal (#106)."""

    def test_arb_signature_components(self):
        s = _arb_signature("LeBron James over 25.5 points")
        self.assertIn("stat:points", s)
        self.assertIn("rate:25.5", s)
        self.assertIn("gt:25.5", s)

    def test_arb_signature_time_and_rate(self):
        s = _arb_signature("Will the Fed set rates above 5% in 2026?")
        self.assertIn("time:year", s)
        self.assertIn("rate:5%", s)

    def test_eligible_for_identical_compatible_pair(self):
        p = _snap("Will Bitcoin reach above $150,000 in 2026?")
        k = _snap("Will Bitcoin be above $150k in 2026?")
        self.assertTrue(is_arb_eligible(p, k))

    def test_not_eligible_for_incompatible_pair(self):
        self.assertFalse(is_arb_eligible(
            _snap("Will the Lakers win?"), _snap("Will it rain in Texas?")))


if __name__ == "__main__":
    unittest.main()

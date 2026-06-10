from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from arb import find_arb
from discover import _match_outcomes_within_group, _parse_dt as discover_parse_dt
from discover import discover, _is_parlay_market
from executor import TradeIntent, check_price_still_valid
from kalshi.client import KalshiClient
from matcher import (
    MatchedPair,
    _FOREIGN_COUNTRIES,
    _confidence,
    _domains,
    _numeric_threshold,
    _offices,
    _proper_names,
    _threshold_equal,
    _parse_dt as matcher_parse_dt,
    is_arb_eligible,
    is_close_time_compatible,
    is_compatible_match,
    match_markets,
)
from monitor import _resolve_poly_token, _verify_kalshi_clob
from pipeline import (
    MarketSnapshot,
    OrderBook,
    PriceLevel,
    _parse_kalshi_full_book,
    kalshi_market_title,
)
from polymarket.client import PolymarketClient


def snap(source: str, market_id: str, bid: float, ask: float, extra: dict | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        source=source,
        market_id=market_id,
        event_id=f"{market_id}-event",
        title=f"{market_id} title",
        status="open",
        close_time="2026-06-01T00:00:00Z",
        fetched_at="2026-05-25T00:00:00Z",
        orderbook=OrderBook(
            bids=[PriceLevel(bid, 10.0)],
            asks=[PriceLevel(ask, 20.0)],
        ),
        extra=extra or {},
    )


def titled_snap(
    source: str,
    market_id: str,
    title: str,
    close_time: str,
    extra: dict | None = None,
) -> MarketSnapshot:
    s = snap(source, market_id, 0.4, 0.5, extra=extra)
    s.title = title
    s.close_time = close_time
    return s


class CoreLogicTests(unittest.TestCase):
    def test_kalshi_full_book_derives_yes_asks_from_no_bids(self) -> None:
        ob = _parse_kalshi_full_book(
            {
                "orderbook_fp": {
                    "yes_dollars": [["0.20", "5"], ["0.35", "7"]],
                    "no_dollars": [["0.40", "11"], ["0.55", "13"]],
                }
            }
        )

        self.assertEqual(ob.best_bid, 0.35)
        self.assertEqual(ob.best_ask, 0.45)
        self.assertEqual(ob.bids[0].size, 7.0)
        self.assertEqual(ob.asks[0].size, 13.0)

    def test_arb_detects_both_complement_directions(self) -> None:
        pair = MatchedPair(
            poly=snap("polymarket", "poly", bid=0.62, ask=0.64),
            kalshi=snap("kalshi", "kalshi", bid=0.80, ask=0.82),
            title_similarity=1.0,
            close_delta_hours=0,
            confidence=1.0,
        )

        opps = find_arb([pair], fee_poly=0.01, fee_kalshi=0.01, min_net_profit_pct=-100)
        directions = {o.direction: o for o in opps}

        self.assertAlmostEqual(directions["poly_yes__kalshi_no"].gross_cost, 0.84)
        self.assertAlmostEqual(directions["poly_yes__kalshi_no"].net_profit, 0.15)
        self.assertAlmostEqual(directions["kalshi_yes__poly_no"].gross_cost, 1.20)
        self.assertLess(directions["kalshi_yes__poly_no"].net_profit, 0)

    def test_resolve_poly_token_uses_outcome_labels_before_indexes(self) -> None:
        poly = snap(
            "polymarket",
            "poly",
            bid=0.4,
            ask=0.5,
            extra={"clob_token_ids": ["token-no", "token-yes"], "outcomes": ["No", "Yes"]},
        )

        self.assertEqual(_resolve_poly_token(poly, "YES"), "token-yes")
        self.assertEqual(_resolve_poly_token(poly, "NO"), "token-no")

    def test_datetime_parsers_accept_fractional_utc_offsets(self) -> None:
        self.assertEqual(
            matcher_parse_dt("2026-05-25T01:02:03.456Z").isoformat(),
            "2026-05-25T01:02:03.456000+00:00",
        )
        self.assertEqual(
            discover_parse_dt("2026-05-25T11:02:03+10:00").isoformat(),
            "2026-05-25T01:02:03+00:00",
        )

    def test_confidence_ignores_nonpositive_time_window(self) -> None:
        self.assertEqual(_confidence(0.42, delta_h=10, max_delta=0), 0.42)

    def test_matcher_rejects_same_template_different_elections(self) -> None:
        poly = snap(
            "polymarket",
            "poly-election",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Marine Le Pen win the 2027 French presidential election?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-election",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Who will win the next Turkish presidential election?"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))
        self.assertEqual(match_markets([poly], [kalshi], min_title_similarity=0.1), [])

    def test_matcher_rejects_office_and_state_mismatch(self) -> None:
        poly = snap(
            "polymarket",
            "poly-governor",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will the Republicans win the South Carolina governor race in 2026?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-senate",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Republicans win the Senate race in South Dakota?"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))
        self.assertEqual(match_markets([poly], [kalshi], min_title_similarity=0.1), [])

    def test_matcher_allows_same_party_state_office_template(self) -> None:
        poly = snap(
            "polymarket",
            "poly-senate",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will the Republicans win the North Carolina Senate race in 2026?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-senate",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Republicans win the Senate race in North Carolina?"},
        )

        self.assertTrue(is_compatible_match(poly, kalshi))
        self.assertEqual(len(match_markets([poly], [kalshi], min_title_similarity=0.1)), 1)

    def test_matcher_rejects_named_contract_against_generic_winner(self) -> None:
        poly = snap(
            "polymarket",
            "poly-french-candidate",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Jordan Bardella win the 2027 French presidential election?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-generic-winner",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Who will win the next presidential election?"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_matcher_rejects_different_contract_predicates(self) -> None:
        poly = snap(
            "polymarket",
            "poly-win",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Andy Beshear win the 2028 US Presidential Election?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-declare",
            bid=0.4,
            ask=0.5,
            extra={
                "full_question": "Will Andy Beshear be first this list to declare for 2028 United States presidential election before Nov 7, 2028?"
            },
        )

        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_matcher_rejects_different_political_events_same_subject(self) -> None:
        # Same subject and time scaffolding ("Will Trump ___ before his term
        # ends?") yields high token overlap, but impeachment and martial law
        # are different events and must not match.
        poly = snap(
            "polymarket",
            "poly-impeach",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Trump be impeached before his term ends?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-martial-law",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Trump impose martial law before his term ends?"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_matcher_allows_same_political_event_same_subject(self) -> None:
        poly = snap(
            "polymarket",
            "poly-impeach",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Trump be impeached before his term ends?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-impeach",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Donald Trump be impeached before the end of his term?"},
        )

        self.assertTrue(is_compatible_match(poly, kalshi))

    def test_kalshi_title_carries_candidate_subtitle(self) -> None:
        # Mutually-exclusive event: generic title, candidate in yes_sub_title.
        self.assertEqual(
            kalshi_market_title(
                {"title": "Who will win the next presidential election?",
                 "yes_sub_title": "J.D. Vance"}
            ),
            "Who will win the next presidential election? J.D. Vance",
        )
        # No duplication when the subtitle already appears in the title.
        self.assertEqual(
            kalshi_market_title({"title": "Will J.D. Vance win?", "yes_sub_title": "J.D. Vance"}),
            "Will J.D. Vance win?",
        )

    def test_generic_contest_phrase_is_not_a_proper_name(self) -> None:
        # "Presidential Election" is a contest, not a person. If it is treated
        # as a proper name, a Polymarket title like "Will JD Vance win the 2028
        # US Presidential Election?" (initials too short to extract) ends up
        # with a phantom name that cannot overlap the Kalshi candidate.
        self.assertNotIn(
            "presidential election",
            _proper_names("Will JD Vance win the 2028 US Presidential Election?"),
        )
        # A real two-token person name is still extracted.
        self.assertIn(
            "gavin newsom",
            _proper_names("Will Gavin Newsom win the 2028 US Presidential Election?"),
        )

    def test_named_poly_matches_kalshi_generic_title_with_subtitle(self) -> None:
        # Regression: before folding yes_sub_title into the title, every
        # candidate market looked generic and named Polymarket contracts could
        # never match (false negative). Same candidate must match; different
        # candidate must not.
        poly = snap(
            "polymarket",
            "poly-newsom",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Gavin Newsom win the 2028 US Presidential Election?"},
        )
        same = snap(
            "kalshi",
            "kalshi-newsom",
            bid=0.4,
            ask=0.5,
            extra={"full_question": kalshi_market_title(
                {"title": "Who will win the next presidential election?",
                 "yes_sub_title": "Gavin Newsom"})},
        )
        other = snap(
            "kalshi",
            "kalshi-shapiro",
            bid=0.4,
            ask=0.5,
            extra={"full_question": kalshi_market_title(
                {"title": "Who will win the next presidential election?",
                 "yes_sub_title": "Josh Shapiro"})},
        )
        self.assertTrue(is_compatible_match(poly, same))
        self.assertFalse(is_compatible_match(poly, other))

    def test_kalshi_city_only_team_matches_poly_city_plus_nickname(self) -> None:
        # Kalshi names a finalist by city only and avoids the league trademark
        # ("Pro Basketball" = NBA); Polymarket uses city + nickname + "NBA".
        poly = snap(
            "polymarket",
            "poly-knicks",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will the New York Knicks win the 2026 NBA Finals?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-ny",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will the New York win the 2026 Pro Basketball Finals?"},
        )
        self.assertTrue(is_compatible_match(poly, kalshi))

    def test_matcher_rejects_cross_league_same_city(self) -> None:
        # Same city, different league (WNBA vs NBA/"Pro Basketball") must not
        # match — guards the false positive introduced by city-only matching.
        poly = snap(
            "polymarket",
            "poly-liberty",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will New York Liberty win the 2026 WNBA Finals?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-ny",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will the New York win the 2026 Pro Basketball Finals?"},
        )
        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_impeach_president_vs_trump_same_deadline_matches(self) -> None:
        # Cross-exchange vocabulary + deadline phrasing differ, but close times
        # prove the same horizon: "by end of 2026" == "before Jan 1, 2027".
        poly = titled_snap(
            "polymarket", "poly-imp",
            "Will Trump be impeached by end of 2026?",
            "2026-12-31T00:00:00Z",
        )
        kalshi = titled_snap(
            "kalshi", "kalshi-imp",
            "Will the President be impeached before Jan 1, 2027?",
            "2027-01-01T15:00:00Z",
        )
        self.assertTrue(is_compatible_match(poly, kalshi))

    def test_close_time_guard_does_not_relax_far_apart_horizons(self) -> None:
        # The same-horizon relaxation must only apply when close times nearly
        # coincide. Far-apart deadlines with disjoint year tokens stay rejected
        # by the year veto (guards against over-relaxation / cycle collisions).
        poly = titled_snap(
            "polymarket", "poly-imp3",
            "Will the President be impeached before Jan 1, 2027?",
            "2027-01-01T00:00:00Z",
        )
        kalshi = titled_snap(
            "kalshi", "kalshi-imp3",
            "Will the President be impeached before Jan 1, 2029?",
            "2029-01-01T15:00:00Z",
        )
        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_bare_president_office_is_not_an_election_domain(self) -> None:
        # "Will the President be impeached?" is about the office holder, not an
        # election; it must not be vetoed against a non-election counterpart.
        self.assertNotIn("election", _domains("Will the President be impeached before Jan 1, 2027?"))
        # A genuine presidential-election market is still detected.
        self.assertIn("election", _domains("Who will win the 2028 Presidential Election?"))

    def test_numeric_threshold_extraction_and_equality(self) -> None:
        self.assertEqual(_numeric_threshold("Will Bitcoin hit $150k by Dec 31, 2026?"), ("up", 150000.0, "usd"))
        self.assertEqual(_numeric_threshold("Will Bitcoin reach $150,000 by Dec 31?"), ("up", 150000.0, "usd"))
        self.assertEqual(
            _numeric_threshold("Will Bitcoin be above $149,999.99 by Dec 31, 2026 at 11:59 PM ET?"),
            ("up", 149999.99, "usd"),
        )
        # Percent thresholds, worded differently, same level.
        self.assertEqual(_numeric_threshold("Will inflation reach more than 5% in 2026?"), ("up", 5.0, "pct"))
        self.assertEqual(_numeric_threshold("Will CPI inflation be above 5.0% for the year?"), ("up", 5.0, "pct"))
        # Two-sided range is not a one-sided threshold.
        self.assertIsNone(_numeric_threshold("GDP growth between 2.0% and 2.5%?"))
        # "$150k" == "$149,999.99"; "5%" == "5.0%"; adjacent rungs / units differ.
        self.assertTrue(_threshold_equal(("up", 150000.0, "usd"), ("up", 149999.99, "usd")))
        self.assertTrue(_threshold_equal(("up", 5.0, "pct"), ("up", 5.0, "pct")))
        self.assertFalse(_threshold_equal(("up", 150000.0, "usd"), ("up", 139999.99, "usd")))
        self.assertFalse(_threshold_equal(("up", 5.0, "pct"), ("up", 4.9, "pct")))
        self.assertFalse(_threshold_equal(("up", 5.0, "pct"), ("up", 5.0, "usd")))

    def test_threshold_led_match_bridges_price_wording(self) -> None:
        # Same crypto level worded differently, with noise tokens, low title
        # overlap — must match on exact threshold + asset + same horizon.
        poly = titled_snap(
            "polymarket", "poly-btc",
            "Will Bitcoin hit $150k by December 31, 2026?",
            "2027-01-01T05:00:00Z",
        )
        kalshi = titled_snap(
            "kalshi", "kalshi-btc",
            "Will Bitcoin be above $149,999.99 by Dec 31, 2026 at 11:59 PM ET?",
            "2027-01-01T04:59:00Z",
        )
        pairs = match_markets([poly], [kalshi], max_close_delta_hours=9999, min_title_similarity=0.30)
        self.assertEqual(len(pairs), 1)

    def test_threshold_veto_rejects_adjacent_price_rungs(self) -> None:
        poly = titled_snap(
            "polymarket", "poly-btc2",
            "Will Bitcoin hit $150k by December 31, 2026?",
            "2027-01-01T05:00:00Z",
        )
        kalshi = titled_snap(
            "kalshi", "kalshi-btc2",
            "Will Bitcoin be above $139,999.99 by Dec 31, 2026 at 11:59 PM ET?",
            "2027-01-01T04:59:00Z",
        )
        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_foreign_market_does_not_match_unmarked_domestic(self) -> None:
        # "Will there be a recession in 2026?" (no country = US/domestic on a US
        # exchange) must match the US market, not the UK or Japan variant.
        kalshi = snap(
            "kalshi", "kalshi-rec", bid=0.4, ask=0.5,
            extra={"full_question": "Will there be a recession in 2026?"},
        )
        us = snap("polymarket", "poly-us", bid=0.4, ask=0.5,
                  extra={"full_question": "US recession by end of 2026?"})
        uk = snap("polymarket", "poly-uk", bid=0.4, ask=0.5,
                  extra={"full_question": "UK Recession in 2026?"})
        jp = snap("polymarket", "poly-jp", bid=0.4, ask=0.5,
                  extra={"full_question": "Japan recession in 2026?"})
        self.assertTrue(is_compatible_match(us, kalshi))
        self.assertFalse(is_compatible_match(uk, kalshi))
        self.assertFalse(is_compatible_match(jp, kalshi))

    def test_us_state_counts_as_domestic_not_foreign(self) -> None:
        # A US-state market vs an unmarked market must NOT be foreign-vetoed
        # (states are domestic); only foreign countries trigger that veto.
        kalshi = snap("kalshi", "kalshi-gen", bid=0.4, ask=0.5,
                      extra={"full_question": "Will the governor win re-election?"})
        ca = snap("polymarket", "poly-ca", bid=0.4, ask=0.5,
                  extra={"full_question": "Will the California governor win re-election?"})
        self.assertNotIn("california", _FOREIGN_COUNTRIES)
        # not rejected by the foreign-vs-unmarked rule (other vetoes may still
        # apply, but jurisdiction must not be the blocker here)
        self.assertTrue(is_compatible_match(ca, kalshi))

    def test_award_phrase_is_not_a_shared_proper_name(self) -> None:
        # "Nobel Peace Prize" must not be treated as a proper name; otherwise two
        # different nominees falsely "overlap" via the shared award phrase.
        unrelated_a = snap(
            "polymarket", "poly-putin", bid=0.4, ask=0.5,
            extra={"full_question": "Will Vladimir Putin win the Nobel Peace Prize in 2026?"},
        )
        unrelated_b = snap(
            "kalshi", "kalshi-amodei", bid=0.4, ask=0.5,
            extra={"full_question": "Who will win the Nobel Peace Prize? Dario Amodei"},
        )
        self.assertFalse(is_compatible_match(unrelated_a, unrelated_b))
        # The genuinely-matching nominee still matches.
        same_a = snap(
            "polymarket", "poly-pope", bid=0.4, ask=0.5,
            extra={"full_question": "Will Pope Leo XIV win the Nobel Peace Prize in 2026?"},
        )
        same_b = snap(
            "kalshi", "kalshi-pope", bid=0.4, ask=0.5,
            extra={"full_question": "Who will win the Nobel Peace Prize? Pope Leo XIV"},
        )
        self.assertTrue(is_compatible_match(same_a, same_b))

    def test_sports_adjacent_year_is_same_season(self) -> None:
        # Polymarket labels by season-start ("2026 NFL MVP"); Kalshi by award
        # year ("2027"). An adjacent-year gap in a sports context is the same
        # award and must match; a 2-year gap is a different season and must not.
        # Distinct, far-apart close times so the year veto (not the same-horizon
        # relaxation) is what is being exercised.
        poly = snap("polymarket", "poly-mvp", bid=0.4, ask=0.5,
                    extra={"full_question": "Will Josh Allen win the 2026 NFL MVP?"})
        poly.close_time = "2027-02-15T00:00:00Z"
        kalshi_same = snap("kalshi", "kalshi-mvp", bid=0.4, ask=0.5,
                           extra={"full_question": "Will Josh Allen win the MVP?",
                                  "event_title": "NFL MVP 2027"})
        kalshi_same.close_time = "2028-02-12T00:00:00Z"
        kalshi_other = snap("kalshi", "kalshi-mvp2", bid=0.4, ask=0.5,
                            extra={"full_question": "Will Josh Allen win the MVP?",
                                   "event_title": "NFL MVP 2028"})
        kalshi_other.close_time = "2029-02-12T00:00:00Z"
        self.assertTrue(is_compatible_match(poly, kalshi_same))
        self.assertFalse(is_compatible_match(poly, kalshi_other))

    def test_non_sports_adjacent_year_still_vetoed(self) -> None:
        # The adjacent-year relaxation is sports-only; elections/other markets
        # keep the strict year veto.
        poly = snap("polymarket", "poly-rec", bid=0.4, ask=0.5,
                    extra={"full_question": "US recession in 2026?"})
        poly.close_time = "2027-01-31T00:00:00Z"
        kalshi = snap("kalshi", "kalshi-rec", bid=0.4, ask=0.5,
                      extra={"full_question": "US recession in 2027?"})
        kalshi.close_time = "2028-01-31T00:00:00Z"
        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_win_nomination_matches_be_the_nominee(self) -> None:
        # Polymarket "win the ... nomination" and Kalshi "be the ... nominee"
        # are the same contract and must match.
        poly = snap("polymarket", "poly-nom", bid=0.4, ask=0.5,
                    extra={"full_question": "Will Gavin Newsom win the 2028 Democratic presidential nomination?"})
        kalshi = snap("kalshi", "kalshi-nom", bid=0.4, ask=0.5,
                      extra={"full_question": "Will Gavin Newsom be the Democratic Presidential nominee in 2028?"})
        self.assertTrue(is_compatible_match(poly, kalshi))

    def test_presidential_nomination_not_vice_presidential(self) -> None:
        # Same candidate, but Presidential vs Vice-Presidential nominee are
        # different offices/contracts and must not match.
        self.assertEqual(_offices("Democratic Vice Presidential nominee"), {"vice_president"})
        self.assertEqual(_offices("Democratic Presidential nominee"), {"president"})
        poly = snap("polymarket", "poly-pn", bid=0.4, ask=0.5,
                    extra={"full_question": "Will Gavin Newsom win the 2028 Democratic presidential nomination?"})
        kalshi_vp = snap("kalshi", "kalshi-vp", bid=0.4, ask=0.5,
                         extra={"full_question": "Will Gavin Newsom be the Democratic Vice Presidential nominee in 2028?"})
        self.assertFalse(is_compatible_match(poly, kalshi_vp))

    def test_head_of_state_matches_leader_not_an_election(self) -> None:
        # "head of state" must not be tagged as an election domain (bare "state")
        # so "be the leader of X" and "be the head of state of X" can match.
        self.assertNotIn("election", _domains("Will X be the head of state of Venezuela?"))
        poly = snap("polymarket", "poly-vz", bid=0.4, ask=0.5,
                    extra={"full_question": "Will María Corina Machado be the leader of Venezuela end of 2026?"})
        kalshi = snap("kalshi", "kalshi-vz", bid=0.4, ask=0.5,
                      extra={"full_question": "Will María Corina Machado be the head of state of Venezuela on Dec 31, 2026?"})
        self.assertTrue(is_compatible_match(poly, kalshi))

    def test_out_as_leader_does_not_match_be_leader(self) -> None:
        # Leaving a role is the opposite of holding it; they must not match.
        poly = snap("polymarket", "poly-out", bid=0.4, ask=0.5,
                    extra={"full_question": "Will Delcy Rodríguez out as leader of Venezuela by end of 2026?"})
        kalshi = snap("kalshi", "kalshi-be", bid=0.4, ask=0.5,
                      extra={"full_question": "Will Delcy Rodríguez be the head of state of Venezuela on Dec 31, 2026?"})
        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_single_seat_does_not_match_chamber_control(self) -> None:
        # A single House district must not match chamber-wide control; the real
        # chamber-control market must.
        # Real Kalshi title carries the party via yes_sub_title (kalshi_market_title).
        kalshi = snap("kalshi", "kalshi-house", bid=0.4, ask=0.5,
                      extra={"full_question": "Will Republicans win the House in 2026? Republican Party"})
        seat = snap("polymarket", "poly-seat", bid=0.4, ask=0.5,
                    extra={"full_question": "Will the Republican Party win the IN-01 House seat?"})
        chamber = snap("polymarket", "poly-chamber", bid=0.4, ask=0.5,
                       extra={"full_question": "Will the Republican Party control the House after the 2026 Midterm elections?"})
        self.assertFalse(is_compatible_match(seat, kalshi))
        self.assertTrue(is_compatible_match(chamber, kalshi))

    def test_conjunctive_parlay_is_filtered(self) -> None:
        self.assertTrue(_is_parlay_market(
            {"title": "Will ACA credits not be extended and will the Republicans win the House in 2026?"}))
        self.assertFalse(_is_parlay_market(
            {"title": "Will the Republican Party control the House after the 2026 Midterm elections?"}))

    def test_party_race_matches_despite_candidate_label(self) -> None:
        # Kalshi labels a party row with the nominee's name (yes_sub_title), but
        # the contract is still "the party wins this race". It must match the
        # Polymarket party market for the SAME race.
        poly = snap("polymarket", "poly-ga-d", bid=0.4, ask=0.5,
                    extra={"full_question": "Will the Democrats win the Georgia Senate race in 2026?"})
        kalshi = snap("kalshi", "kalshi-ga-d", bid=0.4, ask=0.5,
                      extra={"full_question": "Will Democratics win the Senate race in Georgia? Jon Ossoff"})
        self.assertTrue(is_compatible_match(poly, kalshi))
        # ...but not the SAME party in a DIFFERENT state (jurisdiction veto).
        kalshi_tx = snap("kalshi", "kalshi-tx-d", bid=0.4, ask=0.5,
                         extra={"full_question": "Will Democratics win the Senate race in Texas? Colin Allred"})
        self.assertFalse(is_compatible_match(poly, kalshi_tx))

    def test_count_threshold_cutoff_normalization(self) -> None:
        # "more than 84.5 games" and "at least 85 games" are the same cutoff (85).
        self.assertEqual(_numeric_threshold("win more than 84.5 games"), ("up", 85.0, "count"))
        self.assertEqual(_numeric_threshold("win at least 85 games this season"), ("up", 85.0, "count"))
        self.assertEqual(_numeric_threshold("win at least 90 games"), ("up", 90.0, "count"))
        self.assertTrue(_threshold_equal(("up", 85.0, "count"), ("up", 85.0, "count")))
        self.assertFalse(_threshold_equal(("up", 85.0, "count"), ("up", 90.0, "count")))
        # A bare count with no count-noun context is not a threshold.
        self.assertIsNone(_numeric_threshold("more than 84.5 by 2026"))

    def test_win_total_matches_correct_rung_only(self) -> None:
        poly = titled_snap("polymarket", "poly-wt",
                           "Will the Toronto Blue Jays win more than 84.5 games in the 2026 MLB Regular Season?",
                           "2026-10-05T00:00:00Z")
        k85 = titled_snap("kalshi", "k85", "Will Toronto win at least 85 games this season? 85+ wins",
                          "2026-11-08T00:00:00Z")
        k90 = titled_snap("kalshi", "k90", "Will Toronto win at least 90 games this season? 90+ wins",
                          "2026-11-08T00:00:00Z")
        self.assertTrue(is_compatible_match(poly, k85))
        self.assertFalse(is_compatible_match(poly, k90))

    def test_matcher_rejects_win_vs_run_for_office(self) -> None:
        poly = snap(
            "polymarket",
            "poly-win-governor",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Kamala Harris win the California Governor Election in 2026?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-run-governor",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Kamala Harris run for California Governor?"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_matcher_rejects_named_candidate_against_party_contract(self) -> None:
        poly = snap(
            "polymarket",
            "poly-candidate",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Rick Caruso win the California Governor Election in 2026?"},
        )
        kalshi = snap(
            "kalshi",
            "kalshi-party",
            bid=0.4,
            ask=0.5,
            extra={"full_question": "Will Labour win the 2026 Makerfield by-election?"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_arb_gate_rejects_related_but_different_contract_types(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-mvp",
            "Jalen Brunson",
            "2026-06-01T00:00:00Z",
            extra={"event_title": "NBA Playoffs: Eastern Conference Finals MVP"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-steals",
            "Jalen Brunson: 3+ steals",
            "2026-06-01T00:00:00Z",
            extra={"event_title": "New York at Cleveland: Steals"},
        )

        self.assertFalse(is_arb_eligible(poly, kalshi))

    def test_arb_gate_requires_specific_outcome_identity(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-okx",
            "OKX IPO in 2026?",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "OKX IPO in 2026?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-generic-ipo",
            "Who will IPO in 2026?",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "Which Companies will officially announce an IPO this year?"},
        )

        self.assertFalse(is_arb_eligible(poly, kalshi))

    def test_arb_gate_allows_exact_specific_contract(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-temp",
            "80-81°F",
            "2026-05-25T00:00:00Z",
            extra={"event_title": "Lowest temperature in Miami on May 25?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-temp",
            "Will the minimum temperature be 80-81° on May 25, 2026?",
            "2026-05-25T00:00:00Z",
            extra={"event_title": "Lowest temperature in Miami on May 25, 2026?"},
        )

        self.assertTrue(is_arb_eligible(poly, kalshi))

    def test_arb_gate_ignores_market_ids_for_entity_identity(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-opec",
            "Will another country leave OPEC in 2026?",
            "2027-01-01T00:00:00Z",
            extra={"event_title": "Will another country leave OPEC in 2026?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "KXLEAVEOPEC-27",
            "Will another country leave OPEC in 2026?",
            "2027-01-01T00:00:00Z",
            extra={"event_title": "Will another country leave OPEC in 2026?"},
        )

        self.assertTrue(is_arb_eligible(poly, kalshi))

    def test_arb_gate_allows_exact_engagement_contract(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-timothee-kylie",
            "Kylie Jenner and Timothée Chalamet engaged in 2026?",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "Kylie Jenner and Timothée Chalamet engaged in 2026?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "KXENGAGEMENTTIMOTHEEKYLIE-26",
            "Will Timothée Chalamet and Kylie Jenner be engaged in 2026?",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "Will Timothée Chalamet and Kylie Jenner be engaged in 2026?"},
        )

        self.assertTrue(is_arb_eligible(poly, kalshi))

    def test_arb_gate_allows_exact_comparison_contract(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-btc-gold",
            "Will Bitcoin outperform Gold in 2026?",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "Will Bitcoin outperform Gold in 2026?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "KXBTCVSGOLD-26",
            "Will Bitcoin outperform gold in 2026?",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "Will Bitcoin outperform gold in 2026?"},
        )

        self.assertTrue(is_arb_eligible(poly, kalshi))

    @patch("discover._enrich_kalshi")
    @patch("discover._enrich_polymarket")
    @patch("discover._match_groups_then_individual")
    @patch("polymarket.client.PolymarketClient.search_markets")
    @patch("polymarket.client.PolymarketClient.search_events")
    @patch("kalshi.client.KalshiClient.get_markets")
    @patch("kalshi.client.KalshiClient.get_all_events")
    def test_discover_fast_scan_does_not_emit_catalog_price_arbs(
        self,
        mock_events,
        mock_markets,
        mock_poly_events,
        mock_poly_markets,
        mock_match,
        mock_enrich_poly,
        mock_enrich_kalshi,
    ) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-opec",
            "Will another country leave OPEC in 2026?",
            "2027-01-01T00:00:00Z",
            extra={"event_title": "Will another country leave OPEC in 2026?"},
        )
        poly.orderbook = OrderBook(
            bids=[PriceLevel(0.95, 10.0)],
            asks=[PriceLevel(0.95, 10.0)],
        )
        kalshi = titled_snap(
            "kalshi",
            "KXLEAVEOPEC-27",
            "Will another country leave OPEC in 2026?",
            "2027-01-01T00:00:00Z",
            extra={"event_title": "Will another country leave OPEC in 2026?"},
        )
        kalshi.orderbook = OrderBook(
            bids=[PriceLevel(0.10, 10.0)],
            asks=[PriceLevel(0.10, 10.0)],
        )
        mock_events.return_value = [
            {
                "title": "Will another country leave OPEC in 2026?",
                "event_ticker": "KXLEAVEOPEC-27",
                "close_time": "2027-01-01T00:00:00Z",
            }
        ]
        mock_markets.return_value = {
            "markets": [
                {
                    "ticker": "KXLEAVEOPEC-27",
                    "event_ticker": "KXLEAVEOPEC-27",
                    "title": "Will another country leave OPEC in 2026?",
                    "status": "active",
                    "close_time": "2027-01-01T00:00:00Z",
                    "yes_bid_dollars": "0.10",
                    "yes_ask_dollars": "0.10",
                }
            ],
            "cursor": None,
        }
        mock_poly_events.return_value = [
            {
                "title": "Will another country leave OPEC in 2026?",
                "slug": "will-another-country-leave-opec-in-2026",
                "markets": [
                    {
                        "conditionId": "poly-opec",
                        "active": True,
                        "question": "Will another country leave OPEC in 2026?",
                        "outcomePrices": ["0.95", "0.05"],
                    }
                ],
            }
        ]
        mock_poly_markets.return_value = []
        mock_match.return_value = [MatchedPair(poly, kalshi, 1.0, 1.0, 0.0)]

        rows = discover(show_prices=False)

        self.assertTrue(rows[0]["arb_eligible"])
        self.assertIsNone(rows[0]["arb_direction"])
        self.assertIsNone(rows[0]["arb_net_profit"])
        mock_enrich_poly.assert_not_called()
        mock_enrich_kalshi.assert_not_called()

    def test_arb_gate_rejects_time_scope_mismatch(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-hit-may",
            "Olivia Dean",
            "2026-05-31T00:00:00Z",
            extra={"event_title": "Which artists will have #1 hits in May?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-hit-year",
            "Will Olivia Dean have a #1 hit this year?",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "Who will have a #1 hit this year?"},
        )

        self.assertFalse(is_arb_eligible(poly, kalshi))

    def test_arb_gate_rejects_weather_date_mismatch(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-temp-may27",
            "66-67°F",
            "2026-05-27T00:00:00Z",
            extra={"event_title": "Highest temperature in San Francisco on May 27?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-temp-may24",
            "Will the maximum temperature be 66-67° on May 24, 2026?",
            "2026-05-24T00:00:00Z",
            extra={"event_title": "Highest temperature in San Francisco on May 24, 2026?"},
        )

        self.assertFalse(is_arb_eligible(poly, kalshi))

    def test_matcher_rejects_draw_against_winner_market(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-draw",
            "Draw (DR Congo vs. Uzbekistan)",
            "2026-06-01T00:00:00Z",
            extra={"event_title": "DR Congo vs. Uzbekistan"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-winner",
            "Congo DR vs Uzbekistan Winner?",
            "2026-06-01T00:00:00Z",
            extra={"event_title": "Congo DR vs Uzbekistan"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_matcher_rejects_same_prop_on_different_fixtures(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-btts",
            "Both Teams to Score",
            "2026-06-01T00:00:00Z",
            extra={"event_title": "Club Guabirá vs. CDT RealOruro - More Markets"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-btts",
            "Will both teams score?",
            "2026-06-01T00:00:00Z",
            extra={"event_title": "PSG vs Arsenal: BTTS"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))

    def test_matcher_allows_championship_winner_option(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-iihf",
            "Finland",
            "2026-06-01T00:00:00Z",
            extra={"event_title": "Hockey: 2026 IIHF Championship Winner"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-iihf",
            "Will Finland win the IIHF World Championship?",
            "2026-06-01T00:00:00Z",
            extra={"event_title": "IIHF World Championship Winner"},
        )

        self.assertTrue(is_compatible_match(poly, kalshi))

    def test_matcher_does_not_treat_in_as_indiana(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-bitcoin",
            "Bitcoin all time high by December 31, 2026",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "Bitcoin all time high by ___?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-bitcoin",
            "Will BTC be above $85000.00 by 11:59 PM ET on May 31, 2026?",
            "2026-05-31T00:00:00Z",
            extra={"event_title": "How high will Bitcoin get in May?"},
        )

        self.assertNotIn("indiana", __import__("matcher")._jurisdictions(poly.title))
        self.assertTrue(is_compatible_match(poly, kalshi))

    def test_matcher_allows_lula_name_alias(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-lula",
            "Lula da Silva - Brazil President",
            "2027-01-01T00:00:00Z",
            extra={"event_title": "Next leader out of power before 2027?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-lula",
            "Will Luiz Inácio Lula da Silva leave President of Brazil before Jan 1, 2027?",
            "2027-01-01T00:00:00Z",
            extra={"event_title": "Which leaders will leave office in 2026?"},
        )

        self.assertTrue(is_compatible_match(poly, kalshi))

    def test_group_matcher_requires_outcome_label_overlap(self) -> None:
        poly = snap(
            "polymarket",
            "poly-andy",
            bid=0.22,
            ask=0.22,
            extra={"event_title": "2028 Democratic presidential nominee"},
        )
        poly.title = "Andy Beshear"
        kalshi = snap(
            "kalshi",
            "kalshi-generic",
            bid=0.22,
            ask=0.22,
            extra={"event_title": "2028 Democratic presidential nominee"},
        )
        kalshi.title = "Who will win the next presidential election?"

        self.assertEqual(_match_outcomes_within_group([kalshi], [poly], group_sim=0.9), [])

    def test_matcher_rejects_election_vs_trillionaire_topic(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-elon-president",
            "Elon Musk",
            "2028-11-07T00:00:00Z",
            extra={"event_title": "Presidential Election Winner 2028"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-elon-trillionaire",
            "Will Elon Musk be a trillionaire before 2028?",
            "2028-01-01T00:00:00Z",
            extra={"event_title": "When will Elon Musk become a trillionaire?"},
        )

        self.assertFalse(is_compatible_match(poly, kalshi))
        self.assertEqual(match_markets([poly], [kalshi], min_title_similarity=0.1), [])

    def test_matcher_rejects_non_sports_resolution_horizon_gap(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-uk-pm-2026",
            "Andy Burnham",
            "2026-12-31T00:00:00Z",
            extra={"event_title": "Next UK Prime Minister in 2026?"},
        )
        kalshi = titled_snap(
            "kalshi",
            "kalshi-uk-pm-2030",
            "Will Andy Burnham be the next Prime Minister of United Kingdom?",
            "2030-01-01T00:00:00Z",
            extra={"event_title": "Who will be the next Prime Minister of the UK?"},
        )

        self.assertFalse(is_close_time_compatible(poly, kalshi))
        self.assertEqual(match_markets([poly], [kalshi], min_title_similarity=0.1), [])

    def test_matcher_uses_event_ids_for_year_mismatch(self) -> None:
        poly = titled_snap(
            "polymarket",
            "poly-nh-gov-2026",
            "Republican",
            "",
            extra={"event_title": "New Hampshire Governor Election Winner"},
        )
        poly.event_id = "new-hampshire-governor-winner-2026"
        kalshi = titled_snap(
            "kalshi",
            "kalshi-nh-gov-2028",
            "Will the Republican party win the governorship in New Hampshire",
            "2029-11-07T00:00:00Z",
            extra={"event_title": "New Hampshire Governor winner?"},
        )
        kalshi.event_id = "GOVPARTYNH-28"

        self.assertFalse(is_compatible_match(poly, kalshi))
        self.assertEqual(match_markets([poly], [kalshi], min_title_similarity=0.1), [])

    def test_kalshi_get_all_events_runs_until_cursor_exhausted(self) -> None:
        client = KalshiClient()
        pages = [
            {"events": [{"event_ticker": "E1"}], "cursor": "next"},
            {"events": [{"event_ticker": "E2"}], "cursor": ""},
        ]
        client.get_events = MagicMock(side_effect=pages)

        events = client.get_all_events(max_pages=None, page_size=1, status="open")

        self.assertEqual([e["event_ticker"] for e in events], ["E1", "E2"])
        self.assertEqual(client.get_events.call_count, 2)

    def test_kalshi_get_all_markets_runs_until_cursor_exhausted(self) -> None:
        client = KalshiClient()
        pages = [
            {"markets": [{"ticker": "M1"}], "cursor": "next"},
            {"markets": [{"ticker": "M2"}], "cursor": ""},
        ]
        client.get_markets = MagicMock(side_effect=pages)

        markets = client.get_all_markets(max_pages=None, page_size=1, status="open")

        self.assertEqual([m["ticker"] for m in markets], ["M1", "M2"])
        self.assertEqual(client.get_markets.call_count, 2)

    def test_polymarket_search_events_runs_until_empty_page(self) -> None:
        client = PolymarketClient()
        client.get_events = MagicMock(
            side_effect=[
                [{"title": "Elon Musk trillionaire", "slug": "one"}],
                [{"title": "Unrelated event", "slug": "two"}],
                [],
            ]
        )

        events = client.search_events(["trillionaire"], max_offset=None, page_size=1)

        self.assertEqual([e["slug"] for e in events], ["one"])
        self.assertEqual(client.get_events.call_count, 3)

    @patch("kalshi.client.KalshiClient")
    def test_verify_kalshi_clob_checks_derived_yes_ask(self, client_cls: MagicMock) -> None:
        client = client_cls.return_value
        client.get_orderbook.return_value = {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.35", "10"], ["0.58", "10"]],
            }
        }

        ok, details = _verify_kalshi_clob("KXTEST", expected_price=0.42, side="ask")

        self.assertTrue(ok, details)
        self.assertEqual(details["live_yes_ask"], 0.42)

    @patch("kalshi.client.KalshiClient")
    def test_kalshi_no_intent_checks_no_ask_not_yes_bid(self, client_cls: MagicMock) -> None:
        client = client_cls.return_value
        client.get_orderbook.return_value = {
            "orderbook_fp": {
                "yes_dollars": [["0.73", "10"]],
                "no_dollars": [["0.20", "10"]],
            }
        }
        intent = TradeIntent(
            exchange="kalshi",
            contract_side="NO",
            limit_price=0.27,
            size_contracts=5,
            market_id="KXTEST",
            token_id=None,
            description="Buy NO",
        )

        ok, msg = check_price_still_valid(intent, price_tolerance=0.001)

        self.assertTrue(ok, msg)
        self.assertEqual(intent.venue_limit_price, 0.73)


class CrossPlatformFixtureRegressions(unittest.TestCase):
    """Locks in the cross-exchange pairings hardened against pairs_fixture.json.

    Each case mirrors a fixture pair that previously misaligned. They encode the
    *behaviour* (the wording shapes), not the fixture's arbitrary tickers, so the
    matcher cannot silently regress on these patterns.
    """

    def _pm(self, title: str, close: str = "2026-12-31T04:00:00Z") -> MarketSnapshot:
        return titled_snap("polymarket", "pm", title, close)

    def _k(self, title: str, close: str = "2026-12-31T04:00:00Z") -> MarketSnapshot:
        return titled_snap("kalshi", "k", title, close)

    # --- true matches that must survive (paraphrase / ticker-clip wording) ---

    def test_solana_etf_ticker_clip_matches(self) -> None:
        # PAIR-016: "Solana ETF" vs "SOL ETFs" — anchor-token + synonym bridge.
        self.assertTrue(is_compatible_match(
            self._pm("Will a US spot Solana ETF have over $5B AUM by Dec 31, 2026?"),
            self._k("SOL ETFs above $5B AUM by end of 2026?"),
        ))

    def test_cpi_paraphrase_matches(self) -> None:
        # PAIR-021: "CPI YoY" vs "June CPI"; same month, same 3.0% threshold.
        self.assertTrue(is_compatible_match(
            self._pm("Will US CPI YoY for June 2026 print above 3.0%?"),
            self._k("June CPI above 3.0% (released July)?"),
        ))

    def test_bond_actor_qualifier_does_not_block(self) -> None:
        # PAIR-046: "James Bond" vs "New Bond" — "new" is a qualifier, not anchor.
        self.assertTrue(is_compatible_match(
            self._pm("Will the next James Bond actor be announced in 2026?"),
            self._k("New Bond actor named in 2026?"),
        ))

    def test_btc_strike_with_thousands_separator_matches(self) -> None:
        # PAIR-011: "$150,000" vs "$150k" must tokenise to the same level.
        self.assertTrue(is_compatible_match(
            self._pm("Will Bitcoin be above $150,000 on December 31, 2026?"),
            self._k("BTC above $150k at year-end 2026?"),
        ))

    def test_inverted_touch_vs_hold_stays_compatible(self) -> None:
        # PAIR-017: "dip below $80k anytime" vs "stay above $80k all year" are
        # logical complements at one level — an inverted pair, not a strike clash.
        self.assertTrue(is_compatible_match(
            self._pm("Will Bitcoin dip below $80,000 at any point in 2026?"),
            self._k("BTC to stay above $80k for all of 2026?"),
        ))

    # --- traps that must keep being rejected ---

    def test_anthropic_claude_not_openai_gpt(self) -> None:
        # PAIR-041: different org AND product — entity mismatch.
        self.assertFalse(is_compatible_match(
            self._pm("Will Anthropic release a model called Claude 6 in 2026?"),
            self._k("OpenAI to release GPT-6 in 2026?"),
        ))

    def test_btc_touch_vs_close_on_date_is_settlement_mismatch(self) -> None:
        # PAIR-015: "above $175k ON Dec 31" (point) vs "touch $175k anytime".
        self.assertFalse(is_compatible_match(
            self._pm("Will Bitcoin be above $175,000 on Dec 31, 2026?"),
            self._k("BTC to touch $175k in 2026?"),
        ))

    def test_gta_midyear_deadline_not_calendar_year(self) -> None:
        # PAIR-045: "before June 30, 2026" (H1 cutoff) vs "in calendar 2026".
        self.assertFalse(is_compatible_match(
            self._pm("Will GTA VI release before June 30, 2026?", "2026-06-30T04:00:00Z"),
            self._k("GTA 6 released in calendar 2026?"),
        ))

    def test_fed_month_mismatch_rejected(self) -> None:
        # PAIR-020: September FOMC vs July FOMC are different meetings.
        self.assertFalse(is_compatible_match(
            self._pm("Will the Fed cut rates at the September 2026 FOMC meeting?", "2026-09-23T04:00:00Z"),
            self._k("Fed rate cut in July 2026?", "2026-07-29T04:00:00Z"),
        ))

    def test_indiana_pacers_not_treated_as_india(self) -> None:
        # PAIR-028: "Indiana" must not trigger the India foreign-country veto.
        self.assertTrue(is_compatible_match(
            self._pm("Will the Indiana Pacers win the 2026 NBA Finals?", "2026-06-21T04:00:00Z"),
            self._k("Pacers to win 2026 NBA Championship?", "2026-06-21T04:00:00Z"),
        ))

    def test_openai_gpt6_cluster_prefers_openai_kalshi(self) -> None:
        # Global-assignment behaviour for the GPT-6 cluster: the OpenAI PM pairs
        # with an OpenAI GPT-6 Kalshi market, and the Anthropic PM matches
        # neither. Both Kalshi listings are valid OpenAI partners, so we assert
        # the *shape* of the outcome rather than a specific ticker.
        openai_pm = titled_snap("polymarket", "pm-openai",
                                 "Will OpenAI release GPT-6 before Dec 31, 2026?", "2026-12-31T04:00:00Z")
        anthropic_pm = titled_snap("polymarket", "pm-anthropic",
                                    "Will Anthropic release a model called Claude 6 in 2026?", "2026-12-31T04:00:00Z")
        k_plain = titled_snap("kalshi", "k-plain", "GPT-6 released in 2026?", "2026-12-31T04:00:00Z")
        k_openai = titled_snap("kalshi", "k-openai", "OpenAI to release GPT-6 in 2026?", "2026-12-31T04:00:00Z")

        pairs = match_markets([openai_pm, anthropic_pm], [k_plain, k_openai],
                              min_title_similarity=0.30, max_close_delta_hours=9999)
        by_poly = {mp.poly.market_id: mp.kalshi.market_id for mp in pairs}

        self.assertIn(by_poly.get("pm-openai"), {"k-plain", "k-openai"})
        self.assertNotIn("pm-anthropic", by_poly)


if __name__ == "__main__":
    unittest.main()

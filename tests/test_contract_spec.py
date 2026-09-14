"""Tests for the v2 structured decision layer (contract_spec).

v2 must hold full parity with v1 on the 50-pair fixture and beat it on the
order-sensitive head-to-head class. Decisions must carry reasons.
"""

from __future__ import annotations

import json
import os
import unittest

from contract_spec import (
    extract_spec, match_spec,
    _bet_type, _num_range, _first_name_collision, _polarity,
    _corporate_event, _beat_order,
    _playoff_stage, _rank_number, _group_bucket_jurisdictions,
    _selected_names_per_field,
    _team_orgs, _award_family, _is_halftime, _half_number, _is_rate_count,
    _actor_verb_object, _bps_bucket, _is_weekly_recurring,
    _is_runner_up_market, _is_tournament_champion_market, _is_duration_market,
    settlement_source, _settle_src_conflict, _is_first_endorsement_market,
)
from pipeline import MarketSnapshot, OrderBook, PriceLevel

# Default to the in-repo slimmed fixture so parity ALWAYS runs (never silently
# skips off-machine, #19); PAIRS_FIXTURE overrides it with the full local file.
_IN_REPO_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pairs_fixture.json")
FIXTURE = os.environ.get("PAIRS_FIXTURE", _IN_REPO_FIXTURE)

# Precision audit fixtures (mismatch-precision workstream).
AUDIT_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "endorsed_audit_2026-09-14.json")
AUDIT_FIXTURE_ITER4 = os.path.join(os.path.dirname(__file__), "fixtures", "endorsed_audit_iter4.json")
AUDIT_FIXTURE_DEPTH_RESCUED = os.path.join(
    os.path.dirname(__file__), "fixtures", "endorsed_audit_depth_rescued.json")

# Labelled "same" pairs where v2 still fails to endorse. Empty as of
# iteration 4: the one prior entry ("Bayer Leverkusen" / single-word club
# names like "Leverkusen" never producing a selected_name) was fixed by
# matcher._bare_subject_name (a _winner_subject-based fallback kept OUT of
# _selected_names so it can't feed the hard mismatch veto) plus adding
# "bundesliga" to matcher._GENERIC_NAME_TERMS (an event-title phrase
# "Bundesliga Champion" was glomming as a phantom name that collided with the
# real one). Kept as a dict (empty) rather than deleted so the regression test
# holds the line without silently growing it back.
KNOWN_RECALL_EXCEPTIONS: set[tuple[str, str]] = set()

# The precision floor this fixture must hold, measured after the fixes in this
# workstream (see the PR description / final report for the full before/after
# table). same-count and different-count are pinned too so a future edit to
# the fixture is visible as a diff here, not just a silent threshold change.
#
# Iteration 3 (2026-09-14) added narrow structural discriminators for the
# remaining false-positive classes (award families, halftime vs full-match
# settlement, rate level vs count-of-changes, same-person-different-team/org,
# bps bucket boundaries, same-actor-different-object, weekly vs longer
# horizon) and raised precision: signal_subset 96.1% -> 98.4%, stratified
# 98.0% -> 100.0%.
#
# Iteration 4 (2026-09-14) fixed the single-word-name recall exception above,
# raising signal_subset recall to 100%; precision on this fixture is
# unchanged (98.46% / 100.0%) since the fix targeted recall, not this
# fixture's remaining 5 documented false positives.
AUDIT_EXPECTATIONS = {
    "signal_subset": {"same": 319, "different": 20, "min_precision": 0.98},
    "stratified_subset": {"same": 145, "different": 4, "min_precision": 0.99},
}

# Fresh live sample (2026-09-14, post-iteration-3): production signals and a
# stratified endorsed sample NOT already covered by the original fixture
# above. Found four new false-positive classes, each fixed by a narrow
# discriminator in contract_spec.py / matcher.py, gated on zero labelled-same
# pairs lost on EITHER fixture:
#   - runner-up vs champion/winner (outcome-tier mismatch)
#   - single-match winner vs whole-tournament champion
#   - song/album duration vs chart-position
#   - generic connector words ("without borders") wrongly treated as a
#     shared name anchor between two different organisations
# One class is left unfixed and documented: an architecture-class bucket
# ("a dLLM") vs a specific-company bucket ("Z.ai") sharing heavy AI-market
# boilerplate token overlap — too narrow/domain-specific a single case to
# generalise safely without broader regression risk.
AUDIT_EXPECTATIONS_ITER4 = {
    "signal_subset": {"same": 45, "different": 3, "min_precision": 1.0},
    "stratified_subset": {"same": 196, "different": 4, "min_precision": 0.99},
}


def snap(title: str, close: str = "2026-12-31T00:00:00Z", event_title: str = "") -> MarketSnapshot:
    return MarketSnapshot(
        source="x", market_id=title[:12], event_id="", title=title, status="open",
        close_time=close, fetched_at="x",
        orderbook=OrderBook(bids=[PriceLevel(0.4, 9.0)], asks=[PriceLevel(0.5, 9.0)]),
        extra={"event_title": event_title} if event_title else {},
    )


def decide(pm: str, k: str, c1: str = "2026-12-31T00:00:00Z", c2: str = "2026-12-31T00:00:00Z"):
    return match_spec(extract_spec(snap(pm, c1)), extract_spec(snap(k, c2)))


def decide_full(p_title, p_event, k_title, k_event, p_close="2026-12-31", k_close="2026-12-31"):
    """decide() but with event titles too — most of the audit FP classes only
    show up once title+event text is combined the way discover() builds it."""
    return match_spec(
        extract_spec(snap(p_title, p_close, p_event)),
        extract_spec(snap(k_title, k_close, k_event)),
    )


def snap_src(title: str, settle_src, close: str = "2026-12-31T00:00:00Z") -> MarketSnapshot:
    """snap() with a settle_src tuple stashed in extra, as discover.py's
    snapshot builders now do via contract_spec.settlement_source()."""
    return MarketSnapshot(
        source="x", market_id=title[:12], event_id="", title=title, status="open",
        close_time=close, fetched_at="x",
        orderbook=OrderBook(bids=[PriceLevel(0.4, 9.0)], asks=[PriceLevel(0.5, 9.0)]),
        extra={"settle_src": tuple(settle_src)},
    )


class ContractSpecFixtureParity(unittest.TestCase):
    def test_full_fixture_parity(self) -> None:
        if not os.path.exists(FIXTURE):
            self.skipTest(f"fixture not available at {FIXTURE}")
        with open(FIXTURE, encoding="utf-8") as f:
            fx = json.load(f)
        wrong = []
        inverted_ok = 0
        inverted_total = 0
        for pr in fx["pairs"]:
            pm, kk, gt = pr["polymarket"], pr["kalshi"], pr["ground_truth"]
            d = decide(pm["question"], kk["title"], pm["end_date_iso"], kk["close_time_iso"])
            if bool(d) != gt["should_match"]:
                wrong.append((pr["pair_id"], gt["match_type"], d.reasons))
            if gt["inverted"]:
                inverted_total += 1
                if d.match and d.inverted:
                    inverted_ok += 1
        self.assertEqual(wrong, [], msg=f"v2 fixture mismatches: {wrong}")
        self.assertEqual(inverted_ok, inverted_total)


class ContractSpecDecisions(unittest.TestCase):
    def test_reversed_head_to_head_rejected(self) -> None:
        # Identical token bag, only winner/loser order differs — the class the
        # bag-of-words v1 engine structurally cannot reject.
        d = decide("Will Brazil beat Argentina?", "Will Argentina beat Brazil?")
        self.assertFalse(d.match)
        self.assertTrue(any("reversed head-to-head" in r for r in d.reasons))

    def test_same_order_head_to_head_matches(self) -> None:
        d = decide("Will Brazil beat Argentina?", "Brazil to beat Argentina?")
        self.assertTrue(d.match)

    def test_acquisition_vs_ipo_rejected(self) -> None:
        # Mutually exclusive corporate events on the same company (#28).
        d = decide("Anthropic acquired before 2027?", "Will Anthropic IPO before 2027?")
        self.assertFalse(d.match)
        self.assertTrue(any("corporate-event mismatch" in r for r in d.reasons))

    def test_same_corporate_event_still_matches(self) -> None:
        # Both IPO -> the guard must not fire.
        d = decide("Will Mistral AI IPO before 2027?", "Who will IPO before 2027? Mistral AI")
        self.assertTrue(d.match)

    def test_bankruptcy_vs_other_corporate_events_rejected(self) -> None:
        d1 = decide("Will Acme file for bankruptcy before 2027?", "Will Acme IPO before 2027?")
        self.assertFalse(d1.match)
        self.assertTrue(any("corporate-event mismatch" in r for r in d1.reasons))
        d2 = decide("Will Acme go bankrupt before 2027?", "Will Acme be acquired before 2027?")
        self.assertFalse(d2.match)

    def test_same_bankruptcy_event_still_matches(self) -> None:
        d = decide("Will Acme file Chapter 11 before 2027?", "Acme bankruptcy before 2027?")
        self.assertTrue(d.match)

    def test_different_international_orgs_rejected(self) -> None:
        # BRICS vs OPEC: textually near-identical but different organizations —
        # must be rejected at the org-mismatch gate (#13).
        d = decide("Will a country leave BRICS in 2026?",
                   "Will another country leave OPEC in 2026?")
        self.assertFalse(d.match)
        self.assertTrue(any("org mismatch" in r for r in d.reasons))

    def test_same_international_org_still_matches(self) -> None:
        d = decide("Will a country leave OPEC in 2026?",
                   "Will another country leave OPEC in 2026?")
        self.assertTrue(d.match)

    def test_inverted_polarity_flagged(self) -> None:
        d = decide(
            "Will TikTok be banned in the US on Dec 31, 2026?",
            "TikTok operating legally in the US at end of 2026?",
        )
        self.assertTrue(d.match)
        self.assertTrue(d.inverted)

    def test_inverted_threshold_complement_flagged(self) -> None:
        d = decide(
            "Will Bitcoin dip below $80,000 at any point in 2026?",
            "BTC to stay above $80k for all of 2026?",
        )
        self.assertTrue(d.match)
        self.assertTrue(d.inverted)

    def test_point_vs_touch_settlement_rejected(self) -> None:
        d = decide(
            "Will Bitcoin be above $110,000 on Dec 31, 2026?",
            "BTC to touch $110k at any point in 2026?",
            "2026-12-31T17:00:00Z", "2026-12-31T17:00:00Z",
        )
        self.assertFalse(d.match)

    def test_threshold_led_bridge_accepts_low_similarity(self) -> None:
        d = decide(
            "Will BTC top $110k in 2026?",
            "Will Bitcoin be above $109,999.99 by Dec 31, 2026 at 11:59 PM ET?",
            "2026-12-31T17:00:00Z", "2026-12-31T17:00:00Z",
        )
        self.assertTrue(d.match)
        self.assertTrue(any("threshold" in r for r in d.reasons))

    def test_every_decision_has_reasons(self) -> None:
        for pm, k in [
            ("Will the Lakers win the 2026 NBA title?", "Will the Celtics win the 2026 NBA title?"),
            ("Will the Fed cut rates in March 2026?", "Fed rate hike at March 2026 meeting?"),
            ("Will Trump resign before 2027?", "Will Trump be impeached before 2027?"),
        ]:
            d = decide(pm, k)
            self.assertFalse(d.match)
            self.assertTrue(d.reasons, msg=f"no reasons for {pm!r} vs {k!r}")


class SportsBetTypeGate(unittest.TestCase):
    """Run 17 structural fix: settlement/bet-type and player-prop gates reject
    the sports phantom-arb patterns while keeping equivalent contracts matched.
    """

    def test_totals_line_vs_moneyline_rejected(self) -> None:
        d = decide("Sweden 1st Half O/U 0.5", "Will Sweden win the 1st Half?")
        self.assertFalse(d.match)
        self.assertTrue(any("bet-type" in r for r in d.reasons))

    def test_player_prop_vs_plain_rejected(self) -> None:
        self.assertFalse(decide("Cody Gakpo", "Cody Gakpo: 2+ assists?").match)
        self.assertFalse(decide("Pete Crow-Armstrong", "Pete Crow-Armstrong: 3+ total bases?").match)

    def test_equivalent_spread_restatement_still_matches(self) -> None:
        # "(-1.5)" and "wins by over 1.5 goals" are the same line -> stay matched.
        self.assertTrue(decide("DR Congo (-1.5)", "Congo DR wins by over 1.5 goals?").match)

    def test_moneyline_pairs_still_match(self) -> None:
        self.assertTrue(decide("Will the Lakers win the 2026 NBA Finals?",
                               "Lakers to win 2026 NBA Finals?").match)

    def test_spread_vs_to_score_rejected(self) -> None:
        # Run 23: a spread/margin line vs a bare to-score market are different.
        self.assertFalse(decide("Bosnia and Herzegovina (-1.5)",
                                "Will Bosnia and Herzegovina score?").match)

    def test_both_teams_to_score_and_score_totals_still_match(self) -> None:
        self.assertTrue(decide("Both Teams to Score", "Will both teams score?").match)
        # "score over 0.5" is a TOTAL line, matches an O/U line (not 'score').
        self.assertTrue(decide("Saudi Arabia O/U 0.5", "Saudi Arabia score over 0.5?").match)

    def test_total_vs_margin_rejected(self) -> None:
        # Run 26: a total (sum of goals) vs a margin/spread are different.
        self.assertFalse(decide("Korea Republic O/U 2.5",
                                "Republic of Korea wins by over 2.5 goals?").match)

    def test_total_total_and_margin_margin_still_match(self) -> None:
        self.assertTrue(decide("DR Congo (-1.5)", "Congo DR wins by over 1.5 goals?").match)
        self.assertTrue(decide("Bosnia-Herzegovina O/U 0.5",
                               "Will Bosnia and Herzegovina score over 0.5?").match)

    def test_bilateral_different_partner_country_rejected(self) -> None:
        # Run 43: shared country but different partner = different bilateral pair.
        self.assertFalse(decide("Israel and Lebanon normalize relations before 2027?",
                                "Will Israel and Qatar normalize relations before 2027?").match)
        # Subset (one country) and same-set pairs still match.
        self.assertTrue(decide("Will the US and Iran reach a nuclear deal in 2026?",
                               "Iran nuclear deal in 2026?").match)

    def test_different_player_same_first_name_rejected(self) -> None:
        # Run 42: two different players sharing a first name are different contracts.
        self.assertFalse(decide("Julian Ryerson: 1+ goals", "Julian Alvarez: 1+ goals").match)
        # Same person variants (single-token / shared surname) still match.
        self.assertTrue(decide("Will Donald Trump win?", "Trump to win?").match)
        self.assertTrue(decide("Will Gavin Newsom win the 2028 nomination?",
                               "Newsom to win 2028 nomination?").match)

    def test_coin_toss_vs_tournament_rejected(self) -> None:
        # Run 37: "Who wins the toss?" vs "win the tournament" — both action 'win'.
        self.assertFalse(decide("England vs Ireland - Who wins the toss?",
                                "Will Ireland win the Women's T20 World Cup?").match)
        self.assertTrue(decide("Will Ireland win the Women's T20 World Cup?",
                               "Ireland to win the Women's T20 World Cup").match)

    def test_chart_vs_nonchart_and_sweep_rejected(self) -> None:
        # Run 35: song entry vs #1-hit chart achievement; sweep vs qualify.
        self.assertFalse(decide("Hit The Wall - Gracie Abrams",
                                "Will Gracie Abrams have a #1 hit this year?").match)
        self.assertFalse(decide("Will the IEM Cologne Major 2026 Grand Final be a sweep?",
                                "Will B8 qualify for the Grand Final at 2026 IEM Cologne?").match)

    def test_negative_vs_positive_bucket_rejected(self) -> None:
        # Run 34: a negative/contraction bucket vs an explicit positive range.
        self.assertFalse(decide("Negative GDP growth in 2026?",
                                "GDP growth in 2026? 4.6% to 5.0%").match)
        # Two positive overlapping buckets (no negative cue) still match.
        self.assertTrue(decide("GDP growth 2.0% to 2.5%",
                               "GDP growth in 2026? 2.1% to 2.5%").match)

    def test_numeric_range_mismatch_rejected(self) -> None:
        # Run 33: non-overlapping buckets differ; overlapping ones stay matched.
        self.assertFalse(decide("GDP growth in 2026? 2.0% to 2.5%",
                                "GDP growth in 2026? 4.6% to 5.0%").match)
        self.assertTrue(decide("GDP growth 2.0% to 2.5%",
                               "GDP growth in 2026? 2.1% to 2.5%").match)
        self.assertTrue(decide("high temp 78-79 on Jun 15?", "high temp 78 to 79?").match)

    def test_corners_total_vs_count_rejected(self) -> None:
        # Run 32: "Corners O/U 2.5" (total) vs "8+ corners" (count) — different.
        self.assertFalse(decide("New Zealand Corners: O/U 2.5", "New Zealand: 8+ corners").match)
        self.assertFalse(decide("Saudi Arabia Corners: O/U 2.5", "Saudi Arabia: 4+ corners").match)

    def test_correct_score_vs_moneyline_rejected(self) -> None:
        # Run 27: exact scoreline vs a win/margin market are different contracts.
        self.assertFalse(decide("Saudi Arabia 0 - 2 Uruguay",
                                "Will the final score be Saudi Arabia wins by 2 goals?").match)

    def test_scoreline_does_not_catch_years(self) -> None:
        # The single-digit scoreline regex must not classify "2028"/"2026-06" rows.
        self.assertTrue(decide("Will Newsom win the 2028 Democratic nomination?",
                               "Newsom to win 2028 Democratic nomination?").match)


class BetType(unittest.TestCase):
    """Sports contract-type discrimination for the v2 gate (#108)."""

    def test_total(self):
        self.assertEqual(_bet_type("Sweden 1st Half O/U 0.5"), "total")

    def test_margin_wins_by(self):
        self.assertEqual(_bet_type("Korea wins by over 2.5 goals"), "margin")

    def test_margin_spread_paren(self):
        self.assertEqual(_bet_type("DR Congo (-1.5)"), "margin")

    def test_correct_score(self):
        self.assertEqual(_bet_type("Brazil 2-1 Argentina correct score?"), "correct_score")

    def test_score(self):
        self.assertEqual(_bet_type("Both Teams to Score?"), "score")

    def test_prop(self):
        self.assertEqual(_bet_type("Cody Gakpo: 2+ assists"), "prop")

    def test_moneyline(self):
        self.assertEqual(_bet_type("Will the Lakers win?"), "moneyline")

    def test_none(self):
        self.assertIsNone(_bet_type("Who will be president?"))


class NumRange(unittest.TestCase):
    def test_small_bucket(self):
        self.assertEqual(_num_range("GDP growth 2 to 3%"), (2.0, 3.0))

    def test_large_numbers_excluded(self):
        # "2026-06" is a date, not a range.
        self.assertIsNone(_num_range("2026-06 outcome"))


class FirstNameCollision(unittest.TestCase):
    """Different-people phantom-arb guard (run 52/54)."""

    def test_same_first_different_surname_is_collision(self):
        self.assertTrue(_first_name_collision(
            frozenset({"julian ryerson"}), frozenset({"julian alvarez"})))

    def test_single_token_name_is_not_collision(self):
        self.assertFalse(_first_name_collision(
            frozenset({"donald trump"}), frozenset({"trump"})))

    def test_office_descriptor_fragments_excluded(self):
        self.assertFalse(_first_name_collision(
            frozenset({"democratic vice"}), frozenset({"democratic vp"})))

    def test_shared_surname_is_same_person(self):
        self.assertFalse(_first_name_collision(
            frozenset({"donald trump"}), frozenset({"fred trump"})))


class Polarity(unittest.TestCase):
    def test_negative_cue(self):
        self.assertTrue(_polarity("Will TikTok be banned?"))

    def test_positive_only_is_false(self):
        self.assertFalse(_polarity("Will TikTok stay legal?"))


class CorporateEvent(unittest.TestCase):
    """Mutually-exclusive terminal states (#28/#30)."""

    def test_ipo(self):
        self.assertEqual(_corporate_event("Will OpenAI IPO in 2026?"), "ipo")

    def test_acquisition(self):
        self.assertEqual(_corporate_event("Will Figma be acquired by Adobe?"), "acquisition")

    def test_bankruptcy(self):
        self.assertEqual(_corporate_event("Will WeWork file for bankruptcy?"), "bankruptcy")

    def test_none(self):
        self.assertIsNone(_corporate_event("Who wins the election?"))


class BeatOrder(unittest.TestCase):
    """Directional: 'A beat B' != 'B beat A'."""

    def test_a_beats_b(self):
        self.assertEqual(_beat_order("Will Lakers beat Celtics?"), ("lakers", "celtics"))

    def test_b_beats_a_is_reversed(self):
        self.assertEqual(_beat_order("Will Celtics defeat Lakers?"), ("celtics", "lakers"))

    def test_none_without_beat_framing(self):
        self.assertIsNone(_beat_order("Who wins the game?"))


class PlayoffStage(unittest.TestCase):
    """Division / conference / seed are mutually exclusive sports contracts
    (audit FP class: NHL conference-champion phantom-matched to a division
    winner on the same team; AFC West [division] phantom-matched to AFC #5
    seed)."""

    def test_conference_vs_division_is_rejected(self):
        d = decide_full(
            "Columbus Blue Jackets", "NHL: 2027 Eastern Conference Champion",
            "Will the Columbus Blue Jackets win the Metropolitan Division?",
            "NHL Metropolitan Division Winner",
        )
        self.assertFalse(d.match)
        self.assertIn("playoff-stage", d.reasons[0])

    def test_division_named_by_conference_direction_vs_seed(self):
        d = decide_full(
            "Los Angeles Chargers", "Pro Football: AFC West Champion",
            "Will Los Angeles C be the #5 seed in the American Football Conference?",
            "Pro Football Playoffs: AFC #5 Seed",
        )
        self.assertFalse(d.match)

    def test_same_stage_both_sides_not_rejected_by_this_gate(self):
        self.assertIsNone(_playoff_stage("Will McLaren win the F1 Constructors Championship?"))
        self.assertEqual(_playoff_stage("NHL Metropolitan Division Winner"), "division")
        self.assertEqual(_playoff_stage("NHL: 2027 Eastern Conference Champion"), "conference")
        self.assertEqual(_playoff_stage("Pro Football Playoffs: AFC #5 Seed"), "seed")

    def test_stage_silent_when_neither_side_uses_the_words(self):
        # Plain "wins the championship" pairs on both sides must be unaffected.
        d = decide_full(
            "Air Force Falcons", "NCAA Football 2026 Mountain West Conference: Winner",
            "Will Air Force win the College Football Mountain West Championship?",
            "College Football Mountain West Championship Winner",
        )
        self.assertTrue(d.match)


class RankNumber(unittest.TestCase):
    """A '#N' rank market is a different contract from a '#M' rank market."""

    def test_extracts_rank(self):
        self.assertEqual(_rank_number("Will X be the #1 rank on Google?"), 1)
        self.assertIsNone(_rank_number("Will X win the championship?"))

    def test_different_rank_rejected(self):
        d = decide_full(
            "Taylor Swift", "#1 Searched Person on Google in the US 2026?",
            "Will Taylor Swift be the #2 rank on Google's Year in Search 2026 Global – People?",
            "#2 Most Searched Person on Google in 2026?",
        )
        self.assertFalse(d.match)
        self.assertIn("rank mismatch", d.reasons[0])

    def test_same_rank_not_rejected_by_this_gate(self):
        d = decide_full(
            "Timothée Chalamet", "#1 Searched Person on Google 2026?",
            "Will Timothée Chalamet be the #1 rank on Google's Year in Search 2026 Global – People?",
            "#1 searched person on Google in 2026",
        )
        self.assertTrue(d.match)


class GroupBucket(unittest.TestCase):
    """An aggregate GROUP market ('a team from Texas wins') settles on ANY
    member of the group — a different, broader contract than one naming a
    specific member outside that group."""

    def test_group_region_extracted(self):
        self.assertEqual(
            _group_bucket_jurisdictions("Will a team from Texas win the 2027 Pro Football Championship?"),
            frozenset({"texas"}),
        )
        self.assertIsNone(_group_bucket_jurisdictions("Will Atlanta win the 2027 Pro Football Championship?"))

    def test_group_vs_entity_outside_group_rejected(self):
        d = decide_full(
            "Will a team from Texas win the 2027 Pro Football Championship?", "",
            "Will Atlanta win the 2027 Pro Football Championship?", "2027 Pro Football Champion",
        )
        self.assertFalse(d.match)
        self.assertIn("group bucket", d.reasons[0])

    def test_group_vs_member_of_group_not_rejected_by_this_gate(self):
        # A Texas team named on the other side must NOT be rejected by this
        # gate (only by ordinary similarity if that team isn't also named).
        self.assertIsNone(_group_bucket_jurisdictions("Will Houston win the 2027 Pro Football Championship?"))


class SelectedNamesPerField(unittest.TestCase):
    """selected_names is extracted PER FIELD (title, event_title) and unioned,
    not from the single title+event_title-joined string. A capitalized name
    run can otherwise bleed across that artificial join and get dropped
    entirely by matcher._proper_names's office+jurisdiction filter (audit
    false-reject: 'Renan Santos' + 'Brazil Presidential Election First Round
    Winner' read as one 7-word run and lost the name, leaving a phantom
    'first round winner')."""

    def test_name_survives_when_event_title_would_have_glommed_it(self):
        s = snap("Renan Santos", event_title="Brazil Presidential Election First Round Winner")
        self.assertIn("renan santos", _selected_names_per_field(s))

    def test_previously_broken_pair_now_matches(self):
        d = decide_full(
            "Renan Santos", "Brazil Presidential Election First Round Winner",
            "Will Renan Santos win the first round of the 2026 Brazilian presidential election?",
            "Brazil presidential election: first round winner?",
        )
        self.assertTrue(d.match)


class SamePersonDifferentTeamOrg(unittest.TestCase):
    """Same person, but named for a different team/org (audit FP: Rocco
    Baldelli as Phillies manager candidate vs Red Sox manager candidate).
    Gated on both an actual person-name overlap AND both sides naming a KNOWN
    team from the closed gazetteer — never a generic entity diff (a prior
    generic gate broke ~41 true pairs and was reverted)."""

    def test_team_orgs_extracted_and_deduped(self):
        self.assertEqual(_team_orgs("MLB: Next Phillies Manager"), frozenset({"phillies"}))
        self.assertEqual(
            _team_orgs("Who will be the next Manager of the Boston Red Sox?"),
            frozenset({"red sox"}),
        )
        # Full "city + nickname" and bare nickname collapse to one team.
        self.assertEqual(_team_orgs("New York Yankees"), _team_orgs("Yankees"))

    def test_different_team_rejected(self):
        d = decide_full(
            "Rocco Baldelli", "MLB: Next Phillies Manager",
            "Who will be the next Manager of the Boston Red Sox? Rocco Baldelli",
            "Boston: Next Manager",
        )
        self.assertFalse(d.match)
        self.assertIn("different team/org", d.reasons[0])

    def test_same_team_not_rejected_by_this_gate(self):
        d = decide_full(
            "Rocco Baldelli", "MLB: Next Phillies Manager",
            "Who will be the next Manager of the Philadelphia Phillies? Rocco Baldelli",
            "Philadelphia: Next Manager",
        )
        self.assertTrue(d.match)

    def test_no_known_team_named_not_rejected_by_this_gate(self):
        # Neither side names a KNOWN team — this gate must stay silent (only
        # ordinary similarity/other gates apply).
        d = decide_full(
            "Yankees", "New York Yankees win 2026 World Series",
            "Will the Yankees win the 2026 World Series?", "MLB 2026 World Series",
        )
        self.assertTrue(d.match)


class AwardFamily(unittest.TestCase):
    """Mutually exclusive named college-football honors (audit FP: Heisman
    phantom-matched to Walter Camp / Doak Walker on the same player)."""

    def test_extracts_family(self):
        self.assertEqual(_award_family("NCAA Football: 2026 Heisman Award Winner"), "heisman")
        self.assertEqual(_award_family("Walter Camp Award Winner"), "walter_camp")
        self.assertEqual(_award_family("Doak Walker Award Winner"), "doak_walker")
        self.assertIsNone(_award_family("Dante Moore wins the award"))

    def test_different_award_rejected(self):
        d = decide_full(
            "Dante Moore", "NCAA Football: 2026 Heisman Award Winner",
            "Dante Moore wins the award", "Walter Camp Award Winner",
        )
        self.assertFalse(d.match)
        self.assertIn("award-family", d.reasons[0])

    def test_same_award_not_rejected_by_this_gate(self):
        d = decide_full(
            "Dante Moore", "NCAA Football: 2026 Heisman Award Winner",
            "Dante Moore wins the award", "2026 Heisman Trophy Winner",
        )
        self.assertTrue(d.match)

    def test_unrelated_award_wording_not_flagged(self):
        # Generic "award"/"Emmy Awards"/"Game Awards" mentions must not be
        # mistaken for one of the curated families.
        d = decide_full(
            "“Saturday Night Live”", "Emmys 2026: Outstanding variety series",
            "Will Saturday Night Live win Outstanding Variety Series at the Emmy Awards?",
            "Emmy winner: Outstanding Variety Series",
        )
        self.assertTrue(d.match)


class HalftimeVsFullMatch(unittest.TestCase):
    """A halftime/1st-half result market and a full-match moneyline/spread/
    total market are different contracts (audit FP: Al-Shamal vs Al-Ittihad
    halftime result phantom-matched a full-match goal-spread market)."""

    def test_is_halftime(self):
        self.assertTrue(_is_halftime("Al-Shamal vs. Al-Ittihad Club - Halftime Result"))
        self.assertTrue(_is_halftime("Sweden 1st Half O/U 0.5"))
        self.assertFalse(_is_halftime("Half-Life 3"))  # must not fire on unrelated "half"
        self.assertFalse(_is_halftime("Al-Ittihad wins by more than 1.5 goals?"))

    def test_halftime_vs_full_match_spread_rejected(self):
        d = decide_full(
            "Al-Ittihad Club", "Al-Shamal vs. Al-Ittihad Club - Halftime Result",
            "Al-Ittihad wins by more than 1.5 goals?", "Al-Shamal vs Al-Ittihad: Spread",
        )
        self.assertFalse(d.match)
        self.assertIn("settlement-period", d.reasons[0])

    def test_both_halftime_not_rejected_by_this_gate(self):
        d = decide_full(
            "Al-Ittihad Club", "Al-Shamal vs. Al-Ittihad Club - Halftime Result",
            "Will Al-Ittihad win at halftime?", "Al-Shamal vs Al-Ittihad: Halftime Result",
        )
        self.assertTrue(d.match)


class RateLevelVsChangeCount(unittest.TestCase):
    """A Fed-rate LEVEL market ("Fed Rate hit 5.0%") and a number-of-
    cuts/hikes/changes COUNT market are different measurements (audit FP:
    "^ 5.0%" phantom-matched "number of rate changes ... exactly 5")."""

    def test_is_rate_count(self):
        self.assertTrue(_is_rate_count("Will the number of rate changes before 2027 be exactly 5?"))
        self.assertFalse(_is_rate_count("What will Fed Rate hit before 2027?"))

    def test_level_vs_count_rejected(self):
        d = decide_full(
            "↑ 5.0%", "What will Fed Rate hit before 2027?",
            "Will the number of rate changes before 2027 be exactly 5?",
            "Number of Fed rate changes before 2027",
        )
        self.assertFalse(d.match)
        self.assertIn("number-of-changes", d.reasons[0])


class ActorVerbObject(unittest.TestCase):
    """Same actor performing the same curated VERB on two different OBJECTS
    is a different contract (audit FP: "Will Trump nationalize elections?"
    phantom-matched "Will Trump nationalize SpaceX?")."""

    def test_extracts_actor_verb_object(self):
        self.assertEqual(
            _actor_verb_object("Will Trump nationalize elections?"),
            ("trump", "nationalize", frozenset({"elections"})),
        )
        self.assertIsNone(_actor_verb_object("Will Trump win the election?"))

    def test_different_object_rejected(self):
        d = decide_full(
            "Will Trump nationalize elections?", "Will Trump nationalize elections?",
            "Will trump nationalize SpaceX? Before Jan 2027", "Will Trump nationalize SpaceX?",
        )
        self.assertFalse(d.match)
        self.assertIn("different object", d.reasons[0])

    def test_different_verb_not_rejected_by_this_gate(self):
        # "acquire" vs "buy" — different curated verbs, so this gate stays
        # silent (Greenland acquisition pairs must still match on similarity).
        d = decide_full(
            "Will Trump acquire Greenland before 2027?", "",
            "Will Trump buy Greenland? Before 2027", "",
        )
        self.assertTrue(d.match)


class BpsBucketBoundary(unittest.TestCase):
    """Same-direction bps buckets with non-overlapping magnitude ranges are
    different contracts (audit FP: two Bank of England pairs — "25 bps
    decrease" (exact) vs "cut more than 25bps"; "50+ bps increase" vs "hike
    1-25bps")."""

    def test_parses_buckets(self):
        self.assertEqual(_bps_bucket("25 bps decrease"), ("decrease", 25.0, 25.0))
        self.assertEqual(_bps_bucket("50+ bps increase"), ("increase", 50.0, float("inf")))
        self.assertEqual(_bps_bucket("Hike 1-25bps"), ("increase", 1.0, 25.0))
        self.assertEqual(
            _bps_bucket("Cut more than 25bps")[1:], (25.01, float("inf")),
        )
        self.assertIsNone(_bps_bucket("no bps mentioned here"))

    def test_exact_vs_open_ended_rejected(self):
        d = decide_full(
            "25 bps decrease", "Bank of England decision in November?",
            "Will the Bank of England Cut more than 25bps at the November Monetary Policy Committee meeting?",
            "Bank of England rate decision in November",
        )
        self.assertFalse(d.match)
        self.assertIn("bps bucket", d.reasons[0])

    def test_disjoint_ranges_rejected(self):
        d = decide_full(
            "50+ bps increase", "Bank of England decision in November?",
            "Will the Bank of England Hike 1-25bps at the November Monetary Policy Committee meeting?",
            "Bank of England rate decision in November",
        )
        self.assertFalse(d.match)

    def test_boundary_touching_ranges_still_match(self):
        # "25 bps increase" (exact) sits inside the inclusive "1-25bps" bucket
        # — must NOT be rejected by this gate.
        d = decide_full(
            "25 bps increase", "Bank of England decision in September?",
            "Will the Bank of England Hike 1-25bps at the September Monetary Policy Committee meeting?",
            "Bank of England rate decision in September",
        )
        self.assertTrue(d.match)


class WeeklyRecurringVsHorizon(unittest.TestCase):
    """A weekly-recurring market ("Top ... this week?") vs a market with a
    materially different close horizon are different contracts (audit FP:
    "Top Text to Image AI this week? OpenAI" phantom-matched a
    "best Text-to-Image AI end of October" market)."""

    def test_is_weekly_recurring(self):
        self.assertTrue(_is_weekly_recurring("Top Text to Image AI this week?"))
        self.assertFalse(_is_weekly_recurring("Which company has the best AI end of October?"))
        # Must not fire on an unrelated "week of <date>" phrase.
        self.assertFalse(_is_weekly_recurring("Trump posts the week of Sep 13, 2026"))

    def test_weekly_vs_month_scoped_rejected(self):
        d = decide_full(
            "OpenAI", "Which company has the best Text-to-Image AI end of October?",
            "Top Text to Image AI this week? OpenAI", "Top Text to Image AI this week",
            p_close="2026-11-01", k_close="2026-09-18",
        )
        self.assertFalse(d.match)
        self.assertIn("weekly-recurring", d.reasons[0])

    def test_both_weekly_same_horizon_not_rejected_by_this_gate(self):
        d = decide_full(
            "OpenAI", "Top Text to Image AI this week?",
            "Top Text to Image AI this week? OpenAI", "Top Text to Image AI this week",
            p_close="2026-09-18", k_close="2026-09-18",
        )
        self.assertTrue(d.match)


class EndorsedAuditRegression(unittest.TestCase):
    """Regression test over the labelled audit fixture: v2 precision must not
    regress below the value achieved by this workstream's fixes, and labelled
    "same" pairs must keep being endorsed except for the small documented
    exception list above."""

    @classmethod
    def setUpClass(cls):
        with open(AUDIT_FIXTURE, encoding="utf-8") as f:
            cls.fixture = json.load(f)

    def _verdict(self, item):
        p = snap(item["poly_title"], item["poly_close"] or "2026-12-31", item["poly_event_title"])
        k = snap(item["kalshi_title"], item["kalshi_close"] or "2026-12-31", item["kalshi_event_title"])
        return match_spec(extract_spec(p), extract_spec(k))

    def _check_subset(self, name, expectations=None):
        items = self.fixture[name]
        expect = (expectations or AUDIT_EXPECTATIONS)[name]
        same = [i for i in items if i["label"] == "same"]
        different = [i for i in items if i["label"] == "different"]
        self.assertEqual(len(same), expect["same"], f"{name}: unexpected same-count (fixture edited?)")
        self.assertEqual(len(different), expect["different"], f"{name}: unexpected different-count (fixture edited?)")

        tp = fp = 0
        unexpected_rejections = []
        for it in same:
            d = self._verdict(it)
            if d.match:
                tp += 1
            else:
                key = (it["poly_title"], it["kalshi_title"])
                if key not in KNOWN_RECALL_EXCEPTIONS:
                    unexpected_rejections.append(key)
        for it in different:
            if self._verdict(it).match:
                fp += 1

        self.assertEqual(
            unexpected_rejections, [],
            f"{name}: labelled-same pairs newly rejected (not in KNOWN_RECALL_EXCEPTIONS): "
            f"{unexpected_rejections}",
        )
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        self.assertGreaterEqual(
            precision, expect["min_precision"],
            f"{name}: precision {precision:.4f} fell below floor {expect['min_precision']}",
        )

    def test_signal_subset_precision_and_recall(self):
        self._check_subset("signal_subset")

    def test_stratified_subset_precision_and_recall(self):
        self._check_subset("stratified_subset")


class EndorsedAuditRegressionIter4(EndorsedAuditRegression):
    """Same regression harness as EndorsedAuditRegression, over the fresh
    iteration-4 audit fixture (tests/fixtures/endorsed_audit_iter4.json).
    Kept as a separate fixture/class rather than merged into the original so
    each fixture's own same/different counts stay independently pinned."""

    @classmethod
    def setUpClass(cls):
        with open(AUDIT_FIXTURE_ITER4, encoding="utf-8") as f:
            cls.fixture = json.load(f)

    def test_signal_subset_precision_and_recall(self):
        self._check_subset("signal_subset", AUDIT_EXPECTATIONS_ITER4)

    def test_stratified_subset_precision_and_recall(self):
        self._check_subset("stratified_subset", AUDIT_EXPECTATIONS_ITER4)


class DepthRescuedAuditRegression(unittest.TestCase):
    """Regression test over tests/fixtures/endorsed_audit_depth_rescued.json:
    the 85 pairs that became a production signal ONLY because of iteration 4's
    depth-aware executable-edge gating (alerter.compute_signals with
    min_size=MIN_DEPTH) — see docs/history/COVERAGE_ITERATIONS.md iteration 5
    ("audit the depth-rescued signals"). Every pair here is already
    v2-endorsed by construction (it was pulled from a v2-endorsed signal), so
    this is a PURE precision measurement (no recall axis) distinct from
    EndorsedAuditRegression's signal/stratified subsets: it isolates whether
    the depth-aware gate's newly-surfaced signals carry a different
    false-positive profile than the rest of the endorsed signal set.

    Five labelled-different pairs are pinned as KNOWN, documented gaps (not
    fixed by this workstream — narrower than the two discriminators iteration
    5 was scoped to add): two bucket-vs-cumulative-threshold weather pairs
    ("90-91F" vs Kalshi's "<91, 90 or below"), one bucket-granularity pair
    ("Week 1" vs "Week 1 to Week 2"), one adjacent-GDP-bucket pair (exposed by
    a pre-existing matcher._ascii_lower bug that strips the en-dash in
    "1.0–1.5%" before _num_range can parse it — matcher.py is out of this
    workstream's scope), and one GDP annual-vs-Q4 + direction mismatch.
    """

    @classmethod
    def setUpClass(cls):
        with open(AUDIT_FIXTURE_DEPTH_RESCUED, encoding="utf-8") as f:
            cls.fixture = json.load(f)["depth_rescued"]

    def test_precision(self):
        items = self.fixture
        same = [i for i in items if i["label"] == "same"]
        different = [i for i in items if i["label"] == "different"]
        self.assertEqual(len(same), 80, "fixture edited? unexpected same-count")
        self.assertEqual(len(different), 5, "fixture edited? unexpected different-count")

        tp = fp = 0
        for it in same:
            p = snap(it["poly_title"], it["poly_close"] or "2026-12-31", it["poly_event_title"])
            k = snap(it["kalshi_title"], it["kalshi_close"] or "2026-12-31", it["kalshi_event_title"])
            if match_spec(extract_spec(p), extract_spec(k)).match:
                tp += 1
        for it in different:
            p = snap(it["poly_title"], it["poly_close"] or "2026-12-31", it["poly_event_title"])
            k = snap(it["kalshi_title"], it["kalshi_close"] or "2026-12-31", it["kalshi_event_title"])
            if match_spec(extract_spec(p), extract_spec(k)).match:
                fp += 1
        # All 80 labelled-same pairs must still be endorsed (no recall loss).
        self.assertEqual(tp, 80)
        precision = tp / (tp + fp)
        # Documents the CURRENT precision on this subset (94.1%) — lower than
        # the >=98% floor on the general signal/stratified subsets, showing
        # the depth-aware gate's newly-rescued signals skew toward the
        # (documented, unfixed) bucket/threshold-alignment gap above. Not a
        # regression floor to defend below 5 known FPs; a future fix for that
        # class should raise this number, not just avoid lowering it.
        self.assertAlmostEqual(precision, 80 / 85, places=4)


class Iter4DiscriminatorTests(unittest.TestCase):
    """Unit tests for the iteration-4 narrow structural discriminators (see
    contract_spec.py's iteration-4 comment block)."""

    def test_is_runner_up_market(self):
        self.assertTrue(_is_runner_up_market("USL Championship: 2026 Runner-Up"))
        self.assertTrue(_is_runner_up_market("2026 Runner Up"))
        self.assertFalse(_is_runner_up_market("USL Championship Champion"))

    def test_runner_up_vs_champion_rejected(self):
        d = decide_full(
            "New Mexico United", "USL Championship: 2026 Runner-Up",
            "Will New Mexico United win the USL Championship?", "USL Championship Champion",
        )
        self.assertFalse(d.match)
        self.assertIn("outcome-tier mismatch", d.reasons[0])

    def test_both_runner_up_not_rejected_by_this_gate(self):
        d = decide_full(
            "New Mexico United", "USL Championship: 2026 Runner-Up",
            "Will New Mexico United be the USL Championship Runner-Up?", "USL Championship Runner-Up",
        )
        self.assertNotIn("outcome-tier mismatch", " ".join(d.reasons))

    def test_is_tournament_champion_market(self):
        self.assertTrue(_is_tournament_champion_market(
            "Will Antigua win the 2026 Caribbean Premier League championship?"))
        self.assertFalse(_is_tournament_champion_market("Antigua vs Guyana"))

    def test_single_match_vs_tournament_champion_rejected(self):
        d = decide_full(
            "Caribbean Premier League: Antigua And Barbuda Falcons vs Guyana Amazon Warriors",
            "Caribbean Premier League: Antigua And Barbuda Falcons vs Guyana Amazon Warriors",
            "Will Antigua And Barbuda Falcons win the 2026 Caribbean Premier League championship?",
            "Caribbean Premier League Champion",
        )
        self.assertFalse(d.match)
        self.assertIn("single-match vs tournament-champion mismatch", d.reasons)

    def test_two_tournament_champion_markets_not_rejected_by_this_gate(self):
        d = decide_full(
            "Antigua And Barbuda Falcons", "Caribbean Premier League Champion",
            "Will Antigua And Barbuda Falcons win the 2026 Caribbean Premier League championship?",
            "Caribbean Premier League Champion",
        )
        self.assertNotIn("single-match vs tournament-champion mismatch", d.reasons)

    def test_is_duration_market(self):
        self.assertTrue(_is_duration_market("How long will Bass Persuades by Miley Cyrus be?"))
        self.assertFalse(_is_duration_market("#2 Spotify song this week?"))

    def test_duration_vs_chart_position_rejected(self):
        d = decide_full(
            "Bass Persuades - Miley Cyrus", "#2 Spotify song this week? (September 18)",
            "How long will Bass Persuades by Miley Cyrus be? At least 30 minutes",
            "Bass Persuades: Album Duration",
        )
        self.assertFalse(d.match)
        self.assertIn("duration vs chart-position mismatch", d.reasons)

    def test_both_duration_not_rejected_by_this_gate(self):
        d = decide_full(
            "Bass Persuades - Miley Cyrus", "How long will Bass Persuades be? At least 30 minutes",
            "How long will Bass Persuades by Miley Cyrus be? At least 30 minutes",
            "Bass Persuades: Album Duration",
        )
        self.assertNotIn("duration vs chart-position mismatch", d.reasons)

    def test_different_organisation_sharing_generic_suffix_rejected(self):
        d = decide_full(
            "Reporters Without Borders", "Nobel Peace Prize Winner 2026",
            "Who will win the Nobel Peace Prize? Doctors Without Borders (Médecins Sans Frontières)",
            "2026 Nobel Peace Prize winner",
        )
        self.assertFalse(d.match)
        self.assertIn("selected-name mismatch", d.reasons[0])

    def test_half_number(self):
        self.assertEqual(_half_number("Al-Shamal vs. Al-Ittihad Club - Second Half Result"), 2)
        self.assertEqual(_half_number("Al-Shamal wins 1st Half"), 1)
        self.assertEqual(_half_number("Al-Ittihad wins by more than 1.5 goals?"), None)

    def test_second_half_vs_first_half_rejected(self):
        # Caught validating the single-entity bridge on a live sample: a bare
        # single-word outcome title has no classifiable bet_type, so the
        # existing halftime-vs-full-match gate (which requires one) couldn't
        # catch this on its own.
        d = decide_full(
            "Al-Shamal", "Al-Shamal vs. Al-Ittihad Club - Second Half Result",
            "Al-Shamal wins 1st Half", "Al-Shamal vs Al-Ittihad: First Half Winner",
        )
        self.assertFalse(d.match)
        self.assertIn("settlement-period mismatch", d.reasons[0])

    def test_same_half_not_rejected_by_this_gate(self):
        d = decide_full(
            "Al-Shamal", "Al-Shamal vs. Al-Ittihad Club - First Half Result",
            "Al-Shamal wins 1st Half", "Al-Shamal vs Al-Ittihad: First Half Winner",
        )
        self.assertNotIn("settlement-period mismatch: half", " ".join(d.reasons))

    def test_single_word_club_name_recovered(self):
        # KNOWN_RECALL_EXCEPTIONS regression: single-word club names ("Leverkusen")
        # now bridge to the multi-word outcome name via matcher._bare_subject_name.
        d = decide_full(
            "Bayer Leverkusen", "Bundesliga: 2027 Champion",
            "Will Leverkusen win the 2026-27 Bundesliga?", "Bundesliga Champion",
            p_close="2027-06-06", k_close="2027-06-06",
        )
        self.assertTrue(d.match)

    def test_bare_subject_bridge_does_not_collide_same_city_different_team(self):
        # Caught validating the single-entity bridge on a live sample: "MLB:
        # Team to win 100+ games" leaves the generic noun "team" looking like
        # a lone capitalised subject once the league acronym is dropped,
        # which wrongly made this pair ELIGIBLE for the bridge — whose actual
        # overlap was two DIFFERENT Los Angeles teams sharing the bare
        # jurisdiction name, not "team" itself. matcher._bare_subject_name now
        # requires the win-trigger clause to have exactly one capitalised
        # word from the start (never a leftover fragment).
        d = decide_full(
            "Los Angeles Dodgers", "MLB: Team to win 100+ games",
            "Los Angeles F wins", "Dallas vs Los Angeles F",
        )
        self.assertFalse(d.match)


class SettlementSourceTests(unittest.TestCase):
    """Unit tests for the iteration-5 settlement-source extractor and
    discriminator (contract_spec.settlement_source / _settle_src_conflict).
    Text fixtures below are the actual live rules_primary / description text
    for the motivating Atlanta case (KXHIGHTATL-26SEP14 vs the Polymarket
    "Highest temperature in Atlanta on September 14?" event), captured while
    investigating live on 2026-09-14."""

    KALSHI_ATL_RULES = (
        "If the maximum temperature recorded at Atlanta (CLIATL) for Sep 14, "
        "2026, is less than 92° fahrenheit according to The Weather "
        "Company, then the market resolves to Yes."
    )
    POLY_ATL_DESC = (
        "This market will resolve to the temperature range that contains the "
        "highest temperature recorded by NOAA at the Hartsfield-Jackson "
        "International Airport Station in degrees Fahrenheit on 14 Sep '26. "
        "If NOAA data for the observation date is unavailable ... the Weather "
        "Underground Daily Observations table will be used as the resolution "
        "source."
    )
    POLY_ATL_RESOLUTION_SOURCE = "https://www.weather.gov/wrh/timeseries?site=katl"

    def test_kalshi_weather_extraction(self):
        tags = settlement_source(self.KALSHI_ATL_RULES)
        self.assertEqual(tags, ("weather:weather_company:cliatl",))

    def test_polymarket_weather_extraction_includes_fallback(self):
        tags = settlement_source(self.POLY_ATL_DESC, self.POLY_ATL_RESOLUTION_SOURCE)
        self.assertIn("weather:noaa:katl", tags)
        self.assertIn("weather:wunderground:katl", tags)

    def test_accuweather_mentioned_only_as_non_authoritative_is_not_extracted_from_rules_primary(self):
        # rules_secondary (not passed here) is where AccuWeather/Google
        # Weather appear as non-authoritative comparison sources; the
        # ingestion call in discover.py only ever passes rules_primary.
        tags = settlement_source(self.KALSHI_ATL_RULES)
        self.assertNotIn("weather:accuweather", tags)

    def test_empty_text_yields_no_tags(self):
        self.assertEqual(settlement_source(""), ())
        self.assertEqual(settlement_source("Will the Lakers win the NBA title?"), ())

    def test_crypto_source_extraction(self):
        kalshi_btc = (
            "If the simple average of the sixty seconds of CF Benchmarks' "
            "Bitcoin Real-Time Index (BRTI) before 12 PM EDT is below 67200 "
            "at 12 PM EDT on Sep 14, 2026, then the market resolves to Yes."
        )
        poly_btc = (
            'This market will resolve according to the final "Close" price '
            "of the Binance 1 minute candle for BTC/USDT."
        )
        self.assertEqual(settlement_source(kalshi_btc), ("crypto:cf_benchmarks",))
        self.assertEqual(settlement_source(poly_btc), ("crypto:binance",))

    def test_econ_agency_extraction(self):
        kalshi_cpi = (
            "If the Consumer Price Index for All Urban Consumers: U.S. city "
            "average, All items, not seasonally adjusted, 1982-84=100 for "
            "September 2026 is above 335.400, then the market resolves to Yes."
        )
        poly_cpi = (
            "This market will resolve to the percentage change in the "
            "Consumer Price Index (CPI) over the 12-month period ending in "
            "September 2026 according to the monthly Bureau of Labor "
            "Statistics (BLS) report."
        )
        self.assertEqual(settlement_source(kalshi_cpi), ("econ:bls",))
        self.assertEqual(settlement_source(poly_cpi), ("econ:bls",))

    def test_conflict_weather_company_vs_noaa(self):
        conflict = _settle_src_conflict(
            frozenset(settlement_source(self.KALSHI_ATL_RULES)),
            frozenset(settlement_source(self.POLY_ATL_DESC, self.POLY_ATL_RESOLUTION_SOURCE)),
        )
        self.assertIsNotNone(conflict)
        cls, pa, pb = conflict
        self.assertEqual(cls, "weather")
        self.assertEqual(pa, ["weather_company"])
        self.assertEqual(pb, ["noaa", "wunderground"])

    def test_no_conflict_when_one_side_has_no_tags(self):
        self.assertIsNone(_settle_src_conflict(frozenset({"weather:noaa:katl"}), frozenset()))

    def test_no_conflict_when_providers_overlap(self):
        # A side that names BOTH a provider and its documented fallback is
        # never disjoint against a counterparty using either alone.
        a = frozenset({"weather:noaa:katl", "weather:wunderground:katl"})
        b = frozenset({"weather:wunderground:katl"})
        self.assertIsNone(_settle_src_conflict(a, b))

    def test_no_conflict_across_different_classes(self):
        a = frozenset({"weather:noaa:katl"})
        b = frozenset({"crypto:binance"})
        self.assertIsNone(_settle_src_conflict(a, b))

    def test_atlanta_case_end_to_end_rejected(self):
        p = extract_spec(snap_src(
            "Highest temperature in Atlanta on September 14?",
            settlement_source(self.POLY_ATL_DESC, self.POLY_ATL_RESOLUTION_SOURCE),
        ))
        k = extract_spec(snap_src(
            "Will the maximum temperature be <92° on Sep 14, 2026?",
            settlement_source(self.KALSHI_ATL_RULES),
        ))
        d = match_spec(p, k)
        self.assertFalse(d.match)
        self.assertIn("settle_src_mismatch", d.reasons[0])

    def test_same_provider_both_sides_not_rejected_by_this_gate(self):
        p = extract_spec(snap_src("Bitcoin price on September 14?", ("crypto:binance",)))
        k = extract_spec(snap_src("BTC above $67,200 at noon?", ("crypto:binance",)))
        d = match_spec(p, k)
        self.assertNotIn("settle_src_mismatch", " ".join(d.reasons) if not d.match else "")


class ActorScopedSubjectTests(unittest.TestCase):
    """Unit tests for the iteration-5 actor-scoped subject discriminator:
    "Will Trump's first endorsement ... be the 2028 GOP nominee?" is a bet on
    Trump's own not-yet-made endorsement, not on any specific named
    candidate, even against a market for the very same race."""

    def test_is_first_endorsement_market(self):
        self.assertTrue(_is_first_endorsement_market(
            "Will Trump's first endorsement before the primaries be the 2028 GOP nominee?"))
        self.assertFalse(_is_first_endorsement_market(
            "Will Donald J. Trump Jr. win the 2028 Iowa Republican caucus?"))

    def test_endorsement_proxy_vs_named_candidate_rejected(self):
        # Rejected (possibly by this gate, possibly by an earlier one — e.g.
        # the action-mismatch gate on "nomination" vs "win" — either way the
        # pair must not be endorsed); see the isolated gate test below for
        # proof this discriminator specifically fires.
        d = decide_full(
            "Will Trump's first endorsement before the primaries be the 2028 GOP nominee?", "",
            "Will Donald J. Trump Jr. win the 2028 Iowa Republican caucus?", "",
        )
        self.assertFalse(d.match)

    def test_endorsement_proxy_vs_named_candidate_gate_fires_in_isolation(self):
        d = decide_full(
            "Will Trump's first endorsement before the primaries be the 2028 GOP nominee?", "",
            "Will Donald J. Trump Jr. be the 2028 Republican nominee?", "",
        )
        self.assertFalse(d.match)
        self.assertIn("actor-scoped subject mismatch", d.reasons[0])

    def test_endorsement_proxy_vs_family_member_nominee_rejected(self):
        d = decide_full(
            "Will Trump's first endorsement before the primaries be the 2028 GOP nominee?", "",
            "Will a Trump family member be the 2028 Republican nominee?", "",
        )
        self.assertFalse(d.match)
        self.assertIn("actor-scoped subject mismatch", d.reasons[0])

    def test_both_endorsement_markets_not_rejected_by_this_gate(self):
        d = decide_full(
            "Will Trump's first endorsement before the primaries be the 2028 GOP nominee?", "",
            "Will Trump's first endorsement be the 2028 GOP nominee?", "",
        )
        self.assertNotIn("actor-scoped subject mismatch", " ".join(d.reasons))

    def test_two_named_candidate_markets_not_rejected_by_this_gate(self):
        d = decide_full(
            "Donald Trump Jr.", "2028 Iowa Republican caucus",
            "Will Donald J. Trump Jr. win the 2028 Iowa Republican caucus?", "",
        )
        self.assertNotIn("actor-scoped subject mismatch", " ".join(d.reasons))


if __name__ == "__main__":
    unittest.main()

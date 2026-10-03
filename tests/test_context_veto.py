"""Event-context vetoes (matcher.context_veto): one test per observed
high-edge false-positive class from the 2026-09-18 full-catalog run, plus
true pairs that must survive."""
from __future__ import annotations

import unittest

from matcher import _event_titles_agree, _tokens, context_veto, is_compatible_match
from pipeline import MarketSnapshot, OrderBook


def pm(label, event):
    return MarketSnapshot("polymarket", "p", "pe", label, "open", None, "",
                          OrderBook(bids=[], asks=[]), extra={"event_title": event})


def ks(title, event, sub=""):
    return MarketSnapshot("kalshi", "K", "KE", title, "active", None, "",
                          OrderBook(bids=[], asks=[]),
                          extra={"event_title": event, "yes_sub_title": sub})


class Rejects(unittest.TestCase):
    def check(self, p, k, reason_part):
        reason = context_veto(p, k)
        self.assertIsNotNone(reason)
        self.assertIn(reason_part, reason)
        self.assertFalse(is_compatible_match(p, k))

    def test_season_leader_vs_single_game_prop(self):
        self.check(pm("José Ramírez", "MLB: Stolen Bases Leader"),
                   ks("José Ramírez: 1+ stolen bases?", "A's vs Cleveland: Stolen Bases"),
                   "single-game")

    def test_run_for_vs_nominee(self):
        self.check(pm("Phil Murphy", "Democratic Presidential Nominee 2028"),
                   ks("Who will run for the Democratic presidential nomination in 2028? Phil Murphy",
                      "Who will run for the Democratic presidential nomination in 2028?", "Phil Murphy"),
                   "candidacy")

    def test_county_vs_statewide(self):
        self.check(pm("Abdul El-Sayed (D)", "Michigan Senate Election Winner"),
                   ks("Will Abdul El-Sayed win Kent County?",
                      "Michigan Senate: which counties will Abdul El-Sayed win?"),
                   "county")

    def test_division_vs_conference(self):
        self.check(pm("Vegas Golden Knights", "NHL: 2027 Western Conference Champion"),
                   ks("Will the Vegas Golden Knights win the Pacific Division?", "NHL Pacific Division Winner"),
                   "division")

    def test_price_race_vs_single_level(self):
        self.check(pm("by December 31, 2026", "When will Bitcoin hit $100k?"),
                   ks("Will BTC hit $50,000 before $100,000 by Dec 31, 2026?",
                      "Will BTC hit $50,000 before $100,000?", "50,000 first"),
                   "price race")

    def test_different_named_entities_sharing_words(self):
        self.check(pm("Reporters Without Borders", "Nobel Peace Prize Winner 2026"),
                   ks("Who will win the Nobel Peace Prize? Doctors Without Borders",
                      "2026 Nobel Peace Prize winner", "Doctors Without Borders (Médecins Sans Frontières)"),
                   "different entities")

    def test_different_central_banks(self):
        self.check(pm("Bank of England rate hike in 2026?", "Bank of England rate hike in 2026?"),
                   ks("Will there be exactly 1 Fed rate hike in 2026?", "Number of rate hikes in 2026?"),
                   "central banks")

    def test_opposite_party_wave(self):
        self.check(pm("Blue wave in 2026?", "Blue wave in 2026?"),
                   ks("Red wave in 2026? Yes", "Red wave in 2026?"), "party wave")

    def test_negated_contract(self):
        self.check(pm("No Atlantic hurricane will form in September 2026",
                      "When will the first hurricane form in the Atlantic in 2026?"),
                   ks("When will the next hurricane form? Before Oct 1, 2026",
                      "When will the next Atlantic hurricane form?"), "negated")

    def test_exact_count_vs_open_ended(self):
        self.check(pm("Another Fed rate hike in 2026?", "Another Fed rate hike in 2026?"),
                   ks("Will there be exactly 1 Fed rate hike in 2026?", "Number of rate hikes in 2026?"),
                   "exact count")

    def test_same_surname_different_first_name(self):
        self.check(pm("Alex Fitzpatrick", "DP World Tour: BMW PGA Championship Top 20"),
                   ks("Matt Fitzpatrick finishes top 20", "BMW PGA Championship: Top 20 Finishers",
                      "Matt Fitzpatrick"), "different entities")

    def test_different_finishing_position(self):
        self.check(pm("Troyes", "Ligue 1: 3rd Place Finish 2026-27"),
                   ks("Will Troyes finish in last place in the Ligue 1 2026-27 season?",
                      "Ligue 1 last place", "Troyes"), "finishing position")

    def test_different_year(self):
        self.check(pm("5.00-5.49%", "Brazil Annual Inflation 2026"),
                   ks("Will inflation in Brazil be above 5.00% in Dec 2025?", "Brazil inflation Dec 2025"),
                   "year")

    def test_runner_up_vs_winner(self):
        self.check(pm("Chongqing Tonglianglong", "Chinese Super League: 2026 Runner-Up"),
                   ks("Will Chongqing Tonglianglong FC win the Chinese Super League?",
                      "Chinese Super League winner", "Chongqing Tonglianglong FC"), "runner-up")

    def test_league_phase_vs_overall(self):
        self.check(pm("Inter Milan", "UEFA Champions League: League Phase Winner"),
                   ks("Will Inter win the Champions League?", "Champions League Winner", "Inter"), "stage")

    def test_different_bps_rung(self):
        self.check(pm("50+ bps decrease", "Reserve Bank of Australia Decision in September?"),
                   ks("Will the Reserve Bank of Australia Cut 1-25bps at the September meeting?",
                      "RBA decision in September"), "basis-point")


    def test_different_stat_line_values(self):
        self.check(pm("Cameron Ward", "Pro Football: 2026-27 400+ Passing Yards"),
                   ks("Will Cam Ward record 3500+ passing yards during 2026-27?",
                      "Pro Football: 3,500+ passing yards", "Cam Ward"), "stat line")

    def test_reach_a_stage_vs_win_it(self):
        self.check(pm("Utah", "NCAA Football: Team to Make National Championship"),
                   ks("Will Utah win the College Football Playoff National Championship?",
                      "College Football National Champion", "Utah"), "reach a stage")

    def test_top_n_finish_vs_winning(self):
        self.check(pm("Crystal Palace", "English Premier League Top 5 Finishers"),
                   ks("Will Crystal Palace win the English Premier League?", "EPL Winner", "Crystal Palace"),
                   "finishing scope")

    def test_different_playoff_seed(self):
        self.check(pm("Denver Broncos", "Pro Football: 2026-27 AFC #1 Seed"),
                   ks("Will Denver be the #2 seed in the American Football Conference?",
                      "AFC #2 seed", "Denver"), "playoff seed")

    def test_vote_share_threshold_vs_winning(self):
        self.check(pm("Jordan Herrera (D)", "MO-04 House Election Winner"),
                   ks("Will Jordan Herrera receive at least 42% of the popular vote in MO-04?",
                      "MO-04 vote share", "Jordan Herrera"), "vote-share")

    def test_different_district(self):
        self.check(pm("Democratic Party", "CA-04 House Election Winner"),
                   ks("Will Democratic win the House race for MO-04? Democratic",
                      "MO-04 House winner?", "Democratic"), "district")


    def test_different_stat_category(self):
        self.check(pm("Jaylen Waddle", "Pro Football: 2026-27 Week 2 Receiving Yards Leader"),
                   ks("Jaylen Waddle: Leader in fantasy points for Week 2?",
                      "Pro Football Week 2 fantasy points leader", "Jaylen Waddle"), "stat category")

    def test_week_scope_vs_season(self):
        self.check(pm("Blake Corum", "Fantasy Football: 2026-27 RB Points Leader"),
                   ks("Blake Corum: Leader in fantasy points for Week 2?",
                      "Fantasy Football Week 2 RB leader", "Blake Corum"), "week")

    def test_size_conditional_vs_plain(self):
        self.check(pm("OpenAI $1t+ IPO before 2027?", "OpenAI $1t+ IPO before 2027?"),
                   ks("When will OpenAI IPO? Before Jan 1, 2027", "When will OpenAI IPO?"), "size-conditional")

    def test_rank_one_vs_top_five(self):
        self.check(pm("Timothée Chalamet", "#1 Searched Person on Google in 2026"),
                   ks("Will Timothée Chalamet be in the Top 5 rank on Google's Year in Search 2026?",
                      "Top 5 most searched people", "Timothée Chalamet"), "finishing scope")


class PeriodYears(unittest.TestCase):
    def test_targets_and_deadlines_are_separate(self):
        from matcher import _period_years
        self.assertEqual(_period_years("nfl 2026-27 season")[0], {2026, 2027})
        self.assertEqual(_period_years("in 2026")[0], {2026})
        for t in ("before 2027", "by jan 1, 2027", "before jan 2027"):
            targets, deadlines = _period_years(t)
            self.assertEqual((targets, deadlines), (frozenset(), {2026}), t)

    def test_deadline_does_not_conflict_with_target_year(self):
        # "announce before 2027" (deadline) vs "the 2028 nomination" (target)
        self.assertIsNone(context_veto(
            pm("Greg Abbott", "Who will announce Presidential run before 2027?"),
            ks("Who will run for the Republican presidential nomination in 2028? Greg Abbott",
               "Who will run for the 2028 Republican presidential nomination?", "Greg Abbott")))


class SettlementRisk(unittest.TestCase):
    def test_weather_flagged_not_vetoed(self):
        from matcher import settlement_risk
        p = pm("72-73°F", "Highest temperature in Chicago on September 18?")
        k = ks("Will the maximum temperature be 72-73° on Sep 18, 2026?",
               "Highest temperature in Chicago on Sep 18, 2026?")
        self.assertIsNone(context_veto(p, k))
        self.assertIn("weather", settlement_risk(p, k))


class LabelIdentityOverride(unittest.TestCase):
    """An exact outcome-name match outranks the older name/jurisdiction
    heuristics (they exist to separate DIFFERENT contestants)."""

    def test_party_suffix_label_matches_kalshi_sub_title(self):
        self.assertTrue(is_compatible_match(
            pm("Ed Case (D)", "HI-01 House Election Winner"),
            ks("Will Democratic win the House race for HI-01? Ed Case", "HI-01 House winner?", "Ed Case")))

    def test_foreign_jurisdiction_not_vetoed_for_same_named_outcome(self):
        self.assertTrue(is_compatible_match(
            pm("Celina Leão", "Federal District Governor Election Winner"),
            ks("Will Celina Leão win the 2026 Brazilian Federal District governor election?",
               "Brazil Federal District Governor winner?", "Celina Leão")))

    def test_different_contestants_still_vetoed(self):
        self.assertFalse(is_compatible_match(
            pm("Steve Bannon", "Republican Presidential Nominee 2028"),
            ks("Will Tucker Carlson be the nominee for the Presidency for the Republicans?",
               "2028 Republican presidential nominee", "Tucker Carlson")))


class SuccessionHorizon(unittest.TestCase):
    """Kalshi gives open-ended "next X" markets a formal far-out expiry (end of
    term); Polymarket uses a calendar year. Pair them, but flag the horizon."""

    def _pair(self):
        from pipeline import MarketSnapshot, OrderBook
        p = MarketSnapshot("polymarket", "p", "pe", "Pat Adams", "open", "2026-12-31T23:59:00Z", "",
                           OrderBook(bids=[], asks=[]),
                           extra={"event_title": "Who will Trump pick as the next Press Secretary?"})
        k = MarketSnapshot("kalshi", "K", "KE",
                           "Will Pat Adams be the next White House Press Secretary of United States?",
                           "active", "2029-01-21T15:00:00Z", "", OrderBook(bids=[], asks=[]),
                           extra={"event_title": "Who will be Trump's next Press Secretary?",
                                  "yes_sub_title": "Pat Adams"})
        return p, k

    def test_paired_and_flagged(self):
        from matcher import is_close_time_compatible, settlement_risk
        p, k = self._pair()
        self.assertTrue(is_close_time_compatible(p, k))
        self.assertTrue(is_compatible_match(p, k))
        self.assertIn("horizon", settlement_risk(p, k))

    def test_long_gap_without_succession_wording_still_rejected(self):
        from matcher import is_close_time_compatible
        p, k = self._pair()
        p.extra["event_title"] = "Press Secretary approval rating 2026"
        p.title = k.extra["yes_sub_title"] = "Pat Adams approval"
        k.title = "Will Pat Adams approval exceed 50% in 2029?"
        k.extra["event_title"] = "Press Secretary approval 2029"
        self.assertFalse(is_close_time_compatible(p, k))


class StatLeaderWording(unittest.TestCase):
    def test_season_top_qb_is_a_leader_market(self):
        self.assertTrue(is_compatible_match(
            pm("Patrick Mahomes", "Fantasy Football: 2026-27 QB Points Leader"),
            ks("Who will be Fantasy Football: 2026-27 Season Top QB? Patrick Mahomes",
               "Fantasy Football: 2026-27 Season Top QB", "Patrick Mahomes")))

    def test_leader_vs_numeric_threshold_prop_still_rejected(self):
        self.assertFalse(is_compatible_match(
            pm("Josh Jacobs", "Pro Football: 2026-27 Rushing Yards Leader"),
            ks("Will Josh Jacobs record 1250+ rushing yards during 2026-27?",
               "Pro Football: 1,250+ rushing yards", "Josh Jacobs")))

    def test_announce_presidential_run_is_candidacy(self):
        self.assertTrue(is_compatible_match(
            pm("Greg Abbott", "Who will announce Presidential run before 2027?"),
            ks("Who will run for the Republican presidential nomination in 2028? Greg Abbott",
               "Who will run for the 2028 Republican presidential nomination?", "Greg Abbott")))


class Keeps(unittest.TestCase):
    def test_initials_spacing_variant(self):
        self.assertIsNone(context_veto(
            pm("J. J. Spaun", "DP World Tour: BMW PGA Championship Winner"),
            ks("Will J.J. Spaun win the BMW PGA Championship?", "BMW PGA Championship Winner", "J.J. Spaun")))

    def test_first_name_short_forms(self):
        for p_name, k_name in (("Alexander Noren", "Alex Noren"), ("Zach Bauchou", "Zachary Bauchou"),
                               ("David Bronson", "Dave Bronson")):
            self.assertIsNone(context_veto(pm(p_name, "Some Tournament Winner"),
                                           ks(f"Will {k_name} win?", "Some Tournament Winner", k_name)))

    def test_stays_phrasing_ignored(self):
        self.assertIsNone(context_veto(
            pm("Golden State Warriors", "NBA: Steph Curry Next Team"),
            ks("What will be Steph Curry's next team? Stays with Golden State Warriors",
               "Steph Curry's next team?", "Stays with Golden State Warriors")))

    def test_before_jan_next_year_equals_this_year(self):
        self.assertIsNone(context_veto(
            pm("Jesse Pollak", "Which guests will appear on the UpOnly podcast before 2027?"),
            ks("Will Jesse Pollak be on UpOnly Podcast before Jan 2027?", "UpOnly podcast: Guests this year",
               "Jesse Pollak")))

    def test_same_bps_rung(self):
        self.assertIsNone(context_veto(
            pm("25 bps increase", "Bank of England decision in November?"),
            ks("Will the Bank of England Hike 1-25bps at the November meeting?",
               "Bank of England rate decision in November")))

    def test_fed_no_change_is_not_negation(self):
        self.assertIsNone(context_veto(
            pm("No change", "Fed decision in October?"),
            ks("Will the Federal Reserve Hold rates at their October 2026 meeting? Hold",
               "Fed decision in Oct 2026?")))

    def test_house_candidate_with_party_suffix(self):
        self.assertIsNone(context_veto(
            pm("Max Miller (R)", "OH-07 House Election Winner"),
            ks("Will Republican win the House race for OH-07? Max Miller", "OH-07 House winner?", "Max Miller")))

    def test_award_same_event(self):
        self.assertIsNone(context_veto(
            pm("Marvin Harrison Jr.", "Pro Football: 2026-27 AP Offensive Player of the Year Winner"),
            ks("Will Marvin Harrison Jr. win the Offensive Player of the Year?",
               "Offensive Player of the Year Winner?", "Marvin Harrison Jr.")))

    def test_punctuation_and_accent_variants_of_one_name(self):
        self.assertIsNone(context_veto(
            pm("O'Neil Cruz", "MLB: NL Comeback Player of the Year"),
            ks("Will O’Neil Cruz win NL CPOTY?", "NL Comeback Player of the Year Winner?", "O’Neil Cruz")))

    def test_price_level_is_not_a_size_qualifier(self):
        from matcher import is_arb_eligible
        p = pm("Will Bitcoin reach above $150,000 in 2026?", "Will Bitcoin reach above $150,000 in 2026?")
        k = ks("Will Bitcoin be above $150k in 2026?", "Will Bitcoin be above $150k in 2026?")
        self.assertIsNone(context_veto(p, k))
        self.assertTrue(is_arb_eligible(p, k))

    def test_same_top_n_finish(self):
        self.assertIsNone(context_veto(
            pm("Everton", "EPL: 2026-27 Top 2 Finishers"),
            ks("Will Everton finish in the top 2 in the 2026-27 EPL season?", "EPL Top 2 Finishers", "Everton")))

    def test_half_point_line_equals_integer_line(self):
        self.assertIsNone(context_veto(
            pm("Toronto Blue Jays", "Will the Toronto Blue Jays win more than 84.5 games in 2026?"),
            ks("Will Toronto win at least 85 games this season? 85+ wins", "Toronto win total")))

    def test_both_single_game(self):
        self.assertIsNone(context_veto(
            pm("Arsenal", "Arsenal vs. Tottenham"),
            ks("Arsenal wins", "Arsenal vs Tottenham", "Arsenal")))

    def test_price_level_not_race(self):
        self.assertIsNone(context_veto(
            pm("$150,000", "What price will Bitcoin hit before 2027?"),
            ks("Will Bitcoin be above $150,000 before 2027?", "Bitcoin price before 2027?")))


class Oct2026FalsePositiveFamilies(unittest.TestCase):
    """Live top-20 audit families: speaker/venue, central-bank granularity,
    tournament round, qualify vs relegated, fantasy category, team predicate."""

    def test_speaker_albertsons_vs_mrbeast_rejected(self):
        p = pm('AI / Artificial Intelligence', 'What will MrBeast say during his next YouTube video?')
        k = ks('What will Albertsons say during their next earnings call? AI / Artificial Intelligence', 'What will Albertsons say during their next earnings call?')
        self.assertIn('different speaker or venue', context_veto(p, k) or '')
        self.assertFalse(is_compatible_match(p, k))

    def test_speaker_mccormick_vs_constellation_rejected(self):
        p = pm('Mexico', 'What will Constellation Brands say during their next earnings call?')
        k = ks('What will McCormick & Company, Incorporated say during their next earnings call? Mexico', 'What will McCormick & Company, Incorporated say during their next earnings call?')
        self.assertIn('different speaker or venue', context_veto(p, k) or '')
        self.assertFalse(is_compatible_match(p, k))

    def test_same_speaker_survives_ok(self):
        p = pm('AI', 'What will Albertsons say during their next earnings call?')
        k = ks('What will Albertsons say during their next earnings call? AI', 'What will Albertsons say during their next earnings call?')
        self.assertIsNone(context_veto(p, k))

    def test_central_bank_meeting_vs_year_rejected(self):
        p = pm('Yes', 'Bank of Canada Rate Hike in 2026?')
        k = ks('Will Bank of Canada Hike rates by >25bps at their December 2026 meeting? Hike >25bps', 'Bank of Canada Hike rates by >25bps at their December 2026 meeting?')
        self.assertIn('specific meeting/size vs period-wide rate question', context_veto(p, k) or '')
        self.assertFalse(is_compatible_match(p, k))

    def test_central_bank_same_meeting_survives_ok(self):
        p = pm('Hike', 'Bank of Canada decision in December 2026?')
        k = ks('Will Bank of Canada hike rates at their December 2026 meeting?', 'Will Bank of Canada hike rates at their December 2026 meeting?')
        self.assertIsNone(context_veto(p, k))

    def test_golf_round3_vs_round2_rejected(self):
        p = pm('Austin Smotherman', 'PGA Tour: Bank of Utah Championship Second Round Leader')
        k = ks('Austin Smotherman leads at the end of Round 3', 'Bank of Utah Championship: Round 3 Leader')
        self.assertIn('different tournament round', context_veto(p, k) or '')
        self.assertFalse(is_compatible_match(p, k))

    def test_golf_round3_vs_third_survives_ok(self):
        p = pm('Austin Smotherman', 'Bank of Utah Championship Third Round Leader')
        k = ks('Austin Smotherman leads at the end of Round 3', 'Bank of Utah Championship: Round 3 Leader')
        self.assertIsNone(context_veto(p, k))

    def test_nations_league_qualify_vs_relegated_rejected(self):
        p = pm('Czechia', 'UEFA Nations League A: Teams relegated (2026-27)')
        k = ks('Will Czechia qualify for the UEFA Nations League Final?', 'UEFA Nations League Final qualifiers')
        self.assertIn('advancement vs relegation', context_veto(p, k) or '')
        self.assertFalse(is_compatible_match(p, k))

    def test_nations_league_relegated_survives_ok(self):
        p = pm('Czechia', 'UEFA Nations League A: Teams relegated (2026-27)')
        k = ks('Will Czechia be relegated from UEFA Nations League A?', 'UEFA Nations League A relegation')
        self.assertIsNone(context_veto(p, k))

    def test_fantasy_rookie_vs_rb_rejected(self):
        p = pm('Jeremiyah Love', 'Fantasy Football: 2026-27 Top 5 Scoring RBs')
        k = ks('Will Jeremiyah Love be a top 5 fantasy rookie in the 2026-27 Pro Football regular season?', 'Top 5 fantasy rookies 2026-27')
        self.assertIn('different fantasy category', context_veto(p, k) or '')
        self.assertFalse(is_compatible_match(p, k))

    def test_fantasy_rookie_vs_flex_rejected(self):
        p = pm('Carnell Tate', 'Fantasy Football: 2026-27 Top 5 Scoring FLEX')
        k = ks('Will Carnell Tate be a top 5 fantasy rookie in the 2026-27 Pro Football regular season?', 'Top 5 fantasy rookies 2026-27')
        self.assertIn('different fantasy category', context_veto(p, k) or '')
        self.assertFalse(is_compatible_match(p, k))

    def test_fantasy_same_category_survives_ok(self):
        p = pm('Jeremiyah Love', 'Fantasy Football: 2026-27 Top 5 Scoring RBs')
        k = ks('Will Jeremiyah Love be a top 5 fantasy RB in the 2026-27 Pro Football regular season?', 'Top 5 fantasy RBs 2026-27')
        self.assertIsNone(context_veto(p, k))

    def test_last_team_to_win_vs_win_totals_rejected(self):
        p = pm('Houston Texans', 'Pro Football: 2026 Regular Season Win Totals')
        k = ks('Will Houston be the last team to win a game in the 2026-27 Pro Football regular season?', 'Last team to win a game 2026-27')
        self.assertIn('different team-season predicate', context_veto(p, k) or '')
        self.assertFalse(is_compatible_match(p, k))

    def test_almere_winner_survives_ok(self):
        p = pm('Almere City FC', 'Almere City FC vs. FC Volendam')
        k = ks('Almere wins', 'Almere City FC vs. FC Volendam')
        self.assertIsNone(context_veto(p, k))

    def test_volendam_spread_survives_ok(self):
        p = pm('FC Volendam (-1.5)', 'Almere City FC vs. FC Volendam - More Markets')
        k = ks('Rkav Volendam wins by more than 1.5 goals?', 'Almere City FC vs. FC Volendam: Spread')
        self.assertIsNone(context_veto(p, k))


class AlertHistoryFalsePositives(unittest.TestCase):
    """2026-09-14 alerter-history false-positive families: match period,
    near-miss organisation, price race, award category, vote share vs place."""

    def _veto(self, p, k):
        self.assertIsNotNone(context_veto(p, k))
        self.assertFalse(is_compatible_match(p, k))

    def test_halftime_vs_margin_rejected(self):
        for line, outcome in (("1.5", "Al-Ittihad Club"), ("2.5", "Al-Shamal")):
            with self.subTest(line=line):
                self._veto(
                    pm(outcome, "Al-Shamal vs. Al-Ittihad Club - Halftime Result"),
                    ks(f"Al-Ittihad wins by more than {line} goals?",
                       "Al-Shamal vs. Al-Ittihad Club: Spread"))

    def test_full_time_and_halftime_survive(self):
        self.assertIsNone(context_veto(
            pm("Al-Ittihad Club", "Al-Shamal vs. Al-Ittihad Club"),
            ks("Al-Ittihad wins?", "Al-Shamal vs. Al-Ittihad Club")))
        self.assertIsNone(context_veto(
            pm("Al-Ittihad Club", "Al-Shamal vs. Al-Ittihad Club - Halftime Result"),
            ks("Al-Ittihad leads at halftime?", "Al-Shamal vs. Al-Ittihad Club: Halftime")))

    def test_near_miss_organisation_rejected(self):
        self._veto(
            pm("Reporters Without Borders", "Nobel Peace Prize Winner 2026"),
            ks("Who will win the Nobel Peace Prize? Doctors Without Borders (Médecins Sans Frontières)",
               "Who will win the Nobel Peace Prize?", "Doctors Without Borders (Médecins Sans Frontières)"))

    def test_same_organisation_and_abbreviations_survive(self):
        self.assertIsNone(context_veto(
            pm("Doctors Without Borders", "Nobel Peace Prize Winner 2026"),
            ks("Who will win the Nobel Peace Prize? Doctors Without Borders",
               "Who will win the Nobel Peace Prize?", "Doctors Without Borders")))
        self.assertIsNone(context_veto(
            pm("Centre Party (C)", "Which parties will be in next Swedish Government?"),
            ks("Will Centre Party be a part of the next government in Sweden?",
               "Which parties will be in next Swedish Government?")))

    def test_price_race_rejected(self):
        self._veto(
            pm("by December 31, 2026", "When will Bitcoin hit $100k?"),
            ks("Will BTC hit $50,000 before $100,000 by Dec 31, 2026? 50,000 first",
               "Will BTC hit $50,000 before $100,000?", "50,000 first"))

    def test_single_threshold_hit_survives(self):
        self.assertIsNone(context_veto(
            pm("by December 31, 2026", "When will Bitcoin hit $100k?"),
            ks("Will BTC hit $100,000 by Dec 31, 2026?", "Will BTC hit $100,000?")))

    def test_game_of_the_year_vs_best_audio_rejected(self):
        self._veto(
            pm("Resident Evil Requiem", "The Game Awards: Best Audio Design"),
            ks("2026 Game of the Year? Resident Evil Requiem", "2026 Game of the Year?",
               "Resident Evil Requiem"))

    def test_game_of_the_year_same_category_survives(self):
        self.assertIsNone(context_veto(
            pm("Resident Evil Requiem", "The Game Awards: Game of the Year"),
            ks("2026 Game of the Year? Resident Evil Requiem", "2026 Game of the Year?",
               "Resident Evil Requiem")))

    def test_vote_share_vs_first_place_rejected(self):
        self._veto(
            pm("Renan Santos", "Brazil Presidential Election First Round: 1st Place"),
            ks("Will Renan Santos receive at least 12% of the popular vote in the first round "
               "of the 2026 Brazilian presidential election?",
               "Brazilian presidential election: Renan Santos vote percent (1st Round)"))

    def test_finish_first_vs_first_place_survives(self):
        self.assertIsNone(context_veto(
            pm("Renan Santos", "Brazil Presidential Election First Round: 1st Place"),
            ks("Will Renan Santos finish first in the first round?",
               "Brazil Presidential Election first round")))

    def test_real_alerted_pairs_survive(self):
        for p, k in (
            (pm("Los Angeles Rams", "Pro Football: 2026-27 Best Regular Season Record"),
             ks("Will Los Angeles R have the best regular season record in the 2026-27 Pro Football season?",
                "Best regular season record 2026-27")),
            (pm("Caribbean Premier League: Antigua And Barbuda Falcons",
                "Caribbean Premier League: Antigua And Barbuda Falcons"),
             ks("Will Antigua And Barbuda Falcons win the 2026 Caribbean Premier League?",
                "2026 Caribbean Premier League winner")),
            (pm("Stefan Krkobabić", "Next Prime Minister of Serbia?"),
             ks("Will Stefan Krkobabić become Prime Minister of Serbia following the next Serbian election?",
                "Next Prime Minister of Serbia?")),
            (pm("Republican", "Which party wins 2028 US Presidential Election?"),
             ks("Will Republican win the Presidency in 2028? Republican party",
                "Which party wins 2028 US Presidential Election?", "Republican party")),
        ):
            self.assertIsNone(context_veto(p, k))


if __name__ == "__main__":
    unittest.main()


class TopOfBookMismatches(unittest.TestCase):
    """The largest apparent edges in a live run are where mismatches hurt most.
    Each case below was a top-16 'arb' on 2026-09-21 before these rules."""

    def check(self, p, k, part):
        r = context_veto(p, k)
        self.assertIsNotNone(r, f"expected a veto, got none for {p.title!r}")
        self.assertIn(part, r)
        self.assertFalse(is_compatible_match(p, k))

    def test_ordinal_place_vs_top_n(self):          # +74c
        self.check(pm("Cruz Azul", "Liga MX: 2026 Apertura 2nd Place Finish"),
                   ks("Will Cruz Azul finish in the top 8 in the Liga MX Apertura?",
                      "Liga MX Apertura Top 8 Finishers", "Cruz Azul"), "finishing scope")

    def test_award_nomination_vs_win(self):         # +18c
        self.check(pm("Spider-Man: Brand New Day", "Oscars 2027: Best Visual Effects Winner"),
                   ks("2027 Best Visual Effects Oscar nominations? Spider-Man",
                      "Oscar nominees: Best Visual Effects", "Spider-Man: Brand New Day"), "nomination")

    def test_different_awards_body(self):           # +19c
        # Two faults: a different body (Oscars vs Golden Globes) AND a different
        # category (Best Actor vs Best Supporting Actor); either reason is right.
        p = pm("Sam Rockwell", "Oscars 2027: Best Actor Nominations")
        k = ks("Will Sam Rockwell be on the list of nominees for Best Supporting Actor?",
               "Golden Globe Nominations: Best Supporting Actor", "Sam Rockwell")
        reason = context_veto(p, k)
        self.assertIsNotNone(reason)
        self.assertTrue("awards body" in reason or "award category" in reason, reason)
        self.assertFalse(is_compatible_match(p, k))

    def test_awards_body_alone(self):
        self.check(pm("Ella Langley", "Grammys 2027: Female Vocalist of the Year"),
                   ks("Will Ella Langley win Female Vocalist of the Year at the CMA Awards?",
                      "CMA Awards: Female Vocalist of the Year", "Ella Langley"), "awards body")

    def test_different_county(self):                # +19c
        self.check(pm("Abdul El-Sayed (D)", "Michigan Senate Election: Kent County Winner"),
                   ks("Will Abdul El-Sayed win Eaton County?",
                      "Michigan Senate: which counties will Abdul El-Sayed win?", "Abdul El-Sayed"),
                   "county")

    def test_division_title_vs_league_championship(self):   # +19c / +17c
        self.check(pm("Vegas Golden Knights", "NHL: 2027 Champion"),
                   ks("Will the Vegas Golden Knights win the Pacific Division?",
                      "NHL Pacific Division Winner", "Vegas Golden Knights"), "overall title")

    def test_second_best_vs_top(self):              # +19c
        # Two independent faults here (rank 2 vs 1, and a week vs a month), so
        # assert the rejection and accept whichever rule reports first.
        p = pm("Moonshot", "Second-Best Chinese AI Company end of September?")
        k = ks("Top Chinese AI Company week of September 21st? Moonshot",
               "Top Chinese AI Company week of September 21st", "Moonshot")
        reason = context_veto(p, k)
        self.assertIsNotNone(reason)
        self.assertTrue("finishing scope" in reason or "period" in reason, reason)
        self.assertFalse(is_compatible_match(p, k))

    def test_ordinal_best_beyond_second(self):
        self.assertEqual(context_veto(
            pm("Z.ai", "Third-Best Chinese AI Company end of September?"),
            ks("Top Chinese AI Company end of September? Z.ai",
               "Top Chinese AI Company end of September", "Z.ai")), "different finishing scope (top-N vs winner)")

    def test_superlative_stat_vs_advancement(self):   # +16c
        self.check(pm("Kansas City Chiefs", "Pro Football: Team to advance to AFC Championship Game"),
                   ks("Will Kansas City be the highest scoring team?",
                      "Pro Football Highest Scoring Team", "Kansas City"), "superlative")

    def test_school_qualifier_mismatch(self):         # +11c
        self.check(pm("Texas A&M", "NCAA Football: Team to Make National Championship"),
                   ks("Will Texas reach the College Football Playoff National Championship?",
                      "College Football National Championship Qualifiers", "Texas"), "school")

    def test_different_legislative_chamber(self):   # +15c
        self.check(pm("PL", "Next Brazil Senate Election: Most Seats Won"),
                   ks("Will PL win the 2026 Brazilian Chamber of Deputies election?",
                      "Brazil Chamber of Deputies Election: Most Seats", "PL"), "chamber")


class TopOfBookKeeps(unittest.TestCase):
    def test_same_award_same_category_still_pairs(self):
        self.assertIsNone(context_veto(
            pm("Ella Langley", "CMA Female Vocalist of the Year 2026"),
            ks("Will Ella Langley win Female Vocalist of the Year at the CMA Awards?",
               "CMA Awards: Female Vocalist of the Year", "Ella Langley")))

    def test_political_nomination_is_not_an_award_nomination(self):
        self.assertIsNone(context_veto(
            pm("Greg Abbott", "Who will announce Presidential run before 2027?"),
            ks("Who will run for the Republican presidential nomination in 2028? Greg Abbott",
               "Who will run for the 2028 Republican presidential nomination?", "Greg Abbott")))


class LiveTopTenRemediation(unittest.TestCase):
    """Focused guards and keep cases for the live top-yield false positives."""

    def test_game_awards_category_mismatch(self):
        p = pm("Hades II", "The Game Awards: Game of the Year")
        k = ks("Will Hades II win Best Audio Design?", "The Game Awards: Best Audio Design", "Hades II")
        self.assertEqual(context_veto(p, k), "different award category")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_game_awards_category_survives(self):
        p = pm("Hades II", "The Game Awards: Best Audio Design")
        k = ks("Will Hades II win Best Audio Design?", "The Game Awards: Best Audio Design", "Hades II")
        self.assertIsNone(context_veto(p, k))

    def test_hurricane_exact_vs_at_least_category(self):
        p = pm("Yes", "Will a Category 2 hurricane make landfall?")
        k = ks("Category 2 or above hurricane landfall?", "Will a Category 2 or above hurricane make landfall?", "Yes")
        self.assertEqual(context_veto(p, k), "hurricane category scope mismatch")

    def test_hurricane_weaker_vs_stronger_category(self):
        p = pm("Yes", "Category 1 hurricane or weaker landfall")
        k = ks("Category 1 or above hurricane landfall?", "Category 1 or above hurricane landfall", "Yes")
        self.assertEqual(context_veto(p, k), "hurricane category scope mismatch")

    def test_same_hurricane_category_semantics_survive(self):
        p = pm("Yes", "Category 3 hurricane landfall")
        k = ks("Cat 3 hurricane landfall?", "Will a Category 3 hurricane make landfall?", "Yes")
        self.assertIsNone(context_veto(p, k))

    def test_relegation_vs_champion_rejected(self):
        p = pm("Shenzhen Peng City", "Chinese Super League: Relegation")
        k = ks("Will Shenzhen Peng City win the Chinese Super League?",
               "Chinese Super League Champion", "Shenzhen Peng City")
        self.assertEqual(context_veto(p, k), "relegation vs champion outcome")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_league_outcome_scope_survives(self):
        self.assertIsNone(context_veto(
            pm("Shenzhen Peng City", "Chinese Super League: Relegation"),
            ks("Will Shenzhen Peng City be relegated from the Chinese Super League?",
               "Chinese Super League Relegation", "Shenzhen Peng City")))

    def test_coalition_composition_vs_party_membership_rejected(self):
        p = pm("Labour–Liberal coalition", "Which coalition will form after the 2026 election?")
        k = ks("Will Labour be part of the next government?",
               "Who will be part of the next government?", "Labour")
        self.assertEqual(context_veto(p, k), "coalition composition vs party membership")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_government_membership_scope_survives(self):
        self.assertIsNone(context_veto(
            pm("Labour", "Which parties will be part of the next government?"),
            ks("Will Labour be part of the next government?",
               "Which parties will be part of the next government?", "Labour")))

    def test_rent_freeze_vs_congestion_pricing(self):
        p = pm("Yes", "Will NYC freeze rents? NYC rent freeze")
        k = ks("Will congestion pricing end?", "NYC congestion pricing", "Yes")
        self.assertEqual(context_veto(p, k), "different policy topic")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_policy_topic_survives(self):
        p = pm("Yes", "Will NYC freeze rents? NYC rent freeze")
        k = ks("Will NYC freeze rents?", "NYC rent freeze policy", "Yes")
        self.assertIsNone(context_veto(p, k))

    def test_rent_freeze_vs_free_buses(self):
        p = pm("Yes", "Will Mamdani freeze NYC rents before 2027?")
        k = ks("Will NYC offer free buses before 2027?", "NYC free buses", "Yes")
        self.assertEqual(context_veto(p, k), "different policy topic")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_fare_free_transit_topic_survives(self):
        p = pm("Yes", "Will NYC offer free buses before 2027?")
        k = ks("Will NYC make buses fare-free before 2027?", "NYC fare-free transit", "Yes")
        self.assertIsNone(context_veto(p, k))

    def test_featured_artist_on_different_work_rejected(self):
        cases = [
            ("Don Toliver", "Will Don Toliver be featured on Qr\u00f6melife?"),
            ("Kodak Black", "Will Kodak Black be featured on Qr\u00f6melife?"),
            ("Sexyy Red", "Will Sexyy Red be featured on Qr\u00f6melife?"),
        ]
        for artist, kalshi_title in cases:
            with self.subTest(artist=artist):
                p = pm(artist, "Who will be featured on GTA VI: The Album?")
                k = ks(kalshi_title, "Who will be featured on Qr\u00f6melife?", artist)
                self.assertEqual(context_veto(p, k), "different featured work")
                self.assertFalse(is_compatible_match(p, k))

    def test_same_featured_work_variants_survive(self):
        p = pm("Don Toliver", "Who will be featured on GTA VI: The Album?")
        k = ks("Will Don Toliver be featured on GTA VI album?",
               "Who will be featured on GTA VI album?", "Don Toliver")
        self.assertIsNone(context_veto(p, k))
        p = pm("Don Toliver", "Who will be featured on Qr\u00f6melife?")
        k = ks("Will Don Toliver be featured on Qromelife?",
               "Who will be featured on Qromelife?", "Don Toliver")
        self.assertIsNone(context_veto(p, k))

    def test_generic_featured_work_side_is_neutral(self):
        p = pm("Don Toliver", "Who will be featured on GTA VI: The Album?")
        k = ks("Will Don Toliver be featured on an album?",
               "Who will be featured on an album?", "Don Toliver")
        self.assertIsNone(context_veto(p, k))

    def test_hole_in_one_vs_tournament_winner_rejected(self):
        p = pm("Presidents Cup 2026 Winner", "Presidents Cup 2026 Winner")
        k = ks("Will there be 1+ holes-in-one at the 2026 Presidents Cup?",
               "Presidents Cup: Hole-in-One")
        self.assertEqual(context_veto(p, k), "hole-in-one occurrence vs tournament winner")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_competition_result_scopes_survive(self):
        self.assertIsNone(context_veto(
            pm("Yes", "Will there be 1+ holes-in-one at the Presidents Cup?"),
            ks("Presidents Cup hole-in-one?", "Presidents Cup: Hole-in-One", "Yes")))
        self.assertIsNone(context_veto(
            pm("Presidents Cup 2026 Winner", "Presidents Cup 2026 Winner"),
            ks("Who will win the Presidents Cup?", "Presidents Cup winner")))

    def test_compound_hole_in_one_winner_text_is_neutral(self):
        p = pm("Yes", "Will the Presidents Cup winner record a hole-in-one?")
        k = ks("Will the Presidents Cup winner record a hole-in-one?",
               "Presidents Cup winner hole-in-one", "Yes")
        self.assertIsNone(context_veto(p, k))

    def test_tournament_participation_vs_winner_rejected(self):
        p = pm("Presidents Cup 2026 Winner", "Presidents Cup 2026 Winner")
        k = ks("J.J. Spaun to compete in the Presidents Cup in 2026?",
               "Golfers to compete in the Presidents Cup this year", "J.J. Spaun")
        self.assertEqual(context_veto(p, k), "tournament participation vs winner")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_tournament_participation_scope_survives(self):
        p = pm("J.J. Spaun", "Golfers to compete in the Presidents Cup this year")
        k = ks("J.J. Spaun to compete in the Presidents Cup in 2026?",
               "Golfers to compete in the Presidents Cup this year", "J.J. Spaun")
        self.assertIsNone(context_veto(p, k))

    def test_party_contest_election_vs_person_winner_rejected(self):
        # Ranked-audit rank 10: Kalshi's "a party founded by Elon Musk" is
        # both a different predicate (contesting vs winning) and a different
        # subject (the party, not Musk) from Polymarket's "Elon Musk" winner
        # outcome. The predicate mismatch alone is enough to veto here.
        p = pm("Elon Musk", "Presidential Election Winner 2028")
        k = ks("Will a party founded by Elon Musk contest the 2028 U.S. presidential election?",
               "Will the America Party contest the 2028 U.S. presidential election?")
        self.assertEqual(context_veto(p, k), "tournament participation vs winner")
        self.assertFalse(is_compatible_match(p, k))

    def test_founded_by_party_vs_person_same_predicate_rejected(self):
        # Same predicate (winning) on both sides, but the Kalshi subject is
        # "a party founded by Elon Musk", not Musk himself.
        p = pm("Elon Musk", "Presidential Election Winner 2028")
        k = ks("Will a party founded by Elon Musk win the 2028 U.S. presidential election?",
               "Will the America Party win the 2028 U.S. presidential election?")
        self.assertEqual(context_veto(p, k), "organization founded by person vs person")
        self.assertFalse(is_compatible_match(p, k))

    def test_person_winner_survives(self):
        self.assertIsNone(context_veto(
            pm("Elon Musk", "Presidential Election Winner 2028"),
            ks("Will Elon Musk win the 2028 US presidential election?",
               "2028 US Presidential Election Winner", "Elon Musk")))

    def test_party_contest_vs_field_candidate_survives(self):
        self.assertIsNone(context_veto(
            pm("Yes", "Will the America Party contest the 2028 election?"),
            ks("Will the America Party field a 2028 presidential candidate?",
               "Will the America Party field a 2028 presidential candidate?", "Yes")))

    def test_eurovision_song_contest_not_participant(self):
        from semantic_scope import competition_result_scope
        self.assertEqual(
            competition_result_scope("Eurovision Song Contest winner 2026"), "winner")
        self.assertEqual(
            competition_result_scope("hot dog eating contest champion"), "winner")

    def test_steel_bridge_vs_ncaa_football_rejected(self):
        p = pm("Notre Dame", "Steel Bridge National Championship Winner")
        k = ks("Will Notre Dame Fighting Irish win the 2027 National Champion?",
               "NCAA Football: 2027 National Champion", "Notre Dame Fighting Irish")
        self.assertEqual(context_veto(p, k), "competition identity mismatch")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_competition_identity_survives(self):
        p = pm("Notre Dame", "College Football Playoff National Champion: Notre Dame")
        k = ks("Will Notre Dame win the NCAA Football national championship?",
               "College Football Playoff National Champion", "Notre Dame")
        self.assertIsNone(context_veto(p, k))

    def test_competition_identity_neutral_when_one_side_unidentified(self):
        p = pm("Notre Dame", "Who will win the championship?")
        k = ks("Will Notre Dame Fighting Irish win the 2027 National Champion?",
               "NCAA Football: 2027 National Champion", "Notre Dame Fighting Irish")
        self.assertIsNone(context_veto(p, k))

    def test_frozen_four_vs_college_world_series_rejected(self):
        p = pm("Texas", "Frozen Four Champion")
        k = ks("Will Texas win the College World Series?",
               "College World Series Champion", "Texas")
        self.assertEqual(context_veto(p, k), "competition identity mismatch")
        self.assertFalse(is_compatible_match(p, k))

    def test_inter_milan_vs_milan_rejected(self):
        p = pm("Inter Milan", "Serie A Top 4 Finishers (2026-27)")
        k = ks("Will Milan finish in the top 4 in the Serie A season?",
               "Serie A Top 4 Finishers", "Milan")
        self.assertEqual(context_veto(p, k), "different club/team")
        self.assertFalse(is_compatible_match(p, k))

    def test_ac_milan_alias_survives(self):
        p = pm("AC Milan", "Serie A Top 4 Finishers (2026-27)")
        k = ks("Will Milan finish in the top 4 in the Serie A season?",
               "Serie A Top 4 Finishers", "Milan")
        self.assertIsNone(context_veto(p, k))

    def test_mvp_vs_platinum_glove_same_player_rejected(self):
        # Same player, different award: Kalshi's AL MVP market vs
        # Polymarket's AL Platinum Glove outcome for Ceddanne Rafaela.
        p = pm("Ceddanne Rafaela", "MLB: AL Platinum Glove Winner")
        k = ks("Will Ceddanne Rafaela win AL MVP?", "MLB: AL MVP")
        self.assertEqual(context_veto(p, k), "different award category")
        self.assertFalse(is_compatible_match(p, k))

    def test_al_mvp_phrasing_variants_survive(self):
        p = pm("Ceddanne Rafaela", "MLB: American League MVP")
        k = ks("Will Ceddanne Rafaela win AL MVP?", "AL MVP winner")
        self.assertIsNone(context_veto(p, k))

    def test_album_vs_record_of_the_year_rejected(self):
        p = pm("Noah Kahan", "Grammys 2027: Record of the Year Winner")
        k = ks("Will Noah Kahan win Album of the Year?", "Grammys 2027: Album of the Year")
        self.assertEqual(context_veto(p, k), "different award category")
        self.assertFalse(is_compatible_match(p, k))

    def test_grammys_album_of_the_year_phrasing_variants_survive(self):
        p = pm("The Great Divide",
               "Grammys 2027: Album of the Year Winner / The Great Divide - Noah Kahan")
        k = ks("Will The Great Divide win Album of the Year?", "Grammys 2027: Album of the Year")
        self.assertIsNone(context_veto(p, k))

    def test_best_actor_vs_best_supporting_actor_rejected(self):
        p = pm("Timothee Chalamet", "Oscars 2027: Best Supporting Actor Winner")
        k = ks("Will Timothee Chalamet win Best Actor?", "Oscars 2027: Best Actor")
        self.assertEqual(context_veto(p, k), "different award category")
        self.assertFalse(is_compatible_match(p, k))

    def test_single_race_vs_combo_market_rejected(self):
        p = pm("Cindy Holscher (D)", "Kansas Governor Election Winner")
        k = ks("Will Kansas Governor winner be Democratic party and Kansas Senate winner be Democratic party? Cindy Holscher and Adam Hamilton win",
               "Kansas Governor-Senate combo", "Democratic/Democratic")
        self.assertEqual(context_veto(p, k), "single race vs combo market")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_combo_market_survives(self):
        p = pm("Democratic/Democratic", "Kansas Governor-Senate combo")
        k = ks("Will Kansas Governor winner be Democratic party and Kansas Senate winner be Democratic party?",
               "Kansas Governor-Senate combo", "Democratic/Democratic")
        self.assertIsNone(context_veto(p, k))

    def test_party_alliance_vs_member_party_rejected(self):
        p = pm("RZP-Zehut", "Which parties will be in the next Israeli government?")
        k = ks("Will Zehut be a part of the next government in Israel?",
               "Who will be a part of the next government of Israel?", "Zehut")
        self.assertEqual(context_veto(p, k), "party alliance vs member party")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_party_list_scope_survives(self):
        self.assertIsNone(context_veto(
            pm("RZP-Zehut", "Which parties will be in the next Israeli government?"),
            ks("Will RZP-Zehut be a part of the next government in Israel?",
               "Who will be a part of the next government of Israel?", "RZP-Zehut")))
        self.assertIsNone(context_veto(
            pm("Zehut", "Which parties will be in the next Israeli government?"),
            ks("Will Zehut be a part of the next government in Israel?",
               "Who will be a part of the next government of Israel?", "Zehut")))

    def test_f1_retirement_vs_next_team_rejected(self):
        p = pm("Will Max Verstappen retire from F1 in 2026?",
               "Will Max Verstappen retire from F1 in 2026?")
        k = ks("What will be Max Verstappen's next F1 team? Alpine",
               "Max Verstappen's Next F1 Team", "Alpine")
        self.assertEqual(context_veto(p, k), "F1 retirement vs next team")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_f1_next_team_scope_survives(self):
        p = pm("Alpine", "Max Verstappen's Next F1 Team")
        k = ks("What will be Max Verstappen's next F1 team? Alpine",
               "Max Verstappen's Next F1 Team", "Alpine")
        self.assertIsNone(context_veto(p, k))

    def test_judicial_nomination_vs_becoming_justice_rejected(self):
        p = pm("Aileen Cannon", "Who will the Trump admin next nominate as SCOTUS Justice?")
        k = ks("Will Aileen Cannon become the next Justice on the Supreme Court?",
               "Who will be the next Supreme Court justice?", "Aileen Cannon")
        self.assertEqual(context_veto(p, k), "judicial nomination vs becoming justice")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_judicial_nomination_stage_survives(self):
        p = pm("Aileen Cannon", "Who will the Trump admin next nominate as SCOTUS Justice?")
        k = ks("Will Trump nominate Aileen Cannon as Supreme Court justice?",
               "Who will Trump next nominate to the Supreme Court?", "Aileen Cannon")
        self.assertIsNone(context_veto(p, k))

    def test_same_next_justice_stage_survives(self):
        p = pm("Aileen Cannon", "Will Aileen Cannon become the next Justice on the Supreme Court?")
        k = ks("Will Aileen Cannon be the next Supreme Court justice?",
               "Who will be the next Supreme Court justice?", "Aileen Cannon")
        self.assertIsNone(context_veto(p, k))

    def test_non_supreme_court_nomination_context_is_neutral(self):
        p = pm("Yes", "Will Alice be the justice nominee?")
        k = ks("Will Alice become the next justice?", "Next justice", "Yes")
        self.assertIsNone(context_veto(p, k))

    def test_mixed_judicial_stage_text_is_neutral(self):
        p = pm("Aileen Cannon", "Will Aileen Cannon be nominated and become the next Supreme Court justice?")
        k = ks("Will Aileen Cannon become the next Justice on the Supreme Court?",
               "Who will be the next Supreme Court justice?", "Aileen Cannon")
        self.assertIsNone(context_veto(p, k))

    def test_truth_social_different_explicit_post_windows(self):
        p = pm("120-139", "Donald Trump Truth Social posts September 11 - September 18, 2026?")
        k = ks("Will Donald Trump make 120-139 Truth Social posts Sep 13-19, 2026?",
               "Trump Truth Social posts Sep 13-19, 2026", "120-139")
        self.assertEqual(context_veto(p, k), "different Truth Social post-count window")

    def test_truth_social_same_explicit_post_window_survives(self):
        p = pm("120-139", "Donald Trump Truth Social posts September 11 - September 18, 2026?")
        k = ks("Will Donald Trump make 120-139 Truth Social posts 9/11-9/18, 2026?",
               "Trump Truth Social posts 9/11-9/18, 2026", "120-139")
        self.assertIsNone(context_veto(p, k))

    def test_fewest_points_allowed_vs_postseason(self):
        p = pm("Kansas City Chiefs", "NFL: Fewest Points Allowed in 2026")
        k = ks("Will the Kansas City Chiefs make the postseason?", "NFL: Team to Make Postseason", "Kansas City Chiefs")
        self.assertEqual(context_veto(p, k), "superlative stat vs advancement/win")
        self.assertFalse(is_compatible_match(p, k))

    def test_two_fewest_points_allowed_wordings_survive(self):
        self.assertIsNone(context_veto(
            pm("Kansas City Chiefs", "NFL: Fewest Points Allowed in 2026"),
            ks("Will the Kansas City Chiefs allow the fewest points?", "NFL: Least Points Allowed in 2026", "Kansas City Chiefs")))

    def test_expanded_rate_vocabulary_normalizes_both_directions(self):
        self.assertEqual(
            _tokens("Bank of Canada raises its benchmark interest rates in October"),
            _tokens("Bank of Canada rate hike in October"),
        )
        self.assertEqual(
            _tokens("Bank of Canada cuts its federal funds rate in October"),
            _tokens("Bank of Canada rate cut in October"),
        )
        self.assertNotEqual(
            _tokens("Bank of Canada raises rates in October"),
            _tokens("Bank of Canada cuts rates in October"),
        )


class TopOfBookMismatchesRound2(unittest.TestCase):
    """Second batch, from the top of the live list after round 1."""

    def check(self, p, k, part):
        r = context_veto(p, k)
        self.assertIsNotNone(r, f"expected a veto for {p.title!r}")
        self.assertIn(part, r)
        self.assertFalse(is_compatible_match(p, k))

    def test_exit_poll_vs_election_result(self):       # +21c
        self.check(pm("Democratic Party", "Which party will hold more governorships after the midterms?"),
                   ks("Will Democrats win independents in the exit poll? Democratic",
                      "Midterms: which party wins independents?", "Democratic"), "exit poll")

    def test_matchup_vs_single_team_advancement(self):  # +14c
        self.check(pm("New York Giants", "Pro Football: Team to advance to NFC Championship Game"),
                   ks("2026-27 Championship Game Matchup: New York J vs Los Angeles",
                      "Pro Football Championship Game Matchup", "New York J"), "matchup")

    def test_victory_label_is_first_place(self):        # +10c
        self.check(pm("Renan Santos Victory", "Brazil Presidential Election First Round Winner"),
                   ks("Will Renan Santos finish 4th in the first round?",
                      "Brazil presidential election: 4th place (1st round)", "Renan Santos"),
                   "finishing")

    def test_womens_vs_mens_competition(self):          # +9c
        self.check(pm("Arsenal", "UEFA Women's Champions League 2026-27 Winner"),
                   ks("Will Arsenal win the Champions League?", "Champions League Winner", "Arsenal"),
                   "women's")

    def test_day_vs_month_period(self):                 # +9c
        self.check(pm("claude-fable-5.1-max", "Best AI model on September 21?"),
                   ks("What will be the top AI model this month? claude-fable-5.1-max",
                      "Top AI model in September?", "claude-fable-5.1-max"), "period")


class ThreeToFiveCentBand(unittest.TestCase):
    """The alerter emails above 3c, so this band decides what actually reaches
    the operator. Each case was a live 3-5c 'arb' on 2026-09-21."""

    def check(self, p, k, part):
        r = context_veto(p, k)
        self.assertIsNotNone(r, f"expected a veto for {p.title!r}")
        self.assertIn(part, r)
        self.assertFalse(is_compatible_match(p, k))

    def test_dismissal_vs_departure(self):            # +5.0c
        self.check(pm("3", "How many more people leave the Trump cabinet in 2026?"),
                   ks("Will Trump fire 3 Cabinet members before 2027?",
                      "How many Cabinet members will Trump sack?"), "dismissal")

    def test_different_award_category(self):          # +4.7c
        self.check(pm("Tua Tagovailoa", "Pro Football: 2026-27 AP Comeback Player of the Year"),
                   ks("Will Tua Tagovailoa win the Offensive Player of the Year?",
                      "Offensive Player of the Year Winner?", "Tua Tagovailoa"), "award category")

    def test_qualified_model_domain(self):            # +4.4c
        self.check(pm("Anthropic", "Which company has best AI model end of 2026?"),
                   ks("Which AI company will have the best coding model on Dec 31, 2026? Anthropic",
                      "Which AI company will have the best coding model", "Anthropic"), "model domain")

    def test_assists_vs_goals(self):                  # +4.2c
        self.check(pm("Lucas Ocampos", "Liga MX: 2026-27 Apertura Most Assists"),
                   ks("Will Lucas Ocampos lead Liga MX in goals for the Apertura?",
                      "Liga MX Apertura Golden Boot", "Lucas Ocampos"), "stat category")


class Pass11Precision(unittest.TestCase):
    """Focused regressions for the latest high-edge precision classes."""

    def test_bare_name_with_one_sided_rank_is_rejected(self):
        p = pm("Taylor Swift", "Spotify: #2 most streamed artist in 2025")
        k = ks("Will Taylor Swift be the 1st overall pick in the 2025 NFL draft?",
               "2025 NFL Draft: 1st overall pick", "Taylor Swift")
        self.assertEqual(context_veto(p, k), "rank contract matched on a bare name")

    def test_same_rank_bare_name_pair_is_kept(self):
        p = pm("Taylor Swift", "Spotify: #2 most streamed artist in 2025")
        k = ks("Will Taylor Swift rank #2 among Spotify artists in 2025?",
               "Spotify: #2 most streamed artists in 2025", "Taylor Swift")
        self.assertNotEqual(context_veto(p, k), "rank contract matched on a bare name")

    def test_best_of_set_vs_plain_win_is_rejected(self):
        p = pm("Texas", "Which state is the Democrats' best tossup Senate race?")
        k = ks("Will Democrats win Texas?", "Democratic Senate election winner", "Texas")
        self.assertEqual(context_veto(p, k), "superlative (best-of-set) vs plain win")

    def test_award_category_best_is_not_treated_as_superlative(self):
        p = pm("A game", "The Game Awards: Best Audio Design")
        k = ks("Will A game win Best Audio Design?", "The Game Awards: Best Audio Design", "A game")
        self.assertNotEqual(context_veto(p, k), "superlative (best-of-set) vs plain win")

    def test_different_explicit_rounds_rejected_even_without_win_wording(self):
        p = pm("Candidate A", "Brazil election: first round participants")
        k = ks("Second round participants: Candidate A", "Brazil election: second round")
        self.assertEqual(context_veto(p, k), "round scope mismatch")

    def test_same_explicit_round_is_kept(self):
        p = pm("Renan Santos", "Brazil election: first round winner")
        k = ks("Will Renan Santos win the first round?", "Brazil election: 1st round winner", "Renan Santos")
        self.assertNotEqual(context_veto(p, k), "round scope mismatch")

    def test_deadline_vs_within_year_occurrence_is_rejected(self):
        p = pm("Yes", "US recession by end of 2027?")
        k = ks("Will there be a recession in 2027?", "US recession in 2027", "Yes")
        self.assertEqual(context_veto(p, k), "deadline window vs within-year occurrence")

    def test_declaration_year_true_pair_survives_occurrence_gate(self):
        p = pm("Yes", "NBER recession declared by end of 2026?")
        k = ks("Will NBER declare a recession in 2026?", "NBER recession declared in 2026", "Yes")
        self.assertNotEqual(context_veto(p, k), "deadline window vs within-year occurrence")

    def test_event_titles_agree_with_champion_winner_synonym(self):
        self.assertTrue(_event_titles_agree(
            pm("Ecuador", "Ecuador LigaPro Champion 2026"),
            ks("Ecuador wins", "Ecuador LigaPro Serie A: 2026 Winner")))

    def test_missing_event_title_is_neutral(self):
        self.assertTrue(_event_titles_agree(pm("Taylor Swift", ""), ks("Taylor Swift", "Draft")))

    def test_unrelated_event_titles_disagree(self):
        self.assertFalse(_event_titles_agree(
            pm("Taylor Swift", "Spotify most streamed artist"),
            ks("Taylor Swift", "NFL draft first pick")))


def pm_q(label, event, question):
    """Like pm(), but also carries a full_question (as Polymarket does for a
    ladder outcome, where the short label alone doesn't show the "be <value>"
    exact-bucket wording)."""
    return MarketSnapshot("polymarket", "p", "pe", label, "open", None, "",
                          OrderBook(bids=[], asks=[]),
                          extra={"event_title": event, "full_question": question})


class BucketVsThreshold(unittest.TestCase):
    """False positive: an exact-value bucket ("0.2%") on a Polymarket ladder
    is a different contract from a Kalshi open-ended threshold ("Above
    0.2%"), even though they share a strike."""

    def test_cpi_exact_bucket_vs_above_threshold_rejected(self):
        p = pm_q("0.2%", "Core CPI MoM - September 2026",
                  "Will Core CPI MoM be 0.2% in September?")
        k = ks("Will CPI Core rise more than 0.2% in September? Above 0.2%",
               "CPI core in September")
        self.assertEqual(context_veto(p, k), "exact-value bucket vs open-ended threshold")
        self.assertFalse(is_compatible_match(p, k))

    def test_threshold_vs_threshold_survives(self):
        p = pm_q("Above 0.2%", "Core CPI MoM - September 2026",
                  "Core CPI above 0.2%?")
        k = ks("Above 0.2%", "CPI core in September")
        self.assertNotEqual(context_veto(p, k), "exact-value bucket vs open-ended threshold")

    def test_bucket_vs_bucket_survives(self):
        p = pm_q("0.3%", "Core CPI MoM - September 2026",
                  "Will Core CPI MoM be 0.3% in September?")
        k = ks("Will core CPI be exactly 0.3%?", "CPI core in September")
        self.assertNotEqual(context_veto(p, k), "exact-value bucket vs open-ended threshold")

    def test_measles_ladder_edge_threshold_survives(self):
        # Polymarket's ladder-edge rung "↑6k" is itself open-ended (a
        # threshold), not an exact bucket, so it must not be vetoed against
        # Kalshi's open-ended "Above 6000" threshold.
        p = pm_q("↑6k", "Measles cases in U.S. in 2026?",
                  "Will there be at least 6000 measles cases in the U.S. in 2026?")
        k = ks("Will there be more than 6000 measles cases in 2026? Above 6000",
               "Measles cases in 2026")
        self.assertNotEqual(context_veto(p, k), "exact-value bucket vs open-ended threshold")


class ElectionOfficeConflict(unittest.TestCase):
    """False positive: a US House district race is a different office/race
    level from a presidential nominee market, even when the same person's
    name appears on both sides."""

    def test_house_district_vs_presidential_nominee_rejected(self):
        p = pm("Mike Johnson", "Republican Presidential Nominee 2028")
        k = ks("Will Mike Johnson be the Republican nominee for LA-04?",
               "LA-04 Republican nominee?")
        self.assertEqual(context_veto(p, k), "different office/race level")
        self.assertFalse(is_compatible_match(p, k))

    def test_same_district_survives(self):
        p = pm("Mike Johnson", "LA-04 Republican nominee?")
        k = ks("Will Mike Johnson be the Republican nominee for LA-04?",
               "LA-04 Republican nominee?")
        self.assertIsNone(context_veto(p, k))

    def test_presidential_nominee_survives(self):
        p = pm("Mike Johnson", "Republican Presidential Nominee 2028")
        k = ks("Will Mike Johnson be the 2028 Republican presidential nominee?",
               "Republican Presidential Nominee 2028", "Mike Johnson")
        self.assertIsNone(context_veto(p, k))

    def test_unidentified_office_is_neutral(self):
        p = pm("Mike Johnson", "Who will win?")
        k = ks("Will Mike Johnson be the Republican nominee for LA-04?",
               "LA-04 Republican nominee?")
        self.assertIsNone(context_veto(p, k))


class Oct2026AuditTitles(unittest.TestCase):
    """Review-score markets for different works; same award category at a
    different ceremony."""

    def test_different_film_rejected(self):
        p = pm('90+', '"Digger" Rotten Tomatoes Score?')
        k = ks('Clayface Rotten Tomatoes score? Above 90', 'Clayface Rotten Tomatoes score?', 'Above 90')
        self.assertEqual(context_veto(p, k), 'different reviewed work')
        self.assertFalse(is_compatible_match(p, k))

    def test_same_film_survives(self):
        for title, thr in (('Digger', '52'), ('Primetime', '90')):
            p = pm(f'{thr}+', f'"{title}" Rotten Tomatoes Score?')
            k = ks(f'{title} Rotten Tomatoes score? Above {thr}', f'{title} Rotten Tomatoes score?', f'Above {thr}')
            self.assertIsNone(context_veto(p, k))

    def test_different_ceremony_rejected(self):
        p = pm('Jynxzi', 'Esports Awards: Streamer of the Year')
        k = ks('Will Jynxzi win Streamer of the Year at Streamer Awards 2026?', 'Streamer of the Year at Streamer Awards 2026?')
        self.assertEqual(context_veto(p, k), 'different awards ceremony')
        self.assertFalse(is_compatible_match(p, k))

    def test_same_ceremony_survives(self):
        p = pm('Jynxzi', 'The Streamer Awards 2026: Streamer of the Year')
        k = ks('Will Jynxzi win Streamer of the Year at Streamer Awards 2026?', 'Streamer of the Year at Streamer Awards 2026?')
        self.assertIsNone(context_veto(p, k))

    def test_unnamed_ceremony_is_neutral(self):
        p = pm('Resident Evil Requiem', 'The Game Awards: Game of the Year')
        k = ks('2026 Game of the Year? Resident Evil Requiem', '2026 Game of the Year?')
        self.assertIsNone(context_veto(p, k))

    def test_emmys_survives(self):
        p = pm('Last Week Tonight With John Oliver', 'Emmys 2026: Outstanding variety series')
        k = ks('Will Last Week Tonight with John Oliver win Outstanding Variety Series?', 'Outstanding Variety Series')
        self.assertIsNone(context_veto(p, k))

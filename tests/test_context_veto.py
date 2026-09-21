"""Event-context vetoes (matcher.context_veto): one test per observed
high-edge false-positive class from the 2026-09-18 full-catalog run, plus
true pairs that must survive."""
from __future__ import annotations

import unittest

from matcher import context_veto, is_compatible_match
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
        self.check(pm("Sam Rockwell", "Oscars 2027: Best Actor Nominations"),
                   ks("Will Sam Rockwell be on the list of nominees for Best Supporting Actor?",
                      "Golden Globe Nominations: Best Supporting Actor", "Sam Rockwell"), "awards body")

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

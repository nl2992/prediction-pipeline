"""Structured sports-game join (sports_match.py): each safeguard pinned."""
from __future__ import annotations

import unittest

from pipeline import MarketSnapshot, OrderBook
from sports_match import match_sports_games


def k(ticker, sub="", series=None):
    et = ticker.rsplit("-", 1)[0]
    return MarketSnapshot("kalshi", ticker, et, f"{sub} wins", "active", None, "",
                          OrderBook(bids=[], asks=[]),
                          extra={"series_ticker": series or et.split("-")[0], "yes_sub_title": sub})


def p2(slug, outcomes, start, mid="m"):
    """2-way moneyline (team outcomes)."""
    return MarketSnapshot("polymarket", f"{slug}:{mid}", slug, " vs. ".join(outcomes), "open",
                          None, "", OrderBook(bids=[], asks=[]),
                          extra={"sports_market_type": "moneyline", "game_start_time": start,
                                 "market_slug": slug, "outcome_labels": list(outcomes)})


def p3(slug, suffix, title, start):
    """One leg of a 3-way soccer moneyline (YES/NO)."""
    return MarketSnapshot("polymarket", f"{slug}-{suffix}", slug, title, "open", None, "",
                          OrderBook(bids=[], asks=[]),
                          extra={"sports_market_type": "moneyline", "game_start_time": start,
                                 "market_slug": f"{slug}-{suffix}", "outcome_labels": ["Yes", "No"]})


def ids(pairs):
    return sorted((pr.kalshi.market_id, pr.poly.market_id) for pr in pairs)


class TwoWay(unittest.TestCase):
    def test_code_join_pairs_first_slug_team_with_token0(self):
        ks = [k("KXNFLGAME-26SEP14DENKC-DEN", "Denver"), k("KXNFLGAME-26SEP14DENKC-KC", "Kansas City")]
        ps = [p2("nfl-den-kc-2026-09-15", ["Broncos", "Chiefs"], "2026-09-15 00:15:00+00")]
        pairs = match_sports_games(ks, ps)
        self.assertEqual(ids(pairs), [("KXNFLGAME-26SEP14DENKC-DEN", "nfl-den-kc-2026-09-15:m")])
        self.assertEqual(pairs[0].match_source, "sports")
        self.assertEqual(pairs[0].poly.extra["yes_outcome"], "Broncos")

    def test_uses_game_start_time_not_stale_slug_date(self):
        ks = [k("KXMLBGAME-26SEP221305TBNYY-TB", "Tampa Bay"), k("KXMLBGAME-26SEP221305TBNYY-NYY", "New York Y")]
        ps = [p2("mlb-tb-nyy-2026-05-23", ["Tampa Bay Rays", "New York Yankees"], "2026-09-22 17:05:00+00")]
        self.assertEqual(len(match_sports_games(ks, ps)), 1)

    def test_start_time_outside_tolerance_rejected(self):
        ks = [k("KXMLBGAME-26SEP221305TBNYY-TB"), k("KXMLBGAME-26SEP221305TBNYY-NYY")]
        ps = [p2("mlb-tb-nyy-2026-09-23", ["Rays", "Yankees"], "2026-09-23 17:05:00+00")]
        self.assertEqual(match_sports_games(ks, ps), [])

    def test_doubleheader_ambiguity_is_skipped_for_date_only_ticker(self):
        ks = [k("KXMLBGAME-26SEP16SDCOL-SD"), k("KXMLBGAME-26SEP16SDCOL-COL")]
        # Two PM games, same teams, same ET day (the second slug is dated the
        # next day, as PM does for a rescheduled game) — ambiguous without a time.
        ps = [p2("mlb-sd-col-2026-09-16", ["Padres", "Rockies"], "2026-09-16 18:10:00+00", "g1"),
              p2("mlb-sd-col-2026-09-17", ["Padres", "Rockies"], "2026-09-16 23:40:00+00", "g2")]
        self.assertEqual(match_sports_games(ks, ps), [])

    def test_doubleheader_resolved_by_ticker_start_time(self):
        ks = [k("KXMLBGAME-26SEP161940SDCOL-SD"), k("KXMLBGAME-26SEP161940SDCOL-COL")]
        ps = [p2("mlb-sd-col-2026-09-16", ["Padres", "Rockies"], "2026-09-16 18:10:00+00", "g1"),
              p2("mlb-sd-col-2026-09-17", ["Padres", "Rockies"], "2026-09-16 23:40:00+00", "g2")]
        pairs = match_sports_games(ks, ps)
        self.assertEqual([pr.poly.market_id for pr in pairs], ["mlb-sd-col-2026-09-17:g2"])

    def test_league_namespace_majority_vote_drops_cross_league_join(self):
        # Three MLS games join to mls-*; one MLS game's codes collide with an NFL game.
        ks, ps = [], []
        for i, (a, b) in enumerate([("sea", "rsl"), ("lag", "col"), ("atl", "orl")]):
            et = f"KXMLSGAME-26SEP2{i}{a.upper()}{b.upper()}"
            ks += [k(f"{et}-{a.upper()}", a, "KXMLSGAME"), k(f"{et}-{b.upper()}", b, "KXMLSGAME"),
                   k(f"{et}-TIE", "Tie", "KXMLSGAME")]
            slug = f"mls-{a}-{b}-2026-09-2{i}"
            ps += [p3(slug, a, a, f"2026-09-2{i} 20:00:00+00"), p3(slug, "draw", "Draw", f"2026-09-2{i} 20:00:00+00"),
                   p3(slug, b, b, f"2026-09-2{i} 20:00:00+00")]
        et = "KXMLSGAME-26SEP20HOUCIN"
        ks += [k(f"{et}-HOU", "Houston", "KXMLSGAME"), k(f"{et}-CIN", "Cincinnati", "KXMLSGAME"),
               k(f"{et}-TIE", "Tie", "KXMLSGAME")]
        # NFL shape is 2-way so the tie-shape guard already blocks it; use a 3-way
        # foreign-league game to exercise the vote itself.
        slug = "nwsl-hou-cin-2026-09-20"
        ps += [p3(slug, "hou", "Houston Dash", "2026-09-20 20:00:00+00"),
               p3(slug, "draw", "Draw", "2026-09-20 20:00:00+00"),
               p3(slug, "cin", "Cincinnati", "2026-09-20 20:00:00+00")]
        pairs = match_sports_games(ks, ps)
        self.assertFalse(any(pr.poly.event_id.startswith("nwsl") for pr in pairs))
        self.assertEqual(len(pairs), 9)


class ThreeWay(unittest.TestCase):
    def test_soccer_three_legs_map_by_slug_suffix(self):
        et = "KXEPLGAME-26SEP20FULMUN"
        ks = [k(f"{et}-FUL", "Fulham"), k(f"{et}-MUN", "Manchester United"), k(f"{et}-TIE", "Tie")]
        slug = "epl-ful-mun-2026-09-20"
        ps = [p3(slug, "ful", "Fulham FC", "2026-09-20 14:00:00+00"),
              p3(slug, "draw", "Draw (Fulham FC vs. Manchester United FC)", "2026-09-20 14:00:00+00"),
              p3(slug, "mun", "Manchester United FC", "2026-09-20 14:00:00+00")]
        self.assertEqual(ids(match_sports_games(ks, ps)), [
            (f"{et}-FUL", f"{slug}-ful"), (f"{et}-MUN", f"{slug}-mun"), (f"{et}-TIE", f"{slug}-draw")])

    def test_outcome_shape_mismatch_rejected(self):
        # Kalshi 2-way (no tie, e.g. cup incl. extra time) must not pair with PM 3-way.
        et = "KXEFLCUPGAME-26SEP15REABRE"
        ks = [k(f"{et}-REA", "Reading"), k(f"{et}-BRE", "Brentford")]
        slug = "efl-rea-bre-2026-09-15"
        ps = [p3(slug, "rea", "Reading", "2026-09-15 18:45:00+00"),
              p3(slug, "draw", "Draw", "2026-09-15 18:45:00+00"),
              p3(slug, "bre", "Brentford", "2026-09-15 18:45:00+00")]
        self.assertEqual(match_sports_games(ks, ps), [])


class NameFallback(unittest.TestCase):
    def test_codes_differ_names_agree(self):
        ks = [k("KXNFLGAME-26SEP21NYGLAR-NYG", "New York G"), k("KXNFLGAME-26SEP21NYGLAR-LAR", "Los Angeles R")]
        ps = [p2("nfl-nyg-la-2026-09-22", ["New York Giants", "Los Angeles Rams"], "2026-09-22 00:15:00+00")]
        pairs = match_sports_games(ks, ps)
        self.assertEqual(ids(pairs), [("KXNFLGAME-26SEP21NYGLAR-NYG", "nfl-nyg-la-2026-09-22:m")])

    def test_names_ambiguous_rejected(self):
        ks = [k("KXNFLGAME-26SEP21NYJNYG-NYJ", "New York"), k("KXNFLGAME-26SEP21NYJNYG-NYG", "New York")]
        ps = [p2("nfl-jets-giants-2026-09-22", ["New York Jets", "New York Giants"], "2026-09-22 00:15:00+00")]
        self.assertEqual(match_sports_games(ks, ps), [])


if __name__ == "__main__":
    unittest.main()


def kline(ticker, sub, series):
    """Kalshi SPREAD/TOTAL market (its own event, sharing the game key)."""
    et = ticker.rsplit("-", 1)[0]
    return MarketSnapshot("kalshi", ticker, et, sub, "active", None, "",
                          OrderBook(bids=[], asks=[]),
                          extra={"series_ticker": series, "yes_sub_title": sub})


def pline(slug, mslug, title, mtype, outcomes, start):
    return MarketSnapshot("polymarket", mslug, slug, title, "open", None, "",
                          OrderBook(bids=[], asks=[]),
                          extra={"sports_market_type": mtype, "game_start_time": start,
                                 "market_slug": mslug, "outcome_labels": list(outcomes)})


class SpreadsAndTotals(unittest.TestCase):
    """Lines ride on a game key the moneyline join already verified."""

    def _game(self):
        et = "KXNFLGAME-26SEP25ATLGB"
        ks = [k(f"{et}-ATL", "Atlanta", "KXNFLGAME"), k(f"{et}-GB", "GB Packers", "KXNFLGAME")]
        ps = [p2("nfl-atl-gb-2026-09-25", ["Falcons", "Packers"], "2026-09-25 20:00:00+00")]
        return ks, ps

    def test_spread_and_total_pair_on_equal_lines(self):
        ks, ps = self._game()
        ks += [kline("KXNFLSPREAD-26SEP25ATLGB-GB4", "GB Packers wins by over 3.5 points", "KXNFLSPREAD"),
               kline("KXNFLTOTAL-26SEP25ATLGB-3", "Full Game: over 37.5 points scored", "KXNFLTOTAL")]
        ps += [pline("nfl-atl-gb-2026-09-25", "nfl-atl-gb-2026-09-25-spread-home-3pt5",
                     "Spread -3.5", "spreads", ["Packers", "Falcons"], "2026-09-25 20:00:00+00"),
               pline("nfl-atl-gb-2026-09-25", "nfl-atl-gb-2026-09-25-total-37pt5",
                     "Falcons vs. Packers: O/U 37.5", "totals", ["Over", "Under"], "2026-09-25 20:00:00+00")]
        got = {pr.kalshi.market_id: pr.poly.market_id for pr in match_sports_games(ks, ps)}
        self.assertEqual(got.get("KXNFLSPREAD-26SEP25ATLGB-GB4"),
                         "nfl-atl-gb-2026-09-25-spread-home-3pt5")
        self.assertEqual(got.get("KXNFLTOTAL-26SEP25ATLGB-3"),
                         "nfl-atl-gb-2026-09-25-total-37pt5")

    def test_line_value_must_match_exactly(self):
        ks, ps = self._game()
        ks += [kline("KXNFLSPREAD-26SEP25ATLGB-GB4", "GB Packers wins by over 3.5 points", "KXNFLSPREAD")]
        ps += [pline("nfl-atl-gb-2026-09-25", "nfl-atl-gb-2026-09-25-spread-home-6pt5",
                     "Spread -6.5", "spreads", ["Packers", "Falcons"], "2026-09-25 20:00:00+00")]
        self.assertFalse([pr for pr in match_sports_games(ks, ps) if "SPREAD" in pr.kalshi.market_id])

    def test_spread_side_must_be_the_same_team(self):
        ks, ps = self._game()
        ks += [kline("KXNFLSPREAD-26SEP25ATLGB-GB4", "GB Packers wins by over 3.5 points", "KXNFLSPREAD")]
        ps += [pline("nfl-atl-gb-2026-09-25", "nfl-atl-gb-2026-09-25-spread-away-3pt5",
                     "Spread -3.5", "spreads", ["Falcons", "Packers"], "2026-09-25 20:00:00+00")]
        self.assertFalse([pr for pr in match_sports_games(ks, ps) if "SPREAD" in pr.kalshi.market_id])

    def test_first_half_and_team_total_series_are_not_joined(self):
        # KXNFL1HTEAMTOTAL shares the game key but is a different contract.
        ks, ps = self._game()
        ks += [kline("KXNFL1HTEAMTOTAL-26SEP25ATLGB-1", "Full Game: over 37.5 points scored",
                     "KXNFL1HTEAMTOTAL")]
        ps += [pline("nfl-atl-gb-2026-09-25", "nfl-atl-gb-2026-09-25-total-37pt5",
                     "Falcons vs. Packers: O/U 37.5", "totals", ["Over", "Under"], "2026-09-25 20:00:00+00")]
        self.assertFalse([pr for pr in match_sports_games(ks, ps) if "TOTAL" in pr.kalshi.market_id])


class HalfClasses(unittest.TestCase):
    """1H team totals pair on an equal line; soccer half results pair per team;
    football/basketball half WINNERS do not (Kalshi has a tie leg, PM does not)."""

    def _soccer_game(self):
        et = "KXBRASILEIROBGAME-26SEP25ATHBOT"
        ks = [k(f"{et}-ATH", "Athletic Club", "KXBRASILEIROBGAME"),
              k(f"{et}-BOT", "Botafogo", "KXBRASILEIROBGAME"),
              k(f"{et}-TIE", "Tie", "KXBRASILEIROBGAME")]
        slug = "bra2-ath-bot-2026-09-25"
        start = "2026-09-25 22:00:00+00"
        ps = [p3(slug, "ath", "Athletic Club", start),
              p3(slug, "draw", "Draw", start),
              p3(slug, "bot", "Botafogo FC", start)]
        return ks, ps, slug, start

    def test_soccer_halftime_result_pairs_per_team(self):
        ks, ps, slug, start = self._soccer_game()
        ks += [kline("KXBRASILEIROB1H-26SEP25ATHBOT-ATH", "Athletic Club wins 1st Half", "KXBRASILEIROB1H"),
               kline("KXBRASILEIROB1H-26SEP25ATHBOT-TIE", "Tie 1st Half", "KXBRASILEIROB1H")]
        ps += [pline(slug, f"{slug}-halftime-result-home", "Athletic Club",
                     "soccer_halftime_result", ["Yes", "No"], start)]
        got = {pr.kalshi.market_id: pr.poly.market_id for pr in match_sports_games(ks, ps)}
        self.assertEqual(got.get("KXBRASILEIROB1H-26SEP25ATHBOT-ATH"),
                         f"{slug}-halftime-result-home")
        # Kalshi's draw leg must never pair — Polymarket has no halftime-draw market.
        self.assertNotIn("KXBRASILEIROB1H-26SEP25ATHBOT-TIE", got)

    def test_first_half_team_total_pairs_on_equal_line(self):
        et = "KXNFLGAME-26SEP27LARDEN"
        ks = [k(f"{et}-LAR", "Los Angeles R", "KXNFLGAME"), k(f"{et}-DEN", "Denver", "KXNFLGAME")]
        ps = [p2("nfl-lar-den-2026-09-27", ["Rams", "Broncos"], "2026-09-27 20:00:00+00")]
        ks += [kline("KXNFL1HTEAMTOTAL-26SEP27LARDEN-DEN7", "Denver over 6.5 1H points scored",
                     "KXNFL1HTEAMTOTAL")]
        ps += [pline("nfl-lar-den-2026-09-27", "nfl-lar-den-2026-09-27-1h-team-total-den-6pt5",
                     "Broncos 1H O/U 6.5", "first_half_team_totals", ["Over", "Under"],
                     "2026-09-27 20:00:00+00")]
        got = {pr.kalshi.market_id: pr.poly.market_id for pr in match_sports_games(ks, ps)}
        self.assertEqual(got.get("KXNFL1HTEAMTOTAL-26SEP27LARDEN-DEN7"),
                         "nfl-lar-den-2026-09-27-1h-team-total-den-6pt5")

    def test_football_half_winner_not_paired_tie_shape(self):
        # KXNFL1H / KXNCAAF1H / KXWNBA*WINNER all carry a TIE leg; PM's 1H
        # moneyline is 2-way, so a drawn half settles differently.
        et = "KXNFLGAME-26SEP27LARDEN"
        ks = [k(f"{et}-LAR", "Los Angeles R", "KXNFLGAME"), k(f"{et}-DEN", "Denver", "KXNFLGAME")]
        ps = [p2("nfl-lar-den-2026-09-27", ["Rams", "Broncos"], "2026-09-27 20:00:00+00")]
        ks += [kline("KXNFL1H-26SEP27LARDEN-DEN", "Denver wins 1st Half", "KXNFL1H")]
        ps += [pline("nfl-lar-den-2026-09-27", "nfl-lar-den-2026-09-27-1h-moneyline",
                     "1H Moneyline", "first_half_moneyline", ["Broncos", "Rams"],
                     "2026-09-27 20:00:00+00")]
        self.assertFalse([pr for pr in match_sports_games(ks, ps) if "1H-" in pr.kalshi.market_id])

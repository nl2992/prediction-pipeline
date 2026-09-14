"""Tests for discover.py pure helpers (#89)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import discover
from discover import (
    _parse_dt, _is_parlay, _is_parlay_market, _category, _derive_keywords,
    _apply_event_cap, _event_series, _p_snap, _p_snap_from_event, _event_close,
)


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


class EventSeries(unittest.TestCase):
    def test_prefers_series_ticker(self):
        self.assertEqual(_event_series({"series_ticker": "S", "event_ticker": "E-9"}), "S")

    def test_falls_back_to_event_ticker_prefix(self):
        self.assertEqual(_event_series({"event_ticker": "KXFOO-12"}), "KXFOO")

    def test_empty_when_no_keys(self):
        self.assertEqual(_event_series({}), "")


class ApplyEventCap(unittest.TestCase):
    def _ev(self, series, i):
        return {"series_ticker": series, "event_ticker": f"{series}-{i}"}

    def test_none_cap_returns_all(self):
        filt = [self._ev("KXA", 1), self._ev("KXB", 2)]
        self.assertEqual(_apply_event_cap(filt, None), filt)

    def test_under_cap_returns_all(self):
        filt = [self._ev("KXA", 1), self._ev("KXB", 2)]
        self.assertEqual(_apply_event_cap(filt, 10), filt)

    def test_truncates_and_drops_ordinary_beyond_cap(self):
        filt = [self._ev("KXA", 1), self._ev("KXB", 2), self._ev("KXC", 3)]
        kept = [_event_series(e) for e in _apply_event_cap(filt, 2)]
        self.assertEqual(kept, ["KXA", "KXB"])  # ordinary KXC dropped

    def test_always_include_series_retained_beyond_cap(self):
        # KXBILLS sits past the cap but must survive the truncation.
        filt = [self._ev("KXA", 1), self._ev("KXB", 2), self._ev("KXBILLS", 3), self._ev("KXC", 4)]
        kept = [_event_series(e) for e in _apply_event_cap(filt, 2)]
        self.assertEqual(kept, ["KXA", "KXB", "KXBILLS"])  # KXC dropped, KXBILLS kept


class PolymarketSnapshot(unittest.TestCase):
    """Gamma returns outcomePrices/clobTokenIds as JSON strings; the builders
    must parse them and apply the documented fallbacks (#93)."""

    def _market(self, **over):
        m = {
            "outcomePrices": '["0.42", "0.58"]',
            "clobTokenIds": '["tok1", "tok2"]',
            "conditionId": "cond1",
            "endDate": "2027-01-01T00:00:00Z",
            "endDateIso": "2099-01-01",
            "groupItemTitle": "France",
            "question": "Will France win?",
            "active": True,
            "groupSlug": "world-cup",
            "groupTitle": "World Cup",
        }
        m.update(over)
        return m

    def test_parses_json_string_prices_and_tokens(self):
        s = _p_snap(self._market(), "t")
        self.assertAlmostEqual(s.orderbook.bids[0].price, 0.42)
        self.assertEqual(s.extra["clob_token_ids"], ["tok1", "tok2"])

    def test_close_prefers_endDate_over_endDateIso(self):
        self.assertEqual(_p_snap(self._market(), "t").close_time, "2027-01-01T00:00:00Z")

    def test_label_fallback_groupItemTitle_then_question(self):
        self.assertEqual(_p_snap(self._market(), "t").title, "France")
        m = self._market()
        del m["groupItemTitle"]
        self.assertEqual(_p_snap(m, "t").title, "Will France win?")

    def test_malformed_prices_yield_empty_book(self):
        s = _p_snap(self._market(outcomePrices="not json"), "t")
        self.assertEqual(s.orderbook.bids, [])
        self.assertEqual(s.orderbook.asks, [])

    def test_status_from_active_flag(self):
        self.assertEqual(_p_snap(self._market(active=True), "t").status, "open")
        self.assertEqual(_p_snap(self._market(active=False), "t").status, "closed")

    def test_from_event_uses_parent_slug_and_title(self):
        se = _p_snap_from_event(self._market(), "EV TITLE", "ev-slug", "t")
        self.assertEqual(se.event_id, "ev-slug")
        self.assertEqual(se.extra["event_title"], "EV TITLE")


class CatalogCache(unittest.TestCase):
    """TTL round-trip for the catalog cache; a bug here serves stale catalogs
    or silently disables caching (#94)."""

    def setUp(self):
        self._orig = discover._CACHE_DIR
        discover._CACHE_DIR = Path(tempfile.mkdtemp())

    def tearDown(self):
        discover._CACHE_DIR = self._orig

    def test_store_then_load_within_ttl(self):
        discover._cache_store("cat.json", {"x": 1})
        self.assertEqual(discover._cache_load("cat.json", 60), {"x": 1})

    def test_ttl_zero_or_negative_disables(self):
        discover._cache_store("cat.json", {"x": 1})
        self.assertIsNone(discover._cache_load("cat.json", 0))
        self.assertIsNone(discover._cache_load("cat.json", -5))

    def test_missing_file_returns_none(self):
        self.assertIsNone(discover._cache_load("nope.json", 60))

    def test_expired_entry_returns_none(self):
        (discover._CACHE_DIR / "old.json").write_text(
            json.dumps({"fetched_at": time.time() - 100, "data": {"y": 2}}), encoding="utf-8"
        )
        self.assertIsNone(discover._cache_load("old.json", 10))

    def test_malformed_json_returns_none(self):
        (discover._CACHE_DIR / "bad.json").write_text("not json", encoding="utf-8")
        self.assertIsNone(discover._cache_load("bad.json", 60))


class EventClose(unittest.TestCase):
    """Kalshi /events rows carry no close_time; the horizon filter and sort
    depend on deriving it from nested markets."""

    def test_explicit_event_field_wins(self):
        ev = {"close_time": "2026-10-01T00:00:00Z",
              "markets": [{"close_time": "2027-01-01T00:00:00Z"}]}
        self.assertEqual(_event_close(ev), datetime(2026, 10, 1, tzinfo=timezone.utc))

    def test_latest_nested_market_close(self):
        ev = {"markets": [{"close_time": "2026-10-01T00:00:00Z"},
                          {"close_time": "2026-12-01T00:00:00Z"},
                          {"expiration_time": "2026-11-01T00:00:00Z"}]}
        self.assertEqual(_event_close(ev), datetime(2026, 12, 1, tzinfo=timezone.utc))

    def test_unknown_without_markets(self):
        self.assertIsNone(_event_close({"title": "x"}))
        self.assertIsNone(_event_close({"markets": [{"close_time": None}]}))


def _nested_event(ticker, title, close, markets):
    return {"event_ticker": ticker, "series_ticker": ticker.split("-")[0], "title": title,
            "markets": [{"ticker": f"{ticker}-{sfx}", "event_ticker": ticker, "title": t,
                         "status": st, "close_time": close,
                         "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42"}
                        for sfx, t, st in markets]}


class DiscoverNestedIngest(unittest.TestCase):
    """discover() builds Kalshi markets from nested rows — no per-event calls."""

    def _run(self, events, **kw):
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=events) as ge, \
             patch("kalshi.client.KalshiClient.get_all_markets") as gm, \
             patch("polymarket.client.PolymarketClient.search_events", return_value=[]), \
             patch("polymarket.client.PolymarketClient.search_markets", return_value=[]):
            _rows, k_snaps, _p = discover.discover(return_pools=True, **kw)
        return ge, gm, k_snaps

    def test_requests_nested_markets_and_skips_per_event_fetch(self):
        future = "2099-01-01T00:00:00Z"
        ev = _nested_event("KXA-26", "Will A happen?", future,
                           [("Y", "Will A happen?", "active")])
        ge, gm, k_snaps = self._run([ev])
        self.assertTrue(ge.call_args.kwargs.get("with_nested_markets"))
        gm.assert_not_called()
        self.assertEqual([s.market_id for s in k_snaps], ["KXA-26-Y"])
        self.assertEqual(k_snaps[0].extra.get("event_title"), "Will A happen?")

    def test_non_active_nested_markets_dropped(self):
        future = "2099-01-01T00:00:00Z"
        ev = _nested_event("KXB-26", "Fed ladder", future,
                           [("T1", "Above 4%", "active"), ("T2", "Above 5%", "settled"),
                            ("T3", "Above 6%", "closed")])
        _ge, _gm, k_snaps = self._run([ev])
        self.assertEqual([s.market_id for s in k_snaps], ["KXB-26-T1"])

    def test_events_without_markets_key_fall_back_per_event(self):
        ev = {"event_ticker": "KXC-26", "title": "Will C happen?"}
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=[ev]), \
             patch("kalshi.client.KalshiClient.get_all_markets", return_value=[
                 {"ticker": "KXC-26", "event_ticker": "KXC-26", "title": "Will C happen?",
                  "status": "active", "close_time": "2099-01-01T00:00:00Z"}]) as gm, \
             patch("polymarket.client.PolymarketClient.search_events", return_value=[]), \
             patch("polymarket.client.PolymarketClient.search_markets", return_value=[]):
            _rows, k_snaps, _p = discover.discover(return_pools=True)
        gm.assert_called_once()
        self.assertEqual([s.market_id for s in k_snaps], ["KXC-26"])

    def test_horizon_filter_uses_nested_close(self):
        near = _nested_event("KXN-26", "Will N happen?", "2099-01-01T00:00:00Z",
                             [("Y", "Will N happen?", "active")])
        past = _nested_event("KXP-26", "Will P happen?", "2000-01-01T00:00:00Z",
                             [("Y", "Will P happen?", "active")])
        _ge, _gm, k_snaps = self._run([near, past], days=365 * 200)
        self.assertEqual([s.market_id for s in k_snaps], ["KXN-26-Y"])
        _ge, _gm, k_snaps = self._run([near], days=30)
        self.assertEqual(k_snaps, [])


if __name__ == "__main__":
    unittest.main()

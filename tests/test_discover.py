"""Tests for discover.py pure helpers (#89)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import discover
from discover import (
    _parse_dt, _is_parlay, _is_parlay_market, _category, _derive_keywords,
    _apply_event_cap, _event_series, _p_snap, _p_snap_from_event, _event_close,
    _catalog_bid_ask, _catalog_gross_edge, _select_pairs_to_enrich,
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

    def _run(self, events, poly_events=(), **kw):
        kw.setdefault("market_sweep", False)  # no live PM orphan-sweep network calls
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=events) as ge, \
             patch("kalshi.client.KalshiClient.get_all_markets") as gm, \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=list(poly_events)):
            _rows, k_snaps, p_snaps = discover.discover(return_pools=True, **kw)
        return ge, gm, k_snaps, p_snaps

    def test_requests_nested_markets_and_skips_per_event_fetch(self):
        future = "2099-01-01T00:00:00Z"
        ev = _nested_event("KXA-26", "Will A happen?", future,
                           [("Y", "Will A happen?", "active")])
        ge, gm, k_snaps, _p = self._run([ev])
        self.assertTrue(ge.call_args.kwargs.get("with_nested_markets"))
        gm.assert_not_called()
        self.assertEqual([s.market_id for s in k_snaps], ["KXA-26-Y"])
        self.assertEqual(k_snaps[0].extra.get("event_title"), "Will A happen?")

    def test_non_active_nested_markets_dropped(self):
        future = "2099-01-01T00:00:00Z"
        ev = _nested_event("KXB-26", "Fed ladder", future,
                           [("T1", "Above 4%", "active"), ("T2", "Above 5%", "settled"),
                            ("T3", "Above 6%", "closed")])
        _ge, _gm, k_snaps, _p = self._run([ev])
        self.assertEqual([s.market_id for s in k_snaps], ["KXB-26-T1"])

    def test_events_without_markets_key_fall_back_per_event(self):
        ev = {"event_ticker": "KXC-26", "title": "Will C happen?"}
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=[ev]), \
             patch("kalshi.client.KalshiClient.get_all_markets", return_value=[
                 {"ticker": "KXC-26", "event_ticker": "KXC-26", "title": "Will C happen?",
                  "status": "active", "close_time": "2099-01-01T00:00:00Z"}]) as gm, \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=[]):
            _rows, k_snaps, _p = discover.discover(return_pools=True, market_sweep=False)
        gm.assert_called_once()
        self.assertEqual([s.market_id for s in k_snaps], ["KXC-26"])

    def test_horizon_filter_uses_nested_close(self):
        near = _nested_event("KXN-26", "Will N happen?", "2099-01-01T00:00:00Z",
                             [("Y", "Will N happen?", "active")])
        past = _nested_event("KXP-26", "Will P happen?", "2000-01-01T00:00:00Z",
                             [("Y", "Will P happen?", "active")])
        _ge, _gm, k_snaps, _p = self._run([near, past], days=365 * 200)
        self.assertEqual([s.market_id for s in k_snaps], ["KXN-26-Y"])
        _ge, _gm, k_snaps, _p = self._run([near], days=30)
        self.assertEqual(k_snaps, [])


def _poly_market(cid, active=True, closed=False, **over):
    m = {"conditionId": cid, "active": active, "closed": closed,
         "question": f"Q {cid}", "groupItemTitle": f"Q {cid}",
         "outcomePrices": '["0.4", "0.6"]', "clobTokenIds": '["t1", "t2"]',
         "endDate": "2099-01-01T00:00:00Z"}
    m.update(over)
    return m


class DiscoverPolymarketIngest(unittest.TestCase):
    """discover() ingests the full PM catalog (active and not closed), with no
    keyword filter and no legacy /markets offset-search fallback."""

    def _run(self, poly_events, k_events=None, **kw):
        k_events = k_events if k_events is not None else [
            _nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                          [("Y", "Will A happen?", "active")])]
        kw.setdefault("market_sweep", False)
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=k_events), \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=poly_events) as ge:
            _rows, k_snaps, p_snaps = discover.discover(return_pools=True, **kw)
        return ge, k_snaps, p_snaps

    def test_closed_market_excluded(self):
        ev = {"title": "Event", "slug": "ev", "markets": [
            _poly_market("open1", active=True, closed=False),
            _poly_market("closed1", active=True, closed=True),
        ]}
        _ge, _k, p_snaps = self._run([ev])
        self.assertEqual([s.market_id for s in p_snaps], ["open1"])

    def test_inactive_market_excluded(self):
        ev = {"title": "Event", "slug": "ev", "markets": [
            _poly_market("open1", active=True, closed=False),
            _poly_market("inactive1", active=False, closed=False),
        ]}
        _ge, _k, p_snaps = self._run([ev])
        self.assertEqual([s.market_id for s in p_snaps], ["open1"])

    def test_no_keyword_filter_unrelated_event_ingested(self):
        # An event completely unrelated to any Kalshi keyword must still be
        # ingested — discover() no longer keyword-searches Polymarket.
        ev = {"title": "Will the price of cheese exceed $10/lb?", "slug": "cheese",
              "markets": [_poly_market("cheese1")]}
        _ge, _k, p_snaps = self._run([ev])
        self.assertEqual([s.market_id for s in p_snaps], ["cheese1"])

    def test_full_catalog_scan_uses_get_all_events_not_search(self):
        ge, _k, _p = self._run([])
        ge.assert_called_once()
        self.assertEqual(ge.call_args.kwargs.get("closed"), False)


class OrphanSweep(unittest.TestCase):
    """Market-sweep orphans: active&!closed markets whose parent event never
    appeared in the /events/keyset walk (archived/inactive parent)."""

    def test_orphan_added_with_event_title_and_slug(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        # /events/keyset returns nothing embedding "orphan1" — its parent event
        # is archived and therefore absent from the events walk entirely.
        orphan_market = _poly_market("orphan1")
        orphan_market["events"] = [{"title": "Archived Event", "slug": "archived-ev"}]
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=k_events), \
             patch("discover._run_kalshi_market_sweep",
                  return_value={"raw": [], "complete": True, "elapsed": 0.0}), \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=[]), \
             patch("polymarket.client.PolymarketClient.get_all_markets_keyset",
                  return_value=[orphan_market]):
            _rows, _k, p_snaps = discover.discover(return_pools=True, market_sweep=True)
        self.assertEqual([s.market_id for s in p_snaps], ["orphan1"])
        self.assertEqual(p_snaps[0].event_id, "archived-ev")
        self.assertEqual(p_snaps[0].extra.get("event_title"), "Archived Event")

    def test_already_ingested_market_not_duplicated_as_orphan(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        ev = {"title": "Event", "slug": "ev", "markets": [_poly_market("dup1")]}
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=k_events), \
             patch("discover._run_kalshi_market_sweep",
                  return_value={"raw": [], "complete": True, "elapsed": 0.0}), \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=[ev]), \
             patch("polymarket.client.PolymarketClient.get_all_markets_keyset",
                  return_value=[_poly_market("dup1")]):
            _rows, _k, p_snaps = discover.discover(return_pools=True, market_sweep=True)
        self.assertEqual([s.market_id for s in p_snaps], ["dup1"])

    def test_sweep_off_by_default_flag_skips_keyset_call(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=k_events), \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=[]), \
             patch("polymarket.client.PolymarketClient.get_all_markets_keyset") as gmk:
            discover.discover(return_pools=True, market_sweep=False)
        gmk.assert_not_called()


class HeldOutTagging(unittest.TestCase):
    """Stats-only / parlay-format Kalshi markets are ingested (full pool) but
    tagged extra["match_excluded"] and must never reach the matcher."""

    def test_stats_only_and_parlay_held_out_of_matcher(self):
        stats_ev = _nested_event("KXV-26", "What will voter turnout be?",
                                 "2099-01-01T00:00:00Z",
                                 [("Y", "What will voter turnout be?", "active")])
        normal_ev = _nested_event("KXW-26", "Will W happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will W happen?", "active")])
        poly_ev = {"title": "Unrelated PM event", "slug": "unrelated",
                  "markets": [_poly_market("p1")]}
        with patch("kalshi.client.KalshiClient.get_all_events",
                  return_value=[stats_ev, normal_ev]), \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=[poly_ev]), \
             patch("discover._match_groups_then_individual", return_value=[]) as mgi:
            _rows, k_snaps, _p = discover.discover(return_pools=True, market_sweep=False)

        # Full pool (return_pools) keeps BOTH markets.
        ids = {s.market_id for s in k_snaps}
        self.assertEqual(ids, {"KXV-26-Y", "KXW-26-Y"})
        stats_snap = next(s for s in k_snaps if s.market_id == "KXV-26-Y")
        self.assertEqual(stats_snap.extra.get("match_excluded"), "stats_only")

        # The matcher must only ever see the non-held-out snapshot.
        mgi.assert_called_once()
        matcher_input_ids = {s.market_id for s in mgi.call_args.args[0]}
        self.assertEqual(matcher_input_ids, {"KXW-26-Y"})


class CoverageAccounting(unittest.TestCase):
    """coverage dict invariants: every catalog market is ingested XOR excluded."""

    def test_kalshi_default_ingests_all_open_markets(self):
        ev = _nested_event("KXA-26", "Fed ladder", "2099-01-01T00:00:00Z",
                           [("T1", "Above 4%", "active"), ("T2", "Above 5%", "finalized")])
        cov: dict = {}
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=[ev]), \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=[]):
            discover.discover(return_pools=True, market_sweep=False, coverage=cov)
        k = cov["kalshi"]
        self.assertEqual(k["ingested"] + sum(k["excluded"].values()), k["catalog_markets"])
        self.assertEqual(k["ingested"], k["open_markets"])  # defaults exclude nothing
        self.assertEqual(k["excluded"].get("status=finalized"), 1)
        self.assertEqual(discover.last_coverage, cov)

    def test_polymarket_invariant_holds_with_orphans(self):
        k_ev = _nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                             [("Y", "Will A happen?", "active")])
        ev = {"title": "Event", "slug": "ev", "markets": [
            _poly_market("open1"), _poly_market("closed1", closed=True)]}
        orphan = _poly_market("orphan1")
        orphan["events"] = [{"title": "Archived", "slug": "arch"}]
        cov: dict = {}
        with patch("kalshi.client.KalshiClient.get_all_events", return_value=[k_ev]), \
             patch("discover._run_kalshi_market_sweep",
                  return_value={"raw": [], "complete": True, "elapsed": 0.0}), \
             patch("polymarket.client.PolymarketClient.get_all_events", return_value=[ev]), \
             patch("polymarket.client.PolymarketClient.get_all_markets_keyset",
                  return_value=[orphan]):
            discover.discover(return_pools=True, market_sweep=True, coverage=cov)
        p = cov["polymarket"]
        self.assertEqual(p["ingested"] + sum(p["excluded"].values()),
                         p["embedded_markets"] + p["orphans_added"])
        self.assertEqual(p["orphans_added"], 1)


def _orphan_market(ticker, event_ticker, title="Orphan market", close="2099-01-01T00:00:00Z"):
    return {"ticker": ticker, "event_ticker": event_ticker, "title": title,
            "status": "active", "close_time": close,
            "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42"}


class KalshiOrphanSweep(unittest.TestCase):
    """Kalshi orphan sweep: markets whose parent event never appears in ANY
    /events listing (a live /events-vs-/markets inconsistency), recovered by
    walking /markets?status=open&mve_filter=exclude directly (#see
    discover.py module docstring / _run_kalshi_market_sweep).

    ingest_kalshi is called directly (not via discover()) so each test can
    hand a fully-controlled fake KalshiClient and a pre-resolved
    ``_sweep_box`` — this skips starting a background thread entirely (see
    ingest_kalshi: a non-empty ``_sweep_box`` short-circuits ``own_sweep_thread``)
    so no test touches the network or real threading timing.
    """

    def _kc(self, events):
        kc = MagicMock()
        kc.get_all_events.return_value = events
        kc.last_scan_complete = True
        return kc

    def test_standalone_call_starts_its_own_sweep_thread(self):
        # tools/validate_coverage.py calls ingest_kalshi on its own (no
        # _sweep_box / _sweep_thread from discover()), which takes the
        # own-thread path. A function-local `import threading` once shadowed
        # the module import there and crashed with UnboundLocalError.
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        with patch("discover._sweep_cache_state", return_value=(False, False)), \
             patch("discover._run_kalshi_market_sweep",
                   return_value={"raw": [], "complete": True, "elapsed": 0.0}) as sweep:
            cov: dict = {}
            _filtered, k_snaps = discover.ingest_kalshi(
                self._kc(k_events), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage=cov, market_sweep=True)
        sweep.assert_called_once()
        self.assertEqual([s.market_id for s in k_snaps], ["KXA-26-Y"])
        self.assertEqual(cov["sweep"], "fresh")

    def test_orphan_added_with_parent_event_title(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        orphan = _orphan_market("KXORPH-1", "KXORPHEVT")
        sweep_box = {"raw": [orphan], "complete": True, "elapsed": 0.1}
        cov: dict = {}
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT",
                                "title": "Orphan Parent Event",
                                "series_ticker": "KXORPH"}):
            filtered, k_snaps = discover.ingest_kalshi(
                self._kc(k_events), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage=cov, market_sweep=True, _sweep_box=sweep_box)

        orphan_snap = next(s for s in k_snaps if s.market_id == "KXORPH-1")
        self.assertEqual(orphan_snap.extra.get("event_title"), "Orphan Parent Event")
        self.assertEqual(cov["orphans_added"], 1)
        self.assertEqual(cov["sweep"], "fresh")
        # The recovered parent event is exposed via filtered_events so
        # kalshi_series_ticker lookups in discover() keep working.
        self.assertIn("KXORPHEVT", {ev.get("event_ticker") for ev in filtered})

    def test_orphan_not_added_when_ticker_already_ingested(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        # Same ticker as the one already ingested from the nested event walk —
        # must not be double-counted as an orphan.
        dup = _orphan_market("KXA-26-Y", "KXA-26")
        sweep_box = {"raw": [dup], "complete": True, "elapsed": 0.1}
        cov: dict = {}
        with patch("kalshi.client.KalshiClient.get_event") as ge:
            _filtered, k_snaps = discover.ingest_kalshi(
                self._kc(k_events), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage=cov, market_sweep=True, _sweep_box=sweep_box)

        self.assertEqual([s.market_id for s in k_snaps], ["KXA-26-Y"])
        self.assertEqual(cov["orphans_added"], 0)
        ge.assert_not_called()  # event_ticker already processed -> not even a candidate

    def test_orphan_excluded_by_user_category_filter_not_added(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        orphan = _orphan_market("KXORPH-1", "KXORPHEVT", title="Will the Lakers win game 4?")
        sweep_box = {"raw": [orphan], "complete": True, "elapsed": 0.1}
        cov: dict = {}
        # Orphan's parent event title classifies as "sports"; the user asked
        # for "economic" only, so it must be excluded — not silently
        # re-added bypassing the user's own filter choice.
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT",
                                "title": "Will the Lakers win game 4?"}):
            _filtered, k_snaps = discover.ingest_kalshi(
                self._kc(k_events), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                category="economic", coverage=cov, market_sweep=True, _sweep_box=sweep_box)

        self.assertNotIn("KXORPH-1", {s.market_id for s in k_snaps})
        self.assertEqual(cov["orphans_added"], 0)
        self.assertEqual(cov["excluded"].get("orphan_filter_category"), 1)

    def test_sweep_failure_marks_failed_without_affecting_nested_ingestion(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        sweep_box = {"raw": None, "complete": False, "elapsed": 0.05}
        cov: dict = {}
        with patch("kalshi.client.KalshiClient.get_event") as ge:
            _filtered, k_snaps = discover.ingest_kalshi(
                self._kc(k_events), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage=cov, market_sweep=True, _sweep_box=sweep_box)

        self.assertEqual(cov["sweep"], "failed")
        self.assertEqual(cov["orphans_added"], 0)
        self.assertEqual([s.market_id for s in k_snaps], ["KXA-26-Y"])
        ge.assert_not_called()

    def test_sweep_off_skips_orphan_processing(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        cov: dict = {}
        _filtered, k_snaps = discover.ingest_kalshi(
            self._kc(k_events), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            coverage=cov, market_sweep=False)
        self.assertEqual(cov["sweep"], "off")
        self.assertEqual(cov["orphans_added"], 0)
        self.assertEqual([s.market_id for s in k_snaps], ["KXA-26-Y"])

    def test_coverage_invariant_holds_with_orphans(self):
        k_events = [_nested_event("KXA-26", "Will A happen?", "2099-01-01T00:00:00Z",
                                  [("Y", "Will A happen?", "active")])]
        accepted = _orphan_market("KXORPH-1", "KXORPHEVT")
        rejected = _orphan_market("KXORPH-2", "KXORPHEVT2", title="Voter turnout stats")
        sweep_box = {"raw": [accepted, rejected], "complete": True, "elapsed": 0.1}
        cov: dict = {}

        def _fake_get_event(et, with_nested_markets=False):
            # KXORPHEVT2's parent event closed in the past -> orphan_event_closed,
            # even though the sweep saw its market row as "active" (a stale
            # /markets snapshot lagging the event's real close).
            close = "2000-01-01T00:00:00Z" if et == "KXORPHEVT2" else "2099-01-01T00:00:00Z"
            return {"event_ticker": et, "title": "Orphan Parent",
                   "markets": [{"close_time": close}]}

        with patch("kalshi.client.KalshiClient.get_event", side_effect=_fake_get_event):
            discover.ingest_kalshi(
                self._kc(k_events), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage=cov, market_sweep=True, _sweep_box=sweep_box)

        self.assertEqual(cov["orphans_added"], 1)
        self.assertEqual(cov["orphan_scanned"], 2)
        self.assertEqual(cov["excluded"].get("orphan_event_closed"), 1)
        self.assertEqual(
            cov["ingested"] + sum(cov["excluded"].values()),
            cov["catalog_markets"] + cov["orphan_scanned"],
        )


# ---------------------------------------------------------------------------
# Iteration 2 WP-B: catalog-price screen before live books
# ---------------------------------------------------------------------------

class CatalogBidAsk(unittest.TestCase):
    """Gamma's real bestBid/bestAsk, stashed separately from the price_sim mid."""

    def test_parses_floats(self):
        self.assertEqual(_catalog_bid_ask({"bestBid": "0.41", "bestAsk": 0.45}), (0.41, 0.45))

    def test_missing_is_none(self):
        self.assertEqual(_catalog_bid_ask({}), (None, None))

    def test_malformed_is_none(self):
        self.assertEqual(_catalog_bid_ask({"bestBid": "nope", "bestAsk": None}), (None, None))


class PolySnapshotCatalogPrices(unittest.TestCase):
    """_p_snap / _p_snap_from_event must stash Gamma's bestBid/bestAsk in extra
    WITHOUT touching the main orderbook (which stays outcomePrices-as-mid, size
    0.0, so the matcher's price_sim and the alerter's depth filter both keep
    working exactly as before)."""

    def _market(self, **over):
        m = {
            "outcomePrices": '["0.42", "0.58"]', "clobTokenIds": '["tok1", "tok2"]',
            "conditionId": "cond1", "endDate": "2027-01-01T00:00:00Z",
            "groupItemTitle": "France", "question": "Will France win?",
            "active": True, "groupSlug": "world-cup", "groupTitle": "World Cup",
            "bestBid": "0.40", "bestAsk": "0.44",
        }
        m.update(over)
        return m

    def test_p_snap_extra_has_catalog_prices(self):
        s = _p_snap(self._market(), "t")
        self.assertEqual(s.extra["catalog_bid"], 0.40)
        self.assertEqual(s.extra["catalog_ask"], 0.44)
        # Main orderbook is still the outcomePrices mid with zero size.
        self.assertAlmostEqual(s.orderbook.bids[0].price, 0.42)
        self.assertEqual(s.orderbook.bids[0].size, 0.0)
        self.assertEqual(s.orderbook.asks[0].size, 0.0)

    def test_p_snap_from_event_extra_has_catalog_prices(self):
        s = _p_snap_from_event(self._market(), "EV", "ev-slug", "t")
        self.assertEqual(s.extra["catalog_bid"], 0.40)
        self.assertEqual(s.extra["catalog_ask"], 0.44)
        self.assertEqual(s.orderbook.bids[0].size, 0.0)

    def test_missing_gamma_fields_leave_none(self):
        m = self._market()
        del m["bestBid"]
        del m["bestAsk"]
        s = _p_snap_from_event(m, "EV", "ev-slug", "t")
        self.assertIsNone(s.extra["catalog_bid"])
        self.assertIsNone(s.extra["catalog_ask"])


def _pair(k_bid=None, k_ask=None, p_bid=None, p_ask=None, stale=False):
    """Build a minimal fake MatchedPair-like object for the catalog-screen
    helpers, which only ever touch .kalshi.orderbook.best_bid/best_ask and
    .poly.extra["catalog_bid"/"catalog_ask"/"catalog_stale"]."""
    from pipeline import MarketSnapshot, OrderBook, PriceLevel
    from matcher import MatchedPair
    k_ob = OrderBook(
        bids=[PriceLevel(k_bid, 10.0)] if k_bid is not None else [],
        asks=[PriceLevel(k_ask, 10.0)] if k_ask is not None else [],
    )
    kalshi = MarketSnapshot(source="kalshi", market_id="K1", event_id="E1",
                            title="k", status="active", close_time=None,
                            fetched_at="t", orderbook=k_ob)
    poly = MarketSnapshot(source="polymarket", market_id="P1", event_id="e1",
                          title="p", status="open", close_time=None,
                          fetched_at="t", orderbook=OrderBook(bids=[], asks=[]),
                          extra={"catalog_bid": p_bid, "catalog_ask": p_ask,
                                 "catalog_stale": stale})
    return MatchedPair(poly=poly, kalshi=kalshi, title_similarity=1.0,
                       close_delta_hours=0.0, confidence=1.0)


class CatalogGrossEdge(unittest.TestCase):
    """Best catalog-implied gross edge over both arb directions; a missing
    price in EITHER direction is always conservative (needs_fetch=True)."""

    def test_direction_one_edge(self):
        # buy PM YES @ 0.40 ask + buy Kalshi NO @ (1 - 0.50 bid) -> edge = 0.10
        p = _pair(k_bid=0.50, k_ask=0.90, p_bid=0.10, p_ask=0.40)
        edge, needs_fetch = _catalog_gross_edge(p)
        self.assertAlmostEqual(edge, 0.10)
        self.assertFalse(needs_fetch)

    def test_direction_two_can_be_the_best(self):
        # dir1 = kalshi_bid(0.20) - poly_ask(0.90) = -0.70
        # dir2 = poly_bid(0.85) - kalshi_ask(0.30) = 0.55  <- best
        p = _pair(k_bid=0.20, k_ask=0.30, p_bid=0.85, p_ask=0.90)
        edge, needs_fetch = _catalog_gross_edge(p)
        self.assertAlmostEqual(edge, 0.55)
        self.assertFalse(needs_fetch)

    def test_missing_kalshi_price_forces_fetch(self):
        p = _pair(k_bid=None, k_ask=None, p_bid=0.10, p_ask=0.40)
        edge, needs_fetch = _catalog_gross_edge(p)
        self.assertTrue(needs_fetch)

    def test_missing_poly_price_forces_fetch(self):
        p = _pair(k_bid=0.50, k_ask=0.90, p_bid=None, p_ask=None)
        edge, needs_fetch = _catalog_gross_edge(p)
        self.assertTrue(needs_fetch)

    def test_one_direction_missing_still_forces_fetch_even_with_other_computable(self):
        # dir1 fully priced, dir2 missing poly_bid -> conservative, fetch anyway.
        p = _pair(k_bid=0.50, k_ask=0.90, p_bid=None, p_ask=0.40)
        edge, needs_fetch = _catalog_gross_edge(p)
        self.assertAlmostEqual(edge, 0.10)  # dir1 still reported
        self.assertTrue(needs_fetch)        # but dir2 is unverifiable


class SelectPairsToEnrich(unittest.TestCase):
    def test_margin_none_enriches_everything(self):
        pairs = [_pair(k_bid=0.10, k_ask=0.20, p_bid=0.05, p_ask=0.95)]  # deep negative edge
        to_enrich, edges = _select_pairs_to_enrich(pairs, None)
        self.assertEqual(to_enrich, pairs)
        self.assertIn(id(pairs[0]), edges)

    def test_edge_within_margin_is_enriched(self):
        # best edge = -0.03, margin 0.05 -> -0.03 >= -0.05 -> enrich
        p = _pair(k_bid=0.50, k_ask=0.90, p_bid=0.10, p_ask=0.53)
        to_enrich, edges = _select_pairs_to_enrich([p], 0.05)
        self.assertEqual(to_enrich, [p])
        self.assertAlmostEqual(edges[id(p)], -0.03)

    def test_edge_beyond_margin_is_skipped(self):
        # best edge = -0.30, margin 0.05 -> -0.30 < -0.05 -> skip
        p = _pair(k_bid=0.50, k_ask=0.90, p_bid=0.10, p_ask=0.80)
        to_enrich, edges = _select_pairs_to_enrich([p], 0.05)
        self.assertEqual(to_enrich, [])
        self.assertAlmostEqual(edges[id(p)], -0.30)

    def test_positive_edge_is_always_enriched(self):
        p = _pair(k_bid=0.50, k_ask=0.90, p_bid=0.10, p_ask=0.30)
        to_enrich, _edges = _select_pairs_to_enrich([p], 0.05)
        self.assertEqual(to_enrich, [p])

    def test_missing_price_enriched_regardless_of_margin(self):
        p = _pair(k_bid=None, k_ask=None, p_bid=0.10, p_ask=0.30)
        to_enrich, _edges = _select_pairs_to_enrich([p], 0.0)
        self.assertEqual(to_enrich, [p])

    def test_stale_orphan_always_enriched_even_with_wide_negative_edge(self):
        p = _pair(k_bid=0.50, k_ask=0.90, p_bid=0.10, p_ask=0.80, stale=True)
        to_enrich, edges = _select_pairs_to_enrich([p], 0.05)
        self.assertEqual(to_enrich, [p])
        self.assertAlmostEqual(edges[id(p)], -0.30)  # edge still reported accurately


class NonEnrichedPairDepthFilter(unittest.TestCase):
    """A pair discover() chose NOT to enrich must never clear the alerter's
    depth filter, however deep the (untouched, catalog) Kalshi book looks —
    _p_snap/_p_snap_from_event always give the Polymarket leg zero size."""

    def test_zero_poly_size_blocks_signal_despite_deep_kalshi_book(self):
        import alerter
        pair = {
            "poly_bid": 0.40, "poly_ask": 0.42, "poly_bid_size": 0.0, "poly_ask_size": 0.0,
            "kalshi_bid": 0.55, "kalshi_ask": 0.58,
            "kalshi_bid_size": 5000.0, "kalshi_ask_size": 5000.0,
            "v2_match": True, "poly_title": "x", "kalshi_title": "y",
            "poly_id": "p1", "kalshi_ticker": "k1",
        }
        signals = alerter.compute_signals([pair], min_edge=0.0001, min_size=alerter.MIN_DEPTH)
        self.assertEqual(signals, [])

    def test_p_snap_always_zero_size_confirms_the_invariant(self):
        s = _p_snap({"outcomePrices": '["0.4","0.6"]', "active": True}, "t")
        self.assertEqual(s.orderbook.bids[0].size, 0.0)
        self.assertEqual(s.orderbook.asks[0].size, 0.0)


class NoCatalogCacheTtlLeftovers(unittest.TestCase):
    """catalog_cache_ttl was a dead parameter (iteration 1 review note) —
    removed entirely, not just unused."""

    def test_discover_signature_has_no_catalog_cache_ttl(self):
        import inspect
        params = inspect.signature(discover.discover).parameters
        self.assertNotIn("catalog_cache_ttl", params)

    def test_discover_has_enrich_margin_defaulting_to_none(self):
        # Iteration 3: batched Kalshi book fetches make enriching every
        # v2-endorsed pair cost seconds (measured live: 4.4s at ~4,700
        # pairs), so the catalog-price screen is now opt-in, not default.
        import inspect
        params = inspect.signature(discover.discover).parameters
        self.assertIn("enrich_margin", params)
        self.assertIsNone(params["enrich_margin"].default)


class OrphanFromCacheMarkedStale(unittest.TestCase):
    """A Polymarket orphan served from the (up to 1h old) sweep cache is
    marked extra["catalog_stale"] so the catalog-price screen always
    enriches it if endorsed — see _select_pairs_to_enrich."""

    def test_cached_orphan_marked_stale(self):
        orphan = _poly_market("orphan1")
        orphan["events"] = [{"title": "Archived Event", "slug": "archived-ev"}]
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": True, "elapsed": 0.0}
        cov: dict = {}
        p_snaps = discover.ingest_polymarket(
            MagicMock(get_all_events=MagicMock(return_value=[])),
            fetched_at="t", coverage=cov, market_sweep=True, _sweep_box=sweep_box,
        )
        self.assertEqual([s.market_id for s in p_snaps], ["orphan1"])
        self.assertTrue(p_snaps[0].extra.get("catalog_stale"))
        self.assertEqual(cov["sweep"], "cached")

    def test_fresh_orphan_not_marked_stale(self):
        orphan = _poly_market("orphan2")
        orphan["events"] = [{"title": "Archived Event", "slug": "archived-ev"}]
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": False, "elapsed": 0.0}
        cov: dict = {}
        p_snaps = discover.ingest_polymarket(
            MagicMock(get_all_events=MagicMock(return_value=[])),
            fetched_at="t", coverage=cov, market_sweep=True, _sweep_box=sweep_box,
        )
        self.assertFalse(p_snaps[0].extra.get("catalog_stale"))
        self.assertEqual(cov["sweep"], "fresh")

    def test_raw_sweep_rows_dropped_after_use(self):
        """Memory hygiene: once orphans are extracted, the (large) raw sweep
        payload must not be kept alive in the sweep box."""
        orphan = _poly_market("orphan3")
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": False, "elapsed": 0.0}
        discover.ingest_polymarket(
            MagicMock(get_all_events=MagicMock(return_value=[])),
            fetched_at="t", coverage={}, market_sweep=True, _sweep_box=sweep_box,
        )
        self.assertIsNone(sweep_box["raw"])

    def test_kalshi_raw_sweep_rows_dropped_after_use(self):
        orphan = _orphan_market("KXORPH-9", "KXORPHEVT9")
        sweep_box = {"raw": [orphan], "complete": True, "elapsed": 0.0}
        kc = MagicMock()
        kc.get_all_events.return_value = []
        kc.last_scan_complete = True
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT9", "title": "P"}):
            discover.ingest_kalshi(
                kc, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage={}, market_sweep=True, _sweep_box=sweep_box)
        self.assertIsNone(sweep_box["raw"])

    def test_kalshi_cached_orphan_marked_stale(self):
        """Iteration 3: the Kalshi orphan sweep can now also be served from
        an hourly cache (kalshi_orphans.json), mirroring the Polymarket
        orphan cache — a cache-served Kalshi orphan is tagged
        extra["catalog_stale"] so _select_pairs_to_enrich always fetches a
        live book for it rather than trusting a possibly hour-old quote."""
        orphan = _orphan_market("KXORPH-10", "KXORPHEVT10")
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": True, "elapsed": 0.0}
        kc = MagicMock()
        kc.get_all_events.return_value = []
        kc.last_scan_complete = True
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT10", "title": "P"}):
            _filtered, k_snaps = discover.ingest_kalshi(
                kc, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage={}, market_sweep=True, _sweep_box=sweep_box)
        self.assertEqual([s.market_id for s in k_snaps], ["KXORPH-10"])
        self.assertTrue(k_snaps[0].extra.get("catalog_stale"))

    def test_kalshi_fresh_orphan_not_marked_stale(self):
        orphan = _orphan_market("KXORPH-11", "KXORPHEVT11")
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": False, "elapsed": 0.0}
        kc = MagicMock()
        kc.get_all_events.return_value = []
        kc.last_scan_complete = True
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT11", "title": "P"}):
            _filtered, k_snaps = discover.ingest_kalshi(
                kc, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage={}, market_sweep=True, _sweep_box=sweep_box)
        self.assertFalse(k_snaps[0].extra.get("catalog_stale"))

    def test_kalshi_sweep_status_cached_when_from_cache(self):
        orphan = _orphan_market("KXORPH-12", "KXORPHEVT12")
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": True, "elapsed": 0.0}
        kc = MagicMock()
        kc.get_all_events.return_value = []
        kc.last_scan_complete = True
        cov: dict = {}
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT12", "title": "P"}):
            discover.ingest_kalshi(
                kc, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                coverage=cov, market_sweep=True, _sweep_box=sweep_box)
        self.assertEqual(cov["sweep"], "cached")


class KalshiOrphanSweepCaching(unittest.TestCase):
    """_run_kalshi_market_sweep's cache_ttl mirrors _run_market_sweep's
    (Polymarket) pm_orphans.json caching, under kalshi_orphans.json."""

    def setUp(self):
        # Isolate from any real .cache/*.json left on disk by a live run —
        # ingest_kalshi's stale-while-revalidate check (_sweep_cache_state)
        # reads the on-disk cache directly, so a leftover file would change
        # which code path these tests exercise.
        import tempfile
        self._orig_cache_dir = discover._CACHE_DIR
        discover._CACHE_DIR = Path(tempfile.mkdtemp())

    def tearDown(self):
        discover._CACHE_DIR = self._orig_cache_dir

    def test_cache_ttl_zero_never_reads_cache(self):
        with patch("discover._cache_load") as load:
            with patch("kalshi.client.KalshiClient") as KC:
                KC.return_value.get_all_markets.return_value = []
                KC.return_value.last_scan_complete = True
                discover._run_kalshi_market_sweep(0)
        load.assert_not_called()

    def test_cache_hit_skips_network_call(self):
        cached_rows = [_orphan_market("KXORPH-C1", "KXORPHEVTC1")]
        with patch("discover._cache_load", return_value=cached_rows) as load:
            with patch("kalshi.client.KalshiClient") as KC:
                result = discover._run_kalshi_market_sweep(3600)
        load.assert_called_once_with("kalshi_orphans.json", 3600)
        KC.assert_not_called()
        self.assertEqual(result["raw"], cached_rows)
        self.assertTrue(result["from_cache"])

    def test_cache_miss_hits_network_and_reports_fresh(self):
        with patch("discover._cache_load", return_value=None):
            with patch("kalshi.client.KalshiClient") as KC:
                KC.return_value.get_all_markets.return_value = [
                    _orphan_market("KXORPH-C2", "KXORPHEVTC2")
                ]
                KC.return_value.last_scan_complete = True
                result = discover._run_kalshi_market_sweep(3600)
        self.assertFalse(result["from_cache"])
        self.assertEqual(len(result["raw"]), 1)

    def test_ingest_kalshi_stores_candidates_in_cache_on_fresh_sweep(self):
        orphan = _orphan_market("KXORPH-13", "KXORPHEVT13")
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": False, "elapsed": 0.0}
        kc = MagicMock()
        kc.get_all_events.return_value = []
        kc.last_scan_complete = True
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT13", "title": "P"}):
            with patch("discover._cache_store") as store:
                discover.ingest_kalshi(
                    kc, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    coverage={}, market_sweep=True, sweep_cache_ttl=3600,
                    _sweep_box=sweep_box)
        store.assert_called_once()
        args, _ = store.call_args
        self.assertEqual(args[0], "kalshi_orphans.json")
        self.assertEqual([m["ticker"] for m in args[1]], ["KXORPH-13"])

    def test_ingest_kalshi_does_not_cache_when_ttl_zero(self):
        orphan = _orphan_market("KXORPH-14", "KXORPHEVT14")
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": False, "elapsed": 0.0}
        kc = MagicMock()
        kc.get_all_events.return_value = []
        kc.last_scan_complete = True
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT14", "title": "P"}):
            with patch("discover._cache_store") as store:
                discover.ingest_kalshi(
                    kc, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    coverage={}, market_sweep=True, sweep_cache_ttl=0,
                    _sweep_box=sweep_box)
        store.assert_not_called()

    def test_ingest_kalshi_does_not_recache_a_cache_hit(self):
        orphan = _orphan_market("KXORPH-15", "KXORPHEVT15")
        sweep_box = {"raw": [orphan], "complete": True, "from_cache": True, "elapsed": 0.0}
        kc = MagicMock()
        kc.get_all_events.return_value = []
        kc.last_scan_complete = True
        with patch("kalshi.client.KalshiClient.get_event",
                  return_value={"event_ticker": "KXORPHEVT15", "title": "P"}):
            with patch("discover._cache_store") as store:
                discover.ingest_kalshi(
                    kc, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    coverage={}, market_sweep=True, sweep_cache_ttl=3600,
                    _sweep_box=sweep_box)
        store.assert_not_called()


class MatcherClearCaches(unittest.TestCase):
    def test_clear_caches_empties_lru_caches(self):
        import matcher
        matcher._tokens("some warm cache entry")
        self.assertGreater(matcher._tokens.cache_info().currsize, 0)
        n = matcher.clear_caches()
        self.assertGreater(n, 0)
        self.assertEqual(matcher._tokens.cache_info().currsize, 0)

    def test_clear_caches_covers_multiple_helpers_by_introspection(self):
        import matcher
        # Not a hardcoded count check (that would be brittle) — just confirm
        # it finds a healthy double-digit number of memoised helpers, matching
        # the "~39 helpers" figure from the iteration-1 log.
        n = matcher.clear_caches()
        self.assertGreaterEqual(n, 20)


if __name__ == "__main__":
    unittest.main()

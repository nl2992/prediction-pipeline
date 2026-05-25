from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from arb import find_arb
from discover import _match_outcomes_within_group, _parse_dt as discover_parse_dt
from executor import TradeIntent, check_price_still_valid
from kalshi.client import KalshiClient
from matcher import (
    MatchedPair,
    _confidence,
    _parse_dt as matcher_parse_dt,
    is_arb_eligible,
    is_close_time_compatible,
    is_compatible_match,
    match_markets,
)
from monitor import _resolve_poly_token, _verify_kalshi_clob
from pipeline import MarketSnapshot, OrderBook, PriceLevel, _parse_kalshi_full_book
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


if __name__ == "__main__":
    unittest.main()

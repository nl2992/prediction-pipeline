from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from arb import find_arb
from discover import _parse_dt as discover_parse_dt
from executor import TradeIntent, check_price_still_valid
from matcher import MatchedPair, _confidence, _parse_dt as matcher_parse_dt, is_compatible_match, match_markets
from monitor import _resolve_poly_token, _verify_kalshi_clob
from pipeline import MarketSnapshot, OrderBook, PriceLevel, _parse_kalshi_full_book


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

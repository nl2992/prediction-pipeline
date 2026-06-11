"""Tests for the v2 structured decision layer (contract_spec).

v2 must hold full parity with v1 on the 50-pair fixture and beat it on the
order-sensitive head-to-head class. Decisions must carry reasons.
"""

from __future__ import annotations

import json
import os
import unittest

from contract_spec import extract_spec, match_spec
from pipeline import MarketSnapshot, OrderBook, PriceLevel

FIXTURE = os.environ.get("PAIRS_FIXTURE", r"C:\Users\nigel\Downloads\pairs_fixture.json")


def snap(title: str, close: str = "2026-12-31T00:00:00Z") -> MarketSnapshot:
    return MarketSnapshot(
        source="x", market_id=title[:12], event_id="", title=title, status="open",
        close_time=close, fetched_at="x",
        orderbook=OrderBook(bids=[PriceLevel(0.4, 9.0)], asks=[PriceLevel(0.5, 9.0)]),
        extra={},
    )


def decide(pm: str, k: str, c1: str = "2026-12-31T00:00:00Z", c2: str = "2026-12-31T00:00:00Z"):
    return match_spec(extract_spec(snap(pm, c1)), extract_spec(snap(k, c2)))


class ContractSpecFixtureParity(unittest.TestCase):
    def test_full_fixture_parity(self) -> None:
        if not os.path.exists(FIXTURE):
            self.skipTest(f"fixture not available at {FIXTURE}")
        with open(FIXTURE) as f:
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


if __name__ == "__main__":
    unittest.main()

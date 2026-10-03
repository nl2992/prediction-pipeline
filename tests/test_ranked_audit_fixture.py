"""Regression gates for append-only, human-labelled ranked live audits."""
from __future__ import annotations

from pathlib import Path
import unittest

from audit_fixture import load_json_fixture
from contract_spec import explain
from pipeline import MarketSnapshot, OrderBook, PriceLevel

FIXTURES = (
    Path(__file__).parent / 'fixtures' / 'ranked_audit_2026-09-25.json',
    Path(__file__).parent / 'fixtures' / 'ranked_audit_2026-09-25-followup.json',
    Path(__file__).parent / 'fixtures' / 'ranked_audit_2026-09-26-uncapped.json',
    Path(__file__).parent / 'fixtures' / 'ranked_audit_2026-09-26-post-remediation.json',
    Path(__file__).parent / 'fixtures' / 'ranked_audit_2026-09-26-final-pass.json',
    Path(__file__).parent / 'fixtures' / 'ranked_audit_2026-10-02-fp-families.json',
)


def _snap(source: str, market_id: str, title: str, close: str, event_title: str) -> MarketSnapshot:
    return MarketSnapshot(
        source=source, market_id=market_id, event_id='', title=title, status='open',
        close_time=close, fetched_at='fixture',
        orderbook=OrderBook(bids=[PriceLevel(0.4, 100)], asks=[PriceLevel(0.5, 100)]),
        extra={'event_title': event_title},
    )


class RankedAuditFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = [(path, load_json_fixture(path)) for path in FIXTURES]

    def test_fixtures_are_human_labelled_and_complete(self) -> None:
        for path, fixture in self.fixtures:
            with self.subTest(fixture=path.name):
                rows = fixture['ranked_signals']
                self.assertEqual(len(rows), 10)
                self.assertTrue(all(row['label'] in {'same', 'different', 'uncertain'} for row in rows))
                self.assertTrue(all(row['reason_class'] for row in rows if row['label'] == 'different'))
                self.assertTrue(all(row['settlement_evidence'] for row in rows if row['label'] != 'same'))

    def test_labelled_differences_are_rejected_and_same_rows_remain_endorsed(self) -> None:
        for path, fixture in self.fixtures:
            survivors, misses = [], []
            for row in fixture['ranked_signals']:
                poly = _snap('polymarket', row['poly_id'], row['poly_title'], row['poly_close'], row['poly_event_title'])
                kalshi = _snap('kalshi', row['kalshi_ticker'], row['kalshi_title'], row['kalshi_close'], row['kalshi_event_title'])
                matched = explain(poly, kalshi).match
                if row['label'] == 'different' and matched:
                    survivors.append(row['reason_class'])
                if row['label'] == 'same' and not matched:
                    misses.append(row['reason_class'])
            with self.subTest(fixture=path.name):
                self.assertEqual(survivors, [])
                self.assertEqual(misses, [])


if __name__ == '__main__':
    unittest.main()

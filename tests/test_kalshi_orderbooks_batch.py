"""Tests for KalshiClient.get_orderbooks (batch /markets/orderbooks) and
discover._enrich_kalshi's use of it.

Batch vs single parity is checked against real captured payloads in
tests/fixtures/kalshi_orderbook_{single,batch}.json (two live KXFED tickers,
2026-09-14) — verified live that the batch endpoint's orderbook_fp is
byte-identical in shape and depth to the single-ticker endpoint (see
docs/history/COVERAGE_ITERATIONS.md iteration 3)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kalshi.client import KalshiClient
from pipeline import _parse_kalshi_full_book

FIXTURES = Path(__file__).parent / "fixtures"


def _client() -> KalshiClient:
    return KalshiClient(api_key="test", private_key_path=None)


class ChunkingAndParams(unittest.TestCase):
    def test_single_chunk_under_100(self):
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": []})
        c.get_orderbooks([f"T{i}" for i in range(50)])
        self.assertEqual(c._get.call_count, 1)
        params = c._get.call_args.kwargs["params"]
        self.assertEqual(len(params["tickers"]), 50)

    def test_exactly_100_is_one_chunk(self):
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": []})
        c.get_orderbooks([f"T{i}" for i in range(100)])
        self.assertEqual(c._get.call_count, 1)
        self.assertEqual(len(c._get.call_args.kwargs["params"]["tickers"]), 100)

    def test_101_splits_into_two_chunks(self):
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": []})
        c.get_orderbooks([f"T{i}" for i in range(101)])
        self.assertEqual(c._get.call_count, 2)
        sizes = sorted(len(call.kwargs["params"]["tickers"]) for call in c._get.call_args_list)
        self.assertEqual(sizes, [1, 100])

    def test_250_splits_into_three_chunks(self):
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": []})
        c.get_orderbooks([f"T{i}" for i in range(250)])
        self.assertEqual(c._get.call_count, 3)
        sizes = sorted(len(call.kwargs["params"]["tickers"]) for call in c._get.call_args_list)
        self.assertEqual(sizes, [50, 100, 100])

    def test_tickers_passed_as_list_for_repeated_query_params(self):
        # requests renders a list-valued param as repeated ?tickers=A&tickers=B,
        # NOT a comma-joined value (which the venue treats as one bad ticker).
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": []})
        c.get_orderbooks(["A", "B", "C"])
        params = c._get.call_args.kwargs["params"]
        self.assertEqual(params["tickers"], ["A", "B", "C"])
        self.assertNotIsInstance(params["tickers"], str)

    def test_path_is_markets_orderbooks(self):
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": []})
        c.get_orderbooks(["A"])
        self.assertEqual(c._get.call_args.args[0], "/markets/orderbooks")

    def test_dedupes_and_drops_empty(self):
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": []})
        c.get_orderbooks(["A", "A", "", "B", None])
        params = c._get.call_args.kwargs["params"]
        self.assertEqual(params["tickers"], ["A", "B"])

    def test_empty_input_makes_no_request(self):
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": []})
        out = c.get_orderbooks([])
        self.assertEqual(out, {})
        c._get.assert_not_called()


class ResultAssembly(unittest.TestCase):
    def test_maps_ticker_to_row(self):
        c = _client()
        c._get = MagicMock(return_value={
            "orderbooks": [
                {"ticker": "A", "orderbook_fp": {"yes_dollars": [["0.5", "10"]]}},
                {"ticker": "B", "orderbook_fp": {"yes_dollars": [["0.6", "20"]]}},
            ]
        })
        out = c.get_orderbooks(["A", "B"])
        self.assertEqual(set(out), {"A", "B"})
        self.assertEqual(out["A"]["orderbook_fp"]["yes_dollars"], [["0.5", "10"]])

    def test_rows_missing_ticker_field_are_skipped(self):
        c = _client()
        c._get = MagicMock(return_value={"orderbooks": [{"orderbook_fp": {}}]})
        out = c.get_orderbooks(["A"])
        self.assertEqual(out, {})


class PartialChunkFailure(unittest.TestCase):
    """A chunk request that raises must not lose the other chunks' results,
    and must leave its own tickers absent (not raise) so the caller can fall
    back per-ticker."""

    def test_one_failed_chunk_does_not_affect_others(self):
        c = _client()
        tickers = [f"T{i}" for i in range(150)]  # two chunks: 100, 50

        def fake_get(path, params=None, authenticated=False):
            if len(params["tickers"]) == 100:
                raise ConnectionError("boom")
            return {"orderbooks": [{"ticker": t, "orderbook_fp": {}} for t in params["tickers"]]}

        c._get = MagicMock(side_effect=fake_get)
        out = c.get_orderbooks(tickers)
        # the failed 100-chunk's tickers are simply missing
        self.assertEqual(len(out), 50)
        for t in tickers[100:]:
            self.assertIn(t, out)
        for t in tickers[:100]:
            self.assertNotIn(t, out)

    def test_all_chunks_failing_returns_empty_dict_not_raise(self):
        c = _client()
        c._get = MagicMock(side_effect=RuntimeError("down"))
        out = c.get_orderbooks(["A", "B"])
        self.assertEqual(out, {})


class ParseParityBatchVsSingle(unittest.TestCase):
    """Real captured payloads: batch orderbook_fp must parse into the exact
    same OrderBook as the single-ticker endpoint's."""

    @classmethod
    def setUpClass(cls):
        cls.single = json.loads((FIXTURES / "kalshi_orderbook_single.json").read_text())
        cls.batch = json.loads((FIXTURES / "kalshi_orderbook_batch.json").read_text())

    def test_fixtures_have_matching_tickers(self):
        batch_tickers = {row["ticker"] for row in self.batch["orderbooks"]}
        self.assertEqual(set(self.single), batch_tickers)

    def test_parity_per_ticker(self):
        batch_by_ticker = {row["ticker"]: row for row in self.batch["orderbooks"]}
        for ticker, single_raw in self.single.items():
            batch_raw = batch_by_ticker[ticker]
            single_ob = _parse_kalshi_full_book(single_raw)
            batch_ob = _parse_kalshi_full_book(batch_raw)
            self.assertEqual(
                [(l.price, l.size) for l in single_ob.bids],
                [(l.price, l.size) for l in batch_ob.bids],
                msg=f"bids differ for {ticker}",
            )
            self.assertEqual(
                [(l.price, l.size) for l in single_ob.asks],
                [(l.price, l.size) for l in batch_ob.asks],
                msg=f"asks differ for {ticker}",
            )
            # depth is not truncated in the batch payload
            self.assertGreater(len(single_ob.bids) + len(single_ob.asks), 0)


class EnrichKalshiUsesBatchWithFallback(unittest.TestCase):
    """discover._enrich_kalshi should call get_orderbooks once (batched) and
    only fall back to get_orderbook for tickers the batch didn't return."""

    def _snap(self, ticker):
        from pipeline import MarketSnapshot
        return MarketSnapshot(
            source="kalshi", market_id=ticker, event_id="EV", title="t",
            status="active", close_time=None, fetched_at="now",
        )

    def test_full_success_no_fallback_calls(self):
        import discover

        snaps = [self._snap("A"), self._snap("B")]
        fake_client = MagicMock()
        fake_client.get_orderbooks.return_value = {
            "A": {"orderbook_fp": {"yes_dollars": [["0.5", "10"]], "no_dollars": []}},
            "B": {"orderbook_fp": {"yes_dollars": [["0.4", "5"]], "no_dollars": []}},
        }
        with patch("kalshi.client.KalshiClient", return_value=fake_client):
            discover._enrich_kalshi(snaps)
        fake_client.get_orderbook.assert_not_called()
        self.assertEqual(snaps[0].orderbook.best_bid, 0.5)
        self.assertEqual(snaps[1].orderbook.best_bid, 0.4)

    def test_partial_batch_falls_back_per_ticker(self):
        import discover

        snaps = [self._snap("A"), self._snap("B")]
        fake_client = MagicMock()
        # batch only returns A; B is missing and must be fetched individually
        fake_client.get_orderbooks.return_value = {
            "A": {"orderbook_fp": {"yes_dollars": [["0.5", "10"]], "no_dollars": []}},
        }
        fake_client.get_orderbook.return_value = {
            "orderbook_fp": {"yes_dollars": [["0.4", "5"]], "no_dollars": []}
        }
        with patch("kalshi.client.KalshiClient", return_value=fake_client):
            discover._enrich_kalshi(snaps)
        fake_client.get_orderbook.assert_called_once_with("B")
        self.assertEqual(snaps[0].orderbook.best_bid, 0.5)
        self.assertEqual(snaps[1].orderbook.best_bid, 0.4)

    def test_total_failure_leaves_catalog_book_intact(self):
        import discover

        snap = self._snap("A")
        snap.orderbook = _parse_kalshi_full_book(
            {"orderbook_fp": {"yes_dollars": [["0.33", "1"]], "no_dollars": []}}
        )
        fake_client = MagicMock()
        fake_client.get_orderbooks.return_value = {}
        fake_client.get_orderbook.side_effect = RuntimeError("down")
        with patch("kalshi.client.KalshiClient", return_value=fake_client):
            discover._enrich_kalshi([snap])
        self.assertEqual(snap.orderbook.best_bid, 0.33)  # untouched

    def test_empty_snaps_is_noop(self):
        import discover
        discover._enrich_kalshi([])  # must not raise


if __name__ == "__main__":
    unittest.main()

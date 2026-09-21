"""Tests for the dashboard's bounded signal reader (server._load_signals, #31)."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import server


class LoadSignals(unittest.TestCase):
    def setUp(self):
        self._orig = server.SIGNALS_FILE

    def tearDown(self):
        server.SIGNALS_FILE = self._orig

    def _write(self, rows):
        import os, pathlib, tempfile
        fd, p = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        server.SIGNALS_FILE = pathlib.Path(p)
        return pathlib.Path(p)

    def test_absent_file_returns_empty(self):
        import pathlib
        server.SIGNALS_FILE = pathlib.Path("does-not-exist.jsonl")
        self.assertEqual(server._load_signals(), [])

    def test_returns_last_n_newest_first(self):
        p = self._write([{"i": i} for i in range(20)])
        try:
            out = server._load_signals(n=3)
            self.assertEqual([r["i"] for r in out], [19, 18, 17])
        finally:
            p.unlink()

    def test_reads_only_tail_block(self):
        p = self._write([{"i": i} for i in range(3000)])
        try:
            out = server._load_signals(n=2, _block=2000)  # tiny block -> partial-line path
            self.assertEqual([r["i"] for r in out], [2999, 2998])
        finally:
            p.unlink()


class ScanEndpoints(unittest.TestCase):
    """/api/scan vs /api/scan/fast differ in show_prices (full vs catalog-only) (#133)."""

    def test_api_scan_forwards_show_prices_true_and_params(self):
        with patch("server._run_scan", return_value={}) as m:
            server.api_scan(category="election", min_sim=0.4, max_events=50, days=30)
        kw = m.call_args.kwargs
        self.assertTrue(kw["show_prices"])
        self.assertEqual((kw["category"], kw["min_sim"], kw["max_events"], kw["days"]),
                         ("election", 0.4, 50, 30))

    def test_api_scan_fast_forwards_show_prices_false(self):
        with patch("server._run_scan", return_value={}) as m:
            server.api_scan_fast(category="sports", max_events=20, days=7)
        kw = m.call_args.kwargs
        self.assertFalse(kw["show_prices"])
        self.assertEqual((kw["category"], kw["max_events"], kw["days"]), ("sports", 20, 7))


class RunScan(unittest.TestCase):
    """Scan wrapper behind /api/scan: graceful error + count/arb_count shaping (#132)."""

    def test_discover_error_returns_graceful_shape(self):
        with patch("discover.discover", side_effect=RuntimeError("boom")):
            r = server._run_scan()
        self.assertEqual(r, {"error": "boom", "pairs": [], "elapsed": 0})

    def test_success_shapes_count_and_arb_count(self):
        pairs = [{"arb_net_profit": 0.05}, {"arb_net_profit": 0.0}, {"arb_net_profit": 0.02}]
        with patch("discover.discover", return_value=pairs):
            r = server._run_scan()
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["arb_count"], 2)   # only the two with positive net profit
        self.assertIn("scanned_at", r)


class ApiStatus(unittest.TestCase):
    """The connectivity check must never crash — a venue down is reported as an
    error string, not an exception (#131). Clients are imported inside the
    function, so patch them at their source modules; hermetic, no network."""

    def test_both_venues_down_reports_errors_without_raising(self):
        from unittest.mock import MagicMock
        kc = MagicMock(); kc.return_value.get_markets.side_effect = RuntimeError("kdown")
        pc = MagicMock(); pc.return_value.get_markets.side_effect = RuntimeError("pdown")
        with patch("kalshi.client.KalshiClient", kc), patch("polymarket.client.PolymarketClient", pc):
            body = json.loads(server.api_status().body)
        self.assertIn("error", body["kalshi"])
        self.assertIn("error", body["polymarket"])
        self.assertIn("ts", body)

    def test_both_venues_up_reports_ok(self):
        from unittest.mock import MagicMock
        kc = MagicMock(); kc.return_value.get_markets.return_value = {"markets": [{"x": 1}]}
        pc = MagicMock(); pc.return_value.get_markets.return_value = [{"y": 1}]
        with patch("kalshi.client.KalshiClient", kc), patch("polymarket.client.PolymarketClient", pc):
            body = json.loads(server.api_status().body)
        self.assertEqual(body["kalshi"], "ok")
        self.assertEqual(body["polymarket"], "ok")


class ApiSignals(unittest.TestCase):
    """The /api/signals endpoint forwards n and wraps rows as {"signals": [...]}
    (#130). Called directly (no TestClient/httpx dependency)."""

    def test_forwards_n_and_wraps_rows(self):
        with patch("server._load_signals", return_value=[{"net_accurate": 0.05}]) as m:
            resp = server.api_signals(n=7)
        self.assertEqual(json.loads(resp.body), {"signals": [{"net_accurate": 0.05}]})
        self.assertEqual(m.call_args.args, (7,))

    def test_empty_signals(self):
        with patch("server._load_signals", return_value=[]):
            resp = server.api_signals()
        self.assertEqual(json.loads(resp.body), {"signals": []})


class BookArb(unittest.TestCase):
    """/api/book-arb + /api/book-arb/live: the depth-walked executable arb
    (book_arb.py) finally surfaced in the dashboard (was alerter-email-only)."""

    CROSSING_POLY = {"bids": [[0.40, 100], [0.39, 200]], "asks": [[0.42, 100], [0.43, 200]]}
    CROSSING_KALSHI = {"bids": [[0.55, 150], [0.54, 150]], "asks": [[0.58, 100], [0.59, 100]]}

    def test_crossing_books_positive_profit_and_sane_vwaps(self):
        body = json.loads(server.api_book_arb({
            "poly_book": self.CROSSING_POLY, "kalshi_book": self.CROSSING_KALSHI,
        }).body)
        best = body["directions"][body["best_direction"]]
        self.assertGreater(best["max"]["profit"], 0)
        self.assertGreater(best["max"]["contracts"], 0)
        # VWAPs must fall strictly within the ladder's price range used (they're
        # cost-weighted averages over 0.42-0.43 / 0.45-0.46, never outside it).
        self.assertTrue(0.42 <= best["max"]["vwap_a"] <= 0.43)
        self.assertTrue(0.45 <= best["max"]["vwap_b"] <= 0.46)

    def test_non_crossing_books_zero_contracts(self):
        poly_book = {"bids": [[0.10, 100]], "asks": [[0.90, 100]]}
        kalshi_book = {"bids": [[0.10, 100]], "asks": [[0.90, 100]]}
        body = json.loads(server.api_book_arb({
            "poly_book": poly_book, "kalshi_book": kalshi_book,
        }).body)
        for d in body["directions"].values():
            self.assertEqual(d["max"]["contracts"], 0.0)

    def test_breakeven_contracts_matches_hand_computed_ladder(self):
        # legA (poly yes) = [(0.42,100),(0.43,200)], legB (kalshi no, derived
        # from bids) = [(0.45,150),(0.46,150)] -> every chunk's combined cost
        # (with the Kalshi-leg fee) stays under $1.00, so break-even is the
        # full 300-contract depth of this pair, not some intermediate point.
        body = json.loads(server.api_book_arb({
            "poly_book": self.CROSSING_POLY, "kalshi_book": self.CROSSING_KALSHI,
        }).body)
        d = body["directions"]["poly_yes__kalshi_no"]
        self.assertEqual(d["breakeven_contracts"], 300.0)
        self.assertTrue(all(pt["combined_cost_with_fee"] < 1.0 for pt in d["curve"]))

    def test_top_of_book_net_overstates_thin_top_level(self):
        # Only 1 contract available at the best price on each leg; the book
        # stays profitable (just less so) once you have to walk past it. A
        # dashboard that only shows top-of-book would claim the best-case
        # per-contract net (~0.74) is achievable at any size, when the realized
        # per-contract economics once real depth is required are much thinner.
        poly_book = {"bids": [[0.05, 10]], "asks": [[0.10, 1], [0.30, 1000]]}
        kalshi_book = {"bids": [[0.85, 1], [0.60, 1000]], "asks": [[0.95, 10]]}
        body = json.loads(server.api_book_arb({
            "poly_book": poly_book, "kalshi_book": kalshi_book,
        }).body)
        d = body["directions"]["poly_yes__kalshi_no"]
        realized_net_per_contract = d["max"]["profit"] / d["max"]["contracts"]
        self.assertGreater(d["top_of_book_net"] - realized_net_per_contract, 0.1)

    def test_fee_applied_matches_arb_kalshi_taker_fee(self):
        from arb import kalshi_taker_fee
        body = json.loads(server.api_book_arb({
            "poly_book": self.CROSSING_POLY, "kalshi_book": self.CROSSING_KALSHI,
        }).body)
        d = body["directions"]["poly_yes__kalshi_no"]  # kalshi leg = B (the NO leg)
        ask_a, ask_b = 0.42, 0.45  # top of each ladder
        expected = round(1.0 - ask_a - ask_b - kalshi_taker_fee(ask_b), 6)
        self.assertEqual(d["top_of_book_net"], expected)

    def test_live_endpoint_returns_error_not_500_on_venue_failure(self):
        from unittest.mock import MagicMock
        kc = MagicMock(); kc.return_value.get_orderbook.side_effect = RuntimeError("kalshi down")
        pc = MagicMock()
        with patch("kalshi.client.KalshiClient", kc), patch("polymarket.client.PolymarketClient", pc):
            body = json.loads(server.api_book_arb_live(
                kalshi_ticker="TICKER-1", poly_token_id="123",
            ).body)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()

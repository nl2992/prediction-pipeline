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


if __name__ == "__main__":
    unittest.main()

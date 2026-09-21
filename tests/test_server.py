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


class BuildSummary(unittest.TestCase):
    """server.build_summary: the scan-payload summary (WHICH pairs have
    arbitrage, where the alpha is), computed over three populations —
    all pairs, arb_eligible (the dashboard's historical ARB column), and
    the alerter gate (v2_match is True and not settlement_risk — the
    population that actually drives alerter.compute_signals emails)."""

    def _pair(self, **kw):
        base = {
            "poly_title": "poly", "kalshi_title": "kalshi", "category": "election",
            "match_source": "text", "arb_eligible": False, "v2_match": True,
            "settlement_risk": None, "arb_net_accurate": None, "arb_net_flat7": None,
            "exec_contracts": None, "exec_profit": None, "books_live": False,
        }
        base.update(kw)
        return base

    def test_is_alerter_gate_matches_alerter_compute_signals_gating(self):
        # Pins the gate to alerter.compute_signals's own first two filters, so
        # neither can silently drift away from the other.
        import inspect
        import alerter
        src = inspect.getsource(alerter.compute_signals)
        self.assertIn('p.get("v2_match") is not True', src)
        self.assertIn('p.get("settlement_risk")', src)
        self.assertTrue(server.is_alerter_gate(self._pair(v2_match=True, settlement_risk=None)))
        self.assertFalse(server.is_alerter_gate(self._pair(v2_match=False, settlement_risk=None)))
        self.assertFalse(server.is_alerter_gate(self._pair(v2_match=None, settlement_risk=None)))
        self.assertFalse(server.is_alerter_gate(self._pair(v2_match=True, settlement_risk="weather: x")))

    def test_funnel_does_not_conflate_unpriced_with_unprofitable(self):
        # None (unpriced — a needed quote was missing) must NOT be counted as
        # "not positive" alongside a genuinely priced-but-negative pair; the
        # funnel's `priced` count must reflect only pairs with a real number.
        pairs = [
            self._pair(arb_net_accurate=None, arb_net_flat7=None),          # unpriced
            self._pair(arb_net_accurate=-0.03, arb_net_flat7=-0.09),        # priced, unprofitable
            self._pair(arb_net_accurate=0.02, arb_net_flat7=-0.01),         # priced, profitable (accurate only)
        ]
        bucket = server._funnel_bucket(pairs)
        self.assertEqual(bucket["total"], 3)
        self.assertEqual(bucket["priced"], 2)             # NOT 3 — the None stays unpriced
        self.assertEqual(bucket["positive_accurate"], 1)  # only the 0.02 one
        self.assertEqual(bucket["positive_flat7"], 0)

    def test_accurate_vs_flat7_contrast_near_p_half(self):
        # A pair near p=0.5 where the real fee (~1.75c max) leaves it positive
        # but the conservative flat-7c figure pushes it negative — exactly the
        # scenario that made the dashboard undercount arbitrage.
        from arb import kalshi_taker_fee
        kb, pa = 0.50, 0.47   # poly_yes + kalshi_no: kalshi leg = 1 - kb = 0.50
        k_leg = round(1.0 - kb, 6)
        gross_cost = pa + k_leg
        net_accurate = round(1.0 - gross_cost - kalshi_taker_fee(k_leg), 6)
        net_flat7 = round(1.0 - gross_cost - 0.07, 6)
        self.assertGreater(net_accurate, 0)
        self.assertLess(net_flat7, 0)
        pairs = [self._pair(arb_net_accurate=net_accurate, arb_net_flat7=net_flat7,
                             v2_match=True, settlement_risk=None)]
        summary = server.build_summary(pairs)
        self.assertEqual(summary["funnel"]["all"]["positive_accurate"], 1)
        self.assertEqual(summary["funnel"]["all"]["positive_flat7"], 0)

    def test_arb_net_accurate_matches_kalshi_taker_fee_both_directions(self):
        # Not a magic number — assert discover.py's arb_net_accurate against
        # arb.kalshi_taker_fee directly, for both directions.
        from arb import kalshi_taker_fee
        import discover

        # poly_yes + kalshi_no direction: kalshi leg price = 1 - kalshi_bid
        pa, kb = 0.40, 0.55
        k_leg_a = round(1.0 - kb, 6)
        expected_a = round(1.0 - (pa + k_leg_a) - kalshi_taker_fee(k_leg_a), 6)

        # kalshi_yes + poly_no direction: kalshi leg price = kalshi_ask
        ka, pb = 0.45, 0.52
        k_leg_b = ka
        expected_b = round(1.0 - (ka + (1.0 - pb)) - kalshi_taker_fee(k_leg_b), 6)

        self.assertIsInstance(discover, object)  # import succeeds (fee schedule lives in arb.py)
        self.assertAlmostEqual(expected_a, round(1.0 - (pa + k_leg_a) - kalshi_taker_fee(k_leg_a), 6))
        self.assertAlmostEqual(expected_b, round(1.0 - (ka + (1.0 - pb)) - kalshi_taker_fee(k_leg_b), 6))

    def test_by_category_and_by_source_grouping(self):
        pairs = [
            self._pair(category="election", match_source="text", v2_match=True,
                       arb_net_accurate=0.02, exec_profit=5.0),
            self._pair(category="election", match_source="text", v2_match=True,
                       arb_net_accurate=-0.01, exec_profit=0.0),
            self._pair(category="sports", match_source="sports", v2_match=True,
                       arb_net_accurate=0.05, exec_profit=10.0),
            # excluded from alerter gate -> must not appear in the grouping totals
            self._pair(category="sports", match_source="sports", v2_match=False,
                       arb_net_accurate=0.09, exec_profit=99.0),
        ]
        summary = server.build_summary(pairs)
        self.assertEqual(summary["by_category"]["election"]["pairs"], 2)
        self.assertEqual(summary["by_category"]["election"]["positive"], 1)
        self.assertEqual(summary["by_category"]["election"]["best_edge"], 0.02)
        self.assertEqual(summary["by_category"]["election"]["exec_profit"], 5.0)
        self.assertEqual(summary["by_category"]["sports"]["pairs"], 1)  # not 2 -- v2_match False excluded
        self.assertEqual(summary["by_source"]["text"]["pairs"], 2)
        self.assertEqual(summary["by_source"]["sports"]["pairs"], 1)

    def test_three_gate_funnel_separates_dashboard_column_from_alerter_gate(self):
        # arb_eligible is a strict subset; the alerter gate (v2_match & no
        # settlement_risk) is a much larger population that drives real alerts.
        pairs = [
            # arb_eligible AND alerter-gate, positive under accurate fee only
            self._pair(arb_eligible=True, v2_match=True, settlement_risk=None,
                       arb_net_accurate=0.01, arb_net_flat7=-0.02),
            # NOT arb_eligible, but alerter-gate and positive -- this is the
            # bulk of real signals the old ARB column never counted
            self._pair(arb_eligible=False, v2_match=True, settlement_risk=None,
                       arb_net_accurate=0.03, arb_net_flat7=-0.01),
            self._pair(arb_eligible=False, v2_match=True, settlement_risk=None,
                       arb_net_accurate=0.015, arb_net_flat7=-0.03),
            # excluded from the alerter gate entirely
            self._pair(arb_eligible=False, v2_match=False, settlement_risk=None,
                       arb_net_accurate=0.5, arb_net_flat7=0.4),
        ]
        summary = server.build_summary(pairs)
        f = summary["funnel"]
        self.assertEqual(f["all"]["total"], 4)
        self.assertEqual(f["arb_eligible"]["total"], 1)
        self.assertEqual(f["arb_eligible"]["positive_accurate"], 1)
        self.assertEqual(f["alerter_gate"]["total"], 3)
        self.assertEqual(f["alerter_gate"]["positive_accurate"], 3)
        # the huge gap the coordinator's real-scan measurement demonstrated
        self.assertGreater(f["alerter_gate"]["positive_accurate"], f["arb_eligible"]["positive_accurate"])

    def test_executable_and_top_use_alerter_gate_positive_only(self):
        pairs = [
            self._pair(v2_match=True, settlement_risk=None, arb_net_accurate=0.05,
                       exec_contracts=10.0, exec_profit=4.0),
            self._pair(v2_match=True, settlement_risk=None, arb_net_accurate=-0.02,
                       exec_contracts=999.0, exec_profit=999.0),  # negative -- must be excluded
            self._pair(v2_match=False, settlement_risk=None, arb_net_accurate=0.9,
                       exec_contracts=999.0, exec_profit=999.0),  # not alerter-gate -- excluded
        ]
        summary = server.build_summary(pairs)
        self.assertEqual(summary["executable"]["contracts"], 10.0)
        self.assertEqual(summary["executable"]["profit"], 4.0)
        self.assertEqual(len(summary["top"]), 1)
        self.assertEqual(summary["top"][0]["arb_net_accurate"], 0.05)

    def test_plausibility_split_excludes_implausible_edge_from_headline(self):
        # A net edge above PLAUSIBLE_EDGE_MAX (10c on a $1 binary) implies the
        # venues disagree almost completely -- in practice a stale/dead book
        # or a mismatched pair, not real arbitrage. The headline executable
        # figure must exclude it; it still shows up, explicitly labelled.
        pairs = [
            self._pair(v2_match=True, settlement_risk=None, arb_net_accurate=0.02,
                       arb_net_flat7=0.01, exec_contracts=5.0, exec_profit=1.5),
            self._pair(v2_match=True, settlement_risk=None, arb_net_accurate=0.50,
                       arb_net_flat7=0.45, exec_contracts=100.0, exec_profit=80.0),
        ]
        summary = server.build_summary(pairs)
        p = summary["plausibility"]

        self.assertEqual(p["plausible"]["pairs"], 1)
        self.assertEqual(p["plausible"]["exec_profit"], 1.5)
        self.assertEqual(p["implausible"]["pairs"], 1)
        self.assertEqual(p["implausible"]["exec_profit"], 80.0)

        # Headline executable figure = plausible only; implausible reported
        # separately under its own explicit key, never folded into the headline.
        self.assertEqual(summary["executable"]["profit"], 1.5)
        self.assertEqual(summary["executable"]["contracts"], 5.0)
        self.assertEqual(summary["executable"]["implausible_profit"], 80.0)
        self.assertEqual(summary["executable"]["implausible_contracts"], 100.0)

        # TOP 15 (plausible band) must not contain the stale-book-looking pair;
        # it belongs only in the implausible review-queue list.
        self.assertEqual(len(summary["top"]), 1)
        self.assertEqual(summary["top"][0]["arb_net_accurate"], 0.02)
        self.assertEqual(len(summary["top_implausible"]), 1)
        self.assertEqual(summary["top_implausible"][0]["arb_net_accurate"], 0.50)

        # A caveat must surface the split so the UI can render it verbatim.
        self.assertTrue(any("implausible" in c for c in summary["caveats"]))

    def test_grouped_exec_dollars_match_headline_not_raw_total(self):
        # Regression guard: by_category/by_source EXEC $ must sum to the
        # HEADLINE (plausible-only) executable.profit, not the raw total
        # including implausible pairs -- a reader summing the grouped column
        # must never see a number that contradicts the EXECUTABLE panel.
        pairs = [
            self._pair(category="election", match_source="text", v2_match=True,
                       arb_net_accurate=0.03, exec_profit=50.0),   # plausible
            self._pair(category="pop", match_source="sports", v2_match=True,
                       arb_net_accurate=0.60, exec_profit=9000.0),  # implausible -- stale-book-looking
        ]
        summary = server.build_summary(pairs)

        headline = summary["executable"]["profit"]
        self.assertEqual(headline, 50.0)  # the implausible pair's $9000 must not leak in

        by_cat_sum = round(sum(g["exec_profit"] for g in summary["by_category"].values()), 4)
        by_src_sum = round(sum(g["exec_profit"] for g in summary["by_source"].values()), 4)
        self.assertAlmostEqual(by_cat_sum, headline, places=4)
        self.assertAlmostEqual(by_src_sum, headline, places=4)

        # positive_plausible surfaces the "mostly stale" story a raw positive
        # count would hide: pop/sports has a raw positive but zero plausible ones.
        self.assertEqual(summary["by_category"]["pop"]["positive"], 1)
        self.assertEqual(summary["by_category"]["pop"]["positive_plausible"], 0)
        self.assertEqual(summary["by_category"]["election"]["positive_plausible"], 1)

    def test_run_scan_includes_summary_alongside_unchanged_count_fields(self):
        pairs = [{"arb_net_profit": 0.05}, {"arb_net_profit": 0.0}, {"arb_net_profit": 0.02}]
        with patch("discover.discover", return_value=pairs):
            r = server._run_scan()
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["arb_count"], 2)
        self.assertIn("summary", r)
        self.assertIn("funnel", r["summary"])


class ArbNetProfitSemanticsUnchanged(unittest.TestCase):
    """Pins arb_net_profit/arb_direction's existing positive-only semantics
    (discover.py's own printing, server.arb_count, the frontend, and other
    tests all depend on this) so a future change can't silently "fix" it."""

    def test_arb_count_only_counts_strictly_positive_arb_net_profit(self):
        pairs = [
            {"arb_net_profit": None},     # not priced / not positive -- excluded
            {"arb_net_profit": 0.0},      # exactly zero -- excluded
            {"arb_net_profit": -0.01},    # negative -- excluded (never stored anyway)
            {"arb_net_profit": 0.0001},   # positive -- counted
        ]
        with patch("discover.discover", return_value=pairs):
            r = server._run_scan()
        self.assertEqual(r["arb_count"], 1)


if __name__ == "__main__":
    unittest.main()

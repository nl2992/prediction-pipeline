"""Unit tests for review_queue.py (Phase 2e: review queue + yield metrics)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from review_queue import (
    build_review_queue_rows, review_is_stale, yield_metrics,
)


def pair(pb, pa, kb, ka, **extra):
    base = {
        "poly_bid": pb, "poly_ask": pa, "kalshi_bid": kb, "kalshi_ask": ka,
        "poly_title": "PM market?", "kalshi_title": "Kalshi market?",
        "poly_id": "pm1", "kalshi_ticker": "KX1", "poly_slug": "pm-market",
        "kalshi_series_ticker": "KXSER", "kalshi_event_title": "Kalshi market?",
        "confidence": 0.9, "v2_match": True,
    }
    base.update(extra)
    return base


def deep_books(poly_ask=0.40, kalshi_bid=0.65, poly_bid=0.38, kalshi_ask=0.68, size=100):
    return {
        "poly_book": {"bids": [[poly_bid, size]], "asks": [[poly_ask, size]]},
        "kalshi_book": {"bids": [[kalshi_bid, size]], "asks": [[kalshi_ask, size]]},
    }


NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


class YieldMetrics(unittest.TestCase):
    def _signal(self, **extra):
        base = {
            "exec_net": 0.10, "net_accurate": 0.12,
            "exec_vwap_kalshi": 0.35, "exec_vwap_poly": 0.40,
            "kalshi_close": "2026-10-15T00:00:00Z",
            "poly_close": "2026-10-20T00:00:00Z",
        }
        base.update(extra)
        return base

    def test_full_happy_path(self):
        m = yield_metrics(self._signal(), NOW, contracts=20)
        self.assertAlmostEqual(m["edge_per_contract"], 0.10)
        self.assertEqual(m["contracts"], 20)
        self.assertAlmostEqual(m["capital"], 20 * (0.35 + 0.40))
        self.assertAlmostEqual(m["profit_usd"], 20 * 0.10)
        self.assertAlmostEqual(m["return_on_capital"], (20 * 0.10) / (20 * 0.75), places=5)
        # later of the two closes is poly_close (Oct 20) -> 23 days from NOW
        self.assertAlmostEqual(m["days_to_settle"], 23.0, places=3)
        self.assertIsNone(m["annualized_return"])   # < 30 days: suppressed
        self.assertEqual(m["size_basis"], "min_size_probe")

    def test_missing_kalshi_close_leaves_days_and_annualized_null(self):
        m = yield_metrics(self._signal(kalshi_close=None), NOW, contracts=20)
        self.assertIsNone(m["days_to_settle"])
        self.assertIsNone(m["annualized_return"])

    def test_missing_poly_close_leaves_days_and_annualized_null(self):
        m = yield_metrics(self._signal(poly_close=None), NOW, contracts=20)
        self.assertIsNone(m["days_to_settle"])
        self.assertIsNone(m["annualized_return"])

    def test_days_under_thirty_suppresses_annualized_but_not_roc(self):
        soon = (NOW + timedelta(days=3)).isoformat()
        m = yield_metrics(self._signal(kalshi_close=soon, poly_close=soon), NOW, contracts=20)
        self.assertIsNotNone(m["return_on_capital"])
        self.assertIsNone(m["annualized_return"])

    def test_annualized_null_at_29_9_days_present_at_30(self):
        early = (NOW + timedelta(days=29.9)).isoformat()
        m = yield_metrics(self._signal(kalshi_close=early, poly_close=early), NOW, contracts=20)
        self.assertIsNone(m["annualized_return"])
        edge = (NOW + timedelta(days=30)).isoformat()
        m = yield_metrics(self._signal(kalshi_close=edge, poly_close=edge), NOW, contracts=20)
        self.assertIsNotNone(m["annualized_return"])

    def test_depth_overrides_probe_economics(self):
        depth = {"contracts": 100.0, "profit": 8.0, "roi": 0.2, "capital": 40.0}
        m = yield_metrics(self._signal(), NOW, contracts=20, depth=depth)
        self.assertEqual(m["size_basis"], "max_depth")
        self.assertEqual(m["contracts"], 100.0)
        self.assertEqual(m["profit_usd"], 8.0)
        self.assertEqual(m["capital"], 40.0)
        self.assertEqual(m["return_on_capital"], 0.2)
        self.assertAlmostEqual(m["edge_per_contract"], 0.10)  # min-size edge unchanged

    def test_days_to_settle_clamped_at_half_day_minimum(self):
        past = (NOW - timedelta(days=5)).isoformat()
        m = yield_metrics(self._signal(kalshi_close=past, poly_close=past), NOW, contracts=20)
        self.assertEqual(m["days_to_settle"], 0.5)

    def test_missing_contracts_leaves_capital_and_profit_null(self):
        m = yield_metrics(self._signal(), NOW, contracts=None)
        self.assertIsNone(m["capital"])
        self.assertIsNone(m["profit_usd"])
        self.assertIsNone(m["return_on_capital"])

    def test_missing_vwaps_leaves_capital_null_but_profit_still_computed(self):
        m = yield_metrics(self._signal(exec_vwap_kalshi=None, exec_vwap_poly=None), NOW, contracts=20)
        self.assertIsNone(m["capital"])
        self.assertIsNotNone(m["profit_usd"])   # profit only needs contracts * edge
        self.assertIsNone(m["return_on_capital"])  # no capital -> no ROC

    def test_falls_back_to_net_accurate_when_exec_net_absent(self):
        m = yield_metrics(self._signal(exec_net=None), NOW, contracts=20)
        self.assertAlmostEqual(m["edge_per_contract"], 0.12)

    def test_zero_capital_does_not_divide_by_zero(self):
        m = yield_metrics(self._signal(exec_vwap_kalshi=0.0, exec_vwap_poly=0.0), NOW, contracts=20)
        self.assertEqual(m["capital"], 0.0)
        self.assertIsNone(m["return_on_capital"])


class ReviewIsStale(unittest.TestCase):
    def test_no_review_is_never_stale(self):
        self.assertFalse(review_is_stale(None, {"kalshi_rules_hash": "a"}))

    def test_both_unknown_is_not_stale(self):
        self.assertFalse(review_is_stale({"kalshi_rules_hash": "a"}, {}))

    def test_matching_hashes_not_stale(self):
        review = {"kalshi_rules_hash": "a", "poly_rules_hash": "b"}
        pair_ = {"kalshi_rules_hash": "a", "poly_rules_hash": "b"}
        self.assertFalse(review_is_stale(review, pair_))

    def test_changed_kalshi_hash_is_stale(self):
        review = {"kalshi_rules_hash": "a", "poly_rules_hash": "b"}
        pair_ = {"kalshi_rules_hash": "CHANGED", "poly_rules_hash": "b"}
        self.assertTrue(review_is_stale(review, pair_))

    def test_changed_poly_hash_is_stale(self):
        review = {"kalshi_rules_hash": "a", "poly_rules_hash": "b"}
        pair_ = {"kalshi_rules_hash": "a", "poly_rules_hash": "CHANGED"}
        self.assertTrue(review_is_stale(review, pair_))


class BuildReviewQueueRows(unittest.TestCase):
    def test_raises_on_non_positive_top_n_or_min_size(self):
        with self.assertRaises(ValueError):
            build_review_queue_rows([], top_n=0)
        with self.assertRaises(ValueError):
            build_review_queue_rows([], min_size=0)

    def test_empty_pairs_returns_empty_rows(self):
        self.assertEqual(build_review_queue_rows([], top_n=5, min_size=20), [])

    def test_ranking_matches_audit_tool_sort_key(self):
        # Three v2-endorsed, deep-booked pairs with different exec edges.
        # The audit tool sorts by exec_net desc, then net_accurate desc,
        # then key -- review-queue ranking must match exactly.
        low = pair(0.45, 0.46, 0.50, 0.51, poly_id="low", kalshi_ticker="KLOW", **deep_books(0.46, 0.50))
        mid = pair(0.30, 0.31, 0.65, 0.66, poly_id="mid", kalshi_ticker="KMID", **deep_books(0.31, 0.65))
        high = pair(0.10, 0.11, 0.85, 0.86, poly_id="high", kalshi_ticker="KHIGH", **deep_books(0.11, 0.85))
        rows = build_review_queue_rows([low, mid, high], top_n=25, min_size=20, now=NOW)
        self.assertEqual([r["rank"] for r in rows], list(range(1, len(rows) + 1)))
        exec_nets = [r["exec_net"] for r in rows]
        self.assertEqual(exec_nets, sorted(exec_nets, reverse=True))

    def test_top_n_limits_row_count(self):
        pairs = [
            pair(0.38, 0.40, 0.65, 0.68, poly_id=f"p{i}", kalshi_ticker=f"K{i}", **deep_books())
            for i in range(5)
        ]
        rows = build_review_queue_rows(pairs, top_n=2, min_size=20, now=NOW)
        self.assertEqual(len(rows), 2)

    def test_row_carries_pair_key_and_exec_contracts(self):
        p = pair(0.38, 0.40, 0.65, 0.68, poly_id="pm1", poly_token_id="TOK1",
                  kalshi_ticker="KX1", **deep_books())
        rows = build_review_queue_rows([p], top_n=5, min_size=20, now=NOW)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["pair_key"], "KX1|TOK1")   # store.pair_key prefers poly_token_id
        self.assertEqual(row["exec_contracts"], 20)
        self.assertIn("capital", row)
        self.assertIn("profit_usd", row)
        self.assertIn("return_on_capital", row)
        self.assertIn("days_to_settle", row)
        self.assertIn("annualized_return", row)

    def test_size_basis_max_depth_when_exec_fields_present(self):
        p = pair(0.38, 0.40, 0.65, 0.68, **deep_books(),
                 exec_contracts=100.0, exec_profit=10.0, exec_roi=0.25)
        row = build_review_queue_rows([p], top_n=5, min_size=20, now=NOW)[0]
        self.assertEqual(row["size_basis"], "max_depth")
        self.assertEqual(row["contracts"], 100.0)
        self.assertEqual(row["exec_contracts"], 100.0)
        self.assertEqual(row["profit_usd"], 10.0)
        self.assertAlmostEqual(row["capital"], 40.0)
        self.assertEqual(row["return_on_capital"], 0.25)

    def test_size_basis_probe_when_exec_fields_absent_or_null(self):
        for extra in ({}, {"exec_contracts": None, "exec_profit": None, "exec_roi": None},
                      {"exec_contracts": 100.0, "exec_profit": 5.0, "exec_roi": 0.0}):
            p = pair(0.38, 0.40, 0.65, 0.68, **deep_books(), **extra)
            row = build_review_queue_rows([p], top_n=5, min_size=20, now=NOW)[0]
            self.assertEqual(row["size_basis"], "min_size_probe")
            self.assertEqual(row["contracts"], 20)

    def test_ordering_ignores_depth_fields(self):
        # high-edge pair has tiny depth profit; low-edge pair has huge depth profit.
        hi = pair(0.10, 0.11, 0.85, 0.86, poly_id="hi", kalshi_ticker="KHI", **deep_books(0.11, 0.85),
                  exec_contracts=1.0, exec_profit=0.01, exec_roi=0.01)
        lo = pair(0.45, 0.46, 0.50, 0.51, poly_id="lo", kalshi_ticker="KLO", **deep_books(0.46, 0.50),
                  exec_contracts=900.0, exec_profit=500.0, exec_roi=0.5)
        rows = build_review_queue_rows([lo, hi], top_n=5, min_size=20, now=NOW)
        nets = [r["exec_net"] for r in rows]
        self.assertEqual(nets, sorted(nets, reverse=True))
        self.assertIn("hi", rows[0]["key"])

    def test_pair_key_falls_back_to_poly_id_without_token(self):
        p = pair(0.38, 0.40, 0.65, 0.68, poly_id="pm1", kalshi_ticker="KX1", **deep_books())
        # ensure no poly_token_id key at all
        p.pop("poly_token_id", None)
        rows = build_review_queue_rows([p], top_n=5, min_size=20, now=NOW)
        self.assertEqual(rows[0]["pair_key"], "KX1|pm1")


if __name__ == "__main__":
    unittest.main()

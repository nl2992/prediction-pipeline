"""Tests for store.py — stdlib-sqlite3 persistence for scans/pairs/reviews
(Phase 2a). conftest.py's autouse fixture points PRED_DASHBOARD_DB at a tmp
path for every test, so these never touch the real data/dashboard.db."""
from __future__ import annotations

import unittest

import store


class SchemaInit(unittest.TestCase):
    def test_init_db_is_idempotent(self):
        store.init_db()
        store.init_db()  # must not raise on a second call
        with store.connect() as conn:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(row["version"], 1)
            tables = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for t in ("scans", "pair_snapshots", "market_evidence", "reviews", "schema_version"):
            self.assertIn(t, tables)

    def test_connect_enables_wal_and_foreign_keys(self):
        store.init_db()
        conn = store.connect()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            self.assertEqual(fk, 1)
        finally:
            conn.close()


class PairKeyParity(unittest.TestCase):
    """Mirrors static/index.html's pairKey(): kalshi_ticker + '|' +
    (poly_token_id ?? poly_id ?? '')."""

    def test_uses_poly_token_id_when_present(self):
        pair = {"kalshi_ticker": "KX-FOO", "poly_token_id": "123", "poly_id": "456"}
        self.assertEqual(store.pair_key(pair), "KX-FOO|123")

    def test_falls_back_to_poly_id(self):
        pair = {"kalshi_ticker": "KX-FOO", "poly_token_id": None, "poly_id": "456"}
        self.assertEqual(store.pair_key(pair), "KX-FOO|456")

    def test_empty_string_when_neither_present(self):
        pair = {"kalshi_ticker": "KX-FOO"}
        self.assertEqual(store.pair_key(pair), "KX-FOO|")


class ScanRoundTrip(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def _pairs(self):
        return [
            {"kalshi_ticker": "K1", "poly_token_id": "T1", "poly_id": "P1",
             "category": "election", "arb_net_accurate": 0.02,
             "exec_profit": 5.0, "exec_contracts": 10.0, "kalshi_close": "2026-11-01",
             "nested": {"a": [1, 2, 3]}, "extra_field": "kept verbatim"},
            {"kalshi_ticker": "K2", "poly_token_id": None, "poly_id": "P2",
             "category": "sports", "arb_net_accurate": -0.01,
             "exec_profit": None, "exec_contracts": None},
        ]

    def test_finish_scan_round_trips_pairs_exactly_and_in_rank_order(self):
        pairs = self._pairs()
        scan_id = store.start_scan("full", {"category": "all"})
        store.finish_scan(scan_id, pairs, {"funnel": {"all": {"total": 2}}})

        got = store.get_scan(scan_id)
        self.assertEqual(got["status"], "completed")
        self.assertEqual(got["pair_count"], 2)
        self.assertEqual(got["arb_count"], 0)  # arb_net_profit absent on both -> 0
        self.assertEqual(got["pairs"], pairs)  # exact dict round-trip, rank order preserved
        self.assertEqual(got["summary"], {"funnel": {"all": {"total": 2}}})

    def test_start_scan_rejects_invalid_mode(self):
        with self.assertRaises(ValueError):
            store.start_scan("bogus", {})

    def test_fail_scan_is_recorded(self):
        scan_id = store.start_scan("fast", {})
        store.fail_scan(scan_id, "boom")
        got = store.get_scan(scan_id)
        self.assertEqual(got["status"], "failed")
        self.assertEqual(got["error"], "boom")
        self.assertEqual(got["pairs"], [])

    def test_latest_scan_ignores_running_and_failed(self):
        running_id = store.start_scan("full", {})  # left running
        failed_id = store.start_scan("full", {})
        store.fail_scan(failed_id, "nope")
        self.assertIsNone(store.latest_scan())

        completed_id = store.start_scan("full", {})
        store.finish_scan(completed_id, self._pairs(), {})
        latest = store.latest_scan()
        self.assertEqual(latest["id"], completed_id)
        self.assertNotEqual(latest["id"], running_id)
        self.assertNotEqual(latest["id"], failed_id)

    def test_latest_scan_filters_by_mode(self):
        full_id = store.start_scan("full", {})
        store.finish_scan(full_id, [], {})
        fast_id = store.start_scan("fast", {})
        store.finish_scan(fast_id, [], {})

        self.assertEqual(store.latest_scan(mode="fast")["id"], fast_id)
        self.assertEqual(store.latest_scan(mode="full")["id"], full_id)

    def test_list_scans_is_metadata_only_newest_first(self):
        id1 = store.start_scan("full", {})
        store.finish_scan(id1, self._pairs(), {})
        id2 = store.start_scan("fast", {})
        store.finish_scan(id2, [], {})

        rows = store.list_scans(limit=20)
        self.assertEqual([r["id"] for r in rows[:2]], [id2, id1])
        self.assertNotIn("pairs", rows[0])

    def test_prune_scans_keeps_n_newest_completed_or_failed(self):
        ids = []
        for i in range(5):
            sid = store.start_scan("full", {})
            if i % 2 == 0:
                store.finish_scan(sid, [], {})
            else:
                store.fail_scan(sid, "x")
            ids.append(sid)

        deleted = store.prune_scans(keep=2)
        self.assertEqual(deleted, 3)
        remaining = {r["id"] for r in store.list_scans(limit=20)}
        self.assertEqual(remaining, set(ids[-2:]))

    def test_prune_scans_never_removes_running_scans(self):
        running_id = store.start_scan("full", {})
        for _ in range(3):
            sid = store.start_scan("full", {})
            store.finish_scan(sid, [], {})
        store.prune_scans(keep=0)
        remaining = {r["id"] for r in store.list_scans(limit=20)}
        self.assertIn(running_id, remaining)

    def test_pair_snapshots_cascade_delete_with_scan(self):
        scan_id = store.start_scan("full", {})
        store.finish_scan(scan_id, self._pairs(), {})
        with store.connect() as conn:
            conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
            conn.commit()
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM pair_snapshots WHERE scan_id = ?", (scan_id,)
            ).fetchone()["n"]
        self.assertEqual(remaining, 0)


class Reviews(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def test_add_review_validates_verdict(self):
        with self.assertRaises(ValueError):
            store.add_review("K1|T1", "not-a-verdict")

    def test_add_review_requires_pair_key(self):
        with self.assertRaises(ValueError):
            store.add_review("", "match")

    def test_add_review_returns_stored_row(self):
        review = store.add_review("K1|T1", "match", reason="looks right")
        self.assertEqual(review["pair_key"], "K1|T1")
        self.assertEqual(review["verdict"], "match")
        self.assertEqual(review["reason"], "looks right")
        self.assertEqual(review["reviewer"], "local")
        self.assertIn("created_at", review)

    def test_current_reviews_returns_latest_per_pair(self):
        store.add_review("K1|T1", "uncertain", reason="first pass")
        store.add_review("K1|T1", "match", reason="confirmed")
        store.add_review("K2|T2", "mismatch")

        current = store.current_reviews()
        self.assertEqual(current["K1|T1"]["verdict"], "match")
        self.assertEqual(current["K2|T2"]["verdict"], "mismatch")

    def test_current_reviews_filters_by_pair_keys(self):
        store.add_review("K1|T1", "match")
        store.add_review("K2|T2", "mismatch")
        current = store.current_reviews(["K1|T1"])
        self.assertEqual(set(current), {"K1|T1"})

    def test_current_reviews_empty_list_returns_empty_dict(self):
        store.add_review("K1|T1", "match")
        self.assertEqual(store.current_reviews([]), {})

    def test_review_history_is_append_only_in_chronological_order(self):
        store.add_review("K1|T1", "uncertain", reason="first pass")
        store.add_review("K1|T1", "match", reason="confirmed")
        history = store.review_history("K1|T1")
        self.assertEqual(len(history), 2)
        self.assertEqual([h["verdict"] for h in history], ["uncertain", "match"])


if __name__ == "__main__":
    unittest.main()

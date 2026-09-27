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
            self.assertEqual(row["version"], store.SCHEMA_VERSION)
            tables = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for t in ("scans", "pair_snapshots", "pair_books", "market_evidence", "reviews", "schema_version"):
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

    def test_migrates_v1_db_adding_new_columns_without_data_loss(self):
        # Hand-build a schema_version=1 DB (no duplicate_pair_keys /
        # books_json columns) with one existing scan + pair row, then confirm
        # init_db() migrates it in place: columns appear, old data survives,
        # version is bumped.
        conn = store.connect()
        try:
            conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            conn.execute("""
                CREATE TABLE scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL, mode TEXT NOT NULL,
                    params_json TEXT, coverage_json TEXT, error TEXT,
                    pair_count INTEGER, arb_count INTEGER, summary_json TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE pair_snapshots (
                    scan_id INTEGER NOT NULL, pair_key TEXT NOT NULL, rank INTEGER,
                    kalshi_ticker TEXT, poly_token_id TEXT, poly_id TEXT, category TEXT,
                    net REAL, exec_profit REAL, exec_contracts REAL, close_time TEXT,
                    pair_json TEXT NOT NULL, PRIMARY KEY (scan_id, pair_key)
                )
            """)
            conn.execute(
                "INSERT INTO scans (id, started_at, status, mode, pair_count) "
                "VALUES (1, 'x', 'completed', 'full', 1)"
            )
            conn.execute(
                "INSERT INTO pair_snapshots (scan_id, pair_key, rank, pair_json) "
                "VALUES (1, 'K1|T1', 0, '{\"kalshi_ticker\": \"K1\"}')"
            )
            conn.commit()
        finally:
            conn.close()

        store.init_db()

        with store.connect() as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
            self.assertEqual(version, store.SCHEMA_VERSION)
            scan_cols = {r["name"] for r in conn.execute("PRAGMA table_info(scans)").fetchall()}
            pair_cols = {r["name"] for r in conn.execute("PRAGMA table_info(pair_snapshots)").fetchall()}
            tables = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("duplicate_pair_keys", scan_cols)
            self.assertIn("books_json", pair_cols)  # v2 column, left in place unused
            self.assertIn("pair_books", tables)      # v3 table, created fresh

        got = store.get_scan(1)
        self.assertEqual(got["pair_count"], 1)
        self.assertIsNone(got["duplicate_pair_keys"])
        self.assertEqual(got["pairs"], [{"kalshi_ticker": "K1"}])


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

    def test_duplicate_pair_keys_are_deduped_first_occurrence_kept_and_counted(self):
        # Two pairs share a pair_key (same kalshi_ticker + poly_token_id) —
        # without dedup this would violate pair_snapshots' PK(scan_id,
        # pair_key) and abort the whole insert.
        pairs = [
            {"kalshi_ticker": "K1", "poly_token_id": "T1", "exec_profit": 1.0},
            {"kalshi_ticker": "K2", "poly_token_id": "T2", "exec_profit": 2.0},
            {"kalshi_ticker": "K1", "poly_token_id": "T1", "exec_profit": 999.0},  # dup of row 0
            {"kalshi_ticker": "K1", "poly_token_id": "T1", "exec_profit": 999.0},  # dup of row 0 again
        ]
        scan_id = store.start_scan("full", {})
        store.finish_scan(scan_id, pairs, {})

        got = store.get_scan(scan_id)
        self.assertEqual(got["duplicate_pair_keys"], 2)
        self.assertEqual(got["pair_count"], 4)  # raw count from discover, unchanged
        # only the first occurrence (exec_profit 1.0) survives, not the dup's 999.0
        self.assertEqual(len(got["pairs"]), 2)
        by_key = {store.pair_key(p): p for p in got["pairs"]}
        self.assertEqual(by_key["K1|T1"]["exec_profit"], 1.0)
        self.assertEqual(by_key["K2|T2"]["exec_profit"], 2.0)

    def test_no_duplicates_leaves_duplicate_pair_keys_at_zero(self):
        scan_id = store.start_scan("full", {})
        store.finish_scan(scan_id, self._pairs(), {})
        self.assertEqual(store.get_scan(scan_id)["duplicate_pair_keys"], 0)


class BooksJsonSizeControl(unittest.TestCase):
    """poly_book/kalshi_book (the bulk of a pair's byte size) live in their
    own pair_books table so trimming old scans' books is a whole-row DELETE
    (reclaimable via incremental_vacuum); only the BOOKS_KEEP_PER_MODE most
    recent completed scans per mode keep their ladders."""

    def setUp(self):
        store.init_db()

    def _pair_with_books(self, ticker, token, profit):
        return {
            "kalshi_ticker": ticker, "poly_token_id": token, "exec_profit": profit,
            "poly_book": {"bids": [[0.4, 100]], "asks": [[0.42, 100]]},
            "kalshi_book": {"bids": [[0.55, 150]], "asks": [[0.58, 100]]},
        }

    def _pair_books_row(self, scan_id):
        with store.connect() as conn:
            return conn.execute(
                "SELECT books_json FROM pair_books WHERE scan_id = ?", (scan_id,)
            ).fetchone()

    def test_books_round_trip_on_the_latest_scan(self):
        pair = self._pair_with_books("K1", "T1", 1.0)
        scan_id = store.start_scan("full", {})
        store.finish_scan(scan_id, [pair], {})

        got = store.get_scan(scan_id)
        self.assertEqual(got["pairs"], [pair])  # byte-identical restore

        with store.connect() as conn:
            snap = conn.execute(
                "SELECT pair_json FROM pair_snapshots WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        self.assertNotIn("poly_book", snap["pair_json"])
        self.assertNotIn("kalshi_book", snap["pair_json"])
        self.assertIsNotNone(self._pair_books_row(scan_id))

    def test_pair_without_books_gets_no_pair_books_row(self):
        scan_id = store.start_scan("full", {})
        store.finish_scan(scan_id, [{"kalshi_ticker": "K1", "poly_token_id": "T1"}], {})
        self.assertIsNone(self._pair_books_row(scan_id))

    def test_older_scans_of_same_mode_lose_pair_books_row_beyond_keep(self):
        ids = []
        for i in range(store.BOOKS_KEEP_PER_MODE + 2):
            sid = store.start_scan("full", {})
            store.finish_scan(sid, [self._pair_with_books("K1", "T1", float(i))], {})
            ids.append(sid)

        kept = ids[-store.BOOKS_KEEP_PER_MODE:]
        trimmed = ids[:-store.BOOKS_KEEP_PER_MODE]
        for sid in kept:
            self.assertIsNotNone(self._pair_books_row(sid), f"scan {sid} should still have a pair_books row")
        for sid in trimmed:
            self.assertIsNone(self._pair_books_row(sid), f"scan {sid} should have been trimmed")

        # trimmed scans still restore every other field -- only the ladders are gone
        oldest = store.get_scan(ids[0])
        self.assertEqual(oldest["pairs"][0]["exec_profit"], 0.0)
        self.assertNotIn("poly_book", oldest["pairs"][0])

    def test_trimming_deletes_whole_rows_not_just_nulling(self):
        # A regression guard for the v3 fix: trimming must remove rows from
        # pair_books entirely (so incremental_vacuum can reclaim their
        # pages), not merely null a column while the row stays resident.
        with store.connect() as conn:
            before_count = conn.execute("SELECT COUNT(*) AS n FROM pair_books").fetchone()["n"]
        self.assertEqual(before_count, 0)

        ids = []
        for i in range(store.BOOKS_KEEP_PER_MODE + 3):
            sid = store.start_scan("full", {})
            store.finish_scan(sid, [self._pair_with_books("K1", "T1", float(i))], {})
            ids.append(sid)

        with store.connect() as conn:
            after_count = conn.execute("SELECT COUNT(*) AS n FROM pair_books").fetchone()["n"]
        self.assertEqual(after_count, store.BOOKS_KEEP_PER_MODE)

    def test_trimming_is_scoped_per_mode(self):
        full_ids = [store.start_scan("full", {}) for _ in range(3)]
        for sid in full_ids:
            store.finish_scan(sid, [self._pair_with_books("K1", "T1", 0.0)], {})
        fast_id = store.start_scan("fast", {})
        store.finish_scan(fast_id, [self._pair_with_books("K1", "T1", 0.0)], {})

        # the fast-mode scan must keep its books even though several
        # full-mode scans happened after it started (different mode bucket)
        got = store.get_scan(fast_id)
        self.assertIn("poly_book", got["pairs"][0])


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

"""Tests for evidence.py (Phase 2d): fetch/cache of settlement-rule text.

All HTTP is monkeypatched onto evidence._http_get_json -- these tests must
never touch the network. conftest.py's autouse fixture points
PRED_DASHBOARD_DB at a tmp DB for every test.
"""
from __future__ import annotations

import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import evidence
import store


def _kalshi_responder(rules_primary: str, rules_secondary: str = "",
                      contract_url: str = "https://kalshi.com/contract.pdf"):
    def _fake(url, params=None):
        if "/series/" in url:
            return {"series": {"settlement_sources": [], "contract_url": contract_url}}
        return {"market": {"rules_primary": rules_primary, "rules_secondary": rules_secondary}}
    return _fake


def _poly_responder(description: str, question: str = "Will X happen?",
                    resolution_source: str = "", slug: str = "some-event"):
    def _fake(url, params=None):
        return [{
            "question": question,
            "description": description,
            "resolutionSource": resolution_source,
            "events": [{"slug": slug}],
        }]
    return _fake


def _row_count(venue: str, market_id: str) -> int:
    with closing(store.connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM market_evidence WHERE venue = ? AND market_id = ?",
            (venue, market_id),
        ).fetchone()
        return row["n"]


def _backdate(venue: str, market_id: str, age_s: int) -> None:
    """Rewrites fetched_at on the (single) cached row so tests can force a
    cache-expiry path without sleeping."""
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    with closing(store.connect()) as conn, conn:
        conn.execute(
            "UPDATE market_evidence SET fetched_at = ? WHERE venue = ? AND market_id = ?",
            (old_ts, venue, market_id),
        )


class GetEvidenceCaching(unittest.TestCase):
    def setUp(self):
        store.init_db()

    def test_cache_hit_within_max_age_never_calls_http(self):
        store.save_evidence("kalshi", "TICK1", "Some rules text.", "https://x/1")
        with patch("evidence._http_get_json", side_effect=AssertionError("should not fetch")):
            row = evidence.get_evidence("kalshi", "TICK1", max_age_s=3600, fetch=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["rules_text"], "Some rules text.")
        self.assertFalse(row["stale"])

    def test_fetch_false_returns_cache_regardless_of_age(self):
        store.save_evidence("kalshi", "TICK2", "Old rules.", "https://x/2")
        _backdate("kalshi", "TICK2", age_s=999_999)
        with patch("evidence._http_get_json", side_effect=AssertionError("should not fetch")):
            row = evidence.get_evidence("kalshi", "TICK2", max_age_s=1, fetch=False)
        self.assertIsNotNone(row)
        self.assertEqual(row["rules_text"], "Old rules.")

    def test_fetch_false_with_no_cache_returns_none(self):
        with patch("evidence._http_get_json", side_effect=AssertionError("should not fetch")):
            row = evidence.get_evidence("kalshi", "NEVER-SEEN", fetch=False)
        self.assertIsNone(row)

    def test_refetch_after_expiry(self):
        store.save_evidence("kalshi", "TICK3", "Stale text.", "https://x/3")
        _backdate("kalshi", "TICK3", age_s=999_999)
        with patch("evidence._http_get_json", side_effect=_kalshi_responder("Fresh text.")):
            row = evidence.get_evidence("kalshi", "TICK3", max_age_s=1, fetch=True)
        self.assertEqual(row["rules_text"], "Fresh text.")
        self.assertFalse(row["stale"])

    def test_unchanged_text_refresh_counts_as_confirmed_and_stays_fresh(self):
        """Regression for the cache-freshness bug: re-saving the SAME text
        must bump fetched_at (store.save_evidence's "last confirmed"
        semantics), or get_evidence would treat the row as permanently
        expired past the first max_age_s window and refetch on every call
        forever, even though nothing about the rules ever changed."""
        store.save_evidence("kalshi", "TICK7", "Unchanging text.", "https://x/7")
        _backdate("kalshi", "TICK7", age_s=999_999)  # older than any max_age_s below

        # First call: cache is expired, so this must fetch -- and the fetch
        # returns the SAME text, so save_evidence must confirm (bump
        # fetched_at on) the existing row rather than leaving it stale.
        with patch("evidence._http_get_json",
                   side_effect=_kalshi_responder("Unchanging text.")) as mock_fetch:
            row1 = evidence.get_evidence("kalshi", "TICK7", max_age_s=3600, fetch=True)
        self.assertTrue(mock_fetch.called)
        self.assertEqual(row1["rules_text"], "Unchanging text.")
        self.assertFalse(row1["stale"])
        self.assertEqual(_row_count("kalshi", "TICK7"), 1)  # still idempotent, no duplicate

        # Second call, well within max_age_s of the confirm above: must NOT
        # fetch again.
        with patch("evidence._http_get_json",
                   side_effect=AssertionError("should not fetch -- just confirmed")):
            row2 = evidence.get_evidence("kalshi", "TICK7", max_age_s=3600, fetch=True)
        self.assertEqual(row2["rules_text"], "Unchanging text.")
        self.assertFalse(row2["stale"])

    def test_same_text_same_hash_no_duplicate_row(self):
        with patch("evidence._http_get_json", side_effect=_kalshi_responder("Identical text.")):
            row1 = evidence.get_evidence("kalshi", "TICK4", max_age_s=0, fetch=True)
        with patch("evidence._http_get_json", side_effect=_kalshi_responder("Identical text.")):
            row2 = evidence.get_evidence("kalshi", "TICK4", max_age_s=0, fetch=True)
        self.assertEqual(row1["content_hash"], row2["content_hash"])
        self.assertEqual(_row_count("kalshi", "TICK4"), 1)

    def test_changed_text_creates_new_row(self):
        with patch("evidence._http_get_json", side_effect=_kalshi_responder("Version one.")):
            row1 = evidence.get_evidence("kalshi", "TICK5", max_age_s=0, fetch=True)
        with patch("evidence._http_get_json", side_effect=_kalshi_responder("Version two.")):
            row2 = evidence.get_evidence("kalshi", "TICK5", max_age_s=0, fetch=True)
        self.assertNotEqual(row1["content_hash"], row2["content_hash"])
        self.assertEqual(_row_count("kalshi", "TICK5"), 2)
        # latest_evidence reports the newest fetch.
        latest = store.latest_evidence("kalshi", "TICK5")
        self.assertEqual(latest["rules_text"], "Version two.")

    def test_fetch_failure_falls_back_to_stale_cache(self):
        store.save_evidence("kalshi", "TICK6", "Cached text.", "https://x/6")
        _backdate("kalshi", "TICK6", age_s=999_999)
        with patch("evidence._http_get_json", side_effect=RuntimeError("network down")):
            row = evidence.get_evidence("kalshi", "TICK6", max_age_s=1, fetch=True)
        self.assertIsNotNone(row)
        self.assertEqual(row["rules_text"], "Cached text.")
        self.assertTrue(row["stale"])

    def test_fetch_failure_with_no_cache_returns_none(self):
        with patch("evidence._http_get_json", side_effect=RuntimeError("network down")):
            row = evidence.get_evidence("kalshi", "NEVER-CACHED", fetch=True)
        self.assertIsNone(row)

    def test_polymarket_fetch_and_cache(self):
        with patch("evidence._http_get_json",
                   side_effect=_poly_responder("Some description.", question="Q?")):
            row = evidence.get_evidence("polymarket", "0xabc", max_age_s=0, fetch=True)
        self.assertIn("Some description.", row["rules_text"])
        self.assertIn("Q?", row["rules_text"])
        self.assertEqual(row["source_url"], "https://polymarket.com/event/some-event")

    def test_polymarket_falls_back_to_event_description_when_market_description_empty(self):
        call_log = []

        def _fake(url, params=None):
            call_log.append(url)
            if "/events" in url:
                return [{"description": "Event-level description."}]
            return [{
                "question": "Q?",
                "description": "",
                "resolutionSource": "",
                "events": [{"slug": "some-event"}],
            }]

        with patch("evidence._http_get_json", side_effect=_fake):
            row = evidence.get_evidence("polymarket", "0xdef", max_age_s=0, fetch=True)
        self.assertIn("Event-level description.", row["rules_text"])
        self.assertTrue(any("/events" in u for u in call_log))

    def test_unknown_venue_raises(self):
        with self.assertRaises(ValueError):
            evidence.get_evidence("nasdaq", "X")


class LazyDBInitialization(unittest.TestCase):
    def test_get_evidence_on_uninitialized_db_initializes_schema(self):
        """Verify that get_evidence lazy-initializes the DB schema
        on the first call, without requiring explicit store.init_db()."""
        # Clear the module-level initialized paths to simulate a fresh process.
        evidence._initialized_db_paths.clear()

        # Mock _http_get_json to return a response so we don't hit the network.
        with patch("evidence._http_get_json", side_effect=_kalshi_responder("Some rules.")):
            row = evidence.get_evidence("kalshi", "TICK_FRESH", max_age_s=0, fetch=True)

        # Verify the call succeeded and stored data.
        self.assertIsNotNone(row)
        self.assertEqual(row["rules_text"], "Some rules.")

        # Verify the DB was initialized by checking we can query it.
        latest = store.latest_evidence("kalshi", "TICK_FRESH")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["rules_text"], "Some rules.")

    def test_get_evidence_idempotent_multiple_calls_after_lazy_init(self):
        """Verify that after the first call initializes the DB, subsequent
        calls work without re-initializing."""
        evidence._initialized_db_paths.clear()

        # First call initializes with kalshi.
        with patch("evidence._http_get_json", side_effect=_kalshi_responder("First.")):
            row1 = evidence.get_evidence("kalshi", "TICK1", max_age_s=0, fetch=True)
        self.assertEqual(row1["rules_text"], "First.")

        # Second call uses the initialized DB with polymarket.
        with patch("evidence._http_get_json", side_effect=_poly_responder("Second.")):
            row2 = evidence.get_evidence("polymarket", "0xabc", max_age_s=0, fetch=True)
        self.assertIsNotNone(row2)
        self.assertIn("Second.", row2["rules_text"])

    def test_get_evidence_handles_db_path_change_within_process(self):
        """Verify that when PRED_DASHBOARD_DB changes within a process,
        get_evidence lazily initializes the new DB path without error.

        This simulates conftest's per-test DB isolation and tools that
        switch DB paths per case."""
        import os
        import tempfile

        evidence._initialized_db_paths.clear()

        # First call on the test's default tmp DB.
        with patch("evidence._http_get_json", side_effect=_kalshi_responder("First DB.")):
            row1 = evidence.get_evidence("kalshi", "TICK_A", max_age_s=0, fetch=True)
        self.assertEqual(row1["rules_text"], "First DB.")
        first_db_path = str(store.db_path())
        self.assertIn(first_db_path, evidence._initialized_db_paths)

        # Switch to a new un-initialized DB path (simulating a new test case or tool switch).
        with tempfile.TemporaryDirectory() as tmpdir:
            new_db_path = os.path.join(tmpdir, "different-db.db")
            old_db_path = os.environ.get("PRED_DASHBOARD_DB")
            os.environ["PRED_DASHBOARD_DB"] = new_db_path
            try:
                # Verify the path changed.
                self.assertNotEqual(str(store.db_path()), first_db_path)

                # Second call on the new DB should work without "no such table" error.
                with patch("evidence._http_get_json", side_effect=_kalshi_responder("Second DB.")):
                    row2 = evidence.get_evidence("kalshi", "TICK_B", max_age_s=0, fetch=True)
                self.assertIsNotNone(row2)
                self.assertEqual(row2["rules_text"], "Second DB.")

                # The new path should now be in the initialized set.
                second_db_path = str(store.db_path())
                self.assertIn(second_db_path, evidence._initialized_db_paths)
                # Both paths should be tracked.
                self.assertEqual(len(evidence._initialized_db_paths), 2)
            finally:
                # Restore the original DB path.
                if old_db_path is not None:
                    os.environ["PRED_DASHBOARD_DB"] = old_db_path
                else:
                    os.environ.pop("PRED_DASHBOARD_DB", None)


if __name__ == "__main__":
    unittest.main()

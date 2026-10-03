from __future__ import annotations

import unittest

from tools.audit_ranked_signals import run


class RankedSignalAuditTest(unittest.TestCase):
    def test_event_catalog_audit_is_ranked_and_provenanced(self) -> None:
        observed: dict = {}
        pairs = [
            {"poly_id": "pm-a", "kalshi_ticker": "K-A", "category": "politics",
             "match_source": "text", "v2_reasons": ["same predicate"], "books_live": True},
            {"poly_id": "pm-b", "kalshi_ticker": "K-B", "category": "sports",
             "match_source": "sports", "v2_reasons": [], "books_live": True},
        ]

        def fake_discover(**kwargs):
            observed["discover"] = kwargs
            kwargs["coverage"].update({"kalshi": {"open_markets": 10}})
            return pairs

        def fake_signals(actual_pairs, **kwargs):
            observed["signals"] = kwargs
            self.assertIs(actual_pairs, pairs)
            # Deliberately provide reverse economics order; audit owns ranking.
            return [
                {"key": "pm-a|K-A|a", "exec_net": 0.10, "net_accurate": 0.11,
                 "direction": "a"},
                {"key": "pm-b|K-B|b", "exec_net": 0.20, "net_accurate": 0.20,
                 "direction": "b"},
            ]

        result = run(top_n=1, discover_fn=fake_discover, compute_signals_fn=fake_signals)

        self.assertEqual(result["scan_mode"], "event-catalog-only")
        self.assertEqual(result["coverage"]["kalshi"]["open_markets"], 10)
        self.assertEqual(result["pair_count"], 2)
        self.assertEqual(result["depth_backed_signal_count"], 2)
        self.assertEqual(result["signals"][0]["key"], "pm-b|K-B|b")
        self.assertEqual(result["signals"][0]["category"], "sports")
        self.assertEqual(observed["discover"]["market_sweep"], False)
        self.assertEqual(observed["discover"]["sweep_cache_ttl"], 0)
        self.assertEqual(observed["signals"], {
            "min_edge": 0.0, "require_v2": True, "max_edge": 1.0, "min_size": 20.0,
        })

    def test_orphan_sweep_mode_requires_fresh_results_from_both_venues(self) -> None:
        def fake_discover(**kwargs):
            kwargs["coverage"].update({
                "kalshi": {"sweep": "fresh"}, "polymarket": {"sweep": "fresh"},
            })
            return []

        result = run(with_orphan_sweep=True, discover_fn=fake_discover)

        self.assertEqual(result["scan_mode"], "orphan-sweep-complete")

    def test_partial_or_failed_orphan_sweeps_are_not_described_as_complete(self) -> None:
        for poly_status in ("failed", "cached", None):
            with self.subTest(polymarket_sweep=poly_status):
                def fake_discover(poly_status=poly_status, **kwargs):
                    kwargs["coverage"].update({
                        "kalshi": {"sweep": "fresh"}, "polymarket": {"sweep": poly_status},
                    })
                    return []

                result = run(with_orphan_sweep=True, discover_fn=fake_discover)
                self.assertEqual(result["scan_mode"], "orphan-sweep-incomplete")

    def test_rejects_non_depth_audit_parameters(self) -> None:
        with self.assertRaises(ValueError):
            run(top_n=0)
        with self.assertRaises(ValueError):
            run(min_size=0)

    def test_with_rules_on_uninitialized_db_initializes_schema(self) -> None:
        """Verify that with_rules=True works on an un-initialized tmp DB
        by calling store.init_db() before discovery."""
        pairs = [
            {"poly_id": "pm-a", "kalshi_ticker": "K-A", "category": "politics",
             "match_source": "text", "v2_reasons": ["same predicate"], "books_live": True},
        ]

        def fake_discover(**kwargs):
            kwargs["coverage"].update({"kalshi": {"open_markets": 10}})
            return pairs

        def fake_signals(actual_pairs, **kwargs):
            return [
                {"key": "pm-a|K-A|a", "exec_net": 0.10, "net_accurate": 0.11,
                 "direction": "a"},
            ]

        def fake_get_evidence(venue, market_id):
            # Both venues return non-None evidence so compare_rules gets text.
            return {"rules_text": f"{venue}_{market_id}_rules", "source_url": "https://example.com"}

        result = run(
            top_n=1, with_rules=True,
            discover_fn=fake_discover, compute_signals_fn=fake_signals,
            get_evidence_fn=fake_get_evidence,
        )

        # Verify the audit completed and has rule_flags and error counts.
        self.assertEqual(len(result["signals"]), 1)
        self.assertIn("rule_flags", result["signals"][0])
        self.assertIn("rule_flag_errors", result)
        self.assertEqual(result["rule_flag_errors"], 0)

    def test_with_rules_handles_evidence_fetch_errors_per_row(self) -> None:
        """Verify that when get_evidence_fn raises for one row, that row
        gets rule_flags=None + rule_flags_error, the audit continues,
        and rule_flag_errors count is incremented."""
        pairs = [
            {"poly_id": "pm-a", "kalshi_ticker": "K-A", "category": "politics",
             "match_source": "text", "v2_reasons": ["same predicate"], "books_live": True},
            {"poly_id": "pm-b", "kalshi_ticker": "K-B", "category": "sports",
             "match_source": "sports", "v2_reasons": [], "books_live": True},
        ]

        def fake_discover(**kwargs):
            kwargs["coverage"].update({"kalshi": {"open_markets": 20}})
            return pairs

        def fake_signals(actual_pairs, **kwargs):
            return [
                {"key": "pm-a|K-A|a", "exec_net": 0.10, "net_accurate": 0.11, "direction": "a"},
                {"key": "pm-b|K-B|b", "exec_net": 0.20, "net_accurate": 0.20, "direction": "b"},
            ]

        def fake_get_evidence_with_error(venue, market_id):
            # Fail for the second market (K-B).
            # Since signals are sorted by exec_net DESC, K-B comes first (rank 1).
            if market_id == "K-B":
                raise RuntimeError("simulated network failure")
            return {"rules_text": f"{venue}_{market_id}_rules", "source_url": "https://example.com"}

        result = run(
            top_n=2, with_rules=True,
            discover_fn=fake_discover, compute_signals_fn=fake_signals,
            get_evidence_fn=fake_get_evidence_with_error,
        )

        self.assertEqual(len(result["signals"]), 2)
        # After sorting by exec_net DESC: K-B (0.20) is rank 1, K-A (0.10) is rank 2.
        # First row (K-B) should have rule_flags=None and rule_flags_error set.
        self.assertIsNone(result["signals"][0]["rule_flags"])
        self.assertIn("rule_flags_error", result["signals"][0])
        self.assertIn("RuntimeError", result["signals"][0]["rule_flags_error"])
        # Second row (K-A) should have rule_flags (no error).
        self.assertIsNotNone(result["signals"][1]["rule_flags"])
        self.assertNotIn("rule_flags_error", result["signals"][1])
        # Summary should count the error.
        self.assertEqual(result["rule_flag_errors"], 1)


if __name__ == "__main__":
    unittest.main()

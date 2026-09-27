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
                def fake_discover(**kwargs):
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


if __name__ == "__main__":
    unittest.main()

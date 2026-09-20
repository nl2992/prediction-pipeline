"""PrefixIndex must be LOSSLESS: every pair with Jaccard >= t is a candidate."""
from __future__ import annotations

import random
import unittest

from matcher import PrefixIndex, _jaccard, _prefix_len


class PrefixLen(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(_prefix_len(0, 0.3), 0)
        self.assertEqual(_prefix_len(5, 0.0), 5)
        self.assertEqual(_prefix_len(1, 1.0), 1)
        # float noise: 0.3 * 10 must not shorten the prefix to 7
        self.assertEqual(_prefix_len(10, 0.3), 8)


class Lossless(unittest.TestCase):
    def test_matches_brute_force(self):
        rng = random.Random(7)
        vocab = [f"t{i}" for i in range(40)] + ["vs", "fc", "2026"] * 10
        for t in (0.15, 0.3, 0.45, 0.6, 1.0):
            items = {i: frozenset(rng.sample(vocab, rng.randint(1, 8))) for i in range(300)}
            probes = [frozenset(rng.sample(vocab, rng.randint(1, 8))) for _ in range(150)]
            idx = PrefixIndex(items, t, probe_sets=probes)
            for q in probes:
                truth = {k for k, v in items.items() if _jaccard(q, v) >= t}
                self.assertTrue(truth <= idx.candidates(q), (t, q))

    def test_prunes_common_tokens(self):
        items = {i: frozenset({"vs", f"team{i}", f"rival{i}"}) for i in range(1000)}
        idx = PrefixIndex(items, 0.5, probe_sets=[frozenset({"vs", "team1", "rival1"})])
        self.assertEqual(idx.candidates(frozenset({"vs", "team1", "rival1"})), {1})


if __name__ == "__main__":
    unittest.main()

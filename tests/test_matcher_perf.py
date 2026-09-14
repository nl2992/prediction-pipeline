"""Hermetic tests for the matcher/discover performance work:

- greedy 1-to-1 selection is deterministic regardless of input/iteration order
  (the PYTHONHASHSEED-dependent tie-break bug fixed alongside the speedups)
- prefix-filter blocking in match_markets() / _match_groups_then_individual()
  is EXACTLY equivalent to a brute-force (no-blocking) reference, including
  match_markets' below-gate threshold-led acceptance path
- the new text-feature caches never leak a mutable object that a caller could
  corrupt for a later, unrelated call
"""
from __future__ import annotations

import random
import unittest

import matcher
from discover import _match_groups_then_individual
from matcher import (
    MatchedPair,
    _close_delta_hours,
    _contract_actions,
    _domains,
    _jaccard,
    _known_orgs,
    _named_entities,
    _numeric_threshold,
    _offices,
    _parties,
    _proper_names,
    _tokens,
    _threshold_equal,
    is_close_time_compatible,
    is_compatible_match,
    match_markets,
)
from pipeline import MarketSnapshot, OrderBook


def _snap(market_id: str, title: str, event_id: str = "e", close_time: str | None = None,
          full_question: str = "") -> MarketSnapshot:
    return MarketSnapshot(
        source="x", market_id=market_id, event_id=event_id, title=title,
        status="open", close_time=close_time, fetched_at="t",
        orderbook=OrderBook(bids=[], asks=[]),
        extra={"full_question": full_question} if full_question else {},
    )


def _brute_force_match_markets(poly_snaps, kalshi_snaps, min_title_similarity=0.30,
                                max_close_delta_hours=72.0, min_token_ratio=0.0):
    """Reference re-implementation of match_markets' heuristic path with NO
    candidate blocking at all (every (poly, kalshi) pair is scored) — used to
    prove the prefix-filter blocking in the real match_markets() never drops
    a pair it should have found, on small synthetic pools where O(n*m) is
    cheap. Mirrors match_markets()'s exact scoring/acceptance logic.
    """
    scored = []
    for p in poly_snaps:
        p_toks = _tokens(p.title)
        p_thr = _numeric_threshold(matcher._snapshot_text(p))
        p_ents = _named_entities(p.title)
        p_dt = matcher._parse_dt(p.close_time)
        for k in kalshi_snaps:
            k_toks = _tokens(k.title)
            sim = _jaccard(p_toks, k_toks)
            if sim < min_title_similarity:
                if p_thr is None:
                    continue
                k_thr = _numeric_threshold(matcher._snapshot_text(k))
                if not (k_thr and _threshold_equal(p_thr, k_thr)):
                    continue
                k_dt = matcher._parse_dt(k.close_time)
                if p_dt is None or k_dt is None:
                    continue
                if abs((p_dt - k_dt).total_seconds()) / 3600.0 > 72.0:
                    continue
                if not (p_ents & _named_entities(k.title)):
                    continue
            if min_token_ratio > 0 and p_toks and k_toks:
                shorter = min(len(p_toks), len(k_toks))
                longer = max(len(p_toks), len(k_toks))
                if shorter / longer < min_token_ratio:
                    continue
            if not is_compatible_match(p, k):
                continue
            if not is_close_time_compatible(p, k):
                continue
            delta_h = _close_delta_hours(p.close_time, k.close_time)
            score = matcher._confidence(sim, delta_h, max_close_delta_hours)
            scored.append((score, p, k))

    scored.sort(key=lambda x: (-x[0], x[1].market_id, x[2].market_id))
    matched_poly, matched_kalshi = set(), set()
    pairs = []
    for score, p, k in scored:
        if p.market_id in matched_poly or k.market_id in matched_kalshi:
            continue
        pairs.append((p.market_id, k.market_id, round(score, 9)))
        matched_poly.add(p.market_id)
        matched_kalshi.add(k.market_id)
    return sorted(pairs)


def _actual_sig(pairs: list[MatchedPair]) -> list[tuple[str, str, float]]:
    return sorted((p.poly.market_id, p.kalshi.market_id, round(p.confidence, 9)) for p in pairs)


class SyntheticPool:
    """A small, varied synthetic poly/kalshi pool for equivalence testing."""

    TOPICS = [
        ("Will the Democratic candidate win the New York governor race in 2026?",
         "Will the Democrat win the NY gubernatorial election 2026?"),
        ("Will the Republican win the Texas senate race 2026?",
         "Will the GOP candidate win Texas Senate 2026?"),
        ("Will the Lakers win the 2026 NBA Finals?",
         "Will the Celtics win the 2026 NBA Championship?"),
        ("Will the Fed hike rates in December 2026?",
         "Will the Federal Reserve cut interest rates in December 2026?"),
        ("Will GTA VI release in 2026?",
         "Will GTA 6 be released before 2027?"),
        ("Will there be a recession in 2026?",
         "Will there be a UK recession in 2026?"),
        ("Will SCOTUS overturn the ruling in 2026?",
         "Will the Supreme Court uphold the ruling in 2026?"),
        ("Who will win the France presidential election 2027?",
         "Will Macron win the French presidential election 2027?"),
        ("Will Bitcoin reach $200k in 2026?",
         "Will BTC hit $200,000 in 2026?"),
        ("Will unemployment exceed 5% in 2026?",
         "Will the jobless rate be above 5.0% in 2026?"),
    ]

    @classmethod
    def build(cls, seed: int) -> tuple[list[MarketSnapshot], list[MarketSnapshot]]:
        rnd = random.Random(seed)
        poly, kalshi = [], []
        for i, (p_title, k_title) in enumerate(cls.TOPICS):
            close = f"2026-{(i % 12) + 1:02d}-15T00:00:00Z"
            poly.append(_snap(f"p{i}", p_title, event_id=f"pe{i}", close_time=close))
            kalshi.append(_snap(f"k{i}", k_title, event_id=f"ke{i}", close_time=close))
        # A few deliberate non-matches (share generic tokens but different
        # subjects) so blocking has real work to prune.
        for i in range(20):
            poly.append(_snap(f"p_noise_{i}", f"Will event number {i} happen in 2026?",
                               event_id=f"pne{i}", close_time="2026-06-01T00:00:00Z"))
            kalshi.append(_snap(f"k_noise_{i}", f"Will thing number {i} occur in 2026?",
                                 event_id=f"kne{i}", close_time="2026-06-01T00:00:00Z"))
        rnd.shuffle(poly)
        rnd.shuffle(kalshi)
        return poly, kalshi


class Determinism(unittest.TestCase):
    """The greedy 1-to-1 passes must give an identical result regardless of
    input ordering (a stand-in for the PYTHONHASHSEED-dependent set-iteration
    order that caused run-to-run drift before the tie-break fix)."""

    def test_match_markets_order_independent(self):
        poly, kalshi = SyntheticPool.build(seed=1)
        base_sig = None
        for perm_seed in range(5):
            rnd = random.Random(perm_seed + 100)
            p_shuffled = poly[:]
            k_shuffled = kalshi[:]
            rnd.shuffle(p_shuffled)
            rnd.shuffle(k_shuffled)
            pairs = match_markets(p_shuffled, k_shuffled, min_title_similarity=0.30)
            sig = _actual_sig(pairs)
            if base_sig is None:
                base_sig = sig
            else:
                self.assertEqual(sig, base_sig, f"order dependence at perm_seed={perm_seed}")

    def test_match_groups_then_individual_order_independent(self):
        poly, kalshi = SyntheticPool.build(seed=2)
        base_sig = None
        for perm_seed in range(5):
            rnd = random.Random(perm_seed + 200)
            p_shuffled = poly[:]
            k_shuffled = kalshi[:]
            rnd.shuffle(p_shuffled)
            rnd.shuffle(k_shuffled)
            pairs = _match_groups_then_individual(k_shuffled, p_shuffled, min_sim=0.30)
            sig = sorted((p.poly.market_id, p.kalshi.market_id, round(p.confidence, 9)) for p in pairs)
            if base_sig is None:
                base_sig = sig
            else:
                self.assertEqual(sig, base_sig, f"order dependence at perm_seed={perm_seed}")

    def test_tied_scores_broken_by_market_id(self):
        # Two Kalshi markets tied in title similarity and close time against
        # one Polymarket market: the winner must be the lexicographically
        # smaller kalshi market_id, deterministically, regardless of the
        # order the candidates were discovered in.
        p = [_snap("p0", "Will X win the election in 2026?", close_time="2026-01-01T00:00:00Z")]
        k = [
            _snap("k_zzz", "Will X win the election in 2026?", close_time="2026-01-01T00:00:00Z"),
            _snap("k_aaa", "Will X win the election in 2026?", close_time="2026-01-01T00:00:00Z"),
        ]
        pairs = match_markets(p, k, min_title_similarity=0.30)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].kalshi.market_id, "k_aaa")
        # Reversed input order must give the same winner.
        pairs2 = match_markets(p, list(reversed(k)), min_title_similarity=0.30)
        self.assertEqual(pairs2[0].kalshi.market_id, "k_aaa")


class PrefixBlockingEquivalence(unittest.TestCase):
    """The prefix-filter candidate blocking in match_markets() must produce
    byte-for-byte the same matched pairs (ids + score) as brute-force scoring
    every (poly, kalshi) pair with no blocking at all."""

    def test_equivalence_on_synthetic_pool(self):
        for seed in range(4):
            poly, kalshi = SyntheticPool.build(seed=seed)
            actual = _actual_sig(match_markets(poly, kalshi, min_title_similarity=0.30))
            expected = _brute_force_match_markets(poly, kalshi, min_title_similarity=0.30)
            self.assertEqual(actual, expected, f"mismatch at seed={seed}")

    def test_equivalence_with_token_ratio_guard(self):
        poly, kalshi = SyntheticPool.build(seed=7)
        actual = _actual_sig(match_markets(poly, kalshi, min_title_similarity=0.30, min_token_ratio=0.40))
        expected = _brute_force_match_markets(poly, kalshi, min_title_similarity=0.30, min_token_ratio=0.40)
        self.assertEqual(actual, expected)

    def test_threshold_led_pair_below_jaccard_gate_is_found(self):
        # Different wording/units/noise tokens keep title Jaccard low, but the
        # exact-dollar-threshold + shared entity ("bitcoin") + close resolution
        # time must still surface this pair via the below-gate acceptance
        # path — exactly the case prefix filtering must NOT be applied to.
        p = _snap("p_btc", "Bitcoin above $150k on Sep 30?", close_time="2026-09-30T23:59:00Z")
        k = _snap("k_btc", "Will BTC be above $149,999.99 on September 30, 2026, at 11:59 PM ET?",
                  close_time="2026-09-30T23:59:00Z")
        # Sanity check: title overlap alone would NOT clear a realistic gate.
        self.assertLess(_jaccard(_tokens(p.title), _tokens(k.title)), 0.45)

        # Surround with a pile of unrelated noise markets sharing generic
        # tokens, so blocking has to do real pruning work around this pair.
        noise_p = [_snap(f"np{i}", f"Will noise topic {i} occur in 2026?",
                          close_time="2026-06-01T00:00:00Z") for i in range(30)]
        noise_k = [_snap(f"nk{i}", f"Will noise item {i} happen in 2026?",
                          close_time="2026-06-01T00:00:00Z") for i in range(30)]

        pairs = match_markets([p] + noise_p, [k] + noise_k, min_title_similarity=0.45)
        sig = {(mp.poly.market_id, mp.kalshi.market_id) for mp in pairs}
        self.assertIn(("p_btc", "k_btc"), sig)

    def test_length_filter_does_not_reject_below_gate_pairs(self):
        # match_markets' threshold-led path must still work even when the two
        # titles are wildly different lengths — a length filter tuned for the
        # Jaccard gate (used in event-group blocking) must NOT be applied to
        # this below-gate acceptance path, or it would wrongly reject pairs
        # like this one before the threshold/entity/time checks ever run.
        p = _snap("p_short", "BTC above $150k?", close_time="2026-09-30T00:00:00Z")
        k = _snap(
            "k_long",
            "Will the price of Bitcoin be recorded above $149,999.99 dollars at the close of "
            "trading on September 30, 2026, in accordance with the official settlement source?",
            close_time="2026-09-30T00:00:00Z",
        )
        # Sanity: the size/wording gap is real — a naive length filter tuned
        # for the Jaccard gate would rule this candidate out.
        self.assertLess(len(_tokens(p.title)) / len(_tokens(k.title)), 0.40)
        pairs = match_markets([p], [k], min_title_similarity=0.45)
        sig = {(mp.poly.market_id, mp.kalshi.market_id) for mp in pairs}
        self.assertIn(("p_short", "k_long"), sig)


class EventGroupPrefixBlockingEquivalence(unittest.TestCase):
    """_match_groups_then_individual's Step-1 event-group Jaccard gate uses
    prefix filtering with no below-gate exception; verify it matches a
    brute-force (no blocking) reimplementation of that same gate."""

    def test_event_group_gate_equivalence(self):
        for seed in range(4):
            poly, kalshi = SyntheticPool.build(seed=seed + 50)
            # Build single-market "events" (event_title == market title) so
            # Step 1's event-group gate is exercised directly and its output
            # is easy to reconstruct with brute force.
            k_etitles = {s.event_id: (s.extra.get("event_title") or s.title) for s in kalshi}
            p_etitles = {s.event_id: (s.extra.get("event_title") or s.title) for s in poly}
            min_sim = 0.30
            expected = set()
            for k_eid, k_et in k_etitles.items():
                k_toks = _tokens(k_et)
                if not k_toks:
                    continue
                for p_eid, p_et in p_etitles.items():
                    p_toks = _tokens(p_et)
                    if _jaccard(k_toks, p_toks) >= min_sim:
                        expected.add((k_eid, p_eid))

            # Reproduce discover._match_groups_then_individual's own
            # candidate-generation block in isolation (not the full greedy
            # matching / outcome step) to isolate the blocking behaviour.
            from matcher import _token_frequencies, _prefix_tokens, _length_compatible
            p_etoks = {eid: _tokens(t) for eid, t in p_etitles.items()}
            k_etoks = {eid: _tokens(t) for eid, t in k_etitles.items()}
            freq = _token_frequencies(list(p_etoks.values()) + list(k_etoks.values()))
            p_index: dict[str, set[str]] = {}
            for p_eid, toks in p_etoks.items():
                for tok in _prefix_tokens(toks, freq, min_sim):
                    p_index.setdefault(tok, set()).add(p_eid)
            actual = set()
            for k_eid, k_toks in k_etoks.items():
                if not k_toks:
                    continue
                candidates = set()
                for tok in _prefix_tokens(k_toks, freq, min_sim):
                    candidates.update(p_index.get(tok, ()))
                for p_eid in candidates:
                    if not _length_compatible(len(k_toks), len(p_etoks[p_eid]), min_sim):
                        continue
                    if _jaccard(k_toks, p_etoks[p_eid]) >= min_sim:
                        actual.add((k_eid, p_eid))

            self.assertEqual(actual, expected, f"mismatch at seed={seed}")


class CacheMutationSafety(unittest.TestCase):
    """The new lru_cache-memoised text->feature helpers must never hand back
    a mutable object that a caller could corrupt and leak into a later,
    unrelated call for the same text. Every one of them is cached, so the
    SAME object is handed out on every call with equal-content text — the
    only way that is safe is if the object is immutable (frozenset / a
    read-only mapping of frozensets), which is what we assert here: mutating
    APIs must not even exist on the returned object."""

    def _assert_frozen_set(self, fn, text):
        result = fn(text)
        self.assertIsInstance(result, frozenset)
        with self.assertRaises(AttributeError):
            result.add("__poisoned__")
        # Cached: a second call with equal-content text returns an object
        # that is unaffected by anything the first caller could have done.
        self.assertEqual(fn(text), result)

    def test_domains_result_is_frozen(self):
        self._assert_frozen_set(_domains, "Will the Fed hike rates in 2026?")

    def test_offices_result_is_frozen(self):
        self._assert_frozen_set(_offices, "Will the governor win?")

    def test_parties_result_is_frozen(self):
        self._assert_frozen_set(_parties, "Will the Democratic candidate win?")

    def test_contract_actions_result_is_frozen(self):
        self._assert_frozen_set(_contract_actions, "Will there be a rate hike in 2026?")

    def test_proper_names_result_is_frozen(self):
        self._assert_frozen_set(_proper_names, "Will Andy Beshear win the election?")

    def test_known_orgs_result_is_frozen(self):
        self._assert_frozen_set(_known_orgs, "Will Anthropic release Claude 6?")

    def test_tokens_is_frozen_and_cached(self):
        # _tokens returns a frozenset (inherently immutable) and repeated
        # calls with equal-content strings must hit the same cached object.
        a = _tokens("Will BTC hit $150k?")
        b = _tokens("Will BTC hit $150k?")
        self.assertIsInstance(a, frozenset)
        self.assertEqual(a, b)

    def test_comparison_bounds_mapping_and_sets_are_immutable(self):
        from matcher import _comparison_bounds
        bounds = _comparison_bounds("under 5% but over 2%")
        with self.assertRaises(TypeError):
            bounds["lt"] = frozenset()  # the mapping itself must reject writes
        with self.assertRaises(AttributeError):
            bounds["lt"].add("__poisoned__")  # and so must each value set

    def test_stat_thresholds_mapping_and_sets_are_immutable(self):
        from matcher import _stat_thresholds
        stats = _stat_thresholds("2+ assists and 10+ points")
        with self.assertRaises(TypeError):
            stats["points"] = frozenset()
        with self.assertRaises(AttributeError):
            stats["points"].add("__poisoned__")


if __name__ == "__main__":
    unittest.main()

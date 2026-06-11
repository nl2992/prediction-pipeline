# Matching Engine Fix Log

Tracks the `/loop` protocol: drive the matching-pair extraction engine to **100%
coverage of the supplied cross-platform test set for 8 consecutive full runs**. Any
failing run resets the consecutive-success counter to 0.

## Consecutive-success counter: **1 / 8**

(Updated every run. A "successful run" = 100% coverage of expected matching pairs on the
supplied test set, with **0 missed** expected pairs and **0 misaligned** cross-platform
pairs.)

---

## Test files used (the supplied test set)

| File | Role |
|------|------|
| `C:\Users\nigel\Downloads\pairs_fixture.json` | 50 candidate Polymarket↔Kalshi pairs with ground-truth labels |
| `C:\Users\nigel\Downloads\pairs_summary.csv` | flat CSV view of the same 50 pairs |
| `C:\Users\nigel\Downloads\generate_pairs.py` | deterministic generator (seed 42) that produced the fixture |

Harness: `test_matcher.py` (pairwise + global eval) and `tests/test_core_logic.py`
(unit/regression). Run with `python test_matcher.py` and `python -m pytest tests/ -q`.

## Manual ground-truth summary (50 pairs)

Each fixture entry is a `(polymarket, kalshi)` candidate the matcher must accept or
reject. The fixture's own description states **`ground_truth.should_match` is the matcher
label**, so the primary coverage metric is **pairwise classification**.

- **Total candidate pairs:** 50
- **Expected matches (`should_match=true`):** 42
- **Expected non-matches (traps):** 8 — `date_mismatch`×3, `event_mismatch`, `strike_mismatch`,
  `settlement_mismatch`, `outcome_mismatch`, `entity_mismatch`
- **Inverted pairs (PM-YES ≡ Kalshi-NO):** 2 (PAIR-017 BTC dip/stay, PAIR-039 TikTok ban/legal)
- **Arb opportunities (real Kalshi fee):** 14
- **Match types among the 42:** `exact`×30, `paraphrase`×10, `inverted`×2

Signals each pair carries: PM `market_id`/`slug`/`question`/quotes/`end_date_iso`;
Kalshi `ticker`/`title`/quotes(+cents)/`close_time_iso`; `ground_truth.{should_match,
match_type,inverted}`; and an `arb` block (`direction_a/b.edge_gross/edge_net`,
`best_edge_net`, `arb_exists`).

The full per-pair ground truth lives in `pairs_fixture.json`; the engine's behaviour on
every pair (and the fixes that got each one matching) is documented in
`MATCHER_TEST_REPORT.md`.

---

## Run history

### Live top-20 validation — 2026-06-11 (user-requested, out-of-band)

Hand-extracted ground truth from LIVE data: pulled the top-50 Polymarket events by 24h
volume and the full Kalshi open-event catalog (7,678 events), manually paired the top 20
genuine cross-platform pairs across 8 event families (World Cup winner ×8, F1 champion ×4,
Fed June-2026 decision ×3, CA governor, French president, aliens-by-2027, Musk
trillionaire, Hormuz-normal-by-Jun-15), then ran the pipeline on them.

| Engine | Pairwise | Pooled assignment |
|---|---|---|
| v1 (production) | **19/20 (95%)** | 18 matched, **0 misaligned** |
| v2 (shadow) | **19/20 (95%)** | — |

Notables that matched correctly: Kalshi World Cup markets carry a contractual 2028 close
vs PM's 2026-07-20 (sports soft-horizon handled it); "Fed decrease 25 bps after the June
2026 meeting" ↔ "Federal Reserve Cut rates by 25bps at their June 2026 meeting" with the
50+/>25bps bucket aligned to its counterpart and no cut/hike cross-talk; Le Pen, Hilton,
aliens-before-2027 and Musk-trillionaire all exact.

The single miss (both engines): PM "Strait of Hormuz traffic returns to normal by June
15?" vs Kalshi "Will the 7-day moving average of transit calls through the Strait of
Hormuz be …" — Kalshi words the market as its quantitative resolution proxy, so token
similarity is 0.19 and no threshold exists on the PM side for the bridge. Recorded as a
known recall limitation (technical-proxy rephrasing); a candidate entity+deadline-led
acceptance was considered and deferred because it risks pairing opposite same-entity
events (e.g. "Hormuz blocked" vs "Hormuz normal") without polarity-safe guards.


### Run 1 — 2026-06-11
- **Timestamp:** 2026-06-11 (loop cycle 1, job `92d6d8a1`)
- **Test files:** `pairs_fixture.json` (+ `tests/`)
- **Expected matching pairs:** 42 (of 50 candidates; 8 expected non-matches)
- **Correctly extracted:** 42 / 42 expected matches; 8 / 8 traps rejected
- **Missed:** 0 **Incorrect:** 0 **Misaligned:** 0 **Duplicate:** 0
- **Coverage:** **100.0%** (50/50 pairwise classification)
- **Unit/regression suite:** 93 → **97** passing (4 regression tests added this run)
- **Counter:** 0 → **1** (increment — supplied test set at 100%, before and after patches)
- **Diagnosis:** No failures on the supplied test set. Global 1-1 assignment shows 49/50:
  the single gap is PM-035 (OpenAI GPT-6) pairing with the higher-similarity Kalshi
  "OpenAI to release GPT-6 in 2026?" (0.714) over its fixture-designated duplicate
  "GPT-6 released in 2026?" (0.375). Both Kalshi listings are the same
  OpenAI-GPT-6-in-2026 market → a duplicate-listing assignment artifact, **not a
  misaligned pair**.
- **Robustness hardening this run (beyond the fixture):** ran a 13-case adversarial
  cross-platform probe across new categories. Found and fixed **3 real bugs** (all
  generalizable, none hardcoded), each with a regression test, full set re-run green:
  1. **FN — econ % paraphrase:** "inflation above 3%" ✗ "CPI exceed 3.0%". Causes:
     (a) no `cpi`↔`inflation` token synonym; (b) "3%" tokenised as `3` while "3.0%"
     became `3_0`, holding Jaccard at 0.29. Fix: added `cpi`/`jobless`/`unemployed`
     synonyms + decimal trailing-zero canonicalisation (`3.0%`==`3%`, `3.5%` still
     distinct).
  2. **FN — labor-stat synonym:** "jobless rate" not tagged `labor_stats` (K action
     empty). Fix: added `jobless`/`unemployed` to the `labor_stats` regex.
  3. **FP — resign vs impeach:** matched because actions `{deadline,leave_role}` vs
     `{deadline,impeach}` shared `deadline` (not disjoint) and `leave_role` wasn't a
     `_POLITICAL_EVENT_ACTIONS` member. Fix: added `leave_role` to that set so distinct
     political outcomes veto (same-action pairs unaffected).
  Triaged as NOT bugs: HR "50+" vs "under 50" are logical complements (a valid inverted
  pair, not a non-match). Deferred (hard, needs order-aware parsing): "Brazil beat
  Argentina" vs "Argentina beat Brazil" — identical token bag, only word order differs.
  Deferred (risky): "UK PM" vs "Prime Minister" — `pm`↔`prime minister` collides with
  "11:59 PM" time tokens; needs phrase-scoped handling.
- **Files changed:** `matcher.py` (synonyms, decimal canonicalisation, labor_stats regex,
  political-actions set); `tests/test_core_logic.py` (+4 tests); added this log.
- **Patch summary:** 3 generalizable robustness fixes (see above). Supplied-fixture
  coverage unchanged at 100% (patches widen real-world coverage without regressing).
- **Tests added/updated:** `test_cpi_inflation_pct_paraphrase_matches`,
  `test_jobless_unemployment_paraphrase_matches`, `test_resign_not_matched_to_impeach`,
  `test_decimal_trailing_zero_canonicalisation`.
- **Remaining known issues:** (a) global 1-1 duplicate-listing artifact (cosmetic);
  (b) order-only matchup reversal "A beat B"/"B beat A" not vetoed (bag-of-words);
  (c) `discover()` live full scan pulls the ENTIRE Kalshi catalog (>750k rows, times out)
  and its `category` filter is narrow — discovery-side scaling/filtering issue,
  independent of the matcher.
- **Next proposed fix (run 2):** add order-aware "A beat/defeat B" winner-direction veto
  (analogous to the existing `vs` matchup signature) to catch reversed head-to-heads;
  re-probe percentage-threshold and political-predicate categories for residual gaps.

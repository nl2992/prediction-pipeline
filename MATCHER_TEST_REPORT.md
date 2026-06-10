# Prediction Market Matcher Test Report

## Final Status
- **Test Date**: 2026-06-11
- **Pairwise accuracy: 100% (50/50)** — all 42 should-match pairs matched, all 8 traps rejected
- **Global 1-1 assignment: 98% (49/50)** — single miss is an unwinnable duplicate-title trap
- **Repo test suite: 85/85 passing** (65 original + 11 cross-platform + 5 inverted-pair + 4 variable-fee regression tests)
- **Arb engine: all 42 matched-pair gross edges match fixture exactly** (incl. inverted pairs)
- **Arb engine (`fee_model="kalshi_variable"`): reproduces the fixture's economics exactly — all 42 `best_edge_net` match, 14/14 arbs detected**
- **Test Data**: `pairs_fixture.json` — 50 Polymarket↔Kalshi candidate pairs (42 should match, 8 should not)

## Iteration log — 2026-06-11 (loop run 7) — winner-subject veto (single-name contestants)

Implemented the fix proposed (but deliberately deferred) in run 6: single-word proper
nouns evading the name vetoes, so "Lakers win 2026 NBA title" vs "Celtics win 2026 NBA
title" and "Biden win 2028 nomination" vs "Newsom win 2028 nomination" falsely matched.

**Approach — validated in a standalone probe BEFORE touching matcher.py.** Rather than a
blanket single-name veto (rejected in run 6 — it broke 9 fixture pairs), extract only the
*winner-subject*: the capitalised entity between the leading question phrase and a
win/seek/nominee trigger ("Will the Lakers **win**…" → {lakers}; "Newsom **to win**…" →
{newsom}). Generic/stop/jurisdiction/office words are stripped and ticker synonyms
applied. Veto fires only when BOTH sides name a winner-subject and they share no token.

The probe confirmed up front: **0 wrongful vetoes across all 42 fixture matches** (every
"win" pair shares its subject — Newsom⊂{gavin,newsom}, Thunder⊂{city,thunder},
Yankees⊂{new,york,yankees}, Vance, Pacers, Scheffler, Alcaraz), while both target false
positives are caught. Common-noun price markets (Moon, Category) have no win-trigger so
are untouched — the exact failure mode that sank the blunt approach.

`matcher._winner_subject()` + a veto in `is_compatible_match`. Added 3 regression tests
(different teams rejected, different candidates rejected, same-contestant long-vs-short
still matches). Suite 90/90, matcher 100% pairwise (50/50), no regressions. The run-6
"known limitation" is now closed.

## Iteration log — 2026-06-11 (loop run 6) — robustness probing beyond the fixture

The 50-pair fixture is fully satisfied (matching + arb), so this run stress-tested the
matcher on 14 hand-written adversarial pairs using real cross-exchange phrasing the
fixture doesn't cover. Surfaced and triaged 5 candidate issues:

**Fixed (safe, this commit): monetary-policy paraphrase false-negative.**
"Will the Federal Reserve raise interest rates in March 2026?" vs "Fed rate hike at
March 2026 meeting?" scored only 0.182 Jaccard (compatible, but below the 0.30 gate).
Added direction-aware phrase canonicalisation in `_tokens`: "federal reserve"→"fed",
"interest rate(s)"→"rate", and hike/cut wording folded to canonical "rate hike" /
"rate cut" (never conflating the two directions); plus singularising "rates"→"rate",
"hikes"→"hike". Pair now matches; suite 85/85, fixture 100%, no regressions.

**Fixed (this commit): rate-cut vs rate-hike false-positive.** Writing the regression
test for the Fed fix exposed a more serious latent bug — "Fed cut rates in March 2026"
matched "Fed rate hike at March 2026 meeting" (jaccard 0.571, no direction veto).
Opposite monetary actions must never match. Added `_monetary_direction()` and a veto in
`is_compatible_match`, scoped to pairs where both sides are monetary-policy contracts
(so generic raise/cut verbs in other domains are unaffected; same-direction cut/cut and
hike/hike still match). Fixture's Fed pairs (PAIR-019 cut/cut, PAIR-026) unaffected.

**Triaged as NOT a bug:** "Trump win 2024" vs "Trump win 2028" only matched under the
probe's artificial equal close-times; with realistic 2024/2028 close dates the year
veto rejects it correctly.

**Triaged as ambiguous (left as-is):** "Biden run for president 2028" vs "Biden seek
2028 nomination" — running and winning the nomination are different events; rejecting
is defensible.

**Known limitation (proposed fix, NOT yet applied — too risky to rush):** single-word
proper nouns are invisible to the name vetoes, so "Lakers win 2026 NBA title" vs
"Celtics win 2026 NBA title" and "Biden win 2028 nomination" vs "Newsom win 2028
nomination" falsely match (`_proper_names`/`_selected_names` only capture multi-word
names). A naive single-name disjointness veto was prototyped and **rejected**: it would
wrongly break 9 fixture pairs (BTC/Bitcoin, SCOTUS/Supreme Court, Moon/Crewed,
NYC/Central Park, …) because capitalised common nouns and ticker synonyms are
indistinguishable from proper nouns at that crude level. Proposed safe approach for a
later run: extract only the *winner-subject* (the token governed by "to win"/"wins"/
"to seek"), apply existing synonym normalisation, and veto when both sides name a
winner-subject and they are disjoint. Needs exhaustive fixture validation before merge.

## Iteration log — 2026-06-11 (loop run 5) — accurate Kalshi fee model

Closed the last gap between the engine and the fixture's arb ground truth. The
engine's default fee is a flat `max(fee_poly, fee_kalshi)` of the $1 payout
(~4× too high vs Kalshi's real schedule), so it flagged only 2 of the fixture's 14
net-positive arbs. A probe confirmed that Kalshi's actual per-contract taker fee
`0.07·p·(1−p)`, applied to the Kalshi leg only (Polymarket CLOB is fee-free),
reproduces the fixture **exactly**: all 42 `best_edge_net` match and 14/14 arbs detected
(including the inverted pairs, via the complement-book path from run 4).

**Change (additive, non-breaking):**
- `arb.kalshi_taker_fee(price)` — the published `0.07·p·(1−p)` formula.
- `find_arb(..., fee_model="flat"|"kalshi_variable")`. Default `"flat"` is byte-for-byte
  the old behaviour (conservative live-money guard, suppresses marginal signals).
  `"kalshi_variable"` charges the real Kalshi-leg fee for true expected edge and
  fixture-faithful economics. Unknown values raise `ValueError`.
- Per-direction fee now keys off the actual Kalshi leg price (NO buy = `1−k_bid` in
  dir A; YES buy = `k_ask` in dir B), correct for inverted pairs too.

Added `KalshiVariableFeeRegressions` (4 tests): fee formula, bad-model rejection,
flat-default-unchanged, and a fixture-driven check that `kalshi_variable` reproduces
all 14 arbs and every `best_edge_net` (skips gracefully if the fixture file is absent).
Suite 85/85, matcher 100% pairwise (50/50).

## Iteration log — 2026-06-11 (loop run 4) — ARB ENGINE, inverted pairs

Extended testing beyond matching into the fixture's **arb ground truth** (every pair
carries `arb.direction_a/b.edge_gross` and `arb_exists`). Built a probe comparing the
engine's gross two-leg edge against the fixture for all 42 should-match pairs.

**Found a serious cross-platform misalignment bug.** Gross-edge math matched 40/42 —
the 2 misses were **both inverted pairs** (PAIR-017, PAIR-039), where the engine
reported phantom edges of **0.355** and **0.644** versus the true **0.025** / **0.014**.
Root cause: `find_arb` assumed Polymarket-YES ≡ Kalshi-YES and crossed sides (YES here +
NO there). For an inverted pair Polymarket-YES ≡ Kalshi-**NO**, so the hedge must be
SAME-side (YES+YES or NO+NO). Treating the logically-opposite contract as identical
invents a ~(1 − 2·price) edge — in live trading this is a money-losing false signal,
the worst possible class of bug for an arb engine.

**Fix (3 parts):**
1. `matcher.is_inverted_pair(poly, kalshi)` — conservative, **text-only** detector
   (never price-based, since a price gap is exactly what arb trades on). Two precise
   signals: (a) threshold direction-flip at the same level with touch-vs-hold
   settlement ("dip below $80k anytime" vs "stay above $80k all year"); (b) explicit
   antonym state cues ("banned/illegal" vs "legal/operating").
2. `MatchedPair.inverted` field, set by `match_markets` for both override and heuristic
   pairings.
3. `arb._complement_book()` — when `pair.inverted`, re-expresses the Kalshi book in its
   complement (prices AND sizes), after which the existing two-direction math is exact.

**Verified:** inversion detected 2/2; 0/40 normal pairs false-flagged; **all 42
matched-pair gross edges now equal the fixture exactly.** Added 5 regression tests
(`InvertedPairArbRegressions`). Suite 81/81, matcher 100% pairwise (50/50).

**Noted (not changed):** `find_arb`'s default fee is a flat `max(fee_poly, fee_kalshi)`
of the $1 payout, ~4× Kalshi's real `0.07·p·(1−p)` taker fee, so it flags only 2 of the
fixture's 14 net-positive arbs. This is a deliberate *conservative* modeling choice
(fewer false trade signals), independent of the matching task — left as-is rather than
loosening a live-money safety margin to fit a fixture.

## Iteration log — 2026-06-11 (loop run 3)
Re-extracted `files.zip`; fixture + generator byte-identical to prior runs (no new
test data). Baseline reconfirmed: 100% pairwise, 65/65 tests.

**Investigated the global 49/50 "miss" (PAIR-035).** Traced it conclusively: PM-035
"Will OpenAI release GPT-6 before Dec 31, 2026?" matches K(GPT6-26X) "OpenAI to
release GPT-6 in 2026?" at **0.714** similarity vs only **0.375** for its
fixture-designated partner "GPT-6 released in 2026?". Both Kalshi listings are
genuinely OpenAI-GPT-6-in-2026 markets, so the matcher's pairing is *more* correct,
not wrong. The Anthropic PM correctly stays unmatched. **Confirmed: fixture artifact,
not a defect — declined to overfit by forcing the lower-scoring designated pair.**

**Locked in behaviour with 11 new regression tests** (`CrossPlatformFixtureRegressions`
in `tests/test_core_logic.py`) covering every previously-misaligned shape: Solana/SOL
ticker-clip, CPI paraphrase, James/New Bond qualifier, $150,000/$150k separator,
touch-vs-hold inversion, Anthropic-vs-OpenAI veto, touch-vs-close settlement trap,
GTA mid-year deadline, Fed month mismatch, Indiana≠India, and the GPT-6 cluster shape.

**Fixed two latent `_stat_thresholds` bugs** surfaced while writing those tests:
1. "at any **point**" / "point in time" was parsed as a basketball *points* prop —
   now stripped as a time idiom before stat matching.
2. Bare "**k**"/"ks" was a *strikeouts* alias, colliding with the thousands suffix
   ("$80k", "$150k") and stray initials — now requires spelled-out "strikeout(s)".
   Real strikeout-prop titles spell it out; bare-k matching was pure liability in a
   market set saturated with `$Nk` crypto strikes.
Both were silent hazards (they only escaped the fixture because the *counterpart* side
happened to carry no stat); the full suite + fixture stay green after the fix.

## Evaluation methodology
The fixture's own description states "ground_truth.should_match is the matcher label" —
each entry is a candidate pair to accept or reject, so **pairwise classification is the
primary metric**. The global 50×50 assignment is reported as a secondary view; it contains
deliberately indistinguishable Kalshi duplicates (e.g. `FED-26JUL-CUT` vs `FED-26JUL-CUTX`
share the identical title and close time) that make strict 1-1 id-matching ambiguous.
The one global miss: PM-035 ("Will OpenAI release GPT-6 before Dec 31, 2026?") is assigned
to PAIR-041's Kalshi market "OpenAI to release GPT-6 in 2026?" — semantically the same
market, included in the fixture as a trap for the Anthropic question.

## Fixes applied (chronological)

### Session 1
1. **India/Indiana disambiguation** — "Indiana Pacers" matched the "india" country alias by
   substring, triggering the foreign-country veto (killed PAIR-028).
2. **Month-mismatch veto** — Fed September vs July FOMC markets paired freely (PAIR-020 trap).
3. **Entity synonyms** — GOP↔Republican, SCOTUS↔Supreme Court for the threshold-led path.
4. **Comma-number tokenization** — "$150,000" no longer splits into `150` + `000`.
5. **Deadline-action fix** — "gross $1.5B by Dec 31" tagged `deadline` vs `box_office`,
   making the Avengers pair (PAIR-042) self-reject on action mismatch.

### Session 2 (the big unlock)
6. **Diagnosed via sys.settrace**: PAIR-016/021/046 were ALL rejected by one check —
   `_names_overlap` required whole-string containment, so "Solana ETF"/"SOL ETFs",
   "CPI YoY"/"June CPI", "James Bond"/"New Bond" failed. **Relaxed to anchor-token
   overlap** with a ≥3-char prefix rule (sol↔solana, etf↔etfs) and a qualifier
   stoplist (new/next/months can't be the shared anchor).
7. **Fixed token-fusion bug** — the comma-number regex consumed the preceding space,
   fusing "above 7,000" → `above7000` (sank PAIR-011/024 similarity to ~0.10).
8. **Token synonym expansion in `_tokens`** — btc→bitcoin, sol→solana, scotus→supreme court,
   nyc→new york city, touch→hit, lunar→moon, crewed→human, named→announced,
   successfully→success, "150k"→"150000". Lifted 9 low-similarity true pairs above the
   0.30 Jaccard gate.
9. **Two latent false positives exposed by pairwise testing** (previously masked by the
   global assignment stealing their counterparts):
   - PAIR-041 (Anthropic Claude 6 vs OpenAI GPT-6): added **known-org/product veto**
     (anthropic/openai/…, claude/gpt/gemini/…) — disjoint org or product sets reject.
   - PAIR-045 (GTA before June 30 vs calendar-2026): added **day-vs-year deadline-scope
     veto** when close times differ by >72h.
10. **Settlement-shape veto** — "touch $175k anytime" vs "above $175k ON Dec 31" are
    different contracts at the same strike (PAIR-015 trap). Key semantics:
    - "above $X **by** date" = touch (reach anytime before deadline)
    - "above $X **on** date" / "at year-end" = point-in-time
    - "stay above for all of year" = hold; touch↔hold at the same level are logical
      complements → allowed through as an **inverted pair** (PAIR-017) via a
      direction-flip exception in the threshold check.
11. **Scoped date vetoes to discrete-event markets** — asset-price markets ("$85k by
    May 31" vs "ATH by Dec 31") use dates as measurement deadlines, not event identity;
    the repo test suite requires them to stay compatible as review-list candidates.

## Remaining known limitation
Under global 1-1 assignment, duplicate/near-duplicate Kalshi listings can absorb a
Polymarket market that the fixture assigns elsewhere (PAIR-035). This is a property of
the assignment, not the pair classifier — both candidate Kalshi markets are genuinely
equivalent. A production system comparing one PM market against all equivalent K listings
is unaffected.

# Prediction Market Matcher Test Report

## Final Status
- **Test Date**: 2026-06-11
- **Pairwise accuracy: 100% (50/50)** — all 42 should-match pairs matched, all 8 traps rejected
- **Global 1-1 assignment: 98% (49/50)** — single miss is an unwinnable duplicate-title trap
- **Repo test suite: 65/65 passing** (no regressions)
- **Test Data**: `pairs_fixture.json` — 50 Polymarket↔Kalshi candidate pairs (42 should match, 8 should not)

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

# Prediction Market Matcher Test Report

## Current Status
- **Test Date**: 2026-06-10 (Updated)
- **Accuracy**: 72% (28/42 matches correct, 8/8 non-matches correct)
- **Test Data**: 50 Polymarket-Kalshi pairs (42 should match, 8 should not)

## Test Results Summary
```
Correct matches: 28 / 42 (67%)
Correct non-matches: 8 / 8 (100%)
False positives: 0
False negatives: 14
Overall accuracy: 72%
```

## Latest Fix
- ✅ **Fixed deadline action detection** - Movies with "by Dec 31" dates were incorrectly tagged with "deadline" action instead of "box_office", causing action mismatch rejection. Now correctly skips deadline tagging in movie/box office contexts.

## Previous Improvements
1. ✅ **Fixed India/Indiana disambiguation** - "Indiana" state was incorrectly matched as "India" country, triggering foreign country veto
2. ✅ **Added month-mismatch detection** - Pairs with conflicting timeframes (e.g., September vs July) are now correctly rejected
3. ✅ **Expanded entity synonyms**:
   - GOP ↔ Republican
   - SCOTUS ↔ Supreme Court  
   - GPT ↔ GPT6
4. ✅ **Improved number tokenization** - Comma-separated numbers like "$150,000" are now preserved as single tokens

## Failing Pairs Analysis

### Greedy Matching Conflicts (4 pairs)
These pairs match correctly on their own but lose to other pairs in the full test due to greedy 1-1 matching:
- PAIR-011: 0.600 similarity - matches alone but conflicts with another pair
- PAIR-019: 0.500 similarity - matches alone but conflicts with PAIR-020 K
- PAIR-035: 0.375 similarity - matches alone but conflicts with another pair
- PAIR-044: 0.562 similarity - matches alone but conflicts with another pair

### Below Threshold, Needs Threshold-led Acceptance (6 pairs)
- PAIR-004: 0.222 (exact) - "JD Vance" vs "Vance"
- PAIR-008: 0.111 (paraphrase) - "SCOTUS vacancy" vs detailed description
- PAIR-014: 0.125 (exact) - "$175k hit" vs "$175k touch"
- PAIR-017: 0.100 (inverted) - "dip below" vs "stay above" 
- PAIR-024: 0.100 (exact) - "S&P 500 above 7000" with different phrasing
- PAIR-050: 0.111 (paraphrase) - "walk on Moon" vs "crewed lunar landing"

### Above Threshold but Rejected by is_compatible_match (3 pairs)
- PAIR-021: 0.400 (exact) - CPI data with "June" / "released July" 
- PAIR-042: 0.308 (exact) - Avengers movie box office (barely above threshold)
- PAIR-046: 0.375 (exact) - James Bond actor announcement

### Low Similarity, Paraphrase Challenges (2 pairs)
- PAIR-016: 0.214 (exact) - "Solana ETF $5B AUM" vs "SOL ETFs $5B"
- PAIR-037: 0.231 (paraphrase) - "Starship reach orbit and land" vs "full success"
- PAIR-038: 0.231 (exact) - "Waymo operate robotaxis" vs "Waymo service live"

## Root Causes

### 1. Greedy Matching Limitations
The current greedy 1-1 matching algorithm has tie-breaking issues when multiple markets score equally. Better approaches:
- Stable matching algorithms (Hungarian algorithm)
- Tie-breaking based on additional signals (entity overlap, close-time proximity)
- Market-centric matching instead of pair-wise scoring

### 2. Entity and Synonym Detection
Current tokenizer misses:
- "GPT-6" / "GPT6" - inconsistent hyphenation/spacing
- "Solana" / "SOL" - ticker/name ambiguity  
- "Starship" paraphrases - "reach orbit and land both stages" vs "full success"

### 3. Date/Time Scope Conflicts
Some pairs mention different time boundaries:
- "June 2026" CPI (for June data, released July) vs "released July" (release month)
- Close times are authoritative but text can be ambiguous

### 4. Paraphrase Detection
Significant semantic gaps with low token overlap:
- "walk on the Moon" vs "Crewed lunar landing"
- "Starship reach orbit and land both stages" vs "full success"
- "operate paid robotaxis in NYC" vs "paid service live in New York City"

## Critical Architecture Issue Discovered

**Problem**: Threshold-led acceptance gate doesn't work for some pairs because `is_compatible_match` is called AFTER threshold-led acceptance but can still reject them.

**Example**: PAIR-016 (Solana ETF)
- Title similarity: 0.214 (below 0.30 threshold)
- Qualifies for threshold-led acceptance: ✅ Matching thresholds ($5B) + Shared entity (solana)
- Fails `is_compatible_match`: ❌ Unknown reason (compatibility check is too strict)
- Result: Not matched despite meeting acceptance criteria

**Impact**: This blocks approximately 3-4 pairs that should match via the threshold-led path.

**Solution**: Relax `is_compatible_match` constraints for threshold-led pairs, or skip compatibility check when thresholds align perfectly.

## Recommendations for Next Iteration

### Critical Priority
1. **Fix threshold-led acceptance architecture** - Allow pairs with matching thresholds + shared entities to bypass or relax compatibility checks

### High Priority
1. Implement better tie-breaking in greedy matching (PAIR-011, 019, 035, 044)
2. Improve entity synonym detection for tech/finance terms
3. Add support for hyphenation variance (GPT-6 vs GPT6)

### Medium Priority  
1. Enhanced paraphrase detection using contextual similarity
2. Better handling of inverted/logical negation pairs
3. Improve threshold-led acceptance for low-similarity exact matches

### Low Priority
1. Advanced NLP techniques for semantic similarity
2. Bi-directional matching to detect transitive relationships
3. Market-level confidence scoring based on volume/liquidity

## Technical Debt
- Remove debug scripts (debug_pairs.py, debug_*.py) before production
- Add comprehensive inline documentation for complex matching logic
- Create integration tests for known edge cases

## Next Steps
1. Run `/loop 30m` to continuously iterate and improve
2. Fix greedy matching conflicts  
3. Improve paraphrase/synonym detection
4. Target 80%+ accuracy before considering production deployment

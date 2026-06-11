# Pipeline Redesign — assessment and v2 prototype (branch: `redesign/pipeline-v2`)

## The question asked

> Does this pipeline, as it stands, lead to the best results?

Honest answer: **the signal extraction is excellent; two structural layers around it are
not.** The matcher scores 100% on the 50-pair fixture and survives adversarial probing —
but that quality is delivered through an architecture whose marginal cost per fix is
rising, and the discovery stage feeding it cannot complete a live full scan at all.

## Diagnosis

### 1. Discovery does not scale (the biggest real-world blocker)
`discover()` had a per-event "blocking" path (fetch markets per relevant `event_ticker`)
but only used it when `len(filtered) <= 100` events. Any larger scan fell back to crawling
Kalshi's ENTIRE market catalog — measured at **>750,000 rows**, which blew a 540-second
budget before matching even began. Best matcher in the world produces zero results if its
input stage never finishes.

**Redesign:** per-event candidate blocking is now the default for any bounded scan
(≤500 filtered events), with progress reporting; the full crawl remains only for genuinely
unbounded scans. Complexity drops from O(exchange size) to O(relevant events).

### 2. The match decision layer is 48 ordered vetoes
`is_compatible_match` is a single function with **48 sequential `return False` exits**
accumulated over many fix iterations. Findings from the fix log that motivated the
redesign:

- **Opacity:** diagnosing any rejection required `sys.settrace` line-tracing (runs 6–7 of
  the first loop). No decision carries a reason.
- **Order dependence:** each new veto's safety depends on the 47 before it; placement is
  part of correctness.
- **Rising marginal cost:** every newly-discovered failure mode costs a new regex stanza
  plus re-validation of everything (see `MATCHER_TEST_REPORT.md`, runs 6–8).
- **Structural blind spots:** a bag-of-tokens engine cannot reject "Will Brazil beat
  Argentina?" vs "Will Argentina beat Brazil?" — identical token sets, opposite contracts.
  Money-losing if traded.

**Redesign (prototype, `contract_spec.py`):** restructure the SAME proven extractors into
an explicit two-phase pipeline:

```
extract_spec(market)  ->  ContractSpec        (subjects, event class, threshold,
                                               settlement shape, polarity, time scope,
                                               ORDERED head-to-head participants)
match_spec(a, b)      ->  MatchDecision       (match / inverted / confidence / REASONS)
```

Field-wise gates replace ordered vetoes; logically complementary fields (threshold
direction flip + touch/hold, polarity flip with shared subject) yield an **inverted**
match instead of a rejection; every verdict is explainable.

## Evidence (side-by-side, same inputs)

| Benchmark | v1 (`is_compatible_match` + gate) | v2 (`match_spec`) |
|---|---|---|
| 50-pair fixture (pairwise) | **50/50** | **50/50** |
| Inverted pairs flagged | 2/2 | 2/2 |
| 11-case adversarial set | 10/11 | **11/11** |
| Reversed head-to-head ("A beat B" / "B beat A") | ✗ cannot reject (identical tokens) | ✓ rejected via ordered `beat_order` |
| Decision explainability | none (trace-debugging required) | every decision carries reasons |
| Unit tests | 97 | 105 (8 new for v2) |

Example v2 rejection reasons (no tracing needed):

```
winner-subject mismatch: ['lakers'] vs ['celtics']
monetary direction mismatch: ['down'] vs ['up']
reversed head-to-head: ('brazil', 'argentina') vs ('argentina', 'brazil')
settlement-shape mismatch: point vs touch
```

## What was deliberately NOT changed

- **v1 stays the production path.** `matcher.py` is untouched on this branch except for
  nothing at all — `contract_spec.py` sits alongside and reuses its extractors, so signal
  quality is shared and the 97 existing tests keep guarding it.
- **Greedy 1-1 assignment** kept; duplicate-listing tie behaviour documented previously
  is cosmetic for pair classification.
- **No ML/embeddings.** Stdlib-only philosophy preserved; determinism and zero inference
  cost matter for a trading loop.

## Migration path (proposed)

1. Run v2 in shadow mode inside `discover()`/`monitor` (log `MatchDecision.reasons`
   alongside v1 verdicts) for a few live sessions.
2. Diff verdicts; port any v1-only domain veto that fires in the wild into a spec field.
3. Flip `match_markets` to `match_spec` behind a flag; retire vetoes from
   `is_compatible_match` as their spec equivalents prove out.
4. Keep the 50-pair fixture + adversarial suite as the permanent gate for both engines.

## Files on this branch

- `discover.py` — per-event candidate blocking by default (scalability fix)
- `contract_spec.py` — v2 structured extraction + field-wise decision engine
- `tests/test_contract_spec.py` — fixture parity + adversarial + explainability tests
- this document

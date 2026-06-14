# Matcher Validation Log

Truthful audit trail for the continuous Kalshi↔Polymarket matching-engine
validation loop. One section per validation run. **No progress is overstated:**
results below are produced by `validate_matcher.py` (joint `match_markets` over a
full curated slice) and `pytest`, and the methodology limitations are stated
explicitly.

## Methodology & honesty notes

- **Source of truth:** `tests/fixtures/pairs_fixture.json` — 50 hand-labelled
  pairs (42 `should_match=true`, 8 mismatch traps, 2 inverted) spanning
  politics, econ, sports, crypto, tech, culture, misc.
- **Curated harness:** `validate_matcher.py` feeds an entire 20-pair slice's
  Polymarket + Kalshi snapshots into `match_markets` *together* and checks
  recovery of the intended 1-to-1 pairing, false positives, wrong counterpart,
  and polarity (`inverted`) vs ground truth.
- **Live harness (added run 2, per user direction):** `validate_live.py` runs the
  organic `discover` scan over current markets and judges each engine-returned
  live pair with the **independent v2 `contract_spec` engine** (shadow mode). A
  v2 rejection of a v1 live pair = candidate false positive. This measures live
  **precision + polarity only** — NOT recall (missed live matches), which has no
  engine-independent ground truth yet.
- **Known limitation (not yet satisfied):** the loop objective asks for **20
  *freshly* extracted *live* pairs each run**. This log currently validates
  against the *curated* fixture, which was authored to be matchable and is
  therefore weak evidence that the engine generalises to live data. Rotating
  slices (offset) vary the tested subset but draw from the same 42-pair pool.
  Live-fresh extraction is an open item (see Backlog). Passing runs below should
  be read as "engine still correct on the labelled regression pool," **not** as
  proof of live correctness.

## Consecutive-success counter

| Calendar day | Consecutive "fully-correct" runs | Target |
|---|---|---|
| 2026-06-14 | **3** (curated 20-pair runs, offsets 0, 21, 28) | 8 |

A run counts toward the streak only if all 20 pairs are Exact, engine match
count == expected match count, and there are no false positives. Any failure
resets to 0. Counter resets at the start of each calendar day.

**Streak integrity note:** only run 1 (curated) currently meets the *full*
definition (precision + recall on a known set). Live runs (run 2 onward) pass
**precision** but cannot yet satisfy the full definition because (a) live recall
is unmeasured and (b) current live cross-platform overlap is category-limited
(see runs below). The counter is therefore held at 1 and **not** inflated by
live precision passes — to be revisited once live recall ground truth exists.

---

## Run 1 — 2026-06-14 (offset 0)

**Result: PASS** — expected matches = 20, engine matches = 20, exact = 20,
false positives = 0, missed = 0. Consecutive-success count after run: **1/8**.

Corroborating robustness sweep (same pool, rotated slices) — all PASS:
`offset 0, 5, 10, 15, 20, 22` each → 20/20 exact, 0 FP, 0 missed.
Full regression: `pytest` → **121 passed**.

### Test set & engine result (offset-0 slice)

| Pair | Category | Expected Kalshi | Engine Kalshi | Exact | Score | Classification |
|---|---|---|---|---|---|---|
| PAIR-001 | politics | DEMNOM28-GN | DEMNOM28-GN | ✅ | 0.800 | Exact match |
| PAIR-002 | politics | SENATE26-R | SENATE26-R | ✅ | 0.611 | Exact match |
| PAIR-003 | politics | HOUSE26-D | HOUSE26-D | ✅ | 0.600 | Exact match |
| PAIR-011 | crypto | BTC-26DEC31-150 | BTC-26DEC31-150 | ✅ | 0.650 | Exact match |
| PAIR-013 | crypto | ETH-26DEC31-8000 | ETH-26DEC31-8000 | ✅ | 0.700 | Exact match |
| PAIR-014 | crypto | BTC-TOUCH-175 | BTC-TOUCH-175 | ✅ | 0.860 | Exact match |
| PAIR-019 | econ | FED-26JUL-CUT | FED-26JUL-CUT | ✅ | 0.800 | Exact match |
| PAIR-021 | econ | CPI-26JUN-3.0 | CPI-26JUN-3.0 | ✅ | 0.580 | Exact match |
| PAIR-022 | econ | RECESSION-26 | RECESSION-26 | ✅ | 0.700 | Exact match |
| PAIR-027 | sports | NBA-26-OKC | NBA-26-OKC | ✅ | 0.650 | Exact match |
| PAIR-028 | sports | NBA-26-IND | NBA-26-IND | ✅ | 0.700 | Exact match |
| PAIR-029 | sports | WC26-BRA | WC26-BRA | ✅ | 0.883 | Exact match |
| PAIR-035 | tech | GPT6-26 | GPT6-26 | ✅ | 0.562 | Exact match |
| PAIR-036 | tech | AAPL-FOLD-26 | AAPL-FOLD-26 | ✅ | 0.562 | Exact match |
| PAIR-037 | tech | STARSHIP-26 | STARSHIP-26 | ✅ | 0.533 | Exact match |
| PAIR-042 | culture | BOX-DOOM-15 | BOX-DOOM-15 | ✅ | 0.515 | Exact match |
| PAIR-043 | culture | TSWIFT-TOUR-27 | TSWIFT-TOUR-27 | ✅ | 0.738 | Exact match |
| PAIR-044 | culture | GTA6-26 | GTA6-26 | ✅ | 0.562 | Exact match |
| PAIR-047 | misc | CANE-26-CAT5 | CANE-26-CAT5 | ✅ | 0.689 | Exact match |
| PAIR-048 | misc | CLIMATE-26-HOT | CLIMATE-26-HOT | ✅ | 0.767 | Exact match |

**Totals:** expected = 20, engine = 20, exact = 20, FP = 0, missed = 0,
polarity errors = 0, counterpart errors = 0.

### Diagnoses
None — no misses or false positives in this run.

### Fixes this run
None required. New harness `validate_matcher.py` added (no behavior change to the
engine). No matcher source changed, so existing behavior is fully preserved.

---

## Run 2 — 2026-06-14 (LIVE, `validate_live.py`)

**Result: PRECISION PASS / definition NOT fully met.** Engine returned **39**
live pairs (≥20 ✔). v2 referee: **agree 39/39, disagree 0, unjudged 0, polarity
flags 0** → zero candidate false positives. No engine fix indicated.

**Honest gaps (why this is not counted toward the strict streak):**
- **Category coverage = election, political only.** The objective wants a diverse
  spread (sports/crypto/econ/entertainment/legal). Current live Kalshi↔Polymarket
  overlap is concentrated in politics; the missing categories are absent from the
  *live data*, not rejected by the engine. Cannot manufacture diversity that the
  market overlap doesn't contain right now.
- **Recall unmeasured.** v2 referees precision only; missed live matches are not
  detectable without engine-independent ground truth.

Full regression unchanged from run 1 (`pytest` 121 passed; no engine source
touched — only new harness added).

### Diagnoses
None — zero false positives, zero polarity conflicts. Category sparsity is a
data-availability finding, not a matcher defect.

### Fixes this run
Added `validate_live.py` (live precision harness). No engine behavior changed.

---

## Run 3 — 2026-06-14 (LIVE + curated offset 21)

**Result: PASS (curated, strict) + precision PASS (live).** Consecutive
fully-correct curated runs after this: **2/8**.

- **Live** (`validate_live.py`): engine pairs = **39** (≥20 ✔); v2 referee
  agree **39/39**, disagree 0, unjudged 0, polarity flags 0 → no false-positive
  candidates. Categories: election, political (unchanged data-availability gap).
- **Curated** (`validate_matcher.py --offset 21 --n 20`): expected 20, engine 20,
  **exact 20/20**, FP 0, missed 0, polarity 0.
- **Regression:** `pytest -q` → **121 passed**.

### Classification
All tested pairs: Exact match. No Missed / False positive / Incorrect polarity /
expiry / resolution / category / counterpart in either harness.

### Defect found & fixed this run
- [x] **`validate_live.py --json` polluted stdout** with `discover()` progress
      lines, breaking `json.load` for any downstream consumer (the cron's own
      step 1 piped `--json` into a parser and crashed with
      `JSONDecodeError`). Fix: in `--json` mode route progress to stderr and emit
      only the JSON document on stdout. Verified: `validate_live.py --json |
      json.load` now parses cleanly (39 pairs).
  - *Test note (honest):* no unit test added — `validate_live` is network-bound
    (calls live `discover`), so a deterministic unit test would require mocking
    the entire scan; the fix is structural (stream separation) and verified by
    re-run. Tracked rather than forced, per loop policy.

### Diagnosis of the defect
Category: harness/tooling I/O bug (not a matcher engine defect). Cause: mixing
human-progress output and machine output on the same stream.

---

## Run 4 — 2026-06-14 (LIVE + curated offset 28) — idle PASS, no commit

**Result: PASS (curated, strict) + precision PASS (live).** Consecutive
fully-correct curated runs after this: **3/8**.

- **Live**: engine pairs = **39** (≥20 ✔); v2 agree **39/39**, disagree 0,
  unjudged 0, polarity 0 → no false-positive candidates. Categories: election,
  political.
- **Curated** (`--offset 28 --n 20`): expected 20, engine 20, **exact 20/20**,
  FP 0, missed 0, polarity 0.
- **Regression:** `pytest -q` → **121 passed**.

Classification: all Exact. No defect found → **no code change → no commit**
(per loop policy; log updated locally only).

---

## Run 5 — 2026-06-14 (RECALL harness build + first live recall probe)

Focus this iteration: build the live-**recall** capability (the gap flagged in
runs 2–4 that the curated/precision harnesses cannot cover). Curated 20-pair run
not executed this iteration → **strict streak unchanged at 3/8** (this was a
tooling-build run, not a fresh validation run; not counted either way).

**New harness `validate_recall.py`** — runs the real production group matcher
(`_match_groups_then_individual`, what `discover` uses) at the production gate
(0.30) and a relaxed gate (0.20) over one fetched live pool set, then referees
the relaxed-only pairs with the independent v2 engine. Enabled by a minimal
additive `discover(..., return_pools=True)` that returns the snapshot pools.

**First live recall result (200-event scope):**
- pools: poly = 25,724, kalshi = 1,092
- production(0.30) = **39** pairs (matches the live loop's count ✔)
- relaxed(0.20) = 39 pairs → **relaxed-only = 0** → v2-endorsed candidate misses
  = **0** → **CLEAN**.
- Interpretation: lowering the similarity gate surfaces no additional
  v2-acceptable pairs, so **no threshold-driven recall gap** is detectable in
  this scan.

**Honesty / scope (explicit):**
- This measures recall loss from the **similarity gate** over discover's
  candidate universe only. It does **not** measure ingestion/blocking recall
  (markets never fetched into the pools) — still open.
- v2 is an imperfect referee. The earlier `match_markets`-based prototype of this
  probe surfaced a spurious "New York Liberty ↔ New York Jets" candidate (v2
  accepted on shared-token "New York"); that prototype was discarded in favour of
  the production-matcher version above. v2 endorsements remain review-candidates,
  not confirmed misses.
- `pytest` after the `discover` change: **121 passed** (additive `return_pools`
  param defaults False; existing tests exercise the unchanged default path).
- *Test note (honest):* `validate_recall` is network-bound (live `discover`); no
  deterministic unit test added (would require mocking the entire scan), same
  policy as `validate_live`. Verified by live run.

### Diagnoses
None — no recall gap or precision defect found this run.

---

## Backlog / open items (unchecked = not done)

- [x] **Live-fresh extraction (precision).** `validate_live.py` pulls live pairs
      via `discover.py` and referees precision/polarity with the v2 engine
      (run 2). Done for precision.
- [x] **Live recall — similarity-gate sensitivity.** `validate_recall.py`
      (run 5) probes recall loss from the production matcher's gate via a
      relaxed-gate diff refereed by v2. First result: CLEAN (no gate-driven
      misses). Covers the gate dimension of recall.
- [ ] **Live recall — ingestion/blocking.** Detect true pairs where one or both
      markets were never fetched/blocked into the pools (e.g. category sparsity,
      keyword-derivation misses). Not covered by the gate probe; the deeper
      remaining recall gap.
- [ ] **Live recall — independent ground-truth anchor set.** A hand-verified set
      of known-correct *current* live pairs (re-resolved against the live catalog
      each run) to measure recall without relying on v2 as referee.
- [ ] **Live category diversity.** Current live overlap is election/political
      only. Track whether sports/crypto/econ/entertainment overlap appears in
      future scans; cannot be forced when the markets don't co-list.
- [ ] **Negative/trap coverage in the harness.** `validate_matcher.py` currently
      tests only `should_match=true` pairs jointly; add the 8 traps so
      false-positive resistance is scored in the same run.
- [ ] Polarity stress: only 2 inverted pairs exist in the pool; add more
      inverted live cases to exercise `is_inverted_pair`.

## Commits
| Run | Commit | Note |
|---|---|---|
| 1 | 751f80f | add validate_matcher.py + this log (pushed to origin/main) |
| 1 | ed23366 | record run-1 commit hash |
| 2 | 452cdbe | add validate_live.py + run-2 live precision results |
| 3 | c8cc1ab | fix validate_live --json stdout pollution + run-3 results |
| 4 | (none — idle PASS) | run-4 log only, no code change |
| 5 | _this commit_ | add validate_recall.py + discover return_pools + run-5 recall probe |

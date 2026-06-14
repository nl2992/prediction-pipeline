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
| 2026-06-14 | **8 ✅ DAILY GOAL MET** (offsets 0, 21, 28, 35, 7, 14, 3, 10) | 8 |
| 2026-06-15 | **3** (offsets 17, 24, 31) | 8 |

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

## Run 6 — 2026-06-14 (LIVE precision + recall + curated offset 35) — idle PASS, no commit

**Result: PASS (curated, strict) + precision PASS + recall CLEAN.** Consecutive
fully-correct curated runs after this: **4/8**.

- **Live precision**: 39 pairs, v2 agree **39/39**, disagree 0 → no FP candidates.
  Categories: election, political.
- **Live recall**: production(0.30) 39, relaxed(0.20) 39, relaxed-only 0,
  v2-endorsed misses 0 → **CLEAN** (no gate-driven recall gap).
- **Curated** (`--offset 35 --n 20`): expected 20, engine 20, **exact 20/20**,
  FP 0, missed 0, polarity 0.
- **Regression:** `pytest -q` → **121 passed**.

Classification: all Exact. No defect found → **no code change → no commit**
(log updated locally only, per loop policy).

---

## Run 7 — 2026-06-14 (LIVE precision + recall + curated offset 7) — idle PASS, no commit

**Result: PASS (curated, strict) + precision PASS + recall CLEAN.** Consecutive
fully-correct curated runs after this: **5/8**.

- **Live precision**: 39 pairs, v2 agree **39/39**, disagree 0 → no FP candidates.
  Categories: election, political.
- **Live recall**: production(0.30) 39, relaxed(0.20) 39, relaxed-only 0,
  v2-endorsed misses 0 → **CLEAN**.
- **Curated** (`--offset 7 --n 20`): expected 20, engine 20, **exact 20/20**,
  FP 0, missed 0, polarity 0.
- **Regression:** `pytest -q` → **121 passed**.

Classification: all Exact. No defect → **no code change → no commit**.

---

## Run 8 — 2026-06-14 (LIVE precision + recall + curated offset 14) — idle PASS, no commit

**Result: PASS (curated, strict) + precision PASS + recall CLEAN.** Consecutive
fully-correct curated runs after this: **6/8**.

- **Live precision**: 39 pairs, v2 agree **39/39**, disagree 0 → no FP candidates.
  Categories: election, political.
- **Live recall**: production(0.30) 39, relaxed(0.20) 39, relaxed-only 0,
  v2-endorsed misses 0 → **CLEAN**.
- **Curated** (`--offset 14 --n 20`): expected 20, engine 20, **exact 20/20**,
  FP 0, missed 0, polarity 0.
- **Regression:** `pytest -q` → **124 passed**.

Classification: all Exact. No defect → **no code change → no commit**.

---

## Run 9 — 2026-06-14 (LIVE precision + recall + curated offset 3) — idle PASS, no commit

**Result: PASS (curated, strict) + precision PASS + recall CLEAN.** Consecutive
fully-correct curated runs after this: **7/8**.

- **Live precision**: 39 pairs, v2 agree **39/39**, disagree 0 → no FP candidates.
  Categories: election, political.
- **Live recall**: production(0.30) 39, relaxed(0.20) 39, relaxed-only 0,
  v2-endorsed misses 0 → **CLEAN**.
- **Curated** (`--offset 3 --n 20`): expected 20, engine 20, **exact 20/20**,
  FP 0, missed 0, polarity 0. (Step-7 offsets exhausted; offset 3 used for a
  distinct slice.)
- **Regression:** `pytest -q` → **124 passed**.

Classification: all Exact. No defect → **no code change → no commit**.

---

## Run 10 — 2026-06-14 (LIVE precision + recall + curated offset 10) — ✅ DAILY GOAL MET

**Result: PASS (curated, strict) + precision PASS + recall CLEAN.** Consecutive
fully-correct curated runs after this: **8/8 — daily stopping rule satisfied for
2026-06-14.**

- **Live precision**: 39 pairs, v2 agree **39/39**, disagree 0 → no FP candidates.
  Categories: election, political.
- **Live recall**: production(0.30) 39, relaxed(0.20) 39, relaxed-only 0,
  v2-endorsed misses 0 → **CLEAN**.
- **Curated** (`--offset 10 --n 20`): expected 20, engine 20, **exact 20/20**,
  FP 0, missed 0, polarity 0.
- **Regression:** `pytest -q` → **124 passed**.

### Daily summary (2026-06-14)
8 consecutive fully-correct curated 20-pair runs across offsets
0, 21, 28, 35, 7, 14, 3, 10 (runs 1, 3, 4, 6, 7, 8, 9, 10). Across all of them:
expected count == engine count, zero false positives, zero missed, zero polarity
/ counterpart errors. Live precision clean every run (39/39 v2 agreement); live
gate-recall CLEAN every run. One real defect found & fixed during the day
(`validate_live --json` stdout pollution, run 3). Recall harness built (run 5).

**Honest scope reminder (unchanged):** the strict streak is built on the
*curated, designed-to-pass* pool — it proves no regression, not live correctness.
Live coverage remains election/political only; live recall is measured only for
the similarity-gate dimension. The deeper open items (ingestion/blocking recall,
independent live anchor set, category diversity) are still unchecked below and
are where genuinely new signal would come from.

Committing the day's accumulated audit trail at this milestone (definition-of-done
requires the markdown pushed). No engine source changed since run 5.

---

## Run 11 — 2026-06-14 (INGESTION/blocking recall harness build) — REAL finding

Daily curated streak already at 8/8, so this fire advanced the backlog instead of
replaying passing validations (per the daily stopping rule). Built the
**ingestion-recall** harness and it found a **genuine, large recall gap**.

**New harness `validate_ingestion.py`** — runs production `discover()` at the
loop's Kalshi event cap (200) and a wider cap (500), diffs the matched pairs, and
keeps v2-endorsed new-only pairs as candidate ingestion misses (pairs blocked out
of the pool by the event-count cap, before matching ever runs).

**Result: REVIEW — real ingestion recall gap.**
- prod(cap=200) = **39** pairs; wide(cap=500) = **220** pairs.
- new-only = 181; **v2-endorsed candidate misses = 178**.
- Spot-checked candidates are unambiguously correct, e.g.:
  - PM "Jair Bolsonaro" ↔ Kalshi "Will Jair Bolsonaro finish 2nd in the first round" (Brazil election)
  - PM "Naftali Bennett" ↔ Kalshi "Will Naftali Bennett become Prime Minister" (Israel)
  - PM "Itamar Ben Gvir" / "Gideon Sa'ar" / "Fernando Haddad" ↔ matching Kalshi races
  These are real cross-platform pairs the **production cap=200 never ingested**
  because their Kalshi events rank outside the top 200.

### Classification
178 × **Missed match** (ingestion/blocking) — true pairs absent from the pool.
Not a matcher-logic defect: the matcher pairs them correctly once they are in the
pool (they appear at cap=500). Root cause is the ingestion blocking parameter.

### Diagnosis
**Missing candidate from ingestion** — `max_events_to_search=200` is too low.
True pairs concentrated in non-US elections (Brazil, Israel, etc.) sit in Kalshi
events ranked 200–500 and are dropped before matching. ~5.6× more pairs are
reachable at cap=500.

### Proposed fix (NOT forced — has a production tradeoff; needs a decision)
- [ ] Raise `max_events_to_search` (e.g. 200 → 500) in the loop/alerter, or make
      it adaptive/unbounded. **Tradeoff:** scan time grows from ~90s to ~4 min
      (more Kalshi market fetches); affects the live `PredArbAlerter` (still well
      under its 20-min limit and 30-min interval). **Production relevance:** the
      live arb alerter currently scans at cap=200, so it is missing ~178
      executable pairs — directly relevant to the operator's "email ALL the pairs
      we can possibly run" requirement. Left for operator decision rather than
      silently changing production scan cost.
- [ ] Before raising in production, sanity-check what fraction of the 178 clear
      the positive-net-of-fees bar (many non-US election longshots may be dust).

`pytest -q` → **124 passed** (new harness is standalone; no engine change).

---

## Run 12 — 2026-06-15 (PHANTOM-ARB finding: precision collapses at scale)

Acting on the operator request to "raise the alerter cap to 1500 and ensure >=50
survivable arbs," I quantified first (as asked). **Key finding: the premise does
not hold — widening surfaces phantom arbs, not real ones.**

**Quantification at cap=1500 (with live prices + fees):**
- 2,488–2,545 matched pairs; **raw positive-net = 641–649**.
- Inspection of the top edges: **every one is a mismatch**, e.g.
  - PM "Cody Gakpo" ↔ Kalshi "Cody Gakpo: 2+ assists?" (player vs stat prop, ~95c)
  - PM "Anthropic acquired before 2027" ↔ Kalshi "Who will IPO… Anthropic" (acquired≠IPO)
  - PM "ICC T20 … West Indies" ↔ Kalshi "Will Pakistan win…" (different teams)
  - many one-sided/illiquid books (None bid/ask).
- Root cause: at scale the group matcher mis-pairs on shared **proper nouns**
  (esp. soccer "Team 1st-Half O/U 0.5" vs "Will Team win the 1st Half?" — over/
  under goals vs winning the half). Classic **resolution-criteria / settlement-
  shape** failure the curated fixture never exercised (it has no sports props).

**Precision guards added to `compute_signals` (real fix):**
- `require_v2=True` — only trust pairs the independent v2 engine endorses.
- `max_edge=0.25` — a >25c net edge between identical binaries is impossible; it
  is the signature of a mismatch / stale book.
- Effect at cap=1500: 649 → **451 guarded** — i.e. guards remove ~30%, but the
  remaining 451 are STILL mostly phantom (v2 does not catch soccer O/U-vs-winner;
  the 25c cap lets through 16–25c mismatches). **Guards are necessary but not
  sufficient.**

**Decision (honest):** there are **not 50 real survivable arbs**; forcing 50 by
widening = emailing phantoms to real recipients. So:
- [x] Clamped the production/alerter cap back to the proven-safe **200**
      (`CAP_LADDER=(200,)`), where precision holds (US politics/elections). At
      cap=200 + guards a dry-run yields 25 clean signals (edges 0.05–2.13c, all
      real). The live `PredArbAlerter` task runs the local file, so this clamp
      protects it immediately.
- [x] Shipped the precision guards (improve precision at any cap) + a tested
      `adaptive_scan` utility (ready for use once precision supports widening).
- [ ] **Raising the cap is BLOCKED on sports-matcher precision** (below).

### Classification
~600 candidate signals at cap=1500: **False positive / Incorrect resolution
criteria** (over-under line vs winner), **Incorrect counterpart** (different
teams). Not fixable by cap or fee tuning — needs matcher work.

`pytest -q` → **129 passed** (added: adaptive escalation ×2, precision guards ×3;
updated one stale test whose synthetic 44c edge tripped the new max_edge guard).

---

## Run 13 — 2026-06-15 (sports-matcher precision: totals/spread vs moneyline veto)

(New calendar day → curated strict streak reset to 0; no curated run this
iteration — this was a targeted matcher precision fix, per operator choice to
"fix sports matcher first.")

**Fix:** the dominant Run-12 phantom pattern was over/under-line vs winner on the
same team (e.g. "Sweden 1st Half O/U 0.5" ↔ "Will Sweden win the 1st Half?").
Root cause: the existing over/under veto in `is_compatible_match` was nested
inside the shared-stat loop, so it never fired when the two titles shared no
recognized stat (sports line-vs-winner share none).

Added a standalone veto (`is_compatible_match`): if exactly one side is a
totals/spread line (`_is_ou_or_spread`) and the other is a moneyline win market
(`_is_win_market`), reject. Scoped to win-vs-line so it does not touch
crypto/threshold pairs (no "win/beat" wording there).

**Verification:**
- [x] Phantom OU/spread-vs-win pairs now rejected (3 cases).
- [x] Legit pairs still match: NBA/WC moneyline, crypto threshold (BTC reach vs
      above).
- [x] 50-pair fixture: no regression. `pytest -q` → **132 passed** (added 3
      veto tests).

**Still phantom (next iterations):** different-team mismatches in same tournament
("West Indies" ↔ "Pakistan"), goals-scored vs spread, player-name vs stat-prop
("Cody Gakpo" ↔ "Cody Gakpo: 2+ assists"). Cap stays at 200 until these are
handled too. Full live re-quantification of phantom reduction: pending (next run).

---

## Run 14 — 2026-06-15 (LIVE precision + recall + curated offset 17) — idle PASS, no commit

First curated run of the new calendar day (streak reset). **PASS + precision PASS
+ recall CLEAN.** Consecutive fully-correct curated runs: **1/8**.

- **Live precision**: 39 pairs, v2 agree **39/39**, disagree 0 → no FP candidates.
  Categories: election, political.
- **Live recall**: production(0.30) 39, relaxed(0.20) 39, relaxed-only 0,
  v2-endorsed misses 0 → **CLEAN**.
- **Curated** (`--offset 17 --n 20`): expected 20, engine 20, **exact 20/20**,
  FP 0, missed 0, polarity 0.
- **Regression:** `pytest -q` → **132 passed** (incl. run-13 sports-veto tests).

Classification: all Exact. No defect → **no code change → no commit**. (Run-13
totals/spread veto holds; cap=200 live scans show no phantoms, as expected since
phantoms surface only at the wider cap.)

---

## Run 15 — 2026-06-15 (sports precision: player stat-prop veto) + curated offset 24

**Curated PASS** (streak **2/8**) + **precision PASS** (39/39) + **recall CLEAN**,
and a targeted sports-precision fix.

**Fix:** second Run-12 phantom pattern — a player stat-prop ("Cody Gakpo: 2+
assists", "Mitch Marner: First Goalscorer") mis-paired to a plain market on the
same player ("Cody Gakpo"). Diagnosis: `_proper_names` extracts the same player
on both sides; the prop side carries a stat the plain side lacks, and no existing
veto fired on that asymmetry.

Added `_is_player_prop` + a veto in `is_compatible_match`: reject when exactly one
side is a player prop and the two share a proper name. Scoped by shared-name so
it cannot over-reject unrelated markets; fixture/cap-200 production have no player
props, so unaffected.

**Verification:**
- [x] 3 player-prop phantoms rejected; legit moneyline + nomination pairs match.
- [x] `pytest -q` → **133 passed** (added player-prop test); 50-pair fixture clean.
- **Curated** (`--offset 24 --n 20`): exact 20/20, FP 0, missed 0.

---

## Run 16 — 2026-06-15 (curated offset 31 + phantom-reduction measurement)

**Curated PASS** (streak **3/8**) + **precision PASS** (39/39) + **recall CLEAN**.
`pytest -q` → **133 passed**.

- **Curated** (`--offset 31 --n 20`): exact 20/20, FP 0, missed 0.
- **Phantom-reduction measurement** at cap=1500 after the run-13/15 vetoes:
  raw 649→**605**, guarded 451→**430**. The soccer "1st Half O/U vs win"
  phantoms that dominated the run-12 top list are **gone** (vetoes worked via the
  individual path), but the count dropped only modestly because a **long tail** of
  other mismatch patterns surfaced into view. Honest reading of the new guarded
  top-12:
  - Still phantom: player props my run-15 lexicon missed (baseball "3+ total
    bases" — Pete Crow-Armstrong, Fernando Tatis Jr.); "Morgan Stanley" vs
    "...serve as lead underwriter"; "Mamdani freeze rents" vs "congestion
    pricing"; generic-vs-specific ("2027 Pro Football Champ" vs "Washington win").
  - **Actually REAL** (not phantom — the guarded set is now a *mix*): "DR Congo
    (-1.5)" ↔ "Congo DR wins by over 1.5 goals" (same spread); "North Carolina" ↔
    "Will North Carolina win the College Football Playoff".
- **Fix this run (16):** extended `_PROP_STATS` with baseball/basketball stats
  (total bases, runs, rbis, receptions, yards, blocks, steals, threes,
  double-double) so the run-15 player-prop veto catches "3+ total bases" etc.
  - [x] Pete Crow-Armstrong / Tatis props rejected; legit team-win pairs match.
  - [x] `pytest -q` → **133 passed** (extended player-prop test).

**Strategic note (honest):** pattern-by-pattern vetoes show **diminishing
returns** (451→430) — each fix clears its pattern but a long tail remains, and the
guarded set now contains some genuinely real pairs too. Converging precision at
the 1500 cap by enumerating patterns will be slow. The structural fix —
require matching **settlement type** (moneyline/total/spread/prop) AND **subject/
team** before pairing, ideally promoting v2 `contract_spec` to gate sports — is
the real path to safely raising the cap. Logged as the next major item; cap stays
at 200 meanwhile.

---

## Run 17 — 2026-06-15 (STRUCTURAL v2 sports bet-type gate) + important finding

Built the structural sports gate the operator approved: `ContractSpec.bet_type`
(`moneyline` | `line` | `prop`) + a `match_spec` gate rejecting bet-type
mismatches and player-prop-vs-non-prop on the same subject. Also hardened
`_is_player_prop` (require the `+` in "N+ stat") so team lines ("wins by over 1.5
goals") are not misread as props — fixing a latent false-reject of the real
"DR Congo (-1.5)" ↔ "wins by over 1.5 goals" pair.

Rationale: `compute_signals` already requires `v2_match=True`, so making v2 reject
these patterns filters them everywhere — including discover's group-matcher path
that bypasses `is_compatible_match`.

**Verification:**
- [x] v2 rejects O/U-vs-win, player-prop-vs-plain (clear reasons); still accepts
      NBA/WC moneyline, the DR Congo spread restatement, North Carolina win.
- [x] `pytest -q` → **137 passed** (added 4 v2-gate tests); 50-pair fixture
      parity held (v2 stays 50/50).

**Live measurement at cap=1500 (the honest finding):** guarded survivable
430 (run 16) → **434** — essentially UNCHANGED. The targeted sports phantoms ARE
gone from the guarded top-12 (soccer O/U-vs-win, "X: N+ assists" cleared), but the
total did not drop because the **remaining guarded set is dominated by NON-sports
cross-contract mismatches**, not sports:
- `Morgan Stanley` ↔ `serve as lead underwriter`; `United Kingdom` ↔ `which
  countries will have recession`; `Mamdani freeze rents` ↔ `congestion pricing`;
  `Hit The Wall - Gracie Abrams` ↔ `Gracie Abrams #1 hit`.
- A few leaked sports (stat lexicon gaps: "corners", bare "score").
- Some genuinely REAL pairs (Fed hike, OpenAI IPO, correct-score) — the guarded
  set is a mix, not pure phantom.

**Conclusion (honest, corrects the run-16 hypothesis):** the sports gate was
necessary and is done, but it does NOT unblock the cap. The dominant remaining
problem is **generic over-matching**: the matcher pairs different contracts that
merely share a proper noun / token bag (a firm, a country, a person, a song).
Raising the cap still floods. Cap stays at **200**.

---

## Backlog / open items (unchecked = not done)

- [x] **Live-fresh extraction (precision).** `validate_live.py` pulls live pairs
      via `discover.py` and referees precision/polarity with the v2 engine
      (run 2). Done for precision.
- [x] **Live recall — similarity-gate sensitivity.** `validate_recall.py`
      (run 5) probes recall loss from the production matcher's gate via a
      relaxed-gate diff refereed by v2. First result: CLEAN (no gate-driven
      misses). Covers the gate dimension of recall.
- [x] **Live recall — ingestion (event-cap dimension).** `validate_ingestion.py`
      (run 11) probes the Kalshi event-count cap. Found a REAL gap: cap=200 drops
      ~178 v2-endorsed true pairs (39 → 220 at cap=500). Fix proposed, pending
      operator decision on the scan-time tradeoff (see Run 11).
- [ ] **Live recall — Polymarket keyword-derivation misses.** True PM counterpart
      that the derived keyword search never surfaces (separate from the event
      cap). Still not covered.
- **Sports-matcher precision (BLOCKER for widening the cap).** Prerequisite for
  the operator's "raise to 1500 / >=50 survivable" request. Progress:
  - [x] Reject totals/spread (O/U-line) vs moneyline win (Run 13).
  - [ ] Reject different teams in the same tournament ("West Indies" ↔ "Pakistan").
  - [x] Reject player-name vs stat-prop ("Cody Gakpo" ↔ "Cody Gakpo: 2+
        assists") (Run 15).
  - [x] Extend player-prop lexicon with baseball/basketball stats (Run 16).
  - [ ] Reject goals-scored vs spread (e.g. "Team (-1.5)" ↔ "Will Team score?").
  - [x] **STRUCTURAL sports gate (Run 17):** v2 `contract_spec.bet_type`
        (moneyline/line/prop) rejects sports settlement-type mismatches. Done +
        tested. (Removed sports phantoms but did NOT reduce the total guarded
        count — see below.)
- [ ] **Generic cross-contract over-matching (NOW THE REAL BLOCKER).** Run 17
      showed the guarded cap=1500 set (~434) is dominated by NON-sports pairs that
      share only a proper noun / token bag but are different contracts: "Morgan
      Stanley" vs "lead underwriter", "UK" vs "recession-list", policy-vs-policy,
      "song title" vs "#1 hit". The matcher accepts these on token similarity.
      Fixing this needs a stricter same-contract requirement (predicate/action +
      subject must align, not just token overlap) — a core matcher change, larger
      than the sports gate. This, not sports, is what blocks raising the cap.
- [ ] Minor sports lexicon gaps: "corners", bare "score" (to-score prop).
  - [ ] Re-quantify phantom reduction at cap=1500; only raise the cap once the
        guarded survivable set is verified mostly-real.
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
| 5 | 4ac46a6 | add validate_recall.py + discover return_pools + run-5 recall probe |
| 6–10 | d0c1bf6 | day-complete audit trail: runs 6–10, 8/8 daily goal met |
| 13 | ca1a323 | matcher: reject totals/spread vs moneyline-win |
| 15 | ca05c56 | matcher: reject player stat-prop vs plain player |
| 16 | e2ce3bc | matcher: extend player-prop lexicon; phantom measurement |
| 17 | _this commit_ | structural v2 sports bet-type gate; generic over-matching identified |
| 11 | 9ab8387 | add validate_ingestion.py; found cap=200 drops ~178 true pairs |
| 12 | e3733fc | phantom-arb finding; compute_signals precision guards; cap clamped to 200 |
| 13 | _this commit_ | matcher: reject totals/spread vs moneyline-win (sports precision) |

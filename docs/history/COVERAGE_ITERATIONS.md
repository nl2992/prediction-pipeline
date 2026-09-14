# Coverage iterations: Kalshi × Polymarket

Working log for the "100% coverage of both platforms" effort. Each iteration
records **what was proposed, what changed, how it compares with the previous
implementation, and what to do next**. The current design is summarised in
[../COVERAGE.md](../COVERAGE.md).

Measurement method, used by every iteration so the numbers compare:
- Live catalog snapshots of both venues, plus independent ground truth: Kalshi
  `/markets?status=open&mve_filter=exclude`, Gamma `/markets/keyset?closed=false`.
- Offline replay of `discover()` against the snapshots (network patched out),
  timing each stage.
- Matcher changes are checked for **exact** output equivalence against a frozen
  reference copy of the previous code, on the same frozen pools.
- Live `python -m tools.validate_coverage` for the final number.

---

## Iteration 0: baseline (2026-09-14)

**As pulled (`main` @ 4b74301):** production scanned ~13% of Kalshi events
(1,500-event cap, one `/markets` request per event, 429 back-off) and ~10% of
Polymarket (offset pagination silently ended at offset ~2,100 on HTTP 422, then a
Kalshi-keyword filter). 50 matched pairs, 23 endorsed by v2.

**Post-pull WIP (`cleanup/recall-expansion`, uncommitted):** Kalshi nested
`/events` ingestion and Polymarket `/events/keyset` pagination. Offline replay on
full snapshots:

| Stage | Result |
|---|---|
| Kalshi | 11,732 events → 10,588 kept → 98,559 markets in pool |
| Polymarket | keyword filter kept 19,818 / 20,729 events → 187,946 markets in pool |
| Matching | **992 s** (16.5 min) |
| Pairs | 4,790 pairs, 3,764 v2-endorsed |

Gaps found against ground truth:
1. Kalshi: 1,129 stats-only events (7,494 markets) and 8 multi-leg markets
   dropped at ingestion. Callers still capped events (alerter 1,500 + 730-day
   horizon, dashboard 200).
2. Polymarket: ~22k **closed** markets ingested as tradeable (only `active` was
   checked). 911 events dropped by the keyword filter. 482 open "orphan" markets
   unreachable via `/events/keyset` (parent event archived / inactive / closed).
3. Matcher: too slow for full pools (992 s), and **non-deterministic**: identical
   input gave 168 vs 166 pairs, because greedy ties were broken by
   hash-randomised set order.

Verified non-issues: Kalshi nested markets are not truncated (400-market events
are real 400-strike ladders and match `/markets` exactly). Gamma
`/events/keyset` ignores `archived` / `active` params, so orphans need the
market sweep.

---

## Iteration 1: full-catalog ingestion + exact-recall matcher (done)

**Proposed:**
- *Ingestion (WP1):* no Polymarket keyword filter. Polymarket open =
  `active and not closed`. Orphan sweep over `/markets/keyset`, run in a
  background thread and cached. Kalshi stats-only / multi-leg markets ingested but
  held out of matching (tagged). Callers drop caps and horizons. `ingest_kalshi` /
  `ingest_polymarket` split out. Coverage accounting (`coverage=` dict,
  `--coverage-json`, `[coverage]` lines). New live verifier
  `tools/validate_coverage.py`.
- *Matcher (WP2):* deterministic total-order tie-breaks. Memoised pure text→feature
  helpers (80% of matcher time was recomputing regex features per candidate pair).
  Exact prefix-filter blocking for the Jaccard gate, with a separate
  threshold-index candidate path so sub-gate threshold-led matches are preserved.

**Results so far:**
- WP1 landed. Suite 601 → 619 passed, ruff clean.
- First live `tools.validate_coverage` (2026-09-14 06:14 UTC):

  | Venue | Ground truth | Ingested | Coverage | Real gaps | Drift | Closed since |
  |---|---|---|---|---|---|---|
  | Polymarket | 166,086 | 166,268 | 99.99% | **0** | 20 | 202 |
  | Kalshi | 105,941 | 105,923 | 99.97% | **18** | 19 | 19 |

- **New finding:** Kalshi's `/events` listing disagrees with its own `/markets`.
  Some events with `active` markets are absent from `/events` under every status
  (`KXBEATLESRECORD-26`, `KXFISPIRITAWARDS-26DOC`). Others are listed, but not under
  `status=open` (`KXHOUSERACE-DCAL-26`: 359 events unfiltered vs 350 with
  `status=open`). The same 18 tickers were missing in the 01:40 snapshot too, so
  this is persistent, not drift. Fix (WP1b, in progress): a Kalshi orphan sweep
  over `/markets?status=open&mve_filter=exclude` (~20 s, runs in parallel), with
  parent titles fetched via `GET /events/{ticker}`.
- WP1b landed: Kalshi orphan sweep plus `KalshiClient.get_event` and an
  `mve_filter` passthrough. Orphans are markets whose `event_ticker` is absent
  from the raw `/events` walk. Their parent event is fetched and run through the
  same event filters, so user filters are respected. Second live
  `tools.validate_coverage` (06:35 UTC):

  | Venue | Ground truth | Ingested | Coverage | Real gaps | Drift | Closed since |
  |---|---|---|---|---|---|---|
  | Kalshi | 105,886 | 105,886 | **100.0%** | **0** | 0 | 0 |
  | Polymarket | 166,188 | 166,096 | 99.89% | **0** | 175 | 83 |

  Polymarket drift is markets created after ingestion began, during the ~5-min
  ground-truth walk (mostly short-dated crypto / sports). They're picked up next
  cycle, so every miss is explained.
- Review notes for the next pass: `catalog_cache_ttl` is now a dead parameter.
  Nothing reads it, but the alerter `--interval` help and `discover()` docstring
  still claim a 20-min catalog cache. The orphan cache (1 h) can serve an orphan
  that closed within the hour; it's filtered out downstream, but noted.

- WP2 landed (matcher):
  - Deterministic tie-breaks in all three greedy passes: `(score desc, poly id, kalshi id)`.
  - `lru_cache` on ~39 pure text→feature helpers, which now return immutable
    `frozenset` / `MappingProxyType` so a cached value can't be corrupted.
  - Exact prefix-filter + length-filter blocking for the Jaccard gates, plus a
    separate `(direction, unit)` threshold index for the sub-gate threshold-led path.
  - Equivalence against the tie-fixed reference, on frozen full pools
    (98,578 × 165,865): **4,740 = 4,740 pairs, 0 differences, 0 score changes**.
    Re-verified independently under a third hash seed.
  - **951 s → 70–77 s (12.4×).** 17 new tests in `tests/test_matcher_perf.py`.
- Suite: 601 → **649 passed**, ruff clean.
- Live end-to-end `python discover.py --show-prices` (2026-09-14):

  | Stage | Result |
  |---|---|
  | Kalshi ingest | 105,895 / 105,895 (100.0%), incl. 18 orphans · 57 s (bounded by its sweep) |
  | Polymarket ingest | 166,203 / 166,203 (100.0%), incl. 442 orphans · sweep 263 s |
  | Matching | 98,393 eligible Kalshi × 166,203 Polymarket → 4,723 pairs (3,705 v2-endorsed) |
  | Books | live on 99% of endorsed pairs (Kalshi 3,671 / 3,705 multi-level, Polymarket 3,543 / 3,705) |
  | Total | 614 s wall, 112 s CPU (network-bound), 4.6 GB peak RSS |

**Compared with the prior implementation:**
- Coverage: ~13% / ~10% as pulled, then ~93% Kalshi / keyword-filtered
  Polymarket polluted with ~22k closed markets in the WIP, now **100% / 100%**
  with every exclusion counted.
- Matched pairs: 50, then 4,790 (WIP, offline), now 4,723 (live; 3,705 v2-endorsed).
- Matching: ~992 s and non-deterministic, now ~70 s, deterministic, identical output.
- Scan cost is now dominated by network: orphan sweeps and per-pair book fetches.

**What iteration 1 revealed (inputs to iteration 2):**
1. **Precision, not coverage, is now the bottleneck.** The live run found 630
   endorsed pairs with a positive gross edge, but the biggest are v2 false
   positives:
   - "Blue wave in 2026?" ↔ "Red wave in 2026?" (inverted outcome)
   - "Will Mamdani freeze NYC rents" ↔ "Will congestion pricing in NYC end" (different subject)
   - "Aaron Donald to Play Week N" ↔ "Aaron Donald to compete in the Pro Football…" (different scope)
   - "Will Apple release iPhone 18 in 2026?" ↔ "…before …" (horizon)
2. **Only 9 of 3,705 endorsed pairs pass `is_arb_eligible`**, so the discover CLI
   shows 0 arb signals. The gate may be over-strict at this scale, or mostly
   right given (1). Needs an audit before anyone trusts either number.
3. **Cycle time (614 s) exceeds the alerter's 300 s interval.** 3,705 per-pair
   book fetches (~280 s with v2) and the orphan sweeps dominate.
4. `catalog_cache_ttl` is a dead parameter with stale help text. 4.6 GB peak
   RSS from 300k-entry caches × ~39 helpers plus raw sweep rows. The Polymarket
   orphan cache can serve a market that closed within the hour.

**Next fixes to consider (iteration 2 proposal):**
1. **Precision audit + fixes.** Sample ~200 v2-endorsed pairs, stratified by
   category, and label them. Add contract_spec checks for opposite-outcome
   pairs ("blue wave" / "red wave", party-flipped wording), subject mismatch
   inside the same jurisdiction, and "in <year>" vs "before <date>" horizons.
   Report precision before and after.
2. **`is_arb_eligible` audit.** Tabulate which gate rejects each endorsed pair.
   Relax only the gates whose rejections are wrong, judged against the labelled
   sample from (1).
3. **Screen on catalog prices, fetch books only for candidates.** Kalshi nested
   rows already carry `yes_bid/ask_dollars` and Gamma rows `bestBid/bestAsk`, so
   fetch CLOB books only for pairs whose catalog edge is within a few cents of
   positive. That cuts ~3,700 book requests to tens and should bring the cycle
   under 300 s. Verify no signal is lost on the same live run.
4. **Hygiene.**
   - Remove or implement `catalog_cache_ttl`, and fix the alerter `--interval` help.
   - Clear the matcher caches per cycle and drop raw sweep rows after use; measure RSS.
   - Re-filter cached orphans by live state.
   - Fix the `--no-market-sweep` "(default: True)" help wording.
5. **Alerter dry run on the new path** (`python alerter.py --once --dry-run`)
   to confirm email assembly, the AI gate and `compute_signals` all work on
   thousands of pairs.

**How we may expand further:** see the bottom of this file.

---

## Iteration 2: precision + cycle time (done)

**Proposed:** see the iteration-1 "Next fixes" list. Split into two work packages:
- **A: precision.** Labelled audit set, contract_spec discriminators for the
  observed false-positive classes, `is_arb_eligible` audit.
- **B: cycle time and hygiene.** Catalog-price screen before live books, dead
  `catalog_cache_ttl`, matcher cache clearing, stale orphan marking, alerter dry run.

**Baseline for this iteration.** Production's own gate is
`alerter.compute_signals` (v2 endorsement, 25c `max_edge`, depth ≥ 20), not
`is_arb_eligible`. On the iteration-1 live pairs it yields **343 signals, 36
emailable (> 3% net)**. Most of the top emailable ones are false positives:
- temperature buckets probably for different stations
- "No Atlantic hurricane in September" ↔ "next hurricane before Oct 1" (inverted)
- "Al-Ittihad" moneyline ↔ "Al-Ittihad wins by more than 1.5 goals" (spread)

So precision work is measured on the emailable and signal subsets first.

**Results (A: precision):**
- Audit set `tests/fixtures/endorsed_audit_2026-09-14.json`: all 343 production
  signals plus a seed-7 stratified sample of 150 other endorsed pairs, labelled
  `same` / `different` / `unsure` with a reason class. The coordinator spot-checked
  ~25 labels and corrected one: "recession *by end of* 2027" vs "*in* 2027" is a
  horizon mismatch.
- Precision and recall:

  | Subset | v2 precision before | after | Recall on labelled-same |
  |---|---|---|---|
  | Signal subset (339 labelled) | 94.1% | **96.1%** | 99.7% (1 documented exception) |
  | Stratified subset (149) | 97.3% | **98.0%** | 100% |

  (The iteration-1 impression that "most top emailable pairs are wrong" was too
  pessimistic. Atlanta-temperature and Serbia-PM pairs, for example, are genuine.)
- Fixed classes in `contract_spec.py`:
  - playoff stage: conference champion vs division winner vs #N seed (6 pairs)
  - `#N` rank mismatch (1 pair)
  - aggregate bucket ("a team from Texas") vs a specific entity outside it (1 pair)
  - a pre-existing selected-name extraction bug: names bled across the
    title / event-title join. This was the biggest recall win (~8 true pairs recovered).
- Full re-run on the 4,723 live pairs:

  | | Before | After |
  |---|---|---|
  | Endorsed | 3,705 | 3,727 |
  | Production signals | 343 | 335 |
  | Emailable | 36 | 34 |

- **Known, unfixed false-positive classes**, still endorsed and documented:
  - different subject under the same actor (Trump nationalize elections vs SpaceX)
  - rate level vs rate-change count
  - halftime vs full-match spread
  - same person, different team (Baldelli). A generic gate was built and
    **reverted** after it falsely rejected ~41 true pairs.
  - award-family mismatch (Heisman vs Walter Camp)
  - bps bucket boundaries
  - weekly vs month-end horizons
- Suite 649 → 690 passed. No existing fixture expectations changed (verified in the diff).

**Results (B: cycle time + hygiene):**
- **Catalog-price screen.** Gamma `bestBid` / `bestAsk` are now stored in
  `extra["catalog_bid" / "catalog_ask"]`; the matcher's mid-price book is
  untouched. New `discover(enrich_margin=0.05)`: endorsed pairs get live books
  only when their catalog gross edge is ≥ −margin, a price is missing, or the
  quote came from a stale cache. Every result records `catalog_gross_edge` and
  `books_live`. Non-enriched Polymarket legs keep size-0 books, so
  `compute_signals`' depth filter can never promote them (tested).
- **Losslessness** (live, everything enriched, catalog edges recorded first):
  319 signals; signals the screen would have skipped: **0 at margins 0.02, 0.05
  and 0.10**.
- **But the saving is small.** At 0.05, 91% of endorsed pairs still needed books
  (3,386 / 3,717): discover 614 s → **571 s**.
- **Hygiene:**
  - dead `catalog_cache_ttl` removed from discover, alerter and the three probes;
    `--interval` / `--no-market-sweep` help fixed
  - `matcher.clear_caches()` after each run; raw sweep rows dropped once orphans
    are extracted; cache-served orphans tagged `catalog_stale` and always refreshed
  - peak RSS 4.6 → 3.75 GB (peak footprint ~5 GB is ingestion-time pools)
- **Alerter dry run** (`python alerter.py --once --dry-run`, live):
  - 609 s wall, 3.9 GB
  - 4,719 pairs → 353 survivable arbs → **42 emailable**; email and 42 book-depth
    charts assembled; `DRY RUN — would email …`, nothing sent
  - AI gate `key=ABSENT`, failed open as designed
- Suite **690 passed**, ruff clean.

**Compared with iteration 1:**

| | Iteration 1 | Iteration 2 |
|---|---|---|
| Coverage | 100% / 100% | unchanged (ingestion untouched) |
| v2 precision (signal / stratified audit) | 94.1% / 97.3% | **96.1% / 98.0%** |
| Endorsed / signals / emailable (same live pairs) | 3,705 / 343 / 36 | 3,727 / 335 / 34 |
| Discover wall time (`--show-prices`) | 614 s | 571 s |
| Peak RSS | 4.6 GB | 3.75 GB |
| Alerter end-to-end | not run | 609 s dry run, email assembled |
| Tests | 649 | 690 |

**What iteration 2 revealed:**
1. **Per-market book fetching is the real cost, not the number of pairs.** Kalshi
   has a batch endpoint, `GET /markets/orderbooks?tickers=…&tickers=…` (repeated
   param; comma-joined is treated as one ticker). It takes ≤ 100 tickers per
   request (101 → 400, more → 414) and answers in ~0.08 s. 3,400 books would be
   ~34 requests, ~3 s, instead of ~3,400 requests at 8 workers.
2. **The cycle is still ~10 min against a 300 s interval.** Remaining costs: the
   Polymarket orphan sweep (~260 s, but cached 1 h in the alerter, so only the
   first cycle of each hour pays it); the Kalshi sweep (20 s alone, ~57 s under
   contention); v2 over ~4.7k pairs (not yet profiled); matching (~70 s).
3. **The remaining false positives need entity-context reasoning,** e.g. same
   person / different team, award families, or rate level vs change count. A
   generic gate was built and reverted after it broke ~41 true pairs, so these
   need structured features, not regexes.

**Next fixes to consider (iteration 3 proposal):**
1. **Batch Kalshi books** via `/markets/orderbooks` in chunks of 100. Check that
   `_parse_kalshi_full_book` handles the batch `orderbook_fp` shape and depth; keep
   the per-ticker path as a fallback for failed chunks. Then decide whether the
   catalog screen is worth keeping (enriching everything costs ~seconds).
2. **Profile the post-match stage.** Time v2 `explain` over ~4.7k pairs and
   result formatting; cache per-snapshot contract specs if hot.
3. **Hit the 300 s cycle.** Measure alerter cycles 1 and 2 back to back: cycle 2
   should reuse the cached Polymarket orphans. Consider an hourly Kalshi sweep
   too, since its orphans are few and stable.
4. **Precision, structured:** a same-person-different-team check using the event
   title's team/org entity (Baldelli: Phillies vs Red Sox manager); award-family
   tokens (Heisman ≠ Walter Camp); halftime vs full-time settlement; rate level vs
   number-of-cuts. Measure each on the audit fixture: precision up, recall on
   labelled-same unchanged.
5. **Coverage SLO:** surface `[coverage]` and the sweep status in the alerter log
   and `health.py`, so a drop below 100% or a failed / partial sweep shows up as
   DEGRADED.

---

## Iteration 3: batch books + cycle time + coverage SLO (done)

**Proposed:** the iteration-2 "Next fixes" list, split into:
- **C: cycle time.** Batch Kalshi books via `/markets/orderbooks`, with parity
  against the single-ticker payload. Decide whether to keep the screen. Profile
  the post-match stage. Two back-to-back alerter cycles, each < 300 s.
- **D: precision + SLO.** Narrow structural discriminators for the remaining
  false-positive classes, each gated on zero labelled-same pairs lost.
  `health.py` parses the `[coverage]` lines and reports DEGRADED on < 100%, a
  partial catalog, or a failed / partial sweep.

**Results (D: precision + SLO):**
- Seven narrow discriminators in `contract_spec.py`. Each was measured on the
  audit fixture and kept only at **0 labelled-same pairs lost**:

  | Discriminator | False positives removed | Same pairs lost |
  |---|---|---|
  | Same person, different team (closed MLB gazetteer; needs selected-name overlap *and* both sides naming a known team) | 1 (Baldelli) | 0 |
  | Award family (Heisman / Walter Camp / Doak Walker …) | 3 | 0 |
  | Halftime vs full match | 2 | 0 |
  | Rate level vs number of changes | 1 | 0 |
  | Same actor + verb, different object (curated verbs) | 1 (Trump nationalize elections vs SpaceX) | 0 |
  | bps bucket direction / range overlap | 2 | 0 |
  | Weekly-recurring vs longer horizon | 1 | 0 |

- Audit precision: signal subset 96.1% → **98.45%**, stratified 98.0% →
  **100.0%**. Recall unchanged (99.7% / 100%). The regression floors are raised to
  0.98 / 0.99.
- Live re-verdict (4,725 pairs, saved books): endorsed 3,732 → 3,699, production
  signals 341 → 334, emailable 40 → 38. Every rejection traces to the seven new gates.
- **Coverage SLO:** `health.py` parses the latest cycle's `[coverage]` lines. It
  reports DEGRADED on < 100.0%, `[PARTIAL CATALOG]`, or `sweep=partial|failed`.
  `off` / `cached` are OK, and logs without coverage lines show `coverage: n/a`
  without degrading. 10 new tests; `ops.py` shows it via `health.build_report()`.
- Suite 690 → 750 passed. No pre-existing fixture expectations changed (verified).

**Results (C: cycle time):**
- **Batch Kalshi books.** New `KalshiClient.get_orderbooks(tickers)` makes chunks
  of 100 with repeated `tickers` params. `_enrich_kalshi` fetches the chunks in
  parallel and falls back per ticker only for tickers a chunk didn't return; on
  total failure the catalog top-of-book is kept. Parity with the single-ticker
  endpoint: identical on 12+ live tickers (agent), re-verified by the coordinator
  on 6 live `KXFED` ladders (8–21 levels per side, **6 / 6 byte-identical**, no
  truncation). Real captured payloads are committed as test fixtures.
- **Enrichment ~280 s → 4.4 s.** Enriching everything (4.4 s) vs screened (4.9 s)
  is within noise, so the default is now `enrich_margin=None`: every v2-endorsed
  pair gets live books, lossless by construction. `--enrich-margin` stays opt-in.
- **Per-stage profile** (now in `coverage["elapsed_s"]`):

  | Stage | Time |
  |---|---|
  | Kalshi ingest / sweep | 50–64 s |
  | Polymarket events | 192–222 s (waits on the sweep) |
  | Polymarket sweep | 240–284 s |
  | Match | 72 s |
  | v2 | 2.9 s |
  | Enrich | 4.4 s |
  | Format | 0.4 s |

- **Hourly Kalshi orphan cache** (`.cache/kalshi_orphans.json`, same
  `catalog_stale` semantics as the Polymarket one).
- **Two live alerter cycles back to back** (`--once --dry-run`, caches cleared first):

  | Cycle | Sweeps | Wall | Peak RSS | Pairs | Survivable | Emailable |
  |---|---|---|---|---|---|---|
  | 1 | fresh | **343 s** | 3.75 GB | 4,719 | 336 | 32 |
  | 2 | cached | **125 s** | 2.87 GB | 4,719 | 339 | 0 (see bug below) |

- Suite **750 passed**, ruff clean.

**Bug found while verifying (pre-existing):** `run_cycle(dry_run=True)` sets
`delivered = True` (alerter.py ~l.802). So a dry run appends the "emailed"
signals to `alert_signals.jsonl` and writes the re-alert state. Cycle 2 emailed
0 for that reason. Run in the production directory, one `--dry-run` would
suppress real emails for those arbs for 6 h and pollute `signal_report.py`'s
audit log.

**Compared with iteration 2:**

| | Iteration 2 | Iteration 3 |
|---|---|---|
| Coverage | 100% / 100% | unchanged; now **monitored** by `health.py` |
| Audit precision (signal / stratified) | 96.1% / 98.0% | **98.45% / 100.0%** (recall unchanged) |
| Live endorsed / signals / emailable | 3,727 / 335 / 34 | 3,699 / 334 / 38* |
| Book enrichment | ~280 s, screen-dependent | **4.4 s**, every endorsed pair |
| Alerter cycle | 609 s | **343 s cold / 125 s warm** |
| Peak RSS | 3.75–3.9 GB | 3.75 GB cold / 2.87 GB warm |
| Tests | 690 | 750 |

\*Different live snapshot from iteration 2's figure (fresh books). On
like-for-like saved books the new gates took signals 341 → 334 and emailable 40 → 38.

**Next fixes to consider (iteration 4 proposal):**
1. **Dry-run must not persist** (bug above). No audit-log append and no
   realert-state write when `dry_run`; add a regression test.
2. **Cold cycle still > 300 s** (343 s), bounded by the Polymarket orphan sweep
   (~260 s, 100 rows/page). Options: (a) move the sweep off the critical path,
   using last hour's orphans while a background refresh runs, so every cycle is
   warm; (b) resume from the saved keyset cursor (ids ascending) and re-walk only
   the tail. Target: every cycle < 150 s.
3. **Matching is now the largest warm-cycle stage (72 s).** Profile the post-blocking
   `is_compatible_match` calls. Consider incremental matching: re-match only
   events whose market set or titles changed since the last cycle (deterministic
   matcher, so incremental equals full).
4. **Precision on the next layer.** Grow the audit set with a fresh live
   sample, since the current fixture's false positives are mostly fixed. Target
   the one documented recall exception (single-word team names, e.g.
   `Bayer Leverkusen`, in `matcher.py`).
5. **Unify `min_size` / `max_edge` with the batch books.** Now that every endorsed
   pair has full depth, compute executable size / VWAP (book_arb) before
   `max_edge`, so a thin top level doesn't drop a real arb.

---

## Iteration 4: dry-run safety, cold cycle, depth-aware economics, fresh audit (done)

**Proposed:** the iteration-3 "Next fixes" list, with item 3 (incremental
matching) **deferred**. The naive version isn't exact (see backlog item 7), and
warm cycles are already 125 s. Split into:
- **E: alerter + cold cycle.** Dry runs persist nothing. Stale-while-revalidate
  orphan caches so only a process with no cache pays the sweep inline.
  `min_edge` / `max_edge` / `min_size` evaluated on depth-walked executable edge.
- **F: precision.** A fresh live audit sample (the old fixture is nearly
  exhausted), new discriminators gated on zero same-pair loss on *both*
  fixtures, and the `Bayer Leverkusen` team-name recall exception.

**Results (F: precision):**
- New fixture `tests/fixtures/endorsed_audit_iter4.json`, from a fresh live run
  (4,709 pairs, 3,671 endorsed, 335 signals):
  - 48 new signal pairs: 45 same / 3 different
  - 200 stratified endorsed pairs (seed 11): 196 same / 4 different
  - The coordinator checked all 7 "different" labels and 6 random "same" ones: all correct.
- Five discriminators, each **0 same pairs lost on both fixtures**:

  | Discriminator | False positives removed |
  |---|---|
  | Runner-up vs champion (e.g. USL "2026 Runner-Up" ↔ "win the USL Championship") | 3 |
  | Single match vs whole tournament | 1 |
  | Song / album duration vs chart position | 1 |
  | Connector words ("… Without Borders") no longer anchor org names | 1 |
  | Explicit 1st vs 2nd half | 1 |

- Left unfixed and documented: "a dLLM" (architecture class) vs "Z.ai" (company),
  a single occurrence too narrow to generalise.
- Recall exception fixed: `matcher._bare_subject_name` recovers single-word club
  names (needs exactly one capitalised head word, so "Los Angeles Dodgers" can't
  bridge to "Los Angeles FC"), and "bundesliga" was added to the generic name terms.
  `KNOWN_RECALL_EXCEPTIONS` is now empty.
- Precision / recall:

  | Fixture | Signal subset | Stratified subset | Recall |
  |---|---|---|---|
  | 2026-09-14 (old) | 98.45% (unchanged) | 100% | 99.7% → **100%** |
  | iter4 (new) | 93.75% → **100%** | 98.0% → **99.49%** | 100% |

- Live re-verdict on saved books (4,709 pairs): endorsed 3,671 → 3,650, signals
  418 → 414, emailable 48 → 45.
- **Effect on the matcher itself** (the entity-helper edits feed
  `is_compatible_match`), on frozen full pools vs the iteration-1 signature:
  4,740 → 4,739 pairs. The only difference is the removed "Reporters Without
  Borders" ↔ "Doctors Without Borders" Nobel pair; 0 score changes.
- Suite 752 → 781 passed.

**Results (E: alerter + cold cycle):**
- **Dry-run bug fixed:** `delivered = not dry_run`. "Email not configured" still
  counts as delivered (#84/#86 semantics). Regression tests cover byte-identical
  state and audit files, and a prior dry run no longer suppresses the next real
  email. Verified live: three dry runs, and the files' mtimes never moved.
- **Stale-while-revalidate orphan caches:**
  - `_cache_load_any` plus an atomic `_cache_store` (temp file + `os.replace`).
  - Fresh / stale / absent decision with a 6 h hard maximum age.
  - A de-duplicated, never-joined daemon refresh.
  - A stale cache is served immediately with `sweep=stale` (health.py treats it
    as OK). Only a process with no usable cache pays the sweep inline.
- **Live cycles** (`alerter.py --once --dry-run`):

  | Cycle | Cache | Wall | Sweep (Kalshi / Polymarket) |
  |---|---|---|---|
  | A | none | 352 s | fresh / fresh |
  | B | both past TTL | **170 s** | stale / stale (background refresh started) |
  | C | right after B | 216 s | cached / stale |

  `--once` limitation (documented in code): the process exits before the ~260 s
  Polymarket refresh finishes, so under `--once` scheduling Polymarket stays
  `stale` until the 6 h cap forces an inline sweep. The long-running
  `python alerter.py` loop refreshes during its interval sleep.
- **Depth-aware economics:** `book_arb.vwap_for_quantity` and
  `executable_edge_at_size`. With `min_size > 0`, `compute_signals` gates
  `min_edge` / `max_edge` on the depth-walked net edge at `min_size` (Kalshi fee
  on the walked VWAP), recorded as `exec_net` / `exec_vwap_*`. Display fields stay
  top-of-book, and `min_size = 0` is unchanged. Offline on the iteration-3 live
  books: **signals 334 → 421, emailable 38 → 53**. 87 pairs were rescued (thin top
  level, real depth behind it) and 0 newly fail. The coordinator hand-checked the
  VWAP / fee arithmetic on two signals: correct.
- Suite **781 passed**, ruff clean.

**Compared with iteration 3:**

| | Iteration 3 | Iteration 4 |
|---|---|---|
| Coverage | 100% / 100%, monitored | unchanged |
| Precision, old fixture (signal / stratified / recall) | 98.45% / 100% / 99.7% | 98.45% / 100% / **100%** |
| Precision, new fixture (signal / stratified) | 93.75% / 98.0% (new sample) | **100% / 99.49%** |
| Alerter cycle | cold 343 s / warm 125 s | cold **352 s only with no cache ever** / stale-cache **170 s** / warm ~125–216 s |
| Dry run | polluted audit log + suppressed real emails 6 h | **side-effect free** |
| Signals / emailable (iteration-3 live books) | 334 / 38 | **421 / 53** (depth-aware) |
| Full-pool matching | 70 s | **108 s** (regression, see below) |
| Tests | 750 | 781 |

**What iteration 4 revealed:**
1. **Matcher performance regression: 70 s → 108 s** on frozen full pools,
   re-measured with no concurrent load (sub-4000: 15.0 s → 27.6 s). Output is
   unaffected: 4,739 pairs, deterministic, and only the one intended pair differs
   from iteration 1. Profile: `is_compatible_match` is 422k calls and 13.7 s
   cumulative, with hot spots `_matchup_signature` (2.0 s), 1.03 M uncached
   `_snapshot_text` calls, and regex work in `_contract_actions` /
   `_named_entities` / `_numeric_threshold`. The likely source is iteration-4's
   entity-helper additions. There's no saved copy of the iteration-1 code to diff
   profiles against, because this effort hasn't been committed.
2. **The richest remaining signals are settlement-source risks, not text
   mismatches.** Top: Atlanta "90–91°F" at a 23c edge. The venues probably use
   different weather stations or reports, and the AI verify gate that exists for
   this is off here (`DEEPSEEK_API_KEY` absent).
3. **v2 still endorses some cross-subject pairs:** "Will Trump's first
   endorsement before…" ↔ "Will a Trump family member be the 2028 Republican
   nominee", at a 19c executable edge.
4. **Depth-aware gating adds 26% more signals.** Precision on that *new* margin
   hasn't been audited yet.

**Next fixes to consider (iteration 5 proposal):**
1. **Commit iterations 1–4** (needs the operator's go-ahead) so regressions can
   be bisected. Then fix the matcher regression back to ≤ 75 s with byte-identical
   output (`match_bench.py diff`): memoise `_snapshot_text` per snapshot,
   cache or hoist the new entity helpers.
2. **Settlement-source discriminator** for weather and data-release markets: parse
   station / source from each venue's rules (Kalshi `rules_primary`, Gamma
   `description` / `resolutionSource`) and reject pairs whose sources differ. Needs
   those fields kept at ingestion; today they're dropped.
3. **Audit the 87 depth-rescued signals** (label them like the other fixtures)
   before trusting the +26%.
4. **Actor-scoped subject check** for "first endorsement" / "family member"
   style pairs, with the usual zero-same-loss gate on both fixtures.
5. **Enable the AI gate in a test run** (a DeepSeek key in env) to measure how
   many of the current emailable pairs it confirms or flags, versus the fixture
   labels.

---

## Iteration 5: matcher regression + settlement correctness (done)

**Proposed:** the iteration-4 "Next fixes" list, split into:
- **G: matcher regression.** Full-pool matching 108 s → ≤ 75 s using exact
  techniques only; output must diff-identical to the iteration-4 signature
  (`sig_iter4b.json`, 4,739 pairs) under two hash seeds.
- **H: settlement correctness.** Compact settlement-source features at snapshot
  build (weather station / provider, data-release agency, crypto price index),
  with a v2 source-mismatch check investigated first on the live Atlanta case. An
  actor-scoped subject check. A labelled audit of the depth-rescued signals.

Not attempted: item 5, the AI-gate measurement (no `DEEPSEEK_API_KEY` in this
environment). Item 1's commit was approved by the operator after iteration 5 and
done then.

**Results (G: matcher regression):**
- Root causes, from profiling and `cache_info()`:
  1. `_snapshot_text` / `_contract_text` built a **new string object per
     candidate pair**. Each of the ~25 `lru_cache` helpers then re-hashed it,
     because string hashes are cached per object. Iteration 4's extra entity
     helpers multiplied that cost.
  2. `_tokens` / `_ascii_lower` overflowed their 300k LRU on full pools
     (`currsize` pinned at max, misses > 300k).
  3. `_jaccard` materialised `a | b` on 8.9 M calls.
- Fixes, all exact:
  - weakref-evicting identity memoisation of the two per-snapshot text builders
    (they still clear via `clear_caches()`)
  - a 900k cache for the two wide helpers
  - `_jaccard = |a∩b| / (|a| + |b| − |a∩b|)`, bit-identical floats
- Full-pool matching **108 s → 59–72 s** (agent: 59–61 s; coordinator re-run 71.6 s).
  Output diff-identical to `sig_iter4b.json` under hash seeds 0, 42 and 777.

**Results (H: settlement correctness):**
- **Atlanta case, from the live rules text:** Kalshi `KXHIGHTATL` settles on the
  CLIATL maximum "according to **The Weather Company**". Polymarket resolves from
  **NOAA**'s KATL hourly timeseries, falling back to Weather Underground. Same
  airport, different pipelines. Kalshi's own `rules_secondary` warns that other
  readings can diverge. The fresh live run had **101** such weather pairs (every
  US city), all previously v2-endorsed.
- `contract_spec.settlement_source()` extracts compact tags at snapshot build
  (e.g. `weather:weather_company:cliatl`, `crypto:binance`, `econ:bls`) from Kalshi
  `rules_primary` and Gamma `description` + `resolutionSource`. The raw text is
  never stored; untagged markets share an interned empty tuple.
  `_settle_src_conflict` rejects when both sides name providers in the same class
  and they're disjoint (a side listing a provider plus its fallback isn't flagged).
- `_is_first_endorsement_market` rejects "Trump's first endorsement…" against a
  specific-candidate market, which fixes the iteration-4 example.
- **Depth-rescued audit** (`tests/fixtures/endorsed_audit_depth_rescued.json`):
  85 rescued pairs at **94.1% precision** (80 same / 5 different), below the
  ~98–100% of other signals. Unfixed classes:
  - weather bucket vs cumulative threshold
  - bucket granularity mismatch
  - adjacent GDP buckets, exposed by a pre-existing `matcher._ascii_lower` bug
    that strips en-dashes before range parsing
  - GDP annual vs Q4 plus direction
- Old fixtures unchanged. Fresh live run (5,007 pairs): endorsed 3,930 → 3,828
  (101 settlement + 1 actor-scoped). Signals (480) and emailable (36) are
  unchanged on that snapshot, because the rejected weather pairs weren't reaching
  signal economics then.
- Suite **800 passed**, ruff clean.

**Compared with iteration 4:**

| | Iteration 4 | Iteration 5 |
|---|---|---|
| Coverage | 100% / 100% | unchanged |
| Full-pool matching | 108 s | **59–72 s** (identical output) |
| Weather pairs with divergent settlement pipelines | endorsed (101) | **rejected** |
| Depth-rescued signal precision | unaudited | **94.1%** (below other signals) |
| Tests | 781 | 800 |

**Next fixes to consider (iteration 6 proposal):**
1. **Depth-rescued false positives:** bucket vs cumulative threshold, bucket
   granularity, and the `_ascii_lower` en-dash bug (a matcher change, so re-diff
   `match_bench` and accept only the intended pair changes).
2. **Weather policy decision (operator):** all 101 NOAA-vs-Weather-Company pairs
   are now rejected. If the operator wants them, the alternative is to keep them
   with a settlement-risk badge in the email, which needs an alerter change.
3. **Re-run `tools.validate_coverage` live.** Ingestion code changed in
   iterations 3–5 (snapshot builders, SWR caches), so re-confirm 0 gaps.
4. **Fresh cold / stale / warm alerter timing** with the faster matcher, to
   confirm every cycle < 300 s.

---

## Iteration 6: bucket semantics + re-verification (done)

**Proposed:** the iteration-5 "Next fixes" list.
- **I (agent):** the `_ascii_lower` en-dash bug, plus discriminators for bucket vs
  cumulative threshold, bucket boundary / width, and annual vs quarterly period.
  Each is gated on zero same-pair loss across all three fixtures. Matcher output
  is re-diffed and every changed pair explained.
- **Coordinator:** live `tools.validate_coverage` (ingestion changed in
  iterations 3–5 after the last live check) and fresh cold / stale / warm alerter
  timings with the faster matcher.
- **Weather policy:** unchanged (rejected) pending the operator's decision.

**Results (I: depth-rescued bucket semantics):**
- **En-dash bug fixed.** `matcher._ascii_lower` NFKD-normalises then
  ascii-encodes with `errors="ignore"`; en dash (`–`) and em dash
  (`—`) have no NFKD decomposition, so they were silently DROPPED
  instead of folded to a hyphen — `"2.0–2.5%"` became `"2.02.5%"`
  (digits glued together), unparseable by `contract_spec._num_range` /
  `_RANGE_RE`. Fixed by folding both dashes to `"-"` before normalising.
  Once fixed, the *existing* adjacent-bucket-mismatch gate (run 41) rejects
  the GDP pair on its own — no new discriminator needed for that class.
- **Three new narrow discriminators** in `contract_spec.py`, each gated on
  zero labelled-same pairs lost across all three fixtures:

  | Discriminator | What it catches | Depth-rescued FPs removed | Same lost (any fixture) |
  |---|---|---|---|
  | `_is_cumulative_bucket` (numeric bucket vs open-ended "N or below/above" threshold) | Polymarket narrow 2-sided bucket ("90-91°F") vs Kalshi's open-ended ladder-end rung ("<91°... 90° or below", i.e. everything ≤90) | 2 | 0 |
  | `_week_bucket` (sports "Week N" granularity) | single-week bucket ("Week 1") vs multi-week window ("Week 1 to Week 2") | 1 | 0 |
  | `_fiscal_period` (explicit Q1-4 vs "annual"/"full year") + extending the existing negative/positive direction gate to also read `_numeric_threshold` (not just `_num_range`) | annual GDP reading vs a single quarter's threshold ("Negative GDP growth in 2026" vs "...increase by more than 4.0% in Q4 2026") | 1 | 0 |
  | en-dash fix (existing adjacent-bucket gate, newly reachable) | adjacent GDP buckets sharing a boundary, previously unparseable on the en-dash side | 1 | 0 |

  All four fire only when both sides carry an unambiguous, narrow structural
  signal (an explicit range/week/period tag on both sides, or a genuine
  cumulative idiom paired with a genuine two-sided range on the other side),
  so none touch the vast majority of pairs that don't use this phrasing.
- **Precision:** `endorsed_audit_depth_rescued.json` 94.1% (80/85) → **100%
  (85/85)**, all 5 previously-documented false positives now rejected, by
  exactly the intended gate each time (verified via `d.reasons[0]`). The two
  general fixtures (`endorsed_audit_2026-09-14.json`,
  `endorsed_audit_iter4.json`) are **unchanged**: signal/stratified precision
  stayed at 98.46%/100.0% and 100%/99.49% respectively, 0 labelled-same pairs
  lost on either. Regression floor in `tests/test_contract_spec.py` raised
  from 94.1% to 100% for the depth-rescued fixture.
- **Matcher output change check** (`match_bench.py`, full frozen pools:
  98,578 Kalshi x 165,865 Polymarket): `sig_iter6.json` vs the frozen
  `sig_iter4b.json` reference — **4,739 = 4,739 pairs, 0 differences, 0 score
  changes**, in **62.6 s** (within the ≈60-75 s band). Zero diff is expected:
  every iteration-6 change lives in `contract_spec.py`'s v2 decision layer
  (or, for the en-dash fix, only feeds `contract_spec._num_range`), and
  `match_bench` measures `matcher.is_compatible_match`/candidate-generation
  (v1), which doesn't do range-bucket comparison — confirmed no other
  `matcher.py` code path is en-dash sensitive.
- **Live re-verdict** on the saved `live_pairs_iter5.json` (5,007 pairs,
  saved books; `alerter.compute_signals(min_edge=0.0001,
  min_size=alerter.MIN_DEPTH)`), recomputing `match_spec` only for
  previously-endorsed pairs and trusting a flip only when the new reason is
  one of the four gates above (the flat export drops ingestion-time
  `settle_src`/`full_question` context, so a blind full recompute both loses
  the settlement-source gate and can drift a hair on unrelated
  token-similarity — confirmed harmless, but excluded from the "after" count
  for rigor):

  | | Before | After |
  |---|---|---|
  | Endorsed | 3,828 | **3,822** (-6) |
  | Production signals | 480 | **477** (-3) |
  | Emailable | 36 | **35** (-1) |

  All 6 flips trace to the new gates: 3 adjacent GDP-bucket pairs (en-dash
  fix), 2 `Week N` vs `Week N to N+1` pairs, 1 annual-vs-Q4 GDP pair
  (fiscal-period + direction). The bucket-vs-cumulative-threshold class
  removed 0 live pairs because those weather pairs were already rejected
  upstream by iteration 5's settlement-source gate (NOAA vs Weather
  Company) — the fix still matters for a future weather-policy change that
  relaxes that gate. Suite 800 → **815 passed** (15 new tests covering the
  en-dash fix and all three discriminators), `ruff check --select F,E9,B`
  clean.

**Results (coordinator items):**
- **Bug caught by the live re-check, fixed in `8cb5ee3`.**
  `tools.validate_coverage` crashed in 0.15 s with `UnboundLocalError`. Two
  function-local `import threading` statements in `ingest_kalshi` (added in
  iteration 4) made `threading` local to the whole function, so the standalone
  own-sweep-thread path failed before reaching the import. `discover()` always
  injects its thread, so production scans were unaffected, but the verifier
  wasn't. The fix uses the module-level import. A new regression test reproduces
  the exact error on the previous commit and passes on the fix. An AST scan found
  no other shadowed module imports in `discover.py`.
- **Live `tools.validate_coverage`** (16:56 UTC, sweeps on): **0 real gaps on
  both venues** (`clean=True`).

  | Venue | Ground truth | Ingested | Raw overlap | Real gaps | Drift | Closed since |
  |---|---|---|---|---|---|---|
  | Kalshi | 107,219 | 107,651 | 97.67% | **0** | 2,498 | 2,930 |
  | Polymarket | 169,655 | 169,665 | 99.96% | **0** | 69 | 79 |

  Kalshi's raw overlap is lower than in iteration 1 (100.0%, measured at 06:35
  UTC) because this run was during US trading hours. 15-minute crypto and live
  sports series turn over ~2.5k markets in the gap between ingesting Kalshi and
  walking its ground truth, and that gap is several minutes because the verifier
  runs the Polymarket sweep first. Every miss classifies as drift, and every
  extra as closed since.
- **Alerter cycles** (`--once --dry-run`, back to back, no other load):

  | Cycle | Caches | Wall | Peak RSS | Pairs | Survivable | Emailable | Coverage |
  |---|---|---|---|---|---|---|---|
  | A | Polymarket past 6 h cap (inline sweep), Kalshi stale | 380 s | 4.1 GB | 5,003 | 499 | 39 | 100.0% / 100.0% |
  | B | both fresh-cached | **154 s** | 2.9 GB | 5,001 | 495 | 37 | 100.0% / 100.0% |
  | C | both aged 2 h (stale, background refresh) | **148 s** | 3.2 GB | 5,000 | 489 | 38 | 100.0% / 100.0% |

  Every cycle assembled its email and depth charts. The dry runs left
  `alert_state.json` / `alert_signals.jsonl` untouched: their mtimes are still
  07:19, from pre-fix runs in iteration 3.
- Matcher re-diff under a fourth hash seed: 4,739 = 4,739 pairs, 0 changes, **59 s**.
- Suite **815 passed**, ruff clean.

**Compared with iteration 5:**

| | Iteration 5 | Iteration 6 |
|---|---|---|
| Coverage (live verifier) | not re-run since iteration 1 | **0 gaps / 0 gaps** re-confirmed; verifier crash fixed |
| Depth-rescued signal precision | 94.1% | **100%** (85 / 85) |
| Other fixtures | 98.45% / 100% · 100% / 99.49% | unchanged, 0 same-pairs lost |
| Live endorsed / signals / emailable (iteration-5 books) | 3,828 / 480 / 36 | 3,822 / 477 / 35 |
| Alerter cycle, warm / stale | 125–216 s (iteration 4) | **154 s / 148 s** |
| Alerter cycle, no usable cache | 352 s | 380 s (still > 300 s) |
| Full-pool matching | 59–72 s | 59–63 s |
| Tests | 800 | 815 |

**Next fixes to consider (iteration 7 proposal):**
1. **Verifier skew.** Walk each venue's ground truth *concurrently with* (or right
   after) that venue's own ingestion, instead of after both, so drift reflects
   real churn rather than the verifier's ~5–10 min gap. Report coverage on the
   intersection of markets alive at both instants.
2. **No-cache cold cycle (380 s).** The one remaining > 300 s case, paid only by
   a process with no usable cache (first run, or > 6 h idle). Options: persist
   and ship a seed orphan cache on deploy; or run the Polymarket sweep with
   concurrent keyset segments if Gamma cursors can be partitioned by id range
   (needs a probe).
3. **Coverage in `ops.py`.** `health.py` already degrades on coverage; also show
   the per-venue drift and closed-since counts from the last live verifier run,
   if one was written (`--json`).
4. **Weather policy** (operator decision, open since iteration 5).
5. **Grow the audit fixtures from live signals each iteration** so precision
   floors track the current catalog. The depth-rescued fixture is now fully
   fixed (100%), so it no longer finds new problems.

---

## Expansion backlog (beyond ingestion coverage)

Ordered by expected arb surface per unit of work (sources:
[../EXPANSION_PROPOSAL.md](../EXPANSION_PROPOSAL.md) plus this effort's measurements).

1. **Structured sports joins:** (league, team codes, start time) keys. 287
   game events join by exact code, where title Jaccard can't match "Denver wins"
   ↔ "Broncos vs. Chiefs".
2. **US House joins by district code:** 350 / 350 districts. The outcome must match
   on party *and* candidate surname.
3. **Ladder / bucket synthesis** for the now-ingested Kalshi stats-only ranges
   (margin of victory, vote share) ↔ Polymarket range buckets. Needs N-leg
   `book_arb`.
4. **Catalog-price screen, then CLOB books only for near-positive pairs.** Keeps
   the per-cycle book fetch bounded now that pairs number in the thousands.
5. **Many-to-one event matching** (one Kalshi event ↔ several Polymarket events),
   deduped at market level.
6. **Incremental sweeps.** Both keyset cursors are id-ordered, so a cycle could
   resume from the last cursor and only do a full orphan re-walk hourly. That
   removes the ~260 s Polymarket sweep from most cycles.
7. **Snapshot-diff matching.** Keep the previous cycle's catalogs in memory (the
   alerter is long-running). Caveat, corrected in iteration 4: re-matching *only
   changed events* is **not** equivalent to a full re-match. The greedy 1-to-1
   assignment is global, so a changed event can claim a market an unchanged pair
   held. The exact version caches the *scored candidate lists* per event pair,
   keyed by a content hash, recomputes only changed entries, and then re-runs the
   (cheap) global greedy assignment. Worth it once matching (~72 s) dominates.
8. **Coverage SLO in `health.py` / `ops.py`.** Alert when a cycle's `[coverage]`
   drops below 100% or a sweep reports `partial` / `failed`.

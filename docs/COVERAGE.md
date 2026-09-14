# Catalog coverage — Kalshi × Polymarket

**Goal:** every open market on both venues enters `discover()`'s matching pool,
and every catalog market is accounted for as *ingested* or *excluded: reason*.
Matching quality (which of those markets pair up) is a separate concern, covered
in [MATCHING_SAFETY.md](MATCHING_SAFETY.md) and
[EXPANSION_PROPOSAL.md](EXPANSION_PROPOSAL.md).

## Definition of "open" and the ground truth

| Venue | Open market | Independent ground truth |
|---|---|---|
| Kalshi | `status == "active"` | `GET /markets?status=open&mve_filter=exclude` (cursor, 1000/page, ~20 s) |
| Polymarket | `active and not closed` | Gamma `GET /markets/keyset?closed=false` (cursor, 100/page, ~290 s) |

Multivariate (MVE / parlay-combo) Kalshi markets are out of scope. They never
appear in `/events` and are structurally incomparable with Polymarket binaries.

Two API quirks make a single listing insufficient on both venues:
- **Kalshi:** `/events` omits some events whose markets are `active`. Some are
  absent under every status (`KXBEATLESRECORD-26`); some are listed, but not under
  `status=open` (`KXHOUSERACE-DCAL-26`). `/markets` is the authority.
- **Polymarket:** `/events/keyset` omits archived / inactive / closed events, yet
  some of their markets are still open and hold resting orders. The `archived` /
  `active` query params are ignored by both keyset and offset `/events`.

`python -m tools.validate_coverage` checks the pipeline against both ground
truths live (see [Verifying coverage](#verifying-coverage)).

## How ingestion reaches 100%

| Venue | Source | Cost | What it covers |
|---|---|---|---|
| Kalshi | `/events?status=open&with_nested_markets=true` | ~60 pages, ~6 s | every open event with **all** its markets and top-of-book inline (no per-event truncation: 400-strike ladders arrive whole) |
| Polymarket | Gamma `/events/keyset?closed=false` | ~210 pages, ~24 s | every open event with its embedded markets |
| Kalshi | `/markets?status=open&mve_filter=exclude` sweep | ~107 pages, 20–60 s (background thread) | "orphan" active markets whose event Kalshi's `/events` listing omits (18 live on 2026-09-14, e.g. `KXBEATLESRECORD-26`); parent fetched via `GET /events/{ticker}` and run through the same filters |
| Polymarket | Gamma `/markets/keyset?closed=false` sweep | ~1,660 pages, ~260–290 s (background thread; orphan subset cached 1 h in the alerter) | "orphan" open markets whose parent event is archived / inactive / closed and so is omitted by `/events/keyset` (~440–480 live) |

Exclusion rules, applied **after** ingestion and always counted:

- **Not tradeable**: Kalshi `finalized` / `closed` / `inactive` / `initialized`;
  Polymarket `closed` or `active=false`.
- **Held out of matching, but still in the pool**: Kalshi stats-only ranges
  (voter turnout, margin of victory, …) and multi-leg combo titles. Until iteration 1
  these were silently dropped at ingestion. They're now ingested and tagged, and
  kept out of the text matcher, because pairing them correctly needs ladder/bucket
  synthesis (EXPANSION_PROPOSAL §2c).
- **User filters** (`--category`, `--days`, `--max-events`, `--poly-scan`)
  exclude nothing by default. When set, their exclusions are counted too.

## Before / after

Measured against live snapshots taken 2026-09-14 (01:37–01:43 UTC).

| | `main` @ 4b74301 (as pulled) | Uncommitted WIP (after pull) | Iteration 1 (this change) |
|---|---|---|---|
| Kalshi catalog fetch | `/events` without markets + one `/markets` call **per event** (429 storms, ~50 events/min) | nested `/events`, 11,732 events in ~6 s | nested `/events` + `/markets` orphan sweep |
| Kalshi events scanned in production | 1,500 cap (alerter), 200 (dashboard), horizon 730 d | cap still applied by callers | **all** (no cap, no horizon) |
| Kalshi open markets in pool | ≤ markets of 1,500 events | 98,559 of ~106k (stats-only + multi-leg dropped; 18 unlisted-event markets missed) | **105,895 / 105,895 (100.0%)**; 7,502 stats-only/multi-leg held out of matching only |
| Polymarket catalog walk | offset `/events`, **silently stops at offset ~2,100** (HTTP 422 read as end-of-catalog) → ~10% | `/events/keyset`, full walk | same |
| Polymarket event filter | Kalshi-keyword substring filter | keyword filter kept: 19,818 of 20,729 events | **none**: all 20,728 events |
| Polymarket markets in pool | ~1,212 events' markets | 187,946, of which ~22k **closed** (only `active` checked) | **166,203 / 166,203 open (100.0%)**; 27,231 closed + 32,028 inactive excluded |
| Polymarket orphans (open market, parent event not listed) | never seen | never seen | 442 added by the market sweep |
| Live ground-truth check (`validate_coverage`) | — | — | Kalshi 0 gaps (100.0%), Polymarket 0 gaps (99.89%, rest = markets created mid-run) |
| Matching on full pools | n/a (pools were tiny) | 951–992 s, non-deterministic | **70–77 s** (12.4×), deterministic, byte-identical pairs to the old algorithm |
| Matched pairs / v2-endorsed | 50 / 23 | 4,790 / 3,764 (offline) | 4,723 / 3,705 (live) |
| Live books on endorsed pairs | — | — | 99% multi-level on both legs |
| End-to-end live `discover --show-prices` | — | ~17 min+ (matching alone) | 614 s wall, 4.6 GB peak RSS |
| Coverage visible to the operator | no | no | per-venue `[coverage]` line, `--coverage-json`, `tools/validate_coverage.py` |

### Pipeline health by iteration (all at 100% / 100% coverage)

| | Iteration 1 | Iteration 2 | Iteration 3 | Iteration 4 |
|---|---|---|---|---|
| Audit precision, signal / stratified (2026-09-14 fixture) | 94.1% / 97.3% | 96.1% / 98.0% | 98.45% / 100.0% | 98.45% / 100.0%, recall 100% |
| Audit precision, iter4 fixture | — | — | 93.75% / 98.0% | **100% / 99.49%** |
| Live book enrichment | per-market, ~280 s | screened, ~250 s | batched, 4.4 s | same |
| Alerter cycle | 614 s (discover only) | 609 s | 343 s cold / 125 s warm | 352 s first-ever / **170 s stale-cache** / 125–216 s warm |
| Full-pool matching | 70 s | 70 s | 70 s | 108 s (regression; fixed in iteration 5 → 59–72 s, identical output) |
| Signal economics | top-of-book | top-of-book | top-of-book | **depth-walked at `min_size`** |
| Dry run | — | writes state (bug) | writes state (bug) | **side-effect free** |
| Coverage monitoring | `validate_coverage` (manual) | same | + `health.py` DEGRADED on < 100% or failed sweep | same (`sweep=stale` is OK) |

Iteration results are appended to [history/COVERAGE_ITERATIONS.md](history/COVERAGE_ITERATIONS.md).

## Verifying coverage

```bash
python -m tools.validate_coverage          # exit 0 iff no real gaps on either venue
python -m tools.validate_coverage --json   # machine-readable report
python discover.py --coverage-json cov.json
```

Misses are classified as `drift` (the market or its event was created after
ingestion started, so the next cycle picks it up) or `gap` (a real coverage bug).
Ingested ids missing from the ground truth are reported as `closed_since`.

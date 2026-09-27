# Dashboard and Query Assistant: Implementation Plan

Date: 2026-09-26. Source review: [FRONTEND_REVIEW.md](FRONTEND_REVIEW.md).

The matcher is in good shape (the 2026-09-26 top-10 audit found 9 of 10 pairs
matched). This plan turns the dashboard into a trustworthy review tool and adds a
query assistant on top of persisted data.

## Model routing

Planning and verification are done by Opus; implementation is delegated to
cheaper models to keep token spend down.

| Role | Model | Responsibilities |
|---|---|---|
| Planner / verifier | Opus | Write each task spec, review diffs, run tests, verify in the browser, decide merge |
| Implementer | Sonnet | Multi-file or judgement-heavy changes (backend jobs, SQLite schema, agent tools, matcher guards) |
| Implementer | Haiku | Mechanical, well-specified edits (CSS/type scale, button/aria conversions, fixtures, label renames) |

Workflow per task:

1. Opus writes a self-contained spec: files, exact behaviour, acceptance tests.
2. A Sonnet or Haiku subagent implements it in an isolated worktree.
3. Opus reviews the diff, runs `pytest`, and checks the dashboard in the browser
   (desktop and 390 x 844) before merging.
4. Failures go back to the same implementer with the concrete failing output;
   escalate to Sonnet (from Haiku) or Opus only after two failed rounds.

## Phase 0: Land matcher work and close the last false positive

| Task | Model | Acceptance |
|---|---|---|
| Commit semantic guards, ranked-audit tool and fixtures | Opus | Full suite green, merged to `main` |
| Competition-identity guard: same school, different competition (Steel Bridge vs NCAA Football) is vetoed | Sonnet | New regression fixture for the rank-9 pair; oracle and full suite pass |
| Rerun `tools.audit_ranked_signals --top-n 10 --min-size 20` and freeze as a fixture | Haiku | Top 10 all settlement matches |

## Phase 1: Fix the current UI (frontend only)

All in `static/index.html`; no backend changes.

| Task | Model | Acceptance |
|---|---|---|
| Key selection on `kalshi_ticker\|poly_token_id`; re-sync table, detail and Book Scan after sort/filter | Sonnet | Select pair B, reverse sort, Book Scan still targets B |
| ARB/PRICED command toggles flip once; unknown commands show an error | Haiku | Indicator changes on each command |
| Split badges: `matcher-accepted`, `human-reviewed`, quote source + age, `depth-checked` | Sonnet | No generic "VERIFIED"; quote age visible in Book Scan |
| Responsive layout: collapsible sidebar under 900px, 13-14px base type, ~8 default columns + column picker, scrollable table, detail drawer | Sonnet | Usable at 390 x 844 with no overlap |
| Accessibility: buttons/checkboxes instead of clickable divs, keyboard row selection, dialog semantics and focus trap on book modal, focus styles | Haiku | Full keyboard walkthrough works |

## Phase 2: Persistence, background scans, review queue

SQLite at `data/dashboard.db`:

- `scans`: id, started/finished, params, coverage, status
- `pair_snapshots`: scan_id, pair key, prices, edge, depth, close_time, matcher reasons
- `market_evidence`: market_id, venue, rules text, source URL, fetched_at, content hash
  (requires `discover.py` to retain rules/descriptions it currently discards)
- `reviews`: pair key, verdict (match/mismatch/uncertain), reason, reviewer, time, rules hash

| Task | Model | Acceptance |
|---|---|---|
| Schema, migrations, write-path from scans | Sonnet | Unit tests for round-trip; no change to alerting/execution |
| Retain rule text in `discover.py` into `market_evidence` | Sonnet | Evidence rows exist for both legs of every pair |
| Background scan jobs: `POST /api/scans`, `GET /api/scans/{id}`, cancel, single in-flight guard | Sonnet | Second scan is refused while one runs; UI stays usable |
| `GET /api/scans/latest` + restore on load; distinct empty states | Haiku | Reload keeps results; never-scanned / empty / filtered / failed / stale are distinguishable |
| `GET /api/audit/ranked` wrapping `audit_ranked_signals.run()` | Haiku | Matches CLI output |
| Review Queue tab + `POST /api/reviews`; reviews go stale when the rules hash changes | Sonnet | Verdicts survive reload; changed rules flag the review |
| Yield columns: edge, $ profit at size, capital, return on capital, days to settle, annualized (labelled estimate) | Sonnet | Each sortable; annualized hidden when close_time is missing |

## Phase 3: Ask panel (tool-calling assistant)

The model may only call bounded, read-only tools over the Phase 2 store:

| Tool | Example question |
|---|---|
| `search_pairs(category, review_state, min_depth, max_days_to_settle, sort, limit)` | "Top 10 reviewed pairs settling within 30 days" |
| `get_pair_evidence(pair_key)` | "Why was Notre Dame matched?" |
| `calculate_budget(pair_key, usd)` (wraps `_compute_book_arb`) | "What does $500 get me?" |
| `compare_scans(a, b)` | "What changed since yesterday?" |

Rules: numbers come only from queries and existing calculators; every answer cites
scan id/time, tool inputs and rule passages, and links back to table rows; no raw
SQL, shell or order placement; missing rules or stale books answer "unknown/stale";
market text is evidence, never instructions.

| Task | Model | Acceptance |
|---|---|---|
| Tool implementations + schema validation | Sonnet | Unit tests per tool, including stale/missing cases |
| Agent loop endpoint `POST /api/ask` | Sonnet | Answers include citations; refuses out-of-scope actions |
| Side-panel UI with row links | Haiku | Clicking a cited row selects it in the table |

Open decision: which model powers Ask (existing `ai_verify.py` uses `deepseek-chat`;
recommendation is Claude Sonnet for tool-calling reliability).

## Phase 4: Rule-text retrieval

| Task | Model | Acceptance |
|---|---|---|
| SQLite FTS5 index over `market_evidence`, used by `get_pair_evidence` | Sonnet | Snippets returned for keyword queries |
| Eval set from known mismatch fixtures (Inter vs AC Milan, SCOTUS, Notre Dame, ...) | Haiku (fixtures) + Opus (grading) | Assistant explains each mismatch from cited rules |
| Embeddings | — | Only if the eval shows keyword recall gaps |

## Order

0 → 1 → 2 → 3 → 4. Phases 0 and 1 are independent of the open decisions and
can start immediately. Phase 2 is the largest and the foundation for 3 and 4.

# Dashboard UX and Query Assistant Review

Review date: 2026-09-26

Scope: existing FastAPI dashboard, frontend interactions, ranked audit integration,
and feasibility of a query assistant. Review only; no application code changed.

## Findings, in Priority Order

1. **P1: Selection can point at a different market after sorting or filtering.**
   `static/index.html:1006` replaces `displayedPairs` while retaining the numeric
   `selected` index. The detail panel is not rerendered, but `runBookArb` at line
   925 reads the newly ordered array. A two-row JavaScript reproduction selected
   Pair B, reversed the sort, and observed Book Scan targeting Pair A while the
   detail still displayed Pair B. Store selection by stable market IDs and
   reconcile the table, detail, and book action together after every update.

2. **P1: Evidence and freshness are too easy to misread.**
   The default gate at `static/index.html:750` checks matcher acceptance and a
   risk flag; it is not human settlement verification. The green ARB treatment
   at line 1017 depends on positive quoted edge, not current executable depth.
   The signals label at line 1230 maps `clob_verified` to generic VERIFIED.
   Book Scan at line 933 prefers retained scan books without displaying their
   age. Distinguish matcher acceptance, rule review, quote source, quote age,
   and depth checks. Add an explicit refresh-books action and evidence drawer.

3. **P2: The command bar has broken filters.**
   At `static/index.html:1261`, ARB and PRICED invert their state before calling
   functions that invert it again. ARB was reproduced in the browser: its
   indicator remained off. Call each toggle once and show unknown-command errors.

4. **P2: Phone layout is unusable; desktop text is difficult to read.**
   At 390 x 844, the 200px sidebar leaves about 184px of content, panel-heading
   text overlaps, and table columns disappear outside the visible area.
   `static/index.html:113` fixes the main height and sidebar width; no responsive
   breakpoints exist. Desktop uses mostly 9-12px type, muted gray text, and 17
   table columns. Use a collapsible filter panel, larger readable type, fewer
   default columns, explicit horizontal table scrolling, and a full-height
   detail drawer. Keep numeric density but give market names enough room.

5. **P2: Reload loses scan results, and initial empty state is misleading.**
   The state at `static/index.html:650` is browser memory only. Initialization
   calls `renderPairs` through `setCategory`, replacing the initial scan prompt
   with 'No pairs match current filters' before any scan has run. The API at
   `server.py:429` returns a scan without storing a dashboard snapshot. Persist
   scan IDs and results, restore the latest snapshot on load, and distinguish
   never scanned, no results, filtered out, failed, and stale states.

6. **P2: Long scans block the whole interface without useful progress.**
   `static/index.html:786` awaits a single request behind a full-screen overlay.
   There is no progress feed, cancellation, or shared in-flight guard; keyboard
   shortcuts can still start scans. Use a background scan job, progress/status
   endpoint, deduplication, and cancellation. Preserve the previous snapshot
   while a new one is running.

7. **P2: Common controls are not accessible through ordinary keyboard focus.**
   Navigation/actions use clickable divs/spans, and rows use onclick without
   keyboard semantics. The book modal lacks dialog semantics and focus handling.
   Replace commands with buttons, binary filters with checkboxes, expose sort
   state, add visible focus styles, and trap/restore focus for the dialog.

8. **P2: The requested top-ten review workflow is absent from the app.**
   `tools/audit_ranked_signals.py:67` already supplies a callable audit function,
   but `server.py` has no ranked-audit route and the frontend has no persistent
   verdict editor. Add a top-ten review table with match/mismatch/uncertain,
   reasons, reviewer/time, source links, and retained rule versions.
   The CLI sorts by `exec_net` at line 102, not annualized yield. Label edge,
   return on deployed capital, executable profit, and annualized estimates
   separately; any time-based estimate needs an explicit settlement assumption.

## Proposed Product Shape

- Opportunities: market pair, review state, executable profit, capital required,
  return, settlement horizon, and quote age. Move similarity and legacy fee
  comparisons into diagnostics.
- Pair detail: side-by-side questions/rules, differences in date/threshold/source,
  reviewer verdict, venue links, and depth calculations at a chosen budget.
- Scans: current progress, latest completed snapshot, coverage, errors, history,
  and changes since a previous scan.
- Review queue: highest-ranked candidates and unresolved mismatches, with saved
  judgments that survive reloads.
- Ask: a side panel whose answers link back to the same table rows and evidence.

## Query Assistant Design

Use a small set of typed, validated tools for exact queries and existing Python
calculators for arithmetic. Use retrieval for textual evidence. Suggested tools:

- `search_pairs`: category, review state, minimum depth, settlement window, rank.
- `get_pair_evidence`: market IDs, original rule text, source URL, fetched time,
  rule version, matcher reasons, and saved reviews.
- `calculate_budget`: call the existing book-arbitrage calculator for a pair and
  budget; return book timestamps alongside all computed values.
- `compare_scans`: additions, removals, changed edges, and changed evidence.

Examples: 'Show the top 10 reviewed pairs settling within 30 days'; 'Why was
Notre Dame matched across these competitions?'; 'What does $500 support at the
last observed depth?'; 'Which pairs changed since yesterday?'

Start with persisted scans, market snapshots, and review records in SQLite.
SQLite FTS5 provides full-text retrieval, ranked matches, and snippets:
https://www.sqlite.org/fts5.html
Consider semantic embeddings later if evaluated questions demonstrate a recall
gap. Numeric filtering and ranking should remain deterministic database queries.

Discovery currently extracts compact settlement-source tags and discards original
rules/descriptions (`discover.py:373`, `discover.py:450`). Add a separate evidence
store for original text, market ID, source URL, fetched time, and content hash.
Existing `ai_verify.py` provides an optional verifier and verdict cache, but is
not a chat assistant or a complete evidence store. Its no-opinion/failure state
must remain distinct from a positive review in assistant responses.

Every answer should identify its scan/time, calculated inputs, and linked rule
passages. Treat retrieved market text as evidence, never as tool instructions.
Use bounded read-only tools initially; do not expose arbitrary SQL, shell access,
or order placement. Missing rules or stale books should produce an explicit
unknown/stale answer, not an invented verdict or fresh-price claim.

## Suggested Build Order

1. Fix selection, command toggles, evidence labels, and responsive accessibility.
2. Persist snapshots/reviews and expose ranked audits through background jobs.
3. Add the Ask panel with structured filtering and existing depth calculators.
4. Add cited rule retrieval and evaluate it against known mismatch fixtures.

## Verification and Limits

Inspected the running dashboard at desktop size and 390 x 844. Reproduced the
ARB command bug in-browser and the selection bug using the actual frontend
JavaScript with a minimal DOM harness. Read frontend, API, discovery, audit, and
AI-verifier code. The saved signals view returned zero entries. No full market
scan was launched for this review, so populated large-table performance and live
scan completion remain unmeasured. No application tests or code were changed.

# Pipeline Flow

This document explains the end-to-end control flow in execution order.

## Fixed-Series Scan

1. `monitor.main()` parses CLI flags.
2. `monitor.run_one_scan()` creates a scan ID and timestamp.
3. `pipeline.fetch_polymarket()` retrieves Gamma markets and optional CLOB books.
4. `pipeline.fetch_kalshi()` retrieves Kalshi markets and optional full books.
5. `matcher.match_markets()` filters and scores candidate pairs.
6. `arb.find_arb()` checks both two-leg arbitrage directions.
7. `monitor._verify_poly_clob()` and `monitor._verify_kalshi_clob()` re-fetch
   live books for profitable candidates.
8. `monitor.ArbSignal` records the verified or rejected signal.
9. `monitor.append_signals()` appends signal JSON lines for audit history.
10. `monitor.execute_signal()` optionally passes verified signals to
    `executor.Executor`.

## Discover Scan

1. `discover.discover()` fetches Kalshi events.
2. Events are filtered for parlays, category, and horizon.
3. Kalshi markets are fetched per event.
4. Search keywords are derived from Kalshi event titles.
5. Polymarket events are searched using those keywords.
6. Raw markets are converted into `MarketSnapshot` objects.
7. `_match_groups_then_individual()` matches parent events first.
8. `_match_outcomes_within_group()` matches outcomes inside compatible event
   groups.
9. Remaining markets fall back to `matcher.match_markets()`.
10. Optional live order-book enrichment runs only after pairs are found.

## Execution Flow

1. `monitor.execute_signal()` builds economic `TradeIntent` objects.
2. `executor.pre_flight_checks()` validates risk, live price, and optional
   balances.
3. `Executor.execute()` places leg A first.
4. If leg A fails, leg B is not attempted.
5. If leg B fails after leg A, the executor attempts to cancel leg A.
6. If both legs succeed, the result records gross cost and estimated net profit.

## Generated Files

- `signals.jsonl`: append-only signal audit log.
- `monitor.log`: scan and execution log.
- `output/`: optional timestamped pipeline snapshots.
- `pairs.json`: optional discovery output when requested from the CLI.


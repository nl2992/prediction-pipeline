# Execution Safety

Execution is intentionally conservative. Dry-run is the default, and live
execution requires explicit flags plus credentials.

## Price Semantics

The code stores trade intent prices as economic costs:

- Buying YES at `0.64` costs `0.64`.
- Buying NO when YES bid is `0.80` costs `0.20`.

Kalshi's API represents buying NO as selling YES, so `TradeIntent` converts NO
economic cost into the complementary venue price through
`TradeIntent.venue_limit_price`.

Polymarket has separate YES and NO token IDs. The executor buys the requested
token directly rather than selling the YES token to simulate NO exposure.

## Pre-Flight Checks

`pre_flight_checks()` enforces:

- total position cost under `max_position_usd`;
- positive net profit after worst-case fees;
- live CLOB price still within tolerance;
- optional Kalshi balance sufficiency when authenticated clients are available.

## Live Verification

Signal verification and execution pre-flight are separate on purpose:

- monitor verification filters scan output;
- executor pre-flight protects against price drift between signal creation and
  placement.

Both checks should remain in place even if they appear redundant.

## Failure Handling

Orders are placed sequentially:

1. Leg A is attempted.
2. Leg B is attempted only if leg A succeeds.
3. If leg B fails, leg A cancellation is attempted best-effort.

This does not eliminate leg risk. It limits it and makes failures visible in
`ArbExecution.notes`.

## Live Mode Checklist

Before using `--execute --no-dry-run`:

1. Run `python smoke_test.py`.
2. Run monitor with `--execute --dry-run`.
3. Confirm credentials are configured.
4. Start with small `--size-contracts`.
5. Inspect `signals.jsonl` and `monitor.log`.
6. Confirm all expected signals are `clob_verified=True`.


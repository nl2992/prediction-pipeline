# Codebase Guide

This guide documents the repository at the module and function level. It is
intended to answer "where does this behavior live?" without filling the source
with comments that repeat each line of Python syntax.

## Runtime Flow

1. `pipeline.py` fetches and normalizes Polymarket and Kalshi market data.
2. `matcher.py` pairs markets that appear to describe the same contract.
3. `arb.py` computes two-leg YES/NO arbitrage opportunities for matched pairs.
4. `monitor.py` runs scans, verifies live CLOB prices, writes `signals.jsonl`,
   and optionally calls `executor.py`.
5. `executor.py` converts verified signals into exchange-specific orders.
6. `server.py` exposes scan and signal APIs for `static/index.html`.
7. `discover.py` performs broad organic discovery across Kalshi events and
   Polymarket event catalogs.

## Data Model

### `pipeline.PriceLevel`

Represents one order-book level with `price` and `size`.

### `pipeline.OrderBook`

Stores normalized YES-side bids and asks. `best_bid`, `best_ask`, `mid`, and
`spread` are derived properties used throughout matching, arbitrage detection,
and CLOB verification.

### `pipeline.MarketSnapshot`

The common cross-exchange market shape. It carries exchange identity, market and
event identifiers, title, close time, normalized order book, and source-specific
metadata under `extra`.

### `matcher.MatchedPair`

Represents one Polymarket/Kalshi pairing with title similarity, close-time delta,
combined confidence, and an override flag.

### `arb.ArbOpportunity`

Represents one checked arbitrage direction, including leg prices, sizes, costs,
fees, net profit, and the pair that produced it.

### `monitor.ArbSignal`

The serialized signal shape written to `signals.jsonl`. It includes pair
metadata, prices, verification notes, execution notes, and CLOB live values.

### `executor.TradeIntent`, `TradeResult`, `ArbExecution`

`TradeIntent` describes a single order leg in economic terms. `TradeResult`
records one attempted leg. `ArbExecution` records the two-leg result.

## `pipeline.py`

`_parse_polymarket_book(raw_book)` converts CLOB `bids`/`asks` into normalized
`OrderBook` objects and sorts them defensively.

`fetch_polymarket(...)` fetches Gamma markets, optionally enriches with CLOB
order books, falls back to `outcomePrices` when no live book exists, and stores
Polymarket-specific metadata such as CLOB token IDs and outcome labels.

`_parse_kalshi_top_of_book(market)` builds a one-level YES-side book from
Kalshi inline market fields.

`_parse_kalshi_full_book(raw)` reconstructs YES asks from Kalshi NO bids because
Kalshi returns bid ladders for both YES and NO, not explicit asks.

`fetch_kalshi(...)` fetches active Kalshi markets, loads full order books when
requested, falls back to inline top-of-book data on failure, and normalizes the
result into `MarketSnapshot` objects.

`run_pipeline(...)` is the programmatic entry point for fixed-source scans. It
fetches both exchanges, optionally matches markets, optionally computes
arbitrage, and optionally saves timestamped JSON snapshots.

`_save_results(...)` writes normalized output to disk using dataclass-aware JSON
serialization.

## `matcher.py`

`_tokens(title)` lowercases, preserves decimal numbers, strips punctuation, and
removes stopwords to produce comparison tokens.

`_jaccard(a, b)` computes token overlap. This is the base text-similarity score.

False-positive guard helpers extract structured evidence:

- `_domains(text)` detects broad categories such as sports, election, economic.
- `_offices(text)` detects offices such as president, Senate, governor, mayor.
- `_parties(text)` normalizes party names.
- `_jurisdictions(text)` detects countries and U.S. states.
- `_years(text)` extracts election or resolution years.
- `_rates(text)` extracts percentage/rate levels.
- `_proper_names(text)` extracts candidate/person names.
- `_contract_actions(text)` detects contract predicate classes such as win,
  run/declare, finish position, ticket, occur, leave role, and location.
- `_is_generic_winner_market(text)` detects generic "who will win" markets.
- `_is_party_contract(text)` detects party-only contracts.
- `_is_generic_location_market(text)` and `_is_specific_location_option(text)`
  protect location markets from matching option rows.

`is_compatible_match(poly, kalshi)` applies the deterministic vetoes. It rejects
pairs that share text but disagree on domain, office, party, jurisdiction, named
person, predicate, year, or rate level.

`_parse_dt`, `_close_delta_hours`, and `_confidence` parse close times and add a
bounded time-proximity component to the confidence score.

`match_markets(...)` performs greedy one-to-one matching. Manual overrides are
accepted first. Remaining markets are scored only if they pass
`is_compatible_match`, the minimum title similarity, and optional token-ratio
guard.

## `arb.py`

`find_arb(pairs, fee_poly, fee_kalshi, min_net_profit_pct)` checks both
directions for every matched pair:

- Buy YES on Polymarket and buy NO on Kalshi.
- Buy YES on Kalshi and buy NO on Polymarket.

The module uses the worse exchange fee as a conservative payout-fee estimate.

`_min_size(a, b)` returns the limiting available size for the two legs.

## `monitor.py`

`_setup_logging(log_file, level)` configures console and file logging.

`_resolve_poly_token(snap, side)` maps Polymarket outcome labels to YES/NO CLOB
token IDs, falling back to the common binary token order when labels are absent.

`_verify_poly_clob(...)` re-fetches a Polymarket token book and validates the
requested live side, price drift, and depth.

`_verify_kalshi_clob(...)` re-fetches a Kalshi book and validates either live
YES bid or derived YES ask.

`run_one_scan(...)` runs the fixed-source path: fetch, match, arbitrage, CLOB
verify, and return a `ScanSummary`.

`run_discover_scan(...)` runs the broad discovery path and converts discovered
pairs into monitor signals.

`append_signals(signals, signals_file)` appends signal JSON lines.

`execute_signal(...)` translates one verified `ArbSignal` into `TradeIntent`
objects and calls `Executor`.

`print_scan_summary(...)` logs concise scan output.

`_parse_args()` and `main()` implement the polling CLI.

## `discover.py`

`_is_parlay` and `_is_parlay_market` remove multi-leg or stats-only markets that
do not map cleanly to binary Polymarket contracts.

`_category(event)` classifies Kalshi events into election, economic, political,
sports, or pop buckets.

`_derive_keywords(title)` extracts search terms for Polymarket event search.

`_k_snap`, `_p_snap`, and `_p_snap_from_event` convert raw catalog objects into
`MarketSnapshot` objects without expensive order-book calls.

`_enrich_kalshi` and `_enrich_polymarket` fetch live order books only after
candidate pairs have been found.

`_normalise_tokens(title)` extends matcher tokenization with economic direction
synonyms.

`_match_outcomes_within_group(...)` pairs outcomes inside a matched event group.
It requires deterministic compatibility and some outcome-label overlap before
price similarity can influence the score.

`_match_groups_then_individual(...)` first matches event groups, then falls back
to stricter individual market matching for leftovers.

`discover(...)` is the organic discovery entry point and returns formatted pair
dictionaries for the CLI, monitor, server, and dashboard.

`_print_results(...)` and `main()` implement the CLI output.

## `executor.py`

`TradeIntent.venue_limit_price` converts economic NO cost into Kalshi's required
YES-side sell price.

`check_price_still_valid(intent, price_tolerance)` re-fetches live book data and
checks whether a leg's intended economic price is still available.

`pre_flight_checks(...)` enforces position limits, positive net profit, live
price checks, and optional balance checks.

`Executor` owns live/dry-run placement. `_place_kalshi` maps economic YES/NO
legs to Kalshi order sides. `_place_polymarket` buys the requested YES or NO
token directly. `execute(...)` places leg A then leg B and attempts cleanup if
leg B fails. `intents_from_arb_opportunity(...)` converts an `ArbOpportunity`
into order intents.

## `kalshi/client.py`

This file is the Kalshi Trade API v2 wrapper.

Market-data methods are public:

- `get_markets`, `get_all_markets`
- `get_events`, `get_all_events`
- `get_series_list`
- `get_orderbook`
- `parse_top_of_book`

Authenticated methods require API key and RSA private key:

- `_auth_headers`
- `get_balance`
- `get_positions`
- `place_order`
- `cancel_order`
- `get_order`

## `polymarket/client.py`

This file wraps Polymarket Gamma and CLOB APIs.

Public discovery methods:

- `get_events`
- `get_markets`
- `search_markets`
- `search_events`
- `get_event_by_slug`
- `get_markets_for_event_slug`

Public CLOB methods:

- `get_orderbook`
- `get_orderbooks`
- `verify_clob_liquidity`
- `get_price`
- `get_midpoint`
- `get_spread`
- `enrich_markets_with_orderbooks`

Authenticated methods:

- `_clob_auth_headers`
- `_clob_get_auth`, `_clob_post_auth`, `_clob_delete_auth`
- `get_poly_positions`
- `place_order`
- `cancel_order`

## `fed_rate_spread.py`

This standalone analysis script focuses on Fed-rate ladders. It fetches Kalshi
KXFED thresholds, builds an implied distribution, scans Polymarket Fed markets,
classifies them into ladders, checks monotonicity violations, verifies CLOB
liquidity, and prints cross-exchange probability comparisons.

## `server.py`

The FastAPI backend exposes:

- `/api/scan` for live full discovery with order-book prices.
- `/api/scan/fast` for catalog-price discovery.
- `/api/signals` for recent `signals.jsonl` entries.
- `/api/status` for exchange connectivity checks.
- `/` for the dashboard HTML.

## `static/index.html`

The dashboard is a single static app. It calls the FastAPI scan endpoints,
renders pair tables, category filters, sorting, detail panes, exchange links,
and signal views.

## `smoke_test.py`

Runs live connectivity and parser sanity checks for Polymarket Gamma/CLOB and
Kalshi market/order-book endpoints.

## `tests/test_core_logic.py`

Regression tests for the high-risk math and matching rules:

- Kalshi full-book reconstruction.
- Arbitrage direction math.
- Polymarket token side resolution.
- Timestamp parsing.
- Confidence scoring edge cases.
- False-positive match vetoes.
- Group matching outcome-overlap requirement.
- Side-specific CLOB verification.
- Kalshi NO execution price conversion.


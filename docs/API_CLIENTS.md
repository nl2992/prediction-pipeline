# API Clients

The repository keeps exchange-specific HTTP details behind client wrappers.
Pipeline code should use normalized snapshots and order books where possible.

## Kalshi Client

File: `kalshi/client.py`

Base URL: `https://api.elections.kalshi.com/trade-api/v2`

Public methods:

- `get_markets()` lists markets with inline top-of-book fields.
- `get_all_markets()` paginates market lists.
- `get_events()` lists event containers.
- `get_all_events()` paginates event lists.
- `get_series_list()` lists series tickers.
- `get_orderbook()` fetches full YES/NO bid ladders.
- `parse_top_of_book()` converts inline fields to floats.

Authenticated methods:

- `get_balance()`
- `get_positions()`
- `place_order()`
- `cancel_order()`
- `get_order()`

Kalshi full books contain `yes_dollars` and `no_dollars`. Both are bid ladders.
YES asks are derived from NO bids.

## Polymarket Client

File: `polymarket/client.py`

Base URLs:

- Gamma: `https://gamma-api.polymarket.com`
- CLOB: `https://clob.polymarket.com`

Public discovery methods:

- `get_events()`
- `get_markets()`
- `search_markets()`
- `search_events()`
- `get_event_by_slug()`
- `get_markets_for_event_slug()`

Public CLOB methods:

- `get_orderbook()`
- `get_orderbooks()`
- `verify_clob_liquidity()`
- `get_price()`
- `get_midpoint()`
- `get_spread()`
- `enrich_markets_with_orderbooks()`

Authenticated methods:

- `get_poly_positions()`
- `place_order()`
- `cancel_order()`

Polymarket binary markets usually expose separate YES and NO token IDs through
`clobTokenIds`. Token IDs should be resolved by matching the parallel `outcomes`
array before falling back to positional assumptions.

## Normalization Boundary

Exchange clients return raw dictionaries. `pipeline.py` is responsible for
converting those dictionaries into:

- `PriceLevel`
- `OrderBook`
- `MarketSnapshot`

Code outside the client and pipeline layers should prefer normalized objects.


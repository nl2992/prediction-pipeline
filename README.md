# Prediction Market Arbitrage Pipeline

A full-stack Python pipeline that ingests live order-book data from
[Polymarket](https://polymarket.com) and [Kalshi](https://kalshi.com),
matches equivalent markets across exchanges, detects two-leg arbitrage
opportunities, verifies them against live CLOBs, and optionally executes
orders — all with a single command.

> **Network note**: Polymarket's APIs block many residential IPs (incl. AU/US).
> The pipeline routes through **Cloudflare WARP by default** — WARP's Cloudflare
> egress (AS13335) reaches `gamma-api.polymarket.com` where a direct connection
> times out. One-time setup (Windows):
>
> ```powershell
> winget install --id Cloudflare.Warp --accept-package-agreements --accept-source-agreements
> & "C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe" registration new
> & "C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe" connect
> ```
>
> WARP is **Always-On** and its service starts automatically, so it reconnects on
> boot and every process (this pipeline included) uses it without further action.
> Verify with `warp-cli status` (→ `Connected`) and the dashboard's `/api/status`
> (→ `polymarket: ok`).

---

## Repository structure

```
prediction_pipeline/
├── pipeline.py          # Data ingest: fetch + normalise both exchanges
├── matcher.py           # Cross-exchange market pairing (Jaccard + close-time)
├── arb.py               # Two-leg arbitrage detection with fee model
├── executor.py          # Order execution engine (dry-run by default)
├── monitor.py           # Continuous polling loop → signals.jsonl
├── fed_rate_spread.py   # Fed rate spread analysis across both exchanges
├── smoke_test.py        # Connectivity & parsing sanity checks
├── requirements.txt
├── polymarket/
│   ├── __init__.py
│   └── client.py        # Polymarket CLOB + Gamma API client (public + auth)
└── kalshi/
    ├── __init__.py
    └── client.py        # Kalshi Trade API v2 client (public + auth)
```

---

## Quick start

```bash
pip install -r requirements.txt   # only `requests` is required

# Verify connectivity
python smoke_test.py

# One-off scan — Fed rate markets, compare across both exchanges
python monitor.py --once \
  --kalshi-series KXFED \
  --poly-keywords "federal funds" "fed rate" "upper bound" "lower bound" \
  --max-close-delta-hours 9999 \
  --min-profit-pct 0 --no-verify --show-unverified

# Continuous monitor — scan every 5 minutes, write live signals
python monitor.py --interval 300 \
  --kalshi-series KXFED \
  --poly-keywords "federal funds" "fed rate" "upper bound" \
  --max-close-delta-hours 9999

# Fed rate deep-dive: implied distribution + monotonicity arb check
python fed_rate_spread.py
```

---

## How it works

### 1. Data ingest (`pipeline.py`)

**Polymarket** — two public APIs:
- **Gamma API** (`gamma-api.polymarket.com`): market discovery.  
  `tag_slug` and `_q` query params are broken on Gamma; the client paginates  
  the full catalog and filters titles client-side when `--poly-keywords` is given.
- **CLOB API** (`clob.polymarket.com`): live order books via `POST /books`  
  (batch token lookup — one request for all markets in a scan).

**Kalshi** — one public API:
- `api.elections.kalshi.com/trade-api/v2`  
  `/markets` for discovery (filter by `series_ticker` or `event_ticker`),  
  `/markets/{ticker}/orderbook` for full depth (public, no auth).

All data is normalised into `MarketSnapshot` / `OrderBook` objects:

```python
from pipeline import run_pipeline

result = run_pipeline(
    polymarket_limit=50,
    kalshi_limit=50,
    kalshi_series_ticker="KXFED",
    poly_keywords=["federal funds", "upper bound"],
    run_arb=True,
)
for opp in result["arb"]:
    if opp.is_profitable:
        print(opp)
```

### 2. Order-book conventions

**Polymarket** — standard CLOB: `bids` sorted price descending, `asks` ascending.  
YES token is resolved by matching the `outcomes` label; index 0 is the safe default for binary markets.

**Kalshi** — bids-only format:
- `yes_dollars`: list of `[price, size]` YES bid levels (ascending price).
- `no_dollars`: list of `[price, size]` NO bid levels (ascending price).
- YES asks are **derived** from NO bids: `yes_ask_price = 1.0 − no_bid_price`.
- Prices are in dollars (0.59 = 59 cents = 59% implied probability).

### 3. Market matching (`matcher.py`)

Greedy 1-to-1 pairing by combined score:

```
confidence = 0.70 × Jaccard(title_tokens_A, title_tokens_B)
           + 0.30 × max(0, 1 − Δhours / max_close_delta_hours)
```

Stopwords are stripped; punctuation removed.  
Manual overrides bypass heuristics entirely.

### 4. Arbitrage detection (`arb.py`)

For each matched pair, two directions are checked:

| Direction | Leg A | Leg B | Gross cost |
|---|---|---|---|
| `poly_yes__kalshi_no` | Buy YES on Poly at `poly_yes_ask` | Buy NO on Kalshi at `1 − kalshi_yes_bid` | `poly_yes_ask + (1 − kalshi_yes_bid)` |
| `kalshi_yes__poly_no` | Buy YES on Kalshi at `kalshi_yes_ask` | Buy NO on Poly at `1 − poly_yes_bid` | `kalshi_yes_ask + (1 − poly_yes_bid)` |

`net_profit = 1.0 − gross_cost − max(fee_poly, fee_kalshi)`

Defaults: `fee_poly=0.02`, `fee_kalshi=0.07`.

### 5. Live CLOB verification (`monitor.py`)

Before flagging a signal, the monitor re-fetches both legs from the live CLOB and rejects if:
- No live bid/ask exists.
- Price has drifted >3 cents from the detected price.
- Polymarket CLOB depth <$10 at best level.

Verified signals are written as JSON Lines to `signals.jsonl`.

### 6. Execution (`executor.py`)

Dry-run by default — logs intended orders without placing them.

```
Kalshi side convention (v2 API):
  side="bid"  → buy YES contracts
  side="ask"  → sell YES (creates NO exposure)
```

Leg A is placed first. If leg A succeeds but leg B fails, leg A is cancelled automatically.

---

## Fed rate spread analysis

`fed_rate_spread.py` provides a more nuanced view of Fed rate markets:

- Fetches the full **KXFED threshold ladder** from Kalshi and derives an implied  
  probability distribution: `P(rate = X%) = P(above X_prev) − P(above X)`.
- Fetches all Polymarket Fed markets (count cuts / upper/lower bound reach).
- Detects **monotonicity violations** within either ladder  
  (e.g. `P(rate reaches 4.75%) > P(rate reaches 4.50%)` — structurally impossible).
- Verifies violations against live CLOBs before flagging (avoids ghost markets).
- Derives cross-horizon implied probabilities:  
  `P(cut H2 | hold June) = (P_year(at least 1 cut) − P(hold June)) / P(hold June)`

```bash
python fed_rate_spread.py                        # auto-detects next FOMC meeting
python fed_rate_spread.py --event KXFED-26JUN    # target specific meeting
```

> **Horizon note**: Kalshi KXFED markets resolve on the FOMC meeting date;  
> Polymarket "reach X% before 2027" markets resolve end-of-year.  
> These are **not directly arbitrageable** but are comparable for spread analysis.  
> Use `--max-close-delta-hours 9999` in `monitor.py` to surface them anyway.

---

## Monitor CLI reference

```
python monitor.py [flags]

Scan control:
  --once                  Single scan then exit
  --interval INT          Seconds between scans (default: 300)

Market selection:
  --poly-limit INT        Polymarket markets per scan (default: 50)
  --kalshi-limit INT      Kalshi markets per scan (default: 50)
  --kalshi-series STR     Kalshi series ticker, e.g. KXFED
  --kalshi-event STR      Kalshi event ticker
  --poly-keywords KW ...  Substring keywords to search Polymarket catalog

Matching:
  --min-match-sim FLOAT       Minimum Jaccard similarity (default: 0.25)
  --max-close-delta-hours FLOAT
                              Normalisation window for close-time proximity
                              scoring (default: 72). Pairs beyond this window
                              are scored on title similarity alone; they are
                              never hard-excluded (sports markets on Kalshi
                              carry a contractual far-out expiry date).

Arb thresholds:
  --min-profit-pct FLOAT  Minimum net profit % (default: 0.5)
  --fee-poly FLOAT        Polymarket fee (default: 0.02)
  --fee-kalshi FLOAT      Kalshi fee (default: 0.07)

CLOB verification:
  --no-verify             Skip live CLOB recheck (not recommended)

Execution:
  --execute               Auto-execute verified signals
  --dry-run               Log orders only, no real placement (default)
  --no-dry-run            Live placement (requires credentials in env)
  --max-position FLOAT    Max USD per two-leg trade (default: 100)
  --size-contracts FLOAT  Contracts per leg (default: 10)

Output:
  --signals-file PATH     JSONL signal file (default: signals.jsonl)
  --log-file PATH         Log file (default: monitor.log)
  --log-level STR         DEBUG | INFO | WARNING (default: INFO)
  --show-unverified       Print CLOB-failed signals too
```

---

## Credentials (optional — for order placement only)

All market-data endpoints are public. Credentials are only needed for `--execute --no-dry-run`.

```bash
# Kalshi
export KALSHI_API_KEY="your-key-id"
export KALSHI_PRIVATE_KEY_PATH="/path/to/kalshi_private.pem"

# Polymarket
export POLY_API_KEY="your-api-key-uuid"
export POLY_API_SECRET="your-url-safe-base64-secret"
export POLY_API_PASSPHRASE="your-passphrase"
export POLY_PRIVATE_KEY="0x..."   # hex Ethereum/Polygon private key
```

---

## API references

- Polymarket CLOB: [docs.polymarket.com](https://docs.polymarket.com/)
- Kalshi Trade API v2: [docs.kalshi.com](https://docs.kalshi.com/)
- Cloudflare WARP: [one.one.one.one](https://one.one.one.one/)

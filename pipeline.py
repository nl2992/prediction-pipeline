"""
Prediction Market Data Pipeline
================================
Fetches buy/sell (order book) data from Polymarket and Kalshi,
normalises the results into a common schema, and optionally saves
them to JSON.

Usage
-----
Run directly:
    python pipeline.py

Or import and call programmatically:
    from pipeline import run_pipeline
    result = run_pipeline()

No API keys or authentication are required.  All endpoints used are public.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi.client import KalshiClient
from polymarket.client import PolymarketClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("pipeline")

# ---------------------------------------------------------------------------
# Normalised data model
# ---------------------------------------------------------------------------


@dataclass
class PriceLevel:
    """A single price level in an order book."""

    price: float
    size: float


@dataclass
class OrderBook:
    """Normalised order book for one market / token."""

    bids: list[PriceLevel] = field(default_factory=list)  # buy side, best (highest) first
    asks: list[PriceLevel] = field(default_factory=list)  # sell side, best (lowest) first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return round((self.best_bid + self.best_ask) / 2, 6)
        return None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return round(self.best_ask - self.best_bid, 6)
        return None


@dataclass
class MarketSnapshot:
    """Normalised snapshot for one market from either exchange."""

    source: str           # "polymarket" | "kalshi"
    market_id: str        # condition ID (Poly) / ticker (Kalshi)
    event_id: str         # parent event identifier
    title: str
    status: str
    close_time: str | None
    fetched_at: str       # ISO 8601 UTC
    orderbook: OrderBook = field(default_factory=OrderBook)
    last_price: float | None = None
    volume_24h: float | None = None
    extra: dict = field(default_factory=dict)  # source-specific raw fields

    def to_dict(self) -> dict:
        d = asdict(self)
        d["orderbook"]["best_bid"] = self.orderbook.best_bid
        d["orderbook"]["best_ask"] = self.orderbook.best_ask
        d["orderbook"]["mid"] = self.orderbook.mid
        d["orderbook"]["spread"] = self.orderbook.spread
        return d


# ---------------------------------------------------------------------------
# Polymarket normalisation
# ---------------------------------------------------------------------------


def _parse_polymarket_book(raw_book: dict | None) -> OrderBook:
    """
    Convert a raw CLOB /book response into an OrderBook.

    The CLOB response has:
      bids – list of {price: str, size: str}  sorted highest price first
      asks – list of {price: str, size: str}  sorted lowest price first
    """
    if not raw_book:
        return OrderBook()

    def _levels(lst: list[dict]) -> list[PriceLevel]:
        levels = []
        for item in lst or []:
            try:
                levels.append(
                    PriceLevel(price=float(item["price"]), size=float(item["size"]))
                )
            except (KeyError, ValueError, TypeError):
                pass
        return levels

    return OrderBook(
        bids=sorted(_levels(raw_book.get("bids", [])), key=lambda x: x.price, reverse=True),
        asks=sorted(_levels(raw_book.get("asks", [])), key=lambda x: x.price),
    )


def fetch_polymarket(
    limit: int = 20,
    fetch_orderbooks: bool = True,
    keywords: list[str] | None = None,
    event_slug: str | None = None,
) -> list[MarketSnapshot]:
    """
    Fetch active Polymarket markets and their order books.

    Parameters
    ----------
    limit : int
        Number of markets to fetch from the Gamma API when no keywords or
        event_slug given.
    fetch_orderbooks : bool
        Whether to fetch full CLOB order book depth.
        If False, falls back to ``outcomePrices`` (last traded price).
    keywords : list of str, optional
        Search the full Gamma catalog for markets whose question contains any
        of these substrings (case-insensitive).  Gamma's ``tag_slug`` / ``_q``
        params are broken, so this paginates and filters client-side.
    event_slug : str, optional
        Fetch all markets for a specific Polymarket event identified by its URL
        slug (e.g. ``"ky-04-republican-primary-winner"``).  Takes priority over
        ``keywords`` and ``limit``.  Useful for targeting one specific event.
    """
    client = PolymarketClient()
    fetched_at = datetime.now(timezone.utc).isoformat()

    if event_slug:
        logger.info("Polymarket: fetching event slug=%r…", event_slug)
        markets = client.get_markets_for_event_slug(event_slug)
    elif keywords:
        logger.info("Polymarket: searching catalog for keywords %s…", keywords)
        markets = client.search_markets(keywords=keywords)
    else:
        logger.info("Polymarket: fetching %d markets from Gamma API…", limit)
        markets = client.get_markets(limit=limit, active=True, closed=False)
    logger.info("Polymarket: received %d markets", len(markets))

    if fetch_orderbooks:
        logger.info("Polymarket: enriching with CLOB order books…")
        markets = client.enrich_markets_with_orderbooks(markets)

    snapshots: list[MarketSnapshot] = []
    for mkt in markets:
        raw_book = mkt.get("orderbook")
        ob = _parse_polymarket_book(raw_book)

        # Fallback: use outcomePrices (last trade price) if no live book
        if ob.best_bid is None and ob.best_ask is None:
            outcome_prices = mkt.get("outcomePrices")
            if outcome_prices:
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except Exception:
                        outcome_prices = []
                if len(outcome_prices) >= 1:
                    try:
                        yes_price = float(outcome_prices[0])
                        ob = OrderBook(
                            bids=[PriceLevel(price=yes_price, size=0.0)],
                            asks=[PriceLevel(price=yes_price, size=0.0)],
                        )
                    except (ValueError, TypeError):
                        pass

        def _f(v: Any) -> float | None:
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        snap = MarketSnapshot(
            source="polymarket",
            market_id=mkt.get("conditionId") or mkt.get("id", ""),
            event_id=mkt.get("slug", ""),
            title=mkt.get("question", mkt.get("title", "")),
            status="open" if mkt.get("active") else "closed",
            close_time=mkt.get("endDate") or mkt.get("endDateIso"),
            fetched_at=fetched_at,
            orderbook=ob,
            last_price=_f(mkt.get("lastTradePrice")),
            volume_24h=_f(mkt.get("volume24hr")),
            extra={
                "clob_token_ids": mkt.get("clobTokenIds"),
                "outcomes": mkt.get("outcomes"),
                "enable_order_book": mkt.get("enableOrderBook"),
                "volume": mkt.get("volume"),
                "liquidity": mkt.get("liquidityClob"),
                "best_bid": mkt.get("bestBid"),
                "best_ask": mkt.get("bestAsk"),
                "spread": mkt.get("spread"),
                "neg_risk": mkt.get("negRisk"),
                "tick_size": raw_book.get("tick_size") if raw_book else None,
                "min_order_size": raw_book.get("min_order_size") if raw_book else None,
            },
        )
        snapshots.append(snap)

    logger.info("Polymarket: built %d snapshots", len(snapshots))
    return snapshots


# ---------------------------------------------------------------------------
# Kalshi normalisation
# ---------------------------------------------------------------------------


def _parse_kalshi_top_of_book(market: dict) -> OrderBook:
    """
    Build an OrderBook from the inline bid/ask fields in a Kalshi market dict.

    Kalshi returns four inline price fields per market:
      yes_bid_dollars  – best YES bid (someone paying to buy YES)
      yes_ask_dollars  – best YES ask (someone selling YES / buying NO)
      no_bid_dollars   – best NO bid  (someone paying to buy NO)
      no_ask_dollars   – best NO ask  (someone selling NO / buying YES)

    We model the YES side as the primary contract:
      bids → YES bids  (people willing to buy YES)
      asks → YES asks  (people willing to sell YES)

    Zero values (0.0000) mean no resting order on that side and are
    returned as None.
    """

    def _f(val: Any) -> float | None:
        try:
            v = float(val)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    yes_bid = _f(market.get("yes_bid_dollars"))
    yes_ask = _f(market.get("yes_ask_dollars"))
    yes_bid_size = _f(market.get("yes_bid_size_fp"))
    yes_ask_size = _f(market.get("yes_ask_size_fp"))

    bids = [PriceLevel(price=yes_bid, size=yes_bid_size or 0.0)] if yes_bid is not None else []
    asks = [PriceLevel(price=yes_ask, size=yes_ask_size or 0.0)] if yes_ask is not None else []

    return OrderBook(bids=bids, asks=asks)


def _parse_kalshi_full_book(raw: dict) -> OrderBook:
    """
    Convert a raw /markets/{ticker}/orderbook response into an OrderBook.

    The response contains ``yes_dollars`` and ``no_dollars``, each a list of
    [price_str, size_str] pairs representing BID levels (ascending price order).

    Reconstruction:
      bids (buy YES)  = yes_dollars sorted descending by price (best bid first)
      asks (sell YES) = derived from no_dollars: ask_yes_price = 1 - no_bid_price
                        sorted ascending by price (best ask first)

    Example:
      no_dollars = [["0.31", "32.00"], ["0.41", "64.00"]]
      → YES asks at [1-0.41=0.59, 1-0.31=0.69]  (ascending: 0.59 first)
    """
    ob_fp = raw.get("orderbook_fp", {})

    def _parse_levels(lst: list) -> list[PriceLevel]:
        levels = []
        for item in lst or []:
            try:
                price, size = float(item[0]), float(item[1])
                levels.append(PriceLevel(price=price, size=size))
            except (IndexError, ValueError, TypeError):
                pass
        return levels

    yes_bids_raw = _parse_levels(ob_fp.get("yes_dollars", []))
    no_bids_raw = _parse_levels(ob_fp.get("no_dollars", []))

    # Sort bids: highest price first (best bid at index 0)
    yes_bids = sorted(yes_bids_raw, key=lambda x: x.price, reverse=True)

    # Derive YES asks from NO bids: ask_yes = 1 - no_bid_price
    # Sort ascending: lowest ask first (best ask at index 0)
    yes_asks = sorted(
        [PriceLevel(price=round(1.0 - lvl.price, 6), size=lvl.size) for lvl in no_bids_raw],
        key=lambda x: x.price,
    )

    return OrderBook(bids=yes_bids, asks=yes_asks)


def fetch_kalshi(
    limit: int = 20,
    fetch_full_orderbooks: bool = True,
    series_ticker: str | None = None,
    event_ticker: str | None = None,
) -> list[MarketSnapshot]:
    """
    Fetch active Kalshi markets and their order book data.

    Parameters
    ----------
    limit : int
        Number of markets to fetch.
    fetch_full_orderbooks : bool
        If True (default), fetch full order book depth via
        GET /markets/{ticker}/orderbook (public, no auth needed).
        If False, use only the inline top-of-book from the markets list.
    series_ticker : str, optional
        Filter by series, e.g. ``"KXARTEMISII"`` or ``"KXBTC"``.
        Recommended: the default sort returns newest (often illiquid) markets
        first; filtering by series gives more meaningful results.
    event_ticker : str, optional
        Filter by a specific event ticker.

    Notes
    -----
    - The Kalshi API returns ``status: "active"`` for open markets.
    - Prices are in USD (0.59 = 59 cents = 59% implied probability).
    - Zero bid/ask values mean no resting order on that side.
    """
    client = KalshiClient()
    fetched_at = datetime.now(timezone.utc).isoformat()

    logger.info("Kalshi: fetching %d markets…", limit)
    resp = client.get_markets(
        limit=limit,
        series_ticker=series_ticker,
        event_ticker=event_ticker,
        status="open",
    )
    markets = resp.get("markets", [])
    logger.info("Kalshi: received %d markets", len(markets))

    snapshots: list[MarketSnapshot] = []
    for mkt in markets:
        ticker = mkt.get("ticker", "")

        if fetch_full_orderbooks:
            try:
                raw_book = client.get_orderbook(ticker)
                ob = _parse_kalshi_full_book(raw_book)
                # Fall back to inline top-of-book if full book is empty
                if ob.best_bid is None and ob.best_ask is None:
                    ob = _parse_kalshi_top_of_book(mkt)
            except Exception as exc:
                logger.warning(
                    "Kalshi: full orderbook fetch failed for %s: %s — "
                    "falling back to inline top-of-book",
                    ticker,
                    exc,
                )
                ob = _parse_kalshi_top_of_book(mkt)
        else:
            ob = _parse_kalshi_top_of_book(mkt)

        top = KalshiClient.parse_top_of_book(mkt)

        def _f(v: Any) -> float | None:
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        snap = MarketSnapshot(
            source="kalshi",
            market_id=ticker,
            event_id=mkt.get("event_ticker", ""),
            title=mkt.get("title", ""),
            status=mkt.get("status", ""),
            close_time=mkt.get("close_time"),
            fetched_at=fetched_at,
            orderbook=ob,
            last_price=top.get("last_price"),
            volume_24h=top.get("volume_24h"),
            extra={
                "no_bid": top.get("no_bid"),
                "no_ask": top.get("no_ask"),
                "market_type": mkt.get("market_type"),
                "open_interest": _f(mkt.get("open_interest_fp")),
                "liquidity": _f(mkt.get("liquidity_dollars")),
                "series_ticker": mkt.get("series_ticker"),
                "tick_size": mkt.get("tick_size"),
            },
        )
        snapshots.append(snap)

    logger.info("Kalshi: built %d snapshots", len(snapshots))
    return snapshots


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def run_pipeline(
    polymarket_limit: int = 20,
    kalshi_limit: int = 20,
    fetch_polymarket_books: bool = True,
    fetch_kalshi_full_books: bool = True,
    kalshi_series_ticker: str | None = None,
    kalshi_event_ticker: str | None = None,
    poly_keywords: list[str] | None = None,
    poly_event_slug: str | None = None,
    output_dir: str | Path | None = None,
    run_matching: bool = False,
    run_arb: bool = False,
    fee_poly: float = 0.02,
    fee_kalshi: float = 0.07,
    min_arb_profit_pct: float = 0.0,
    match_overrides: list[tuple[str, str]] | None = None,
) -> dict:
    """
    Run the full pipeline for both exchanges, and optionally match markets
    and detect arbitrage opportunities.

    Returns
    -------
    dict with keys:
      ``"polymarket"`` – list of MarketSnapshot
      ``"kalshi"``     – list of MarketSnapshot
      ``"pairs"``      – list of MatchedPair (if run_matching or run_arb)
      ``"arb"``        – list of ArbOpportunity (if run_arb)

    If ``output_dir`` is provided, snapshots are also saved as:
      <output_dir>/polymarket_<timestamp>.json
      <output_dir>/kalshi_<timestamp>.json
    """
    logger.info("=== Prediction Market Pipeline starting ===")

    poly_snapshots = fetch_polymarket(
        limit=polymarket_limit,
        fetch_orderbooks=fetch_polymarket_books,
        keywords=poly_keywords,
        event_slug=poly_event_slug,
    )
    kalshi_snapshots = fetch_kalshi(
        limit=kalshi_limit,
        fetch_full_orderbooks=fetch_kalshi_full_books,
        series_ticker=kalshi_series_ticker,
        event_ticker=kalshi_event_ticker,
    )

    result: dict = {
        "polymarket": poly_snapshots,
        "kalshi": kalshi_snapshots,
    }

    if run_matching or run_arb:
        from matcher import match_markets
        pairs = match_markets(
            poly_snapshots,
            kalshi_snapshots,
            overrides=match_overrides,
        )
        result["pairs"] = pairs
        logger.info("Matcher: found %d paired market(s)", len(pairs))

        if run_arb:
            from arb import find_arb
            opportunities = find_arb(
                pairs,
                fee_poly=fee_poly,
                fee_kalshi=fee_kalshi,
                min_net_profit_pct=min_arb_profit_pct,
            )
            result["arb"] = opportunities
            profitable = [o for o in opportunities if o.is_profitable]
            logger.info(
                "Arb: %d candidate(s) checked, %d profitable after fees",
                len(opportunities), len(profitable),
            )

    if output_dir:
        _save_results(result, Path(output_dir))

    logger.info(
        "=== Pipeline complete — Polymarket: %d, Kalshi: %d ===",
        len(poly_snapshots),
        len(kalshi_snapshots),
    )
    return result


def _save_results(
    result: dict[str, list[MarketSnapshot]], output_dir: Path
) -> None:
    """Persist pipeline results to JSON files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for source, snapshots in result.items():
        path = output_dir / f"{source}_{ts}.json"
        data = [s.to_dict() for s in snapshots]
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        logger.info("Saved %d %s snapshots → %s", len(snapshots), source, path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch buy/sell order book data from Polymarket and Kalshi."
    )
    parser.add_argument(
        "--poly-limit", type=int, default=20,
        help="Number of Polymarket markets to fetch (default: 20)",
    )
    parser.add_argument(
        "--kalshi-limit", type=int, default=20,
        help="Number of Kalshi markets to fetch (default: 20)",
    )
    parser.add_argument(
        "--no-poly-books", action="store_true",
        help="Skip fetching full Polymarket CLOB order books",
    )
    parser.add_argument(
        "--no-kalshi-books", action="store_true",
        help="Skip fetching full Kalshi order book depth (use inline top-of-book only)",
    )
    parser.add_argument(
        "--kalshi-series", default=None,
        help="Filter Kalshi markets by series ticker, e.g. KXARTEMISII",
    )
    parser.add_argument(
        "--kalshi-event", default=None,
        help="Filter Kalshi markets by event ticker",
    )
    parser.add_argument(
        "--output-dir", default="output",
        help="Directory to save JSON output (default: ./output)",
    )
    parser.add_argument(
        "--arb", action="store_true",
        help="Run cross-exchange market matching and arbitrage detection",
    )
    parser.add_argument(
        "--fee-poly", type=float, default=0.02,
        help="Polymarket fee rate on $1 payout (default: 0.02)",
    )
    parser.add_argument(
        "--fee-kalshi", type=float, default=0.07,
        help="Kalshi fee rate on $1 payout (default: 0.07)",
    )
    parser.add_argument(
        "--min-profit-pct", type=float, default=0.0,
        help="Minimum net profit %% of gross cost to show (default: 0.0)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    result = run_pipeline(
        polymarket_limit=args.poly_limit,
        kalshi_limit=args.kalshi_limit,
        fetch_polymarket_books=not args.no_poly_books,
        fetch_kalshi_full_books=not args.no_kalshi_books,
        kalshi_series_ticker=args.kalshi_series,
        kalshi_event_ticker=args.kalshi_event,
        output_dir=args.output_dir,
        run_arb=args.arb,
        fee_poly=args.fee_poly,
        fee_kalshi=args.fee_kalshi,
        min_arb_profit_pct=args.min_profit_pct,
    )

    # Print a brief summary to stdout
    print("\n--- POLYMARKET (top 5) ---")
    for snap in result["polymarket"][:5]:
        ob = snap.orderbook
        print(
            f"  {snap.title[:60]:<60}  "
            f"bid={ob.best_bid}  ask={ob.best_ask}  mid={ob.mid}"
        )

    print("\n--- KALSHI (top 5) ---")
    for snap in result["kalshi"][:5]:
        ob = snap.orderbook
        print(
            f"  {snap.title[:60]:<60}  "
            f"bid={ob.best_bid}  ask={ob.best_ask}  mid={ob.mid}"
        )

    if args.arb:
        pairs = result.get("pairs", [])
        opps = result.get("arb", [])
        print(f"\n--- MATCHED PAIRS ({len(pairs)}) ---")
        for pair in pairs[:10]:
            tag = "[override]" if pair.via_override else f"[sim={pair.title_similarity:.2f}]"
            print(f"  {tag}  {pair.poly.title[:45]:<45}  ↔  {pair.kalshi.title[:45]}")

        profitable = [o for o in opps if o.is_profitable]
        print(f"\n--- ARB OPPORTUNITIES ({len(profitable)} profitable / {len(opps)} scanned) ---")
        if profitable:
            for opp in profitable[:10]:
                p = opp.pair
                print(
                    f"  {opp.direction:<25}  "
                    f"gross_cost={opp.gross_cost:.4f}  "
                    f"net_profit={opp.net_profit:.4f}  "
                    f"({opp.net_profit_pct:.2f}%)  "
                    f"max_size={opp.max_size}"
                )
                print(f"    Poly:  {p.poly.title[:70]}")
                print(f"    Kalshi:{p.kalshi.title[:70]}")
        else:
            print("  No profitable arb found in this snapshot.")

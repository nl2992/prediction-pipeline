"""
Kalshi Trade API v2 client.

Base URL: https://api.elections.kalshi.com/trade-api/v2

All endpoints used here are **public** — no authentication is required
for reading market data or order books.

Authentication (RSA-PSS signed headers) is only needed for order
placement and account management, which are out of scope for this
pipeline.  The auth scaffolding is retained in this module for
completeness but is never invoked by the pipeline itself.

Key API facts (verified against live API, March 2026):
  - Markets are returned with status field value "active" (not "open").
  - The status query filter accepts "open" and correctly returns active markets.
  - GET /markets/{ticker}/orderbook is publicly accessible (no auth needed).
  - The orderbook response contains BID levels only for both YES and NO sides.
    An ask for YES at price X is implied by a NO bid at price (1 - X).
  - Prices are in USD (dollar-denominated), e.g. 0.59 means 59 cents.
  - The default sort order from GET /markets is by creation time descending,
    meaning the most recently created (often illiquid) markets appear first.
    Use series_ticker or event_ticker filters to target specific markets.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_RETRIES = 8


def _load_private_key(pem_path: str):
    """Load an RSA private key from a PEM file (requires cryptography package)."""
    try:
        from cryptography.hazmat.primitives import serialization

        with open(pem_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except ImportError as exc:
        raise RuntimeError(
            "Install the 'cryptography' package to use authenticated Kalshi "
            "endpoints: pip install cryptography"
        ) from exc


def _sign_message(private_key, message: str) -> str:
    """Sign a message with RSA-PSS SHA-256 and return a base64-encoded signature."""
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    sig = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


class KalshiClient:
    """
    Kalshi Trade API v2 client.

    All market-data methods are public and require no credentials.

    Parameters
    ----------
    api_key : str, optional
        Your Kalshi API key ID.  Falls back to env var ``KALSHI_API_KEY``.
        Only needed for order management endpoints (not used here).
    private_key_path : str, optional
        Path to your RSA private key PEM file.
        Falls back to env var ``KALSHI_PRIVATE_KEY_PATH``.
        Only needed for order management endpoints (not used here).
    timeout : int
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str | None = None,
        private_key_path: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or os.environ.get("KALSHI_API_KEY")
        pem_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        self._private_key = _load_private_key(pem_path) if pem_path else None
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _is_authenticated(self) -> bool:
        return bool(self.api_key and self._private_key)

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Build the three authentication headers for a signed request."""
        if not self._is_authenticated:
            raise RuntimeError(
                "Kalshi authentication not configured.  "
                "Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH."
            )
        ts_ms = str(int(time.time() * 1000))
        message = f"{ts_ms}{method}{path}"
        signature = _sign_message(self._private_key, message)
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _get(
        self,
        path: str,
        params: dict | None = None,
        authenticated: bool = False,
    ) -> Any:
        url = f"{BASE_URL}{path}"
        headers = (
            self._auth_headers("GET", f"/trade-api/v2{path}")
            if authenticated
            else {}
        )
        # Retry on 429 with exponential backoff
        backoff = 5
        resp = None
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException:
                if attempt == DEFAULT_MAX_RETRIES - 1:
                    raise
                import time as _time
                _time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 429:
                import time as _time
                _time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            resp.raise_for_status()
            return resp.json()
        # Final attempt — let it raise
        if resp is None:
            raise RuntimeError("Kalshi GET failed before a response was received")
        resp.raise_for_status()
        return resp.json()

    def _post(
        self,
        path: str,
        json_body: Any = None,
        authenticated: bool = False,
    ) -> Any:
        url = f"{BASE_URL}{path}"
        headers = (
            self._auth_headers("POST", f"/trade-api/v2{path}")
            if authenticated
            else {}
        )
        resp = self.session.post(
            url, json=json_body, headers=headers, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(
        self,
        path: str,
        params: dict | None = None,
        authenticated: bool = False,
    ) -> Any:
        url = f"{BASE_URL}{path}"
        headers = (
            self._auth_headers("DELETE", f"/trade-api/v2{path}")
            if authenticated
            else {}
        )
        resp = self.session.delete(
            url, params=params, headers=headers, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Market & event discovery (all public)
    # ------------------------------------------------------------------

    def get_markets(
        self,
        limit: int = 100,
        cursor: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        status: str | None = None,
    ) -> dict:
        """
        List markets (public endpoint).

        Returns a dict with keys:
          markets – list of market objects.  Each includes inline top-of-book:
                    yes_bid_dollars, yes_ask_dollars,
                    no_bid_dollars,  no_ask_dollars
          cursor  – opaque pagination cursor for the next page

        Notes
        -----
        - The ``status`` filter accepts ``"open"`` (active/trading markets).
          The response field is named ``status`` and contains ``"active"``.
        - Default sort is by creation time descending, so the first page
          often contains very recently created (illiquid) markets.
          Use ``series_ticker`` or ``event_ticker`` to target specific markets.
        - ``limit`` max is 1000 per request.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return self._get("/markets", params=params)

    def get_all_markets(
        self,
        max_pages: int | None = None,
        page_size: int = 200,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str = "open",
    ) -> list[dict]:
        """
        Paginate through markets until the API cursor is exhausted.

        Pass ``max_pages`` to deliberately cap traversal for smoke tests.

        Each market already contains top-of-book bid/ask prices inline.
        """
        all_markets: list[dict] = []
        cursor: str | None = None
        pages = 0
        while max_pages is None or pages < max_pages:
            resp = self.get_markets(
                limit=page_size,
                cursor=cursor,
                series_ticker=series_ticker,
                event_ticker=event_ticker,
                status=status,
            )
            batch = resp.get("markets", [])
            all_markets.extend(batch)
            cursor = resp.get("cursor")
            pages += 1
            if not cursor or not batch:
                break
        return all_markets

    def get_events(
        self,
        limit: int = 100,
        cursor: str | None = None,
        series_ticker: str | None = None,
        status: str | None = None,
    ) -> dict:
        """
        List events (public endpoint).

        Returns a dict with keys:
          events    – list of event objects
          milestones – list of milestone objects
          cursor    – pagination cursor
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return self._get("/events", params=params)

    def get_all_events(
        self,
        max_pages: int | None = None,
        page_size: int = 200,
        series_ticker: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Paginate through events until exhausted, unless ``max_pages`` caps it."""
        all_events: list[dict] = []
        cursor: str | None = None
        pages = 0
        while max_pages is None or pages < max_pages:
            resp = self.get_events(
                limit=page_size,
                cursor=cursor,
                series_ticker=series_ticker,
                status=status,
            )
            batch = resp.get("events", [])
            all_events.extend(batch)
            cursor = resp.get("cursor")
            pages += 1
            if not cursor or not batch:
                break
        return all_events

    def get_series_list(self, limit: int = 100) -> list[dict]:
        """
        Return a list of series (public endpoint).

        A series groups related events, e.g. ``KXBTC`` for all Bitcoin price
        markets.  Use the ``ticker`` field as ``series_ticker`` in
        ``get_markets()`` to filter by series.
        """
        data = self._get("/series", params={"limit": limit})
        return data.get("series", data) if isinstance(data, dict) else data

    # ------------------------------------------------------------------
    # Full order book (public — no auth required)
    # ------------------------------------------------------------------

    def get_orderbook(self, ticker: str, depth: int = 0) -> dict:
        """
        Fetch the full order book for a market ticker (public endpoint).

        Returns a dict with key ``orderbook_fp`` containing:
          yes_dollars – list of [price_str, size_str] pairs (YES bids, ascending price)
          no_dollars  – list of [price_str, size_str] pairs (NO bids, ascending price)

        Interpretation
        --------------
        Kalshi only returns BID levels for each side.  In a binary market:
          - A YES bid at price P means someone will pay $P to buy YES.
          - A NO bid at price Q means someone will pay $Q to buy NO.
          - Since YES + NO = $1, a NO bid at Q implies a YES ask at (1 - Q).
          - Similarly, a YES bid at P implies a NO ask at (1 - P).

        So to reconstruct the full YES order book:
          bids (buy YES)  = yes_dollars  (sorted ascending, best bid = highest price)
          asks (sell YES) = derived from no_dollars: ask_price = 1 - no_bid_price
                            (sorted ascending, best ask = lowest price)

        Parameters
        ----------
        ticker : str
            Market ticker, e.g. ``"KXARTEMISII-APRIL-26MAY01"``.
        depth : int
            Number of price levels to return per side.
            0 (default) = all levels.  Max 100.
        """
        path = f"/markets/{ticker}/orderbook"
        params: dict[str, Any] = {}
        if depth > 0:
            params["depth"] = depth
        return self._get(path, params=params or None, authenticated=False)

    # ------------------------------------------------------------------
    # Convenience: parse bid/ask from market object (no extra API call)
    # ------------------------------------------------------------------

    @staticmethod
    def parse_top_of_book(market: dict) -> dict:
        """
        Extract the top-of-book bid/ask from a market dict returned by
        ``get_markets()``.  No additional API call required.

        Returns a normalised dict:
          ticker          – market ticker
          event_ticker    – parent event ticker
          title           – market title
          status          – market status (will be "active" for open markets)
          yes_bid         – best YES bid price (dollars, float or None)
          yes_ask         – best YES ask price (dollars, float or None)
          no_bid          – best NO bid price (dollars, float or None)
          no_ask          – best NO ask price (dollars, float or None)
          last_price      – last traded price (dollars, float or None)
          volume_24h      – 24-hour volume (contracts, float or None)
          close_time      – market close time (ISO 8601 string)

        Notes
        -----
        - Prices are in USD (e.g. 0.59 = 59 cents = 59% implied probability).
        - A zero bid/ask (0.0000) means no resting order on that side.
        - yes_bid + no_ask ≈ 1.0 and no_bid + yes_ask ≈ 1.0 for liquid markets.
        """

        def _f(val: Any) -> float | None:
            try:
                v = float(val)
                return v if v > 0 else None
            except (TypeError, ValueError):
                return None

        return {
            "ticker": market.get("ticker"),
            "event_ticker": market.get("event_ticker"),
            "title": market.get("title"),
            "status": market.get("status"),
            "yes_bid": _f(market.get("yes_bid_dollars")),
            "yes_ask": _f(market.get("yes_ask_dollars")),
            "no_bid": _f(market.get("no_bid_dollars")),
            "no_ask": _f(market.get("no_ask_dollars")),
            "last_price": _f(market.get("last_price_dollars")),
            "volume_24h": _f(market.get("volume_24h_fp")),
            "close_time": market.get("close_time"),
        }

    # ------------------------------------------------------------------
    # Portfolio & order management (all authenticated)
    # ------------------------------------------------------------------

    def get_balance(self) -> dict:
        """
        Return account balance (authenticated).

        Response keys:
          balance         – int64, cents
          balance_dollars – string, dollars (FixedPointDollars)
          portfolio_value – int64, cents
        """
        return self._get("/portfolio/balance", authenticated=True)

    def get_positions(
        self,
        ticker: str | None = None,
        event_ticker: str | None = None,
        limit: int = 100,
    ) -> dict:
        """
        Return open positions (authenticated).

        Response keys:
          market_positions – list of MarketPosition objects
          event_positions  – list of EventPosition objects
          cursor           – pagination cursor
        """
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._get("/portfolio/positions", params=params, authenticated=True)

    def place_order(
        self,
        ticker: str,
        side: str,
        price: float,
        count: float,
        time_in_force: str = "good_till_canceled",
        client_order_id: str | None = None,
        post_only: bool = False,
    ) -> dict:
        """
        Place a limit order (authenticated).

        Parameters
        ----------
        ticker : str
            Market ticker, e.g. ``"KXFED-26JUN-T3.50"``.
        side : str
            ``"bid"`` – buy YES contracts at this price.
            ``"ask"`` – sell YES contracts at this price (creates NO exposure).
        price : float
            Limit price in dollars (0.0–1.0), e.g. 0.97.
        count : float
            Number of contracts, e.g. 10.0.
        time_in_force : str
            ``"good_till_canceled"`` (default), ``"fill_or_kill"``, or
            ``"immediate_or_cancel"``.
        client_order_id : str, optional
            Your reference ID; auto-generated UUID if omitted.
        post_only : bool
            If True, order is rejected if it would immediately fill (maker-only).

        Returns
        -------
        CreateOrderResponse with keys:
          order_id, client_order_id, status, remaining_count, …
        """
        import uuid
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": side,
            "count": f"{count:.2f}",
            "price": f"{price:.6f}".rstrip("0").rstrip("."),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only,
        }
        return self._post("/portfolio/orders", json_body=body, authenticated=True)

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel an open order (authenticated).

        Returns CancelOrderV2Response:
          order_id, reduced_by (contracts cancelled), ts_ms
        """
        return self._delete(f"/portfolio/orders/{order_id}", authenticated=True)

    def get_order(self, order_id: str) -> dict:
        """Return the current state of an order (authenticated)."""
        return self._get(f"/portfolio/orders/{order_id}", authenticated=True)

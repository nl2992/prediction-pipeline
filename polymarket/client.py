"""
Polymarket CLOB & Gamma API client.

Public endpoints — no authentication required.

Base URLs:
  CLOB:  https://clob.polymarket.com
  Gamma: https://gamma-api.polymarket.com
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

DEFAULT_TIMEOUT = 15  # seconds
DEFAULT_MAX_RETRIES = 3  # transient-failure retries for GET (mirrors Kalshi #69)


class PolymarketClient:
    """
    Thin wrapper around the Polymarket REST APIs.

    Public market-data endpoints require no credentials.

    Order placement requires L2 API key credentials (set via env vars or
    constructor args):
      POLY_API_KEY        – API key UUID (from Polymarket CLOB dashboard)
      POLY_API_SECRET     – URL-safe base64 secret key
      POLY_API_PASSPHRASE – passphrase chosen when key was created

    Order placement also requires the ``eth-account`` package for EIP-712
    signing, and a Polygon private key:
      POLY_PRIVATE_KEY    – hex-encoded Ethereum/Polygon private key (0x...)
    """

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        private_key: str | None = None,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("POLY_API_KEY")
        self.api_secret = api_secret or os.environ.get("POLY_API_SECRET")
        self.api_passphrase = api_passphrase or os.environ.get("POLY_API_PASSPHRASE")
        self.private_key = private_key or os.environ.get("POLY_PRIVATE_KEY")

    @property
    def _is_authenticated(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, base: str, path: str, params: dict | None = None) -> Any:
        url = f"{base}{path}"
        # Retry transient failures (network errors, 429) with exponential backoff,
        # mirroring the Kalshi client (#69). Only GET is retried; _post stays
        # single-attempt so an order is never resubmitted (#120).
        backoff = 5
        resp = None
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException:
                if attempt == DEFAULT_MAX_RETRIES - 1:
                    raise
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code == 429 and attempt < DEFAULT_MAX_RETRIES - 1:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            resp.raise_for_status()
            return resp.json()
        # Exhausted retries (last response was 429) — let it raise.
        if resp is None:
            raise RuntimeError("Polymarket GET failed before a response was received")
        resp.raise_for_status()
        return resp.json()

    def _post(self, base: str, path: str, json_body: Any) -> Any:
        url = f"{base}{path}"
        resp = self.session.post(url, json=json_body, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Market discovery (Gamma API)
    # ------------------------------------------------------------------

    def get_events(
        self,
        limit: int = 20,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
    ) -> list[dict]:
        """Return a list of events from the Gamma API."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        data = self._get(GAMMA_BASE, "/events", params=params)
        return data if isinstance(data, list) else data.get("events", data)

    def get_markets(
        self,
        limit: int = 20,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
    ) -> list[dict]:
        """Return a list of markets from the Gamma API."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        data = self._get(GAMMA_BASE, "/markets", params=params)
        return data if isinstance(data, list) else data.get("markets", data)

    def search_markets(
        self,
        keywords: list[str],
        active: bool = True,
        closed: bool = False,
        max_offset: int | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """
        Paginate the full Gamma catalog and return markets whose ``question``
        field contains **any** of the given keywords (case-insensitive).

        Note
        ----
        The Gamma API's ``tag_slug`` and ``_q`` query parameters are broken
        (they return the same front-page results regardless of value).
        Client-side filtering over paginated pages is the only reliable method.

        Parameters
        ----------
        keywords   : list of substring patterns to match in the question field
        active     : only return active markets (default True)
        closed     : include closed markets (default False)
        max_offset : optional stop offset; None scans until the catalog ends
        page_size  : records per API request (max 100 on Gamma, default 100)
        """
        kw_lower = [k.lower() for k in keywords]
        results: list[dict] = []
        seen: set[str] = set()        # conditionIds already added to results (match dedup)
        scanned: set[str] = set()     # all conditionIds seen, for the stuck-page guard

        offset = 0
        while max_offset is None or offset <= max_offset:
            try:
                batch = self.get_markets(
                    limit=page_size,
                    offset=offset,
                    active=active,
                    closed=closed,
                )
            except Exception as exc:
                logger.warning("search_markets: fetch failed at offset=%d: %s", offset, exc)
                break

            if not batch:
                break  # end of catalog

            new_this_page = 0
            for mkt in batch:
                question = (mkt.get("question") or mkt.get("title") or "").lower()
                cid = mkt.get("conditionId") or mkt.get("id") or question
                if cid not in scanned:
                    scanned.add(cid)
                    new_this_page += 1
                if cid in seen:
                    continue
                if any(kw in question for kw in kw_lower):
                    results.append(mkt)
                    seen.add(cid)
            # If a full page contributes no new markets, the API isn't advancing
            # (e.g. ignoring offset) — stop rather than loop forever (#68, like #67).
            if new_this_page == 0:
                break
            offset += page_size

        logger.info(
            "search_markets: scanned through offset %d, found %d matching markets for keywords %s",
            offset, len(results), keywords,
        )
        return results

    def search_events(
        self,
        keywords: list[str],
        active: bool = True,
        closed: bool = False,
        max_offset: int | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """
        Paginate the Gamma /events catalog and return events whose ``title``
        field contains any of the given keywords (case-insensitive).

        Each returned event dict includes an embedded ``markets`` list — the
        individual outcome contracts for that event.  This makes it possible to
        do two-level (event → outcome) matching without extra API calls.

        Parameters
        ----------
        keywords   : list of substring patterns to match in the event title
        active     : only return active events (default True)
        closed     : include closed events (default False)
        max_offset : optional stop offset; None scans until the catalog ends
        page_size  : records per API request (default 100)
        """
        kw_lower = [k.lower() for k in keywords]
        results: list[dict] = []
        seen: set[str] = set()

        # Windowed parallel pagination: the Gamma catalog is offset-addressed,
        # so W consecutive pages can be fetched concurrently. Pages are
        # PROCESSED in strict offset order after each window completes, so
        # dedup and result ordering are identical to the sequential scan; the
        # window stops at the first empty/short page. Cost of the speedup: up
        # to window-1 speculative page requests past the catalog end. Fetching
        # goes through self.get_events so instance-level mocks/tests apply;
        # urllib3's pool handles concurrent GETs on the shared session.
        from concurrent.futures import ThreadPoolExecutor

        window = 8

        def fetch_page(off: int) -> tuple[int, list[dict] | None]:
            try:
                return off, self.get_events(
                    limit=page_size, offset=off, active=active, closed=closed,
                )
            except Exception as exc:
                logger.warning("search_events: fetch failed at offset=%d: %s", off, exc)
                return off, None

        offset = 0
        done = False
        with ThreadPoolExecutor(max_workers=window) as pool:
            while not done and (max_offset is None or offset <= max_offset):
                offsets = [offset + i * page_size for i in range(window)]
                if max_offset is not None:
                    offsets = [o for o in offsets if o <= max_offset]
                pages = dict(pool.map(fetch_page, offsets))
                for off in offsets:  # strict offset order, as sequential
                    batch = pages.get(off)
                    if not batch:  # error or empty page -> end of catalog
                        done = True
                        break
                    for ev in batch:
                        title = (ev.get("title") or "").lower()
                        slug = ev.get("slug") or ev.get("id", "")
                        if slug in seen:
                            continue
                        if any(kw in title for kw in kw_lower):
                            results.append(ev)
                            seen.add(slug)
                    if len(batch) < page_size:
                        done = True
                        break
                offset += window * page_size

        logger.info(
            "search_events: found %d events for keywords %s",
            len(results), keywords,
        )
        return results

    def get_event_by_slug(self, slug: str) -> dict | None:
        """
        Fetch a single Polymarket event and all its markets by event slug.

        The slug is the URL path component from a Polymarket event URL:
          https://polymarket.com/event/<slug>/...

        Returns the event dict (with a ``markets`` list embedded), or None
        if no event was found for that slug.

        Example
        -------
        ::

            ev = client.get_event_by_slug("ky-04-republican-primary-winner")
            for m in ev["markets"]:
                print(m["question"], m["outcomePrices"])
        """
        data = self._get(GAMMA_BASE, "/events", params={"slug": slug})
        events = data if isinstance(data, list) else data.get("events", [data])
        return events[0] if events else None

    def get_markets_for_event_slug(self, slug: str) -> list[dict]:
        """
        Return the flat list of markets for a Polymarket event identified by
        its URL slug (e.g. ``"ky-04-republican-primary-winner"``).

        Each market dict includes ``question``, ``conditionId``,
        ``clobTokenIds``, ``outcomePrices``, ``endDate``, etc.
        Returns an empty list if the event is not found.
        """
        import json as _json

        ev = self.get_event_by_slug(slug)
        if not ev:
            logger.warning("get_markets_for_event_slug: no event found for slug=%r", slug)
            return []

        markets = ev.get("markets", [])
        # Normalise outcomePrices and clobTokenIds from JSON strings if needed
        for m in markets:
            for key in ("outcomePrices", "clobTokenIds"):
                val = m.get(key)
                if isinstance(val, str):
                    try:
                        m[key] = _json.loads(val)
                    except Exception:
                        pass
        logger.info(
            "get_markets_for_event_slug: slug=%r → event=%r, %d markets",
            slug, ev.get("title"), len(markets),
        )
        return markets

    # ------------------------------------------------------------------
    # Order book (CLOB API)
    # ------------------------------------------------------------------

    def get_orderbook(self, token_id: str) -> dict:
        """
        Fetch the full order book for a single token.

        Returns a dict with keys:
          market    – condition ID
          asset_id  – token ID
          bids      – list of {price, size}  (buy side, highest first)
          asks      – list of {price, size}  (sell side, lowest first)
          tick_size
          min_order_size
          hash
        """
        return self._get(CLOB_BASE, "/book", params={"token_id": token_id})

    def get_orderbooks(self, token_ids: list[str]) -> list[dict]:
        """
        Fetch order books for multiple tokens in a single request (up to 500).

        Each element of token_ids should be a plain token ID string.
        """
        body = [{"token_id": tid} for tid in token_ids]
        return self._post(CLOB_BASE, "/books", json_body=body)

    def verify_clob_liquidity(
        self,
        token_id: str,
        min_bid: float = 0.01,
        max_ask: float = 0.99,
        min_depth_usd: float = 0.0,
    ) -> dict:
        """
        Fetch live CLOB depth for a token and return a liquidity verdict.

        A market is considered illiquid (ghost market) when:
          - best_bid < min_bid  (no real buyer — dust bids at 0.001)
          - best_ask > max_ask  (no real seller — dust asks at 0.999)

        Parameters
        ----------
        token_id       : CLOB token ID (YES or NO side)
        min_bid        : minimum acceptable best-bid price (default 0.01)
        max_ask        : maximum acceptable best-ask price (default 0.99)
        min_depth_usd  : minimum dollar size at best bid or ask (default 0 = no check)

        Returns
        -------
        dict with keys:
          token_id       – echoed
          best_bid       – float or None
          best_ask       – float or None
          bid_size       – dollar size at best bid (or None)
          ask_size       – dollar size at best ask (or None)
          spread         – best_ask - best_bid (or None)
          is_liquid      – True if bid >= min_bid and ask <= max_ask
          reason         – human-readable liquidity verdict
        """
        try:
            book = self.get_orderbook(token_id)
        except Exception as exc:
            return {
                "token_id": token_id,
                "best_bid": None, "best_ask": None,
                "bid_size": None, "ask_size": None,
                "spread": None, "is_liquid": False,
                "reason": f"CLOB fetch failed: {exc}",
            }

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        def _levels(lst):
            out = []
            for item in lst or []:
                try:
                    out.append((float(item["price"]), float(item["size"])))
                except (KeyError, TypeError, ValueError):
                    continue
            return out

        # Pick the true best by PRICE (max bid / min ask) rather than trusting the
        # raw list order — the CLOB response isn't guaranteed best-first (this is
        # why _parse_polymarket_book sorts it), so taking [0] could read a non-best
        # level into the executor's live-edge safety check (#61).
        bid_levels = _levels(bids)
        ask_levels = _levels(asks)
        best_bid_lvl = max(bid_levels, key=lambda x: x[0]) if bid_levels else None
        best_ask_lvl = min(ask_levels, key=lambda x: x[0]) if ask_levels else None
        best_bid = best_bid_lvl[0] if best_bid_lvl else None
        bid_size = best_bid_lvl[1] if best_bid_lvl else None
        best_ask = best_ask_lvl[0] if best_ask_lvl else None
        ask_size = best_ask_lvl[1] if best_ask_lvl else None
        spread = round(best_ask - best_bid, 6) if best_bid and best_ask else None

        reasons = []
        if best_bid is None or best_bid < min_bid:
            reasons.append(f"bid {best_bid} < min {min_bid}")
        if best_ask is None or best_ask > max_ask:
            reasons.append(f"ask {best_ask} > max {max_ask}")
        if min_depth_usd > 0:
            if bid_size is not None and bid_size < min_depth_usd:
                reasons.append(f"bid_size ${bid_size:.0f} < ${min_depth_usd:.0f}")
            if ask_size is not None and ask_size < min_depth_usd:
                reasons.append(f"ask_size ${ask_size:.0f} < ${min_depth_usd:.0f}")

        is_liquid = len(reasons) == 0
        reason = "liquid" if is_liquid else "; ".join(reasons)

        return {
            "token_id": token_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "spread": spread,
            "is_liquid": is_liquid,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Prices (CLOB API)
    # ------------------------------------------------------------------

    def get_price(self, token_id: str, side: str = "BUY") -> dict:
        """
        Get the best available price for a single token.

        side: "BUY" (best ask) or "SELL" (best bid)
        """
        return self._get(
            CLOB_BASE, "/price", params={"token_id": token_id, "side": side}
        )

    def get_midpoint(self, token_id: str) -> dict:
        """Return the midpoint price for a token."""
        return self._get(CLOB_BASE, "/midpoint", params={"token_id": token_id})

    def get_spread(self, token_id: str) -> dict:
        """Return the bid-ask spread for a token."""
        return self._get(CLOB_BASE, "/spread", params={"token_id": token_id})

    # ------------------------------------------------------------------
    # Convenience: enrich market list with live order book data
    # ------------------------------------------------------------------

    def enrich_markets_with_orderbooks(
        self, markets: list[dict], batch_size: int = 50
    ) -> list[dict]:
        """
        Given a list of market dicts (from Gamma API), fetch live order books
        for every token and attach them under the key ``orderbook``.

        Markets without a ``clobTokenIds`` field are skipped.
        """
        enriched: list[dict] = []
        token_to_market: dict[str, dict] = {}

        for mkt in markets:
            raw_ids = mkt.get("clobTokenIds")
            if not raw_ids:
                enriched.append({**mkt, "orderbook": None})
                continue
            # clobTokenIds is a JSON-encoded list stored as a string
            if isinstance(raw_ids, str):
                import json
                try:
                    token_ids = json.loads(raw_ids)
                except Exception:
                    token_ids = [raw_ids]
            else:
                token_ids = raw_ids

            # Resolve YES token by matching against outcome labels.
            # clobTokenIds[i] corresponds to outcomes[i]; fall back to index 0.
            outcomes_raw = mkt.get("outcomes")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes = json.loads(outcomes_raw)
                except Exception:
                    outcomes = []
            else:
                outcomes = outcomes_raw or []

            primary_token = None
            for i, outcome in enumerate(outcomes):
                if isinstance(outcome, str) and outcome.strip().lower() in ("yes", "y") and i < len(token_ids):
                    primary_token = token_ids[i]
                    break
            if primary_token is None:
                primary_token = token_ids[0] if token_ids else None
            if primary_token:
                token_to_market[primary_token] = mkt
            enriched.append({**mkt, "_primary_token": primary_token, "orderbook": None})

        # Batch fetch order books
        token_ids_list = [
            m["_primary_token"]
            for m in enriched
            if m.get("_primary_token")
        ]

        books_by_token: dict[str, dict] = {}
        for i in range(0, len(token_ids_list), batch_size):
            chunk = token_ids_list[i : i + batch_size]
            try:
                books = self.get_orderbooks(chunk)
                for book in books:
                    asset_id = book.get("asset_id")
                    if asset_id:
                        books_by_token[asset_id] = book
            except Exception as exc:
                logger.warning("Batch orderbook fetch failed for chunk: %s", exc)

        # Attach books back to markets
        for mkt in enriched:
            token = mkt.pop("_primary_token", None)
            if token and token in books_by_token:
                mkt["orderbook"] = books_by_token[token]

        return enriched

    # ------------------------------------------------------------------
    # Authenticated helpers (L2 API key + HMAC-SHA256)
    # ------------------------------------------------------------------

    def _clob_auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """
        Build CLOB L2 authentication headers for a signed request.

        Header construction (from py-clob-client source):
          message = str(timestamp) + method.upper() + path + body
          key     = base64.urlsafe_b64decode(api_secret)
          sig     = HMAC-SHA256(key, message.encode()).digest()
          POLY_SIGNATURE = base64.urlsafe_b64encode(sig).decode()
        """

        if not self._is_authenticated:
            raise RuntimeError(
                "Polymarket L2 auth not configured.  "
                "Set POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE."
            )
        ts = str(int(time.time()))
        # Polymarket normalises single quotes → double quotes in body for signing
        body_norm = body.replace("'", '"')
        message = ts + method.upper() + path + body_norm
        key = base64.urlsafe_b64decode(self.api_secret + "==")  # add padding
        sig = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
        return {
            "POLY_ADDRESS": self.api_key,
            "POLY_SIGNATURE": base64.urlsafe_b64encode(sig).decode(),
            "POLY_TIMESTAMP": ts,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
            "Content-Type": "application/json",
        }

    def _clob_get_auth(self, path: str, params: dict | None = None) -> Any:
        url = f"{CLOB_BASE}{path}"
        headers = self._clob_auth_headers("GET", path)
        resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _clob_post_auth(self, path: str, json_body: Any) -> Any:
        import json as _json
        body_str = _json.dumps(json_body, separators=(",", ":"))
        url = f"{CLOB_BASE}{path}"
        headers = self._clob_auth_headers("POST", path, body_str)
        resp = self.session.post(url, data=body_str, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _clob_delete_auth(self, path: str, json_body: Any = None) -> Any:
        import json as _json
        body_str = _json.dumps(json_body or {}, separators=(",", ":"))
        url = f"{CLOB_BASE}{path}"
        headers = self._clob_auth_headers("DELETE", path, body_str)
        resp = self.session.delete(url, data=body_str, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Portfolio (authenticated)
    # ------------------------------------------------------------------

    def get_poly_positions(self) -> list[dict]:
        """
        Return current open positions (authenticated).

        Uses the CLOB /data endpoint.
        Returns a list of position dicts with keys:
          asset, size, avgPrice, currentPrice, unrealizedPnl, …
        """
        data = self._clob_get_auth("/data")
        return data if isinstance(data, list) else data.get("positions", [])

    # ------------------------------------------------------------------
    # Order placement (authenticated + EIP-712 signing)
    # ------------------------------------------------------------------

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
        order_type: str = "GTC",
    ) -> dict:
        """
        Place a limit order on the Polymarket CLOB.

        Parameters
        ----------
        token_id  : str   CLOB token ID (YES or NO, from clobTokenIds)
        side      : str   "BUY" or "SELL"
        price     : float Price in dollars (0.0 – 1.0)
        size_usdc : float Order size in USDC (dollar amount)
        order_type: str   "GTC" (default), "FOK", "FAK", "GTD"

        Requires
        --------
        - L2 credentials (POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE)
        - Polygon private key (POLY_PRIVATE_KEY) for EIP-712 signing
        - ``py-clob-client-v2`` package:  pip install py-clob-client-v2

        Raises
        ------
        RuntimeError if credentials or py-clob-client-v2 are not available.
        """
        if not self._is_authenticated:
            raise RuntimeError("Polymarket L2 credentials not configured.")
        if not self.private_key:
            raise RuntimeError(
                "POLY_PRIVATE_KEY not set.  "
                "Polymarket order placement requires a Polygon private key for EIP-712 signing."
            )
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType
        except ImportError as exc:
            raise RuntimeError(
                "py-clob-client-v2 not installed.  "
                "Run: pip install py-clob-client-v2\n"
                "Then re-run with your Polygon private key set in POLY_PRIVATE_KEY."
            ) from exc

        chain_id = 137  # Polygon mainnet
        client = ClobClient(
            host=CLOB_BASE,
            chain_id=chain_id,
            key=self.private_key,
            creds={
                "apiKey": self.api_key,
                "secret": self.api_secret,
                "passphrase": self.api_passphrase,
            },
        )
        order_type_map = {
            "GTC": OrderType.GTC,
            "FOK": OrderType.FOK,
            "FAK": OrderType.FAK,
            "GTD": OrderType.GTD,
        }
        signed_order = client.create_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=size_usdc / price,  # convert USDC → contract count
                side=side,
            )
        )
        return client.post_order(signed_order, order_type_map.get(order_type, OrderType.GTC))

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel an open order (authenticated).

        Requires L2 credentials (same as place_order, but no EIP-712 needed).
        """
        return self._clob_delete_auth("/order", json_body={"orderID": order_id})

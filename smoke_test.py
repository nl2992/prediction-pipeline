"""
Smoke test — verifies connectivity, response shapes, and order book parsing
for both Polymarket and Kalshi.

Fetches only a small number of markets to minimise API calls.
Uses KXARTEMISII (Artemis II launch date) as the Kalshi test series
because it is a well-known, actively traded market with real bid/ask data.

Run:
    python smoke_test.py
"""

from __future__ import annotations

import json
import sys
import traceback

from kalshi.client import KalshiClient
from pipeline import (
    OrderBook,
    _parse_kalshi_full_book,
    _parse_kalshi_top_of_book,
    _parse_polymarket_book,
)
from polymarket.client import PolymarketClient


def _ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str, exc: Exception | None = None) -> None:
    print(f"  [FAIL] {msg}")
    if exc:
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------


def test_polymarket() -> bool:
    print("\n=== Polymarket smoke test ===")
    try:
        client = PolymarketClient(timeout=20)

        # 1. Fetch markets from Gamma API
        markets = client.get_markets(limit=3, active=True, closed=False)
        assert isinstance(markets, list), "Expected list from get_markets()"
        assert len(markets) > 0, "No markets returned"
        _ok(f"Gamma API: received {len(markets)} market(s)")

        first = markets[0]
        title = first.get("question") or first.get("title", "")
        _ok(f"First market: {title[:80]}")

        # 2. Verify expected fields are present
        for field in ["conditionId", "active", "clobTokenIds", "outcomePrices"]:
            assert field in first, f"Missing field: {field}"
        _ok("Required fields present (conditionId, active, clobTokenIds, outcomePrices)")

        # 3. Fetch CLOB order book
        raw_ids = first.get("clobTokenIds")
        if raw_ids:
            if isinstance(raw_ids, str):
                token_ids = json.loads(raw_ids)
            else:
                token_ids = raw_ids

            if token_ids:
                raw_book = client.get_orderbook(token_ids[0])
                assert "bids" in raw_book, "CLOB book missing 'bids'"
                assert "asks" in raw_book, "CLOB book missing 'asks'"

                ob = _parse_polymarket_book(raw_book)
                assert isinstance(ob, OrderBook)

                bids = raw_book.get("bids", [])
                asks = raw_book.get("asks", [])
                _ok(f"CLOB order book: {len(bids)} bid level(s), {len(asks)} ask level(s)")

                if ob.best_bid is not None:
                    _ok(f"Best bid: ${ob.best_bid:.4f}")
                if ob.best_ask is not None:
                    _ok(f"Best ask: ${ob.best_ask:.4f}")
                if ob.mid is not None:
                    _ok(f"Mid:      ${ob.mid:.4f}  (spread: ${ob.spread:.4f})")

                _ok(f"tick_size={raw_book.get('tick_size')}  min_order_size={raw_book.get('min_order_size')}")
            else:
                _warn("clobTokenIds list is empty — skipping CLOB test")
        else:
            _warn("No clobTokenIds on first market — skipping CLOB test")

        return True

    except Exception as exc:
        _fail("Polymarket test failed", exc)
        return False


# ---------------------------------------------------------------------------
# Kalshi
# ---------------------------------------------------------------------------


def test_kalshi() -> bool:
    print("\n=== Kalshi smoke test ===")
    try:
        client = KalshiClient(timeout=20)

        # 1. Fetch markets (no filter — verify basic connectivity)
        resp = client.get_markets(limit=3)
        assert isinstance(resp, dict), "Expected dict from get_markets()"
        markets = resp.get("markets", [])
        assert len(markets) > 0, "No markets returned"
        _ok(f"GET /markets: received {len(markets)} market(s)")

        first = markets[0]
        assert "ticker" in first, "Market missing 'ticker'"
        assert "status" in first, "Market missing 'status'"
        _ok(f"Status field value: '{first['status']}' (API returns 'active' for open markets)")

        # 2. Fetch a known liquid series for meaningful bid/ask data
        resp2 = client.get_markets(series_ticker="KXARTEMISII", limit=3)
        artemis_markets = resp2.get("markets", [])
        assert len(artemis_markets) > 0, "No KXARTEMISII markets returned"
        _ok(f"KXARTEMISII series: {len(artemis_markets)} market(s)")

        liquid = artemis_markets[0]
        _ok(f"Ticker: {liquid['ticker']}")
        _ok(f"Title:  {liquid['title'][:80]}")

        # 3. Parse inline top-of-book
        top = KalshiClient.parse_top_of_book(liquid)
        ob_top = _parse_kalshi_top_of_book(liquid)
        _ok(
            f"Inline top-of-book — "
            f"YES bid=${top['yes_bid']}  YES ask=${top['yes_ask']}  "
            f"NO bid=${top['no_bid']}  NO ask=${top['no_ask']}"
        )
        if ob_top.best_bid and ob_top.best_ask:
            _ok(f"  mid=${ob_top.mid:.4f}  spread=${ob_top.spread:.4f}")

        # 4. Fetch full order book (public endpoint — no auth needed)
        ticker = liquid["ticker"]
        raw_book = client.get_orderbook(ticker)
        assert "orderbook_fp" in raw_book, "Missing 'orderbook_fp' in response"
        ob_fp = raw_book["orderbook_fp"]
        yes_levels = ob_fp.get("yes_dollars", [])
        no_levels = ob_fp.get("no_dollars", [])
        _ok(
            f"Full order book for {ticker}: "
            f"{len(yes_levels)} YES bid level(s), {len(no_levels)} NO bid level(s)"
        )

        # 5. Parse full book and verify derived asks
        ob_full = _parse_kalshi_full_book(raw_book)
        assert isinstance(ob_full, OrderBook)
        if ob_full.best_bid is not None:
            _ok(f"Best YES bid: ${ob_full.best_bid:.4f}  (size: {ob_full.bids[0].size})")
        if ob_full.best_ask is not None:
            _ok(f"Best YES ask: ${ob_full.best_ask:.4f}  (derived from NO bids)")
        if ob_full.mid is not None:
            _ok(f"Mid: ${ob_full.mid:.4f}  spread: ${ob_full.spread:.4f}")

        # 6. Sanity checks on the reconstructed YES order book
        if ob_full.best_bid is not None and ob_full.best_ask is not None:
            bid, ask = ob_full.best_bid, ob_full.best_ask
            if 0 < bid < ask < 1:
                _ok(
                    f"Book structure: 0 < bid={bid:.4f} < ask={ask:.4f} < 1.0 "
                    f"spread={ob_full.spread:.4f} ✓"
                )
            else:
                _warn(f"Book structure unexpected: bid={bid:.4f}  ask={ask:.4f}")

        # Complement check: yes_bid + no_ask ≈ 1.0 for a liquid market
        yes_bid_inline = top.get("yes_bid")
        no_ask_inline = top.get("no_ask")
        if yes_bid_inline and no_ask_inline:
            complement = yes_bid_inline + no_ask_inline
            if abs(complement - 1.0) < 0.05:
                _ok(f"Complement: yes_bid({yes_bid_inline}) + no_ask({no_ask_inline}) = {complement:.4f} ≈ 1.0 ✓")
            else:
                _warn(f"Complement: yes_bid + no_ask = {complement:.4f} (expected ~1.0)")

        # 7. Events endpoint
        evts = client.get_events(limit=3)
        assert "events" in evts, "Missing 'events' key"
        _ok(f"GET /events: received {len(evts['events'])} event(s)")

        return True

    except Exception as exc:
        _fail("Kalshi test failed", exc)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    poly_ok = test_polymarket()
    kalshi_ok = test_kalshi()

    print("\n=== Summary ===")
    print(f"  Polymarket : {'PASS' if poly_ok else 'FAIL'}")
    print(f"  Kalshi     : {'PASS' if kalshi_ok else 'FAIL'}")

    if not (poly_ok and kalshi_ok):
        sys.exit(1)
    print("\nAll smoke tests passed.")

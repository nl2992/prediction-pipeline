"""
Prediction Market Monitor
=========================
Continuously polls Polymarket and Kalshi, runs cross-exchange matching and
arbitrage detection, verifies any profitable signals against live CLOBs, and
writes confirmed opportunities to ``signals.jsonl`` for downstream execution.

Usage
-----
    # Watch loop — scan every 5 minutes, print and log signals
    python monitor.py

    # Single scan — useful for cron / manual checks
    python monitor.py --once

    # Activate auto-execution in dry-run mode (logs intents, no real orders)
    python monitor.py --execute --dry-run

    # Live execution — requires Kalshi + Polymarket credentials in environment
    python monitor.py --execute --no-dry-run --max-position 50

    # Focus on Fed rate markets
    python monitor.py --kalshi-series KXFED --poly-keywords "fed rate" "federal funds"

CLI flags
---------
    --interval INT          Seconds between scans (default: 300)
    --once                  Exit after one scan
    --dry-run / --no-dry-run
                            Dry-run mode for execution (default: dry-run)
    --execute               Auto-execute verified profitable signals
    --min-profit-pct FLOAT  Minimum net profit %% to consider a live signal (default: 0.5)
    --max-position FLOAT    Max combined USD exposure per two-leg trade (default: 100)
    --fee-poly FLOAT        Polymarket fee rate (default: 0.02)
    --fee-kalshi FLOAT      Kalshi fee rate (default: 0.07)
    --poly-limit INT        Polymarket markets per scan (default: 50)
    --kalshi-limit INT      Kalshi markets per scan (default: 50)
    --kalshi-series STR     Kalshi series ticker filter, e.g. KXFED
    --kalshi-event STR      Kalshi event ticker filter
    --poly-keywords KW ...  Extra Polymarket title keywords to filter (substring match)
    --min-match-sim FLOAT   Minimum Jaccard title similarity for market pairing (default: 0.25)
    --no-verify             Skip live CLOB re-verification of arb signals (not recommended)
    --signals-file PATH     Output JSONL for signals (default: signals.jsonl)
    --log-file PATH         Log file path (default: monitor.log)
    --log-level STR         Logging verbosity: DEBUG | INFO | WARNING (default: INFO)

Output files
------------
signals.jsonl   One JSON object per line, one per verified signal per scan.
monitor.log     Full scan log with timestamps (append mode).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging — dual handler: console (INFO) + file (DEBUG)
# ---------------------------------------------------------------------------

logger = logging.getLogger("monitor")


def _setup_logging(log_file: str, level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(numeric_level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # File handler (always DEBUG so we never lose detail)
    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)


# ---------------------------------------------------------------------------
# Signal data model
# ---------------------------------------------------------------------------


@dataclass
class ArbSignal:
    """One verified cross-exchange arb signal written to signals.jsonl."""

    scan_id: str
    ts: str                         # ISO 8601 UTC timestamp of detection
    signal_type: str                # "cross_exchange_arb"
    direction: str                  # "poly_yes__kalshi_no" | "kalshi_yes__poly_no"

    poly_title: str
    poly_market_id: str
    poly_token_id: str | None       # CLOB YES token id (None if unresolved)
    poly_no_token_id: str | None    # CLOB NO token id (None if unresolved)
    poly_yes_ask: float | None
    poly_yes_bid: float | None

    kalshi_ticker: str
    kalshi_title: str
    kalshi_yes_bid: float | None
    kalshi_yes_ask: float | None

    gross_cost: float
    net_profit: float
    net_profit_pct: float
    winning_leg_fee: float
    max_size_contracts: float | None

    match_confidence: float
    match_via_override: bool

    clob_verified: bool             # did the live CLOB recheck pass?
    clob_live_poly_ask: float | None = None
    clob_live_poly_bid: float | None = None
    clob_live_kalshi_bid: float | None = None
    clob_notes: list[str] = field(default_factory=list)

    executed: bool = False
    execution_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary_line(self) -> str:
        direction_arrow = (
            "Poly→YES  Kalshi→NO"
            if self.direction == "poly_yes__kalshi_no"
            else "Kalshi→YES  Poly→NO"
        )
        verified_tag = "[CLOB OK]" if self.clob_verified else "[UNVERIFIED]"
        return (
            f"{verified_tag} {direction_arrow}  "
            f"net={self.net_profit:.4f} ({self.net_profit_pct:.2f}%)  "
            f"gross_cost={self.gross_cost:.4f}  "
            f"size={self.max_size_contracts}  "
            f"Poly: {self.poly_title[:45]!r}  "
            f"Kalshi: {self.kalshi_title[:45]!r}"
        )


# ---------------------------------------------------------------------------
# CLOB verification helpers
# ---------------------------------------------------------------------------


def _resolve_poly_token(snap, side: str = "YES") -> str | None:
    """
    Extract the requested CLOB token from a Polymarket MarketSnapshot.

    ``clobTokenIds`` aligns with the market's ``outcomes`` array.  Most binary
    markets are YES/NO in that order, but categorical markets and API edge cases
    make label matching safer than assuming index 0.
    """
    token_ids = snap.extra.get("clob_token_ids") or []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except Exception:
            return None
    outcomes = snap.extra.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []

    wanted = side.strip().lower()
    for i, outcome in enumerate(outcomes or []):
        if isinstance(outcome, str) and outcome.strip().lower() == wanted and i < len(token_ids):
            return token_ids[i]

    if not token_ids:
        return None
    if wanted == "no" and len(token_ids) > 1:
        return token_ids[1]
    return token_ids[0]


def _resolve_poly_yes_token(snap) -> str | None:
    """Backward-compatible wrapper for callers that need the YES token."""
    return _resolve_poly_token(snap, "YES")


def _verify_poly_clob(
    token_id: str,
    expected_price: float,
    side: str = "ask",
    price_tolerance: float = 0.03,
    min_depth_usd: float = 10.0,
) -> tuple[bool, dict]:
    """
    Re-check Polymarket CLOB for a given YES token.

    ``side`` is the live side to validate: ``"ask"`` when buying a token and
    ``"bid"`` when validating the complement price from a YES bid.
    """
    details: dict[str, Any] = {"token_id": token_id[:16] + "..."}
    try:
        from polymarket.client import PolymarketClient
        client = PolymarketClient(timeout=12)
        liq = client.verify_clob_liquidity(
            token_id, min_depth_usd=min_depth_usd
        )
        live_ask = liq.get("best_ask")
        live_bid = liq.get("best_bid")
        depth = liq.get("ask_size") if side == "ask" else liq.get("bid_size")
        live_price = live_ask if side == "ask" else live_bid

        details["live_ask"] = live_ask
        details["live_bid"] = live_bid
        details["depth_usd"] = depth
        details["clob_ok"] = liq.get("is_liquid", False)

        if live_price is None:
            details["reason"] = f"no live {side}"
            return False, details

        drift = abs(live_price - expected_price)
        details["price_drift"] = round(drift, 5)

        if drift > price_tolerance:
            details["reason"] = f"price drifted {drift:.4f} > tolerance {price_tolerance}"
            return False, details

        if depth is not None and depth < min_depth_usd:
            details["reason"] = f"depth ${depth:.2f} < min ${min_depth_usd:.2f}"
            return False, details

        details["reason"] = "ok"
        return True, details

    except Exception as exc:
        details["reason"] = f"CLOB check error: {exc}"
        logger.debug("Polymarket CLOB check failed for %s: %s", token_id[:16], exc)
        return False, details


def _verify_kalshi_clob(
    ticker: str,
    expected_price: float,
    side: str = "bid",
    price_tolerance: float = 0.03,
) -> tuple[bool, dict]:
    """
    Re-check Kalshi orderbook for a given ticker.

    ``side="bid"`` validates the live YES bid.  ``side="ask"`` validates the
    live YES ask derived from the best NO bid.
    """
    details: dict[str, Any] = {"ticker": ticker}
    try:
        from kalshi.client import KalshiClient
        from executor import best_kalshi_bid
        client = KalshiClient(timeout=12)
        raw = client.get_orderbook(ticker)
        ob_fp = raw.get("orderbook_fp", {})

        # Pick best by PRICE, not list order — the raw orderbook_fp isn't guaranteed
        # sorted (pipeline sorts it), so [-1] could be a non-best level (#63).
        best_yes = best_kalshi_bid(ob_fp.get("yes_dollars", []))
        best_no = best_kalshi_bid(ob_fp.get("no_dollars", []))
        live_bid = best_yes
        live_ask = round(1.0 - best_no, 6) if best_no is not None else None
        live_price = live_bid if side == "bid" else live_ask
        details["live_yes_bid"] = live_bid
        details["live_yes_ask"] = live_ask

        if live_price is None:
            details["reason"] = f"no YES {side} in live book"
            return False, details

        drift = abs(live_price - expected_price)
        details["price_drift"] = round(drift, 5)

        if drift > price_tolerance:
            details["reason"] = f"{side} drifted {drift:.4f} > tolerance {price_tolerance}"
            return False, details

        details["reason"] = "ok"
        return True, details

    except Exception as exc:
        details["reason"] = f"orderbook check error: {exc}"
        logger.debug("Kalshi orderbook check failed for %s: %s", ticker, exc)
        return False, details


# ---------------------------------------------------------------------------
# One scan
# ---------------------------------------------------------------------------


@dataclass
class ScanSummary:
    scan_id: str
    ts: str
    poly_markets: int
    kalshi_markets: int
    matched_pairs: int
    arb_candidates: int
    profitable_raw: int       # before CLOB verification
    verified_signals: int     # after CLOB verification
    signals: list[ArbSignal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


def run_one_scan(
    poly_limit: int = 50,
    kalshi_limit: int = 50,
    kalshi_series: str | None = None,
    kalshi_event: str | None = None,
    poly_keywords: list[str] | None = None,
    poly_event_slug: str | None = None,
    fee_poly: float = 0.02,
    fee_kalshi: float = 0.07,
    min_profit_pct: float = 0.5,
    min_match_sim: float = 0.30,
    max_close_delta_hours: float = 72.0,
    verify_clob: bool = True,
    clob_price_tolerance: float = 0.03,
    clob_min_depth_usd: float = 10.0,
    scan_id: str | None = None,
) -> ScanSummary:
    """
    Fetch both exchanges, match markets, detect arb, and verify live CLOBs.

    Returns a ScanSummary with all found ArbSignals.
    """
    t0 = time.time()
    scan_id = scan_id or uuid.uuid4().hex[:12]
    ts_now = datetime.now(timezone.utc).isoformat()
    summary = ScanSummary(scan_id=scan_id, ts=ts_now, poly_markets=0,
                          kalshi_markets=0, matched_pairs=0,
                          arb_candidates=0, profitable_raw=0,
                          verified_signals=0)

    # 1. Fetch both exchanges
    try:
        from pipeline import fetch_polymarket, fetch_kalshi
        poly_snaps = fetch_polymarket(
            limit=poly_limit,
            fetch_orderbooks=True,
            keywords=poly_keywords or None,
            event_slug=poly_event_slug or None,
        )
        summary.poly_markets = len(poly_snaps)
    except Exception as exc:
        msg = f"Polymarket fetch failed: {exc}"
        logger.error(msg)
        summary.errors.append(msg)
        poly_snaps = []

    try:
        from pipeline import fetch_kalshi
        kalshi_snaps = fetch_kalshi(
            limit=kalshi_limit,
            fetch_full_orderbooks=True,
            series_ticker=kalshi_series,
            event_ticker=kalshi_event,
        )
        summary.kalshi_markets = len(kalshi_snaps)
    except Exception as exc:
        msg = f"Kalshi fetch failed: {exc}"
        logger.error(msg)
        summary.errors.append(msg)
        kalshi_snaps = []

    if not poly_snaps or not kalshi_snaps:
        summary.elapsed_s = round(time.time() - t0, 2)
        return summary

    # 2. Match markets
    try:
        from matcher import match_markets
        pairs = match_markets(
            poly_snaps, kalshi_snaps,
            min_title_similarity=min_match_sim,
            max_close_delta_hours=max_close_delta_hours,
        )
        summary.matched_pairs = len(pairs)
    except Exception as exc:
        msg = f"Matcher failed: {exc}"
        logger.error(msg)
        summary.errors.append(msg)
        pairs = []

    if not pairs:
        summary.elapsed_s = round(time.time() - t0, 2)
        return summary

    # 3. Arb detection
    try:
        from arb import find_arb
        opps = find_arb(
            pairs,
            fee_poly=fee_poly,
            fee_kalshi=fee_kalshi,
            min_net_profit_pct=min_profit_pct,
        )
        summary.arb_candidates = len(opps)
        profitable = [o for o in opps if o.is_profitable]
        summary.profitable_raw = len(profitable)
    except Exception as exc:
        msg = f"Arb detection failed: {exc}"
        logger.error(msg)
        summary.errors.append(msg)
        profitable = []

    # 4. CLOB verification for each profitable candidate
    for opp in profitable:
        pair = opp.pair
        poly_snap = pair.poly
        kalshi_snap = pair.kalshi

        yes_token_id = _resolve_poly_token(poly_snap, "YES")
        no_token_id = _resolve_poly_token(poly_snap, "NO")

        signal = ArbSignal(
            scan_id=scan_id,
            ts=ts_now,
            signal_type="cross_exchange_arb",
            direction=opp.direction,
            poly_title=poly_snap.title,
            poly_market_id=poly_snap.market_id,
            poly_token_id=yes_token_id,
            poly_no_token_id=no_token_id,
            poly_yes_ask=poly_snap.orderbook.best_ask,
            poly_yes_bid=poly_snap.orderbook.best_bid,
            kalshi_ticker=kalshi_snap.market_id,
            kalshi_title=kalshi_snap.title,
            kalshi_yes_bid=kalshi_snap.orderbook.best_bid,
            kalshi_yes_ask=kalshi_snap.orderbook.best_ask,
            gross_cost=opp.gross_cost,
            net_profit=opp.net_profit,
            net_profit_pct=opp.net_profit_pct,
            winning_leg_fee=opp.winning_leg_fee,
            max_size_contracts=opp.max_size,
            match_confidence=pair.confidence,
            match_via_override=pair.via_override,
            clob_verified=False,
        )

        if not verify_clob:
            signal.clob_verified = True
            signal.clob_notes.append("verification skipped (--no-verify)")
        else:
            # Polymarket YES CLOB check
            poly_ok = True
            if opp.direction == "poly_yes__kalshi_no":
                # We're buying YES on Poly — verify live YES ask
                if yes_token_id and opp.buy_yes_ask:
                    ok, details = _verify_poly_clob(
                        yes_token_id,
                        expected_price=opp.buy_yes_ask,
                        side="ask",
                        price_tolerance=clob_price_tolerance,
                        min_depth_usd=clob_min_depth_usd,
                    )
                    signal.clob_live_poly_ask = details.get("live_ask")
                    signal.clob_live_poly_bid = details.get("live_bid")
                    signal.clob_notes.append(f"poly_clob: {details.get('reason', '?')}")
                    if not ok:
                        poly_ok = False
                        logger.info(
                            "[scan=%s] Poly CLOB fail for %s: %s",
                            scan_id, poly_snap.market_id[:20], details.get("reason")
                        )
                else:
                    signal.clob_notes.append("poly_clob: no token_id — skipped")
                    poly_ok = False  # no token = can't verify or trade

                # Kalshi YES bid check (we're buying NO → care about YES bid depth)
                if kalshi_snap.orderbook.best_bid:
                    ok_k, details_k = _verify_kalshi_clob(
                        kalshi_snap.market_id,
                        expected_price=kalshi_snap.orderbook.best_bid,
                        side="bid",
                        price_tolerance=clob_price_tolerance,
                    )
                    signal.clob_live_kalshi_bid = details_k.get("live_yes_bid")
                    signal.clob_notes.append(f"kalshi_clob: {details_k.get('reason', '?')}")
                    if not ok_k:
                        poly_ok = False  # reuse flag — both legs must pass

            else:  # kalshi_yes__poly_no
                # Buying YES on Kalshi — verify live YES ask
                if kalshi_snap.orderbook.best_ask:
                    ok_k, details_k = _verify_kalshi_clob(
                        kalshi_snap.market_id,
                        expected_price=kalshi_snap.orderbook.best_ask,
                        side="ask",
                        price_tolerance=clob_price_tolerance,
                    )
                    signal.clob_live_kalshi_bid = details_k.get("live_yes_bid")
                    signal.clob_notes.append(f"kalshi_live_ask: {details_k.get('live_yes_ask')}")
                    signal.clob_notes.append(f"kalshi_clob: {details_k.get('reason', '?')}")
                    if not ok_k:
                        poly_ok = False

                # Polymarket NO check (we're buying the NO token directly)
                if no_token_id and opp.buy_no_ask:
                    ok_p, details_p = _verify_poly_clob(
                        no_token_id,
                        expected_price=opp.buy_no_ask,
                        side="ask",
                        price_tolerance=clob_price_tolerance,
                        min_depth_usd=clob_min_depth_usd,
                    )
                    signal.clob_live_poly_bid = details_p.get("live_bid")
                    signal.clob_live_poly_ask = details_p.get("live_ask")
                    signal.clob_notes.append(f"poly_clob: {details_p.get('reason', '?')}")
                    if not ok_p:
                        poly_ok = False
                else:
                    signal.clob_notes.append("poly_clob: no NO token_id — skipped")
                    poly_ok = False

            signal.clob_verified = poly_ok

        if signal.clob_verified:
            summary.verified_signals += 1

        summary.signals.append(signal)

    summary.elapsed_s = round(time.time() - t0, 2)
    return summary


# ---------------------------------------------------------------------------
# Discover-mode scan  (uses organic event-catalog discovery from discover.py)
# ---------------------------------------------------------------------------


def _detect_pair_arb(pa: float, pb: float, ka: float, kb: float, worst_fee: float):
    """Best two-leg arb for one priced pair (flat worst-case fee), matching
    arb.find_arb economics. Returns (arb_dir, arb_profit, cost_pyk, cost_kyp).
    arb_dir/arb_profit are None when poly_yes isn't profitable, and arb_profit may be
    <= 0 (the caller filters non-positive). Extracted from run_discover_scan so the
    money math is unit-testable (#66)."""
    arb_dir = arb_profit = None
    # poly_yes + kalshi_no: buy YES on Poly at ask, buy NO on Kalshi at (1 - bid)
    cost_pyk = pa + (1.0 - kb)
    profit_pyk = round(1.0 - cost_pyk - worst_fee, 6)
    if profit_pyk > 0:
        arb_dir = "poly_yes__kalshi_no"
        arb_profit = profit_pyk
    # kalshi_yes + poly_no: buy YES on Kalshi at ask, buy NO on Poly at (1 - bid)
    cost_kyp = ka + (1.0 - pb)
    profit_kyp = round(1.0 - cost_kyp - worst_fee, 6)
    if arb_profit is None or profit_kyp > arb_profit:
        arb_dir = "kalshi_yes__poly_no"
        arb_profit = profit_kyp
    return arb_dir, arb_profit, cost_pyk, cost_kyp


def run_discover_scan(
    category: str = "all",
    days: int | None = None,
    min_sim: float = 0.28,
    max_events: int | None = None,
    fee_poly: float = 0.02,
    fee_kalshi: float = 0.07,
    min_profit_pct: float = 0.5,
    verify_clob: bool = True,
    clob_price_tolerance: float = 0.03,
    clob_min_depth_usd: float = 10.0,
    scan_id: str | None = None,
) -> ScanSummary:
    """
    Organic scan: run discover() to find cross-exchange pairs via the events
    catalog, fetch live orderbooks, detect arb, and verify via CLOB.

    This replaces the fixed-series run_one_scan() with a broad, auto-discovering
    scan that covers all Kalshi categories.
    """
    t0 = time.time()
    scan_id = scan_id or uuid.uuid4().hex[:12]
    ts_now = datetime.now(timezone.utc).isoformat()
    summary = ScanSummary(
        scan_id=scan_id, ts=ts_now,
        poly_markets=0, kalshi_markets=0, matched_pairs=0,
        arb_candidates=0, profitable_raw=0, verified_signals=0,
    )

    try:
        from discover import discover
        # Pass show_prices=False — catalog mid-prices are sufficient for the
        # first-pass arb screen.  We fetch live CLOB only for arb candidates
        # (below), which keeps the scan fast (no per-pair orderbook calls here).
        pairs = discover(
            category=category,
            days=days,
            min_sim=min_sim,
            show_prices=False,
            max_events_to_search=max_events,
        )
    except Exception as exc:
        msg = f"Discovery failed: {exc}"
        logger.error(msg)
        summary.errors.append(msg)
        summary.elapsed_s = round(time.time() - t0, 2)
        return summary

    summary.matched_pairs = len(pairs)
    # poly/kalshi market counts come from discover internals; approximate here
    summary.poly_markets = len({p["poly_id"] for p in pairs})
    summary.kalshi_markets = len({p["kalshi_ticker"] for p in pairs})

    worst_fee = max(fee_poly, fee_kalshi)

    for pair in pairs:
        pb = pair.get("poly_bid")
        pa = pair.get("poly_ask")
        kb = pair.get("kalshi_bid")
        ka = pair.get("kalshi_ask")

        if not (pa and kb and ka and pb):
            continue  # no live prices — skip arb check

        arb_dir, arb_profit, cost_pyk, cost_kyp = _detect_pair_arb(pa, pb, ka, kb, worst_fee)
        if arb_profit is None or arb_profit <= 0:
            continue

        summary.arb_candidates += 1
        gross_cost_for_pct = cost_pyk if arb_dir == "poly_yes__kalshi_no" else cost_kyp
        net_pct = round(arb_profit / gross_cost_for_pct * 100, 4) if gross_cost_for_pct > 0 else 0.0
        if net_pct < min_profit_pct:
            continue

        summary.profitable_raw += 1

        # Build a minimal ArbSignal (token_id not available from discover dicts)
        poly_token_id = None  # YES token; resolved below when verification runs
        poly_no_token_id = None
        if arb_dir == "poly_yes__kalshi_no":
            gross_cost = round(pa + (1.0 - kb), 6)
            winning_fee = worst_fee
        else:
            gross_cost = round(ka + (1.0 - pb), 6)
            winning_fee = worst_fee

        signal = ArbSignal(
            scan_id=scan_id,
            ts=ts_now,
            signal_type="cross_exchange_arb",
            direction=arb_dir,
            poly_title=pair["poly_title"],
            poly_market_id=pair["poly_id"],
            poly_token_id=poly_token_id,
            poly_no_token_id=poly_no_token_id,
            poly_yes_ask=pa,
            poly_yes_bid=pb,
            kalshi_ticker=pair["kalshi_ticker"],
            kalshi_title=pair["kalshi_title"],
            kalshi_yes_bid=kb,
            kalshi_yes_ask=ka,
            gross_cost=gross_cost,
            net_profit=round(arb_profit, 6),
            net_profit_pct=net_pct,
            winning_leg_fee=winning_fee,
            max_size_contracts=None,
            match_confidence=pair.get("confidence", 0.0),
            match_via_override=False,
            clob_verified=False,
        )

        if not verify_clob:
            signal.clob_verified = True
            signal.clob_notes.append("verification skipped (--no-verify)")
        else:
            # For discover mode we don't have a poly token_id stored per pair.
            # Re-fetch it from the Polymarket catalog using the conditionId.
            poly_ok = True
            try:
                from polymarket.client import PolymarketClient
                pc = PolymarketClient()
                mkt_data = pc._get(
                    "https://gamma-api.polymarket.com",
                    f"/markets/{pair['poly_id']}",
                )
                tids = mkt_data.get("clobTokenIds") or "[]"
                if isinstance(tids, str):
                    tids = json.loads(tids)
                poly_token_id = tids[0] if tids else None
                poly_no_token_id = tids[1] if len(tids) > 1 else None
                signal.poly_token_id = poly_token_id
                signal.poly_no_token_id = poly_no_token_id
            except Exception:
                poly_token_id = None
                poly_no_token_id = None

            if arb_dir == "poly_yes__kalshi_no":
                if poly_token_id:
                    ok_p, det_p = _verify_poly_clob(
                        poly_token_id,
                        expected_price=pa,
                        side="ask",
                        price_tolerance=clob_price_tolerance,
                        min_depth_usd=clob_min_depth_usd,
                    )
                    signal.clob_live_poly_ask = det_p.get("live_ask")
                    signal.clob_live_poly_bid = det_p.get("live_bid")
                    signal.clob_notes.append(f"poly_clob: {det_p.get('reason', '?')}")
                    if not ok_p:
                        poly_ok = False
                else:
                    signal.clob_notes.append("poly_clob: no token_id")
                    poly_ok = False

                ok_k, det_k = _verify_kalshi_clob(
                    pair["kalshi_ticker"],
                    expected_price=kb,
                    side="bid",
                    price_tolerance=clob_price_tolerance,
                )
                signal.clob_live_kalshi_bid = det_k.get("live_yes_bid")
                signal.clob_notes.append(f"kalshi_clob: {det_k.get('reason', '?')}")
                if not ok_k:
                    poly_ok = False
            else:  # kalshi_yes__poly_no
                ok_k, det_k = _verify_kalshi_clob(
                    pair["kalshi_ticker"],
                    expected_price=ka,
                    side="ask",
                    price_tolerance=clob_price_tolerance,
                )
                signal.clob_live_kalshi_bid = det_k.get("live_yes_bid")
                signal.clob_notes.append(f"kalshi_clob: {det_k.get('reason', '?')}")
                if not ok_k:
                    poly_ok = False

                if poly_no_token_id:
                    ok_p, det_p = _verify_poly_clob(
                        poly_no_token_id,
                        expected_price=1.0 - pb,
                        side="ask",
                        price_tolerance=clob_price_tolerance,
                        min_depth_usd=clob_min_depth_usd,
                    )
                    signal.clob_live_poly_bid = det_p.get("live_bid")
                    signal.clob_live_poly_ask = det_p.get("live_ask")
                    signal.clob_notes.append(f"poly_clob: {det_p.get('reason', '?')}")
                    if not ok_p:
                        poly_ok = False
                else:
                    signal.clob_notes.append("poly_clob: no token_id")
                    poly_ok = False

            signal.clob_verified = poly_ok

        if signal.clob_verified:
            summary.verified_signals += 1

        summary.signals.append(signal)

    summary.elapsed_s = round(time.time() - t0, 2)
    return summary


# ---------------------------------------------------------------------------
# Signal file writer
# ---------------------------------------------------------------------------


def append_signals(signals: list[ArbSignal], signals_file: str) -> None:
    """Append verified ArbSignal objects to signals.jsonl (one JSON per line)."""
    from jsonl_utils import cap_jsonl
    path = Path(signals_file)
    with path.open("a", encoding="utf-8") as f:
        for sig in signals:
            f.write(json.dumps(sig.to_dict(), default=str) + "\n")
    # Bound the append-only signal log so a long-running monitor can't grow it
    # without limit (#32; same treatment as the alerter's audit logs).
    cap_jsonl(path, max_bytes=8_000_000, keep_rows=50_000, tag="monitor")


# ---------------------------------------------------------------------------
# Execution bridge
# ---------------------------------------------------------------------------


def execute_signal(
    signal: ArbSignal,
    dry_run: bool = True,
    max_position_usd: float = 100.0,
    fee_poly: float = 0.02,
    fee_kalshi: float = 0.07,
    size_contracts: float = 10.0,
) -> list[str]:
    """
    Convert a verified ArbSignal into TradeIntents and execute via Executor.

    Returns a list of note strings describing what happened.
    """
    notes: list[str] = []
    try:
        from executor import Executor, TradeIntent

        # Build leg intents from signal data
        if signal.direction == "poly_yes__kalshi_no":
            leg_a = TradeIntent(
                exchange="polymarket",
                contract_side="YES",
                limit_price=signal.poly_yes_ask or 0.0,
                size_contracts=size_contracts,
                market_id=signal.poly_market_id,
                token_id=signal.poly_token_id,
                description=f"Buy YES on Poly @ {signal.poly_yes_ask}",
            )
            leg_b = TradeIntent(
                exchange="kalshi",
                contract_side="NO",
                limit_price=round(1.0 - (signal.kalshi_yes_bid or 0.0), 6),
                size_contracts=size_contracts,
                market_id=signal.kalshi_ticker,
                token_id=None,
                description=f"Buy NO on Kalshi @ {round(1.0 - (signal.kalshi_yes_bid or 0), 6)}",
            )
        else:  # kalshi_yes__poly_no
            leg_a = TradeIntent(
                exchange="kalshi",
                contract_side="YES",
                limit_price=signal.kalshi_yes_ask or 0.0,
                size_contracts=size_contracts,
                market_id=signal.kalshi_ticker,
                token_id=None,
                description=f"Buy YES on Kalshi @ {signal.kalshi_yes_ask}",
            )
            leg_b = TradeIntent(
                exchange="polymarket",
                contract_side="NO",
                limit_price=round(1.0 - (signal.poly_yes_bid or 0.0), 6),
                size_contracts=size_contracts,
                market_id=signal.poly_market_id,
                token_id=signal.poly_no_token_id,
                description=f"Buy NO on Poly @ {round(1.0 - (signal.poly_yes_bid or 0), 6)}",
            )

        executor = Executor(
            dry_run=dry_run,
            max_position_usd=max_position_usd,
            fee_poly=fee_poly,
            fee_kalshi=fee_kalshi,
        )
        result = executor.execute(leg_a, leg_b)
        notes.extend(result.notes)
        if result.success:
            notes.append(f"Execution SUCCESS — net_profit_est={result.net_profit_estimate}")
        else:
            notes.append("Execution FAILED — see notes above")

    except Exception as exc:
        notes.append(f"Execution error: {exc}")
        logger.error("Execution error for signal %s: %s", signal.scan_id, exc)

    return notes


# ---------------------------------------------------------------------------
# Scan printer
# ---------------------------------------------------------------------------


def print_scan_summary(summary: ScanSummary, show_unverified: bool = False) -> None:
    """Print a human-readable scan summary to stdout (via logger)."""
    logger.info(
        "━━━  SCAN %s  |  %s  |  elapsed=%.1fs  ━━━",
        summary.scan_id, summary.ts[:19], summary.elapsed_s,
    )
    logger.info(
        "  Markets: Poly=%d  Kalshi=%d  Pairs=%d  "
        "ArbCandidates=%d  ProfitableRaw=%d  VerifiedSignals=%d",
        summary.poly_markets, summary.kalshi_markets, summary.matched_pairs,
        summary.arb_candidates, summary.profitable_raw, summary.verified_signals,
    )

    for sig in summary.signals:
        if not sig.clob_verified and not show_unverified:
            continue
        tag = "✓ VERIFIED" if sig.clob_verified else "✗ UNVERIFIED"
        logger.info("  %s  %s", tag, sig.summary_line())
        for note in sig.clob_notes:
            logger.debug("    clob_note: %s", note)

    for err in summary.errors:
        logger.warning("  ERROR: %s", err)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Continuous prediction market arbitrage monitor.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--interval", type=int, default=300,
                   help="Seconds between scans")
    p.add_argument("--once", action="store_true",
                   help="Run a single scan and exit")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                   help="Dry-run mode for execution (no real orders)")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                   help="Enable live order placement (requires credentials)")
    p.add_argument("--execute", action="store_true",
                   help="Auto-execute verified profitable signals via executor")
    p.add_argument("--min-profit-pct", type=float, default=0.5,
                   help="Minimum net profit %% to flag a signal")
    p.add_argument("--max-position", type=float, default=100.0,
                   help="Max combined USD exposure per two-leg trade")
    p.add_argument("--fee-poly", type=float, default=0.02,
                   help="Polymarket fee rate on $1 payout")
    p.add_argument("--fee-kalshi", type=float, default=0.07,
                   help="Kalshi fee rate on $1 payout")
    p.add_argument("--poly-limit", type=int, default=50,
                   help="Polymarket markets per scan")
    p.add_argument("--kalshi-limit", type=int, default=50,
                   help="Kalshi markets per scan")
    p.add_argument("--kalshi-series", default=None,
                   help="Kalshi series ticker filter, e.g. KXFED")
    p.add_argument("--kalshi-event", default=None,
                   help="Kalshi event ticker filter")
    p.add_argument("--poly-event-slug", default=None,
                   help="Fetch all markets for a specific Polymarket event by URL slug "
                        "(e.g. 'ky-04-republican-primary-winner'). Takes priority over --poly-keywords.")
    p.add_argument("--poly-keywords", nargs="*", default=[],
                   help="Extra Polymarket title keywords (substring, case-insensitive)")
    p.add_argument("--min-match-sim", type=float, default=0.30,
                   help="Minimum Jaccard title similarity for cross-exchange pairing")
    p.add_argument("--max-close-delta-hours", type=float, default=72.0,
                   help="Normalisation window (hours) for close-time proximity scoring. "
                        "Pairs within this window get a time-proximity bonus; pairs beyond "
                        "it are scored on title similarity alone. No longer a hard exclusion "
                        "gate — sports markets with far-out contractual expiry dates are "
                        "always considered. (default: 72)")
    p.add_argument("--no-verify", dest="verify_clob", action="store_false", default=True,
                   help="Skip live CLOB re-verification (not recommended)")
    p.add_argument("--size-contracts", type=float, default=10.0,
                   help="Number of contracts per leg when executing")
    p.add_argument("--signals-file", default="signals.jsonl",
                   help="Path to JSONL signal output file")
    p.add_argument("--log-file", default="monitor.log",
                   help="Path to log file (appended)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Console logging verbosity")
    p.add_argument("--show-unverified", action="store_true",
                   help="Print unverified (CLOB-failed) signals in output")
    # ── Discover mode ──────────────────────────────────────────────────────────
    p.add_argument("--discover", action="store_true",
                   help="Use organic event-catalog discovery (discover.py) instead of "
                        "fixed-series fetching.  Covers all market categories automatically.")
    p.add_argument("--discover-category", default="all",
                   choices=["all", "election", "sports", "economic", "political", "pop"],
                   help="Category filter for discover mode (default: all)")
    p.add_argument("--discover-days", type=int, default=None,
                   help="Horizon in days for discover mode; omit for no day limit")
    p.add_argument("--discover-max-events", type=int, default=None,
                   help="Max Kalshi events to scan per discovery cycle; omit for no event limit")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    _setup_logging(args.log_file, args.log_level)

    # Graceful shutdown on Ctrl-C / SIGTERM
    _running = [True]

    def _stop(sig, frame):
        logger.info("Received signal %s — shutting down after current scan.", sig)
        _running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    mode_tag = "DRY-RUN" if args.dry_run else "LIVE"
    exec_tag = f"  auto-execute={mode_tag}" if args.execute else "  monitoring-only"

    scan_mode = (
        f"discover(category={args.discover_category}, days={args.discover_days})"
        if args.discover else "fixed-series"
    )
    logger.info(
        "Monitor starting — mode=%s  interval=%ds%s  signals→%s  log→%s",
        scan_mode, args.interval, exec_tag, args.signals_file, args.log_file,
    )
    if not args.dry_run and args.execute:
        logger.warning(
            "LIVE EXECUTION MODE — real orders will be placed when signals are verified."
        )

    scan_count = 0
    total_verified = 0

    while _running[0]:
        scan_count += 1
        logger.info("Starting scan #%d…", scan_count)

        try:
            if args.discover:
                summary = run_discover_scan(
                    category=args.discover_category,
                    days=args.discover_days,
                    min_sim=args.min_match_sim,
                    max_events=args.discover_max_events,
                    fee_poly=args.fee_poly,
                    fee_kalshi=args.fee_kalshi,
                    min_profit_pct=args.min_profit_pct,
                    verify_clob=args.verify_clob,
                )
            else:
                summary = run_one_scan(
                    poly_limit=args.poly_limit,
                    kalshi_limit=args.kalshi_limit,
                    kalshi_series=args.kalshi_series,
                    kalshi_event=args.kalshi_event,
                    poly_keywords=args.poly_keywords or None,
                    poly_event_slug=args.poly_event_slug or None,
                    fee_poly=args.fee_poly,
                    fee_kalshi=args.fee_kalshi,
                    min_profit_pct=args.min_profit_pct,
                    min_match_sim=args.min_match_sim,
                    max_close_delta_hours=args.max_close_delta_hours,
                    verify_clob=args.verify_clob,
                )

            print_scan_summary(summary, show_unverified=args.show_unverified)

            # Optionally execute verified signals
            if args.execute and summary.signals:
                for sig in summary.signals:
                    if not sig.clob_verified:
                        continue
                    logger.info(
                        "Executing signal: %s — %s", sig.scan_id, sig.summary_line()
                    )
                    notes = execute_signal(
                        sig,
                        dry_run=args.dry_run,
                        max_position_usd=args.max_position,
                        fee_poly=args.fee_poly,
                        fee_kalshi=args.fee_kalshi,
                        size_contracts=args.size_contracts,
                    )
                    sig.executed = True
                    sig.execution_notes = notes
                    for n in notes:
                        logger.info("  exec: %s", n)

            # Write all signals (verified + unverified) to file for audit trail
            # Only verified signals should be acted on, but we log everything
            if summary.signals:
                append_signals(summary.signals, args.signals_file)
                logger.info(
                    "Wrote %d signal(s) to %s (%d verified)",
                    len(summary.signals), args.signals_file, summary.verified_signals,
                )

            total_verified += summary.verified_signals

        except Exception as exc:
            logger.error("Scan #%d uncaught error: %s", scan_count, exc)
            logger.debug(traceback.format_exc())

        if args.once or not _running[0]:
            break

        logger.info(
            "Scan #%d complete — sleeping %ds  (total verified signals: %d)",
            scan_count, args.interval, total_verified,
        )
        # Sleep in 1-second ticks so SIGINT is caught promptly
        for _ in range(args.interval):
            if not _running[0]:
                break
            time.sleep(1)

    logger.info(
        "Monitor stopped — %d scans completed, %d total verified signals.",
        scan_count, total_verified,
    )


if __name__ == "__main__":
    main()

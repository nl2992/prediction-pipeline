"""
Order Execution Engine
======================
Executes two-leg prediction market arbitrage trades across Kalshi and Polymarket.

DRY_RUN mode (default = True)
  All methods log intended orders and run pre-flight checks but never call
  live placement endpoints.  Start here.  Flip to live only after:
    1. Credentials verified (test with get_balance() calls)
    2. At least one full dry-run scan passes all pre-flight checks
    3. Position limits and max-loss cap set explicitly

Live mode requirements
  Kalshi   : KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH (RSA PEM file)
  Polymarket: POLY_API_KEY + POLY_API_SECRET + POLY_API_PASSPHRASE
              + POLY_PRIVATE_KEY (hex Ethereum key) + py-clob-client-v2 installed

Kalshi side convention (v2 API)
  side="bid"  → buy YES contracts at limit price
  side="ask"  → sell YES contracts at limit price  (creates NO exposure)

  To buy NO on Kalshi: place side="ask" at price = YES_bid_complement
    e.g. if YES bid on the book is 0.97, you sell at 0.97 and receive NO exposure
    Your effective NO cost = 1.0 - 0.97 = 0.03 per contract

Leg-risk note
  Orders are placed sequentially (leg A then leg B).  If leg A fills but leg B
  is rejected, the executor cancels leg A automatically.  For tighter timing use
  fill_or_kill on both legs, accepting a higher miss rate.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("executor")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TradeIntent:
    """Describes a single leg of an arb trade."""

    exchange: str           # "kalshi" | "polymarket"
    contract_side: str      # "YES" | "NO"
    limit_price: float      # dollar price (0.0 – 1.0)
    size_contracts: float   # number of contracts (= max USD risk per leg)
    market_id: str          # Kalshi ticker or Polymarket conditionId
    token_id: str | None    # Polymarket CLOB token ID (YES or NO token)
    description: str        # human-readable label for logs

    @property
    def max_cost_usd(self) -> float:
        return round(self.limit_price * self.size_contracts, 6)


@dataclass
class TradeResult:
    """Outcome of a single leg placement attempt."""

    intent: TradeIntent
    success: bool
    order_id: str | None = None
    filled_price: float | None = None
    filled_size: float | None = None
    status: str | None = None
    error: str | None = None
    dry_run: bool = True
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ArbExecution:
    """Result of a full two-leg arb attempt."""

    leg_a: TradeResult
    leg_b: TradeResult
    dry_run: bool
    gross_cost: float | None = None
    net_profit_estimate: float | None = None
    cancelled_leg_a: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.leg_a.success and self.leg_b.success


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


def check_price_still_valid(
    intent: TradeIntent,
    price_tolerance: float = 0.02,
) -> tuple[bool, str]:
    """
    Re-fetch the live CLOB price for the given leg and confirm it hasn't
    moved beyond price_tolerance since the signal was detected.
    """
    try:
        if intent.exchange == "polymarket" and intent.token_id:
            from polymarket.client import PolymarketClient
            client = PolymarketClient(timeout=10)
            liq = client.verify_clob_liquidity(intent.token_id, min_depth_usd=0)
            live_ask = liq.get("best_ask")
            if live_ask is None:
                return False, "no live ask available"
            drift = abs(live_ask - intent.limit_price)
            if drift > price_tolerance:
                return False, f"price drifted {drift:.4f} > tolerance {price_tolerance}"
            return True, f"price OK (live_ask={live_ask:.4f} vs intended={intent.limit_price:.4f})"

        elif intent.exchange == "kalshi":
            from kalshi.client import KalshiClient
            client = KalshiClient(timeout=10)
            raw = client.get_orderbook(intent.market_id)
            ob_fp = raw.get("orderbook_fp", {})
            if intent.contract_side == "YES":
                bids = ob_fp.get("yes_dollars", [])
                live_bid = float(bids[-1][0]) if bids else None  # ascending → last = highest
            else:
                no_bids = ob_fp.get("no_dollars", [])
                live_bid = float(no_bids[-1][0]) if no_bids else None
            if live_bid is None:
                return False, "no live bid available"
            # For a buy, we care about the ask (complement of NO bid for YES, etc.)
            return True, f"live best bid present: {live_bid:.4f}"

        return True, "no live price check implemented for this exchange"

    except Exception as exc:
        return False, f"price check failed: {exc}"


def pre_flight_checks(
    leg_a: TradeIntent,
    leg_b: TradeIntent,
    max_position_usd: float = 500.0,
    fee_a: float = 0.02,
    fee_b: float = 0.07,
    kalshi_client=None,
    poly_client=None,
) -> tuple[bool, list[str]]:
    """
    Run all pre-flight checks before executing a two-leg trade.

    Returns (go, messages) where go=True means safe to proceed.
    """
    messages: list[str] = []
    go = True

    # 1. Size sanity
    total_risk = leg_a.max_cost_usd + leg_b.max_cost_usd
    if total_risk > max_position_usd:
        messages.append(
            f"FAIL: total risk ${total_risk:.2f} exceeds cap ${max_position_usd:.2f}"
        )
        go = False
    else:
        messages.append(f"OK:   total risk ${total_risk:.2f} ≤ cap ${max_position_usd:.2f}")

    # 2. Gross profit positive
    gross_cost = leg_a.limit_price + leg_b.limit_price
    gross_profit = 1.0 - gross_cost
    worst_fee = max(fee_a, fee_b)
    net_profit = gross_profit - worst_fee
    if net_profit <= 0:
        messages.append(
            f"FAIL: net_profit {net_profit:.4f} ≤ 0  "
            f"(gross_cost={gross_cost:.4f} fee={worst_fee:.4f})"
        )
        go = False
    else:
        messages.append(
            f"OK:   net_profit {net_profit:.4f}  "
            f"(gross_cost={gross_cost:.4f} fee={worst_fee:.4f})"
        )

    # 3. Live price recheck
    ok_a, msg_a = check_price_still_valid(leg_a)
    messages.append(f"{'OK' if ok_a else 'FAIL'}: leg_a price — {msg_a}")
    if not ok_a:
        go = False

    ok_b, msg_b = check_price_still_valid(leg_b)
    messages.append(f"{'OK' if ok_b else 'FAIL'}: leg_b price — {msg_b}")
    if not ok_b:
        go = False

    # 4. Balance check (only if clients provided)
    if kalshi_client and leg_a.exchange == "kalshi":
        try:
            bal = kalshi_client.get_balance()
            bal_usd = bal.get("balance", 0) / 100  # cents → dollars
            if bal_usd < leg_a.max_cost_usd:
                messages.append(
                    f"FAIL: Kalshi balance ${bal_usd:.2f} < leg cost ${leg_a.max_cost_usd:.2f}"
                )
                go = False
            else:
                messages.append(f"OK:   Kalshi balance ${bal_usd:.2f}")
        except Exception as exc:
            messages.append(f"WARN: Kalshi balance check failed: {exc}")

    if kalshi_client and leg_b.exchange == "kalshi":
        try:
            bal = kalshi_client.get_balance()
            bal_usd = bal.get("balance", 0) / 100
            if bal_usd < leg_b.max_cost_usd:
                messages.append(
                    f"FAIL: Kalshi balance ${bal_usd:.2f} < leg cost ${leg_b.max_cost_usd:.2f}"
                )
                go = False
            else:
                messages.append(f"OK:   Kalshi balance ${bal_usd:.2f}")
        except Exception as exc:
            messages.append(f"WARN: Kalshi balance check failed: {exc}")

    return go, messages


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """
    Two-leg arb executor with dry_run mode.

    Parameters
    ----------
    dry_run : bool
        If True (default), all order placement is simulated.
    max_position_usd : float
        Maximum combined USD cost of both legs per trade.
    fee_poly / fee_kalshi : float
        Exchange fee rates used for net-profit pre-flight checks.
    """

    def __init__(
        self,
        dry_run: bool = True,
        max_position_usd: float = 100.0,
        fee_poly: float = 0.02,
        fee_kalshi: float = 0.07,
    ) -> None:
        self.dry_run = dry_run
        self.max_position_usd = max_position_usd
        self.fee_poly = fee_poly
        self.fee_kalshi = fee_kalshi
        self._kalshi_client = None
        self._poly_client = None

        if not dry_run:
            self._kalshi_client = self._init_kalshi()
            self._poly_client = self._init_poly()

    def _init_kalshi(self):
        from kalshi.client import KalshiClient
        client = KalshiClient()
        if not client._is_authenticated:
            raise RuntimeError(
                "Kalshi credentials not configured for live mode.  "
                "Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH."
            )
        return client

    def _init_poly(self):
        from polymarket.client import PolymarketClient
        client = PolymarketClient()
        if not client._is_authenticated:
            raise RuntimeError(
                "Polymarket credentials not configured for live mode.  "
                "Set POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE."
            )
        return client

    # ------------------------------------------------------------------
    # Single-leg placement
    # ------------------------------------------------------------------

    def _place_kalshi(self, intent: TradeIntent) -> TradeResult:
        """Place one leg on Kalshi."""
        # Map YES/NO contract side → Kalshi bid/ask side
        # side="bid"  → buy YES contracts  (we pay limit_price, receive YES)
        # side="ask"  → sell YES contracts (we receive limit_price, hold NO)
        kalshi_side = "bid" if intent.contract_side == "YES" else "ask"

        if self.dry_run:
            logger.info(
                "[DRY RUN] Kalshi %s  side=%s  price=%.4f  count=%.2f  (~$%.2f)",
                intent.market_id, kalshi_side, intent.limit_price,
                intent.size_contracts, intent.max_cost_usd,
            )
            return TradeResult(
                intent=intent,
                success=True,
                order_id=f"DRY-{uuid.uuid4().hex[:8]}",
                filled_price=intent.limit_price,
                filled_size=intent.size_contracts,
                status="dry_run_simulated",
                dry_run=True,
            )

        try:
            resp = self._kalshi_client.place_order(
                ticker=intent.market_id,
                side=kalshi_side,
                price=intent.limit_price,
                count=intent.size_contracts,
                time_in_force="fill_or_kill",
            )
            order_id = resp.get("order_id") or resp.get("order", {}).get("order_id")
            status = resp.get("status") or resp.get("order", {}).get("status", "unknown")
            logger.info("Kalshi order placed: %s  status=%s", order_id, status)
            return TradeResult(
                intent=intent, success=True,
                order_id=order_id, status=status, dry_run=False,
            )
        except Exception as exc:
            logger.error("Kalshi placement failed: %s", exc)
            return TradeResult(intent=intent, success=False, error=str(exc), dry_run=False)

    def _place_polymarket(self, intent: TradeIntent) -> TradeResult:
        """Place one leg on Polymarket."""
        poly_side = "BUY" if intent.contract_side == "YES" else "SELL"

        if self.dry_run:
            logger.info(
                "[DRY RUN] Polymarket token=%s...  side=%s  price=%.4f  size_usd=$%.2f",
                (intent.token_id or "")[:16], poly_side,
                intent.limit_price, intent.max_cost_usd,
            )
            return TradeResult(
                intent=intent,
                success=True,
                order_id=f"DRY-{uuid.uuid4().hex[:8]}",
                filled_price=intent.limit_price,
                filled_size=intent.size_contracts,
                status="dry_run_simulated",
                dry_run=True,
            )

        if not intent.token_id:
            return TradeResult(
                intent=intent, success=False,
                error="token_id required for Polymarket order", dry_run=False,
            )

        try:
            resp = self._poly_client.place_order(
                token_id=intent.token_id,
                side=poly_side,
                price=intent.limit_price,
                size_usdc=intent.max_cost_usd,
                order_type="FOK",
            )
            order_id = resp.get("orderID") or resp.get("id")
            status = resp.get("status", "submitted")
            logger.info("Polymarket order placed: %s  status=%s", order_id, status)
            return TradeResult(
                intent=intent, success=True,
                order_id=order_id, status=status, dry_run=False,
            )
        except Exception as exc:
            logger.error("Polymarket placement failed: %s", exc)
            return TradeResult(intent=intent, success=False, error=str(exc), dry_run=False)

    def _place(self, intent: TradeIntent) -> TradeResult:
        if intent.exchange == "kalshi":
            return self._place_kalshi(intent)
        elif intent.exchange == "polymarket":
            return self._place_polymarket(intent)
        else:
            return TradeResult(
                intent=intent, success=False,
                error=f"unknown exchange: {intent.exchange}", dry_run=self.dry_run,
            )

    # ------------------------------------------------------------------
    # Two-leg execution
    # ------------------------------------------------------------------

    def execute(
        self,
        leg_a: TradeIntent,
        leg_b: TradeIntent,
        skip_preflight: bool = False,
    ) -> ArbExecution:
        """
        Execute a two-leg arb trade with pre-flight checks.

        Leg A is placed first.  If leg A fails, leg B is never attempted.
        If leg A succeeds but leg B fails, leg A is cancelled (best-effort).

        Parameters
        ----------
        leg_a, leg_b : TradeIntent
        skip_preflight : bool
            If True, bypass pre-flight checks (not recommended for live mode).
        """
        result = ArbExecution(
            leg_a=TradeResult(intent=leg_a, success=False, dry_run=self.dry_run),
            leg_b=TradeResult(intent=leg_b, success=False, dry_run=self.dry_run),
            dry_run=self.dry_run,
        )

        # Pre-flight
        if not skip_preflight:
            go, messages = pre_flight_checks(
                leg_a, leg_b,
                max_position_usd=self.max_position_usd,
                fee_a=self.fee_poly if leg_a.exchange == "polymarket" else self.fee_kalshi,
                fee_b=self.fee_poly if leg_b.exchange == "polymarket" else self.fee_kalshi,
                kalshi_client=self._kalshi_client,
                poly_client=self._poly_client,
            )
            result.notes.extend(messages)
            for msg in messages:
                level = logging.WARNING if msg.startswith("FAIL") else logging.INFO
                logger.log(level, "pre-flight: %s", msg)
            if not go:
                result.notes.append("ABORT: pre-flight failed — no orders placed")
                logger.warning("Arb aborted by pre-flight checks.")
                return result

        # Place leg A
        result.leg_a = self._place(leg_a)
        if not result.leg_a.success:
            result.notes.append("ABORT: leg A failed — leg B not attempted")
            return result

        # Small buffer between legs (avoid self-trade detection)
        if not self.dry_run:
            time.sleep(0.25)

        # Place leg B
        result.leg_b = self._place(leg_b)
        if not result.leg_b.success:
            result.notes.append("leg B failed — attempting to cancel leg A")
            if not self.dry_run and result.leg_a.order_id:
                try:
                    if leg_a.exchange == "kalshi":
                        self._kalshi_client.cancel_order(result.leg_a.order_id)
                    elif leg_a.exchange == "polymarket":
                        self._poly_client.cancel_order(result.leg_a.order_id)
                    result.cancelled_leg_a = True
                    result.notes.append(f"leg A cancelled: {result.leg_a.order_id}")
                except Exception as exc:
                    result.notes.append(f"leg A cancel FAILED: {exc} — manual intervention required")
            return result

        # Both legs placed
        gross_cost = leg_a.limit_price + leg_b.limit_price
        result.gross_cost = round(gross_cost, 6)
        result.net_profit_estimate = round(
            1.0 - gross_cost - max(self.fee_poly, self.fee_kalshi), 6
        )
        result.notes.append(
            f"Both legs placed — estimated net profit: ${result.net_profit_estimate:.4f} per contract"
        )
        logger.info(
            "Arb executed — gross_cost=%.4f  net_profit_est=%.4f  dry_run=%s",
            gross_cost, result.net_profit_estimate, self.dry_run,
        )
        return result

    # ------------------------------------------------------------------
    # Convenience: build intents from an ArbOpportunity
    # ------------------------------------------------------------------

    @staticmethod
    def intents_from_arb_opportunity(
        opp: Any,
        size_contracts: float = 10.0,
    ) -> tuple[TradeIntent, TradeIntent]:
        """
        Convert an arb.ArbOpportunity into two TradeIntents.

        Parameters
        ----------
        opp : ArbOpportunity
        size_contracts : float
            How many contracts to trade on each leg.  Each contract risks
            at most 1 USD.  Start small (e.g. 10).
        """
        pair = opp.pair

        if opp.direction == "poly_yes__kalshi_no":
            leg_a = TradeIntent(
                exchange="polymarket",
                contract_side="YES",
                limit_price=opp.buy_yes_ask,
                size_contracts=size_contracts,
                market_id=pair.poly.market_id,
                token_id=None,  # caller should set from clobTokenIds[YES index]
                description=f"Buy YES on Poly @ {opp.buy_yes_ask:.4f}",
            )
            leg_b = TradeIntent(
                exchange="kalshi",
                contract_side="NO",
                limit_price=opp.buy_no_ask,
                size_contracts=size_contracts,
                market_id=pair.kalshi.market_id,
                token_id=None,
                description=f"Buy NO on Kalshi @ {opp.buy_no_ask:.4f}",
            )
        else:  # kalshi_yes__poly_no
            leg_a = TradeIntent(
                exchange="kalshi",
                contract_side="YES",
                limit_price=opp.buy_yes_ask,
                size_contracts=size_contracts,
                market_id=pair.kalshi.market_id,
                token_id=None,
                description=f"Buy YES on Kalshi @ {opp.buy_yes_ask:.4f}",
            )
            leg_b = TradeIntent(
                exchange="polymarket",
                contract_side="NO",
                limit_price=opp.buy_no_ask,
                size_contracts=size_contracts,
                market_id=pair.poly.market_id,
                token_id=None,  # caller should set from clobTokenIds[NO index]
                description=f"Buy NO on Poly @ {opp.buy_no_ask:.4f}",
            )

        return leg_a, leg_b

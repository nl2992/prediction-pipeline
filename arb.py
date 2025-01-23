"""
Arbitrage detection for matched prediction market pairs.

For each MatchedPair from matcher.match_markets(), this module checks two
directions of two-leg binary arbitrage:

  Direction A — buy YES on Polymarket, buy NO on Kalshi:
    cost = poly_yes_ask + (1 - kalshi_yes_bid)
    profit = 1.0 - cost - fee   (risk-free: one leg always pays $1)

  Direction B — buy YES on Kalshi, buy NO on Polymarket:
    cost = kalshi_yes_ask + (1 - poly_yes_bid)
    profit = 1.0 - cost - fee

A profit > 0 after fees is a riskless arb: regardless of outcome, one leg
settles at $1 and the other at $0, for a guaranteed net gain.

Fee model
---------
The winning leg (the $1 payout) is charged a fraction of winnings.
For a conservative estimate, ``winning_leg_fee = max(fee_poly, fee_kalshi)``
is applied regardless of which exchange wins.  Pass explicit fee rates to
``find_arb()`` to customise.

Defaults:
  fee_poly   = 0.02   (Polymarket: ~2% of profits)
  fee_kalshi = 0.07   (Kalshi: ~7% of profits; varies by market tier)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matcher import MatchedPair

FEE_POLY_DEFAULT: float = 0.02
FEE_KALSHI_DEFAULT: float = 0.07


@dataclass
class ArbOpportunity:
    """A two-leg arbitrage opportunity across Polymarket and Kalshi."""

    pair: "MatchedPair"
    direction: str               # "poly_yes__kalshi_no" | "kalshi_yes__poly_no"

    buy_yes_exchange: str        # exchange where we buy YES
    buy_yes_ask: float           # YES ask price on that exchange
    buy_yes_size: float | None   # liquidity at best YES ask

    buy_no_exchange: str         # exchange where we buy NO
    buy_no_ask: float            # NO ask price (= 1 - other_yes_bid)
    buy_no_size: float | None    # liquidity at implied NO ask

    gross_cost: float            # buy_yes_ask + buy_no_ask
    gross_profit: float          # 1.0 - gross_cost
    winning_leg_fee: float       # fee applied to the $1 winning payout
    net_profit: float            # gross_profit - winning_leg_fee
    net_profit_pct: float        # net_profit / gross_cost × 100
    max_size: float | None       # min(buy_yes_size, buy_no_size)

    @property
    def is_profitable(self) -> bool:
        return self.net_profit > 0


def find_arb(
    pairs: list["MatchedPair"],
    fee_poly: float = FEE_POLY_DEFAULT,
    fee_kalshi: float = FEE_KALSHI_DEFAULT,
    min_net_profit_pct: float = 0.0,
) -> list[ArbOpportunity]:
    """
    Scan matched pairs for two-leg arbitrage opportunities.

    Parameters
    ----------
    pairs
        Output from ``matcher.match_markets()``.
    fee_poly
        Polymarket fee as a fraction of the $1 payout (default 0.02).
    fee_kalshi
        Kalshi fee as a fraction of the $1 payout (default 0.07).
    min_net_profit_pct
        Minimum net profit as a percentage of gross cost.
        Use 0.0 to include all break-even-or-better arbs.

    Returns
    -------
    list of ArbOpportunity sorted by net_profit descending.
    """
    opps: list[ArbOpportunity] = []
    # Worst-case fee: whichever exchange pays out charges the higher rate.
    worst_fee = max(fee_poly, fee_kalshi)

    for pair in pairs:
        p_ob = pair.poly.orderbook
        k_ob = pair.kalshi.orderbook

        p_ask = p_ob.best_ask
        p_bid = p_ob.best_bid
        k_ask = k_ob.best_ask
        k_bid = k_ob.best_bid

        # --- Direction A: buy YES on Poly, buy NO on Kalshi ---
        # "Buy NO on Kalshi" = sell YES on Kalshi = the other side of the YES bid.
        # No-ask on Kalshi is implied: no_ask = 1 - yes_bid.
        if p_ask is not None and k_bid is not None:
            no_ask_kalshi = round(1.0 - k_bid, 6)
            gross_cost = round(p_ask + no_ask_kalshi, 6)
            gross_profit = round(1.0 - gross_cost, 6)
            net_profit = round(gross_profit - worst_fee, 6)
            net_profit_pct = round(net_profit / gross_cost * 100, 4) if gross_cost > 0 else 0.0

            yes_size = p_ob.asks[0].size if p_ob.asks else None
            no_size = k_ob.bids[0].size if k_ob.bids else None
            max_sz = _min_size(yes_size, no_size)

            if net_profit_pct >= min_net_profit_pct:
                opps.append(ArbOpportunity(
                    pair=pair,
                    direction="poly_yes__kalshi_no",
                    buy_yes_exchange="polymarket",
                    buy_yes_ask=p_ask,
                    buy_yes_size=yes_size,
                    buy_no_exchange="kalshi",
                    buy_no_ask=no_ask_kalshi,
                    buy_no_size=no_size,
                    gross_cost=gross_cost,
                    gross_profit=gross_profit,
                    winning_leg_fee=worst_fee,
                    net_profit=net_profit,
                    net_profit_pct=net_profit_pct,
                    max_size=max_sz,
                ))

        # --- Direction B: buy YES on Kalshi, buy NO on Poly ---
        if k_ask is not None and p_bid is not None:
            no_ask_poly = round(1.0 - p_bid, 6)
            gross_cost = round(k_ask + no_ask_poly, 6)
            gross_profit = round(1.0 - gross_cost, 6)
            net_profit = round(gross_profit - worst_fee, 6)
            net_profit_pct = round(net_profit / gross_cost * 100, 4) if gross_cost > 0 else 0.0

            yes_size = k_ob.asks[0].size if k_ob.asks else None
            no_size = p_ob.bids[0].size if p_ob.bids else None
            max_sz = _min_size(yes_size, no_size)

            if net_profit_pct >= min_net_profit_pct:
                opps.append(ArbOpportunity(
                    pair=pair,
                    direction="kalshi_yes__poly_no",
                    buy_yes_exchange="kalshi",
                    buy_yes_ask=k_ask,
                    buy_yes_size=yes_size,
                    buy_no_exchange="polymarket",
                    buy_no_ask=no_ask_poly,
                    buy_no_size=no_size,
                    gross_cost=gross_cost,
                    gross_profit=gross_profit,
                    winning_leg_fee=worst_fee,
                    net_profit=net_profit,
                    net_profit_pct=net_profit_pct,
                    max_size=max_sz,
                ))

    opps.sort(key=lambda x: x.net_profit, reverse=True)
    return opps


def _min_size(a: float | None, b: float | None) -> float | None:
    if a is None and b is None:
        return None
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)

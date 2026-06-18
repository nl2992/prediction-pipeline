"""Executable arbitrage against the REAL order book — depth, VWAP, profit-by-budget.

The alerter's net edge uses only top-of-book, which overstates what you can
actually fill. This module walks the full bid/ask ladders to answer the questions
the operator wants in the email:

  * How many contract-pairs are ACTUALLY arbable (profitable after fees) given the
    live depth?
  * Walking the book, what is the VWAP (volume-weighted average price) per leg?
  * For a fixed budget PER MARKET ($1000 / $2000 / $2500 / $5000), how many
    contracts fill, at what VWAP, for what total profit / ROI?

Everything here is derived strictly from the OrderBook ladders, so it matches the
displayed book. Stdlib-only.

Conventions (both books expressed YES-side, as pipeline.OrderBook):
  buy YES → take asks (ascending price), pay ask price per share.
  buy NO  → take YES bids (best bid first); NO price = 1 − yes_bid, size = bid size.

Directions:
  "poly_yes__kalshi_no": buy PM YES (PM asks) + buy Kalshi NO (Kalshi YES bids).
  "kalshi_yes__poly_no": buy Kalshi YES (Kalshi asks) + buy PM NO (PM YES bids).
"""
from __future__ import annotations

from dataclasses import dataclass

from arb import kalshi_taker_fee


@dataclass
class Fill:
    contracts: float          # contract-pairs filled
    vwap_a: float             # volume-weighted avg price, leg A (per contract)
    vwap_b: float             # volume-weighted avg price, leg B
    cost_a: float             # $ deployed in market A
    cost_b: float             # $ deployed in market B
    profit: float             # $ net profit after the Kalshi taker fee
    roi: float                # profit / (cost_a + cost_b)


def build_buy_ladder(book, side: str) -> list[tuple[float, float]]:
    """(unit_cost, size) levels for BUYING ``side`` ('yes'|'no'), cheapest first."""
    if side == "yes":
        lv = [(l.price, l.size) for l in (book.asks or [])
              if l.price is not None and l.size and l.size > 0]
    else:  # buy NO from YES bids: no_price = 1 - yes_bid_price
        lv = [(round(1.0 - l.price, 6), l.size) for l in (book.bids or [])
              if l.price is not None and l.size and l.size > 0]
    return sorted(lv, key=lambda x: x[0])


def _fill(legA: list[tuple[float, float]], legB: list[tuple[float, float]],
          kalshi_leg: str, budget: float | None) -> Fill:
    """Walk both ladders in lockstep, pairing one contract from each leg.

    Includes a contract only while the pair is profitable after the Kalshi taker
    fee; respects a per-market ``budget`` cap on each leg (None = unbounded depth).
    """
    ia = ib = 0
    ra = legA[ia][1] if legA else 0.0
    rb = legB[ib][1] if legB else 0.0
    contracts = cost_a = cost_b = profit = 0.0

    while ia < len(legA) and ib < len(legB):
        pa, pb = legA[ia][0], legB[ib][0]
        k_price = pa if kalshi_leg == "A" else pb
        fee = kalshi_taker_fee(k_price)
        pair_profit = 1.0 - pa - pb - fee
        if pair_profit <= 0:                      # no longer arbable at this depth
            break

        q = min(ra, rb)
        if budget is not None:                    # cap deployment per market
            if pa > 0:
                q = min(q, (budget - cost_a) / pa)
            if pb > 0:
                q = min(q, (budget - cost_b) / pb)
        if q <= 1e-9:
            break

        contracts += q
        cost_a += q * pa
        cost_b += q * pb
        profit += q * pair_profit
        ra -= q
        rb -= q
        if ra <= 1e-9:
            ia += 1
            ra = legA[ia][1] if ia < len(legA) else 0.0
        if rb <= 1e-9:
            ib += 1
            rb = legB[ib][1] if ib < len(legB) else 0.0

    return Fill(
        contracts=round(contracts, 4),
        vwap_a=round(cost_a / contracts, 6) if contracts else 0.0,
        vwap_b=round(cost_b / contracts, 6) if contracts else 0.0,
        cost_a=round(cost_a, 2),
        cost_b=round(cost_b, 2),
        profit=round(profit, 4),
        roi=round(profit / (cost_a + cost_b), 6) if (cost_a + cost_b) else 0.0,
    )


def cumulative_curve(legA: list[tuple[float, float]], legB: list[tuple[float, float]],
                     kalshi_leg: str) -> list[tuple[float, float, float, float]]:
    """Walk the full overlapping depth (ignoring profitability) and record, at each
    chunk boundary, ``(cum_contracts, price_a, price_b, combined_cost_with_fee)``.

    Used to chart how the per-contract cost rises with depth and where it crosses
    break-even (1.0) — i.e. exactly how much is arbable.
    """
    ia = ib = 0
    ra = legA[ia][1] if legA else 0.0
    rb = legB[ib][1] if legB else 0.0
    cum = 0.0
    out: list[tuple[float, float, float, float]] = []
    while ia < len(legA) and ib < len(legB):
        pa, pb = legA[ia][0], legB[ib][0]
        fee = kalshi_taker_fee(pa if kalshi_leg == "A" else pb)
        q = min(ra, rb)
        if q <= 1e-9:
            break
        cum += q
        out.append((round(cum, 4), pa, pb, round(pa + pb + fee, 6)))
        ra -= q
        rb -= q
        if ra <= 1e-9:
            ia += 1
            ra = legA[ia][1] if ia < len(legA) else 0.0
        if rb <= 1e-9:
            ib += 1
            rb = legB[ib][1] if ib < len(legB) else 0.0
    return out


# Direction → (leg A side, leg B side, which leg is the Kalshi leg)
_DIRECTIONS = {
    "poly_yes__kalshi_no": ("yes", "no", "B"),   # A=PM YES, B=Kalshi NO
    "kalshi_yes__poly_no": ("yes", "no", "A"),   # A=Kalshi YES, B=PM NO
}


def executable_arb(poly_book, kalshi_book, direction: str,
                   budgets: tuple[float, ...] = (1000, 2000, 2500, 5000)) -> dict:
    """Full executable picture for one direction against the live books.

    Returns ``{"max": Fill, "by_budget": {budget: Fill}, "ladders": (legA, legB)}``
    where ``max`` is the entire profitable depth (unbounded budget) and each
    ``by_budget`` entry caps deployment at that many dollars PER MARKET.
    """
    side_a, side_b, kalshi_leg = _DIRECTIONS[direction]
    if direction == "poly_yes__kalshi_no":
        legA = build_buy_ladder(poly_book, side_a)     # PM YES
        legB = build_buy_ladder(kalshi_book, side_b)   # Kalshi NO
    else:
        legA = build_buy_ladder(kalshi_book, side_a)   # Kalshi YES
        legB = build_buy_ladder(poly_book, side_b)     # PM NO

    return {
        "max": _fill(legA, legB, kalshi_leg, None),
        "by_budget": {b: _fill(legA, legB, kalshi_leg, b) for b in budgets},
        "ladders": (legA, legB),
    }

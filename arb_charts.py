"""Professional per-arb charts for the alert email, derived from the live book.

Two panels per pair:
  1. Depth — per-contract cost of each leg (PM and Kalshi) and the combined cost,
     vs cumulative contracts, with the break-even line and the ARBABLE region
     shaded. Shows exactly how much is arbable before the edge disappears.
  2. Profit by budget — net $ profit (after the real Kalshi fee) for a per-market
     investment of $1000 / $2000 / $2500 / $5000, with the contracts filled.

Everything is computed by book_arb from the order-book ladders, so it matches the
displayed book. Chart generation degrades gracefully: any failure returns None so
the email still sends (text + table).
"""
from __future__ import annotations

import io

from pipeline import OrderBook, PriceLevel
from book_arb import executable_arb, cumulative_curve, _DIRECTIONS

BUDGETS = (1000, 2000, 2500, 5000)

_DIR_FROM_TEXT = {
    "buy YES on Polymarket + buy NO on Kalshi": "poly_yes__kalshi_no",
    "buy YES on Kalshi + buy NO on Polymarket": "kalshi_yes__poly_no",
}


def _level(lvl) -> PriceLevel:
    # Tolerate book levels that carry extra fields beyond [price, size] (some
    # multi-outcome markets append e.g. an order count) — take the first two.
    return PriceLevel(lvl[0], lvl[1])


def _ob(d: dict | None) -> OrderBook:
    d = d or {}
    return OrderBook(
        bids=[_level(l) for l in d.get("bids", [])],
        asks=[_level(l) for l in d.get("asks", [])],
    )


def executable_summary(signal: dict, budgets: tuple[float, ...] = BUDGETS):
    """Return (direction_key, executable_arb_result) or None if books are absent."""
    direction = _DIR_FROM_TEXT.get(signal.get("direction"))
    pb, kb = signal.get("poly_book"), signal.get("kalshi_book")
    if not direction or not pb or not kb:
        return None
    return direction, executable_arb(_ob(pb), _ob(kb), direction, budgets)


def make_arb_chart(signal: dict, budgets: tuple[float, ...] = BUDGETS) -> bytes | None:
    """Render the 2-panel PNG for one signal; None on any failure (degrade safe)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        summ = executable_summary(signal, budgets)
        if not summ:
            return None
        direction, res = summ
        legA, legB = res["ladders"]
        kalshi_leg = _DIRECTIONS[direction][2]
        curve = cumulative_curve(legA, legB, kalshi_leg)
        if not curve:
            return None

        c_bar, c_edge, c_fill, c_zero = "#16a34a", "#111827", "#bbf7d0", "#dc2626"

        # Right panel = net edge PER PAIR (cents) as you buy deeper; it shrinks as
        # you take worse prices and hits 0 at the max profitable depth. Clamp the
        # x-axis to the largest realistic budget tier ($5k/market) so an illiquid
        # market's thin/stale deep book can't stretch it to ~1e6 contracts.
        cap = res["by_budget"][max(budgets)].contracts or res["max"].contracts
        view = min(curve[-1][0], cap) if cap else curve[-1][0]
        xs = [0.0]
        edge = []  # net edge per pair, in cents, at each depth chunk
        for c0, _c1, _c2, c3 in curve:
            edge.append((1.0 - c3) * 100.0)
            xs.append(min(c0, view))
            if c0 >= view:
                break
        edge.append(edge[-1])  # match xs length for where='post'
        max_arb = min(res["max"].contracts, view) if view else res["max"].contracts

        fig, (axL, axR) = plt.subplots(1, 2, figsize=(9.6, 3.5))

        # ---- Panel 1: net profit by stake per market (the money question) ----
        buds = list(budgets)
        profits = [res["by_budget"][b].profit for b in buds]
        conts = [res["by_budget"][b].contracts for b in buds]
        bars = axL.bar([f"${b/1000:g}k" for b in buds], profits, color=c_bar, alpha=0.9)
        for bar, p, c in zip(bars, profits, conts):
            axL.annotate(f"${p:,.0f}\n{c:,.0f} pairs", xy=(bar.get_x() + bar.get_width() / 2,
                         bar.get_height()), xytext=(0, 2), textcoords="offset points",
                         ha="center", va="bottom", fontsize=7.5)
        axL.set_xlabel("amount staked per market", fontsize=9)
        axL.set_ylabel("net profit after fees ($)", fontsize=9)
        axL.set_title("How much you make, by stake", fontsize=10, fontweight="bold")
        axL.grid(True, axis="y", alpha=0.25)
        axL.tick_params(labelsize=8)
        if profits and max(profits) > 0:
            axL.set_ylim(0, max(profits) * 1.28)

        # ---- Panel 2: edge per pair vs how many you buy ----
        axL2 = axR
        axL2.step(xs, edge, where="post", color=c_edge, lw=2.0)
        axL2.axhline(0.0, color=c_zero, ls="--", lw=1.2, label="break-even")
        axL2.fill_between(xs, edge, 0.0, where=[e > 0 for e in edge], step="post",
                          color=c_fill, alpha=0.85, zorder=0)
        if max_arb > 0:
            axL2.axvline(max_arb, color=c_bar, ls=":", lw=1.3)
            axL2.annotate(f"{max_arb:,.0f} pairs\nprofitable", xy=(max_arb, max(edge) * 0.5 if edge else 1),
                          xytext=(4, 0), textcoords="offset points", fontsize=8,
                          color=c_bar, va="center")
        if view and view > 0:
            axL2.set_xlim(0, view * 1.05)
        axL2.set_xlabel("contract-pairs bought (depth)", fontsize=9)
        axL2.set_ylabel("net edge per pair (¢ / $1)", fontsize=9)
        axL2.set_title("How deep the edge lasts", fontsize=10, fontweight="bold")
        axL2.grid(True, alpha=0.25)
        axL2.tick_params(labelsize=8)

        edge0 = (1.0 - curve[0][3]) * 100.0
        fig.suptitle(f"{(signal.get('poly_title') or '')[:42]}  ↔  {(signal.get('kalshi_title') or '')[:42]}"
                     f"   ·   {edge0:.1f}¢/$1 net edge at best price", fontsize=8.5)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        return None

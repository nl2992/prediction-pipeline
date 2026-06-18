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


def _ob(d: dict | None) -> OrderBook:
    d = d or {}
    return OrderBook(
        bids=[PriceLevel(p, s) for p, s in d.get("bids", [])],
        asks=[PriceLevel(p, s) for p, s in d.get("asks", [])],
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

        pm_first = direction == "poly_yes__kalshi_no"
        a_label = "PM YES" if pm_first else "Kalshi YES"
        b_label = "Kalshi NO" if pm_first else "PM NO"
        c_pm, c_k, c_comb, c_be, c_fill = "#2563eb", "#d97706", "#111827", "#dc2626", "#bbf7d0"

        xs = [c[0] for c in curve]
        pa = [c[1] for c in curve]
        pb = [c[2] for c in curve]
        comb = [c[3] for c in curve]
        max_arb = res["max"].contracts

        fig, (axL, axR) = plt.subplots(1, 2, figsize=(9.2, 3.4))

        # ---- Panel 1: depth / arbable ----
        axL.step(xs, pa, where="post", color=c_pm, lw=1.6, label=f"{a_label} cost")
        axL.step(xs, pb, where="post", color=c_k, lw=1.6, label=f"{b_label} cost")
        axL.step(xs, comb, where="post", color=c_comb, lw=2.0, label="combined + fee")
        axL.axhline(1.0, color=c_be, ls="--", lw=1.2, label="break-even ($1)")
        axL.fill_between(xs, comb, 1.0, where=[c < 1.0 for c in comb], step="post",
                         color=c_fill, alpha=0.8, zorder=0)
        if max_arb > 0:
            axL.axvline(max_arb, color="#16a34a", ls=":", lw=1.3)
            axL.annotate(f"{max_arb:,.0f} arbable", xy=(max_arb, 0.5),
                         xytext=(4, 0), textcoords="offset points", fontsize=8,
                         color="#16a34a", rotation=90, va="center")
        axL.set_xlabel("cumulative contracts (depth)", fontsize=9)
        axL.set_ylabel("price per $1 payout", fontsize=9)
        axL.set_title("Executable depth — both books", fontsize=10, fontweight="bold")
        axL.legend(fontsize=7, loc="best", framealpha=0.9)
        axL.grid(True, alpha=0.25)
        axL.tick_params(labelsize=8)

        # ---- Panel 2: profit by budget ----
        buds = list(budgets)
        profits = [res["by_budget"][b].profit for b in buds]
        conts = [res["by_budget"][b].contracts for b in buds]
        bars = axR.bar([f"${b/1000:g}k" for b in buds], profits, color=c_pm, alpha=0.85)
        for bar, p, n in zip(bars, profits, conts):
            axR.annotate(f"${p:,.0f}\n{n:,.0f}c", xy=(bar.get_x() + bar.get_width() / 2,
                         bar.get_height()), xytext=(0, 2), textcoords="offset points",
                         ha="center", va="bottom", fontsize=7.5)
        axR.set_xlabel("investment per market", fontsize=9)
        axR.set_ylabel("net profit ($, after fee)", fontsize=9)
        axR.set_title("VWAP profit by budget", fontsize=10, fontweight="bold")
        axR.grid(True, axis="y", alpha=0.25)
        axR.tick_params(labelsize=8)
        if profits and max(profits) > 0:
            axR.set_ylim(0, max(profits) * 1.25)

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        return None

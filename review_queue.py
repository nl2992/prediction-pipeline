"""Phase 2e: review-queue ranking and honest yield metrics.

Pure functions only -- no network calls, no store writes, no alert/executor
side effects. Ranks the LATEST COMPLETED scan's pairs with exactly the same
economics and sort key ``tools.audit_ranked_signals.run()`` uses (so the
dashboard's review queue always matches what the CLI audit tool would print
for the same pairs), then attaches capital/return economics per row.

Reuses ``alerter.compute_signals`` (the same signal calculator the alerter
and the CLI audit tool use) and ``tools.audit_ranked_signals._serialize_signal``
/``_pair_provenance`` (the same row shape the CLI audit tool emits) rather
than duplicating either.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from alerter import compute_signals
from store import pair_key as _store_pair_key
from tools.audit_ranked_signals import _pair_provenance, _serialize_signal


def _parse_dt(value: Any) -> datetime | None:
    """Best-effort ISO-8601 parse; None on anything unparsable/absent. Naive
    datetimes are assumed UTC (matching how discover.py/store.py stamp times)."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_MIN_DAYS_TO_SETTLE = 0.5     # a same-day settlement is not truly instant capital recycling
_MIN_DAYS_FOR_ANNUALIZED = 30  # below this, compounding out to a year is noise (11d -> 1742%)


def yield_metrics(signal: dict[str, Any], now: datetime, *,
                   contracts: float | None = None,
                   depth: dict[str, float] | None = None) -> dict[str, Any]:
    """Pure capital/return economics for one signal row (docs/IMPLEMENTATION_PLAN.md
    Phase 2, "Yield columns"). ``signal`` is a compute_signals()-shaped dict (or
    anything carrying the same field names): ``exec_net``/``net_accurate``,
    ``exec_vwap_kalshi``/``exec_vwap_poly``, ``kalshi_close``/``poly_close``.

    ``contracts`` is the depth-walked executable size for this row, supplied
    by the caller (compute_signals's ``min_size`` parameter -- see
    ``alerter._executable_edge``: a direction with ``min_size > 0`` either
    fills EXACTLY that quantity on both legs or is dropped entirely, so
    ``min_size`` *is* the executable contract count whenever
    ``exec_vwap_kalshi``/``exec_vwap_poly`` are present). None when the
    caller doesn't know the executable size (e.g. a top-of-book-only signal).

    ``depth`` (optional) carries the full depth-walk economics discover.py
    computed for the pair (``contracts``, ``profit``, ``capital``, ``roi``);
    when given it replaces the probe-sized contracts/capital/profit/return
    (``size_basis="max_depth"``). ``edge_per_contract`` always stays the
    min-size edge. ``annualized_return`` is only computed when
    ``days_to_settle >= 30``.

    Every field can be None -- callers must handle missing capital/profit/
    return/annualized gracefully; that's the whole point of separating an
    honest "unknown" from a fabricated number.
    """
    edge = signal.get("exec_net")
    if edge is None:
        edge = signal.get("net_accurate")
    edge = float(edge) if edge is not None else None

    vwap_k = signal.get("exec_vwap_kalshi")
    vwap_p = signal.get("exec_vwap_poly")
    capital: float | None = None
    if contracts is not None and vwap_k is not None and vwap_p is not None:
        capital = round(float(contracts) * (float(vwap_k) + float(vwap_p)), 6)

    profit_usd: float | None = None
    if contracts is not None and edge is not None:
        profit_usd = round(float(contracts) * edge, 6)

    return_on_capital: float | None = None
    if profit_usd is not None and capital:  # capital not None and not 0.0
        return_on_capital = round(profit_usd / capital, 6)

    size_basis = "min_size_probe"
    if depth is not None:
        contracts = depth["contracts"]
        profit_usd = round(float(depth["profit"]), 6)
        capital = round(float(depth["capital"]), 6)
        return_on_capital = round(float(depth["roi"]), 6)
        size_basis = "max_depth"

    kalshi_close = _parse_dt(signal.get("kalshi_close"))
    poly_close = _parse_dt(signal.get("poly_close"))
    # Capital is locked until BOTH legs settle -- if either leg's close date
    # is unknown, the LATER of the two is not actually knowable, so the
    # horizon is "unknown" rather than silently using whichever one exists.
    if kalshi_close is None or poly_close is None:
        days_to_settle: float | None = None
    else:
        later = max(kalshi_close, poly_close)
        days_to_settle = max((later - now).total_seconds() / 86400.0, _MIN_DAYS_TO_SETTLE)

    annualized_return: float | None = None
    if (return_on_capital is not None and days_to_settle is not None
            and days_to_settle >= _MIN_DAYS_FOR_ANNUALIZED):
        try:
            annualized_return = round(
                (1.0 + return_on_capital) ** (365.0 / days_to_settle) - 1.0, 6)
        except (OverflowError, ValueError):
            annualized_return = None

    return {
        "edge_per_contract": edge,
        "contracts": contracts,
        "size_basis": size_basis,
        "capital": capital,
        "profit_usd": profit_usd,
        "return_on_capital": return_on_capital,
        "days_to_settle": round(days_to_settle, 4) if days_to_settle is not None else None,
        # Always labelled "estimate" by the caller/UI -- annualizing a single
        # settlement horizon assumes the capital gets redeployed at the same
        # rate all year, which is never actually true.
        "annualized_return": annualized_return,
    }


def _full_pair_index(pairs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Same key shape as ``_pair_provenance`` (``poly_id|kalshi_ticker``), but
    indexing the FULL original pair dict (so ``poly_token_id`` -- absent from
    ``_pair_provenance``'s slim provenance dict -- is available for
    ``store.pair_key``, which prefers it over ``poly_id``)."""
    return {f"{p.get('poly_id')}|{p.get('kalshi_ticker')}": p for p in pairs}


def _signal_pair_key(signal: dict[str, Any], full_index: dict[str, dict[str, Any]]) -> str:
    prov_key = "|".join(str(signal.get("key", "")).split("|")[:2])
    pair = full_index.get(prov_key)
    if pair is not None:
        return _store_pair_key(pair)
    poly_id, _, kalshi_ticker = prov_key.partition("|")
    return _store_pair_key({"poly_id": poly_id, "kalshi_ticker": kalshi_ticker})


def review_is_stale(review: dict[str, Any] | None, pair: dict[str, Any] | None) -> bool:
    """True only when BOTH the review's stored rules hash and the pair's
    CURRENT rules hash are known and they differ. Rules hashes are populated
    by the (separate, not-yet-landed) evidence/rule-diff work; until a pair
    carries ``kalshi_rules_hash``/``poly_rules_hash`` this always returns
    False rather than guessing -- an unknown current hash is not evidence of
    staleness."""
    if not review or not pair:
        return False
    stale = False
    current_k = pair.get("kalshi_rules_hash")
    review_k = review.get("kalshi_rules_hash")
    if current_k is not None and review_k is not None and current_k != review_k:
        stale = True
    current_p = pair.get("poly_rules_hash")
    review_p = review.get("poly_rules_hash")
    if current_p is not None and review_p is not None and current_p != review_p:
        stale = True
    return stale


def _depth_economics(pair: dict[str, Any] | None) -> dict[str, float] | None:
    """discover.py's full depth walk for the pair (``exec_contracts``,
    ``exec_profit``, ``exec_roi`` = book_arb ``profit / (cost_a + cost_b)``,
    so capital = profit / roi). None unless all are present and positive."""
    if not pair:
        return None
    c, p, r = pair.get("exec_contracts"), pair.get("exec_profit"), pair.get("exec_roi")
    if c is None or p is None or r is None:
        return None
    try:
        c, p, r = float(c), float(p), float(r)
    except (TypeError, ValueError):
        return None
    if c <= 0 or r <= 0:
        return None
    return {"contracts": c, "profit": p, "roi": r, "capital": p / r}


def serialize_review_row(signal: dict[str, Any], rank: int,
                          provenance: dict[str, dict[str, Any]],
                          full_index: dict[str, dict[str, Any]],
                          now: datetime) -> dict[str, Any]:
    """One review-queue row: the CLI audit tool's row shape
    (``tools.audit_ranked_signals._serialize_signal``) plus a stable
    ``pair_key`` and this phase's yield metrics. Reviews/staleness are NOT
    attached here (that needs a store lookup across all rows at once) --
    see ``server.api_review_queue``."""
    row = _serialize_signal(signal, rank, provenance)
    has_exec = signal.get("exec_vwap_kalshi") is not None and signal.get("exec_vwap_poly") is not None
    contracts = signal.get("exec_contracts")
    if contracts is None and has_exec:
        contracts = signal.get("_min_size_hint")
    row["exec_contracts"] = contracts
    row["pair_key"] = _signal_pair_key(signal, full_index)
    prov_key = "|".join(str(signal.get("key", "")).split("|")[:2])
    depth = _depth_economics(full_index.get(prov_key))
    row.update(yield_metrics(signal, now, contracts=contracts, depth=depth))
    if depth is not None:
        row["exec_contracts"] = depth["contracts"]
    return row


def build_review_queue_rows(
    pairs: list[dict[str, Any]], *, top_n: int = 25, min_size: float = 20.0,
    now: datetime | None = None,
    compute_signals_fn=compute_signals,
) -> list[dict[str, Any]]:
    """Ranks ``pairs`` exactly like ``tools.audit_ranked_signals.run()``
    (``compute_signals(min_edge=0.0, require_v2=True, max_edge=1.0,
    min_size=min_size)``, sorted by ``exec_net`` desc, then ``net_accurate``
    desc, then ``key`` -- see that module's ``run()``) and returns the top
    ``top_n`` as review-queue rows. Does not touch the reviews store."""
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    if min_size <= 0:
        raise ValueError("min_size must be positive for a depth-backed queue")

    now = now or datetime.now(timezone.utc)
    signals = compute_signals_fn(
        pairs, min_edge=0.0, require_v2=True, max_edge=1.0, min_size=min_size,
    )
    # compute_signals() only ever fills EXACTLY min_size contracts on both
    # legs when it reports exec_net/exec_vwap_* at all (see
    # alerter._executable_edge / book_arb.executable_edge_at_size) -- so
    # min_size IS the executable contract count for every returned signal.
    for signal in signals:
        signal.setdefault("_min_size_hint", min_size)
    signals.sort(key=lambda s: (
        -float(s.get("exec_net", s.get("net_accurate", float("-inf")))),
        -float(s.get("net_accurate", float("-inf"))),
        str(s.get("key", "")),
    ))
    provenance = _pair_provenance(pairs)
    full_index = _full_pair_index(pairs)
    return [
        serialize_review_row(signal, rank, provenance, full_index, now)
        for rank, signal in enumerate(signals[:top_n], start=1)
    ]

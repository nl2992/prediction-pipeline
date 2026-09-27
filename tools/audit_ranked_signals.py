"""Read-only, ranked audit dataset for human settlement-equivalence review.

This intentionally uses discovery plus the pure signal calculator only.  It never
calls alert delivery, alert state, AI verification, the executor, server routes,
or ladder execution.  By default it scans the event catalogs only; pass
``--with-orphan-sweep`` only when a slower complete orphan sweep is required.

Usage:
    python -m tools.audit_ranked_signals --top-n 25
    python -m tools.audit_ranked_signals --top-n 50 --with-orphan-sweep
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable
from typing import Any

from alerter import compute_signals
from discover import discover


_SIGNAL_FIELDS = (
    "key", "exec_net", "exec_vwap_poly", "exec_vwap_kalshi", "net_accurate",
    "direction", "legs", "poly_title", "kalshi_title", "poly_event_title",
    "kalshi_event_title", "poly_bid", "poly_ask", "kalshi_bid", "kalshi_ask",
    "poly_close", "kalshi_close", "confidence",
)


def _pair_provenance(pairs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index discovery metadata by the stable two-market portion of signal key."""
    out: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        key = f"{pair.get('poly_id')}|{pair.get('kalshi_ticker')}"
        out[key] = {
            "poly_id": pair.get("poly_id"),
            "poly_slug": pair.get("poly_slug"),
            "kalshi_ticker": pair.get("kalshi_ticker"),
            "category": pair.get("category"),
            "match_source": pair.get("match_source"),
            "v2_reasons": pair.get("v2_reasons"),
            "catalog_gross_edge": pair.get("catalog_gross_edge"),
            "books_live": pair.get("books_live"),
        }
    return out


def _serialize_signal(signal: dict[str, Any], rank: int,
                      provenance: dict[str, dict[str, Any]]) -> dict[str, Any]:
    row = {field: signal.get(field) for field in _SIGNAL_FIELDS}
    pair_key = "|".join(str(signal.get("key", "")).split("|")[:2])
    row.update(provenance.get(pair_key, {}))
    row["rank"] = rank
    return row


def _scan_mode(coverage: dict[str, Any], with_orphan_sweep: bool) -> str:
    if not with_orphan_sweep:
        return "event-catalog-only"
    statuses = {venue: coverage.get(venue, {}).get("sweep")
                for venue in ("kalshi", "polymarket")}
    return ("orphan-sweep-complete" if all(status == "fresh" for status in statuses.values())
            else "orphan-sweep-incomplete")


def run(
    *,
    top_n: int = 25,
    min_size: float = 20.0,
    with_orphan_sweep: bool = False,
    max_events: int | None = None,
    discover_fn: Callable[..., list[dict[str, Any]]] = discover,
    compute_signals_fn: Callable[..., list[dict[str, Any]]] = compute_signals,
) -> dict[str, Any]:
    """Return a reproducible, side-effect-free ranked signal audit dataset.

    ``coverage`` is captured solely for provenance.  Discovery prints progress,
    so callers that require JSON-only stdout should redirect it (``main`` does).
    Prices are retained as live-audit provenance, never as regression criteria.
    """
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    if min_size <= 0:
        raise ValueError("min_size must be positive for a depth-backed audit")

    coverage: dict[str, Any] = {}
    pairs = discover_fn(
        category="all", days=None, min_sim=0.30, show_prices=True,
        max_poly_offset=None, max_events_to_search=max_events,
        market_sweep=with_orphan_sweep,
        # A complete sweep is deliberately fresh and never persists a cache.
        sweep_cache_ttl=0,
        enrich_margin=None,
        coverage=coverage,
    )
    signals = compute_signals_fn(
        pairs, min_edge=0.0, require_v2=True, max_edge=1.0, min_size=min_size,
    )
    signals.sort(key=lambda s: (
        -float(s.get("exec_net", s.get("net_accurate", float("-inf")))),
        -float(s.get("net_accurate", float("-inf"))),
        str(s.get("key", "")),
    ))
    provenance = _pair_provenance(pairs)
    ranked = [_serialize_signal(signal, rank, provenance)
              for rank, signal in enumerate(signals[:top_n], start=1)]

    return {
        "audit_version": 1,
        "scan_mode": _scan_mode(coverage, with_orphan_sweep),
        "coverage": coverage,
        "parameters": {
            "top_n": top_n,
            "min_size": min_size,
            "max_events": max_events,
            "require_v2": True,
            "min_edge": 0.0,
            "max_edge": 1.0,
        },
        "pair_count": len(pairs),
        "depth_backed_signal_count": len(signals),
        "signals": ranked,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top-n", type=int, default=25)
    parser.add_argument("--min-size", type=float, default=20.0)
    parser.add_argument("--max-events", type=int)
    parser.add_argument(
        "--with-orphan-sweep", action="store_true",
        help="Run fresh Kalshi and Polymarket orphan sweeps; slow but complete, no cache writes.",
    )
    args = parser.parse_args()
    # discover() emits progress. Preserve a single machine-readable JSON document on stdout.
    with contextlib.redirect_stdout(sys.stderr):
        result = run(
            top_n=args.top_n,
            min_size=args.min_size,
            with_orphan_sweep=args.with_orphan_sweep,
            max_events=args.max_events,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

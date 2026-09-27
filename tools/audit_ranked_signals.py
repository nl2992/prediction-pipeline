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

import evidence
from rule_diff import compare_rules


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


def _attach_rule_flags(row: dict[str, Any], get_evidence_fn: Callable[..., dict | None]) -> None:
    """Mutates ``row`` in place, adding a "rule_flags" list -- the whole
    point of ``--with-rules``: title-level matching (everything else in this
    audit) can't see a settlement-rule difference, only fetching the actual
    rule text and diffing it can (Phase 2d)."""
    kalshi_ticker = row.get("kalshi_ticker")
    poly_id = row.get("poly_id")
    kalshi_evidence = get_evidence_fn("kalshi", kalshi_ticker) if kalshi_ticker else None
    poly_evidence = get_evidence_fn("polymarket", poly_id) if poly_id else None
    kalshi_text = kalshi_evidence.get("rules_text") if kalshi_evidence else None
    poly_text = poly_evidence.get("rules_text") if poly_evidence else None
    row["rule_flags"] = compare_rules(kalshi_text, poly_text)


def run(
    *,
    top_n: int = 25,
    min_size: float = 20.0,
    with_orphan_sweep: bool = False,
    max_events: int | None = None,
    with_rules: bool = False,
    discover_fn: Callable[..., list[dict[str, Any]]] = discover,
    compute_signals_fn: Callable[..., list[dict[str, Any]]] = compute_signals,
    get_evidence_fn: Callable[..., dict | None] = evidence.get_evidence,
) -> dict[str, Any]:
    """Return a reproducible, side-effect-free ranked signal audit dataset.

    ``coverage`` is captured solely for provenance.  Discovery prints progress,
    so callers that require JSON-only stdout should redirect it (``main`` does).
    Prices are retained as live-audit provenance, never as regression criteria.

    ``with_rules`` (default off, so existing callers/fixtures see unchanged
    output) fetches settlement-rule evidence for each top-N signal's two
    markets and adds a ``rule_flags`` list to it (see ``rule_diff.compare_rules``),
    plus a top-level ``high_severity_rule_flag_pairs`` count.
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

    result: dict[str, Any] = {
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

    if with_rules:
        for row in ranked:
            _attach_rule_flags(row, get_evidence_fn)
        result["high_severity_rule_flag_pairs"] = sum(
            1 for row in ranked
            if any(flag.get("severity") == "high" for flag in row.get("rule_flags") or [])
        )

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top-n", type=int, default=25)
    parser.add_argument("--min-size", type=float, default=20.0)
    parser.add_argument("--max-events", type=int)
    parser.add_argument(
        "--with-orphan-sweep", action="store_true",
        help="Run fresh Kalshi and Polymarket orphan sweeps; slow but complete, no cache writes.",
    )
    parser.add_argument(
        "--with-rules", action="store_true",
        help="Fetch settlement-rule evidence for the top-N signals and flag rule conflicts "
             "(Phase 2d); off by default so existing output stays unchanged.",
    )
    args = parser.parse_args()
    # discover() emits progress. Preserve a single machine-readable JSON document on stdout.
    with contextlib.redirect_stdout(sys.stderr):
        result = run(
            top_n=args.top_n,
            min_size=args.min_size,
            with_orphan_sweep=args.with_orphan_sweep,
            max_events=args.max_events,
            with_rules=args.with_rules,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

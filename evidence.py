"""Settlement-rule evidence: fetch each pair's actual rule text from the two
venue APIs, normalize/hash it, and cache it in the ``market_evidence`` table
(Phase 2d).

Why: a live audit found that the four highest-edge "arbitrage" pairs on
Kalshi vs. Polymarket were not arbitrage -- market TITLES matched but the
underlying settlement RULES differed. Title-level guards can't see this;
this module fetches the actual rule text so ``rule_diff.compare_rules`` can.

All HTTP goes through the single module-level ``_http_get_json`` function so
tests can monkeypatch it -- the unit suite must never touch the network.

SECURITY NOTE: rule text returned by these venue APIs is UNTRUSTED DATA. It
is stored and compared as opaque text only. Nothing here executes, evals, or
follows a URL found inside it, and fetches are read-only GETs restricted to
the two fixed venue API hosts below.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import requests

import store

logger = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

_TIMEOUT = 10
_MAX_ATTEMPTS = 2  # one try + one retry, matching the brief's "10s timeout, single retry"

DEFAULT_MAX_AGE_S = 6 * 60 * 60  # 6 hours


def _http_get_json(url: str, params: dict | None = None) -> Any:
    """Single injectable HTTP GET-JSON call used by every fetcher below.

    Kept as a bare module-level function (rather than reusing
    KalshiClient/PolymarketClient's session objects) so tests can monkeypatch
    exactly one seam and so this module never depends on those clients'
    heavier retry/auth machinery -- this is a light, read-only, evidence-only
    path. One retry on a transient failure, mirroring the venue clients'
    own (larger) retry behavior.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.get(
                url, params=params, timeout=_TIMEOUT,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == _MAX_ATTEMPTS - 1:
                raise
    raise last_exc  # pragma: no cover -- unreachable, the loop always raises or returns


def _series_ticker_for(ticker: str, market: dict) -> str:
    """Series ticker = market.series_ticker if the API ever returns one
    (checked against the live API during development -- it currently does
    NOT appear on the market object), else the ticker prefix before the
    first '-' (e.g. "KXLEADERMLBWINS-26-SGRA" -> "KXLEADERMLBWINS")."""
    return market.get("series_ticker") or ticker.split("-")[0]


def fetch_kalshi_evidence(ticker: str) -> dict | None:
    """Fetch a Kalshi market's settlement-rule evidence.

    ``GET /markets/{ticker}`` -> ``market.rules_primary`` / ``rules_secondary``.
    ``GET /series/{series_ticker}`` -> ``settlement_sources`" (list of
    ``{name, url}``) and ``contract_url``, used only to build a
    human-openable ``source_url`` (a settlement-sources name is also folded
    into the rules text below, since it can carry additional resolution
    policy, e.g. which outlet's numbers govern).

    Normalized ``rules_text`` = ``rules_primary`` + "\\n\\n" + ``rules_secondary``,
    with each settlement source's ``name`` appended as its own paragraph
    (skipped if the series lookup fails or has none) -- this exact
    concatenation is what gets sha256-hashed for ``content_hash``.

    ``source_url`` = the series' ``contract_url`` (a human-readable filing
    that documents the contract) if present, else
    ``https://kalshi.com/markets/{series_ticker_lowercased}`` -- a market
    page a human can open directly.

    Returns ``None`` if the market fetch fails or the market has no rules
    text at all.
    """
    try:
        market_resp = _http_get_json(f"{KALSHI_BASE}/markets/{ticker}")
    except Exception:
        logger.warning("fetch_kalshi_evidence: market fetch failed for %s", ticker, exc_info=True)
        return None
    market = market_resp.get("market", market_resp) if isinstance(market_resp, dict) else {}
    if not market:
        return None

    series_ticker = _series_ticker_for(ticker, market)
    settlement_sources: list[dict] = []
    contract_url = None
    try:
        series_resp = _http_get_json(f"{KALSHI_BASE}/series/{series_ticker}")
        series = series_resp.get("series", series_resp) if isinstance(series_resp, dict) else {}
        settlement_sources = series.get("settlement_sources") or []
        contract_url = series.get("contract_url")
    except Exception:
        logger.info(
            "fetch_kalshi_evidence: series fetch failed for %s (rule text still usable)",
            series_ticker, exc_info=True,
        )

    parts = [market.get("rules_primary") or "", market.get("rules_secondary") or ""]
    for source in settlement_sources:
        name = source.get("name") if isinstance(source, dict) else None
        if name:
            parts.append(str(name))
    rules_text = "\n\n".join(p for p in parts if p).strip()
    if not rules_text:
        return None

    source_url = contract_url or f"https://kalshi.com/markets/{series_ticker.lower()}"
    return {"rules_text": rules_text, "source_url": source_url}


def fetch_polymarket_evidence(condition_id: str) -> dict | None:
    """Fetch a Polymarket market's settlement-rule evidence.

    ``GET /markets?condition_ids={id}`` -> ``question``, ``description``,
    ``resolutionSource``, ``events[0].slug``. If ``description`` is empty,
    falls back to the parent event's own ``description``
    (``GET /events?slug={slug}``) -- Gamma sometimes leaves a sub-market's own
    description blank when the resolution text lives on the shared event.

    Normalized ``rules_text`` = ``question`` + "\\n\\n" + ``description`` +
    "\\n\\n" + ``resolutionSource`` (empty parts dropped) -- this exact
    concatenation is what gets sha256-hashed for ``content_hash``.

    ``source_url`` = ``https://polymarket.com/event/{slug}`` -- a page a
    human can open directly. ``None`` if there is no parent event slug.

    Returns ``None`` if the market fetch fails, returns no rows, or the
    market has no rules text at all.
    """
    try:
        resp = _http_get_json(f"{GAMMA_BASE}/markets", params={"condition_ids": condition_id})
    except Exception:
        logger.warning(
            "fetch_polymarket_evidence: market fetch failed for %s", condition_id, exc_info=True,
        )
        return None
    markets = resp if isinstance(resp, list) else (resp or {}).get("markets") or []
    if not markets:
        return None
    market = markets[0]

    question = market.get("question") or ""
    description = market.get("description") or ""
    resolution_source = market.get("resolutionSource") or ""
    events = market.get("events") or []
    slug = events[0].get("slug") if events else None

    if not description and slug:
        try:
            ev_resp = _http_get_json(f"{GAMMA_BASE}/events", params={"slug": slug})
            ev_list = ev_resp if isinstance(ev_resp, list) else (ev_resp or {}).get("events") or []
            if ev_list:
                description = ev_list[0].get("description") or ""
        except Exception:
            logger.info(
                "fetch_polymarket_evidence: event fetch failed for slug=%s (rule text still usable)",
                slug, exc_info=True,
            )

    rules_text = "\n\n".join(p for p in (question, description, resolution_source) if p).strip()
    if not rules_text:
        return None

    source_url = f"https://polymarket.com/event/{slug}" if slug else None
    return {"rules_text": rules_text, "source_url": source_url}


_FETCHERS = {
    "kalshi": fetch_kalshi_evidence,
    "polymarket": fetch_polymarket_evidence,
}


def _age_seconds(iso_ts: str) -> float:
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def get_evidence(
    venue: str, market_id: str, max_age_s: int = DEFAULT_MAX_AGE_S, fetch: bool = True,
) -> dict | None:
    """Cache-first evidence lookup for one ``(venue, market_id)``.

    - A cached row fresher than ``max_age_s`` (or any cached row, if
      ``fetch`` is False) is returned as-is, with ``stale`` set to False.
      ``fetched_at`` is a "last confirmed" timestamp (see
      ``store.save_evidence``), not "first seen" -- an unchanged-text
      re-save bumps it to now, so a market whose rules never change stays
      "fresh" forever instead of forcing a live refetch on every call once
      the first ``max_age_s`` window elapses.
    - Otherwise, fetches live. On success, saves (idempotent on unchanged
      text -- see ``store.save_evidence``) and returns the fresh row
      (``stale`` False).
    - On a fetch failure, falls back to the stale cached row (with ``stale``
      set to True), or returns None if there is no cache at all.

    ``venue`` must be "kalshi" or "polymarket".
    """
    fetcher = _FETCHERS.get(venue)
    if fetcher is None:
        raise ValueError(f"unknown venue: {venue!r}")

    cached = store.latest_evidence(venue, market_id)
    if cached is not None:
        cached = dict(cached)
        cached["stale"] = False
        if not fetch:
            return cached
        if cached.get("fetched_at") and _age_seconds(cached["fetched_at"]) <= max_age_s:
            return cached
    elif not fetch:
        return None

    try:
        result = fetcher(market_id)
    except Exception:
        logger.warning("get_evidence: fetch failed for %s/%s", venue, market_id, exc_info=True)
        result = None

    if not result:
        if cached is not None:
            cached["stale"] = True
            return cached
        return None

    row = store.save_evidence(venue, market_id, result["rules_text"], result.get("source_url"))
    row = dict(row)
    row["stale"] = False
    return row


def content_hash(rules_text: str) -> str:
    """sha256 hex digest of the normalized rules text -- the same hash
    ``store.save_evidence`` computes and stores as ``content_hash``. Exposed
    here so callers (e.g. server.py's review-staleness check) can compute it
    without importing hashlib themselves."""
    return hashlib.sha256(rules_text.encode("utf-8")).hexdigest()

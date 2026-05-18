"""
Organic Cross-Exchange Market Discovery
========================================
Scans the full Kalshi events catalog (excluding sports parlays), auto-derives
search keywords from each event title, searches Polymarket for matching markets,
and runs the matcher to surface real cross-exchange price comparisons.

No manual slug or series configuration needed.  Just run it.

Usage
-----
    python discover.py                          # all near-term non-sports events
    python discover.py --days 180               # closing within 180 days
    python discover.py --category election      # elections only
    python discover.py --category economic      # Fed/crypto/GDP/etc.
    python discover.py --category all           # everything
    python discover.py --min-sim 0.30           # stricter title matching
    python discover.py --show-prices            # fetch live orderbooks for pairs
    python discover.py --output pairs.json      # save results to JSON

Categories
----------
  election  – Congressional, Senate, Governor, primary, AG, Sec of State races
  economic  – Fed rate, Bitcoin, GDP, CPI, jobs, economic indicators
  political – SCOTUS, Trump admin, legislation, international politics
  pop       – Entertainment, sports (non-parlay), celebrity
  all       – Everything non-parlay

Algorithm
---------
1. Fetch all active Kalshi events (3000 events, ~7s)
2. Filter to non-sports, category-matching, near-term events
3. For each filtered event, derive 2–4 Polymarket search keywords from title
4. Deduplicate keywords; search Polymarket in batches (paginated keyword scan)
5. Build MarketSnapshots from both sides
6. Run Jaccard+close-time matcher with per-category thresholds
7. Print ranked pairs with bid/ask spread and implied arb
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Sports prefixes — exclude from discovery
# ---------------------------------------------------------------------------

SPORTS_EVENT_PREFIXES = (
    "KXMVE", "KXMLB", "KXNBA", "KXNFL", "KXNHL", "KXSOCCER",
    "KXNASCAR", "KXTENNIS", "KXGOLF", "KXPGA", "KXFORMULA",
    "KXBOXING", "KXMMA", "KXUFC", "KXESPORT", "KXOLYMPIC",
    "KXNCAAFW", "KXNCAAMB", "KXNCAAH",
)

SPORTS_TITLE_PATTERNS = re.compile(
    r"^(yes |no |over |under |\d+\.\d+ (points|runs|games|rebounds|assists|strikeouts))",
    re.IGNORECASE,
)


def _is_sports(event: dict) -> bool:
    et = (event.get("event_ticker") or "").upper()
    if et.startswith(SPORTS_EVENT_PREFIXES):
        return True
    title = event.get("title", "").lower()
    # Voter turnout / margin of victory are Kalshi-only stats markets
    if any(p in title for p in ("voter turnout", "margin of victory", "vote count",
                                 "total vote", "win percentage", "vote share",
                                 "double double", "triple double")):
        return True
    return bool(SPORTS_TITLE_PATTERNS.match(event.get("title", "")))


# ---------------------------------------------------------------------------
# Category classifiers
# ---------------------------------------------------------------------------

ELECTION_HINTS = re.compile(
    r"(republican|democrat|nominee|primary|senate|house|governor|congress|"
    r"election|race|seat|candidate|ag |attorney general|secretary of state|"
    r"win the|who will win|special election|runoff|party)",
    re.IGNORECASE,
)

ECONOMIC_HINTS = re.compile(
    r"(fed|federal funds|interest rate|fomc|bitcoin|btc|ethereum|eth|gdp|cpi|"
    r"inflation|jobs|unemployment|payroll|s&p|nasdaq|dow|oil|gold|gas price|"
    r"tariff|trade deficit|treasury|yield|mortgage rate)",
    re.IGNORECASE,
)

POLITICAL_HINTS = re.compile(
    r"(scotus|supreme court|trump|biden|president|congress|legislation|bill|"
    r"impeach|resign|cabinet|nato|ukraine|russia|china|israel|iran|"
    r"executive order|veto|pardon|indictment|conviction|sanction)",
    re.IGNORECASE,
)


def _category(event: dict) -> str:
    title = event.get("title", "")
    if ELECTION_HINTS.search(title):
        return "election"
    if ECONOMIC_HINTS.search(title):
        return "economic"
    if POLITICAL_HINTS.search(title):
        return "political"
    return "pop"


# ---------------------------------------------------------------------------
# Keyword extraction from Kalshi event titles
# ---------------------------------------------------------------------------

# US state abbreviations (2-letter) and common district patterns
_STATE_RE = re.compile(r"\b([A-Z]{2})-?(\d{2})\b")
_CANDIDATE_RE = re.compile(r"Will ([A-Z][a-z]+ [A-Z][a-z]+)")

_QUESTION_WORDS = re.compile(
    r"^(will|which|who|who will|when will|what|how|does|is|are|can|"
    r"should|does|did|was|were)\s+",
    re.IGNORECASE,
)
_FILLER = re.compile(
    r"\b(the|a|an|be|win|lose|get|have|make|take|go|come|run|become|"
    r"happen|occur|pass|reach|above|below|over|under|before|after|"
    r"during|next|new|another|other|this|that|than|more|less|at|in|"
    r"on|of|for|to|by|with|from|or|and|but|not|no|yes|any|all|each|"
    r"ever|never|first|last|most|least|per|between|within|until|since|"
    r"once|also|already|only|just|even|still|yet|again|then|when|where|"
    r"who|whom|whose|how|why|whether|either|both|neither|very|really|"
    r"party|parties|election|race|seat|candidate|winner|win)\b",
    re.IGNORECASE,
)


def _derive_keywords(title: str) -> list[str]:
    """
    Extract 2–4 short search terms from a Kalshi event title.

    Strategy:
      1. Named candidates → use full name (e.g. "Thomas Massie")
      2. District codes → use "XX-NN" style (e.g. "KY-04")
      3. State name → use state abbreviation + surrounding noun
      4. Fall back to removing stopwords and taking the 3 longest tokens
    """
    keywords: list[str] = []

    # Named candidates (two capitalised words)
    for m in _CANDIDATE_RE.finditer(title):
        keywords.append(m.group(1))

    # District/state codes like "KY-4", "MD-06", "TX-35"
    for m in _STATE_RE.finditer(title):
        state, num = m.group(1), m.group(2).lstrip("0") or "0"
        keywords.append(f"{state}-{num}")
        keywords.append(f"{state}-{num.zfill(2)}")

    if keywords:
        return list(dict.fromkeys(keywords))[:4]

    # Fall back: remove question words + stopwords, keep longest tokens
    cleaned = _QUESTION_WORDS.sub("", title)
    cleaned = _FILLER.sub(" ", cleaned)
    tokens = [t for t in re.split(r"\W+", cleaned) if len(t) > 2]
    # Sort by length desc to get the most informative terms
    tokens.sort(key=len, reverse=True)
    return list(dict.fromkeys(tokens[:3]))


# ---------------------------------------------------------------------------
# Snapshot builders (no orderbook fetch — speed first)
# ---------------------------------------------------------------------------

def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.split("+")[0].rstrip("Zz"), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _k_snap(m: dict, fetched_at: str):
    from pipeline import MarketSnapshot, OrderBook, PriceLevel, _parse_kalshi_top_of_book
    ob = _parse_kalshi_top_of_book(m)
    close = m.get("close_time") or m.get("expiration_time")
    return MarketSnapshot(
        source="kalshi",
        market_id=m.get("ticker", ""),
        event_id=m.get("event_ticker", ""),
        title=m.get("title", ""),
        status=m.get("status", ""),
        close_time=close,
        fetched_at=fetched_at,
        orderbook=ob,
    )


def _p_snap(m: dict, fetched_at: str):
    from pipeline import MarketSnapshot, OrderBook, PriceLevel
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = []
    yes_p = None
    if prices:
        try:
            yes_p = float(prices[0])
        except (TypeError, ValueError):
            pass
    ob = OrderBook(
        bids=[PriceLevel(yes_p, 0.0)] if yes_p else [],
        asks=[PriceLevel(yes_p, 0.0)] if yes_p else [],
    )
    tids = m.get("clobTokenIds")
    if isinstance(tids, str):
        try:
            tids = json.loads(tids)
        except Exception:
            tids = []
    close = m.get("endDate") or m.get("endDateIso")
    return MarketSnapshot(
        source="polymarket",
        market_id=m.get("conditionId") or m.get("id", ""),
        event_id=m.get("slug", ""),
        title=m.get("question") or m.get("title", ""),
        status="open" if m.get("active") else "closed",
        close_time=close,
        fetched_at=fetched_at,
        orderbook=ob,
        extra={"clob_token_ids": tids},
    )


# ---------------------------------------------------------------------------
# Live orderbook enrichment (called only for confirmed pairs)
# ---------------------------------------------------------------------------

def _enrich_kalshi(snaps, max_workers: int = 8):
    """Fetch full Kalshi orderbooks for the given snapshots (in parallel-ish)."""
    from kalshi.client import KalshiClient
    from pipeline import _parse_kalshi_full_book
    client = KalshiClient()
    for snap in snaps:
        try:
            raw = client.get_orderbook(snap.market_id)
            snap.orderbook = _parse_kalshi_full_book(raw)
        except Exception:
            pass


def _enrich_polymarket(snaps):
    """Fetch live CLOB orderbooks for Polymarket snapshots."""
    from polymarket.client import PolymarketClient
    from pipeline import _parse_polymarket_book
    client = PolymarketClient()
    token_map: dict[str, Any] = {}
    for snap in snaps:
        tids = snap.extra.get("clob_token_ids") or []
        if tids:
            token_map[snap.market_id] = tids[0]  # YES token
    if not token_map:
        return
    try:
        bodies = [{"token_id": tid} for tid in token_map.values()]
        books = client._post("https://clob.polymarket.com", "/books", json_body=bodies)
        for snap, (_, tid) in zip(
            [s for s in snaps if s.market_id in token_map],
            token_map.items(),
        ):
            book = next((b for b in books if b.get("asset_id") == tid), None)
            if book:
                snap.orderbook = _parse_polymarket_book(book)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main discovery function
# ---------------------------------------------------------------------------


def discover(
    category: str = "election",
    days: int = 365,
    min_sim: float = 0.28,
    show_prices: bool = False,
    max_poly_offset: int = 3000,
    max_events_to_search: int = 300,
) -> list[dict]:
    """
    Run organic cross-exchange discovery and return matched pairs as dicts.
    """
    import logging
    logging.disable(logging.WARNING)

    from kalshi.client import KalshiClient
    from polymarket.client import PolymarketClient
    from matcher import match_markets

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    fetched_at = now.isoformat()

    # ── 1. Kalshi events catalog ────────────────────────────────────────────
    print(f"[1/5] Fetching Kalshi event catalog…", flush=True)
    kc = KalshiClient()
    t0 = time.time()
    all_events = kc.get_all_events(max_pages=15, page_size=200, status="open")
    print(f"      {len(all_events)} events in {time.time()-t0:.1f}s")

    # ── 2. Filter events ────────────────────────────────────────────────────
    filtered = []
    for ev in all_events:
        if _is_sports(ev):
            continue
        close = _parse_dt(ev.get("close_time") or ev.get("end_date"))
        if close and (close < now or close > horizon):
            continue
        cat = _category(ev)
        if category != "all" and cat != category:
            continue
        filtered.append(ev)

    print(f"[2/5] Filtered to {len(filtered)} {category} events within {days} days")
    if not filtered:
        print("      No events matched. Try --category all or --days 730.")
        return []

    # Limit to avoid too many API calls
    filtered = filtered[:max_events_to_search]

    # ── 3. Fetch Kalshi markets for each event ─────────────────────────────
    print(f"[3/5] Fetching Kalshi markets for {len(filtered)} events…", flush=True)
    t0 = time.time()
    k_snaps = []
    k_keywords: list[str] = []
    for ev in filtered:
        et = ev.get("event_ticker", "")
        try:
            resp = kc.get_markets(limit=50, event_ticker=et, status="open")
            mkts = resp.get("markets", [])
        except Exception:
            continue
        for m in mkts:
            k_snaps.append(_k_snap(m, fetched_at))
        # Derive keywords from event title
        kws = _derive_keywords(ev.get("title", ""))
        k_keywords.extend(kws)

    # Deduplicate keywords, keep most specific (longest first)
    k_keywords = list(dict.fromkeys(kw for kw in k_keywords if len(kw) > 2))
    k_keywords.sort(key=len, reverse=True)
    k_keywords = k_keywords[:60]  # cap Poly search budget

    print(f"      {len(k_snaps)} Kalshi markets in {time.time()-t0:.1f}s")
    print(f"      Derived {len(k_keywords)} Polymarket search keywords")

    if not k_snaps:
        print("      No Kalshi markets found. Exiting.")
        return []

    # ── 4. Search Polymarket ─────────────────────────────────────────────
    print(f"[4/5] Searching Polymarket ({max_poly_offset} market scan + keyword filter)…", flush=True)
    t0 = time.time()
    pc = PolymarketClient()
    p_raw = pc.search_markets(
        keywords=k_keywords,
        active=True,
        closed=False,
        max_offset=max_poly_offset,
    )
    p_snaps = [_p_snap(m, fetched_at) for m in p_raw]
    print(f"      {len(p_snaps)} Polymarket markets in {time.time()-t0:.1f}s")

    if not p_snaps:
        print("      No Polymarket markets found.")
        return []

    # ── 5. Match ────────────────────────────────────────────────────────────
    print(f"[5/5] Running matcher (min_sim={min_sim})…", flush=True)
    pairs = match_markets(
        p_snaps, k_snaps,
        min_title_similarity=min_sim,
        max_close_delta_hours=9999,
    )

    # Optionally enrich with live orderbooks
    if show_prices and pairs:
        print(f"      Fetching live orderbooks for {len(pairs)} pairs…", flush=True)
        _enrich_kalshi([pair.kalshi for pair in pairs])
        _enrich_polymarket([pair.poly for pair in pairs])

    # ── Format output ────────────────────────────────────────────────────────
    results = []
    for pair in sorted(pairs, key=lambda x: -x.confidence):
        pb = pair.poly.orderbook.best_bid
        pa = pair.poly.orderbook.best_ask
        kb = pair.kalshi.orderbook.best_bid
        ka = pair.kalshi.orderbook.best_ask

        # Arb check
        arb_dir = None
        arb_profit = None
        FEE = 0.07
        if pa is not None and kb is not None:
            cost = pa + (1.0 - kb)
            profit = round(1.0 - cost - FEE, 4)
            if profit > 0:
                arb_dir = "poly_yes + kalshi_no"
                arb_profit = profit
        if ka is not None and pb is not None:
            cost = ka + (1.0 - pb)
            profit = round(1.0 - cost - FEE, 4)
            if arb_profit is None or profit > arb_profit:
                arb_dir = "kalshi_yes + poly_no"
                arb_profit = profit

        results.append({
            "confidence": round(pair.confidence, 3),
            "title_sim":  round(pair.title_similarity, 3),
            "close_delta_days": round(pair.close_delta_hours / 24) if pair.close_delta_hours else None,
            "poly_title":  pair.poly.title,
            "poly_id":     pair.poly.market_id,
            "poly_slug":   pair.poly.event_id,
            "poly_close":  (pair.poly.close_time or "")[:10],
            "poly_bid":    pb,
            "poly_ask":    pa,
            "kalshi_title":  pair.kalshi.title,
            "kalshi_ticker": pair.kalshi.market_id,
            "kalshi_close":  (pair.kalshi.close_time or "")[:10],
            "kalshi_bid":  kb,
            "kalshi_ask":  ka,
            "arb_direction": arb_dir,
            "arb_net_profit": arb_profit,
        })

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_results(results: list[dict], show_prices: bool) -> None:
    if not results:
        print("\nNo pairs found.")
        return

    arb_results = [r for r in results if r.get("arb_net_profit", 0) and r["arb_net_profit"] > 0]

    print(f"\n{'═' * 120}")
    print(f"  {len(results)} MATCHED PAIRS   |   {len(arb_results)} with positive arb signal")
    print(f"{'═' * 120}\n")

    for i, r in enumerate(results, 1):
        conf_tag = f"[{r['confidence']:.2f}]"
        arb_tag = f"  ⚡ ARB +{r['arb_net_profit']:.4f} ({r['arb_direction']})" if r.get("arb_net_profit", 0) > 0 else ""

        print(f"  #{i:>3} {conf_tag}  sim={r['title_sim']:.2f}{arb_tag}")
        print(f"        Poly:   {r['poly_title'][:80]}")
        print(f"        Kalshi: {r['kalshi_title'][:80]}")

        if show_prices:
            pb, pa = r.get("poly_bid"), r.get("poly_ask")
            kb, ka = r.get("kalshi_bid"), r.get("kalshi_ask")
            poly_str = f"bid={pb:.3f} ask={pa:.3f}" if pb and pa else "no live CLOB"
            kalshi_str = f"bid={kb:.3f} ask={ka:.3f}" if kb and ka else "no live book"
            print(f"        Prices: Poly [{poly_str}]   Kalshi [{kalshi_str}]")
            print(f"        Close:  Poly={r['poly_close']}   Kalshi={r['kalshi_close']}   Δ={r['close_delta_days']} days")

        print()


def main() -> None:
    p = argparse.ArgumentParser(
        description="Organically discover cross-exchange market pairs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--category", default="election",
                   choices=["election", "economic", "political", "pop", "all"],
                   help="Event category to scan")
    p.add_argument("--days", type=int, default=365,
                   help="Only include markets closing within this many days")
    p.add_argument("--min-sim", type=float, default=0.28,
                   help="Minimum Jaccard title similarity")
    p.add_argument("--show-prices", action="store_true",
                   help="Fetch live orderbooks for matched pairs")
    p.add_argument("--max-events", type=int, default=300,
                   help="Maximum Kalshi events to scan (caps API calls)")
    p.add_argument("--poly-scan", type=int, default=3000,
                   help="Polymarket catalog depth (max offset)")
    p.add_argument("--output", default=None,
                   help="Save matched pairs to this JSON file")
    args = p.parse_args()

    print(f"\nDiscover: category={args.category}  days={args.days}  "
          f"min_sim={args.min_sim}  max_events={args.max_events}\n")

    results = discover(
        category=args.category,
        days=args.days,
        min_sim=args.min_sim,
        show_prices=args.show_prices,
        max_poly_offset=args.poly_scan,
        max_events_to_search=args.max_events,
    )

    _print_results(results, show_prices=args.show_prices)

    if args.output and results:
        Path(args.output).write_text(json.dumps(results, indent=2, default=str))
        print(f"Saved {len(results)} pairs → {args.output}")


if __name__ == "__main__":
    main()

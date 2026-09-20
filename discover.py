"""
Organic Cross-Exchange Market Discovery
========================================
Ingests 100% of both venues' OPEN markets (full Kalshi event catalog + an
orphan-market sweep, full Polymarket event catalog + an orphan-market
sweep), runs the matcher to surface real cross-exchange price comparisons —
with live bid/ask spreads.

No manual slug or series configuration, no keyword search.  Just run it.

Usage
-----
    python discover.py                          # everything open, no horizon cap
    python discover.py --category election      # Senate/House/Governor/primary
    python discover.py --category sports        # NBA/MLB/NFL/NHL/soccer/etc.
    python discover.py --category economic      # Fed/crypto/GDP/CPI/jobs
    python discover.py --category political     # SCOTUS/Trump/legislation/intl
    python discover.py --category pop           # entertainment/celebrity
    python discover.py --days 90                # closing within 90 days
    python discover.py --min-sim 0.30           # stricter title matching
    python discover.py --show-prices            # fetch live orderbooks for pairs
    python discover.py --no-market-sweep        # skip both Kalshi + PM orphan sweeps
    python discover.py --coverage-json cov.json # dump the ingestion accounting
    python discover.py --output pairs.json      # save results to JSON

What gets held out of MATCHING (still ingested — see ingest_kalshi)
---------------------------------------------------------------------
Kalshi stats-only markets (voter turnout, margin of victory, …) and
parlay-format markets ("yes Yankees, yes OKC wins by 4.5, …") are ingested
into the pool for full coverage accounting but tagged
``extra["match_excluded"]`` so the matcher never sees them — they are
structurally incomparable to any Polymarket market. Every other market type
(elections, sports game winners, economic indicators, entertainment,
politics) is matched normally.

Algorithm
---------
1. Ingest the full Kalshi open-market catalog + orphan sweep (ingest_kalshi)
2. Ingest the full Polymarket open-market catalog + orphan sweep (ingest_polymarket)
3. Run the two-level (event -> outcome) Jaccard+close-time matcher
4. Optionally fetch live orderbooks for matched pairs, format and print
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Parlay exclusion — the ONLY filter applied to Kalshi markets
# ---------------------------------------------------------------------------
# Kalshi KXMVE / KXMVECROSS markets bundle multiple props into one contract:
#   "yes Oklahoma City, yes Shohei Ohtani: 1+, no Cubs win by 4.5"
# These are structurally incomparable with Polymarket binary markets.
# All other Kalshi markets — including single-game sports, elections, crypto —
# are kept.

_PARLAY_EVENT_PREFIXES = ("KXMVE",)

_PARLAY_TITLE_RE = re.compile(
    r"^(yes |no )(.*,)*(yes |no )",   # "yes X, yes Y, ..." multi-leg pattern
    re.IGNORECASE,
)

# Kalshi voter-turnout / margin-of-victory markets are numeric range markets
# (e.g. "Will the total vote count be between 800k and 900k?") that have no
# Polymarket equivalent.  Exclude them too.
_STATS_ONLY_RE = re.compile(
    r"(voter turnout|margin of victory|total vote count|vote share|"
    r"win percentage|double double|triple double|quadruple double)",
    re.IGNORECASE,
)


def _kalshi_hold_reason(event: dict) -> str | None:
    """Event-level match-exclusion reason ("mve" / "parlay_title" /
    "stats_only"), or None.

    Markets belonging to these events are still INGESTED into the pool (full
    coverage) but tagged ``extra["match_excluded"] = <reason>`` so discover()
    can hold them out of the matcher only, preserving prior matching
    precision while return_pools still exposes the complete catalog.
    """
    et = (event.get("event_ticker") or "").upper()
    if et.startswith(_PARLAY_EVENT_PREFIXES):
        return "mve"
    title = event.get("title", "")
    if _PARLAY_TITLE_RE.match(title):
        return "parlay_title"
    if _STATS_ONLY_RE.search(title):
        return "stats_only"
    return None


def _is_parlay(event: dict) -> bool:
    """Backward-compatible boolean wrapper around ``_kalshi_hold_reason``."""
    return _kalshi_hold_reason(event) is not None


def _is_parlay_market(market: dict) -> bool:
    """Also filter individual markets with parlay-format titles."""
    title = market.get("title", "")
    if _PARLAY_TITLE_RE.match(title):
        return True
    # "Will X win Y, X win Z, and X win W?" — three or more "win" occurrences
    # signals a multi-leg parlay even when the yes/no prefix is absent.
    if len(re.findall(r"\bwin\b", title, re.IGNORECASE)) >= 3:
        return True
    # Conjunctive parlay: two separate "will <clause>" conditions joined by
    # "and will" ("Will ACA credits not be extended and will the GOP win the
    # House?"). These have no single Kalshi counterpart and otherwise outscore
    # the real single-leg market.
    if re.search(r"\band will\b", title, re.IGNORECASE):
        return True
    return False


# ---------------------------------------------------------------------------
# Category classifiers
# ---------------------------------------------------------------------------

_SPORTS_HINTS = re.compile(
    # Leagues / governing bodies — very unambiguous
    r"(\bnba\b|\bnfl\b|\bmlb\b|\bnhl\b|\bmls\b|\bnascar\b|\bufc\b|"
    r"\bboxing\b|\bmma\b|\btennis\b|\bgolf\b|\bpga\b|"
    r"formula 1|\bf1\b|\bolympics\b|\bncaa\b|"
    r"college football|college basketball|"
    # Championships / rounds
    r"super bowl|world series|stanley cup|nba finals|nba championship|"
    r"\bplayoffs\b|\bplayoff\b|\bchampionship\b|"
    r"premier league|la liga|bundesliga|serie a|champions league|"
    r"\bworld cup\b|\beuro \d{4}\b|\bcopa\b|"
    # NBA teams (unambiguous standalone words)
    r"\blakers\b|\bceltics\b|\bwarriors\b|\bknicks\b|\bbucks\b|"
    r"\bnuggets\b|\bthunder\b|\btimberwolves\b|\bpacers\b|"
    # MLB teams
    r"\byankees\b|\bdodgers\b|\bmets\b|\bcubs\b|\bbraves\b|"
    r"\bastros\b|\bred sox\b|\bpadres\b|"
    # NFL teams — keep only clearly unambiguous ones
    r"\bchiefs\b|\b49ers\b|\bpackers\b|\bravens\b|\bbengals\b|"
    # Known player names (no short/common-word names to avoid false positives)
    r"\blebron\b|\bsteph curry\b|\bkd durant\b|\bgiannis\b|"
    r"\bwembanyama\b|\bjokic\b|\bohtani\b|\bmahomes\b|\bburrow\b|"
    r"\bhurts\b|"
    # Game/match terminology — with word boundaries to avoid 'matchup', 'Seth', 'Dakota'
    r"\bgame \d\b|\bseries\b|\bovertime\b|\binning\b|\bpitching\b|"
    r"\bhalftime\b|\bsoccer\b|\bgoalscorer\b)",
    re.IGNORECASE,
)

_ELECTION_HINTS = re.compile(
    # Party names are strong signals for electoral content
    r"(republican|democrat|democratic\s+party|gop\b|"
    # Electoral process keywords (unambiguous)
    r"\bnominee\b|\bprimary\b|\brunoff\b|\bcaucus\b|\bmidterm\b|"
    r"\belection\b|\belectoral\b|\bballot\b|\bcandidate\b|"
    # Senate/House/Congress in ELECTORAL context only (compound phrases)
    r"senate\s+(race|seat|primary|election|runoff|candidate)|"
    r"house\s+(race|seat|district|primary|election|candidate)|"
    r"house\s+of\s+representatives|"
    r"congressional\s+(race|primary|district|election|seat)|"
    # Governor / state-level offices
    r"\bgubernatorial\b|governor\s+(race|primary|election)|"
    r"attorney\s+general|secretary\s+of\s+state|lieutenant\s+governor|"
    # Local offices
    r"mayor\s+(race|primary|election)|\bmayoral\b|\balderman\b|\bcomptroller\b|"
    r"special\s+election)",
    re.IGNORECASE,
)

_ECONOMIC_HINTS = re.compile(
    r"(\bfed\b|federal\s+reserve|federal\s+funds|interest\s+rate|fomc|"
    r"bitcoin|\bbtc\b|ethereum|\beth\b|solana|"
    r"\bgdp\b|\bcpi\b|inflation|jobs\s+report|unemployment|payroll|nonfarm|"
    r"s&p\s*500|nasdaq|dow\s+jones|oil\s+price|gold\s+price|gas\s+price|"
    r"tariff|trade\s+deficit|treasury|bond\s+yield|mortgage\s+rate|"
    r"earnings|revenue|market\s+cap|\bipo\b|merger|acquisition)",
    re.IGNORECASE,
)

_POLITICAL_HINTS = re.compile(
    r"(scotus|supreme court|trump|biden|harris|president|white house|"
    r"congress|senate|legislation|bill|impeach|resign|cabinet|nato|"
    r"ukraine|russia|china|taiwan|israel|iran|north korea|"
    r"executive order|veto|pardon|indictment|conviction|sanction|"
    r"diplomacy|treaty|ceasefire|invasion|coup)",
    re.IGNORECASE,
)


def _category(event: dict) -> str:
    title = event.get("title", "")
    # Election/political checks take priority — prevents NFL "Bills" / "race" /
    # player-name substrings from mis-classifying Senate/House/Governor events.
    if _ELECTION_HINTS.search(title):
        return "election"
    if _ECONOMIC_HINTS.search(title):
        return "economic"
    if _POLITICAL_HINTS.search(title):
        return "political"
    if _SPORTS_HINTS.search(title):
        return "sports"
    return "pop"


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

_QUESTION_PREFIX = re.compile(
    r"^(will|which|who|who will|when will|what|how|does|is|are|can|"
    r"should|did|was|were|has|have)\s+",
    re.IGNORECASE,
)

_GENERIC_FILLER = re.compile(
    r"\b(the|a|an|be|to|of|in|on|at|by|for|or|and|but|not|that|this|"
    r"their|they|it|its|he|she|we|you|i|am|is|are|was|were|"
    r"have|has|had|do|does|did|will|would|could|should|may|might|"
    r"more|less|most|least|very|really|just|only|even|still|yet|"
    r"before|after|during|between|within|until|since|once|already|"
    r"again|also|then|when|where|how|why|whether|either|both|"
    r"any|all|each|every|some|other|another|same|first|last|next|"
    r"new|old|big|small|high|low|long|short|early|late|"
    r"happen|occur|reach|pass|become|get|go|come|run|make|take|"
    r"win|lose|beat|score|play|start|end|finish)\b",
    re.IGNORECASE,
)

# Proper-noun pairs (e.g. "Thomas Massie", "LeBron James", "New Hampshire")
_PROPER_PAIR_RE = re.compile(r"\b([A-Z][a-z]{1,15} [A-Z][a-z]{1,15})\b")

# District codes: "KY-4", "MD-06", "TX-35"
_DISTRICT_RE = re.compile(r"\b([A-Z]{2})-?(\d{1,2})\b")

# Game numbers: "Game 4", "Round 2"
_GAME_NUM_RE = re.compile(r"\b(game|round|series|match|leg)\s*(\d)\b", re.IGNORECASE)

# Sports team / league shorthands
_LEAGUE_RE = re.compile(r"\b(NBA|NFL|MLB|NHL|MLS|NCAA|UFC|PGA|F1)\b")


def _derive_keywords(title: str) -> list[str]:
    """
    Extract the most specific search terms from a Kalshi event title.

    Priority:
      1. Proper-noun pairs (candidate/player/place names)
      2. League + game number combos
      3. District codes
      4. Longest meaningful tokens after stopword removal
    """
    keywords: list[str] = []

    # Strip the leading question word ("Will", "Who will", ...) before proper-noun
    # extraction so it isn't captured as part of a name pair — e.g. "Will Donald
    # Trump win ..." must yield "Donald Trump", not "Will Donald", which would
    # weaken the cross-venue search query.
    proper_src = _QUESTION_PREFIX.sub("", title)

    # 1. Proper noun pairs — most specific signal
    for m in _PROPER_PAIR_RE.finditer(proper_src):
        kw = m.group(1)
        # Skip generic two-word phrases
        if not any(skip in kw.lower() for skip in ("which ", "that ", "this ")):
            keywords.append(kw)

    # 2. League + game number (e.g. "NBA Finals game 4")
    league = _LEAGUE_RE.search(title)
    game = _GAME_NUM_RE.search(title)
    if league and game:
        keywords.append(f"{league.group(1)} {game.group(0).lower()}")

    # 3. District codes
    for m in _DISTRICT_RE.finditer(title):
        state, num = m.group(1), m.group(2).lstrip("0") or "0"
        keywords.append(f"{state}-{num}")
        keywords.append(f"{state}-{num.zfill(2)}")

    if keywords:
        return list(dict.fromkeys(keywords))[:4]

    # 4. Fallback: remove stopwords, keep 3 longest tokens
    cleaned = _QUESTION_PREFIX.sub("", title)
    cleaned = _GENERIC_FILLER.sub(" ", cleaned)
    tokens = [t for t in re.split(r"\W+", cleaned) if len(t) > 3]
    tokens.sort(key=len, reverse=True)
    return list(dict.fromkeys(tokens[:3]))


# ---------------------------------------------------------------------------
# Snapshot builders (no orderbook fetch — speed pass first)
# ---------------------------------------------------------------------------

def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    iso = s.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.split("+")[0].rstrip("Zz"), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# Series whose events are ALWAYS scanned, even when they close later than the
# close-time-sorted max_events_to_search cap would keep. These carry rich, REAL,
# non-political cross-platform arbs that close far out (2027) and were therefore
# truncated by the cap — verified examples: "Which companies will the US take a
# stake in" (KXUSACOMPANYSTAKE — Freeport-McMoRan ~7.4c), "Which bills will become
# law" (KXBILLS — FISA-702 ~23c, Housing-21st-Century-Act ~6.8c), "Who will IPO
# before 2027" (KXIPO — Applied Intuition ~6c, Mistral). Each series is a handful
# of events, so the cap's scan-time guarantee is preserved. (run 78)
_ALWAYS_INCLUDE_SERIES = frozenset({
    "KXUSACOMPANYSTAKE", "KXBILLS", "KXIPO",
})


def _event_series(ev: dict) -> str:
    return ev.get("series_ticker") or (ev.get("event_ticker", "") or "").split("-")[0]


def _apply_event_cap(filtered: list, cap: int | None) -> list:
    """Truncate the (close-time-sorted) event list to ``cap`` events, but always
    retain events from ``_ALWAYS_INCLUDE_SERIES`` even if they fall past the cap."""
    if cap is None or len(filtered) <= cap:
        return filtered
    kept = filtered[:cap]
    extra = [e for e in filtered[cap:] if _event_series(e) in _ALWAYS_INCLUDE_SERIES]
    return kept + extra


def _event_close(ev: dict) -> datetime | None:
    """Close time of a Kalshi event.

    ``/events`` rows carry no ``close_time``, so without nested markets the
    horizon filter and close-time sort were silent no-ops. Prefer an explicit
    event field; otherwise use the LATEST nested market close (the event is
    live until its last market closes).
    """
    close = _parse_dt(ev.get("close_time") or ev.get("end_date"))
    if close:
        return close
    closes = [c for c in (_parse_dt(m.get("close_time") or m.get("expiration_time"))
                          for m in ev.get("markets") or []) if c]
    return max(closes) if closes else None


def _k_snap(m: dict, fetched_at: str, event_title: str = "", series_ticker: str = ""):
    from pipeline import MarketSnapshot, _parse_kalshi_top_of_book, kalshi_market_title
    from contract_spec import settlement_source
    ob = _parse_kalshi_top_of_book(m)
    close = m.get("close_time") or m.get("expiration_time")
    # Settlement-source tag extracted from rules_primary ONLY (the actual
    # settlement sentence, e.g. "...according to The Weather Company") —
    # rules_secondary is deliberately excluded: it names non-authoritative
    # comparison sources ("checking AccuWeather... may help guide your
    # decision") that would otherwise be misread as the settlement provider.
    # The rules text itself is never kept — see settlement_source()'s
    # docstring in contract_spec.py for the memory rationale.
    settle_src = settlement_source(m.get("rules_primary") or "")
    return MarketSnapshot(
        source="kalshi",
        market_id=m.get("ticker", ""),
        event_id=m.get("event_ticker", ""),
        title=kalshi_market_title(m),
        status=m.get("status", ""),
        close_time=close,
        fetched_at=fetched_at,
        orderbook=ob,
        extra={
            "event_title": event_title,
            "settle_src": settle_src,
            "series_ticker": series_ticker,
            "yes_sub_title": m.get("yes_sub_title") or "",
        },
    )


def _catalog_bid_ask(m: dict) -> tuple[float | None, float | None]:
    """Gamma's own top-of-book (``bestBid``/``bestAsk``) for a Polymarket market row.

    These are REAL bid/ask (unlike ``outcomePrices``, which is a single mid used
    for both sides of the snapshot's main orderbook so the matcher's price_sim
    keeps working). Stashed in ``extra`` — never in the main orderbook — so the
    catalog-price screen in discover() can compare against Kalshi's real
    catalog top-of-book without disturbing price_sim or the depth filter (a
    non-enriched pair's orderbook sizes must stay 0/None; see discover()'s
    enrich_margin docs).
    """
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return _f(m.get("bestBid")), _f(m.get("bestAsk"))


def _p_snap(m: dict, fetched_at: str):
    """Build a Polymarket snapshot from a raw /markets result (legacy path)."""
    from pipeline import MarketSnapshot, OrderBook, PriceLevel
    from contract_spec import settlement_source
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
    # For categorical markets, groupItemTitle is the short outcome label
    # (e.g. "France", "Ken Paxton", "No change").  For binary markets it is
    # absent, so fall back to the full question text.
    outcome_label = m.get("groupItemTitle") or m.get("question") or m.get("title", "")
    group_slug = m.get("groupSlug") or m.get("slug", "")
    catalog_bid, catalog_ask = _catalog_bid_ask(m)
    # Compact settlement-source tag from description + resolutionSource —
    # neither text is kept (see settlement_source()'s docstring).
    settle_src = settlement_source(m.get("description") or "", m.get("resolutionSource") or "")
    return MarketSnapshot(
        source="polymarket",
        market_id=m.get("conditionId") or m.get("id", ""),
        event_id=group_slug,
        title=outcome_label,
        status="open" if m.get("active") else "closed",
        close_time=close,
        fetched_at=fetched_at,
        orderbook=ob,
        extra={
            "clob_token_ids": tids,
            "outcomes": m.get("outcomes"),
            "event_title": m.get("groupTitle", ""),
            "full_question": m.get("question", ""),
            "catalog_bid": catalog_bid,
            "catalog_ask": catalog_ask,
            "settle_src": settle_src,
        },
    )


def _p_snap_from_event(m: dict, ev_title: str, ev_slug: str, fetched_at: str):
    """Build a Polymarket snapshot from a market embedded in a /events response.

    Uses the parent event's title and slug for group-level matching, and
    groupItemTitle (short outcome label like "France") for outcome matching.
    """
    from pipeline import MarketSnapshot, OrderBook, PriceLevel
    from contract_spec import settlement_source
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
    outcome_label = m.get("groupItemTitle") or m.get("question") or m.get("title", "")
    close = m.get("endDate") or m.get("endDateIso")
    catalog_bid, catalog_ask = _catalog_bid_ask(m)
    # Compact settlement-source tag from description + resolutionSource —
    # neither text is kept (see settlement_source()'s docstring).
    settle_src = settlement_source(m.get("description") or "", m.get("resolutionSource") or "")
    return MarketSnapshot(
        source="polymarket",
        market_id=m.get("conditionId") or m.get("id", ""),
        event_id=ev_slug,
        title=outcome_label,
        status="open" if m.get("active") else "closed",
        close_time=close,
        fetched_at=fetched_at,
        orderbook=ob,
        extra={
            "clob_token_ids": tids,
            "outcomes": m.get("outcomes"),
            "event_title": ev_title,
            "full_question": m.get("question", ""),
            "catalog_bid": catalog_bid,
            "catalog_ask": catalog_ask,
            "settle_src": settle_src,
            # Structured sports join (sports_match.py)
            "sports_market_type": m.get("sportsMarketType"),
            "game_start_time": m.get("gameStartTime"),
            "market_slug": m.get("slug", ""),
            "outcome_labels": _json_list(m.get("outcomes")),
        },
    )


def _json_list(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return []
    return list(v) if isinstance(v, (list, tuple)) else []


# ---------------------------------------------------------------------------
# Live orderbook enrichment (only for confirmed pairs)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Small on-disk cache — used only for the (small) orphan-market subset of
# each venue (see ingest_kalshi/ingest_polymarket(sweep_cache_ttl=...)).
# There is no catalog cache: both venues' event catalogs are always walked
# fresh every cycle.
#
# Stale-while-revalidate (iteration 4): a long-running alerter process's
# COLD cycle (no cache at all) is bounded below by the Polymarket orphan
# sweep (~240-284s at 100 rows/page). Once ANY cache exists — even past its
# nominal ``sweep_cache_ttl`` — a cycle should never block on the sweep
# again: an expired-but-not-too-old cache is served immediately (tagged
# "stale" in coverage) while a background thread refreshes it for the NEXT
# cycle. Only a cache older than ``_SWR_HARD_MAX_AGE`` (or no cache at all)
# is treated as absent and paid for inline. See
# docs/history/COVERAGE_ITERATIONS.md, iteration 4.
# ---------------------------------------------------------------------------

_CACHE_DIR = Path(__file__).parent / ".cache"
_SWR_HARD_MAX_AGE = 6 * 3600  # a cache this old (or older) is treated as absent

_SWR_LOCK = threading.Lock()
_SWR_IN_PROGRESS: set[str] = set()  # cache names with a background refresh in flight


def _cache_load(name: str, ttl: int):
    if ttl <= 0:
        return None
    path = _CACHE_DIR / name
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - obj.get("fetched_at", 0) <= ttl:
                return obj.get("data")
    except Exception:
        pass
    return None


def _cache_load_any(name: str) -> tuple[Any, float]:
    """Return ``(data, age_seconds)`` for a cache file regardless of any TTL,
    or ``(None, inf)`` if the file is absent/corrupt. Used for
    stale-while-revalidate: the caller decides freshness against its own
    ``sweep_cache_ttl`` / ``_SWR_HARD_MAX_AGE``."""
    path = _CACHE_DIR / name
    try:
        if path.exists():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj.get("data"), time.time() - obj.get("fetched_at", 0)
    except Exception:
        pass
    return None, float("inf")


def _cache_store(name: str, data) -> None:
    """Write ``data`` to the on-disk cache atomically: a temp file, then
    ``os.replace`` (atomic on POSIX and Windows). Without this, a reader
    (this process's next cycle, or a background refresh racing a foreground
    fetch) could observe a half-written JSON file and crash mid-cycle."""
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        path = _CACHE_DIR / name
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "data": data}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def _maybe_refresh_in_background(name: str, refresh_fn) -> None:
    """Kick a one-shot background refresh of cache ``name`` via
    ``refresh_fn()`` (which returns the new data, or ``None`` on failure)
    unless a refresh for this name is already in flight (guards against two
    overlapping refreshes clobbering each other / doubling the network
    cost). Never blocks the caller.

    In ``--once`` mode the process exits right after the cycle, so this
    daemon thread may not finish before the process dies — it is simply
    abandoned (not joined). That is an accepted trade-off: joining would
    defeat the entire point of stale-while-revalidate (the cycle would
    block exactly as before), and letting it die loses nothing — the stale
    cache on disk is untouched and still perfectly usable next run. Only a
    cache past ``_SWR_HARD_MAX_AGE`` forces an inline (blocking) refresh,
    including under ``--once`` — see the callers in ingest_kalshi /
    ingest_polymarket."""
    with _SWR_LOCK:
        if name in _SWR_IN_PROGRESS:
            return
        _SWR_IN_PROGRESS.add(name)

    def _bg():
        try:
            data = refresh_fn()
            if data is not None:
                _cache_store(name, data)
        except Exception:
            pass
        finally:
            with _SWR_LOCK:
                _SWR_IN_PROGRESS.discard(name)

    threading.Thread(target=_bg, daemon=True).start()


def _run_market_sweep(cache_ttl: int = 0) -> dict:
    """Run (or reuse a cached) Polymarket orphan sweep.

    Walking the full ``/markets/keyset` catalog (~166k open rows, ~290s at
    100 rows/page) is the only way to reach markets whose parent event is
    archived/inactive/closed and therefore invisible to ``/events/keyset``
    (~480 live). Intended to run in a background thread started as early as
    possible (see ``discover()``) so its cost overlaps Kalshi ingestion and
    the Polymarket event walk instead of adding on top.

    Returns ``{"raw": list[dict] | None, "complete": bool, "from_cache":
    bool, "elapsed": float}``. ``raw`` is the full sweep (fresh run) or the
    previously-computed ORPHAN subset (cache hit) — either shape is safe to
    feed through the same active/closed/seen_ids filter in
    ``ingest_polymarket``. ``raw`` is None only when the fresh fetch failed.
    """
    t0 = time.time()
    if cache_ttl > 0:
        cached = _cache_load("pm_orphans.json", cache_ttl)
        if cached is not None:
            return {"raw": cached, "complete": True, "from_cache": True,
                    "elapsed": time.time() - t0}
    from polymarket.client import PolymarketClient
    try:
        client = PolymarketClient()
        raw = client.get_all_markets_keyset(closed=False)
        complete = getattr(client, "last_scan_complete", True)
    except Exception:
        raw = None
        complete = False
    return {"raw": raw, "complete": complete, "from_cache": False,
            "elapsed": time.time() - t0}


def _run_kalshi_market_sweep(cache_ttl: int = 0) -> dict:
    """Run (or reuse a cached) Kalshi orphan-market sweep.

    Root cause (verified live): Kalshi's ``/events`` listing is inconsistent
    with ``/markets`` — some events whose markets are ``active`` are absent
    from ``/events`` under ANY status filter (e.g. an event's markets are
    live and tradeable, yet the event itself never appears in a full
    ``/events`` walk, paginated or not), so the nested-event ingestion in
    ``ingest_kalshi`` can never reach their markets no matter how it's tuned.
    This walks ``/markets?status=open&mve_filter=exclude`` directly instead
    (bypassing ``/events`` entirely) — ground truth, ~106k markets in ~20s at
    page_size=1000 — to recover those markets as "orphans" in
    ``ingest_kalshi``.

    Intended to run in a background thread started as early as possible (see
    ``discover()``) so its ~20s cost (up to ~57s under contention with the
    Polymarket sweep) overlaps Kalshi event ingestion instead of adding on
    top.

    ``cache_ttl`` mirrors ``_run_market_sweep``'s (Polymarket) caching: a
    cache hit returns the previously-computed ORPHAN CANDIDATE subset (not
    the full ~106k-row walk) under ``"raw"`` — safe to feed through the same
    ticker/event_ticker filter in ``ingest_kalshi`` as a fresh full sweep,
    since that filter re-checks against the CURRENT cycle's ingested
    tickers/events regardless of where the candidates came from. The (tiny)
    orphan subset is cached by ``ingest_kalshi`` itself, once the full raw
    walk has been filtered down — see its ``sweep_cache_ttl`` handling.

    Returns ``{"raw": list[dict] | None, "complete": bool, "from_cache":
    bool, "elapsed": float}``. ``raw`` is None only when the fresh fetch
    failed outright.
    """
    t0 = time.time()
    if cache_ttl > 0:
        cached = _cache_load("kalshi_orphans.json", cache_ttl)
        if cached is not None:
            return {"raw": cached, "complete": True, "from_cache": True,
                    "elapsed": time.time() - t0}
    from kalshi.client import KalshiClient
    try:
        client = KalshiClient()
        raw = client.get_all_markets(status="open", mve_filter="exclude", page_size=1000)
        complete = getattr(client, "last_scan_complete", True)
    except Exception:
        raw = None
        complete = False
    return {"raw": raw, "complete": complete, "from_cache": False,
            "elapsed": time.time() - t0}


def _sweep_cache_state(name: str, sweep_cache_ttl: int) -> tuple[bool, bool]:
    """Return ``(usable, stale)`` for the on-disk orphan cache ``name``.

    ``usable=False`` means: caching is off (``sweep_cache_ttl<=0``), there is
    no cache file, or the cache is older than ``_SWR_HARD_MAX_AGE`` — in all
    three cases the caller must pay for a real (inline) sweep this cycle.
    ``usable=True, stale=False`` is a plain cache hit within its TTL.
    ``usable=True, stale=True`` is stale-while-revalidate: old enough to have
    exceeded ``sweep_cache_ttl`` but young enough to still serve, while a
    background refresh is kicked off for the next cycle."""
    if sweep_cache_ttl <= 0:
        return False, False
    data, age = _cache_load_any(name)
    if data is None:
        return False, False
    if age <= sweep_cache_ttl:
        return True, False
    if age <= _SWR_HARD_MAX_AGE:
        return True, True
    return False, False  # past the hard cap -> treat exactly like no cache


def _kalshi_orphan_refresh(ingested_tickers: set, processed_event_tickers: set):
    """Background-refresh body for ``kalshi_orphans.json``: run a fresh full
    sweep and reduce it to the same orphan-candidate shape ingest_kalshi
    would compute on a cold cache miss, using the ticker/event sets captured
    (by value) from the cycle that triggered the refresh. Returns ``None`` on
    fetch failure so ``_maybe_refresh_in_background`` leaves the old cache
    file untouched rather than overwriting it with nothing."""
    result = _run_kalshi_market_sweep(0)
    raw = result.get("raw")
    if raw is None:
        return None
    candidates = []
    for m in raw:
        status = m.get("status")
        if status not in (None, "", "active", "open"):
            continue
        ticker = m.get("ticker", "")
        if not ticker or ticker in ingested_tickers:
            continue
        et = m.get("event_ticker", "")
        if et in processed_event_tickers:
            continue
        candidates.append(m)
    return candidates


def _pm_orphan_refresh(seen_ids: set):
    """Background-refresh body for ``pm_orphans.json`` — the Polymarket
    analogue of ``_kalshi_orphan_refresh``: a fresh full ``/markets/keyset``
    sweep reduced to live, not-already-seen markets."""
    result = _run_market_sweep(0)
    raw = result.get("raw")
    if raw is None:
        return None
    local_seen = set(seen_ids)
    orphan_rows = []
    for m in raw:
        cid = m.get("conditionId") or m.get("id", "")
        if cid in local_seen or m.get("closed") or not m.get("active"):
            continue
        local_seen.add(cid)
        orphan_rows.append(m)
    return orphan_rows


def _enrich_kalshi(snaps: list) -> None:
    """Fetch live Kalshi orderbooks for matched pairs, via the batch endpoint.

    Uses ``KalshiClient.get_orderbooks`` (``GET /markets/orderbooks``,
    repeated ``tickers`` params, ≤100/request, ~0.08s/request) instead of
    one ``GET /markets/{ticker}/orderbook`` per pair — verified live to
    return byte-identical ``orderbook_fp`` payloads (see
    ``KalshiClient.get_orderbooks`` docstring and
    ``tests/test_kalshi_orderbooks_batch.py``). Chunks are fetched from a
    few threads in parallel (one client per thread; requests.Session is not
    guaranteed thread-safe).

    Any ticker a chunk failed to return (its own request errored, or the
    venue simply omitted it) is re-fetched individually via
    ``get_orderbook`` as a fallback, so per-pair coverage matches the old
    per-ticker behaviour. Failures (batch AND per-ticker fallback) leave the
    snapshot's catalog top-of-book intact — nothing is ever cleared.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from kalshi.client import KalshiClient
    from pipeline import _parse_kalshi_full_book

    if not snaps:
        return

    by_ticker: dict[str, list] = {}
    for snap in snaps:
        by_ticker.setdefault(snap.market_id, []).append(snap)
    tickers = list(by_ticker.keys())

    _tl = threading.local()

    def _client() -> KalshiClient:
        client = getattr(_tl, "kc", None)
        if client is None:
            client = _tl.kc = KalshiClient()
        return client

    chunk_size = KalshiClient.MAX_ORDERBOOKS_PER_REQUEST
    chunks = [tickers[i : i + chunk_size] for i in range(0, len(tickers), chunk_size)]

    books: dict[str, dict] = {}
    lock = threading.Lock()

    def fetch_chunk(chunk: list[str]) -> None:
        try:
            result = _client().get_orderbooks(chunk)
        except Exception:
            result = {}
        with lock:
            books.update(result)

    workers = max(1, min(8, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch_chunk, chunks))

    missing = [t for t in tickers if t not in books]
    if missing:
        def fetch_one(ticker: str) -> None:
            try:
                raw = _client().get_orderbook(ticker)
            except Exception:
                return
            with lock:
                books[ticker] = raw

        workers = max(1, min(8, len(missing)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(fetch_one, missing))

    for ticker, raw in books.items():
        for snap in by_ticker.get(ticker, []):
            try:
                snap.orderbook = _parse_kalshi_full_book(raw)
            except Exception:
                pass


def _enrich_polymarket(snaps: list) -> None:
    from polymarket.client import PolymarketClient
    from pipeline import _parse_polymarket_book
    client = PolymarketClient()
    # Batch by groups of 500 (CLOB /books limit)
    token_order: list[tuple[Any, str]] = []
    for snap in snaps:
        tids = snap.extra.get("clob_token_ids") or []
        if tids:
            token_order.append((snap, tids[0]))
    if not token_order:
        return
    BATCH = 500
    for i in range(0, len(token_order), BATCH):
        batch = token_order[i : i + BATCH]
        try:
            books = client._post(
                "https://clob.polymarket.com",
                "/books",
                json_body=[{"token_id": tid} for _, tid in batch],
            )
            tid_to_book = {b.get("asset_id"): b for b in books}
            for snap, tid in batch:
                book = tid_to_book.get(tid)
                if book:
                    snap.orderbook = _parse_polymarket_book(book)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Two-level hierarchical matcher
# ---------------------------------------------------------------------------

# Economic-outcome direction synonyms: normalise before Jaccard so
# "cut"/"decrease"/"lower" all collapse to "cut", etc.
_DIRECTION_SYNONYMS: dict[str, str] = {
    "cut": "cut", "cuts": "cut", "cutting": "cut",
    "decrease": "cut", "decreases": "cut", "decreased": "cut",
    "lower": "cut", "lowering": "cut",
    "reduce": "cut", "reduction": "cut",
    "hike": "hike", "hikes": "hike", "hiking": "hike",
    "increase": "hike", "increases": "hike", "increased": "hike",
    "raise": "hike", "raising": "hike",
    "hold": "hold", "holds": "hold",
    "maintain": "hold", "maintains": "hold",
    "unchanged": "hold",
    "pause": "hold", "steady": "hold",
    "no change": "hold",
}


@functools.lru_cache(maxsize=1 << 20)
def _normalise_tokens(title: str) -> frozenset[str]:
    """Like matcher._tokens but with bps-split and direction synonym folding."""
    from matcher import _tokens
    # Split "25bps" → "25 bps" before standard tokenisation
    title = re.sub(r"(\d+)\s*bps\b", lambda m: m.group(1) + " bps", title, flags=re.IGNORECASE)
    toks = _tokens(title)
    return frozenset(_DIRECTION_SYNONYMS.get(t, t) for t in toks)


def _match_outcomes_within_group(
    k_outcomes: list,
    p_outcomes: list,
    group_sim: float,
) -> list:
    """
    Pair individual outcomes within a matched event group.

    Scoring combines three signals:
    - title_sim:  Jaccard on short outcome labels ("France" ↔ "France" → 1.0)
    - price_sim:  how close the catalogue mid-prices are (strong signal when
                  labels differ, e.g. "No change" ↔ "Fed maintains rate")
    - group_sim:  confidence that the parent events were correctly matched

    Returns a list of MatchedPair objects (already deduplicated 1-to-1).
    """
    from matcher import (
        _jaccard,
        MatchedPair,
        _close_delta_hours,
        is_close_time_compatible,
        is_compatible_match,
    )

    scored = []
    for k in k_outcomes:
        k_toks = _normalise_tokens(k.title)
        # Kalshi's yes_sub_title IS the outcome label ("Dividend", "Denver");
        # the title is the whole question. Compare label to label when we can —
        # the question's extra words otherwise sink a correct pair under the
        # 0.15 floor below (Costco earnings words, NFL best/worst record).
        k_sub = (k.extra or {}).get("yes_sub_title") or ""
        k_sub_toks = _normalise_tokens(k_sub) if k_sub else frozenset()
        k_mid = k.orderbook.mid or k.orderbook.best_bid
        for p in p_outcomes:
            p_toks = _normalise_tokens(p.title)
            title_sim = _jaccard(k_toks, p_toks)
            # Label-to-label only for NAMED outcomes: on numeric ladders ("Below
            # 5.21%", "25 bps increase") a lifted label score lets the price-led
            # mode below pick an adjacent rung ("below 5.22%").
            if k_sub_toks and not any(ch.isdigit() for ch in p.title + k_sub):
                title_sim = max(title_sim, _jaccard(k_sub_toks, p_toks))
            # Price proximity is useful only after the two outcomes share some
            # lexical evidence.  Without this floor, a categorical Polymarket
            # outcome like "Andy Beshear" can match a generic Kalshi question
            # such as "Who will win the next presidential election?" solely
            # because their catalogue prices happen to be close.
            # (Checked before the compatibility vetoes: same outcome, far cheaper.)
            if title_sim < 0.15:
                continue
            if not is_compatible_match(p, k):
                continue
            if not is_close_time_compatible(p, k):
                continue
            p_mid = p.orderbook.mid or p.orderbook.best_bid

            if k_mid is not None and p_mid is not None and k_mid > 0 and p_mid > 0:
                # Within a matched event, a small price difference is a very
                # strong signal.  Scale so 0.15 difference → 0 score.
                price_sim = max(0.0, 1.0 - abs(k_mid - p_mid) / 0.15)
            else:
                price_sim = 0.0

            # Two scoring modes: title-led or price-led (take the higher)
            title_led = 0.70 * title_sim + 0.30 * group_sim
            price_led = 0.50 * price_sim + 0.30 * group_sim + 0.20 * title_sim
            combined = max(title_led, price_led)

            scored.append((combined, p, k, title_sim))

    # Tie-break by (poly market_id, kalshi market_id) so greedy selection is
    # independent of PYTHONHASHSEED-dependent set-iteration order.
    scored.sort(key=lambda x: (-x[0], x[1].market_id, x[2].market_id))
    pairs: list = []
    used_p: set[str] = set()
    used_k: set[str] = set()

    for combined, p, k, title_sim in scored:
        if p.market_id in used_p or k.market_id in used_k:
            continue
        if combined < 0.35:
            break
        pairs.append(MatchedPair(
            poly=p,
            kalshi=k,
            title_similarity=title_sim,
            close_delta_hours=_close_delta_hours(p.close_time, k.close_time),
            confidence=combined,
        ))
        used_p.add(p.market_id)
        used_k.add(k.market_id)

    return pairs


_CONTEXT_GATE = 0.30


def _context_similarity(p, k) -> float:
    """Jaccard over "<title> <event title>" of both sides."""
    from matcher import _jaccard, _tokens
    pe = (p.extra or {}).get("event_title") or ""
    ke = (k.extra or {}).get("event_title") or ""
    return _jaccard(_tokens(f"{p.title} {pe}"), _tokens(f"{k.title} {ke}"))


def _match_groups_then_individual(
    k_snaps: list,
    p_snaps: list,
    min_sim: float,
) -> list:
    """
    Primary matching strategy.

    Step 1 — Event-group matching (handles categorical markets):
      Group Kalshi markets by event_ticker (all outcomes of one Kalshi event
      share the same event_ticker and event_title).  Group Polymarket markets
      by their event_id (groupSlug or slug).  Match K-event-groups to
      P-event-groups by Jaccard on their event titles, then call
      _match_outcomes_within_group() for each matched pair.

    Step 2 — Individual Jaccard fallback:
      Any markets not consumed by Step 1 are matched with the standard
      match_markets() call (works well for binary markets and sports).
    """
    from matcher import _tokens, _jaccard, match_markets, _token_frequencies, _prefix_tokens, _length_compatible

    # ── Group by event ────────────────────────────────────────────────────────
    k_groups: dict[str, list] = {}
    for s in k_snaps:
        k_groups.setdefault(s.event_id, []).append(s)

    p_groups: dict[str, list] = {}
    for s in p_snaps:
        p_groups.setdefault(s.event_id, []).append(s)

    # Event titles: prefer stored event_title, fall back to the market title
    # itself (works for single-market binary events where title == event title)
    k_etitles = {
        eid: (snaps[0].extra.get("event_title") or snaps[0].title)
        for eid, snaps in k_groups.items()
    }
    p_etitles = {
        eid: (snaps[0].extra.get("event_title") or snaps[0].title)
        for eid, snaps in p_groups.items()
    }
    p_etoks = {eid: _tokens(title) for eid, title in p_etitles.items()}
    k_etoks = {eid: _tokens(title) for eid, title in k_etitles.items()}

    # PERFORMANCE — candidate blocking via prefix filtering (see the module
    # note above matcher._jaccard). Unlike match_markets, event-group matching
    # here is a PURE Jaccard >= min_sim gate with no below-gate acceptance
    # path, so prefix filtering can replace the old any-shared-token blocking
    # outright: it is provably exact for "could this pair reach min_sim" and
    # produces a (typically much smaller) superset of the true matches, with
    # zero risk of a false negative. A cheap length filter additionally skips
    # exact-Jaccard evaluation for pairs whose sizes alone rule them out.
    freq = _token_frequencies(list(p_etoks.values()) + list(k_etoks.values()))
    p_prefix_index: dict[str, set[str]] = {}
    for p_eid, toks in p_etoks.items():
        for tok in _prefix_tokens(toks, freq, min_sim):
            p_prefix_index.setdefault(tok, set()).add(p_eid)

    # ── Step 1: match event groups ────────────────────────────────────────────
    event_scores: list[tuple[float, str, str]] = []
    for k_eid, k_toks in k_etoks.items():
        if not k_toks:
            continue
        candidate_p_eids: set[str] = set()
        for tok in _prefix_tokens(k_toks, freq, min_sim):
            candidate_p_eids.update(p_prefix_index.get(tok, ()))
        for p_eid in candidate_p_eids:
            p_toks = p_etoks[p_eid]
            if not _length_compatible(len(k_toks), len(p_toks), min_sim):
                continue
            sim = _jaccard(k_toks, p_toks)
            if sim >= min_sim:
                event_scores.append((sim, k_eid, p_eid))

    # Tie-break by (k_eid, p_eid) — deterministic regardless of
    # PYTHONHASHSEED-dependent set-iteration order.
    event_scores.sort(key=lambda x: (-x[0], x[1], x[2]))
    matched_k_events: set[str] = set()
    matched_p_events: set[str] = set()
    used_k: set[str] = set()
    used_p: set[str] = set()
    all_pairs: list = []

    # Score outcome pairs for EVERY candidate event pairing, then assign greedily
    # at MARKET level (each market used once). An event-level 1-1 (or capped)
    # assignment stranded outcomes whenever a venue lists one race several ways
    # ("CA-07 House Election Winner" and "... (by individual)", a statewide race
    # alongside its county sub-events): the wrong duplicate claimed the event and
    # its outcomes never got a second chance. Market-level greedy keeps the best
    # scoring pair for each market regardless of which event pairing produced it.
    scored_outcomes: list = []
    for event_sim, k_eid, p_eid in event_scores:
        for pair in _match_outcomes_within_group(k_groups[k_eid], p_groups[p_eid], event_sim):
            scored_outcomes.append((pair.confidence, k_eid, p_eid, pair))
    scored_outcomes.sort(key=lambda x: (-x[0], x[3].kalshi.market_id, x[3].poly.market_id))

    for _conf, k_eid, p_eid, pair in scored_outcomes:
        if pair.kalshi.market_id in used_k or pair.poly.market_id in used_p:
            continue
        all_pairs.append(pair)
        used_k.add(pair.kalshi.market_id)
        used_p.add(pair.poly.market_id)
        matched_k_events.add(k_eid)
        matched_p_events.add(p_eid)

    # ── Step 2: individual Jaccard for remaining markets ──────────────────────
    # Use stricter thresholds than group matching:
    #   min_sim raised to 0.45 to stop short generic labels from matching
    #   min_token_ratio=0.40 blocks "Democratic Party" (2 toks) matching a
    #   long parlay question (8 toks) — ratio 0.25 < 0.40 → rejected.
    # Bare Polymarket labels ("St. Louis Blues") score high against ANY Kalshi
    # question naming the team ("...win the Presidents' Trophy?"); vetoing one
    # wrong counterpart then hands the slot to the next wrong one. Gate every
    # candidate on CONTEXTUAL similarity (label + parent event title, both
    # sides) BEFORE the 1-1 assignment, so wrong candidates never take a slot.
    rem_k = [s for s in k_snaps if s.market_id not in used_k]
    rem_p = [s for s in p_snaps if s.market_id not in used_p]
    if rem_k and rem_p:
        fallback = match_markets(
            rem_p, rem_k,
            min_title_similarity=max(min_sim, 0.45),
            max_close_delta_hours=100_000,
            min_token_ratio=0.40,
            pair_gate=lambda p, k: _context_similarity(p, k) >= _CONTEXT_GATE,
        )
        all_pairs.extend(fallback)

    all_pairs.sort(key=lambda x: -x.confidence)
    return all_pairs


# ---------------------------------------------------------------------------
# Full-coverage ingestion
# ---------------------------------------------------------------------------
# discover() delegates all per-venue catalog ingestion to these two functions
# so a verifier tool (tools/validate_coverage.py) can run ingestion WITHOUT
# matching, and so the coverage accounting lives in one obvious place per
# venue. Every catalog market is accounted for as ingested or
# excluded:<reason> in the caller-provided ``coverage`` dict (filled in
# place) — see the module docstring on discover() for the overall shape.

def ingest_kalshi(
    kc,
    *,
    now: datetime,
    category: str = "all",
    horizon: datetime | None = None,
    max_events: int | None = None,
    kalshi_workers: int = 6,
    coverage: dict | None = None,
    market_sweep: bool = True,
    sweep_cache_ttl: int = 0,
    _sweep_thread=None,
    _sweep_box: dict | None = None,
) -> tuple[list, list]:
    """Ingest the full Kalshi OPEN-market catalog into ``(filtered_events, k_snaps)``.

    "Open" = status "active" (ground truth ``/markets?status=open&mve_filter=exclude``).
    Every event falls into exactly one bucket below (event_closed / a user
    filter / kept-for-per-market-processing), and every one of ITS markets
    then falls into exactly one further bucket (a non-tradeable status, or
    ingested) — so ``ingested + sum(excluded.values()) == catalog_markets``
    when the orphan sweep is off. With the sweep on, orphan markets recovered
    outside the /events catalog extend this to ``ingested +
    sum(excluded.values()) == catalog_markets + orphan_scanned`` (see the
    orphan sweep section below).

    Stats-only (_STATS_ONLY_RE) and parlay-format markets (KXMVE/parlay-title
    events, or individually via _is_parlay_market) are no longer excluded at
    ingestion: they are INGESTED (full pool coverage) but tagged
    ``extra["match_excluded"] = "<reason>"`` so discover() can hold them out
    of the matcher only — matching precision for these is unchanged.

    category / horizon / max_events are optional user-facing filters; with
    the defaults (category="all", horizon=None, max_events=None) none of
    them exclude anything, so ``ingested == open_markets`` (100% coverage).

    Orphan sweep: Kalshi's ``/events`` listing is verifiably inconsistent
    with ``/markets`` — some events with tradeable ("active") markets never
    appear in ANY ``/events`` walk, so the nested-event ingestion above can
    structurally never reach their markets. When ``market_sweep`` is True,
    ``_run_kalshi_market_sweep`` (a direct ``/markets?status=open&
    mve_filter=exclude`` walk) runs in ``_sweep_thread``/``_sweep_box`` if the
    caller already started one (see discover(), which starts it at the very
    top so its cost overlaps the event walk above); otherwise it is started
    here so this function is safely callable on its own (e.g.
    tools/validate_coverage.py). A recovered market is an "orphan" only if
    its ticker was not already ingested above AND its event_ticker was not
    already seen in ``all_events`` (i.e. genuinely invisible to /events, not
    merely excluded by event_closed/category/horizon/cap). Orphans still
    respect the user's category/horizon filters: each distinct orphan's
    parent event is fetched once via ``kc.get_event`` (tolerating failures
    with a fallback synthetic event built from the market's own title) and
    run through the same event-level filter/hold-out logic as every other
    event, and appended to the returned ``filtered_events`` list so
    ``kalshi_series_ticker`` lookups in discover() keep working. The
    (max_events) cap is NOT re-applied to orphans — they are, by
    construction, markets the cap-driving close-time sort never saw.
    """
    from kalshi.client import KalshiClient

    cov = coverage if coverage is not None else {}
    excluded: dict[str, int] = cov.setdefault("excluded", {})
    held_out: dict[str, int] = cov.setdefault("match_held_out", {})

    def _exclude(reason: str, n: int) -> None:
        if n:
            excluded[reason] = excluded.get(reason, 0) + n

    def _ev_count(ev: dict) -> int:
        m = ev.get("markets")
        return len(m) if m is not None else 0

    # Stale-while-revalidate: only start the (slow) inline sweep thread when
    # the on-disk cache isn't usable (absent, disabled, or past
    # _SWR_HARD_MAX_AGE) — a fresh or merely-stale cache is served without
    # ever touching the network this cycle (see the orphan-sweep section
    # below and _sweep_cache_state).
    _k_cache_usable, _k_cache_stale = (
        _sweep_cache_state("kalshi_orphans.json", sweep_cache_ttl) if market_sweep else (False, False))
    sweep_box = _sweep_box if _sweep_box is not None else {}
    own_sweep_thread = None
    if market_sweep and not _k_cache_usable and _sweep_thread is None and not sweep_box:
        def _bg_sweep():
            sweep_box.update(_run_kalshi_market_sweep(0))

        own_sweep_thread = threading.Thread(target=_bg_sweep, daemon=True)
        own_sweep_thread.start()

    # with_nested_markets embeds every event's market rows (incl. top-of-book),
    # so the whole open catalog (~12k events / ~106k markets) arrives in ~60
    # pages / ~5s — see discover.py module docstring for the history.
    all_events = kc.get_all_events(max_pages=None, page_size=200, status="open",
                                   with_nested_markets=True)
    cov["catalog_events"] = len(all_events)
    cov["complete"] = getattr(kc, "last_scan_complete", True)
    catalog_markets = sum(_ev_count(ev) for ev in all_events)

    # ── Event-level filters, in priority order — each event lands in exactly
    # one bucket so its markets are counted exactly once. ───────────────────
    filtered: list = []
    for ev in all_events:
        n = _ev_count(ev)
        close = _event_close(ev)
        if close and close < now:
            _exclude("event_closed", n)
            continue
        ev_cat = _category(ev)
        if category != "all" and ev_cat != category:
            _exclude("filter_category", n)
            continue
        if horizon is not None and close and close > horizon and ev_cat != "sports":
            _exclude("filter_horizon", n)
            continue
        filtered.append(ev)

    # Prioritise events closing sooner (more liquid, more likely to match)
    def _sort_key(ev):
        dt = _event_close(ev)
        return dt if dt else now + timedelta(days=9999)

    filtered.sort(key=_sort_key)
    pre_cap = filtered
    filtered = _apply_event_cap(pre_cap, max_events)
    kept_ids = {id(ev) for ev in filtered}
    for ev in pre_cap:
        if id(ev) not in kept_ids:
            _exclude("filter_cap", _ev_count(ev))

    # ── Per-market ingestion for events that survived every filter ──────────
    # Only events that arrived WITHOUT a markets list (older API behaviour,
    # or a partial response) fall back to a per-event /markets fetch.
    missing = [ev.get("event_ticker", "") for ev in filtered if ev.get("markets") is None]
    fetched: dict[str, list[dict]] = {}
    if missing:
        # Parallel per-event fetch: events are independent (one client per thread).
        # (threading is imported at module level: a local import here would make
        # it a local name for the whole function and break the sweep thread above.)
        from concurrent.futures import ThreadPoolExecutor

        _tl = threading.local()

        def _fetch_event_markets(et: str) -> list[dict]:
            if not et:
                return []
            client = getattr(_tl, "kc", None)
            if client is None:
                client = _tl.kc = KalshiClient()
            try:
                return client.get_all_markets(event_ticker=et, status="open", page_size=200)
            except Exception:
                return []

        workers = max(1, min(kalshi_workers, len(missing)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for et, batch in zip(missing, pool.map(_fetch_event_markets, missing), strict=True):
                fetched[et] = batch
        catalog_markets += sum(len(v) for v in fetched.values())

    cov["catalog_markets"] = catalog_markets

    fetched_at = now.isoformat()
    ingested = 0
    k_snaps: list = []

    for ev in filtered:
        et = ev.get("event_ticker", "")
        markets = ev.get("markets")
        if markets is None:
            markets = fetched.get(et, [])
        event_title = ev.get("title") or ""
        ev_reason = _kalshi_hold_reason(ev)
        for m in markets:
            # Nested rows are not server-filtered by status (the /markets call
            # used status=open); keep only tradeable markets.
            status = m.get("status")
            if status not in (None, "", "active", "open"):
                _exclude(f"status={status}", 1)
                continue
            reason = ev_reason or ("parlay_title" if _is_parlay_market(m) else None)
            m_title = event_title or m.get("event_title") or m.get("title", "")
            snap = _k_snap(m, fetched_at, event_title=m_title,
                           series_ticker=ev.get("series_ticker") or "")
            if reason:
                snap.extra["match_excluded"] = reason
                held_out[reason] = held_out.get(reason, 0) + 1
            k_snaps.append(snap)
            ingested += 1

    # ── Orphan sweep: markets whose parent event never appeared in /events ──
    ingested_tickers = {s.market_id for s in k_snaps}
    processed_event_tickers = {ev.get("event_ticker", "") for ev in all_events}
    filtered_event_tickers = {ev.get("event_ticker", "") for ev in filtered if ev.get("event_ticker")}

    orphans_added = 0
    orphan_scanned = 0
    sweep_status = "off"
    if market_sweep:
        result = None
        if _k_cache_usable:
            # Fresh or stale-while-revalidate hit: serve the on-disk orphan
            # candidates immediately, no network wait this cycle.
            raw, _age = _cache_load_any("kalshi_orphans.json")
            from_cache = True
            complete = True
            cov["_sweep_elapsed"] = 0.0
        else:
            thread = _sweep_thread or own_sweep_thread
            if thread is not None:
                thread.join()
                result = sweep_box
            elif sweep_box:
                # A caller (e.g. tests, tools/validate_coverage.py) pre-populated
                # _sweep_box directly without a thread — use it as-is.
                result = sweep_box
            else:
                result = _run_kalshi_market_sweep(0)
            cov["_sweep_elapsed"] = result.get("elapsed", 0.0)
            raw = result.get("raw")
            from_cache = bool(result.get("from_cache"))
            complete = result.get("complete", True)
        if raw is None:
            sweep_status = "failed"
        else:
            candidates = []
            for m in raw:
                status = m.get("status")
                if status not in (None, "", "active", "open"):
                    continue
                ticker = m.get("ticker", "")
                if not ticker or ticker in ingested_tickers:
                    continue
                et = m.get("event_ticker", "")
                if et in processed_event_tickers:
                    continue
                candidates.append(m)
            orphan_scanned = len(candidates)
            if _k_cache_stale:
                # Serve stale now (above); refresh the cache in the
                # background for the NEXT cycle using this cycle's
                # ticker/event context, captured by value. Never blocks.
                _maybe_refresh_in_background(
                    "kalshi_orphans.json",
                    lambda _ing=set(ingested_tickers), _proc=set(processed_event_tickers):
                        _kalshi_orphan_refresh(_ing, _proc))
            elif sweep_cache_ttl > 0 and not from_cache:
                # Cache only the (tiny) orphan-candidate subset, not the whole
                # ~106k-row sweep — mirrors ingest_polymarket's pm_orphans.json.
                _cache_store("kalshi_orphans.json", candidates)

            # Fetch each distinct orphan parent event once (handful of calls).
            distinct_ets = sorted({m.get("event_ticker", "") for m in candidates
                                   if m.get("event_ticker")})
            event_by_ticker: dict[str, dict] = {}
            if distinct_ets:
                from concurrent.futures import ThreadPoolExecutor

                _tl2 = threading.local()

                def _fetch_orphan_event(et: str):
                    client = getattr(_tl2, "kc", None)
                    if client is None:
                        client = _tl2.kc = KalshiClient()
                    try:
                        # with_nested_markets so _event_close() below can find a
                        # close time (bare /events/{ticker} rows carry none at
                        # the top level, same as the /events listing).
                        return et, client.get_event(et, with_nested_markets=True)
                    except Exception:
                        return et, None

                workers = max(1, min(kalshi_workers, len(distinct_ets)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for et, ev in pool.map(_fetch_orphan_event, distinct_ets):
                        if ev:
                            event_by_ticker[et] = ev

            for m in candidates:
                ticker = m.get("ticker", "")
                et = m.get("event_ticker", "")
                # Tolerate a failed per-event fetch with a synthetic event
                # built from the market's own title (best-effort attribution
                # rather than dropping a genuinely-open market).
                ev = event_by_ticker.get(et) or {"event_ticker": et, "title": m.get("title", "")}

                # Orphan exclusions use an "orphan_"-prefixed reason so they
                # don't get double-subtracted from catalog_markets (which
                # never included these markets in the first place) in the
                # open_markets accounting below — see the invariant note.
                close = _event_close(ev)
                if close and close < now:
                    _exclude("orphan_event_closed", 1)
                    continue
                ev_cat = _category(ev)
                if category != "all" and ev_cat != category:
                    _exclude("orphan_filter_category", 1)
                    continue
                if horizon is not None and close and close > horizon and ev_cat != "sports":
                    _exclude("orphan_filter_horizon", 1)
                    continue

                ev_reason = _kalshi_hold_reason(ev)
                reason = ev_reason or ("parlay_title" if _is_parlay_market(m) else None)
                event_title = ev.get("title") or m.get("title", "")
                snap = _k_snap(m, fetched_at, event_title=event_title,
                               series_ticker=ev.get("series_ticker") or "")
                if reason:
                    snap.extra["match_excluded"] = reason
                    held_out[reason] = held_out.get(reason, 0) + 1
                if from_cache:
                    # A cache hit can be up to sweep_cache_ttl (1h) old: the
                    # orphan's inline top-of-book may be stale. Tagged the
                    # same way as ingest_polymarket's cache-served orphans so
                    # the catalog-price screen (_select_pairs_to_enrich)
                    # always fetches a live book rather than trusting a
                    # possibly hour-old catalog price.
                    snap.extra["catalog_stale"] = True
                k_snaps.append(snap)
                ingested += 1
                orphans_added += 1
                ingested_tickers.add(ticker)
                if et and et not in filtered_event_tickers:
                    filtered.append(ev)
                    filtered_event_tickers.add(et)

            sweep_status = "stale" if _k_cache_stale else (
                "cached" if from_cache else ("fresh" if complete else "partial"))

        # Drop the raw sweep rows (~106k dicts) now that orphans are extracted —
        # they are never read again this cycle and a long-running alerter
        # process would otherwise keep every past cycle's sweep_box alive via
        # closures/threads (memory hygiene, iteration 2 WP-B). Only applies
        # when a real (non-cached) sweep was fetched this cycle.
        if result is not None:
            result["raw"] = None

    cov["ingested"] = ingested
    cov["orphans_added"] = orphans_added
    cov["orphan_scanned"] = orphan_scanned
    cov["sweep"] = sweep_status
    # Ground truth: tradeable markets regardless of the category/horizon/cap
    # filters above (those are user choices, not non-tradeable exclusions).
    # orphan_scanned markets are added in (they were never part of
    # catalog_markets) minus the orphan subset that turned out non-tradeable
    # (orphan_event_closed) — orphan_filter_category/horizon orphans are
    # still genuinely open, just held out of ingestion by user choice, so
    # (like their catalog counterparts) they are NOT subtracted here. This
    # keeps ``ingested + sum(excluded.values()) == catalog_markets +
    # orphan_scanned`` as the full-accounting invariant.
    cov["open_markets"] = catalog_markets - excluded.get("event_closed", 0) - sum(
        n for r, n in excluded.items() if r.startswith("status=")
    ) + orphan_scanned - excluded.get("orphan_event_closed", 0)
    return filtered, k_snaps


def ingest_polymarket(
    pc,
    *,
    fetched_at: str,
    max_events: int | None = None,
    market_sweep: bool = True,
    sweep_cache_ttl: int = 0,
    known_ids: set | None = None,
    coverage: dict | None = None,
    _sweep_thread=None,
    _sweep_box: dict | None = None,
) -> list:
    """Ingest the full Polymarket OPEN-market catalog into ``p_snaps``.

    "Open" = ``active and not closed`` (ground truth Gamma
    ``/markets/keyset?closed=false``). No keyword filter — the whole event
    catalog is walked via ``get_all_events``. A market is dropped only for
    being ``closed`` or not ``active``; every embedded market is accounted
    for as ingested or excluded:<reason> in ``coverage``.

    Orphan sweep: ~480 live markets whose parent event is archived/inactive/
    closed are invisible to ``/events/keyset`` and can only be reached by
    walking ``/markets/keyset`` directly (~290s for the full catalog). When
    ``market_sweep`` is True, that walk runs in ``_sweep_thread``/``_sweep_box``
    if the caller already started one (see discover(), which starts it at the
    very top so its cost overlaps Kalshi ingestion and this function's own
    event walk); otherwise it is started here so this function is safely
    callable on its own (e.g. tools/validate_coverage.py). Orphan rows (not
    the full sweep) are cached under "pm_orphans.json" when
    ``sweep_cache_ttl`` > 0.
    """
    cov = coverage if coverage is not None else {}
    excluded: dict[str, int] = cov.setdefault("excluded", {})

    def _exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    # Stale-while-revalidate: skip starting the (slow) inline sweep thread
    # entirely when the on-disk cache is usable (fresh or merely stale but
    # under _SWR_HARD_MAX_AGE) — see _sweep_cache_state and the orphan-sweep
    # section below.
    _pm_cache_usable, _pm_cache_stale = (
        _sweep_cache_state("pm_orphans.json", sweep_cache_ttl) if market_sweep else (False, False))
    sweep_box = _sweep_box if _sweep_box is not None else {}
    own_thread = None
    if market_sweep and not _pm_cache_usable and _sweep_thread is None and not sweep_box:
        def _bg():
            sweep_box.update(_run_market_sweep(0))

        own_thread = threading.Thread(target=_bg, daemon=True)
        own_thread.start()

    p_events = pc.get_all_events(closed=False, max_events=max_events)
    cov["catalog_events"] = len(p_events)
    cov["complete"] = getattr(pc, "last_scan_complete", True)

    p_snaps: list = []
    seen_ids: set = set(known_ids or ())
    embedded_markets = 0
    for ev in p_events:
        ev_title = ev.get("title", "")
        ev_slug = ev.get("slug") or ev.get("id", "")
        markets = ev.get("markets") or []
        embedded_markets += len(markets)
        for m in markets:
            cid = m.get("conditionId") or m.get("id", "")
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            if m.get("closed"):
                _exclude("closed")
                continue
            if not m.get("active"):
                _exclude("inactive")
                continue
            p_snaps.append(_p_snap_from_event(m, ev_title, ev_slug, fetched_at))
    cov["embedded_markets"] = embedded_markets

    orphans_added = 0
    sweep_status = "off"
    if market_sweep:
        result = None
        if _pm_cache_usable:
            # Fresh or stale-while-revalidate hit: serve on-disk orphans
            # immediately, no network wait this cycle.
            raw, _age = _cache_load_any("pm_orphans.json")
            from_cache = True
            complete = True
            cov["_sweep_elapsed"] = 0.0
        else:
            thread = _sweep_thread or own_thread
            if thread is not None:
                thread.join()
                result = sweep_box
            elif sweep_box:
                # A caller (e.g. tests, tools/validate_coverage.py) pre-populated
                # _sweep_box directly without a thread — use it as-is.
                result = sweep_box
            else:
                result = _run_market_sweep(0)
            cov["_sweep_elapsed"] = result.get("elapsed", 0.0)
            raw = result.get("raw")
            from_cache = bool(result.get("from_cache"))
            complete = result.get("complete", True)
        if raw is None:
            sweep_status = "failed"
        else:
            orphan_rows = []
            for m in raw:
                cid = m.get("conditionId") or m.get("id", "")
                if cid in seen_ids or m.get("closed") or not m.get("active"):
                    continue
                seen_ids.add(cid)
                orphan_rows.append(m)
                evs = m.get("events") or []
                ev0 = evs[0] if evs else {}
                ev_title = ev0.get("title") or m.get("question") or m.get("title", "")
                ev_slug = ev0.get("slug") or m.get("slug", "")
                snap = _p_snap_from_event(m, ev_title, ev_slug, fetched_at)
                if from_cache:
                    # A cache hit can be up to sweep_cache_ttl (1h) old: the
                    # orphan's catalog quote (and the matcher-safe outcomePrices
                    # mid) may be stale. Still fine for matching (title/close
                    # time don't go stale that fast), but the catalog-price
                    # screen must treat it as needing a fresh live book if the
                    # pair is endorsed — never decide "not worth enriching" off
                    # a possibly hour-old catalog price.
                    snap.extra["catalog_stale"] = True
                p_snaps.append(snap)
                orphans_added += 1
            sweep_status = "stale" if _pm_cache_stale else (
                "cached" if from_cache else ("fresh" if complete else "partial"))
            if _pm_cache_stale:
                # Serve stale now (above); refresh in the background for the
                # NEXT cycle using this cycle's seen-id context, captured by
                # value. Never blocks.
                _maybe_refresh_in_background(
                    "pm_orphans.json",
                    lambda _seen=set(seen_ids): _pm_orphan_refresh(_seen))
            elif sweep_cache_ttl > 0 and not from_cache:
                # Cache only the (tiny) orphan subset, not the whole ~166k-row sweep.
                _cache_store("pm_orphans.json", orphan_rows)

        # Drop the raw sweep rows (~166k dicts on a fresh run) now that
        # orphans are extracted — see the matching comment in ingest_kalshi.
        if result is not None:
            result["raw"] = None

    cov["orphans_added"] = orphans_added
    cov["sweep"] = sweep_status
    cov["ingested"] = len(p_snaps)
    cov["open_markets"] = embedded_markets - excluded.get("closed", 0) - excluded.get("inactive", 0) + orphans_added
    return p_snaps


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else "n/a"


def _print_kalshi_coverage(cov: dict) -> None:
    open_m = cov.get("open_markets", 0)
    ingested = cov.get("ingested", 0)
    orphans = cov.get("orphans_added", 0)
    held = cov.get("match_held_out") or {}
    excl = cov.get("excluded") or {}
    held_str = " ".join(f"{k}={v:,}" for k, v in sorted(held.items())) or "none"
    excl_str = " ".join(f"{k}={v:,}" for k, v in sorted(excl.items())) or "none"
    complete_tag = "" if cov.get("complete", True) else "  [PARTIAL CATALOG]"
    print(f"      [coverage] Kalshi: {ingested:,}/{open_m:,} open markets ingested "
          f"({_pct(ingested, open_m)}, incl. {orphans:,} orphans) · "
          f"held out of matching: {held_str} · "
          f"excluded: {excl_str} · sweep={cov.get('sweep', 'off')}{complete_tag}")


def _print_polymarket_coverage(cov: dict) -> None:
    open_m = cov.get("open_markets", 0)
    ingested = cov.get("ingested", 0)
    orphans = cov.get("orphans_added", 0)
    excl = cov.get("excluded") or {}
    excl_str = " ".join(f"{k}={v:,}" for k, v in sorted(excl.items())) or "none"
    complete_tag = "" if cov.get("complete", True) else "  [PARTIAL CATALOG]"
    print(f"      [coverage] Polymarket: {ingested:,}/{open_m:,} open markets ingested "
          f"({_pct(ingested, open_m)}, incl. {orphans:,} orphans) · "
          f"excluded: {excl_str} · sweep={cov.get('sweep', 'off')}{complete_tag}")


# ---------------------------------------------------------------------------
# Catalog-price screen (iteration 2 WP-B) — decide which v2-endorsed pairs are
# worth a live orderbook fetch BEFORE fetching, using prices already sitting
# in the catalog snapshots for free:
#   - Kalshi: the nested-event top-of-book (real yes_bid/ask_dollars, see
#     _parse_kalshi_top_of_book) IS the catalog price, already on
#     pair.kalshi.orderbook.best_bid / best_ask.
#   - Polymarket: outcomePrices is a single MID reused for both sides of the
#     main orderbook (needed so the matcher's price_sim keeps working) and so
#     can't be used as a real bid/ask; Gamma's own bestBid/bestAsk are the
#     real catalog quotes, stashed separately in extra["catalog_bid"/"_ask"]
#     by _p_snap_from_event / _p_snap (see _catalog_bid_ask above) — the main
#     orderbook is deliberately left untouched.
# ---------------------------------------------------------------------------

def _catalog_prices(pair) -> tuple[float | None, float | None, float | None, float | None]:
    """(kalshi_bid, kalshi_ask, poly_bid, poly_ask) from CATALOG data only."""
    k_bid = pair.kalshi.orderbook.best_bid
    k_ask = pair.kalshi.orderbook.best_ask
    p_bid = pair.poly.extra.get("catalog_bid")
    p_ask = pair.poly.extra.get("catalog_ask")
    return k_bid, k_ask, p_bid, p_ask


def _catalog_gross_edge(pair) -> tuple[float | None, bool]:
    """Best catalog-implied GROSS edge (no fees) over both arb directions, and
    whether a price needed to evaluate either direction is missing.

    Direction 1 (buy PM YES + buy Kalshi NO): edge = kalshi_bid - poly_ask
    Direction 2 (buy Kalshi YES + buy PM NO): edge = poly_bid - kalshi_ask

    Returns ``(best_edge_or_None, needs_fetch)``. ``needs_fetch`` is True
    whenever EITHER direction's prices are incomplete — a missing catalog
    price is always treated conservatively (fetch the live book) rather than
    silently skipping a direction we can't evaluate.
    """
    k_bid, k_ask, p_bid, p_ask = _catalog_prices(pair)
    edges: list[float] = []
    needs_fetch = False
    if k_bid is not None and p_ask is not None:
        edges.append(k_bid - p_ask)
    else:
        needs_fetch = True
    if p_bid is not None and k_ask is not None:
        edges.append(p_bid - k_ask)
    else:
        needs_fetch = True
    best = max(edges) if edges else None
    if best is None:
        needs_fetch = True
    return best, needs_fetch


def _select_pairs_to_enrich(
    endorsed: list, enrich_margin: float | None,
) -> tuple[list, dict]:
    """Decide which v2-endorsed pairs get a live orderbook fetch.

    A pair is enriched (fetched) when ANY of:
      - ``enrich_margin`` is None (the default since iteration 3: enrich
        everything endorsed — cheap now that Kalshi books are batched);
      - a needed catalog price is missing (conservative — can't screen it);
      - either leg is a cache-served orphan (``catalog_stale``), whose
        catalog quote can be up to an hour old (Polymarket's own orphan
        sweep, or — since iteration 3 — Kalshi's hourly-cached orphan sweep);
      - the best catalog GROSS edge over both directions is >= -enrich_margin
        (i.e. within ``enrich_margin`` of positive, or already positive).

    Returns ``(pairs_to_enrich, catalog_edge_by_pair_id)`` — the latter covers
    EVERY endorsed pair (not just the enriched ones) so results can report
    ``catalog_gross_edge`` regardless of whether books were fetched.
    """
    to_enrich = []
    catalog_edge: dict[int, float | None] = {}
    for p in endorsed:
        edge, needs_fetch = _catalog_gross_edge(p)
        catalog_edge[id(p)] = edge
        stale = bool(p.poly.extra.get("catalog_stale")) or bool(p.kalshi.extra.get("catalog_stale"))
        if enrich_margin is None or needs_fetch or stale or edge >= -enrich_margin:
            to_enrich.append(p)
    return to_enrich, catalog_edge


# ---------------------------------------------------------------------------
# Core discovery function
# ---------------------------------------------------------------------------

def discover(
    category: str = "all",
    days: int | None = None,
    min_sim: float = 0.30,
    show_prices: bool = False,
    max_poly_offset: int | None = None,
    max_events_to_search: int | None = None,
    kalshi_workers: int = 6,  # polite: ~10 req/s API limit; more workers trigger 429 backoff storms
    return_pools: bool = False,
    market_sweep: bool = True,
    sweep_cache_ttl: int = 0,
    enrich_margin: float | None = None,
    coverage: dict | None = None,
):
    """
    Run organic cross-exchange discovery and return matched pairs as dicts.

    Ingests 100% of both venues' OPEN markets (see ingest_kalshi /
    ingest_polymarket) into the matching pools. Structurally-incomparable
    Kalshi markets (stats-only, parlay-format) are still ingested but held
    out of the matcher only (``extra["match_excluded"]``), so return_pools
    exposes the FULL catalog while matching precision is unchanged.

    There is no catalog cache: both venues' event catalogs (and market
    quotes) are always walked fresh every call. The only thing ever cached
    is the small orphan-market subset for each venue, via ``sweep_cache_ttl``.

    enrich_margin (default None, since iteration 3) decides which v2-endorsed
    pairs get a live book fetch in step 4 (``show_prices``). Kalshi order
    books are now fetched via the batch endpoint (``KalshiClient.
    get_orderbooks``, ~100 tickers/request, ~0.08s/request), so enriching
    EVERY endorsed pair costs low single-digit seconds even at ~4,700 pairs
    (measured live: 4.4s) — simpler and lossless by construction, so it's now
    the default. Passing a float (e.g. ``0.05``) re-enables the iteration-2
    catalog-price pre-screen: of the v2-endorsed pairs, only fetch live
    orderbooks for those whose best catalog GROSS edge (Kalshi's real
    nested-event top-of-book vs Polymarket's Gamma bestBid/bestAsk, see
    ``_catalog_gross_edge``) is >= -enrich_margin, or whose needed catalog
    price is missing, or whose Kalshi/Polymarket leg is a cache-served orphan
    (``extra["catalog_stale"]``) — all three fetch conservatively rather than
    risk skipping a real arb. The flag is kept for a slower network / a
    future high-pair-count regime where the screen's saving matters again
    (see docs/history/COVERAGE_ITERATIONS.md, iterations 2 and 3). Every
    result carries ``catalog_gross_edge`` (best catalog
    edge, whether or not it was enriched) and ``books_live`` (whether its
    orderbook is a live fetch vs. the catalog snapshot) so callers — notably
    ``alerter.compute_signals`` — never mistake a screened-out pair's stale
    catalog book for an executable quote: a non-enriched pair's Polymarket
    orderbook sizes are always 0.0 (see ``_p_snap``/``_p_snap_from_event``),
    which already fails ``min_size`` depth filters on its own.

    market_sweep (default True) additionally runs TWO background orphan
    sweeps, each started here at the very top so their cost overlaps normal
    ingestion instead of adding on top:
      - Kalshi: a direct ``/markets?status=open&mve_filter=exclude`` walk
        (~20s) to recover markets whose parent event never appears in ANY
        ``/events`` listing (a live /events-vs-/markets inconsistency).
      - Polymarket: a ``/markets/keyset`` walk (~290s) to recover ~480
        "orphan" markets whose parent event is archived/inactive/closed
        (invisible to the events walk).
    sweep_cache_ttl > 0 caches the (small) orphan subset for BOTH sweeps
    (``pm_orphans.json`` / ``kalshi_orphans.json``), so only the first cycle
    within the TTL window pays the full walk; cache-served orphans on either
    leg are tagged ``extra["catalog_stale"]`` so the catalog-price screen
    always fetches a live book for them rather than trusting a possibly
    hour-old catalog quote (see ``_select_pairs_to_enrich``).

    coverage, if given, is filled in place with a full accounting of every
    catalog market as ingested or excluded:<reason>, per venue (also exposed
    as the module attribute ``discover.last_coverage``).
    """
    global last_coverage
    import logging
    logging.disable(logging.WARNING)

    from kalshi.client import KalshiClient
    from polymarket.client import PolymarketClient

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days) if days is not None else None
    fetched_at = now.isoformat()

    coverage = coverage if coverage is not None else {}
    cov_k = coverage.setdefault("kalshi", {})
    cov_p = coverage.setdefault("polymarket", {})
    cov_t = coverage.setdefault("elapsed_s", {})

    # Start the (slow) orphan sweeps FIRST, in the background, so their cost
    # overlaps Kalshi ingestion + the Polymarket event walk below rather than
    # adding on top. See ingest_polymarket / _run_market_sweep and
    # ingest_kalshi / _run_kalshi_market_sweep.
    #
    # Stale-while-revalidate (iteration 4): only start these inline-fetch
    # threads when the on-disk cache isn't usable this cycle (absent,
    # disabled, or past _SWR_HARD_MAX_AGE) — ingest_kalshi/ingest_polymarket
    # make and act on the identical cache-usability check themselves (see
    # _sweep_cache_state), so pre-starting a real fetch here whenever a
    # cache/stale-cache hit is about to be served would just waste a network
    # round-trip every cycle for no benefit (the thread would never be
    # joined). A cold cache (or one older than the hard cap) still gets
    # started here so its cost overlaps the ingestion below, same as before.
    sweep_thread = None
    sweep_box: dict = {}
    k_sweep_thread = None
    k_sweep_box: dict = {}
    if market_sweep:
        pm_cache_usable, _ = _sweep_cache_state("pm_orphans.json", sweep_cache_ttl)
        k_cache_usable, _ = _sweep_cache_state("kalshi_orphans.json", sweep_cache_ttl)

        if not pm_cache_usable:
            def _bg_sweep():
                sweep_box.update(_run_market_sweep(0))

            sweep_thread = threading.Thread(target=_bg_sweep, daemon=True)
            sweep_thread.start()

        if not k_cache_usable:
            def _bg_kalshi_sweep():
                k_sweep_box.update(_run_kalshi_market_sweep(0))

            k_sweep_thread = threading.Thread(target=_bg_kalshi_sweep, daemon=True)
            k_sweep_thread.start()

    # ── 1. Kalshi ingestion (full open catalog) ─────────────────────────────
    print("[1/4] Ingesting Kalshi open-market catalog…", flush=True)
    kc = KalshiClient()
    t0 = time.time()
    filtered, k_snaps = ingest_kalshi(
        kc, now=now, category=category, horizon=horizon,
        max_events=max_events_to_search, kalshi_workers=kalshi_workers,
        coverage=cov_k, market_sweep=market_sweep, sweep_cache_ttl=sweep_cache_ttl,
        _sweep_thread=k_sweep_thread, _sweep_box=k_sweep_box,
    )
    cov_t["kalshi"] = round(time.time() - t0, 1)
    cov_t["kalshi_sweep"] = round(cov_k.pop("_sweep_elapsed", 0.0), 1)
    _print_kalshi_coverage(cov_k)

    filtered_by_ticker = {
        ev.get("event_ticker", ""): ev
        for ev in filtered
        if ev.get("event_ticker")
    }
    if not k_snaps:
        print("      No Kalshi markets found.")
        if sweep_thread is not None:
            sweep_thread.join()
        if k_sweep_thread is not None:
            k_sweep_thread.join()
        last_coverage = coverage
        return ([], k_snaps, []) if return_pools else []

    # ── 2. Polymarket ingestion (full open catalog + orphan sweep) ──────────
    poly_scan_label = f"{max_poly_offset:,}-event catalog scan" if max_poly_offset is not None else "full event catalog scan"
    print(f"[2/4] Ingesting Polymarket open-market catalog ({poly_scan_label})…", flush=True)
    t0 = time.time()
    pc = PolymarketClient()
    p_snaps = ingest_polymarket(
        pc, fetched_at=fetched_at, max_events=max_poly_offset,
        market_sweep=market_sweep, sweep_cache_ttl=sweep_cache_ttl,
        coverage=cov_p, _sweep_thread=sweep_thread, _sweep_box=sweep_box,
    )
    cov_t["polymarket_events"] = round(time.time() - t0, 1)
    cov_t["sweep"] = round(cov_p.pop("_sweep_elapsed", 0.0), 1)
    _print_polymarket_coverage(cov_p)

    last_coverage = coverage
    if not p_snaps:
        print("      No Polymarket markets found.")
        return ([], k_snaps, p_snaps) if return_pools else []

    # ── 3. Match ─────────────────────────────────────────────────────────────
    # Structurally-incomparable Kalshi markets (stats-only, parlay-format) are
    # ingested above for full coverage but held OUT of the matcher only, so
    # matching precision is unchanged from before full-coverage ingestion.
    match_k_snaps = [s for s in k_snaps if not s.extra.get("match_excluded")]
    print(f"[3/4] Running two-level group matcher (min_sim={min_sim})  "
          f"({len(match_k_snaps):,}/{len(k_snaps):,} Kalshi markets eligible)…", flush=True)
    t0 = time.time()
    # Structured sports-game join first: game titles share no tokens across
    # venues ("Denver wins" vs "Broncos vs. Chiefs"), so the text matcher
    # cannot find them. Matched markets are removed from the text pools.
    from sports_match import match_sports_games
    sports_pairs = match_sports_games(match_k_snaps, p_snaps)
    used_k = {pr.kalshi.market_id for pr in sports_pairs}
    used_p = {pr.poly.market_id for pr in sports_pairs}
    text_pairs = _match_groups_then_individual(
        [s for s in match_k_snaps if s.market_id not in used_k],
        [s for s in p_snaps if s.market_id not in used_p],
        min_sim=min_sim,
    )
    pairs = sports_pairs + text_pairs
    cov_t["match"] = round(time.time() - t0, 1)
    print(f"      {len(pairs)} pairs found ({len(sports_pairs)} sports-key, {len(text_pairs)} text)")

    # ── 4. Format / enrich ───────────────────────────────────────────────────
    print("[4/4] Formatting results…", flush=True)
    # v2 verdicts are TEXT-ONLY (no prices). The alerter / compute_signals discard
    # any pair whose v2_match isn't True (require_v2), so compute v2 up front and
    # fetch live order books ONLY for v2-endorsed pairs — enriching the rest is
    # pure waste (they can never become a signal). Provably lossless for the
    # alerter; non-endorsed pairs keep catalog prices (review-only). The verdict is
    # reused in the results loop below so v2 is computed once. (run 47)
    t0 = time.time()
    from contract_spec import explain as _v2_explain
    v2_by_pair: dict = {}
    for pair in pairs:
        if pair.match_source == "sports":
            continue  # structural key join; v2's text rules do not apply
        try:
            v2_by_pair[id(pair)] = _v2_explain(pair.poly, pair.kalshi)
        except Exception as exc:  # shadow mode must never break production
            v2_by_pair[id(pair)] = exc
    cov_t["v2"] = round(time.time() - t0, 1)

    # Catalog-price screen (iteration 2 WP-B): of the v2-endorsed pairs, only
    # fetch live books for those whose catalog prices already look close to an
    # edge (or are missing/stale) — see _select_pairs_to_enrich. Every endorsed
    # pair still gets a catalog_gross_edge recorded, whether or not it was
    # enriched, so results/tests can audit the screen's decisions.
    t0 = time.time()
    catalog_edge_by_pair: dict = {}
    books_live_ids: set = set()
    if show_prices and pairs:
        endorsed = [p for p in pairs if p.match_source == "sports"
                    or getattr(v2_by_pair.get(id(p)), "match", False) is True]
        to_enrich, catalog_edge_by_pair = _select_pairs_to_enrich(endorsed, enrich_margin)
        print(f"      Fetching live orderbooks for {len(to_enrich)} of {len(endorsed)} "
              f"v2-endorsed pairs (catalog screen, margin={enrich_margin}) "
              f"of {len(pairs)} total…", flush=True)
        _enrich_kalshi([p.kalshi for p in to_enrich])
        _enrich_polymarket([p.poly for p in to_enrich])
        books_live_ids = {id(p) for p in to_enrich}
    cov_t["enrich"] = round(time.time() - t0, 1)

    # ── Format results ────────────────────────────────────────────────────────
    t0 = time.time()
    from matcher import is_arb_eligible, settlement_risk

    FEE = 0.07  # conservative worst-case fee
    results = []
    for pair in sorted(pairs, key=lambda x: -x.confidence):
        pb = pair.poly.orderbook.best_bid
        pa = pair.poly.orderbook.best_ask
        kb = pair.kalshi.orderbook.best_bid
        ka = pair.kalshi.orderbook.best_ask

        arb_dir = arb_profit = None
        arb_eligible = pair.match_source == "sports" or is_arb_eligible(pair.poly, pair.kalshi)
        if show_prices and arb_eligible:
            if pa is not None and kb is not None:
                profit = round(1.0 - (pa + 1.0 - kb) - FEE, 4)
                if profit > 0:
                    arb_dir, arb_profit = "poly_yes + kalshi_no", profit
            if ka is not None and pb is not None:
                profit = round(1.0 - (ka + 1.0 - pb) - FEE, 4)
                if profit > 0 and (arb_profit is None or profit > arb_profit):
                    arb_dir, arb_profit = "kalshi_yes + poly_no", profit

        # Categorise using the event title (not the short outcome label like
        # "France" or "No change" which would always return "pop").
        k_event_title = pair.kalshi.extra.get("event_title") or pair.kalshi.title
        p_event_title = pair.poly.extra.get("event_title") or pair.poly.title
        cat = _category({"title": k_event_title or p_event_title})

        # v2 SHADOW MODE: structured decision engine (contract_spec) verdict,
        # precomputed above (text-only) and reused here. A v2 rejection of a v1
        # match is a candidate v1 false positive — surfaced, never silently dropped.
        _v2 = v2_by_pair.get(id(pair))
        if pair.match_source == "sports":
            v2_fields = {"v2_match": True, "v2_inverted": False,
                         "v2_reasons": [pair.match_reason]}
        elif isinstance(_v2, Exception):  # shadow mode must never break production
            v2_fields = {"v2_match": None, "v2_error": str(_v2)}
        else:
            v2_fields = {
                "v2_match": _v2.match,
                "v2_inverted": _v2.inverted,
                "v2_reasons": _v2.reasons,
            }

        results.append({
            "confidence":       round(pair.confidence, 3),
            "title_sim":        round(pair.title_similarity, 3),
            "category":         cat,
            "close_delta_days": round(pair.close_delta_hours / 24) if pair.close_delta_hours else None,
            "poly_title":       pair.poly.title,
            "poly_event_title": p_event_title,
            "poly_id":          pair.poly.market_id,
            "poly_slug":        pair.poly.event_id,
            "poly_close":       (pair.poly.close_time or "")[:10],
            "poly_bid":         pb,
            "poly_ask":         pa,
            "kalshi_title":        pair.kalshi.title,
            "kalshi_event_title":  k_event_title,
            "kalshi_ticker":       pair.kalshi.market_id,
            "kalshi_event_ticker": pair.kalshi.event_id,
            # Series ticker is what kalshi.com routes market pages on — event
            # tickers 404. Sourced from the event catalog; fall back to
            # stripping the event ticker's date/outcome suffix.
            "kalshi_series_ticker": (
                (filtered_by_ticker.get(pair.kalshi.event_id) or {}).get("series_ticker")
                or (pair.kalshi.event_id.rsplit("-", 1)[0] if "-" in pair.kalshi.event_id
                    else pair.kalshi.event_id)
            ),
            "kalshi_close":     (pair.kalshi.close_time or "")[:10],
            "kalshi_bid":       kb,
            "kalshi_ask":       ka,
            # Best-level depth (shares for Polymarket, contracts for Kalshi) so a
            # liquidity filter can drop illiquid/one-sided books — a major source
            # of high-edge phantom arbs at the wide cap (run 20).
            "poly_bid_size":    pair.poly.orderbook.bids[0].size if pair.poly.orderbook.bids else None,
            "poly_ask_size":    pair.poly.orderbook.asks[0].size if pair.poly.orderbook.asks else None,
            "kalshi_bid_size":  pair.kalshi.orderbook.bids[0].size if pair.kalshi.orderbook.bids else None,
            "kalshi_ask_size":  pair.kalshi.orderbook.asks[0].size if pair.kalshi.orderbook.asks else None,
            # Full ladders (top 30 levels/side) so the alerter can compute
            # executable depth / VWAP / profit-by-budget and chart it (book_arb).
            "poly_book": {
                "bids": [[l.price, l.size] for l in (pair.poly.orderbook.bids or [])[:30]],
                "asks": [[l.price, l.size] for l in (pair.poly.orderbook.asks or [])[:30]],
            },
            "kalshi_book": {
                "bids": [[l.price, l.size] for l in (pair.kalshi.orderbook.bids or [])[:30]],
                "asks": [[l.price, l.size] for l in (pair.kalshi.orderbook.asks or [])[:30]],
            },
            "match_source":     pair.match_source,
            "settlement_risk":  settlement_risk(pair.poly, pair.kalshi),
            "arb_eligible":     arb_eligible,
            "arb_direction":    arb_dir,
            "arb_net_profit":   arb_profit,
            # Catalog-price screen bookkeeping (iteration 2 WP-B): the best
            # catalog-implied gross edge (None if not v2-endorsed, since only
            # endorsed pairs are screened) and whether this pair's book below
            # is a live fetch or still the catalog snapshot.
            "catalog_gross_edge": catalog_edge_by_pair.get(id(pair)),
            "books_live":       id(pair) in books_live_ids,
            **v2_fields,
        })
    cov_t["format"] = round(time.time() - t0, 1)

    agree = sum(1 for r in results if r.get("v2_match") is True)
    disagree = [r for r in results if r.get("v2_match") is False]
    if results:
        print(f"      v2 shadow: agrees on {agree}/{len(results)} v1 pairs"
              + (f", {len(disagree)} disagreement(s):" if disagree else ""))
        for r in disagree:
            print(f"        v2 REJECTS: {r['poly_title'][:46]!r} <-> {r['kalshi_title'][:46]!r}")
            for reason in (r.get("v2_reasons") or [])[:2]:
                print(f"          - {reason}")

    # Memory hygiene (iteration 2 WP-B): a long-running alerter process calls
    # discover() every cycle, and matcher's ~39 memoised text->feature helpers
    # (up to 300k entries each) would otherwise accumulate across cycles.
    # Results are already built above, so it's safe to drop them now.
    import matcher
    matcher.clear_caches()

    return (results, k_snaps, p_snaps) if return_pools else results


# Exposed for callers that want the last run's ingestion accounting without
# threading return_pools/coverage through every call site (e.g. health checks).
last_coverage: dict = {}


# ---------------------------------------------------------------------------
# CLI printer
# ---------------------------------------------------------------------------

def _print_results(results: list[dict], show_prices: bool) -> None:
    if not results:
        print("\nNo pairs found.")
        return

    arb = [r for r in results if (r.get("arb_net_profit") or 0) > 0]

    # Group by category for the summary line
    by_cat: dict[str, int] = {}
    for r in results:
        c = r.get("category", "?")
        by_cat[c] = by_cat.get(c, 0) + 1
    cat_summary = "  ".join(f"{c}={n}" for c, n in sorted(by_cat.items()))

    print(f"\n{'═' * 122}")
    print(f"  {len(results)} MATCHED PAIRS   |   {len(arb)} arb signals   |   {cat_summary}")
    print(f"{'═' * 122}\n")

    for i, r in enumerate(results, 1):
        arb_tag = ""
        if (r.get("arb_net_profit") or 0) > 0:
            arb_tag = f"  ⚡ +{r['arb_net_profit']:.4f} ({r['arb_direction']})"
        cat_tag = f"[{r.get('category','?')[:4]}]"
        conf_tag = f"[{r['confidence']:.2f}]"

        print(f"  #{i:>4} {conf_tag} {cat_tag}  sim={r['title_sim']:.2f}{arb_tag}")
        print(f"         Poly:   {r['poly_title'][:90]}")
        print(f"         Kalshi: {r['kalshi_title'][:90]}")

        if show_prices:
            pb, pa = r.get("poly_bid"), r.get("poly_ask")
            kb, ka = r.get("kalshi_bid"), r.get("kalshi_ask")
            poly_str = (
                f"bid={pb:.3f}  ask={pa:.3f}"
                if pb is not None and pa is not None else "no live CLOB / frozen"
            )
            kalshi_str = (
                f"bid={kb:.3f}  ask={ka:.3f}"
                if kb is not None and ka is not None else "no live book"
            )
            print(f"         Prices: Poly [{poly_str}]   Kalshi [{kalshi_str}]")
            print(f"         Close:  Poly={r['poly_close'] or '—':<12} "
                  f"Kalshi={r['kalshi_close'] or '—':<12} "
                  f"Δ={str(r['close_delta_days'])+'d' if r['close_delta_days'] is not None else '?'}")
        print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Organically discover cross-exchange market pairs across "
                    "Kalshi and Polymarket.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--category", default="all",
                   choices=["all", "election", "sports", "economic", "political", "pop"],
                   help="Event category filter")
    p.add_argument("--days", type=int, default=None,
                   help="Only scan events closing within this many days; omit for no day limit")
    p.add_argument("--min-sim", type=float, default=0.30,
                   help="Minimum Jaccard title similarity to report a pair")
    p.add_argument("--show-prices", action="store_true",
                   help="Fetch live orderbooks for matched pairs (slower)")
    p.add_argument("--max-events", type=int, default=None,
                   help="Maximum Kalshi events to scan; omit for no event limit")
    p.add_argument("--poly-scan", type=int, default=None,
                   help="Cap on Polymarket events scanned (not an offset — the "
                        "catalog is walked via /events/keyset); omit to scan the whole catalog")
    p.add_argument("--kalshi-workers", type=int, default=6,
                   help="Concurrent workers for Kalshi event-market fetches")
    p.add_argument("--no-market-sweep", dest="market_sweep", action="store_false",
                   help="Skip BOTH orphan sweeps — Kalshi's /markets?status=open&"
                        "mve_filter=exclude walk (faster, misses markets whose parent "
                        "event never appears in /events) and Polymarket's /markets/keyset "
                        "walk (faster, misses ~480 markets whose parent event is "
                        "archived/inactive). Sweeps run by default: passing this flag is "
                        "what turns them off (%(default)s shown below is market_sweep's "
                        "own default, i.e. sweeps ON, not this flag's)")
    p.add_argument("--enrich-margin", type=float, default=None,
                   help="Catalog-price screen (off by default since iteration 3 — batched "
                        "Kalshi book fetches make enriching every endorsed pair cost "
                        "seconds, not minutes): when set, only fetch live orderbooks for "
                        "v2-endorsed pairs within this much of a positive catalog edge (or "
                        "with a missing/stale catalog price)")
    p.add_argument("--no-enrich-margin", dest="enrich_margin", action="store_const", const=None,
                   help="Fetch live orderbooks for every v2-endorsed pair (the default; "
                        "kept for explicitness / scripts written against the old flag)")
    p.add_argument("--coverage-json", default=None,
                   help="Write the full per-venue ingestion coverage accounting to this JSON file")
    p.add_argument("--output", default=None,
                   help="Save matched pairs to this JSON file")
    args = p.parse_args()

    print(f"\nDiscover  category={args.category}  days={args.days}  "
          f"min_sim={args.min_sim}  max_events={args.max_events}\n")

    coverage: dict = {}
    results = discover(
        category=args.category,
        days=args.days,
        min_sim=args.min_sim,
        show_prices=args.show_prices,
        max_poly_offset=args.poly_scan,
        max_events_to_search=args.max_events,
        kalshi_workers=args.kalshi_workers,
        market_sweep=args.market_sweep,
        enrich_margin=args.enrich_margin,
        coverage=coverage,
    )

    _print_results(results, show_prices=args.show_prices)

    if args.output and results:
        Path(args.output).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"Saved {len(results)} pairs → {args.output}")

    if args.coverage_json:
        Path(args.coverage_json).write_text(json.dumps(coverage, indent=2, default=str), encoding="utf-8")
        print(f"Saved coverage accounting → {args.coverage_json}")


if __name__ == "__main__":
    main()

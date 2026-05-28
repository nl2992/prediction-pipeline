"""
Organic Cross-Exchange Market Discovery
========================================
Scans the full Kalshi events catalog, auto-derives search keywords from each
event title, searches Polymarket for matching markets, and runs the matcher to
surface real cross-exchange price comparisons — with live bid/ask spreads.

No manual slug or series configuration.  Just run it.

Usage
-----
    python discover.py                          # everything active, no horizon cap
    python discover.py --category election      # Senate/House/Governor/primary
    python discover.py --category sports        # NBA/MLB/NFL/NHL/soccer/etc.
    python discover.py --category economic      # Fed/crypto/GDP/CPI/jobs
    python discover.py --category political     # SCOTUS/Trump/legislation/intl
    python discover.py --category pop           # entertainment/celebrity
    python discover.py --days 90                # closing within 90 days
    python discover.py --min-sim 0.30           # stricter title matching
    python discover.py --show-prices            # fetch live orderbooks for pairs
    python discover.py --output pairs.json      # save results to JSON

What gets excluded
------------------
Only Kalshi multi-event parlays are excluded — these are markets with titles
like "yes Yankees, yes OKC wins by 4.5, yes LeBron: 25+" that bundle multiple
props into one contract.  Every other market type (elections, sports game
winners, economic indicators, entertainment, politics) is included.

Algorithm
---------
1. Fetch all active Kalshi events until the API cursor is exhausted
2. Exclude multi-event parlays; filter by category and close-date horizon
3. For each event, fetch its markets and auto-derive Polymarket search keywords
4. Search Polymarket with those keywords (paginated catalog scan)
5. Build MarketSnapshots from both sides
6. Run Jaccard+close-time matcher
7. Optionally fetch live orderbooks for matched pairs
8. Print ranked pairs with bid/ask spread and arb signals
"""

from __future__ import annotations

import argparse
import json
import re
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


def _is_parlay(event: dict) -> bool:
    """Return True only for multi-event combo markets and stats-only markets."""
    et = (event.get("event_ticker") or "").upper()
    if et.startswith(_PARLAY_EVENT_PREFIXES):
        return True
    title = event.get("title", "")
    if _PARLAY_TITLE_RE.match(title):
        return True
    if _STATS_ONLY_RE.search(title):
        return True
    return False


def _is_parlay_market(market: dict) -> bool:
    """Also filter individual markets with parlay-format titles."""
    title = market.get("title", "")
    if _PARLAY_TITLE_RE.match(title):
        return True
    # "Will X win Y, X win Z, and X win W?" — three or more "win" occurrences
    # signals a multi-leg parlay even when the yes/no prefix is absent.
    if len(re.findall(r"\bwin\b", title, re.IGNORECASE)) >= 3:
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

    # 1. Proper noun pairs — most specific signal
    for m in _PROPER_PAIR_RE.finditer(title):
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


def _k_snap(m: dict, fetched_at: str, event_title: str = ""):
    from pipeline import MarketSnapshot, _parse_kalshi_top_of_book
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
        extra={"event_title": event_title},
    )


def _p_snap(m: dict, fetched_at: str):
    """Build a Polymarket snapshot from a raw /markets result (legacy path)."""
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
    # For categorical markets, groupItemTitle is the short outcome label
    # (e.g. "France", "Ken Paxton", "No change").  For binary markets it is
    # absent, so fall back to the full question text.
    outcome_label = m.get("groupItemTitle") or m.get("question") or m.get("title", "")
    group_slug = m.get("groupSlug") or m.get("slug", "")
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
        },
    )


def _p_snap_from_event(m: dict, ev_title: str, ev_slug: str, fetched_at: str):
    """Build a Polymarket snapshot from a market embedded in a /events response.

    Uses the parent event's title and slug for group-level matching, and
    groupItemTitle (short outcome label like "France") for outcome matching.
    """
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
    outcome_label = m.get("groupItemTitle") or m.get("question") or m.get("title", "")
    close = m.get("endDate") or m.get("endDateIso")
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
        },
    )


# ---------------------------------------------------------------------------
# Live orderbook enrichment (only for confirmed pairs)
# ---------------------------------------------------------------------------

def _enrich_kalshi(snaps: list) -> None:
    from kalshi.client import KalshiClient
    from pipeline import _parse_kalshi_full_book
    client = KalshiClient()
    for snap in snaps:
        try:
            raw = client.get_orderbook(snap.market_id)
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
    "unchanged": "hold", "unchanged": "hold",
    "pause": "hold", "steady": "hold",
    "no change": "hold",
}


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
        k_mid = k.orderbook.mid or k.orderbook.best_bid
        for p in p_outcomes:
            if not is_compatible_match(p, k):
                continue
            if not is_close_time_compatible(p, k):
                continue
            p_toks = _normalise_tokens(p.title)
            p_mid = p.orderbook.mid or p.orderbook.best_bid

            title_sim = _jaccard(k_toks, p_toks)
            # Price proximity is useful only after the two outcomes share some
            # lexical evidence.  Without this floor, a categorical Polymarket
            # outcome like "Andy Beshear" can match a generic Kalshi question
            # such as "Who will win the next presidential election?" solely
            # because their catalogue prices happen to be close.
            if title_sim < 0.15:
                continue

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

    scored.sort(key=lambda x: x[0], reverse=True)
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
    from matcher import _tokens, _jaccard, match_markets

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
    p_events_by_token: dict[str, set[str]] = {}
    for p_eid, toks in p_etoks.items():
        for tok in toks:
            p_events_by_token.setdefault(tok, set()).add(p_eid)

    # ── Step 1: match event groups ────────────────────────────────────────────
    event_scores: list[tuple[float, str, str]] = []
    for k_eid, k_et in k_etitles.items():
        k_toks = _tokens(k_et)
        if not k_toks:
            continue
        candidate_p_eids: set[str] = set()
        for tok in k_toks:
            candidate_p_eids.update(p_events_by_token.get(tok, ()))
        for p_eid in candidate_p_eids:
            p_toks = p_etoks[p_eid]
            sim = _jaccard(k_toks, p_toks)
            if sim >= min_sim:
                event_scores.append((sim, k_eid, p_eid))

    event_scores.sort(key=lambda x: x[0], reverse=True)
    matched_k_events: set[str] = set()
    matched_p_events: set[str] = set()
    used_k: set[str] = set()
    used_p: set[str] = set()
    all_pairs: list = []

    for event_sim, k_eid, p_eid in event_scores:
        if k_eid in matched_k_events or p_eid in matched_p_events:
            continue
        matched_k_events.add(k_eid)
        matched_p_events.add(p_eid)

        k_outs = [s for s in k_groups[k_eid] if s.market_id not in used_k]
        p_outs = [s for s in p_groups[p_eid] if s.market_id not in used_p]

        for pair in _match_outcomes_within_group(k_outs, p_outs, event_sim):
            all_pairs.append(pair)
            used_k.add(pair.kalshi.market_id)
            used_p.add(pair.poly.market_id)

    # ── Step 2: individual Jaccard for remaining markets ──────────────────────
    # Use stricter thresholds than group matching:
    #   min_sim raised to 0.45 to stop short generic labels from matching
    #   min_token_ratio=0.40 blocks "Democratic Party" (2 toks) matching a
    #   long parlay question (8 toks) — ratio 0.25 < 0.40 → rejected.
    rem_k = [s for s in k_snaps if s.market_id not in used_k]
    rem_p = [s for s in p_snaps if s.market_id not in used_p]
    if rem_k and rem_p:
        fallback = match_markets(
            rem_p, rem_k,
            min_title_similarity=max(min_sim, 0.45),
            max_close_delta_hours=100_000,
            min_token_ratio=0.40,
        )
        all_pairs.extend(fallback)

    all_pairs.sort(key=lambda x: -x.confidence)
    return all_pairs


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
    kalshi_workers: int = 24,
) -> list[dict]:
    """
    Run organic cross-exchange discovery and return matched pairs as dicts.
    """
    import logging
    logging.disable(logging.WARNING)

    from kalshi.client import KalshiClient
    from polymarket.client import PolymarketClient

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days) if days is not None else None
    fetched_at = now.isoformat()

    # ── 1. Kalshi event catalog ─────────────────────────────────────────────
    print("[1/5] Fetching Kalshi event catalog…", flush=True)
    kc = KalshiClient()
    t0 = time.time()
    all_events = kc.get_all_events(max_pages=None, page_size=200, status="open")
    print(f"      {len(all_events):,} events in {time.time()-t0:.1f}s")

    # ── 2. Filter events ─────────────────────────────────────────────────────
    filtered = []
    for ev in all_events:
        if _is_parlay(ev):
            continue
        close = _parse_dt(ev.get("close_time") or ev.get("end_date"))
        # Determine the category first so we can apply category-appropriate filters
        ev_cat = _category(ev)
        if category != "all" and ev_cat != category:
            continue
        # Keep events with unknown close time (might resolve soon) or within horizon.
        # Sports contracts on Kalshi often carry a far-out contractual expiry date
        # (e.g. 2028) even though the market resolves as soon as the game/series
        # ends.  Skip only events that have already closed; accept sports events
        # regardless of their stated close date.
        if close and close < now:
            continue
        if horizon is not None and close and close > horizon and ev_cat != "sports":
            continue
        filtered.append(ev)

    # Prioritise events closing sooner (more liquid, more likely to match)
    def _sort_key(ev):
        dt = _parse_dt(ev.get("close_time") or ev.get("end_date"))
        return dt if dt else now + timedelta(days=9999)

    filtered.sort(key=_sort_key)
    if max_events_to_search is not None:
        filtered = filtered[:max_events_to_search]

    cat_counts: dict[str, int] = {}
    for ev in filtered:
        c = _category(ev)
        cat_counts[c] = cat_counts.get(c, 0) + 1

    horizon_label = f"within {days} days" if days is not None else "with no day limit"
    print(f"[2/5] Filtered to {len(filtered)} events {horizon_label}  "
          f"({', '.join(f'{c}={n}' for c, n in sorted(cat_counts.items()))})")
    if not filtered:
        print("      Nothing to scan.  Try --category all or --days 730.")
        return []

    # ── 3. Fetch Kalshi markets ──────────────────────────────────────────────
    print("[3/5] Fetching full Kalshi market catalog…", flush=True)
    t0 = time.time()
    k_snaps: list = []
    k_keywords: list[str] = []
    skipped_parlay = 0

    for ev in filtered:
        k_keywords.extend(_derive_keywords(ev.get("title", "")))

    filtered_by_ticker = {
        ev.get("event_ticker", ""): ev
        for ev in filtered
        if ev.get("event_ticker")
    }
    all_kalshi_markets: list[dict] = []
    if max_events_to_search is not None and len(filtered) <= 100:
        for ev in filtered:
            et = ev.get("event_ticker", "")
            if et:
                all_kalshi_markets.extend(
                    kc.get_all_markets(event_ticker=et, status="open", page_size=200)
                )
    else:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page = 0
        while True:
            resp = kc.get_markets(limit=1000, cursor=cursor, status="open")
            batch = resp.get("markets", [])
            all_kalshi_markets.extend(batch)
            page += 1
            if page % 25 == 0:
                print(f"      fetched {len(all_kalshi_markets):,} Kalshi market rows…", flush=True)
            next_cursor = resp.get("cursor")
            if not batch or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    for m in all_kalshi_markets:
        event_ticker = m.get("event_ticker", "")
        event = filtered_by_ticker.get(event_ticker)
        event_title = (event or {}).get("title") or m.get("event_title") or m.get("title", "")
        if event is None:
            market_cat = _category({"title": event_title or m.get("title", "")})
            if category != "all" and market_cat != category:
                continue
            close = _parse_dt(m.get("close_time") or m.get("expiration_time"))
            if close and close < now:
                continue
            if horizon is not None and close and close > horizon and market_cat != "sports":
                continue
        if _is_parlay_market(m):
            skipped_parlay += 1
            continue
        k_snaps.append(_k_snap(m, fetched_at, event_title=event_title))
        k_keywords.extend(_derive_keywords(event_title))
        k_keywords.extend(_derive_keywords(m.get("title", "")))

    # Deduplicate keywords from the events scan (before supplemental)
    k_keywords = [kw for kw in dict.fromkeys(k_keywords) if len(kw) > 2]
    k_keywords.sort(key=len, reverse=True)

    elapsed = time.time() - t0
    print(f"      {len(k_snaps):,} Kalshi markets ({skipped_parlay} parlay-format skipped) in {elapsed:.1f}s")
    print(f"      Derived {len(k_keywords)} Polymarket search keywords")
    if not k_snaps:
        print("      No Kalshi markets found.")
        return []

    # ── 4. Search Polymarket ─────────────────────────────────────────────────
    # Primary path: search /events so we get the event title + all outcome
    # markets in one call.  This enables two-level (event → outcome) matching
    # for categorical markets (Fed rate, FIFA World Cup, Senate primaries, …).
    # Fall back to the legacy /markets search for any keywords not matched.
    poly_scan_label = f"{max_poly_offset:,}-event catalog scan" if max_poly_offset is not None else "full event catalog scan"
    print(f"[4/5] Searching Polymarket events ({poly_scan_label})…", flush=True)
    t0 = time.time()
    pc = PolymarketClient()

    p_events = pc.search_events(
        keywords=k_keywords,
        active=True,
        closed=False,
        max_offset=max_poly_offset,
    )

    p_snaps: list = []
    seen_poly_ids: set[str] = set()

    for ev in p_events:
        ev_title = ev.get("title", "")
        ev_slug = ev.get("slug") or ev.get("id", "")
        markets = ev.get("markets") or []
        if not markets:
            # Event endpoint didn't embed markets; build a stub from the event
            # itself so the event title is still available for group matching.
            continue
        for m in markets:
            if not m.get("active", True):
                continue
            cid = m.get("conditionId") or m.get("id", "")
            if cid in seen_poly_ids:
                continue
            seen_poly_ids.add(cid)
            p_snaps.append(_p_snap_from_event(m, ev_title, ev_slug, fetched_at))

    # Fallback: if the event search found nothing (e.g. the /events endpoint
    # didn't embed markets for these results), try the legacy /markets search.
    if len(p_snaps) < 5:
        p_raw = pc.search_markets(
            keywords=k_keywords,
            active=True,
            closed=False,
            max_offset=max_poly_offset,
        )
        for m in p_raw:
            cid = m.get("conditionId") or m.get("id", "")
            if cid not in seen_poly_ids:
                seen_poly_ids.add(cid)
                p_snaps.append(_p_snap(m, fetched_at))

    elapsed_p = time.time() - t0
    print(f"      {len(p_snaps):,} Polymarket markets from {len(p_events)} events in {elapsed_p:.1f}s")
    if not p_snaps:
        print("      No Polymarket markets found.")
        return []

    # ── 5. Match ─────────────────────────────────────────────────────────────
    # Two-level group matching: first match Kalshi events to Polymarket events
    # by event-title similarity, then match outcomes within matched events by
    # outcome-label similarity + price proximity.  Remaining markets fall back
    # to the standard individual Jaccard matcher.
    print(f"[5/5] Running two-level group matcher (min_sim={min_sim})…", flush=True)
    pairs = _match_groups_then_individual(k_snaps, p_snaps, min_sim=min_sim)
    print(f"      {len(pairs)} pairs found")

    if show_prices and pairs:
        print(f"      Fetching live orderbooks for {len(pairs)} pairs…", flush=True)
        _enrich_kalshi([p.kalshi for p in pairs])
        _enrich_polymarket([p.poly for p in pairs])

    # ── Format results ────────────────────────────────────────────────────────
    from matcher import is_arb_eligible

    FEE = 0.07  # conservative worst-case fee
    results = []
    for pair in sorted(pairs, key=lambda x: -x.confidence):
        pb = pair.poly.orderbook.best_bid
        pa = pair.poly.orderbook.best_ask
        kb = pair.kalshi.orderbook.best_bid
        ka = pair.kalshi.orderbook.best_ask

        arb_dir = arb_profit = None
        arb_eligible = is_arb_eligible(pair.poly, pair.kalshi)
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
            "kalshi_close":     (pair.kalshi.close_time or "")[:10],
            "kalshi_bid":       kb,
            "kalshi_ask":       ka,
            "arb_eligible":     arb_eligible,
            "arb_direction":    arb_dir,
            "arb_net_profit":   arb_profit,
        })
    return results


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
                   help="Polymarket catalog depth (max offset); omit for no catalog offset limit")
    p.add_argument("--kalshi-workers", type=int, default=24,
                   help="Concurrent workers for Kalshi event-market fetches")
    p.add_argument("--output", default=None,
                   help="Save matched pairs to this JSON file")
    args = p.parse_args()

    print(f"\nDiscover  category={args.category}  days={args.days}  "
          f"min_sim={args.min_sim}  max_events={args.max_events}\n")

    results = discover(
        category=args.category,
        days=args.days,
        min_sim=args.min_sim,
        show_prices=args.show_prices,
        max_poly_offset=args.poly_scan,
        max_events_to_search=args.max_events,
        kalshi_workers=args.kalshi_workers,
    )

    _print_results(results, show_prices=args.show_prices)

    if args.output and results:
        Path(args.output).write_text(json.dumps(results, indent=2, default=str))
        print(f"Saved {len(results)} pairs → {args.output}")


if __name__ == "__main__":
    main()

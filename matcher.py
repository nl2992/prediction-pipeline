"""
Cross-exchange market matcher.

Pairs Polymarket and Kalshi MarketSnapshot objects that represent the same
real-world binary event, using title-token Jaccard similarity and close-time
proximity.  No external dependencies beyond the standard library.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline import MarketSnapshot

# ---------------------------------------------------------------------------
# Title normalisation
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "the", "to", "of", "in", "on", "at", "be", "is", "by",
    "for", "and", "or", "not", "no", "yes", "will", "would", "does",
    "do", "did", "has", "have", "had", "can", "could", "should",
    "may", "might", "this", "that", "with", "from", "which", "who",
    "when", "where", "what", "how", "are", "was", "were", "been",
    "before", "after", "than", "more", "any", "all", "as", "if",
    "it", "its", "their", "they", "he", "she", "we", "you", "i",
})


def _tokens(title: str) -> frozenset[str]:
    title = title.lower()
    # Preserve decimal numbers (e.g. "5.25%", "4.75") as single tokens before
    # stripping punctuation — otherwise "5.25" splits into "5" (dropped, len=1)
    # and "25", making different rate levels match each other.
    title = re.sub(r"\b(\d+\.\d+)%?", lambda m: m.group(1).replace(".", "_"), title)
    title = re.sub(r"[^\w\s]", " ", title)
    return frozenset(t for t in title.split() if t not in _STOPWORDS and len(t) > 1)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# ---------------------------------------------------------------------------
# Close-time parsing
# ---------------------------------------------------------------------------

_DT_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    iso = s.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    s = s.split("+")[0].rstrip("Z").rstrip("z")
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _close_delta_hours(t1: str | None, t2: str | None) -> float | None:
    dt1 = _parse_dt(t1)
    dt2 = _parse_dt(t2)
    if dt1 is None or dt2 is None:
        return None
    return abs((dt1 - dt2).total_seconds()) / 3600.0


def _confidence(title_sim: float, delta_h: float | None, max_delta: float) -> float:
    """
    Combined score: 70% title similarity + 30% close-time proximity.
    If close time is unknown, title similarity alone determines the score.
    """
    if delta_h is None:
        return title_sim
    if max_delta <= 0:
        return title_sim
    time_score = max(0.0, 1.0 - delta_h / max_delta)
    return 0.70 * title_sim + 0.30 * time_score


# ---------------------------------------------------------------------------
# Match result
# ---------------------------------------------------------------------------


@dataclass
class MatchedPair:
    """A Polymarket + Kalshi snapshot identified as the same event."""

    poly: "MarketSnapshot"
    kalshi: "MarketSnapshot"
    title_similarity: float           # Jaccard score [0, 1]
    close_delta_hours: float | None   # |close_time difference| in hours; None if unknown
    confidence: float                 # combined score [0, 1]
    via_override: bool = False        # True if paired by manual override, not heuristic


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_markets(
    poly_snaps: list["MarketSnapshot"],
    kalshi_snaps: list["MarketSnapshot"],
    min_title_similarity: float = 0.30,
    max_close_delta_hours: float = 72.0,
    overrides: list[tuple[str, str]] | None = None,
    min_token_ratio: float = 0.0,
) -> list[MatchedPair]:
    """
    Pair Polymarket and Kalshi snapshots representing the same event.

    Parameters
    ----------
    poly_snaps, kalshi_snaps
        Output from ``fetch_polymarket`` / ``fetch_kalshi``.
    min_title_similarity
        Minimum Jaccard title similarity to consider a heuristic match.
    max_close_delta_hours
        Maximum allowed close-time difference in hours.
    overrides
        Manual pairings as ``[(poly_market_id, kalshi_ticker), ...]``.
        These bypass the heuristic and are always included.

    Returns
    -------
    list of MatchedPair, sorted by confidence descending (overrides first).
    """
    pairs: list[MatchedPair] = []
    used_poly: set[str] = set()
    used_kalshi: set[str] = set()

    # Manual overrides — always accepted regardless of similarity scores
    if overrides:
        poly_by_id = {s.market_id: s for s in poly_snaps}
        kalshi_by_id = {s.market_id: s for s in kalshi_snaps}
        for poly_id, kalshi_id in overrides:
            p = poly_by_id.get(poly_id)
            k = kalshi_by_id.get(kalshi_id)
            if p and k:
                pairs.append(MatchedPair(
                    poly=p,
                    kalshi=k,
                    title_similarity=_jaccard(_tokens(p.title), _tokens(k.title)),
                    close_delta_hours=_close_delta_hours(p.close_time, k.close_time),
                    confidence=1.0,
                    via_override=True,
                ))
                used_poly.add(poly_id)
                used_kalshi.add(kalshi_id)

    # Heuristic matching on unmatched markets
    remaining_poly = [s for s in poly_snaps if s.market_id not in used_poly]
    remaining_kalshi = [s for s in kalshi_snaps if s.market_id not in used_kalshi]

    poly_tok = {s.market_id: _tokens(s.title) for s in remaining_poly}
    kalshi_tok = {s.market_id: _tokens(s.title) for s in remaining_kalshi}

    # Score all candidate pairs.
    # Close-time delta is a scoring signal only — never a hard exclusion gate.
    # Kalshi sports series carry a contractual far-out expiry (e.g. 2028 for
    # the current NBA Finals) even though the market resolves this season.
    # Hard-filtering by delta_h would silently drop all such sports pairs.
    scored: list[tuple[float, "MarketSnapshot", "MarketSnapshot"]] = []
    for p in remaining_poly:
        p_toks = poly_tok[p.market_id]
        for k in remaining_kalshi:
            k_toks = kalshi_tok[k.market_id]
            sim = _jaccard(p_toks, k_toks)
            if sim < min_title_similarity:
                continue
            # Token-ratio guard: block short labels ("Democratic Party", 2 tokens)
            # from matching long questions (8+ tokens) via coincidental Jaccard.
            if min_token_ratio > 0 and p_toks and k_toks:
                shorter = min(len(p_toks), len(k_toks))
                longer  = max(len(p_toks), len(k_toks))
                if shorter / longer < min_token_ratio:
                    continue
            delta_h = _close_delta_hours(p.close_time, k.close_time)
            score = _confidence(sim, delta_h, max_close_delta_hours)
            scored.append((score, p, k))

    # Greedy 1-1 matching: highest score first
    scored.sort(key=lambda x: x[0], reverse=True)
    matched_poly: set[str] = set()
    matched_kalshi: set[str] = set()
    for score, p, k in scored:
        if p.market_id in matched_poly or k.market_id in matched_kalshi:
            continue
        pairs.append(MatchedPair(
            poly=p,
            kalshi=k,
            title_similarity=_jaccard(_tokens(p.title), _tokens(k.title)),
            close_delta_hours=_close_delta_hours(p.close_time, k.close_time),
            confidence=score,
        ))
        matched_poly.add(p.market_id)
        matched_kalshi.add(k.market_id)

    pairs.sort(key=lambda x: (not x.via_override, -x.confidence))
    return pairs

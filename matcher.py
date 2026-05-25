"""
Cross-exchange market matcher.

Pairs Polymarket and Kalshi MarketSnapshot objects that represent the same
real-world binary event, using title-token Jaccard similarity and close-time
proximity.  No external dependencies beyond the standard library.
"""

from __future__ import annotations

import re
import unicodedata
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
# False-positive guards
# ---------------------------------------------------------------------------

_SPORT_TERMS = frozenset({
    "nba", "nfl", "mlb", "nhl", "mls", "baseball", "basketball", "football",
    "hockey", "soccer", "championship", "finals", "super", "bowl",
    "world", "series", "stanley", "cup", "playoff", "game",
})

_ELECTION_TERMS = frozenset({
    "election", "presidential", "presidency", "president", "senate", "senator",
    "governor", "governorship", "gubernatorial", "primary", "nominee",
    "nomination", "democratic", "democratics", "democrat", "republican",
    "republicans", "attorney", "general", "secretary", "state", "mayor",
    "minister", "parliament", "parliamentary",
})

_ECON_TERMS = frozenset({
    "fed", "federal", "funds", "rate", "rates", "cut", "cuts", "hike",
    "hikes", "inflation", "cpi", "gdp", "bitcoin", "btc", "ethereum",
    "eth", "oil", "gold", "nasdaq", "dow", "trillionaire", "billionaire",
    "debt",
})

_OFFICE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("president", r"\b(president|presidential|presidency)\b"),
    ("senate", r"\b(senate|senator)\b"),
    ("house", r"\b(house|representative|congressional district|congress)\b"),
    ("governor", r"\b(governor|governorship|gubernatorial)\b"),
    ("attorney_general", r"\battorney\s+general\b"),
    ("secretary_state", r"\bsecretary\s+of\s+state\b"),
    ("mayor", r"\b(mayor|mayoral)\b"),
    ("prime_minister", r"\b(prime\s+minister|pm)\b"),
)

_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "brazil": ("brazil", "brazilian"),
    "colombia": ("colombia", "colombian"),
    "france": ("france", "french"),
    "ghana": ("ghana", "ghanian", "ghanaian"),
    "moldova": ("moldova", "moldovan"),
    "philippines": ("philippines", "philippine", "filipino"),
    "turkey": ("turkey", "turkish"),
    "united kingdom": ("united kingdom", "uk", "britain", "british"),
    "israel": ("israel", "israeli"),
}

_US_STATE_ALIASES: dict[str, tuple[str, ...]] = {
    "alabama": ("alabama", " al "),
    "alaska": ("alaska", " ak "),
    "arizona": ("arizona", " az "),
    "arkansas": ("arkansas", " ar "),
    "california": ("california", " ca "),
    "colorado": ("colorado", " co "),
    "connecticut": ("connecticut", " ct "),
    "delaware": ("delaware", " de "),
    "florida": ("florida", " fl "),
    "georgia": ("georgia", " ga "),
    "hawaii": ("hawaii", " hi "),
    "idaho": ("idaho", " id "),
    "illinois": ("illinois", " il "),
    "indiana": ("indiana", " in "),
    "iowa": ("iowa", " ia "),
    "kansas": ("kansas", " ks "),
    "kentucky": ("kentucky", " ky "),
    "louisiana": ("louisiana", " la "),
    "maine": ("maine", " me "),
    "maryland": ("maryland", " md "),
    "massachusetts": ("massachusetts", " ma "),
    "michigan": ("michigan", " mi "),
    "minnesota": ("minnesota", " mn "),
    "mississippi": ("mississippi", " ms "),
    "missouri": ("missouri", " mo "),
    "montana": ("montana", " mt "),
    "nebraska": ("nebraska", " ne "),
    "nevada": ("nevada", " nv "),
    "new hampshire": ("new hampshire", " nh "),
    "new jersey": ("new jersey", " nj "),
    "new mexico": ("new mexico", " nm "),
    "new york": ("new york", " ny "),
    "north carolina": ("north carolina", " nc "),
    "north dakota": ("north dakota", " nd "),
    "ohio": ("ohio", " oh "),
    "oklahoma": ("oklahoma", " ok "),
    "oregon": ("oregon", " or "),
    "pennsylvania": ("pennsylvania", " pa "),
    "rhode island": ("rhode island", " ri "),
    "south carolina": ("south carolina", " sc "),
    "south dakota": ("south dakota", " sd "),
    "tennessee": ("tennessee", " tn "),
    "texas": ("texas", " tx "),
    "utah": ("utah", " ut "),
    "vermont": ("vermont", " vt "),
    "virginia": ("virginia", " va "),
    "washington": ("washington", " wa "),
    "west virginia": ("west virginia", " wv "),
    "wisconsin": ("wisconsin", " wi "),
    "wyoming": ("wyoming", " wy "),
}


def _ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _snapshot_text(s: "MarketSnapshot") -> str:
    extra = getattr(s, "extra", {}) or {}
    return " ".join(
        str(x)
        for x in (
            getattr(s, "title", ""),
            getattr(s, "event_id", ""),
            extra.get("event_title", ""),
            extra.get("full_question", ""),
        )
        if x
    )


def _domains(text: str) -> set[str]:
    toks = _tokens(text)
    found: set[str] = set()
    if toks & _SPORT_TERMS:
        found.add("sports")
    if toks & _ELECTION_TERMS:
        found.add("election")
    if toks & _ECON_TERMS:
        found.add("economic")
    return found


def _offices(text: str) -> set[str]:
    low = _ascii_lower(text)
    return {office for office, pat in _OFFICE_PATTERNS if re.search(pat, low)}


def _parties(text: str) -> set[str]:
    low = _ascii_lower(text)
    parties: set[str] = set()
    if re.search(r"\b(rep|republican|republicans|gop)\b", low):
        parties.add("republican")
    if re.search(r"\b(dem|democrat|democrats|democratic|democratics|labour)\b", low):
        parties.add("democratic")
    if re.search(r"\b(conservative|tory|tories)\b", low):
        parties.add("conservative")
    return parties


def _jurisdictions(text: str) -> set[str]:
    low = f" {_ascii_lower(text)} "
    found: set[str] = set()
    for canonical, aliases in _COUNTRY_ALIASES.items():
        if any(alias in low for alias in aliases):
            found.add(canonical)
    for canonical, aliases in _US_STATE_ALIASES.items():
        if any(alias in low for alias in aliases):
            found.add(canonical)
    return found


def _years(text: str) -> set[str]:
    years = set(re.findall(r"\b(20\d{2})\b", text))
    for yy in re.findall(r"(?:^|[-\s])(\d{2})(?:$|[-\s])", text):
        if 20 <= int(yy) <= 49:
            years.add(f"20{yy}")
    return years


def _rates(text: str) -> set[str]:
    return set(re.findall(r"\b\d+(?:\.\d+)?\s*%|\b\d+\.\d+\b", text))


_NAME_STOP = {
    "will", "who", "which", "what", "when", "where", "how", "republican",
    "democratic", "democrat", "republicans", "democrats", "senate", "house",
    "governor", "president", "presidential", "new york", "north carolina",
    "south carolina", "south dakota", "north dakota", "united states",
}

_LEADING_QUESTION_WORDS = re.compile(
    r"\b(Will|Who|Which|What|When|Where|How|Does|Is|Are|Can|Should)\b\s+"
)


def _proper_names(text: str) -> set[str]:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    ascii_text = _LEADING_QUESTION_WORDS.sub("", ascii_text)
    names = set()
    for match in re.finditer(r"\b([A-Z][a-z]{2,}(?:\s+(?:de|la|le|van|von|the))?\s+[A-Z][A-Za-z]{1,})\b", ascii_text):
        name = match.group(1).lower()
        if name not in _NAME_STOP:
            names.add(name)
    return names


def _contract_actions(text: str) -> set[str]:
    low = _ascii_lower(text)
    actions: set[str] = set()
    if re.search(r"\b(run|runs|running|declare|declares|first this list)\b", low):
        actions.add("run_or_declare")
    if re.search(r"\b(win|wins|winner)\b.*\b(nominee|nomination)\b|\b(nominee|nomination)\b.*\b(win|wins|winner)\b", low):
        actions.add("win_nomination")
    elif re.search(r"\b(nominee|nomination)\b", low):
        actions.add("nomination")
    if re.search(r"\b(defeat|defeats)\b", low):
        actions.add("head_to_head")
    elif re.search(r"\b(win|wins|winner)\b", low):
        actions.add("win")
    if re.search(r"\bfinish(?:es)?\s+(?:1st|first|2nd|second|3rd|third)\b", low):
        actions.add("finish_position")
    if re.search(r"\bticket\b|running mate|vice president", low):
        actions.add("ticket")
    if re.search(r"\b(occur|occurs|happen|happens|held|take place)\b", low):
        actions.add("occur")
    if re.search(r"\b(leave|resign|depart|ousted|fired)\b", low):
        actions.add("leave_role")
    if re.search(r"\bmeet next\b|where will .*meet|next meet", low):
        actions.add("meeting_location")
    if re.search(r"\bbecome\s+prime\s+minister|next\s+prime\s+minister", low):
        actions.add("become_pm")
    return actions


def _is_generic_winner_market(text: str) -> bool:
    low = _ascii_lower(text)
    return bool(re.search(r"\b(who|which)\s+will\s+win\b|\bwho\s+will\s+be\b", low))


def _is_party_contract(text: str) -> bool:
    return bool(_parties(text)) and not _proper_names(text)


def _is_generic_location_market(text: str) -> bool:
    low = _ascii_lower(text)
    return bool(re.search(r"\bwhere\s+will\b|where .* next meet", low))


def _is_specific_location_option(text: str) -> bool:
    low = _ascii_lower(text)
    return bool(re.search(r"\bmeet\s+next\s+in\b|\bnext\s+meet\s+in\b", low))


def is_compatible_match(poly: "MarketSnapshot", kalshi: "MarketSnapshot") -> bool:
    """Return False for high-confidence false-positive patterns."""
    p_text = _snapshot_text(poly)
    k_text = _snapshot_text(kalshi)
    p_domains = _domains(p_text)
    k_domains = _domains(k_text)
    if p_domains and k_domains and p_domains.isdisjoint(k_domains):
        return False
    if (p_domains == {"election"} and not k_domains) or (k_domains == {"election"} and not p_domains):
        return False

    if "election" in p_domains and "election" in k_domains:
        p_offices = _offices(p_text)
        k_offices = _offices(k_text)
        if p_offices and k_offices and p_offices.isdisjoint(k_offices):
            return False

        p_parties = _parties(p_text)
        k_parties = _parties(k_text)
        if p_parties and k_parties and p_parties.isdisjoint(k_parties):
            return False

        p_juris = _jurisdictions(p_text)
        k_juris = _jurisdictions(k_text)
        if p_juris and k_juris and p_juris.isdisjoint(k_juris):
            return False

        p_names = _proper_names(p_text)
        k_names = _proper_names(k_text)
        if p_names and k_names and p_names.isdisjoint(k_names):
            return False
        if (p_names and not k_names and _is_generic_winner_market(k_text)) or (
            k_names and not p_names and _is_generic_winner_market(p_text)
        ):
            return False
        if (p_names and _is_party_contract(k_text)) or (k_names and _is_party_contract(p_text)):
            return False

    p_actions = _contract_actions(p_text)
    k_actions = _contract_actions(k_text)
    if p_actions and k_actions and p_actions.isdisjoint(k_actions):
        return False
    if (_proper_names(p_text) and _is_generic_location_market(k_text)) or (
        _proper_names(k_text) and _is_generic_location_market(p_text)
    ):
        return False
    if (_is_specific_location_option(p_text) and _is_generic_location_market(k_text)) or (
        _is_specific_location_option(k_text) and _is_generic_location_market(p_text)
    ):
        return False

    p_years = _years(p_text)
    k_years = _years(k_text)
    if p_years and k_years and p_years.isdisjoint(k_years):
        return False

    p_rates = _rates(p_text)
    k_rates = _rates(k_text)
    if p_rates and k_rates and p_rates.isdisjoint(k_rates):
        return False

    return True


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


def is_close_time_compatible(
    poly: "MarketSnapshot",
    kalshi: "MarketSnapshot",
    max_non_sports_delta_hours: float = 24 * 400,
) -> bool:
    """
    Reject non-sports pairs with clearly incompatible resolution horizons.

    Sports markets often carry contractual far-out expiry dates, so they keep
    the old soft-scoring behavior. For political/election/pop markets, a
    multi-year close-date gap usually means a different market scope, not a
    tradable equivalent.
    """
    delta_h = _close_delta_hours(poly.close_time, kalshi.close_time)
    if delta_h is None:
        return True
    domains = _domains(_snapshot_text(poly)) | _domains(_snapshot_text(kalshi))
    if "sports" in domains:
        return True
    return delta_h <= max_non_sports_delta_hours


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
            if not is_compatible_match(p, k):
                continue
            if not is_close_time_compatible(p, k):
                continue
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

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
    "united states": ("united states", " us ", " usa ", " u s "),
    "israel": ("israel", "israeli"),
    "taiwan": ("taiwan", "taiwanese"),
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
    "hawaii": ("hawaii",),
    "idaho": ("idaho", " id "),
    "illinois": ("illinois", " il "),
    "indiana": ("indiana",),
    "iowa": ("iowa", " ia "),
    "kansas": ("kansas", " ks "),
    "kentucky": ("kentucky", " ky "),
    "louisiana": ("louisiana",),
    "maine": ("maine",),
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
    "oregon": ("oregon",),
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


def _contract_text(s: "MarketSnapshot") -> str:
    extra = getattr(s, "extra", {}) or {}
    return " ".join(
        str(x)
        for x in (
            getattr(s, "title", ""),
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


def _stat_thresholds(text: str) -> dict[str, set[float]]:
    low = _ascii_lower(text)
    stats = {
        "points": r"\b(points?|pts)\b",
        "rebounds": r"\b(rebounds?|rbs?)\b",
        "assists": r"\b(assists?|asts?)\b",
        "hits": r"\bhits?\b",
        "strikeouts": r"\b(strikeouts?|ks?)\b",
        "blocks": r"\b(blocks?|blks?)\b",
        "threes": r"\b(threes?|three[-\s]?pointers?|3[-\s]?pointers?)\b",
        "runs": r"\bruns?\b",
        "goals": r"\bgoals?\b",
    }
    nums = {float(n) for n in re.findall(r"\b(\d+(?:\.\d+)?)\s*(?:\+|o/u|over|under)?", low)}
    found: dict[str, set[float]] = {}
    for stat, pat in stats.items():
        if nums and re.search(pat, low):
            found[stat] = nums
    return found


def _has_over_under(text: str) -> bool:
    low = _ascii_lower(text)
    return bool(re.search(r"\bo/u\b|\bover\s*/\s*under\b|\bover\b|\bunder\b", low))


def _comparison_bounds(text: str) -> dict[str, set[float]]:
    low = _ascii_lower(text)
    return {
        "lt": {float(n) for n in re.findall(r"(?:<|less than|under|below)\s*(\d+(?:\.\d+)?)\s*%?", low)},
        "gt": {float(n) for n in re.findall(r"(?:>|more than|over|above)\s*(\d+(?:\.\d+)?)\s*%?", low)},
    }


_MONTHS = {
    "jan": "jan", "january": "jan",
    "feb": "feb", "february": "feb",
    "mar": "mar", "march": "mar",
    "apr": "apr", "april": "apr",
    "may": "may",
    "jun": "jun", "june": "jun",
    "jul": "jul", "july": "jul",
    "aug": "aug", "august": "aug",
    "sep": "sep", "sept": "sep", "september": "sep",
    "oct": "oct", "october": "oct",
    "nov": "nov", "november": "nov",
    "dec": "dec", "december": "dec",
}


def _time_scopes(text: str) -> set[str]:
    low = _ascii_lower(text)
    scopes: set[str] = set()
    for month, day in re.findall(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+(\d{1,2})\b",
        low,
    ):
        scopes.add(f"day:{_MONTHS[month]}-{int(day):02d}")
    for month in re.findall(
        r"\bin\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        low,
    ):
        scopes.add(f"month:{_MONTHS[month]}")
    if not scopes and re.search(
        r"\bthis year\b|\bin 20\d{2}\b|\bduring 20\d{2}\b|\bby end of 20\d{2}\b",
        low,
    ):
        scopes.add("year")
    return scopes


def _set_numbers(text: str) -> set[str]:
    low = _ascii_lower(text)
    return set(re.findall(r"\bset\s*(\d+)\b", low))


def _draft_pick_numbers(text: str) -> set[str]:
    low = _ascii_lower(text)
    nums = set(re.findall(r"\b(\d+)(?:st|nd|rd|th)?\s+(?:overall\s+)?pick\b", low))
    nums.update(re.findall(r"\bpicked\s+(\d+)(?:st|nd|rd|th)?\b", low))
    return nums


def _is_generic_match_winner(text: str) -> bool:
    low = _ascii_lower(text)
    return bool(re.search(r"\bset\s+\d+\s+winner\b|\bmatch\s+winner\b", low))


def _is_unselected_vs_winner(text: str) -> bool:
    low = _ascii_lower(text)
    return bool(
        re.search(r"\bvs\.?\b", low)
        and re.search(r"\b(set\s+\d+\s+winner|match\s+winner)\b", low)
        and not re.search(r"\bwill\s+[a-z][a-z .'-]+\s+win\b", low)
    )


def _has_no_ipo(text: str) -> bool:
    return bool(re.search(r"\bno ipo\b|\bwithout an ipo\b", _ascii_lower(text)))


def _clean_matchup_side(side: str) -> frozenset[str]:
    side = re.split(r"[:\\-]", _ascii_lower(side), maxsplit=1)[0]
    side = re.sub(r"\b(winner|wins?|btts|both teams to score|more markets)\b", " ", side)
    return frozenset(t for t in re.split(r"\W+", side) if len(t) > 1)


def _matchup_signature(text: str) -> frozenset[frozenset[str]] | None:
    low = _ascii_lower(text)
    match = re.search(r"(.+?)\s+(?:vs\.?|at)\s+(.+)", low)
    if not match:
        return None
    left = _clean_matchup_side(match.group(1))
    right = _clean_matchup_side(match.group(2))
    if not left or not right:
        return None
    return frozenset((left, right))


def _selected_names(text: str) -> set[str]:
    low = _LEADING_QUESTION_WORDS.sub("", text)
    low_ascii = _ascii_lower(low)
    for splitter in (" vs. ", " vs ", " v. ", " at "):
        if splitter in low_ascii:
            low = low[:low_ascii.index(splitter)]
            break
    return _proper_names(low)


_NAME_STOP = {
    "will", "who", "which", "what", "when", "where", "how", "republican",
    "democratic", "democrat", "republicans", "democrats", "senate", "house",
    "governor", "president", "presidential", "new york", "north carolina",
    "south carolina", "south dakota", "north dakota", "united states",
    "championship winner", "world championship", "constructors champion",
    "drivers champion", "silver ball", "most valuable", "brazil president",
}
_GENERIC_NAME_TERMS = frozenset({
    "championship", "champion", "winner", "drivers", "constructors",
    "hockey", "world", "silver", "ball", "award", "nominee", "primary",
    "president", "senate", "race", "iihf",
})

_LEADING_QUESTION_WORDS = re.compile(
    r"\b(Will|Who|Which|What|When|Where|How|Does|Is|Are|Can|Should)\b\s+"
)


def _proper_names(text: str) -> set[str]:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    ascii_text = _LEADING_QUESTION_WORDS.sub("", ascii_text)
    names = set()
    for match in re.finditer(
        r"\b([A-Z][A-Za-z]{2,}(?:\s+(?:de|da|la|le|van|von|the|[A-Z][A-Za-z]{1,})){1,4})\b",
        ascii_text,
    ):
        name = match.group(1).lower()
        toks = _tokens(name)
        if _offices(name) and _jurisdictions(name):
            continue
        if name not in _NAME_STOP and not (toks and toks <= _GENERIC_NAME_TERMS):
            names.add(name)
    return names


def _names_overlap(a_names: set[str], b_names: set[str]) -> bool:
    return any(a == b or a in b or b in a for a in a_names for b in b_names)


_ENTITY_STOP = _NAME_STOP | {
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "roland garros", "nba playoffs", "atp", "wta", "mlb", "ufc", "fifa", "world cup",
    "set winner", "exact match", "match score", "ipo",
}


def _named_entities(text: str) -> set[str]:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    ascii_text = _LEADING_QUESTION_WORDS.sub("", ascii_text)
    entities = set(_proper_names(ascii_text))
    low = ascii_text.lower()
    for entity in ("bitcoin", "btc", "ethereum", "eth", "gold", "silver", "opec"):
        if re.search(rf"\b{entity}\b", low):
            entities.add(entity)
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]{1,})?)\b", ascii_text):
        entity = match.group(1).lower()
        if entity not in _ENTITY_STOP:
            entities.add(entity)
    return entities


def _contract_actions(text: str) -> set[str]:
    low = _ascii_lower(text)
    actions: set[str] = set()
    if re.search(r"\b(rain|snow|temperature|temp|weather)\b", low):
        actions.add("weather")
    if re.search(r"\bdraw\b", low):
        actions.add("draw")
    if re.search(r"\bbtts\b|\bboth teams to score\b", low):
        actions.add("btts")
    if re.search(r"\bipo\b|initial public offering|publicly list", low):
        actions.add("ipo")
    if re.search(r"\bbankrupt(?:cy)?\b", low):
        actions.add("bankruptcy")
    if re.search(r"\btake a stake\b|\bstake in\b|government stake", low):
        actions.add("government_stake")
    if re.search(r"\b(largest|biggest|highest|second-highest|third-highest|top|best|rank|ranking|#\s*\d+)\b.*\b(company|model|ai|movie|opening|album|revenue|ipo|market cap)\b|\b(company|model|ai|movie|opening|album|revenue|ipo|market cap)\b.*\b(largest|biggest|highest|second-highest|third-highest|top|best|rank|ranking|#\s*\d+)\b", low):
        actions.add("rank")
    if re.search(r"\brevenue\b", low):
        actions.add("revenue")
    if re.search(r"\b(chatbot arena|elo|benchmark|ai model)\b", low):
        actions.add("ai_benchmark")
    if re.search(r"\b(rotten tomatoes|metacritic|review score|critic score|audience score)\b", low):
        actions.add("review_score")
    if re.search(r"\b(opening weekend|opening week|box office)\b", low):
        actions.add("box_office")
    if re.search(r"\b(launch|expansion|available in)\b", low):
        actions.add("launch")
    if re.search(r"\brelocat(?:e|ed|ion)|moved away|away from\b", low):
        actions.add("relocate")
    if re.search(r"\bplayed in\b|\bhost\b|\bheld in\b|\btake place in\b", low):
        actions.add("host_location")
    if re.search(r"\b(unemployment|jobs report|payroll|nonfarm)\b", low):
        actions.add("labor_stats")
    if re.search(r"\binflation\b|\bcpi\b", low):
        actions.add("inflation")
    if re.search(r"\b(rate hike|rate cut|interest rate|bps|fomc|ecb|bank of england|bank of japan|fed)\b", low):
        actions.add("monetary_policy")
    if re.search(r"\b(points?|pts|rebounds?|rbs?|assists?|asts?|hits?|strikeouts?|blocks?|steals?|stls?|threes?|three[-\s]?pointers?|3[-\s]?pointers?|runs?|goals?)\b", low):
        actions.add("stat_prop")
    if re.search(r"\b(leader|lead|era leader)\b", low) and ("stat_prop" in actions or "era" in low):
        actions.add("stat_leader")
    if re.search(r"\bmvp\b|\bmost valuable player\b|\baward\b", low):
        actions.add("award")
    if re.search(r"\brelease\b.*\balbums?\b|\balbums?\b.*\brelease\b", low):
        actions.add("album_release")
    if re.search(r"\brelease\b.*\bsongs?\b|\bsongs?\b.*\brelease\b", low):
        actions.add("song_release")
    if re.search(r"#\s*1\s+albums?|\bnumber one albums?\b|\btop albums?\b", low):
        actions.add("album_chart")
    if re.search(r"#\s*1\s+artists?|\bbillboard\s+#?\s*1\s+artists?|\btop artists?\b", low):
        actions.add("artist_chart")
    if re.search(r"#\s*1\s+(songs?|hits?)|\bbillboard\s+#?\s*1\s+(songs?|hits?)|\btop (songs?|hits?)\b", low):
        actions.add("song_chart")
    if re.search(r"\bfight next\b|\bnext fight\b", low):
        actions.add("fight_next")
    if re.search(r"\blaunch a token\b|\btoken before\b|\btoken this year\b", low):
        actions.add("token_launch")
    if re.search(r"\bdraft\b|\boverall pick\b|\bpicked\s+\d+", low):
        actions.add("draft_pick")
    if re.search(r"\bteam to draft\b|\bdrafted by\b", low):
        actions.add("draft_team")
    if re.search(r"\bexact match score\b|\bset score\b|\bscore of \d+-\d+\b", low):
        actions.add("exact_score")
    if re.search(r"\bset\s+\d+\s+o/u\b|\bset\s+\d+\s+(?:total|over|under)\b", low):
        actions.add("set_total")
    if re.search(r"\bhandicap\b", low):
        actions.add("handicap")
    if re.search(r"\bwedding\b|\bbridesmaids?\b|\battend\b", low):
        actions.add("wedding_attendance")
    if re.search(r"\bhow many\b|\bnumber of\b", low):
        actions.add("count")
    if re.search(r"\bwin more\b|\bmore .* than\b", low):
        actions.add("comparison")
    if re.search(r"\bwinless\b", low):
        actions.add("winless")
    if re.search(r"\bengag(?:e|ed|ement)\b", low):
        actions.add("engagement")
    if re.search(r"\barrest(?:ed)?\b", low):
        actions.add("arrest")
    if re.search(r"\btrillionaire\b|\bbillionaire\b", low):
        actions.add("wealth_status")
    if re.search(r"\bminimum wage\b|\bwage\b", low):
        actions.add("wage_policy")
    if re.search(r"\boutperform\b", low):
        actions.add("comparison")
    if re.search(r"\bgroup [a-h]\b.*\bwin\b|\bteam from group\b", low):
        actions.add("group_winner")
    if re.search(r"\bbefore\b|by\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|q[1-4]|\d{1,2})", low):
        actions.add("deadline")
    if re.search(r"\bstarting qb\b|\bquarterback\b", low):
        actions.add("starting_qb")
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
    p_all_juris = _jurisdictions(p_text)
    k_all_juris = _jurisdictions(k_text)
    if p_all_juris and k_all_juris and p_all_juris.isdisjoint(k_all_juris):
        return False
    p_matchup = _matchup_signature(p_text)
    k_matchup = _matchup_signature(k_text)
    if p_matchup and k_matchup and p_matchup != k_matchup:
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
        if p_names and k_names and not _names_overlap(p_names, k_names):
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
    if ("rank" in p_actions) != ("rank" in k_actions):
        return False
    if ("stat_leader" in p_actions) != ("stat_leader" in k_actions):
        return False
    if ("deadline" in p_actions) != ("deadline" in k_actions):
        return False
    if ("comparison" in p_actions) != ("comparison" in k_actions):
        return False
    if ("count" in p_actions) != ("count" in k_actions):
        return False
    if _is_generic_match_winner(p_text) != _is_generic_match_winner(k_text):
        return False
    if _is_unselected_vs_winner(p_text) != _is_unselected_vs_winner(k_text):
        return False
    if _has_no_ipo(p_text) != _has_no_ipo(k_text):
        return False
    if ("draft_team" in p_actions) != ("draft_team" in k_actions):
        return False
    if "fight_next" in p_actions and "fight_next" in k_actions:
        p_names = _proper_names(p_text)
        k_names = _proper_names(k_text)
        p_event_names = _proper_names((getattr(poly, "extra", {}) or {}).get("event_title", ""))
        k_event_names = _proper_names((getattr(kalshi, "extra", {}) or {}).get("event_title", ""))
        if p_event_names and k_event_names and not _names_overlap(p_event_names, k_event_names):
            return False
        if p_event_names and k_names and not _names_overlap(p_event_names, k_names) and not _names_overlap(p_names, k_names):
            return False
        if k_event_names and p_names and not _names_overlap(k_event_names, p_names) and not _names_overlap(p_names, k_names):
            return False
    if "token_launch" in p_actions and "token_launch" in k_actions:
        p_entities = _named_entities(p_text)
        k_entities = _named_entities(k_text)
        if p_entities and k_entities and p_entities.isdisjoint(k_entities):
            return False
    p_selected_names = _selected_names(p_text)
    k_selected_names = _selected_names(k_text)
    if p_selected_names and k_selected_names and not _names_overlap(p_selected_names, k_selected_names):
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

    p_bounds = _comparison_bounds(p_text)
    k_bounds = _comparison_bounds(k_text)
    if (p_bounds["lt"] & k_bounds["gt"]) or (p_bounds["gt"] & k_bounds["lt"]):
        return False

    p_sets = _set_numbers(p_text)
    k_sets = _set_numbers(k_text)
    if p_sets and k_sets and p_sets.isdisjoint(k_sets):
        return False

    p_picks = _draft_pick_numbers(p_text)
    k_picks = _draft_pick_numbers(k_text)
    if p_picks and k_picks and p_picks.isdisjoint(k_picks):
        return False

    p_stats = _stat_thresholds(p_text)
    k_stats = _stat_thresholds(k_text)
    if p_stats and k_stats and p_stats.keys().isdisjoint(k_stats.keys()):
        return False
    for stat in p_stats.keys() & k_stats.keys():
        p_vals = p_stats[stat]
        k_vals = k_stats[stat]
        if p_vals and k_vals and min(abs(pv - kv) for pv in p_vals for kv in k_vals) > 0.75:
            return False
        if _has_over_under(p_text) != _has_over_under(k_text) and p_vals != k_vals:
            return False

    return True


_ARB_ACTIONS = frozenset({
    "weather",
    "ipo",
    "bankruptcy",
    "government_stake",
    "rank",
    "revenue",
    "ai_benchmark",
    "review_score",
    "box_office",
    "launch",
    "relocate",
    "host_location",
    "labor_stats",
    "inflation",
    "monetary_policy",
    "stat_prop",
    "stat_leader",
    "award",
    "album_release",
    "song_release",
    "album_chart",
    "artist_chart",
    "song_chart",
    "fight_next",
    "token_launch",
    "draft_pick",
    "draft_team",
    "exact_score",
    "set_total",
    "handicap",
    "wedding_attendance",
    "count",
    "comparison",
    "winless",
    "engagement",
    "arrest",
    "wealth_status",
    "wage_policy",
    "group_winner",
    "starting_qb",
    "run_or_declare",
    "win_nomination",
    "nomination",
    "head_to_head",
    "win",
    "finish_position",
    "ticket",
    "occur",
    "leave_role",
    "meeting_location",
    "become_pm",
})


def _arb_signature(text: str) -> set[str]:
    actions = _contract_actions(text) & _ARB_ACTIONS
    signature = set(actions)

    for stat in _stat_thresholds(text):
        signature.add(f"stat:{stat}")
    for set_num in _set_numbers(text):
        signature.add(f"set:{set_num}")
    for pick_num in _draft_pick_numbers(text):
        signature.add(f"pick:{pick_num}")
    bounds = _comparison_bounds(text)
    for side, values in bounds.items():
        for value in values:
            signature.add(f"{side}:{value:g}")
    for rate in _rates(text):
        signature.add(f"rate:{rate}")
    for scope in _time_scopes(text):
        signature.add(f"time:{scope}")

    return signature


def is_arb_eligible(poly: "MarketSnapshot", kalshi: "MarketSnapshot") -> bool:
    """
    Return True only when a pair is specific enough to promote to arb output.

    ``is_compatible_match`` is intentionally permissive enough to produce a
    review list.  Arbitrage display must be stricter: contract type, outcome
    side, and named entity evidence need to line up so broad related markets do
    not become false trade signals.
    """
    if not is_compatible_match(poly, kalshi):
        return False

    p_text = _contract_text(poly)
    k_text = _contract_text(kalshi)
    p_sig = _arb_signature(p_text)
    k_sig = _arb_signature(k_text)
    if not p_sig or not k_sig or p_sig != k_sig:
        return False

    p_entities = _named_entities(p_text)
    k_entities = _named_entities(k_text)
    if p_entities or k_entities:
        if not p_entities or not k_entities or p_entities != k_entities:
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
    kalshi_by_id = {s.market_id: s for s in remaining_kalshi}
    kalshi_by_token: dict[str, set[str]] = {}
    for k in remaining_kalshi:
        for tok in kalshi_tok[k.market_id]:
            kalshi_by_token.setdefault(tok, set()).add(k.market_id)

    # Score all candidate pairs.
    # Close-time delta is a scoring signal only — never a hard exclusion gate.
    # Kalshi sports series carry a contractual far-out expiry (e.g. 2028 for
    # the current NBA Finals) even though the market resolves this season.
    # Hard-filtering by delta_h would silently drop all such sports pairs.
    scored: list[tuple[float, "MarketSnapshot", "MarketSnapshot"]] = []
    for p in remaining_poly:
        p_toks = poly_tok[p.market_id]
        candidate_ids: set[str] = set()
        for tok in p_toks:
            candidate_ids.update(kalshi_by_token.get(tok, ()))
        for kalshi_id in candidate_ids:
            k = kalshi_by_id[kalshi_id]
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
            if not is_compatible_match(p, k):
                continue
            if not is_close_time_compatible(p, k):
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

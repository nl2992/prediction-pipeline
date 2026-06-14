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


_ROMAN_NUMERALS = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8",
    "ix": "9", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
    "xvi": "16", "xvii": "17", "xviii": "18", "xix": "19", "xx": "20",
    "xxi": "21", "xxii": "22", "xxiii": "23", "xxiv": "24", "xxv": "25",
    "xxx": "30", "xl": "40", "xlv": "45", "lx": "60",
}


# Cross-exchange wording synonyms, applied at the token level so Jaccard
# similarity sees "$150k"=="$150,000", "BTC"=="Bitcoin", "SCOTUS"=="Supreme
# Court" etc. Each entry REPLACES the token with its expansion (1->many allowed).
_TOKEN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "btc": ("bitcoin",),
    "eth": ("ethereum",),
    "sol": ("solana",),
    "xrp": ("ripple",),
    "doge": ("dogecoin",),
    "gop": ("republican",),
    "scotus": ("supreme", "court"),
    "nomination": ("nominee",),
    "nominations": ("nominee",),
    "touch": ("hit",),
    "touches": ("hit",),
    "touched": ("hit",),
    "lunar": ("moon",),
    "crewed": ("human",),
    "named": ("announced",),
    "nyc": ("new", "york", "city"),
    "successfully": ("success",),
    "etfs": ("etf",),
    # Singularise high-frequency plurals so "rates" == "rate", "hikes" == "hike".
    "rates": ("rate",),
    "hikes": ("hike",),
    # Econ-stat synonyms so the two exchanges' wordings share tokens:
    # "CPI" == "inflation", "jobless" == "unemployment".
    "cpi": ("inflation",),
    "jobless": ("unemployment",),
    "unemployed": ("unemployment",),
    # Shipping/traffic synonyms: Kalshi's "transit calls" is Polymarket's "traffic".
    "transit": ("traffic",),
}

# Multi-word phrase canonicalisation, applied to the raw (lowercased) title
# before tokenisation. Targets monetary-policy wording, where the two exchanges
# phrase the same event very differently ("Federal Reserve raise interest rates"
# vs "Fed rate hike"). Kept narrow and direction-aware so a hike is never
# conflated with a cut. (pattern, replacement) pairs, applied in order.
_PHRASE_NORMALISERS: tuple[tuple[str, str], ...] = (
    (r"\bfederal\s+reserve\b", "fed"),
    (r"\binterest\s+(rates?)\b", r"\1"),
    (r"\b(?:raise|raises|raising|hike|hikes|hiking|increase|increases|increasing)\s+rates?\b", "rate hike"),
    (r"\brates?\s+(?:hike|increase)\b", "rate hike"),
    (r"\b(?:cut|cuts|cutting|lower|lowers|lowering|reduce|reduces|reducing|decrease|decreases|decreasing)\s+rates?\b", "rate cut"),
    (r"\brates?\s+(?:cut|reduction|decrease)\b", "rate cut"),
    # Keep one-sided comparators attached to their number: ">25bps" must stay
    # distinct from "25bps" after punctuation stripping, or sibling rate buckets
    # ("Cut by 25bps" vs "Cut by >25bps") collapse to identical token sets and
    # pooled assignment swaps them arbitrarily.
    (r">\s*(\d+)", r"gt\1"),
    # Split fused number+unit so Kalshi "25bps" aligns with Polymarket "25 bps".
    (r"(\d)(bps)\b", r"\1 \2"),
    # Kalshi often words a market as its quantitative resolution PROXY while
    # Polymarket uses the headline phrasing ("Strait of Hormuz traffic returns
    # to normal" vs "7-day moving average of transit calls ... as reported by
    # the IMF PortWatch be above 60"). Strip metric scaffolding that describes
    # HOW the quantity is measured, not WHICH event it is. NOTE: the smoothing
    # window ("7-day") is intentionally dropped with it — differing windows on
    # the same series are treated as the same market.
    (r"\b\d+[\s-]?day moving average of\b", " "),
    # Resolution-source clause: "as reported by <Source>" names the data
    # provider, not the event. Strip up to the next verb/preposition boundary.
    (r"\bas reported by\b.{0,40}?(?=\b(?:be|is|are|was|were|above|below|over|under|before|after|on|by)\b|[?,.])", " "),
)


def _tokens(title: str) -> frozenset[str]:
    title = title.lower()
    for pat, repl in _PHRASE_NORMALISERS:
        title = re.sub(pat, repl, title)
    # Preserve numbers (with decimals and thousands separators) as single tokens.
    # E.g. "$150,000" / "150k" / "5.25%" all become single tokens to avoid
    # splitting into ['150', '000'] or ['5', '25'].
    # NOTE: the replacement must re-emit a leading space — the \$?\s* prefix is
    # consumed by the match, and dropping it fused "above 7,000" -> "above7000".
    title = re.sub(r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?)", lambda m: " " + m.group(1).replace(",", ""), title)
    # Expand k-suffixed amounts so "150k" == "150,000" == "150000".
    title = re.sub(r"\b(\d+(?:\.\d+)?)k\b", lambda m: str(int(float(m.group(1)) * 1000)), title)
    # Canonicalise decimals by dropping trailing-zero fractions so "3.0%" == "3%"
    # and "5.0" == "5" tokenise identically (the off-by-rounding levels "3.5",
    # "5.25" keep their distinct fractional tokens). Without this, "above 3%" and
    # "exceed 3.0%" stay just under the Jaccard gate despite being the same level.
    def _decnorm(m: "re.Match[str]") -> str:
        whole, frac = m.group(1), m.group(2)
        frac = frac.rstrip("0")
        return whole if not frac else f"{whole}_{frac}"
    title = re.sub(r"\b(\d+)\.(\d+)%?", _decnorm, title)
    # Strip other punctuation
    title = re.sub(r"[^\w\s]", " ", title)
    # Keep single-character digits ("GTA 6", "Round 1") — they are meaningful and
    # must align with roman-numeral normalisation ("GTA VI" -> "6").
    toks = [t for t in title.split() if t not in _STOPWORDS and (len(t) > 1 or t.isdigit())]
    # Normalise roman numerals to arabic so "GTA VI" == "GTA 6", "Super Bowl LX"
    # == "Super Bowl 60". Only multi-letter numerals survive (single letters are
    # already dropped by len>1), avoiding ambiguity with initials.
    out: set[str] = set()
    for t in toks:
        t = _ROMAN_NUMERALS.get(t, t)
        out.update(_TOKEN_SYNONYMS.get(t, (t,)))
    return frozenset(out)


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
    "world", "series", "stanley", "cup", "playoff", "game", "mvp",
})

_ELECTION_TERMS = frozenset({
    # NOTE: bare "president" is intentionally absent. It marks the office, not
    # an election — "Will the President be impeached?" is not an election
    # market, and classifying it as one vetoed legitimate impeachment matches
    # whose counterpart (e.g. "Will Trump be impeached?") names no office.
    # "presidential"/"presidency" remain as genuine election signals.
    # Bare "state" is also intentionally absent: it appears in "head of state",
    # "state of the union", etc. and falsely tagged those as elections. Genuine
    # election markets carry stronger signals (election/senate/governor/nominee),
    # and "Secretary of State" is still caught via "secretary".
    "election", "presidential", "presidency", "senate", "senator",
    "governor", "governorship", "gubernatorial", "primary", "nominee",
    "nomination", "democratic", "democratics", "democrat", "republican",
    "republicans", "attorney", "general", "secretary", "mayor",
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
    "japan": ("japan", "japanese"),
    "china": ("china", "chinese"),
    "germany": ("germany", "german"),
    "canada": ("canada", "canadian"),
    "india": ("india", "indian"),
    "russia": ("russia", "russian"),
    "mexico": ("mexico", "mexican"),
    "australia": ("australia", "australian"),
    "italy": ("italy", "italian"),
    "spain": ("spain", "spanish"),
    "south korea": ("south korea", "korean"),
    "argentina": ("argentina", "argentine", "argentinian"),
    "ukraine": ("ukraine", "ukrainian"),
    "venezuela": ("venezuela", "venezuelan"),
    "iran": ("iran", "iranian"),
    "north korea": ("north korea", "north korean"),
}

# Foreign (non-US) countries. US states and "united states" are domestic and
# excluded — used to veto a foreign-named market against an unmarked one.
_FOREIGN_COUNTRIES: frozenset[str] = frozenset(_COUNTRY_ALIASES) - {"united states"}

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
    found = {office for office, pat in _OFFICE_PATTERNS if re.search(pat, low)}
    # "vice president(ial)" matches the president pattern as a substring. Treat
    # it as its own office so a VP nominee/race is not confused with a
    # presidential one (they are different contracts).
    if re.search(r"\bvice[\s-]+presiden(?:t|tial|cy)\b", low):
        found.discard("president")
        found.add("vice_president")
    return found


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
    # Check states first to avoid "indiana" matching "india"
    state_matches = set()
    for canonical, aliases in _US_STATE_ALIASES.items():
        if any(alias in low for alias in aliases):
            found.add(canonical)
            state_matches.add(canonical)
    # Check countries, but skip "india" if "indiana" was found
    for canonical, aliases in _COUNTRY_ALIASES.items():
        # Special case: skip "india" if "indiana" state was found
        if canonical == "india" and "indiana" in state_matches:
            continue
        if any(alias in low for alias in aliases):
            found.add(canonical)
    return found


def _years(text: str) -> set[str]:
    years = set(re.findall(r"\b(20\d{2})\b", text))
    for yy in re.findall(r"(?:^|[-\s])(\d{2})(?:$|[-\s])", text):
        if 20 <= int(yy) <= 49:
            years.add(f"20{yy}")
    return years


def _numeric_threshold(text: str) -> tuple[str, float, str] | None:
    """Extract a single one-sided numeric threshold: ``(direction, value, unit)``.

    Markets express the same level differently across exchanges:
      * crypto:    "hit $150k" / "reach $150,000"  vs  "above $149,999.99"
      * inflation: "reach more than 5%"            vs  "Above 5.0%"
    ``unit`` is ``"usd"`` or ``"pct"``; thresholds of different units are never
    comparable. Two-sided ranges ("between 2% and 3%") return None — they are a
    different shape and are handled by the range logic / left to title scoring.
    Returns None without a clear above/below direction.
    """
    low = _ascii_lower(text)
    if re.search(r"\bbetween\b.*\band\b", low):
        return None  # two-sided range, not a one-sided threshold
    if re.search(r"\b(below|under|less than|at most|or below)\b", low):
        direction = "down"
    elif re.search(
        r"\b(above|over|reach|reaches|hit|hits|exceed|exceeds|at least|greater than|more than|or above|"
        r"top|tops|topping|surpass|surpasses|surpassing|breach|breaches|cross|crosses)\b", low
    ):
        direction = "up"
    else:
        return None
    dollars: list[float] = []
    # [kmb] suffix must not be the start of a following word (e.g. "by").
    for m in re.finditer(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kmb])?(?![a-z])", low):
        num = float(m.group(1).replace(",", ""))
        suffix = m.group(2)
        if suffix == "k":
            num *= 1e3
        elif suffix == "m":
            num *= 1e6
        elif suffix == "b":
            num *= 1e9
        dollars.append(num)
    if dollars:
        return (direction, max(dollars), "usd")
    pcts = [float(x) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*%", low)]
    if pcts:
        return (direction, max(pcts), "pct")
    # Integer count thresholds, scoped to a count noun so bare numbers/years are
    # not mistaken for thresholds. Normalise to the integer cutoff (smallest
    # value that resolves YES) so "more than 84.5 games" and "at least 85 games"
    # are recognised as the same level (cutoff 85), while "at least 90" (90) is
    # distinct. Only "up" direction (win totals, seat counts) is handled.
    if direction == "up" and re.search(
        r"\b(games?|wins?|seats?|points?|goals?|runs?|medals?|votes?|electoral|cuts?|home runs?|strikeouts?)\b", low
    ):
        m = re.search(r"\b(at least|more than|over|greater than)\s+(\d+(?:\.\d+)?)", low)
        if m:
            n = float(m.group(2))
            if m.group(1) == "at least":
                cutoff = int(n) if n == int(n) else int(n) + 1  # >= n
            else:
                cutoff = int(n) + 1  # > n  (84.5 -> 85, 85 -> 86)
            return ("up", float(cutoff), "count")
        m2 = re.search(r"\b(\d+)\s*\+", low)  # "85+ wins"
        if m2:
            return ("up", float(m2.group(1)), "count")
    return None


def _threshold_equal(
    a: tuple[str, float, str] | None,
    b: tuple[str, float, str] | None,
) -> bool:
    """True when two thresholds match in direction, unit, and value.

    Tolerances treat "$149,999.99"=="$150,000" and "5%"=="5.0%" as the same
    level, while keeping adjacent rungs distinct ($140k≠$150k, 4.9%≠5.0%) — the
    off-by-one trap that sank the iter-7 catalog-price path.
    """
    if not a or not b or a[0] != b[0] or a[2] != b[2]:
        return False
    va, vb, unit = a[1], b[1], a[2]
    if unit == "usd":
        return abs(va - vb) / max(va, vb, 1.0) <= 0.001
    if unit == "count":
        return va == vb  # normalised integer cutoffs; exact match
    return abs(va - vb) <= 0.05  # pct: absolute, tighter than one bucket step


def _rates(text: str) -> set[str]:
    return set(re.findall(r"\b\d+(?:\.\d+)?\s*%|\b\d+\.\d+\b", text))


def _stat_thresholds(text: str) -> dict[str, set[float]]:
    low = _ascii_lower(text)
    # "at any/this/some point", "point in time", "to the point" are time/idiom
    # phrases, not a basketball points prop — strip them before stat matching so
    # "dip below $80k at any point in 2026" is not read as a points line.
    low = re.sub(r"\b(?:at\s+)?(?:any|this|some|that)\s+point\b", " ", low)
    low = re.sub(r"\bpoint\s+in\s+time\b", " ", low)
    stats = {
        "points": r"\b(points?|pts)\b",
        "rebounds": r"\b(rebounds?|rbs?)\b",
        "assists": r"\b(assists?|asts?)\b",
        "hits": r"\bhits?\b",
        # Bare "k"/"ks" is intentionally NOT an alias: it collides with the
        # thousands suffix that pervades these markets ("$80k", "$150k") and
        # with stray initials. Real strikeout-prop titles spell it out.
        "strikeouts": r"\bstrikeouts?\b",
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


def _is_ou_or_spread(text: str) -> bool:
    """True for a totals (over/under line) or point-spread market.

    Targets sports lines like "Sweden 1st Half O/U 0.5", "France O/U 1.5",
    "Bosnia and Herzegovina (-1.5)" — a different BET TYPE from a moneyline
    win market on the same team.
    """
    low = _ascii_lower(text)
    return bool(
        re.search(r"\bo/u\b|\bover\s*/\s*under\b", low)
        or re.search(r"\b(?:over|under)\b\s*\d", low)        # "over 1.5"
        or re.search(r"[a-z)]\s*\(\s*[+-]?\d+(?:\.\d+)?\s*\)", low)  # "Team (-1.5)"
    )


def _is_win_market(text: str) -> bool:
    """True for a moneyline win/winner/beat market (not a totals/spread line)."""
    low = _ascii_lower(text)
    return bool(re.search(r"\bwin(?:s|ner)?\b|\bbeat(?:s|en)?\b|\bdefeat(?:s|ed)?\b", low))


_PROP_STATS = (r"assists?|goals?|points?|hits?|saves?|rebounds?|shots?|"
               r"tackles?|goalscorer|touchdowns?|passing yards?|strikeouts?|"
               r"total bases|bases|runs|rbis?|receptions?|yards?|blocks?|"
               r"steals?|threes|three[- ]pointers?|aces|double[- ]double")


def _is_player_prop(text: str) -> bool:
    """True for a player stat-prop market, e.g. "Cody Gakpo: 2+ assists",
    "Mitch Marner: First Goalscorer", "Player: anytime goal".

    Kalshi player props use a "Name: <prop>" shape; the prop is a stat keyword
    or a "N+ stat" threshold. A bare "Cody Gakpo" market (to-win/transfer) is a
    DIFFERENT contract from "Cody Gakpo: 2+ assists" even for the same player.
    """
    low = _ascii_lower(text)
    if ":" in low and re.search(rf":\s*[^:]*\b(?:{_PROP_STATS})\b", low):
        return True
    if re.search(rf"\b\d+\+?\s*(?:{_PROP_STATS})\b", low):       # "2+ assists"
        return True
    if re.search(r"\b(?:first|anytime)\s+goalscorer\b|\bto score\b", low):
        return True
    return False


def _settlement_type(text: str) -> str | None:
    """Classify path-dependent price settlements.

    "touch": resolves YES if the level trades at any point ("hit $175k",
             "to touch $175k", "dip below $80k at any point").
    "hold":  resolves YES only if the level holds the whole period
             ("stay above $80k for all of 2026").
    None:    point-in-time settlement ("above $150k on Dec 31") or no signal.

    "Touch anytime" and "close above on a date" are DIFFERENT contracts even at
    the same strike (PAIR-015 trap); "touch below" vs "hold above" are logical
    complements of each other (an inverted pair, not a mismatch).
    """
    low = _ascii_lower(text)
    if re.search(r"\b(stay|stays|remain|remains)\s+(above|below|under|over)\b", low) or re.search(
        r"\bfor all of\b", low
    ):
        return "hold"
    if re.search(
        r"\b(hit|hits|touch|touches|touched|dip|dips|dipped|pass|passes|"
        r"surpass|surpasses|top|tops|topping|breach|breaches|cross|crosses)\b", low
    ) or re.search(r"\bat any point\b", low):
        return "touch"
    # A price reach-verb scoped to a WHOLE PERIOD ("above $X in 2026", "over $X
    # by Dec 31") resolves the moment the level is reached — a touch, not a
    # point-in-time read. The full-period framings "in 20XX" / "during 20XX" /
    # "by <deadline>" are equivalent ways to say "anytime before the close".
    # A SPECIFIC-DATE settlement ("above $X ON Dec 31") or bare "at year-end"
    # stays None (point), so the PAIR-015 trap (point vs touch) still vetoes.
    reach = re.search(r"\b(above|below|over|under|reach|reaches|exceed|exceeds)\b", low)
    if "$" in low and reach and (
        re.search(r"\bby\b", low)
        or re.search(r"\b(?:in|during|throughout)\s+20\d{2}\b", low)
    ):
        return "touch"
    return None


# Org/product disambiguation for tech markets: "Anthropic ... Claude 6" must
# never match "OpenAI ... GPT-6" however similar the rest of the wording is.
_KNOWN_ORGS = (
    "anthropic", "openai", "deepmind", "meta", "microsoft", "apple",
    "amazon", "nvidia", "tesla", "spacex", "xai", "mistral", "waymo",
)
_KNOWN_AI_PRODUCTS = ("claude", "gpt", "gemini", "llama", "grok")


def _monetary_direction(text: str) -> set[str]:
    """Direction of a rate-policy market: ``{"up"}`` (hike), ``{"down"}`` (cut),
    or empty. Used to veto a rate-cut market against a rate-hike one — opposite
    monetary actions are never the same contract. Scoped to rate/Fed wording by
    the caller so generic "raise"/"cut" verbs elsewhere don't trip it.
    """
    low = _ascii_lower(text)
    dirs: set[str] = set()
    if re.search(r"\b(hike|hikes|raise|raises|raising|increase|increases|increasing)\b", low):
        dirs.add("up")
    if re.search(r"\b(cut|cuts|cutting|lower|lowers|lowering|reduce|reduces|reducing)\b", low):
        dirs.add("down")
    return dirs


def _known_orgs(text: str) -> set[str]:
    low = _ascii_lower(text)
    return {o for o in _KNOWN_ORGS if re.search(rf"\b{o}\b", low)}


def _known_products(text: str) -> set[str]:
    low = _ascii_lower(text)
    return {p for p in _KNOWN_AI_PRODUCTS if re.search(rf"\b{p}\b", low)}


# Antonym cue groups for logical-inversion detection: one side asserts the
# "negative" state, the other the "positive" state of the SAME event. Each tuple
# is (negative_pattern, positive_pattern). Kept deliberately small and specific
# so normal (non-inverted) pairs are never flagged.
_INVERSION_ANTONYMS: tuple[tuple[str, str], ...] = (
    (r"\b(banned|ban|prohibited|outlawed|illegal|blocked|shut down|shutdown)\b",
     r"\b(legal|legally|operating|allowed|permitted|available|stay online|remain online)\b"),
    (r"\bnot\s+reach\b|\bfails?\s+to\b|\bmiss(?:es)?\b",
     r"\breach(?:es)?\b|\bhits?\b|\bachiev(?:e|es)\b"),
)


def is_inverted_pair(poly: "MarketSnapshot", kalshi: "MarketSnapshot") -> bool:
    """True when the two markets price LOGICALLY OPPOSITE outcomes of the same
    event — i.e. Polymarket-YES is economically Kalshi-NO.

    Two precise signals only (both must concern the same underlying event, which
    the caller has already established via is_compatible_match):

    1. Threshold direction flip at the SAME level with touch-vs-hold settlement:
       "dip below $80k at any point" (touch, down) vs "stay above $80k all year"
       (hold, up). One resolves YES exactly when the other resolves NO.
    2. Antonym state cue: one side says banned/illegal/blocked, the other
       legal/operating/allowed ("TikTok banned" vs "TikTok operating legally").

    Detection is text-only (never price-based): a price gap is what arb trades on,
    so inferring inversion from prices would corrupt genuine arbitrage signals.
    """
    p_text = _contract_text(poly)
    k_text = _contract_text(kalshi)

    # Signal 1: threshold direction flip + touch/hold complement.
    p_thr = _numeric_threshold(p_text)
    k_thr = _numeric_threshold(k_text)
    if p_thr and k_thr and p_thr[0] != k_thr[0] and p_thr[2] == k_thr[2]:
        if abs(p_thr[1] - k_thr[1]) / max(p_thr[1], k_thr[1], 1.0) <= 0.001:
            if {_settlement_type(p_text), _settlement_type(k_text)} == {"touch", "hold"}:
                return True

    # Signal 2: explicit antonym state cues (one negative, one positive).
    p_low = _ascii_lower(p_text)
    k_low = _ascii_lower(k_text)
    for neg, pos in _INVERSION_ANTONYMS:
        p_neg, p_pos = bool(re.search(neg, p_low)), bool(re.search(pos, p_low))
        k_neg, k_pos = bool(re.search(neg, k_low)), bool(re.search(pos, k_low))
        # Exactly one side negative and the other positive (not both-mixed).
        if (p_neg and k_pos and not p_pos and not k_neg) or (
            k_neg and p_pos and not k_pos and not p_neg
        ):
            return True

    return False


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


def _month_names(text: str) -> set[str]:
    """Extract all month names mentioned in text, regardless of context."""
    low = _ascii_lower(text)
    months = set()
    for month in re.findall(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        low,
    ):
        months.add(_MONTHS[month])
    return months


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
    # Generic contest phrases are not people. Without these, a Titlecased
    # "Presidential Election" in a Polymarket title is mistaken for a proper
    # name, which then fails to overlap the candidate name on the Kalshi side.
    "presidential", "election", "elections", "primaries",
    # Sports event descriptors. Kalshi names a finalist by city only ("Will the
    # New York win the 2026 Pro Basketball Finals?"); "New York" is stripped as
    # a jurisdiction, so without these the phantom name "Pro Basketball Finals"
    # is all that remains and never overlaps the Polymarket team name. League
    # acronyms (nba/wnba/nfl/nhl/mlb) are deliberately NOT generic so the NBA
    # vs WNBA distinction survives.
    "pro", "basketball", "baseball", "football", "soccer", "finals", "final",
    # Award/contest phrases. "Nobel Peace Prize" etc. are not people; if treated
    # as proper names they create a phantom shared name that makes unrelated
    # candidates (e.g. "Putin" vs "Dario Amodei") falsely overlap.
    "nobel", "peace", "prize", "oscar", "oscars", "emmy", "grammy", "heisman",
    "cup", "trophy", "medal",
    # League acronyms as NAME tokens only — so "NBA Finals"/"NBA Championship"
    # are not phantom proper names that block a team-name overlap. League
    # detection uses _sports_league (regex), which is unaffected, so the
    # NBA-vs-WNBA distinction is preserved.
    "nba", "wnba", "nfl", "nhl", "mlb", "mls", "ncaa",
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


# Qualifier/date words that must not count as the shared "anchor" of two names:
# "New Bond actor" and "James Bond actor" share "bond", not "new".
_NAME_QUALIFIER_TOKENS = frozenset({
    "new", "next", "the", "former", "current", "will",
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august",
    "sep", "sept", "september", "oct", "october", "nov", "november",
    "dec", "december",
})


def _name_anchor_tokens(name: str) -> set[str]:
    return {
        t for t in re.split(r"\W+", name)
        if len(t) >= 3 and t not in _NAME_QUALIFIER_TOKENS
    }


def _names_overlap(a_names: set[str], b_names: set[str]) -> bool:
    """True when two name sets plausibly refer to the same entity.

    Exchanges abbreviate differently ("Solana ETF" vs "SOL ETFs", "CPI YoY" vs
    "June CPI", "James Bond" vs "New Bond"), so whole-string containment is too
    strict. Accept a shared significant token, with a >=3-char prefix rule so
    ticker-style clips match their full word (sol/solana, etf/etfs).
    """
    for a in a_names:
        for b in b_names:
            if a == b or a in b or b in a:
                return True
            for at in _name_anchor_tokens(a):
                for bt in _name_anchor_tokens(b):
                    if at == bt or at.startswith(bt) or bt.startswith(at):
                        return True
    return False


_WIN_TRIGGERS = re.compile(
    r"\b(?:to\s+win|wins?|winning|to\s+seek|to\s+capture|to\s+claim|"
    r"be\s+the\b[^?]*\bnomin\w+|win\s+the\b)",
    re.I,
)
_LEAD_WIN = re.compile(
    r"^\s*(?:will|who|which|what|does|is|are|can|should)\s+(?:the\s+)?", re.I
)


def _winner_subject(text: str) -> set[str]:
    """Significant tokens naming the entity claimed to WIN/be nominated.

    Captures the capitalised subject between the leading question phrase and a
    win/seek/nominee trigger ("Will the Lakers win …" → {lakers}; "Newsom to
    win …" → {newsom}). Strips generic/stop/jurisdiction/office words and applies
    ticker synonyms, so a winner-subject veto fires only on genuinely different
    contestants — never on common-noun price markets (Moon, Category) that lack
    a win-trigger, and never when only one side names a contestant.
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    m = _WIN_TRIGGERS.search(ascii_text)
    if not m:
        return set()
    head = _LEAD_WIN.sub("", ascii_text[: m.start()])
    out: set[str] = set()
    for tok in re.findall(r"\b([A-Z][A-Za-z]{2,})\b", head):
        t = tok.lower()
        if t in _NAME_STOP or t in _GENERIC_NAME_TERMS or t in _STOPWORDS:
            continue
        if _jurisdictions(t) or _offices(t):
            continue
        t = _ROMAN_NUMERALS.get(t, t)
        for syn in _TOKEN_SYNONYMS.get(t, (t,)):
            if len(syn) >= 3:
                out.add(syn)
    return out


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
    # Canonicalise ticker/name synonyms so "BTC" and "Bitcoin" (etc.) are the
    # same entity across exchanges — needed for the threshold-led match path.
    _ENTITY_SYNONYMS = {
        "btc": "bitcoin", "bitcoin": "bitcoin",
        "eth": "ethereum", "ethereum": "ethereum",
        "sol": "solana", "solana": "solana",
        "xrp": "ripple", "ripple": "ripple",
        "doge": "dogecoin", "dogecoin": "dogecoin",
        "gpt": "gpt", "gpt6": "gpt",
        "scotus": "supreme court", "supreme court": "supreme court",
        "gop": "republican", "republican": "republican",
        "dem": "democratic", "democratic": "democratic",
    }
    for alias, canonical in _ENTITY_SYNONYMS.items():
        if re.search(rf"\b{alias}\b", low):
            entities.add(canonical)
    for entity in ("gold", "silver", "opec"):
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
    if re.search(r"\b(unemployment|unemployed|jobless|jobs report|payroll|nonfarm)\b", low):
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
    # "how many / number of X" is an enumeration count — but "number of goals
    # over 2.5" is a threshold on that count, not an open enumeration, so do not
    # tag it (it would be vetoed against an "over 2.5 goals" phrasing).
    if re.search(r"\bhow many\b|\bnumber of\b", low) and not re.search(
        r"\b(over|under|above|below|more than|at least|fewer than|exactly)\b\s*\d", low
    ):
        actions.add("count")
    # "more than <number>" is a numeric threshold (win totals, etc.), not a
    # head-to-head comparison — exclude it so it is not vetoed against an
    # "at least <number>" phrasing of the same level.
    if re.search(r"\bwin more\b|\bmore .* than\b", low) and not re.search(r"\bmore than\s+\d", low):
        actions.add("comparison")
    if re.search(r"\bwinless\b", low):
        actions.add("winless")
    # Reaching a round (final / semifinal / playoffs / knockout) is NOT winning
    # the whole event — "France reach the final" != "France win the World Cup".
    if (re.search(r"\breach(?:es|ed)?\b.{0,30}\b(final|finals|semi-?final|quarter-?final|playoffs?|knockout|round of \d+)\b", low)
            or re.search(r"\b(advance|advances|advanced|qualify|qualifies|qualified)\b", low)):
        actions.add("reach_round")
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
    # "deadline" is only added if there's a temporal constraint (before/by date) that's NOT
    # part of a box office or similar context. Avoid false positives on "by Dec 31" in box office
    # questions, where the deadline is just the end of the measurement period.
    if re.search(r"\bbefore\b|by\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|q[1-4]|\d{1,2})", low):
        # Skip if this is clearly a box office/movie context with a date
        if not re.search(r"\b(box office|movie|film|release|gross)\b", low):
            actions.add("deadline")
    if re.search(r"\bstarting qb\b|\bquarterback\b", low):
        actions.add("starting_qb")
    if re.search(r"\b(run|runs|running|declare|declares|first this list)\b", low):
        actions.add("run_or_declare")
    # "win the nomination" and "be the nominee" are the same contract outcome;
    # collapse to one action so Polymarket ("win the ... nomination") and Kalshi
    # ("be the ... nominee") candidate ladders are not vetoed as disjoint.
    if re.search(r"\b(nominee|nomination)\b", low):
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
    leaving = re.search(r"\b(leave|leaves|resign|resigns|depart|departs|ousted|fired|step down|steps down)\b|\bout as\b|\bout of office\b", low)
    if leaving:
        actions.add("leave_role")
    elif re.search(r"\b(head of state|be the leader of|officially lead|de facto lead|in power|hold power|remain in power)\b", low):
        # Holding/becoming the leader is the OPPOSITE of leaving; keep them as
        # disjoint actions so "X out as leader" never matches "X be head of state".
        actions.add("hold_office")
    if re.search(r"\bmeet next\b|where will .*meet|next meet", low):
        actions.add("meeting_location")
    if re.search(r"\bbecome\s+prime\s+minister|next\s+prime\s+minister", low):
        actions.add("become_pm")
    # Specific political-event predicates. These share generic scaffolding
    # ("Will <person> ___ before his term ends?") so token overlap is high and
    # generic vetoes miss them; treat each as a distinct, mutually-exclusive
    # outcome (see _POLITICAL_EVENT_ACTIONS veto in is_compatible_match).
    if re.search(r"\bimpeach(?:ed|ment|es)?\b", low):
        actions.add("impeach")
    # Removal/conviction is a STRICTLY HARDER outcome than impeachment alone
    # (House vote vs Senate conviction). "Impeached" and "impeached AND removed"
    # are different contracts — surfaced live as a phantom 41c arb signal.
    if re.search(r"\bremoved?\s+from\s+office\b|\bconvicted\s+by\s+the\s+senate\b", low):
        actions.add("removal")
    if re.search(r"\bmartial law\b", low):
        actions.add("martial_law")
    if re.search(r"\b(?:government|govt)\s+shutdown\b", low):
        actions.add("govt_shutdown")
    if re.search(r"\bnational emergency\b", low):
        actions.add("national_emergency")
    if re.search(r"\bpardon(?:ed|s)?\b", low):
        actions.add("pardon")
    if re.search(r"\bindict(?:ed|ment)?\b|\bcriminally charged\b", low):
        actions.add("indicted")
    return actions


# Distinct, mutually-exclusive political-event outcomes. If both sides name a
# specific one and they disagree, the markets are about different events even
# when subject, timing, and most tokens match.
_POLITICAL_EVENT_ACTIONS = frozenset(
    # Distinct, mutually-exclusive political outcomes. leave_role (resign/step
    # down/ousted) is included so a resignation market is never matched to an
    # impeachment/pardon/indictment one — different events that share generic
    # scaffolding ("Will <person> ___ before 2027?"). Same-action pairs
    # (resign vs resign) are unaffected; the veto fires only on DIFFERENT ones.
    {"impeach", "martial_law", "govt_shutdown", "national_emergency", "pardon",
     "indicted", "leave_role"}
)


def _sports_league(text: str) -> set[str]:
    """Detect the specific sports league a market refers to.

    Kalshi avoids trademarks ("Pro Basketball" = NBA), so map both phrasings to
    one canonical league. Crucially this separates NBA from WNBA (and the men's
    from women's / pro from college variants) so a finalist named by city alone
    ("New York") cannot match across leagues.
    """
    low = _ascii_lower(text)
    leagues: set[str] = set()
    if re.search(r"\bwnba\b|women'?s? (?:pro )?basketball", low):
        leagues.add("wnba")
    elif re.search(r"\bnba\b|\bpro basketball\b", low):
        leagues.add("nba")
    if re.search(r"\bwnfl\b", low):
        leagues.add("wnfl")
    elif re.search(r"\bnfl\b|\bpro football\b", low):
        leagues.add("nfl")
    if re.search(r"\bnhl\b|\bpro hockey\b", low):
        leagues.add("nhl")
    if re.search(r"\bmlb\b|\bpro baseball\b", low):
        leagues.add("mlb")
    return leagues


def _legislative_scope(text: str) -> str | None:
    """Distinguish a single legislative seat from chamber-wide control.

    "Will the Republican Party win the IN-01 House seat?" (one district) is a
    different contract from "Which party will win the U.S. House?" (the whole
    chamber). Without this they share enough tokens ("win", "house", party) to
    match, and a district market can even outscore the correct chamber market.
    """
    low = _ascii_lower(text)
    # A specific seat: a district code (e.g. "in-01"), or explicit seat/district.
    if re.search(r"\b[a-z]{2}-\d{1,2}\b|\bhouse seat\b|\bsenate seat\b|\bcongressional district\b", low):
        return "seat"
    # Chamber-wide control of the House or Senate.
    if re.search(r"\b(control|controls|majority|win|wins|take|takes|flip|hold)\b[^.]*\b(the\s+)?(u\.?s\.?\s+)?(house|senate)\b", low):
        return "chamber"
    return None


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
    # A market naming a FOREIGN country must not match one that names no
    # jurisdiction at all. Both exchanges are US-centric, so an unmarked market
    # ("Will there be a recession in 2026?") is the domestic/US contract — it is
    # not the same as "UK Recession in 2026?". Without this, the unmarked side
    # is greedily captured by a foreign variant, starving the correct US match.
    # US states count as domestic, so this only fires on foreign countries.
    p_foreign = bool(p_all_juris & _FOREIGN_COUNTRIES)
    k_foreign = bool(k_all_juris & _FOREIGN_COUNTRIES)
    if (p_foreign and not k_all_juris) or (k_foreign and not p_all_juris):
        return False
    p_matchup = _matchup_signature(p_text)
    k_matchup = _matchup_signature(k_text)
    if p_matchup and k_matchup and p_matchup != k_matchup:
        return False
    p_league = _sports_league(p_text)
    k_league = _sports_league(k_text)
    if p_league and k_league and p_league.isdisjoint(k_league):
        return False
    p_scope = _legislative_scope(p_text)
    k_scope = _legislative_scope(k_text)
    if {p_scope, k_scope} == {"seat", "chamber"}:
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
            # Exception: in a single race, a party IS its one candidate. Kalshi
            # may label a party row with the nominee (yes_sub_title "Jon Ossoff")
            # while Polymarket says "Will the Democrats win ...". When BOTH sides
            # assert the SAME party, the named side is that party's candidate, so
            # do not reject. (Office/jurisdiction/year vetoes still apply.)
            if not (p_parties and k_parties and p_parties == k_parties):
                return False

    p_actions = _contract_actions(p_text)
    k_actions = _contract_actions(k_text)
    if p_actions and k_actions and p_actions.isdisjoint(k_actions):
        return False
    p_pol = p_actions & _POLITICAL_EVENT_ACTIONS
    k_pol = k_actions & _POLITICAL_EVENT_ACTIONS
    if p_pol and k_pol and p_pol.isdisjoint(k_pol):
        return False
    # Rate-policy direction veto: a rate CUT market and a rate HIKE market price
    # opposite monetary actions and must never match. Scoped to pairs where both
    # sides are monetary-policy contracts so generic "raise"/"cut" verbs in other
    # domains don't trip it. (Same-direction cut/cut or hike/hike is unaffected.)
    if "monetary_policy" in p_actions and "monetary_policy" in k_actions:
        p_dir = _monetary_direction(p_text)
        k_dir = _monetary_direction(k_text)
        if p_dir and k_dir and p_dir.isdisjoint(k_dir):
            return False
    # Close times are the authoritative horizon signal. When both sides resolve
    # within ~72h, they are the same deadline expressed differently
    # ("by end of 2026" vs "before Jan 1, 2027"), so the noisy text-derived
    # horizon vetoes (deadline-action asymmetry, year tokens) must not reject
    # them. Election-cycle close dates are months/years apart, so this guard
    # never relaxes those.
    _hdelta = _close_delta_hours(getattr(poly, "close_time", None), getattr(kalshi, "close_time", None))
    _same_horizon = _hdelta is not None and _hdelta <= 72.0
    if ("rank" in p_actions) != ("rank" in k_actions):
        return False
    if ("stat_leader" in p_actions) != ("stat_leader" in k_actions):
        return False
    # "Impeached" vs "impeached AND removed from office" are different bars
    # (House vote vs Senate conviction) — one-sided removal wording vetoes.
    if ("removal" in p_actions) != ("removal" in k_actions):
        return False
    if not _same_horizon and ("deadline" in p_actions) != ("deadline" in k_actions):
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
    # Winner-subject veto: two markets each naming a DIFFERENT contestant to win
    # the same contest ("Lakers to win" vs "Celtics to win", "Biden" vs "Newsom")
    # are different contracts. Catches single-word names that _proper_names misses.
    # Fires only when BOTH sides name a winner-subject and they share no token.
    p_winner = _winner_subject(p_text)
    k_winner = _winner_subject(k_text)
    if p_winner and k_winner and not _names_overlap(p_winner, k_winner):
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
    if not _same_horizon and p_years and k_years and p_years.isdisjoint(k_years):
        # Sports seasons span two calendar years and the exchanges label them
        # differently: Polymarket by season-start ("2026 NFL MVP"), Kalshi by
        # award year (ticker KXNFLMVP-27). So an ADJACENT-year gap (exactly 1)
        # in a sports context is the same award, not a different one. Election
        # cycles differ by >= 2, so this never relaxes them.
        sports_ctx = "sports" in p_domains or "sports" in k_domains
        year_gap = min(abs(int(a) - int(b)) for a in p_years for b in k_years)
        if not (sports_ctx and year_gap == 1):
            return False

    # Settlement-shape veto for price markets (both sides quote a $ level):
    # "touch $175k at any point" and "close above $175k on Dec 31" are different
    # contracts at the same strike. Only fires when exactly ONE side is
    # path-dependent — touch-vs-hold pairs are logical complements (inverted
    # framing), handled by the threshold inversion exception below.
    p_settle = _settlement_type(p_text)
    k_settle = _settlement_type(k_text)
    if "$" in p_text and "$" in k_text:
        if (p_settle is None) != (k_settle is None):
            return False

    # Org/product veto: tech markets naming different companies or AI products
    # ("Anthropic ... Claude" vs "OpenAI ... GPT") are never the same contract.
    p_orgs = _known_orgs(p_text)
    k_orgs = _known_orgs(k_text)
    if p_orgs and k_orgs and p_orgs.isdisjoint(k_orgs):
        return False
    p_products = _known_products(p_text)
    k_products = _known_products(k_text)
    if p_products and k_products and p_products.isdisjoint(k_products):
        return False

    # One-sided numeric thresholds ($ or %) must agree. Different rungs of a
    # ladder ("above $140k" vs "above $150k"; "above 4.9%" vs "above 5.0%") are
    # different contracts. When BOTH sides parse a threshold, this tolerant
    # numeric comparison is authoritative and supersedes the cruder string-based
    # `_rates` veto below — which would otherwise reject equal-but-differently-
    # written levels ("5%" vs "5.0%").
    # EXCEPTION: opposite directions at the SAME level where one side is a
    # "touch" and the other a "hold" are complements of one another ("dip below
    # $80k at any point" vs "stay above $80k all year") — an inverted pair the
    # matcher must keep, not a strike mismatch.
    p_thr = _numeric_threshold(p_text)
    k_thr = _numeric_threshold(k_text)
    if p_thr and k_thr:
        if not _threshold_equal(p_thr, k_thr):
            inverted_complement = (
                p_thr[0] != k_thr[0]
                and p_thr[2] == k_thr[2]
                and abs(p_thr[1] - k_thr[1]) / max(p_thr[1], k_thr[1], 1.0) <= 0.001
                and {p_settle, k_settle} == {"touch", "hold"}
            )
            if not inverted_complement:
                return False
    else:
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

    # Totals/spread vs moneyline: a "Team O/U 0.5" or "Team (-1.5)" line is a
    # different contract from "Will Team win?" even on the same team/period. The
    # over/under stat-veto below only fires when both titles share a recognized
    # stat; sports line-vs-winner pairs share none, so guard them explicitly.
    # (Run 12: this was the dominant phantom-arb pattern at scale.)
    if _is_ou_or_spread(p_text) != _is_ou_or_spread(k_text):
        other = k_text if _is_ou_or_spread(p_text) else p_text
        if _is_win_market(other):
            return False

    # Player stat-prop ("Gakpo: 2+ assists", "Marner: First Goalscorer") vs a
    # plain market on the same player are different contracts. Reject when only
    # one side is a player prop and they share a proper name. (Run 15.)
    if _is_player_prop(p_text) != _is_player_prop(k_text):
        if _proper_names(p_text) & _proper_names(k_text):
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

    # Date-scope vetoes apply to DISCRETE-EVENT markets only (FOMC meetings,
    # product releases): there the month identifies WHICH event. Asset-price
    # threshold markets ("$85k by May 31" vs "ATH by Dec 31") use dates as mere
    # measurement deadlines and stay compatible as review-list candidates —
    # the threshold/settlement logic above is their real gate.
    _price_market = "$" in p_text or "$" in k_text

    # Month mismatch: if both markets mention specific months, they must be the same
    # (unless they're within the same ~72h window, which suggests the same deadline
    # expressed differently). Close-time delta is authoritative.
    p_months = _month_names(p_text)
    k_months = _month_names(k_text)
    if not _same_horizon and not _price_market and p_months and k_months and p_months.isdisjoint(k_months):
        return False

    # Deadline-scope mismatch when close times disagree: a mid-year cutoff
    # ("before June 30, 2026", closing in June) is NOT the calendar-year market
    # ("released in 2026", closing in December). Only fires when the horizons
    # actually differ — "before Dec 31, 2026" vs "in 2026" close together and
    # are the same deadline.
    if not _same_horizon and not _price_market:
        p_scopes = _time_scopes(p_text)
        k_scopes = _time_scopes(k_text)
        p_day = {s for s in p_scopes if s.startswith("day:")}
        k_day = {s for s in k_scopes if s.startswith("day:")}
        # "Year-only" side: no day/month deadline in the text, but a year is
        # named ("in 2026", "in calendar 2026", "GTA 6 released in 2026").
        p_year_only = (
            not p_day
            and not any(s.startswith("month:") for s in p_scopes)
            and bool(_years(p_text))
        )
        k_year_only = (
            not k_day
            and not any(s.startswith("month:") for s in k_scopes)
            and bool(_years(k_text))
        )
        if (p_day and k_year_only) or (k_day and p_year_only):
            return False
        if p_day and k_day and p_day.isdisjoint(k_day):
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
    inverted: bool = False            # True if Polymarket-YES is economically Kalshi-NO


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
                    inverted=is_inverted_pair(p, k),
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
    # PERFORMANCE: the candidate loop can visit tens of millions of (poly,
    # kalshi) pairs on full-catalog scans (e.g. 27k × 1.1k). Every signal used
    # below the similarity gate is a pure function of ONE market, so extract it
    # once per market here instead of per candidate pair — the threshold-led
    # check then costs dict lookups and set ops instead of a fresh regex suite
    # (_numeric_threshold, _named_entities, datetime parsing) per pair. This
    # took dashboard stage-5 matching from minutes-to-never to seconds.
    kalshi_thr = {s.market_id: _numeric_threshold(_snapshot_text(s)) for s in remaining_kalshi}
    kalshi_ents = {s.market_id: _named_entities(s.title) for s in remaining_kalshi}
    kalshi_dt = {s.market_id: _parse_dt(s.close_time) for s in remaining_kalshi}

    scored: list[tuple[float, "MarketSnapshot", "MarketSnapshot"]] = []
    for p in remaining_poly:
        p_toks = poly_tok[p.market_id]
        p_thr = _numeric_threshold(_snapshot_text(p))
        p_ents: set[str] | None = None  # lazy: only needed when p_thr exists
        p_dt = _parse_dt(p.close_time)
        candidate_ids: set[str] = set()
        for tok in p_toks:
            candidate_ids.update(kalshi_by_token.get(tok, ()))
        for kalshi_id in candidate_ids:
            k = kalshi_by_id[kalshi_id]
            k_toks = kalshi_tok[k.market_id]
            sim = _jaccard(p_toks, k_toks)
            if sim < min_title_similarity:
                # Threshold-led acceptance: asset price markets word the same
                # level differently ("$150k" vs "above $149,999.99") and carry
                # noise tokens ("at 11:59 PM ET"), so title overlap is low.
                # Accept ONLY on an EXACT dollar-threshold match, a shared named
                # entity (the asset), and a near-identical resolution time.
                # Exact-threshold gating avoids the off-by-one ladder regression
                # that sank the iter-7 catalog-price path.
                if p_thr is None:
                    continue
                k_thr = kalshi_thr[kalshi_id]
                if not (k_thr and _threshold_equal(p_thr, k_thr)):
                    continue
                k_dt = kalshi_dt[kalshi_id]
                if p_dt is None or k_dt is None:
                    continue
                if abs((p_dt - k_dt).total_seconds()) / 3600.0 > 72.0:
                    continue
                if p_ents is None:
                    p_ents = _named_entities(p.title)
                if not (p_ents & kalshi_ents[kalshi_id]):
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
            inverted=is_inverted_pair(p, k),
        ))
        matched_poly.add(p.market_id)
        matched_kalshi.add(k.market_id)

    pairs.sort(key=lambda x: (not x.via_override, -x.confidence))
    return pairs

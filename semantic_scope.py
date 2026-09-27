"""Small pure extractors for explicit contract outcome scopes.

These deliberately recognize only strong wording. They are shared by the v1
matcher and the v2 contract-spec comparator so mirrored vetoes stay aligned.
"""

from __future__ import annotations

import re


_RELEGATION_RE = re.compile(r"\b(?:relegat(?:e|ed|es|ing|ion)|drop(?:ped)?\s+down)\b", re.I)
_CHAMPION_RE = re.compile(
    r"\b(?:champion|champions|league\s+winner|championship\s+winner)\b", re.I
)
_CHAMPIONS_LEAGUE_RE = re.compile(r"\bchampions\s+league\b", re.I)


def league_outcome_scope(text: str) -> str | None:
    """Return an explicit league outcome class (relegation or champion)."""
    if _RELEGATION_RE.search(text):
        return "relegation"
    if _CHAMPION_RE.search(text) and not _CHAMPIONS_LEAGUE_RE.search(text):
        return "champion"
    return None


_COALITION_FORMATION_RE = re.compile(
    r"\b(?:which|what)\s+(?:political\s+)?coalition\b"
    r"|\bcoalition\s+(?:will\s+)?form(?:s|ed|ing)?\b"
    r"|\bform\s+(?:a|the)\s+coalition\b"
    r"|\bcoalition\s+composition\b",
    re.I,
)
_PARTY_IN_GOVERNMENT_RE = re.compile(
    r"\b(?:be|become|join|enter|entered)\b.{0,35}\b"
    r"(?:a\s+)?(?:part\s+of|member\s+of|in)\s+(?:the\s+)?(?:next\s+)?government\b"
    r"|\b(?:a\s+)?(?:part\s+of|member\s+of)\s+(?:the\s+)?(?:next\s+)?government\b",
    re.I,
)


def government_outcome_scope(text: str) -> str | None:
    """Return coalition-composition or party-membership scope, when explicit."""
    if _COALITION_FORMATION_RE.search(text):
        return "coalition_composition"
    if _PARTY_IN_GOVERNMENT_RE.search(text):
        return "party_membership"
    return None


_CLUB_ALIASES = {
    "inter milan": "inter_milan",
    "internazionale": "inter_milan",
    "inter": "inter_milan",
    "ac milan": "ac_milan",
    "a.c. milan": "ac_milan",
    "milan": "ac_milan",
}
_KNOWN_CLUBS = tuple(sorted(_CLUB_ALIASES, key=len, reverse=True))


def club_team_scope(text: str) -> frozenset[str]:
    """Return explicit known club identities, resolving common aliases."""
    low = text.lower()
    found: set[str] = set()
    remaining = low
    for spelling in _KNOWN_CLUBS:
        pattern = rf"\b{re.escape(spelling)}\b"
        if re.search(pattern, remaining):
            found.add(_CLUB_ALIASES[spelling])
            remaining = re.sub(pattern, " ", remaining, count=1)
    return frozenset(found)


_US_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO"
    "|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
# A two-letter USPS state code followed by a district number ("LA-04",
# "CA-12") names a US House seat. Restricted to real state abbreviations
# (rather than any two letters) so it doesn't fire on unrelated dashed codes
# ("AI-2", ticker fragments, etc.).
_DISTRICT_CODE_RE = rf"\b(?:{_US_STATE_CODES})-\d{{1,2}}\b"

_ELECTION_OFFICES = {
    "governor": re.compile(r"\bgovernor(?:ship)?\b|\bgubernatorial\b", re.I),
    "senate": re.compile(r"\bsenate\b|\bsenator\b", re.I),
    # "house" alone names a US House race, but "House of Representatives" is
    # also the formal name of many foreign/state legislative CHAMBERS (Berlin's
    # Abgeordnetenhaus, Bosnia's, Australia's) that indirectly elect a mayor or
    # prime minister — a bare institutional mention, not that race's own
    # office. Excluding the "of representatives" phrasing avoids reading a
    # "Governing Mayor of Berlin ... by the Berlin House of Representatives"
    # market as BOTH a mayoral and a US-House-style combo race.
    "house": re.compile(
        rf"\bhouse\b(?!\s+of\s+representatives)|\bcongressional\s+district\b|{_DISTRICT_CODE_RE}",
        re.I,
    ),
    "president": re.compile(r"\bpresident(?:ial)?\b", re.I),
    "mayor": re.compile(r"\bmayor(?:al)?\b", re.I),
}


def election_office_bundle(text: str) -> frozenset[str]:
    """Return explicit offices named by an election market."""
    return frozenset(
        office for office, pattern in _ELECTION_OFFICES.items()
        if pattern.search(text)
    )


def election_office_conflict(a: str, b: str) -> bool:
    """True when both texts identify exactly one elected office, and they
    differ (e.g. a US House district race vs a presidential nominee market).
    A market naming more than one office (a combo market) is left to the
    existing single-race-vs-combo check; unidentified offices never veto."""
    oa, ob = election_office_bundle(a), election_office_bundle(b)
    return len(oa) == 1 and len(ob) == 1 and oa != ob


_PARTY_LIST_ALIASES = {
    "rzp-zehut": "rzp_zehut",
    "rzp zehut": "rzp_zehut",
    "zehut": "zehut",
}
_KNOWN_PARTY_LISTS = tuple(sorted(_PARTY_LIST_ALIASES, key=len, reverse=True))


def party_list_scope(text: str) -> frozenset[str]:
    """Return explicit known party/list identities, preserving alliances."""
    low = text.lower()
    found: set[str] = set()
    remaining = low
    for spelling in _KNOWN_PARTY_LISTS:
        pattern = rf"\b{re.escape(spelling)}\b"
        if re.search(pattern, remaining):
            found.add(_PARTY_LIST_ALIASES[spelling])
            remaining = re.sub(pattern, " ", remaining, count=1)
    return frozenset(found)


_F1_CONTEXT_RE = re.compile(r"\bf1\b|\bformula\s+1\b|\bformula\s+one\b", re.I)
_F1_RETIRE_RE = re.compile(r"\bretire(?:s|d|ment)?\b", re.I)
_F1_NEXT_TEAM_RE = re.compile(r"\bnext\s+(?:f1\s+)?team\b", re.I)


def f1_career_scope(text: str) -> str | None:
    """Return explicit F1 career predicate: retirement or next-team."""
    if not _F1_CONTEXT_RE.search(text):
        return None
    retirement = bool(_F1_RETIRE_RE.search(text))
    next_team = bool(_F1_NEXT_TEAM_RE.search(text))
    if retirement == next_team:
        return None
    return "retirement" if retirement else "next_team"


_HOLE_IN_ONE_RE = re.compile(r"\bholes?[-\s]in[-\s]one\b", re.I)
_WINNER_RE = re.compile(
    r"\b(?:winner|champion|championship winner)\b|\bwin(?:s|ning)?\b", re.I,
)
_PARTICIPATION_RE = re.compile(
    r"\b(?:compete|competes|competing|play|plays|playing|participate|participates|participating)\s+"
    r"in\s+(?:the\s+)?[A-Z0-9][^?]{0,80}\b(?:cup|championship|tournament|open|classic|masters)\b",
    re.I,
)
# Election-participation phrasing: a party/candidate CONTESTING, RUNNING IN,
# or FIELDING a candidate for an election is a participation predicate, not a
# winner predicate — even though the sentence may separately mention
# "election". Verb-form patterns only: "contests the election" is
# participation, but "contested election"/"contested convention" (adjective)
# and "Eurovision Song Contest"/"hot dog eating contest" (bare noun) must NOT
# match, so the pattern requires "contest(s)" immediately followed by an
# article ("the"/"a"/"an"), never by "-ed" or a noun.
_ELECTION_PARTICIPATION_RE = re.compile(
    r"\bcontests?\s+(?:the|a|an)\b.{0,40}\belection\b"
    r"|\brun(?:s|ning)?\s+in\s+(?:the|a|an)\b.{0,40}\belection\b"
    r"|\bfield(?:s|ed|ing)?\s+a\b.{0,40}\bcandidate\b"
    r"|\bappear(?:s|ed|ing)?\s+on\s+the\s+ballot\b",
    re.I,
)


def competition_result_scope(text: str) -> str | None:
    """Return narrow tournament predicate classes when exactly one is explicit."""
    hole = bool(_HOLE_IN_ONE_RE.search(text))
    winner = bool(_WINNER_RE.search(text))
    participant = bool(_PARTICIPATION_RE.search(text)) or bool(
        _ELECTION_PARTICIPATION_RE.search(text)
    )
    if sum(bool(x) for x in (hole, winner, participant)) != 1:
        return None
    if hole:
        return "hole_in_one"
    if participant:
        return "participant"
    return "winner"


# A party/company framed as "founded by <person>" or "<person>'s party" is
# that ORGANIZATION, not the person themselves — "a party founded by Elon
# Musk" contesting an election is not "Elon Musk" winning it. Conservative:
# only the explicit founded-by/possessive-organization phrasing counts. The
# name capture stops at the next sentence/stop word so it doesn't swallow the
# rest of the question (e.g. "founded by Elon Musk contest the ..." must
# capture just "Elon Musk", not "Elon Musk contest the").
_NAME_STOPWORDS = (
    r"will|would|shall|is|are|was|were|win|wins|winning|won|contest|contests"
    r"|contested|run|runs|running|field|fields|fielding|the|a|an|and|or|to"
    r"|on|for|of|in|by"
)
_FOUNDED_BY_RE = re.compile(
    rf"\b(?:party|company|organization|organisation)\s+founded\s+by\s+"
    rf"((?:(?!\b(?:{_NAME_STOPWORDS})\b)[a-z][\w.-]*\s*){{1,3}})"
    rf"|\b((?:(?!\b(?:{_NAME_STOPWORDS})\b)[a-z][\w.-]*\s*){{1,3}})'s\s+"
    rf"(?:party|company|organization|organisation)\b",
    re.I,
)


def founded_by_scope(text: str) -> bool:
    """True when text frames a party/company/org as founded by, or
    possessively belonging to, a named person (rather than being that
    person)."""
    return bool(_FOUNDED_BY_RE.search(text))


def founded_by_subject(text: str) -> str | None:
    """Return the lowercase person name behind a founded-by/possessive-
    organization framing, or None when the pattern doesn't match."""
    m = _FOUNDED_BY_RE.search(text)
    if not m:
        return None
    name = m.group(1) or m.group(2)
    return name.strip().lower() if name else None


_JUDICIAL_CONTEXT_RE = re.compile(r"\b(?:scotus|supreme\s+court)\b", re.I)
_JUDICIAL_NOMINATION_RE = re.compile(
    r"\b(?:nominate|nominates|nominated|nominating|nominee|nomination)\b", re.I,
)
_JUDICIAL_SEATED_RE = re.compile(
    r"\b(?:become|becomes|became|becoming)\b.{0,40}\b"
    r"(?:(?:supreme\s+court\s+)?justice|supreme\s+court)\b"
    r"|\b(?:be|is|are|was|were)\b.{0,25}\b(?:the\s+)?next\s+"
    r"(?:supreme\s+court\s+justice|justice\s+on\s+the\s+supreme\s+court)\b",
    re.I,
)


def judicial_selection_stage(text: str) -> str | None:
    """Return explicit Supreme Court selection stage: nomination or seated."""
    if not _JUDICIAL_CONTEXT_RE.search(text):
        return None
    nomination = bool(_JUDICIAL_NOMINATION_RE.search(text))
    seated = bool(_JUDICIAL_SEATED_RE.search(text))
    if nomination == seated:
        return None
    return "nomination" if nomination else "seated"


# Distinct named competitions/events that can share a school or place name
# (e.g. Notre Dame fields a football team and a steel-bridge team). Each
# pattern is conservative: matched only on phrasing unlikely to appear
# inside an unrelated sentence. Order matters only in that more specific
# patterns (e.g. explicit women's basketball) are checked before generic
# ones so a generic phrase doesn't shadow a specific one.
_COMPETITION_IDENTITY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsteel\s+bridge\b", re.I), "steel_bridge"),
    (re.compile(r"\bconcrete\s+canoe\b", re.I), "concrete_canoe"),
    (re.compile(r"\bsolar\s+decathlon\b", re.I), "solar_decathlon"),
    (re.compile(r"\bmoot\s+court\b", re.I), "moot_court"),
    (re.compile(r"\bquiz\s+bowl\b", re.I), "quiz_bowl"),
    (re.compile(r"\b(?:robotics\s+competition|robotics\s+championship)\b", re.I), "robotics"),
    (re.compile(r"\b(?:the\s+boat\s+race|boat\s+race\b.{0,20}\browing)\b", re.I), "rowing"),
    (
        re.compile(r"\b(?:ncaa\s+football|college\s+football\s+playoff|cfp|college\s+football)\b", re.I),
        "ncaa_football",
    ),
    (
        re.compile(r"\b(?:frozen\s+four|ncaa\s+(?:men'?s\s+|women'?s\s+)?hockey)\b", re.I),
        "ncaa_hockey",
    ),
    (
        re.compile(r"\b(?:college\s+world\s+series|ncaa\s+baseball)\b", re.I),
        "ncaa_baseball",
    ),
    (
        re.compile(r"\b(?:women'?s\s+march\s+madness|ncaa\s+women'?s\s+basketball)\b", re.I),
        "ncaa_basketball_women",
    ),
    (
        re.compile(r"\b(?:march\s+madness|ncaa\s+basketball|ncaa\s+tournament)\b", re.I),
        "ncaa_basketball",
    ),
)


def competition_identity(text: str) -> str | None:
    """Return a canonical key for an explicitly named competition/event.

    Conservative and small: only patterns unlikely to occur inside an
    unrelated phrase are matched. Returns None when no identifiable
    competition is named, which keeps this guard silent (no veto) rather
    than risk suppressing a true match.
    """
    for pattern, key in _COMPETITION_IDENTITY_PATTERNS:
        if pattern.search(text):
            return key
    return None


# A generic NCAA basketball reference doesn't imply men's, so it stays
# compatible with an explicit women's-basketball reference.
_COMPATIBLE_COMPETITIONS = {frozenset({"ncaa_basketball", "ncaa_basketball_women"})}


def competition_identity_conflict(a: str, b: str) -> bool:
    """True when both texts name identifiable, different competitions."""
    ca, cb = competition_identity(a), competition_identity(b)
    return bool(ca and cb and ca != cb
                and frozenset({ca, cb}) not in _COMPATIBLE_COMPETITIONS)



# An exact-value bucket ("be 0.2%", "exactly 0.3%", "between 0.2% and 0.3%")
# and an open-ended threshold ("above/more than/at least X%", "or above",
# a trailing "+", "≥"/"≤"/"↑"/"↓") are disjoint contract shapes even when
# they share a strike/value: a CPI ladder's bare "0.2%" bucket resolves YES
# only for that exact reading, while "Above 0.2%" resolves YES for any
# reading above it. Conservative: fires only when exactly one side carries
# each signal (a "0.6%+"/"≤0.0%" ladder-edge rung is itself a threshold, not
# an exact bucket, so it correctly stays out of `_EXACT_BUCKET_RE`).
_EXACT_BUCKET_RE = re.compile(
    r"\bbe\s+(?:exactly\s+)?-?\d+(?:\.\d+)?\s*%"
    r"|\bexactly\s+-?\d+(?:\.\d+)?\s*%"
    r"|\bbetween\s+-?\d+(?:\.\d+)?\s*%?\s+and\s+-?\d+(?:\.\d+)?\s*%\b",
    re.I,
)
_THRESHOLD_CUE_RE = re.compile(
    r"\b(?:above|more than|over|at least|greater than|or above"
    r"|below|under|at most|less than|or below)\b"
    r"|[≥≤↑↓]"  # ≥ ≤ ↑ ↓
    r"|\d+(?:\.\d+)?\s*%\s*\+",
    re.I,
)


def bucket_threshold_conflict(a: str, b: str) -> bool:
    """True when one side is an exact-value bucket and the other an
    open-ended threshold, with neither text carrying both signals."""
    a_bucket, a_thresh = bool(_EXACT_BUCKET_RE.search(a)), bool(_THRESHOLD_CUE_RE.search(a))
    b_bucket, b_thresh = bool(_EXACT_BUCKET_RE.search(b)), bool(_THRESHOLD_CUE_RE.search(b))
    if a_bucket and not a_thresh and b_thresh and not b_bucket:
        return True
    if b_bucket and not b_thresh and a_thresh and not a_bucket:
        return True
    return False


def founded_by_conflict(a: str, b: str) -> bool:
    """True when one side frames an organization founded by (or belonging
    to) a person and the other side names that person bare."""
    fa, fb = founded_by_scope(a), founded_by_scope(b)
    if fa == fb:
        return False
    org_text, person_text = (a, b) if fa else (b, a)
    person = founded_by_subject(org_text)
    if not person:
        return False
    surname = person.split()[-1]
    return bool(re.search(rf"\b{re.escape(surname)}\b", person_text, re.I))

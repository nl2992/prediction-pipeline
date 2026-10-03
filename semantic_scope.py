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



# ---------------------------------------------------------------------------
# Oct audit families: speaker/venue, central-bank granularity, tournament
# round, qualify-vs-relegated, fantasy category, team-season predicate.
# Every guard fires only when BOTH sides carry an identifiable, differing
# signal; an unidentified side never vetoes.
# ---------------------------------------------------------------------------

_SPEAKER_RE = re.compile(
    r"\b(?:what\s+)?will\s+(?!(?:the\s+)?(?:market|price|yes|no)\b)([^?:/|]{1,60}?)\s+(?:say|mention)\b",
    re.I,
)
_SPEAKER_SUFFIX_RE = re.compile(
    r"\b(?:incorporated|inc|corporation|corp|company|co|limited|ltd|plc|llc|holdings?|group)\b",
    re.I,
)
_MENTION_CONTEXT_RE = re.compile(r"\b(?:say|says|mention|mentions)\b", re.I)
_SPEECH_VENUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bearnings\s+(?:call|conference)", re.I), "earnings_call"),
    (re.compile(r"\byoutube\b", re.I), "youtube"),
    (re.compile(r"\bpodcast\b", re.I), "podcast"),
    (re.compile(r"\bpress\s+(?:conference|briefing)\b", re.I), "press_conference"),
    (re.compile(r"\b(?:speech|address|rally|debate)\b", re.I), "speech"),
)


def _speaker_tokens(text: str) -> frozenset[str] | None:
    m = _SPEAKER_RE.search(text)
    if not m:
        return None
    span = _SPEAKER_SUFFIX_RE.sub(" ", m.group(1).lower().replace("&", " "))
    toks = [t for t in re.findall(r"[a-z0-9]+", span) if t not in {"the", "a", "an"}]
    return frozenset(toks) or None


def _speech_venues(text: str) -> frozenset[str]:
    return frozenset(key for pat, key in _SPEECH_VENUES if pat.search(text))


def speaker_conflict(a: str, b: str) -> bool:
    """True when two "what will <SPEAKER> say/mention ..." markets name
    different speakers, or different venues (earnings call vs YouTube video,
    speech, podcast, press conference). Token-subset speakers ("Trump" vs
    "Donald Trump") are treated as the same."""
    sa, sb = _speaker_tokens(a), _speaker_tokens(b)
    if sa and sb and not (sa <= sb or sb <= sa):
        return True
    if _MENTION_CONTEXT_RE.search(a) and _MENTION_CONTEXT_RE.search(b):
        va, vb = _speech_venues(a), _speech_venues(b)
        if va and vb and va.isdisjoint(vb):
            return True
    return False


_CENTRAL_BANK_RE = re.compile(
    r"\b(?:fed|federal reserve|fomc|ecb|boc|boe|boj|rba|snb|riksbank|"
    r"bank of (?:canada|england|japan|mexico|korea)|reserve bank|central bank)\b",
    re.I,
)
_RATE_MOVE_RE = re.compile(r"\b(?:rates?|hikes?|cuts?|raise[sd]?|lower(?:s|ed)?)\b", re.I)
_MONTH_RE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
             r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_SPECIFIC_MEETING_RE = re.compile(
    rf"\b{_MONTH_RE}\b[^?.]{{0,25}}\b(?:meeting|decision)\b"
    rf"|\b(?:meeting|decision)\b[^?.]{{0,20}}\b(?:in|of)\s+{_MONTH_RE}\b"
    r"|\b\d+(?:\.\d+)?\s*(?:bps|basis points?)\b"
    r"|[<>≥≤]\s*\d+\s*(?:bps|bp)\b",
    re.I,
)
_PERIOD_WIDE_RE = re.compile(
    r"\b(?:in|during|throughout)\s+20\d{2}\b|\bby\s+(?:the\s+)?end\s+of\s+(?:the\s+year|20\d{2})\b"
    r"|\bthis\s+year\b",
    re.I,
)


def central_bank_granularity_conflict(a: str, b: str) -> bool:
    """True when one central-bank rate market is tied to a specific meeting
    or bps size and the other asks about any time in a year/period."""
    if not (_CENTRAL_BANK_RE.search(a) and _CENTRAL_BANK_RE.search(b)
            and _RATE_MOVE_RE.search(a) and _RATE_MOVE_RE.search(b)):
        return False
    spec_a, spec_b = bool(_SPECIFIC_MEETING_RE.search(a)), bool(_SPECIFIC_MEETING_RE.search(b))
    wide_a = bool(_PERIOD_WIDE_RE.search(a)) and not spec_a
    wide_b = bool(_PERIOD_WIDE_RE.search(b)) and not spec_b
    return (spec_a and wide_b) or (spec_b and wide_a)


_ORDINAL_ROUNDS = {"1st": "1", "first": "1", "2nd": "2", "second": "2",
                   "3rd": "3", "third": "3", "4th": "4", "fourth": "4"}
_ROUND_RE = re.compile(
    r"\bround\s+([1-4])\b(?!\s*(?:of|-))"
    r"|\b(1st|2nd|3rd|4th|first|second|third|fourth)[\s-]+round\b"
    r"|\b(final)\s+round\b",
    re.I,
)


def _tournament_rounds(text: str) -> frozenset[str]:
    out = set()
    for m in _ROUND_RE.finditer(text):
        if m.group(1):
            out.add(m.group(1))
        elif m.group(2):
            out.add(_ORDINAL_ROUNDS[m.group(2).lower()])
        else:
            out.add("final")
    return frozenset(out)


def tournament_round_conflict(a: str, b: str) -> bool:
    """True when both texts name tournament rounds and none overlap.
    "final round" is compatible with round 4 (golf's last round)."""
    ra, rb = _tournament_rounds(a), _tournament_rounds(b)
    if not ra or not rb:
        return False
    if "final" in ra:
        ra = ra | {"4"}
    if "final" in rb:
        rb = rb | {"4"}
    return ra.isdisjoint(rb)


_ADVANCE_RE = re.compile(
    r"\b(?:qualif(?:y|ies|ied|ication)|advance[sd]?|promoted|promotion|reach(?:es)?\s+the\s+final)\b",
    re.I,
)
_ELIMINATE_RE = re.compile(r"\b(?:relegat\w*|eliminat\w*)\b", re.I)


def advancement_vs_relegation_conflict(a: str, b: str) -> bool:
    """True when one side is a qualify/advance/promoted contract and the
    other a relegated/eliminated one (neither carrying both signals)."""
    a_up, a_down = bool(_ADVANCE_RE.search(a)), bool(_ELIMINATE_RE.search(a))
    b_up, b_down = bool(_ADVANCE_RE.search(b)), bool(_ELIMINATE_RE.search(b))
    return (a_up and not a_down and b_down and not b_up) or (b_up and not b_down and a_down and not a_up)


_FANTASY_CATEGORIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brookies?\b", re.I), "rookie"),
    (re.compile(r"\b(?:rbs?|running\s*backs?)\b", re.I), "rb"),
    (re.compile(r"\b(?:wrs?|wide\s*receivers?)\b", re.I), "wr"),
    (re.compile(r"\b(?:tes?|tight\s*ends?)\b", re.I), "te"),
    (re.compile(r"\b(?:qbs?|quarterbacks?)\b", re.I), "qb"),
    (re.compile(r"\bsuper\s*flex\b", re.I), "superflex"),
    (re.compile(r"\bflex\b", re.I), "flex"),
)


def fantasy_category_conflict(a: str, b: str) -> bool:
    """True when two fantasy-leaderboard markets name disjoint categories
    (rookie vs RB/WR/TE/QB/FLEX)."""
    if not (re.search(r"\bfantasy\b", a, re.I) and re.search(r"\bfantasy\b", b, re.I)):
        return False
    ca = {k for p, k in _FANTASY_CATEGORIES if p.search(a)}
    cb = {k for p, k in _FANTASY_CATEGORIES if p.search(b)}
    return bool(ca and cb and ca.isdisjoint(cb))


_TEAM_SEASON_PREDICATES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\blast\s+team\s+to\s+win\b", re.I), "last_to_win"),
    (re.compile(r"\bfirst\s+team\s+to\s+(?:lose|win)\b", re.I), "first_team_event"),
    (re.compile(r"\bwinless\b", re.I), "winless"),
    (re.compile(r"\b(?:undefeated|unbeaten|perfect\s+season)\b", re.I), "undefeated"),
    (re.compile(r"\bwin\s+totals?\b|\bwins?\s+(?:over|under)\b|\b(?:over|under)\s*/\s*(?:under|over)\b"
                r"|\b(?:over|under)\s+\d+(?:\.\d+)?\s+wins\b", re.I), "win_total"),
)


def team_season_predicate_conflict(a: str, b: str) -> bool:
    """True when two team-season markets use distinct predicates (last team
    to win a game / winless / undefeated / win totals)."""
    pa = {k for p, k in _TEAM_SEASON_PREDICATES if p.search(a)}
    pb = {k for p, k in _TEAM_SEASON_PREDICATES if p.search(b)}
    return bool(pa and pb and pa.isdisjoint(pb))


# ---------------------------------------------------------------------------
# Alert-history families: match period, price race, award category phrases,
# vote share vs placement.
# ---------------------------------------------------------------------------

_PERIOD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bhalf\s*-?\s*time\b|\b(?:1st|first)\s+half\b|\b1h\b", re.I), "h1"),
    (re.compile(r"\b(?:2nd|second)\s+half\b|\b2h\b", re.I), "h2"),
    (re.compile(r"\b(?:1st|first)\s+(?:quarter|period)\b", re.I), "p1"),
    (re.compile(r"\b(?:2nd|second)\s+(?:quarter|period)\b", re.I), "p2"),
    (re.compile(r"\b(?:3rd|third)\s+(?:quarter|period)\b", re.I), "p3"),
    (re.compile(r"\b(?:4th|fourth)\s+quarter\b", re.I), "p4"),
)
# "second half of 2026", "first quarter earnings" are calendar/fiscal periods.
_CALENDAR_PERIOD_RE = re.compile(
    r"\b(?:half|quarter)\b\s+(?:of\s+)?(?:the\s+)?(?:year|20\d\d|fiscal)\b"
    r"|\b(?:earnings|gdp|revenue|eps|inflation|cpi|jobs|growth|sales|profit|fiscal)\b",
    re.I,
)


def match_periods(text: str) -> frozenset[str]:
    """Sub-match periods named in a sports market (halftime/1st half/2nd half/
    quarter/period). Empty means full time. Calendar/fiscal periods
    ("second half of 2026", "Q1 earnings") are ignored."""
    if _CALENDAR_PERIOD_RE.search(text):
        return frozenset()
    return frozenset(key for pat, key in _PERIOD_PATTERNS if pat.search(text))


def match_period_conflict(a: str, b: str) -> bool:
    """True when one market settles on a sub-match period (halftime, 1st half,
    2nd half, quarter, period) and the other on a different period or on the
    full match (unqualified = full time)."""
    pa, pb = match_periods(a), match_periods(b)
    if not pa and not pb:
        return False
    return pa != pb and (not pa or not pb or pa.isdisjoint(pb))


_RACE_RE = re.compile(
    r"\bbefore\s+\$\s?\d"
    r"|\$?\d[\d,.]*\s*[kmb]?\s+first\b"
    r"|\bwhich\s+(?:level|price|target)?\s*(?:will\s+)?(?:be\s+)?(?:hit|reached|touched)?\s*first\b"
    r"|\bfirst\s+to\s+(?:hit|reach|touch)\s+\$",
    re.I,
)
_PRICE_TARGET_RE = re.compile(r"\$\s?\d|\b\d[\d,.]*\s*k\b", re.I)
_HIT_VERB_RE = re.compile(r"\b(?:hit|hits|reach|reaches|touch|touches|above|exceed)\b|\bwhen\s+will\b", re.I)


def price_race_conflict(a: str, b: str) -> bool:
    """True when one side is a "X before Y" / "which first" race between price
    levels and the other a single-threshold hit/reach market."""
    ra, rb = bool(_RACE_RE.search(a)), bool(_RACE_RE.search(b))
    if ra == rb:
        return False
    plain = b if ra else a
    return bool(_PRICE_TARGET_RE.search(plain) and _HIT_VERB_RE.search(plain))


_VOTE_SHARE_RE = re.compile(r"\d[\d.]*\s*%")
_VOTE_WORD_RE = re.compile(r"\b(?:popular\s+vote|votes?|vote\s+share|ballots?)\b", re.I)
_PLACEMENT_RE = re.compile(
    r"\b(?:\d+(?:st|nd|rd|th)|first|second|third)\s+place\b|\bfinish(?:es)?\s+(?:in\s+)?"
    r"(?:first|second|third|top|\d+(?:st|nd|rd|th))\b|\bplace\s+finish\b",
    re.I,
)


def _is_vote_share(text: str) -> bool:
    return bool(_VOTE_SHARE_RE.search(text) and _VOTE_WORD_RE.search(text))


def vote_share_vs_placement_conflict(a: str, b: str) -> bool:
    """True when one side is a vote-share percentage threshold and the other a
    finishing-position (1st place / finish first) market."""
    sa, sb = _is_vote_share(a), _is_vote_share(b)
    return (sa and not sb and bool(_PLACEMENT_RE.search(b))) or (
        sb and not sa and bool(_PLACEMENT_RE.search(a))
    )


_AWARD_CONTEXT_RE = re.compile(
    r"\b(?:awards?|oscars?|emmys?|grammys?|golden\s+globes?|tonys?|bafta|goty|nominee|nominees|nominated)\b"
    r"|\bof\s+the\s+year\b",
    re.I,
)
_BEST_PHRASE_RE = re.compile(r"\bbest\s+((?:[a-z&'-]+\s+){0,3}[a-z&'-]+)", re.I)
_OF_YEAR_PHRASE_RE = re.compile(r"\b((?:[a-z&'-]+\s+){0,2}[a-z&'-]+)\s+of\s+the\s+year\b", re.I)
_PHRASE_STOP = {"of", "in", "for", "at", "by", "to", "on", "with", "from", "is", "be", "will", "win",
                "wins", "the", "a", "an", "winner", "award", "awards"}


def award_category_phrases(text: str) -> frozenset[frozenset[str]]:
    """Token sets of generic award categories: "Best <X>" and "<X> of the Year"."""
    if not _AWARD_CONTEXT_RE.search(text):
        return frozenset()
    out: set[frozenset[str]] = set()
    for m in _BEST_PHRASE_RE.finditer(text):
        toks: list[str] = []
        for t in re.findall(r"[a-z0-9]+", m.group(1).lower()):
            if t in _PHRASE_STOP or t.isdigit():
                break
            toks.append(t)
        if toks:
            out.add(frozenset(toks))
    for m in _OF_YEAR_PHRASE_RE.finditer(text):
        toks = [t for t in re.findall(r"[a-z0-9]+", m.group(1).lower())
                if t not in _PHRASE_STOP and not t.isdigit()]
        if toks:
            out.add(frozenset(toks + ["oty"]))
    return frozenset(out)


def award_phrase_conflict(a: str, b: str) -> bool:
    """True when both sides name award categories ("Best Audio Design", "Game
    of the Year") and no category on one side contains/equals one on the other.
    Subset phrases ("best record" vs "best regular season record") agree."""
    pa, pb = award_category_phrases(a), award_category_phrases(b)
    if not pa or not pb:
        return False
    return not any(x <= y or y <= x for x in pa for y in pb)


_REVIEW_SOURCE = (r"(?:rotten\s+tomatoes|metacritic|imdb|cinemascore|letterboxd|"
                  r"opening\s+weekend|box\s+office)")
_REVIEW_QUOTED_RE = re.compile(rf"[\"“]([^\"”]{{1,60}})[\"”]\s+{_REVIEW_SOURCE}\b", re.I)
_REVIEW_SOURCE_RE = re.compile(rf"\b{_REVIEW_SOURCE}\b", re.I)
_REVIEW_LEAD_STOP = {"will", "the", "a", "an", "does", "did", "what", "is", "be", "score", "of", "for"}


def review_work_title(text: str) -> str | None:
    """Work title of a review-score / box-office market ("Clayface Rotten
    Tomatoes score?", '"Digger" Rotten Tomatoes Score?'), normalised for
    comparison; None when no title can be identified."""
    m = _REVIEW_QUOTED_RE.search(text)
    if m:
        raw = m.group(1)
    else:
        src = _REVIEW_SOURCE_RE.search(text)
        if not src:
            return None
        raw = re.split(r"[?.!/\"“”]", text[:src.start()])[-1]
    raw = re.sub(r"['’]s\b", "", raw.lower())
    toks = re.findall(r"[a-z0-9]+", raw)
    # A leading outcome label ("$80M+", "90+") is not part of the title.
    while toks and (toks[0] in _REVIEW_LEAD_STOP or re.fullmatch(r"\d+[kmb]?", toks[0])):
        toks.pop(0)
    toks = [t for t in toks if t not in {"the", "a", "an"}]
    if not toks or len(toks) > 6:
        return None
    return " ".join(toks)


def review_title_conflict(a: str, b: str) -> bool:
    """True when both sides are review-score markets and neither side's
    extracted title occurs (whole-word) in the other side's text. Containment
    rather than equality keeps sloppy unquoted extractions ("oppenheimer gross
    more than 80m on") from vetoing genuine pairs."""
    if not (_REVIEW_SOURCE_RE.search(a) and _REVIEW_SOURCE_RE.search(b)):
        return False
    ta, tb = review_work_title(a), review_work_title(b)
    if not (ta or tb):
        return False
    na, nb = _review_norm(a), _review_norm(b)
    a_in_b = bool(ta) and re.search(rf"\b{re.escape(ta)}\b", nb) is not None
    b_in_a = bool(tb) and re.search(rf"\b{re.escape(tb)}\b", na) is not None
    return not a_in_b and not b_in_a


def _review_norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


_CEREMONIES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.I), k) for p, k in (
        (r"\bstreamer\s+awards?\b", "streamer"),
        (r"\besports\s+awards?\b", "esports"),
        (r"\bgame\s+awards\b", "game_awards"),
        (r"\bgolden\s+joystick\b", "golden_joystick"),
        (r"\boscars?\b|\bacademy\s+awards?\b", "oscars"),
        (r"\bgolden\s+globes?\b", "golden_globes"),
        (r"\bemmys?\b", "emmys"),
        (r"\bgrammys?\b", "grammys"),
        (r"\bbafta\b", "bafta"),
        (r"\bsag\s+awards?\b|\bscreen\s+actors\s+guild\b", "sag"),
        (r"\bcritics['’]?\s+choice\b", "critics_choice"),
        (r"\bmtv\s+(?:video\s+music\s+awards?|vmas?)\b|\bvmas?\b", "vmas"),
        (r"\bbillboard\s+music\s+awards?\b", "billboard"),
        (r"\bballon\s+d['’]?or\b", "ballon_dor"),
        (r"\btony\s+awards?\b|\btonys\b", "tonys"),
    )
)


def award_ceremonies(text: str) -> frozenset[str]:
    return frozenset(key for pat, key in _CEREMONIES if pat.search(text))


def award_ceremony_conflict(a: str, b: str) -> bool:
    """True when both sides name an awarding body and the named sets are
    disjoint (Streamer Awards vs Esports Awards). A side that names no
    ceremony is neutral."""
    ca, cb = award_ceremonies(a), award_ceremonies(b)
    return bool(ca and cb and ca.isdisjoint(cb))

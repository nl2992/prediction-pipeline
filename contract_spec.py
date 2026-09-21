"""
Structured contract matching — the v2 decision layer (redesign prototype).

WHY: matcher.is_compatible_match grew into ~48 ordered `return False` vetoes.
It is accurate (100% on the 50-pair fixture) but opaque — diagnosing a rejection
required sys.settrace — and every new failure mode costs another regex stanza
whose safe placement depends on the 47 before it.

This module restructures the SAME proven signals into an explicit two-phase
pipeline:

  1. EXTRACT  each market title into a ContractSpec — subject entities, event
              class, numeric threshold, settlement shape, polarity, time scope,
              ordered head-to-head participants.
  2. COMPARE  two specs field by field. Every verdict carries human-readable
              reasons, and logically-complementary fields (threshold direction
              flip + touch/hold, polarity flip) yield an INVERTED match instead
              of a rejection.

Extraction reuses matcher.py's battle-tested helpers, so v1 and v2 share signal
quality and differ only in decision structure. v1 remains the production path;
match_spec() is evaluated side-by-side by tests/test_contract_spec.py and
documented in docs/history/PIPELINE_REDESIGN.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from matcher import (
    _ascii_lower,
    _bare_subject_name,
    _same_person_variant,
    _close_delta_hours,
    _contract_actions,
    _contract_text,
    _domains,
    _is_ou_or_spread,
    _is_player_prop,
    _is_win_market,
    _jaccard,
    _jurisdictions,
    _known_orgs,
    _known_products,
    _matchup_signature,
    _month_names,
    _monetary_direction,
    _named_entities,
    _names_overlap,
    _numeric_threshold,
    _selected_names,
    _settlement_type,
    _threshold_equal,
    _time_scopes,
    _tokens,
    _winner_subject,
    _years,
    _INVERSION_ANTONYMS,
    _POLITICAL_EVENT_ACTIONS,
    _squash,
)

if TYPE_CHECKING:
    from pipeline import MarketSnapshot


# ---------------------------------------------------------------------------
# Phase 1 — extraction
# ---------------------------------------------------------------------------

_BEAT_RE = re.compile(
    r"\b([A-Z][\w .'-]*?)\s+(?:to\s+)?(?:beats?|defeats?|upsets?)\s+(?:the\s+)?([A-Z][\w .'-]*?)(?:\s*[?.]|$)"
)


@dataclass(frozen=True)
class ContractSpec:
    """Structured reading of one market title."""

    tokens: frozenset[str]
    entities: frozenset[str]
    winner_subject: frozenset[str]
    selected_names: frozenset[str]
    bare_subject_names: frozenset[str]
    orgs: frozenset[str]
    products: frozenset[str]
    domains: frozenset[str]
    jurisdictions: frozenset[str]
    actions: frozenset[str]
    political_actions: frozenset[str]
    monetary_direction: frozenset[str]
    threshold: tuple[str, float, str] | None
    bet_type: str | None                # "moneyline" | "line" | "prop" | None
    settlement: str | None              # "point"(None) | "touch" | "hold"
    polarity: bool                      # True = negative state framing (banned/illegal…)
    years: frozenset[str]
    months: frozenset[str]
    time_scopes: frozenset[str]
    beat_order: tuple[str, str] | None  # ordered (winner, loser) for "A beat B"
    close_time: str | None
    settle_src: frozenset[str]          # compact settlement-source tags (see settlement_source())
    # Outcome label: Kalshi's yes_sub_title, Polymarket's short market title.
    # Exact equality means both contracts name the SAME outcome, which outranks
    # the name/subject heuristics (they exist to separate different contestants).
    outcome_label: str = ""
    raw: str = field(repr=False, default="")


_IPO_RE = re.compile(r"\b(ipo|go(?:es)? public|public offering|direct listing)\b")
_ACQUISITION_RE = re.compile(
    r"\b(acquir\w*|acquisition|bought by|buyout|taken private|merger|merges?|merged)\b")
_BANKRUPTCY_RE = re.compile(r"\b(bankruptcy|bankrupt|chapter 11|chapter 7|insolvenc\w+|insolvent)\b")


def _corporate_event(text: str) -> str | None:
    """'ipo' | 'acquisition' | 'bankruptcy' for corporate-event markets, else None.
    These are mutually exclusive terminal states for a company, so a market about
    one is a different contract from a market about another (#28, #30)."""
    low = _ascii_lower(text)
    if _IPO_RE.search(low):
        return "ipo"
    if _ACQUISITION_RE.search(low):
        return "acquisition"
    if _BANKRUPTCY_RE.search(low):
        return "bankruptcy"
    return None


def _bet_type(text: str) -> str | None:
    """Sports bet type: a moneyline (win), a totals/spread line, or a player
    stat-prop are different CONTRACTS even on the same team/player. Used by the
    v2 sports gate so e.g. "Sweden 1st Half O/U 0.5" never matches "Will Sweden
    win the 1st Half?". 'line' merges totals and spreads on purpose — they are
    often equivalent restatements ("(-1.5)" == "wins by over 1.5 goals").
    """
    low = _ascii_lower(text)
    # Order matters: a totals/spread line ("score over 0.5", "(-1.5)") is a LINE,
    # checked first. A bare to-score / both-teams-to-score market (no numeric
    # line) is its own 'score' type — different from a spread/margin line, so
    # "(-1.5)" vs "Will Team score?" is rejected, while "Both Teams to Score" vs
    # "Will both teams score?" stays matched (run 23).
    if _is_ou_or_spread(text):
        # Split into TOTAL (sum of goals: "O/U 2.5", "score over 0.5") vs
        # MARGIN/spread ("(-1.5)", "wins by over 2.5 goals"). These are different
        # contracts — "Korea O/U 2.5" != "Korea wins by over 2.5 goals" (run 26) —
        # while total↔total and margin↔margin stay matched (Bosnia totals, DR
        # Congo "(-1.5)" ↔ "wins by over 1.5 goals").
        if re.search(r"\(\s*[+-]?\d", low) or re.search(r"\bwins?\s+by\b|\bwin\s+by\b", low):
            return "margin"
        return "total"
    # Exact correct-score ("Saudi Arabia 0 - 2 Uruguay", "Brazil 2-1 Argentina")
    # is a different contract from a moneyline/margin on the same teams (run 27).
    # Single-digit, word-bounded so it doesn't catch years/dates (e.g. 2026-06).
    # Checked before 'score' so "Brazil 2-1 ... correct score?" is correct_score.
    if re.search(r"\b\d\s*[-–]\s*\d\b", low):
        return "correct_score"
    if re.search(r"\bboth teams\b.{0,20}\bscore\b|\bto score\b|\bscore\b\s*\??\s*$", low):
        return "score"
    if _is_player_prop(text):
        return "prop"
    if _is_win_market(text):
        return "moneyline"
    return None


_RANGE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%?\s*(?:to|[-–—])\s*(-?\d+(?:\.\d+)?)")


def _num_range(text: str) -> tuple[float, float] | None:
    """Extract a small numeric bucket "A to B" / "A-B" (GDP %, temperature °,
    correct-score). Returns (lo, hi) or None. Excludes large numbers (years,
    ids) so "2026-06" isn't read as a range.
    """
    m = _RANGE_RE.search(_ascii_lower(text))
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    if abs(a) >= 100 or abs(b) >= 100:
        return None
    return (min(a, b), max(a, b))


# ---------------------------------------------------------------------------
# Iteration-6 discriminators — narrow structural fixes for the remaining
# depth-rescued false-positive classes (see
# docs/history/COVERAGE_ITERATIONS.md, iteration 5 "unfixed classes" /
# iteration 6). Each is gated on zero labelled-same pairs lost across all
# three audit fixtures.
# ---------------------------------------------------------------------------

# A ladder-end idiom Kalshi (and occasionally Polymarket) titles use for the
# OPEN-ENDED rung of a bucket ladder: "90° or below" / "$150k or above". This
# covers the whole half-line beyond the named value, unlike a genuine
# two-sided bucket ("90-91°F"), which covers only that narrow band. The two
# are structurally different contracts even when they share a boundary
# number — run audit caught "90-91°F" (a mid-ladder Polymarket bucket)
# phantom-matched to Kalshi's "<91°... 90° or below" (the BOTTOM, cumulative
# rung: YES for any reading <=90, not just [90, 91)).
_CUMULATIVE_BUCKET_RE = re.compile(r"\b\d+(?:\.\d+)?\s*[a-z]?\s*(?:or\s+(?:below|above|less|more))\b")


def _is_cumulative_bucket(text: str) -> bool:
    """True when text phrases a threshold as an open-ended "or below"/"or
    above" bucket rather than a two-sided numeric range. Pure threshold
    phrasings ("Below 5.15%") also match this idiom on both sides of a
    genuinely-equivalent pair, but those never produce a competing
    ``_num_range`` on either side, so they are unaffected by the gate that
    uses this (it only fires when the OTHER side is a genuine narrow range).
    """
    return bool(_CUMULATIVE_BUCKET_RE.search(_ascii_lower(text)))


# Sports "Week N" scheduling buckets ("Week 1", "Week 1 to Week 2"). A single
# week and a multi-week window are different granularity contracts even when
# they overlap — run audit caught Polymarket "Week 1" (single-week) phantom-
# matched to Kalshi "Week 1 to Week 2" (a two-week window) for the same
# underlying question ("Fernando Mendoza first start").
_WEEK_RANGE_RE = re.compile(r"\bweek\s*(\d+)\s*(?:to|through|thru|-|–|—)\s*week?\s*(\d+)\b")
_WEEK_SINGLE_RE = re.compile(r"\bweek\s*(\d+)\b")


def _week_bucket(text: str) -> tuple[int, int] | None:
    """Extract the (lo, hi) week window a title names, or None if it names
    none. A bare "Week N" is the single-week bucket (N, N)."""
    low = _ascii_lower(text)
    m = _WEEK_RANGE_RE.search(low)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))
    m = _WEEK_SINGLE_RE.search(low)
    if m:
        n = int(m.group(1))
        return (n, n)
    return None


# One-sided COUNT rungs ("Above 68", "at least 85", "fewer than 66", "85+"):
# integers with a comparative cue but no count noun, which _numeric_threshold
# deliberately skips. Each match normalises to the integer cutoff that
# resolves YES, mirroring _numeric_threshold's count branch so equivalent
# phrasings ("more than 84.5" -> 85, "at least 85" -> 85) compare equal.
# Numbers followed by "." or "%" are excluded — those are pct/usd thresholds,
# handled by the threshold gate in match_spec.
_RUNG_UP_RE = re.compile(
    r"\b(at least|or above|above|over|more than|greater than)\s+(\d[\d,]*)(?![\d.%])")
_RUNG_DOWN_RE = re.compile(
    r"\b(at most|or below|below|under|less than)\s+(\d[\d,]*)(?![\d.%])")
# The lookbehind keeps decimals ("84.5+") from matching the "5+" tail.
_RUNG_PLUS_RE = re.compile(r"(?<![\d.])\b(\d[\d,]*)\s*\+(?![\d.])")


def _rung_cutoffs(text: str) -> frozenset[int]:
    """Normalised integer cutoffs of one-sided count rungs in ``text``."""
    low = _ascii_lower(text)
    out: set[int] = set()
    for m in _RUNG_UP_RE.finditer(low):
        n = int(m.group(2).replace(",", ""))
        out.add(n if m.group(1) == "at least" else n + 1)
    for m in _RUNG_DOWN_RE.finditer(low):
        n = int(m.group(2).replace(",", ""))
        out.add(n + 1 if m.group(1) in ("at most", "or below") else n)
    for m in _RUNG_PLUS_RE.finditer(low):
        out.add(int(m.group(1).replace(",", "")))
    return frozenset(out)


# Explicit fiscal-period tags: a calendar quarter ("Q4 2026") or the word
# "annual"/"full year". Two DIFFERENT explicit tags are mutually exclusive
# time scopes for the same recurring economic series (GDP, inflation) — run
# audit caught Polymarket "Negative GDP growth in 2026?" (an annual, whole-
# year question by construction — no quarter is named) phantom-matched to
# Kalshi's "...GDP... in Q4 2026?" (a single-quarter reading). Deliberately
# requires an EXPLICIT "annual"/"full year" cue rather than inferring it from
# the absence of a quarter mention, so the vast majority of same-period pairs
# that just say "in 2026" on both sides (naming no period at all) are never
# touched by this gate.
_QUARTER_RE = re.compile(r"\bq([1-4])\b")
_ANNUAL_RE = re.compile(r"\bannual(?:ly)?\b|\bfull[- ]year\b|\bfull[- ]calendar[- ]year\b")


def _fiscal_period(text: str) -> str | None:
    low = _ascii_lower(text)
    m = _QUARTER_RE.search(low)
    if m:
        return f"q{m.group(1)}"
    if _ANNUAL_RE.search(low):
        return "annual"
    return None


# Party/office descriptor tokens that are NOT parts of a person's name. A
# two-token fragment containing one ("Democratic Vice", "Democratic VP", "Vice
# Presidency") is an office label, not a person, and must not drive the
# different-person collision gate (run 52: "Ro Khanna" ↔ "Ro Khanna … VP nominee"
# was falsely rejected because "democratic vice" vs "democratic vp" shared the
# "first name" democratic).
_NON_PERSON_NAME_TOKENS = frozenset({
    # party / office descriptors (run 52)
    "democratic", "republican", "party", "vice", "vp", "presidency",
    "presidential", "president", "nominee", "senate", "house", "governor",
    # competition / contest descriptors (run 54): event titles like "Liga 1 Peru
    # Champion" / "Peru Liga 1: Winner" produce pseudo-names "peru champion" vs
    # "peru winner" that share the "first name" peru — a country, not a person —
    # and falsely rejected same-club pairs (Cusco FC ↔ Cusco FC, etc.).
    "champion", "champions", "championship", "winner", "league", "liga",
    "cup", "title", "final", "finals",
})


def _first_name_collision(an: frozenset[str], bn: frozenset[str]) -> bool:
    """True when both sides name two-token people who share a FIRST name but have
    DIFFERENT surnames — i.e. different people ("Julian Ryerson" vs "Julian
    Alvarez"). False if either side has a single-token name or any surname is
    shared, so "Trump"↔"Donald Trump" and "Lula"↔"Lula da Silva" are NOT flagged.

    Office/party descriptor fragments ("Democratic Vice", "Vice Presidency") are
    not people and are excluded, so they cannot create a phantom collision.
    """
    def people(names: frozenset[str]) -> list[list[str]]:
        out = []
        for n in names:
            toks = n.split()
            if len(toks) == 2 and not (set(toks) & _NON_PERSON_NAME_TOKENS):
                out.append(toks)
        return out

    two_a = people(an)
    two_b = people(bn)
    if not two_a or not two_b:
        return False
    if {t[1] for t in two_a} & {t[1] for t in two_b}:   # shared surname = same person
        return False
    return bool({t[0] for t in two_a} & {t[0] for t in two_b})  # shared first name only


def _polarity(text: str) -> bool:
    low = _ascii_lower(text)
    for neg, _pos in _INVERSION_ANTONYMS:
        if re.search(neg, low):
            return True
    return False


_STAGE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("seed", re.compile(r"#\s*\d+\s*seed\b|\b\d+(?:st|nd|rd|th)\s+seed\b|\bwild\s*card\b")),
    ("division", re.compile(r"\bdivision\b|\b(?:afc|nfc)\s+(?:east|west|north|south)\b")),
    ("conference", re.compile(r"\bconference\b")),
)


def _playoff_stage(text: str) -> str | None:
    """Sports tournament STAGE the market is about: a specific playoff seed, a
    division title, or a conference championship. These are mutually exclusive
    and NOT interchangeable — winning a division is a materially different
    (and easier) bar than winning the conference, and being seeded #N is
    different again from winning anything.

    Structural fix for an audit FP class: an NHL "2027 Eastern Conference
    Champion" market phantom-matched a "Metropolitan Division Winner" market
    on the same team (5 pairs, run audit_2026-09-14), and an "AFC West
    Champion" market (a division, named by conference+direction per NFL
    convention) phantom-matched an "AFC #5 Seed" market. Matches when both
    sides name the SAME stage; silent (None) when a side doesn't use any of
    these words, so plain "X wins the championship" pairs are unaffected.
    """
    low = _ascii_lower(text)
    for tag, rx in _STAGE_PATTERNS:
        if rx.search(low):
            return tag
    return None


_RANK_RE = re.compile(r"#\s*(\d+)\b")


def _rank_number(text: str) -> int | None:
    """The '#N' rank a market is about ('#1 Searched Person', 'AFC #5 Seed').
    Two different rank numbers are different contracts even when everything
    else (subject, event, close date) lines up — run audit caught '#1 Searched
    Person on Google (US)' phantom-matched to a '#2' rank market for the same
    person/event family."""
    m = _RANK_RE.search(text)
    return int(m.group(1)) if m else None


_GROUP_FROM_RE = re.compile(r"\b(?:a|any)\s+(?:team|player|company|country|state|coach)\s+from\b")


def _group_bucket_jurisdictions(text: str) -> frozenset[str] | None:
    """Jurisdictions named by an aggregate GROUP phrasing ('a team from
    Texas wins...'), or None if the text isn't phrased as a group bucket.

    A group-bucket market settles YES on ANY member of the named group — a
    structurally broader (and different) contract than a market naming one
    specific member. Matching them mis-signals a real settlement difference:
    run audit caught 'a team from Texas' phantom-matched to a market about
    the Atlanta franchise specifically (Atlanta is not a Texas team).
    """
    if not _GROUP_FROM_RE.search(_ascii_lower(text)):
        return None
    return frozenset(_jurisdictions(text))


# ---------------------------------------------------------------------------
# Iteration-3 discriminators — narrow structural fixes for the remaining
# endorsed false-positive classes from the 2026-09-14 audit (see
# docs/history/COVERAGE_ITERATIONS.md, iteration 3). Each is a small, targeted
# regex/lookup — not a generic entity-collision gate — because a previous
# generic "same subject, different context" gate was built and REVERTED after
# it falsely rejected ~41 true same-pairs on noisy multi-word entity glomming.
# ---------------------------------------------------------------------------

# Major-league team nicknames, used ONLY to disambiguate a same-PERSON market
# by the team/org each side names (e.g. "Rocco Baldelli" as Phillies manager
# vs as Red Sox manager). Deliberately a small closed gazetteer of proper
# multi-word franchise names rather than generic capitalized-run extraction,
# so it can't glom onto unrelated text the way the reverted gate did. MLB only
# for now (the observed audit FP class); safe to extend if new classes appear.
# Maps every recognised spelling (full "city + nickname" and bare nickname) to
# one CANONICAL nickname, so "New York Yankees" and "Yankees" collapse to the
# same team for comparison instead of counting as two different ones.
_TEAM_ALIASES: dict[str, str] = {}
for _full, _nick in (
    ("boston red sox", "red sox"), ("new york yankees", "yankees"),
    ("new york mets", "mets"), ("los angeles dodgers", "dodgers"),
    ("los angeles angels", "angels"), ("san francisco giants", "giants"),
    ("san diego padres", "padres"), ("chicago cubs", "cubs"),
    ("chicago white sox", "white sox"), ("philadelphia phillies", "phillies"),
    ("atlanta braves", "braves"), ("miami marlins", "marlins"),
    ("washington nationals", "nationals"), ("milwaukee brewers", "brewers"),
    ("st louis cardinals", "cardinals"), ("pittsburgh pirates", "pirates"),
    ("cincinnati reds", "reds"), ("cleveland guardians", "guardians"),
    ("detroit tigers", "tigers"), ("minnesota twins", "twins"),
    ("kansas city royals", "royals"), ("houston astros", "astros"),
    ("texas rangers", "rangers"), ("seattle mariners", "mariners"),
    ("oakland athletics", "athletics"), ("tampa bay rays", "rays"),
    ("toronto blue jays", "blue jays"), ("baltimore orioles", "orioles"),
    ("colorado rockies", "rockies"), ("arizona diamondbacks", "diamondbacks"),
):
    _TEAM_ALIASES[_full] = _nick
    _TEAM_ALIASES[_nick] = _nick
# Longest spelling first, so "boston red sox" matches before the bare "red sox"
# substring within it (avoids double-counting one mention as two teams).
_KNOWN_TEAMS: tuple[str, ...] = tuple(
    sorted(_TEAM_ALIASES, key=len, reverse=True)
)


def _team_orgs(text: str) -> frozenset[str]:
    low = _ascii_lower(text)
    found: set[str] = set()
    remaining = low
    for spelling in _KNOWN_TEAMS:
        if re.search(rf"\b{re.escape(spelling)}\b", remaining):
            found.add(_TEAM_ALIASES[spelling])
            # Blank out this span so the bare nickname inside a longer,
            # already-matched full name isn't counted a second time.
            remaining = re.sub(rf"\b{re.escape(spelling)}\b", " ", remaining, count=1)
    return frozenset(found)


# Known college-football award FAMILIES. These are mutually-exclusive
# terminal honors — a Heisman market and a Walter Camp market on the same
# player are different contracts even though both reduce to "<player> wins
# the award" once the award name is stripped (audit: Dante Moore / Bo Jackson
# / Julian Sayin all phantom-matched across Heisman <-> Walter Camp/Doak Walker).
_AWARD_FAMILIES: tuple[tuple[str, re.Pattern], ...] = (
    ("heisman", re.compile(r"\bheisman\b")),
    ("walter_camp", re.compile(r"\bwalter camp\b")),
    ("doak_walker", re.compile(r"\bdoak walker\b")),
    ("davey_obrien", re.compile(r"\bdavey o'?brien\b")),
    ("maxwell", re.compile(r"\bmaxwell award\b")),
    ("outland", re.compile(r"\boutland trophy\b")),
    ("biletnikoff", re.compile(r"\bbiletnikoff\b")),
    ("butkus", re.compile(r"\bbutkus\b")),
    ("thorpe", re.compile(r"\bjim thorpe award\b")),
    ("lombardi", re.compile(r"\blombardi award\b")),
    ("unitas", re.compile(r"\bunitas\b")),
)


def _award_family(text: str) -> str | None:
    low = _ascii_lower(text)
    for tag, rx in _AWARD_FAMILIES:
        if rx.search(low):
            return tag
    return None


# Settlement-period markers: a halftime/1st-half result and a full-match
# moneyline/spread/total are different contracts even on the same fixture
# (audit: "Al-Shamal vs. Al-Ittihad - Halftime Result" phantom-matched
# "Al-Ittihad wins by more than N goals?", a full-match spread).
_HALFTIME_RE = re.compile(r"\bhalf\s*-?\s*time\b|\b1st\s+half\b|\bfirst\s+half\b")


def _is_halftime(text: str) -> bool:
    return bool(_HALFTIME_RE.search(_ascii_lower(text)))


# Explicit "1st half" vs "2nd half" result markers — a stricter, bet-type
# independent companion to the halftime-vs-full-match gate above. That gate
# only fires when the non-halftime side resolves to a classifiable
# margin/total/moneyline bet_type, which a bare single-word outcome title
# ("Al-Shamal") never does — so a "Second Half Result" market on one side
# could otherwise bridge to a "First Half" market on the other (iteration-4
# single-entity bridge exposed this: "Al-Shamal vs. Al-Ittihad Club - Second
# Half Result" vs "Al-Shamal vs Al-Ittihad: First Half Winner", same two
# teams, different half). Fires whenever BOTH sides name an explicit half
# and they disagree, independent of bet_type.
_SECOND_HALF_RE = re.compile(r"\b(?:2nd|second)\s+half\b")


def _half_number(text: str) -> int | None:
    low = _ascii_lower(text)
    if _SECOND_HALF_RE.search(low):
        return 2
    if _HALFTIME_RE.search(low):
        return 1
    return None


# Fed/central-bank rate LEVEL ("Fed Rate hit 5.0%") vs number-of-CUTS/CHANGES
# ("number of rate changes ... be exactly 5") are different measurements of
# the same underlying process, not the same contract (audit: "^ 5.0%" / "What
# will Fed Rate hit before 2027?" phantom-matched a rate-change-COUNT market).
_RATE_COUNT_RE = re.compile(
    r"\bnumber of (?:fed |federal (?:funds )?)?rate (?:changes|cuts|hikes)\b"
    r"|\brate (?:cuts|hikes|changes) (?:happen|occur)\b"
)


def _is_rate_count(text: str) -> bool:
    return bool(_RATE_COUNT_RE.search(_ascii_lower(text)))


# "Will <actor> <verb> <object>?" — a small curated verb list where the same
# actor performing the same VERB on two different OBJECTS is a different
# contract (audit: "Will Trump nationalize elections?" phantom-matched "Will
# trump nationalize SpaceX?" — same actor+verb, unrelated object). Narrowly
# scoped to this literal phrasing and a short verb list so it can't glom onto
# unrelated text the way the reverted generic gate did.
_ACTOR_VERB_OBJECT_RE = re.compile(
    r"\bwill\s+([a-z][\w.'-]*(?:\s+[a-z][\w.'-]*)?)\s+"
    r"(nationalize|ban|acquire|buy|sell|invade|annex|sanction|deport|fire|"
    r"pardon|sue|tax|regulate|dissolve)\s+"
    r"(?:the\s+)?([\w\s'.-]*?)\s*\?",
    re.IGNORECASE,
)


def _actor_verb_object(text: str) -> tuple[str, str, frozenset[str]] | None:
    m = _ACTOR_VERB_OBJECT_RE.search(text)
    if not m:
        return None
    actor = _ascii_lower(m.group(1)).strip()
    verb = _ascii_lower(m.group(2)).strip()
    obj = frozenset(_tokens(m.group(3)))
    if not actor or not obj:
        return None
    return (actor, verb, obj)


# bps bucket boundaries: "25 bps decrease" (exact) vs "cut more than 25bps"
# (open-ended, excludes exactly 25) or "50+ bps increase" vs "hike 1-25bps"
# are non-overlapping buckets even though they share direction and most
# tokens (audit: two Bank of England pairs phantom-matched this way).
_BPS_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*bps")
_BPS_MORE_RE = re.compile(r"(?:more than|over|>)\s*(\d+(?:\.\d+)?)\s*bps")
_BPS_ATLEAST_RE = re.compile(r"(?:at least|no less than|>=|≥)\s*(\d+(?:\.\d+)?)\s*bps")
_BPS_PLUS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+\s*bps")
_BPS_LESS_RE = re.compile(r"(?:less than|under|up to|<)\s*(\d+(?:\.\d+)?)\s*bps")
_BPS_EXACT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*bps")
_BPS_DECREASE_RE = re.compile(r"\b(decrease|decreases|cut|cuts|lower|lowers|drop|drops|ease|eases)\b")
_BPS_INCREASE_RE = re.compile(r"\b(increase|increases|hike|hikes|raise|raises)\b")


def _bps_bucket(text: str) -> tuple[str | None, float, float] | None:
    low = _ascii_lower(text)
    if "bps" not in low:
        return None
    if _BPS_DECREASE_RE.search(low):
        direction = "decrease"
    elif _BPS_INCREASE_RE.search(low):
        direction = "increase"
    else:
        direction = None
    m = _BPS_RANGE_RE.search(low)
    if m:
        return (direction, float(m.group(1)), float(m.group(2)))
    m = _BPS_ATLEAST_RE.search(low)
    if m:
        return (direction, float(m.group(1)), float("inf"))
    m = _BPS_MORE_RE.search(low)
    if m:
        return (direction, float(m.group(1)) + 0.01, float("inf"))
    m = _BPS_PLUS_RE.search(low)
    if m:
        return (direction, float(m.group(1)), float("inf"))
    m = _BPS_LESS_RE.search(low)
    if m:
        return (direction, 0.0, float(m.group(1)) - 0.01)
    m = _BPS_EXACT_RE.search(low)
    if m:
        v = float(m.group(1))
        return (direction, v, v)
    return None


# Weekly-recurring markets ("Top ... this week?") vs a market with a
# materially different (month-end/longer) close horizon are different
# contracts even when the subject overlaps a lot (audit: "Top Text to Image
# AI this week? OpenAI" phantom-matched a "best Text-to-Image AI end of
# October" market — same subject, weekly-recurring vs month-scoped).
_RECURRING_WEEK_RE = re.compile(r"\bthis week\b|\bweekly\b")


def _is_weekly_recurring(text: str) -> bool:
    return bool(_RECURRING_WEEK_RE.search(_ascii_lower(text)))


# ---------------------------------------------------------------------------
# Iteration-4 discriminators — narrow structural fixes for the false-positive
# classes found in the fresh 2026-09-14 live audit (tests/fixtures/
# endorsed_audit_iter4.json), since the original 2026-09-14 fixture's false
# positives were mostly fixed by iteration 3 and stopped finding new problems.
# Same philosophy as iteration 3: small, targeted checks, never a generic
# entity-collision gate (one of those was tried earlier in this project and
# reverted for breaking ~41 true pairs).
# ---------------------------------------------------------------------------

# A "Runner-Up" market and a "Champion"/"Winner" market on the same
# competition are mutually exclusive outcomes, not the same contract (audit:
# three USL Championship pairs — New Mexico United, Charleston Battery,
# Colorado Springs Switchbacks — phantom-matched a "2026 Runner-Up" market
# on Polymarket to Kalshi's "win the USL Championship" market for the same
# club).
_RUNNER_UP_RE = re.compile(r"\brunner[- ]?up\b")


def _is_runner_up_market(text: str) -> bool:
    return bool(_RUNNER_UP_RE.search(text.lower()))


# A single fixture's match-winner market ("Team A vs Team B") and a market on
# winning the whole multi-fixture TOURNAMENT/championship are different
# contracts even when they name the same team (audit: "Caribbean Premier
# League: Antigua And Barbuda Falcons vs Guyana Amazon Warriors" — a
# specific-match title — phantom-matched "Will Antigua And Barbuda Falcons
# win the 2026 Caribbean Premier League championship?", a tournament-winner
# market). Detected structurally: one side's text reduces to a bare "X vs Y"
# matchup signature (matcher._matchup_signature), the other explicitly says
# "win the ... championship/cup".
_TOURNAMENT_WIN_RE = re.compile(r"\bwin(?:s|ning)?\s+the\b[^?]{0,40}\b(?:championship|cup|title)\b")


def _is_tournament_champion_market(text: str) -> bool:
    return bool(_TOURNAMENT_WIN_RE.search(text.lower()))


# A song/album DURATION question ("How long will X be?") and a chart-position
# / ranking question about the same work are different contracts (audit:
# "Bass Persuades - Miley Cyrus" #2 Spotify song this week phantom-matched
# "How long will Bass Persuades by Miley Cyrus be? ... Album Duration").
_DURATION_RE = re.compile(r"\bhow long will\b.{0,60}\bbe\b")


def _is_duration_market(text: str) -> bool:
    return bool(_DURATION_RE.search(text.lower()))


# ---------------------------------------------------------------------------
# Settlement-source extraction (iteration 5) — a COMPACT provider/station tag,
# never the full rules/description text (memory: ~270k live snapshots, peak
# RSS already ~3.75 GB; a market's rules_primary/rules_secondary/description
# run to hundreds-to-thousands of chars each, far bigger than the short
# question text already kept in extra["full_question"]).
#
# Investigated live on the motivating case (2026-09-14): Kalshi's
# "Highest temperature in Atlanta" (KXHIGHTATL-26SEP14) rules_primary reads
# "If the maximum temperature recorded at Atlanta (CLIATL) for Sep 14, 2026,
# is less than 92° fahrenheit ACCORDING TO THE WEATHER COMPANY, then the
# market resolves to Yes." Its rules_secondary adds: "checking a source like
# AccuWeather or Google Weather may help guide your decision" but "the
# official and final value ... is ... as reported by the Weather Company."
# Polymarket's "Highest temperature in Atlanta on September 14?" event
# resolves "to the temperature range that contains the highest temperature
# recorded BY NOAA at the Hartsfield-Jackson International Airport Station",
# resolutionSource https://www.weather.gov/wrh/timeseries?site=katl (raw
# hourly obs), falling back to "the Weather Underground Daily Observations
# table" if NOAA data is unavailable.
#
# Both nominally reference the same airport, but they are DIFFERENT
# settlement pipelines: Kalshi's own disclaimer exists precisely because The
# Weather Company's official station report can disagree with other readings
# of "the same" day's high. That is a genuine settlement-source risk, not a
# text-matching false positive — hence a hard reject below, not a soft flag
# (the alerter already requires v2_match True, so rejecting here is what
# keeps mismatched-source pairs out of emails).
# ---------------------------------------------------------------------------

_WEATHER_PROVIDER_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("weather_company", re.compile(r"\bthe weather company\b", re.I)),
    ("noaa", re.compile(r"\bnoaa\b|\bnational weather service\b", re.I)),
    ("wunderground", re.compile(r"\bweather underground\b|\bwunderground\b", re.I)),
    ("accuweather", re.compile(r"\baccuweather\b", re.I)),
)
# Kalshi: "recorded at Atlanta (CLIATL)" -> station "cliatl".
_WEATHER_STATION_PAREN_RE = re.compile(r"recorded at [^(]*\(([a-z]{3,6})\)", re.I)
# Polymarket resolutionSource URL: "...timeseries?site=katl" -> station "katl".
_WEATHER_STATION_SITE_RE = re.compile(r"[?&]site=([a-z]{3,4})\b", re.I)

_CRYPTO_SOURCE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("cf_benchmarks", re.compile(r"\bcf benchmarks\b|\bbrti\b|\bbrri\b", re.I)),
    ("binance", re.compile(r"\bbinance\b", re.I)),
    ("coinbase", re.compile(r"\bcoinbase\b", re.I)),
    ("kraken", re.compile(r"\bkraken\b", re.I)),
)

_ECON_AGENCY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("bls", re.compile(r"\bbureau of labor statistics\b|\bbls\b|\ball urban consumers\b", re.I)),
    ("bea", re.compile(r"\bbureau of economic analysis\b|\bbea\b", re.I)),
    ("fed", re.compile(r"\bfederal reserve\b|\bfomc\b", re.I)),
    ("ons", re.compile(r"\boffice for national statistics\b|\bons\b", re.I)),
    ("stats_sa", re.compile(r"\bstatistics south africa\b|\bstats sa\b", re.I)),
)


def settlement_source(*texts: str) -> tuple[str, ...]:
    """Compact settlement-source tags extracted from a market's OWN rules
    text at INGESTION time — the rules/description text itself is never
    stored (see module comment above). Called from discover.py's snapshot
    builders (_k_snap on ``rules_primary``; _p_snap / _p_snap_from_event on
    ``description`` + ``resolutionSource``) and stashed compactly in
    ``extra["settle_src"]``.

    Tags are ``"<class>:<provider>[:<station>]"``, e.g.
    ``"weather:weather_company:cliatl"``, ``"weather:noaa:katl"``,
    ``"crypto:binance"``, ``"econ:bls"``. Returns () for the overwhelming
    majority of markets outside these three classes (sports "official
    source" wording is deliberately NOT extracted here — low discriminating
    value per the iteration-5 scope).

    A tuple (not frozenset) so it survives ``json.dumps`` in discover.py's
    ``--output``/coverage dumps unchanged; extract_spec() below wraps it in a
    frozenset for comparison.
    """
    blob = " ".join(t for t in texts if t)
    if not blob:
        return ()
    tags: set[str] = set()

    station = None
    m = _WEATHER_STATION_PAREN_RE.search(blob)
    if m:
        station = m.group(1).lower()
    else:
        m = _WEATHER_STATION_SITE_RE.search(blob)
        if m:
            station = m.group(1).lower()
    for tag, rx in _WEATHER_PROVIDER_PATTERNS:
        if rx.search(blob):
            tags.add(f"weather:{tag}:{station}" if station else f"weather:{tag}")

    for tag, rx in _CRYPTO_SOURCE_PATTERNS:
        if rx.search(blob):
            tags.add(f"crypto:{tag}")

    for tag, rx in _ECON_AGENCY_PATTERNS:
        if rx.search(blob):
            tags.add(f"econ:{tag}")

    return tuple(sorted(tags))


def _settle_src_conflict(
    a_src: frozenset[str], b_src: frozenset[str]
) -> tuple[str, list[str], list[str]] | None:
    """None if compatible; else (class, sorted providers A, sorted providers B).

    Compares PROVIDER identity within the same class only (station codes are
    spelled differently per venue for the very same airport — "cliatl" vs
    "katl" — so station is informational, not itself a gate). A side that
    cites a provider AND its documented fallback (e.g. Polymarket's "NOAA,
    falling back to Weather Underground") carries both tags, so it is never
    disjoint against a counterparty using either one alone — only a genuine,
    unshared provider set trips this.
    """
    def by_class(src: frozenset[str]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for t in src:
            cls, _, rest = t.partition(":")
            provider = rest.split(":", 1)[0]
            out.setdefault(cls, set()).add(provider)
        return out

    da, db = by_class(a_src), by_class(b_src)
    for cls in sorted(set(da) & set(db)):
        if da[cls].isdisjoint(db[cls]):
            return cls, sorted(da[cls]), sorted(db[cls])
    return None


# Iteration-5: "Will Trump's first endorsement before the primaries be the
# 2028 GOP nominee?" (Polymarket) is a bet on TRUMP'S OWN, not-yet-made
# endorsement decision proving correct — it settles YES only if whoever Trump
# endorses first also becomes the nominee. That is a structurally different
# contract from any market naming a SPECIFIC candidate for the same race
# (e.g. a "Donald Trump Jr." or "Ivanka Trump" 2028-nominee/caucus market),
# even when both share heavy token overlap (2028, republican, nominee,
# trump). Narrowly scoped to this literal "first endorsement" phrasing so it
# can't glom onto unrelated endorsement markets.
_FIRST_ENDORSEMENT_RE = re.compile(r"\bfirst endorsement\b", re.I)


def _is_first_endorsement_market(text: str) -> bool:
    return bool(_FIRST_ENDORSEMENT_RE.search(text))


def _beat_order(text: str) -> tuple[str, str] | None:
    m = _BEAT_RE.search(text)
    if not m:
        return None
    win = frozenset(_tokens(m.group(1)))
    lose = frozenset(_tokens(m.group(2)))
    if not win or not lose:
        return None
    return (" ".join(sorted(win)), " ".join(sorted(lose)))


def _field_texts(snap: "MarketSnapshot") -> tuple[str, ...]:
    """The market's title/event-title/full-question text AS SEPARATE fields
    (not joined). _contract_text() concatenates them into one string for
    bag-of-words signals (tokens, jaccard), which is fine — but a regex that
    hunts for a capitalized NAME run can bleed ACROSS that artificial join:
    "Renan Santos" (title) directly followed by "Brazil Presidential Election
    First Round Winner" (event title) reads as one long capitalized run, and
    matcher._proper_names's office+jurisdiction filter drops the WHOLE blob —
    losing the real name entirely and leaving a phantom "First Round Winner"
    fragment behind (audit false-reject: this cost ~8 genuinely-same pairs
    their selected-name overlap). Extracting names per FIELD and unioning
    avoids the cross-field artifact while still catching names that are whole
    within a single field.
    """
    extra = getattr(snap, "extra", {}) or {}
    return tuple(
        str(x) for x in (
            getattr(snap, "title", ""),
            extra.get("event_title", ""),
            extra.get("full_question", ""),
        )
        if x
    )


def _names_without_parentheticals(snap: "MarketSnapshot") -> tuple[str, ...]:
    """Field texts with "(…)" spans removed before name extraction.

    A Polymarket disambiguating parenthetical is not part of a person's name:
    "Gary (Stephen Wilson Jr.)" yielded the phantom 2-token person
    "stephen wilson", and the first-name-collision gate then read "Gary" vs
    "Stephen Wilson" as two different people (pass 10 — a v2 false-reject on a
    true CMA Song of the Year pair). Party suffixes ("(D)") are already
    ignored by the label comparators; this keeps name extraction consistent.
    """
    return tuple(re.sub(r"\(.*?\)", " ", t) for t in _field_texts(snap))


def _selected_names_per_field(snap: "MarketSnapshot") -> frozenset[str]:
    names: set[str] = set()
    for field_text in _names_without_parentheticals(snap):
        names.update(_selected_names(field_text))
    return frozenset(names)


def _bare_subject_names_per_field(snap: "MarketSnapshot") -> frozenset[str]:
    """Per-field union of _bare_subject_name — single-word names recall-only,
    kept separate from selected_names so they never feed the hard
    selected-name-mismatch veto (see _bare_subject_name's docstring)."""
    names: set[str] = set()
    for field_text in _names_without_parentheticals(snap):
        name = _bare_subject_name(field_text)
        if name:
            names.add(name)
    return frozenset(names)


def extract_spec(snap: "MarketSnapshot") -> ContractSpec:
    text = _contract_text(snap)
    return ContractSpec(
        tokens=frozenset(_tokens(text)),
        entities=frozenset(_named_entities(text)),
        winner_subject=frozenset(_winner_subject(text)),
        selected_names=_selected_names_per_field(snap),
        bare_subject_names=_bare_subject_names_per_field(snap),
        orgs=frozenset(_known_orgs(text)),
        products=frozenset(_known_products(text)),
        domains=frozenset(_domains(text)),
        jurisdictions=frozenset(_jurisdictions(text)),
        actions=frozenset(_contract_actions(text)),
        political_actions=frozenset(_contract_actions(text)) & _POLITICAL_EVENT_ACTIONS,
        monetary_direction=frozenset(_monetary_direction(text)),
        threshold=_numeric_threshold(text),
        bet_type=_bet_type(text),
        settlement=_settlement_type(text),
        polarity=_polarity(text),
        years=frozenset(_years(text)),
        months=frozenset(_month_names(text)),
        time_scopes=frozenset(_time_scopes(text)),
        beat_order=_beat_order(text),
        close_time=getattr(snap, "close_time", None),
        settle_src=frozenset((getattr(snap, "extra", {}) or {}).get("settle_src") or ()),
        outcome_label=((getattr(snap, "extra", None) or {}).get("yes_sub_title")
                       if getattr(snap, "source", "") == "kalshi"
                       else getattr(snap, "title", "")) or "",
        raw=text,
    )


# ---------------------------------------------------------------------------
# Phase 2 — field-wise comparison
# ---------------------------------------------------------------------------


@dataclass
class MatchDecision:
    match: bool
    inverted: bool
    confidence: float
    reasons: list[str]

    def __bool__(self) -> bool:  # truthy when matched
        return self.match


def _reject(reason: str) -> MatchDecision:
    return MatchDecision(False, False, 0.0, [reason])


def _label_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _ascii_lower(re.sub(r"\(.*?\)", "", label or "")))


def _is_name_label(label: str) -> bool:
    """Outcome label that names a person/team ("Patrick Mahomes", "Tokyo Yakult
    Swallows") rather than a level or phrase ("25 bps increase", "Above 4.5%")."""
    words = re.findall(r"[^\W\d_][\w'’.-]*", label or "")
    return len(words) >= 2 and all(w[:1].isupper() for w in words) and not re.search(r"\d", label or "")


def _different_named_outcome(a: ContractSpec, b: ContractSpec) -> bool:
    """Both sides name an outcome and neither name contains the other —
    "Bo Nix" vs "Patrick Mahomes" is a different contract, while
    "Yakult Swallows" vs "Tokyo Yakult Swallows" is the same one."""
    if not (_is_name_label(a.outcome_label) and _is_name_label(b.outcome_label)):
        return False
    la, lb = _label_key(a.outcome_label), _label_key(b.outcome_label)
    if len(la) < 5 or len(lb) < 5:
        return False
    if la in lb or lb in la:
        return False
    # Name variants of ONE person are the same outcome: short forms ("Ben" /
    # "Benjamin Silverman", "Cam" / "Cameron Davis"), spelling ("Jaxon" /
    # "Jaxson"), and inserted middle names ("Jordan L. Smith").
    if _same_person_variant(a.outcome_label, b.outcome_label):
        return False
    # One name may carry middle names or a second surname the other omits
    # ("Justin J. Pearson", "Kendor Gregorio Macías Martínez"): a strict token
    # subset sharing >= 2 words is the same person. Party suffixes "(D)"/"(R)"
    # are stripped first. "Los Angeles Lakers" vs "Los Angeles C" is NOT a
    # subset (C is not a Lakers token), so rival teams stay rejected.
    def toks(label: str) -> set[str]:
        bare = re.sub(r"\(.*?\)", " ", label or "")
        return {w for w in (re.sub(r"[^a-z0-9]", "", x) for x in _ascii_lower(bare).split()) if w}

    ta, tb = toks(a.outcome_label), toks(b.outcome_label)
    if ta and tb and len(ta & tb) >= 2 and (ta <= tb or tb <= ta):
        return False
    return True


def _same_outcome_label(a: ContractSpec, b: ContractSpec) -> bool:
    # Floor 3 so short esports handles ("gary", "bang") count as outcome
    # identity, mirroring matcher's v1 floor (pass 10): an identical label
    # outranks the person/name heuristics, which exist to separate DIFFERENT
    # contestants. Generic labels like "Yes" can now also match, but only when
    # the question text is similar enough to pass the gates below.
    la = re.sub(r"[^a-z0-9]", "", _ascii_lower(re.sub(r"\(.*?\)", "", a.outcome_label)))
    lb = re.sub(r"[^a-z0-9]", "", _ascii_lower(re.sub(r"\(.*?\)", "", b.outcome_label)))
    return len(la) >= 3 and la == lb


def match_spec(
    a: ContractSpec,
    b: ContractSpec,
    min_similarity: float = 0.30,
    same_event: bool = False,
) -> MatchDecision:
    """Compare two ContractSpecs field by field.

    Hard gates reject with an explicit reason; complementary fields flip the
    pair to inverted instead of rejecting; acceptance requires either token
    similarity over the gate or the threshold-led bridge (equal strike + shared
    entity + same horizon). ``same_event`` (squashed event-title equality,
    computed by explain()) additionally accepts an identical outcome label:
    a one-line PM label and a verbose Kalshi legal description ("SELF DRIVE
    Act" vs a 40-word bill description) are the same contract, and the
    full-text similarity gate under-fires on the length asymmetry (pass 10).
    """
    reasons: list[str] = []
    inverted = False

    dh = _close_delta_hours(a.close_time, b.close_time)
    same_horizon = dh is not None and dh <= 72.0

    # --- identity gates -----------------------------------------------------
    if same_event and _same_outcome_label(a, b):
        reasons.append("identical event + identical outcome label")
        return MatchDecision(True, inverted, 1.0, reasons)
    if a.domains and b.domains and a.domains.isdisjoint(b.domains):
        return _reject(f"domain mismatch: {sorted(a.domains)} vs {sorted(b.domains)}")
    if a.jurisdictions and b.jurisdictions and a.jurisdictions.isdisjoint(b.jurisdictions):
        return _reject(f"jurisdiction mismatch: {sorted(a.jurisdictions)} vs {sorted(b.jurisdictions)}")
    # Bilateral markets that share one country but each name another the other
    # lacks are different contracts ("Israel and Lebanon normalize" vs "Israel
    # and Qatar normalize") — the disjoint gate above misses them because they
    # overlap on the shared country. (run 43)
    if (len(a.jurisdictions) >= 2 and len(b.jurisdictions) >= 2
            and (a.jurisdictions - b.jurisdictions) and (b.jurisdictions - a.jurisdictions)):
        return _reject(
            f"different country set: {sorted(a.jurisdictions)} vs {sorted(b.jurisdictions)}")
    if a.orgs and b.orgs and a.orgs.isdisjoint(b.orgs):
        return _reject(f"org mismatch: {sorted(a.orgs)} vs {sorted(b.orgs)}")
    # Settlement-source mismatch: both sides cite a provider for the same
    # class (weather/crypto/econ) but the providers don't overlap — a genuine
    # settlement risk, not a text-matching false positive. See
    # settlement_source()'s docstring for the motivating Atlanta case.
    src_conflict = _settle_src_conflict(a.settle_src, b.settle_src)
    if src_conflict:
        cls, pa, pb = src_conflict
        return _reject(f"settle_src_mismatch ({cls}): {pa} vs {pb}")
    if a.products and b.products and a.products.isdisjoint(b.products):
        return _reject(f"product mismatch: {sorted(a.products)} vs {sorted(b.products)}")
    # Same named outcome on both sides: the subject/name heuristics exist to
    # separate DIFFERENT contestants, so they must not fire here (a party-worded
    # Kalshi question carries the candidate in yes_sub_title).
    same_outcome = _same_outcome_label(a, b)
    if _different_named_outcome(a, b):
        return _reject(f"different named outcome: {a.outcome_label!r} vs {b.outcome_label!r}")
    if not same_outcome and a.winner_subject and b.winner_subject and not _names_overlap(
        set(a.winner_subject), set(b.winner_subject)
    ):
        return _reject(
            f"winner-subject mismatch: {sorted(a.winner_subject)} vs {sorted(b.winner_subject)}"
        )
    if not same_outcome and a.selected_names and b.selected_names and not _names_overlap(
        set(a.selected_names), set(b.selected_names)
    ):
        return _reject(
            f"selected-name mismatch: {sorted(a.selected_names)} vs {sorted(b.selected_names)}"
        )
    # Different people who share a first name ("Julian Ryerson" vs "Julian
    # Alvarez") — _names_overlap treats them as overlapping via the shared first
    # name, so guard surnames explicitly on the SELECTED names (run 42). Entities
    # are too broad (they broke fixture parity), so only selected_names is used.
    if not same_outcome and _first_name_collision(a.selected_names, b.selected_names):
        return _reject("different person: shared first name, different surname")

    # Same PERSON, different team/org context (e.g. Rocco Baldelli as Phillies
    # manager candidate vs Red Sox manager candidate). Gated on an actual
    # selected-name overlap (so this only applies to person markets already
    # agreeing on WHO) plus both sides naming a KNOWN team/org from the closed
    # gazetteer above — never a generic entity diff, which was tried and
    # reverted for breaking ~41 true pairs.
    if a.selected_names & b.selected_names:
        teams_a, teams_b = _team_orgs(a.raw), _team_orgs(b.raw)
        if teams_a and teams_b and teams_a.isdisjoint(teams_b):
            return _reject(
                f"same person, different team/org: {sorted(teams_a)} vs {sorted(teams_b)}"
            )

    # Award-family mismatch: mutually exclusive named honors (Heisman, Walter
    # Camp, Doak Walker, ...) on the same player.
    award_a, award_b = _award_family(a.raw), _award_family(b.raw)
    if award_a and award_b and award_a != award_b:
        return _reject(f"award-family mismatch: {award_a} vs {award_b}")

    # Aggregate GROUP bucket ("a team from Texas") vs a specific member named
    # on the other side but outside that group's jurisdiction.
    grp_a = _group_bucket_jurisdictions(a.raw)
    grp_b = _group_bucket_jurisdictions(b.raw)
    if grp_a and not (grp_a & frozenset(b.tokens)):
        return _reject(f"group bucket ({sorted(grp_a)}) vs entity outside that group")
    if grp_b and not (grp_b & frozenset(a.tokens)):
        return _reject(f"group bucket ({sorted(grp_b)}) vs entity outside that group")

    # Sports playoff STAGE (division / conference / seed) — mutually exclusive.
    stage_a, stage_b = _playoff_stage(a.raw), _playoff_stage(b.raw)
    if stage_a and stage_b and stage_a != stage_b:
        return _reject(f"playoff-stage mismatch: {stage_a} vs {stage_b}")

    # "#N" rank markets — different N is a different contract.
    rank_a, rank_b = _rank_number(a.raw), _rank_number(b.raw)
    if rank_a is not None and rank_b is not None and rank_a != rank_b:
        return _reject(f"rank mismatch: #{rank_a} vs #{rank_b}")

    # --- sports bet-type gate -------------------------------------------------
    # A moneyline (win), a totals/spread line, and a player stat-prop are
    # DIFFERENT contracts even on the same team/player. This is the structural
    # fix for the run-12 phantom-arb flood ("Sweden 1st Half O/U 0.5" vs "Will
    # Sweden win the 1st Half?", "Cody Gakpo" vs "Cody Gakpo: 2+ assists").
    if a.bet_type and b.bet_type and a.bet_type != b.bet_type:
        return _reject(f"bet-type mismatch: {a.bet_type} vs {b.bet_type}")
    # Corporate event-type gate: an IPO market and an acquisition/merger market on
    # the same company are mutually exclusive outcomes, not the same contract (#28).
    ca, cb = _corporate_event(a.raw), _corporate_event(b.raw)
    if ca and cb and ca != cb:
        return _reject(f"corporate-event mismatch: {ca} vs {cb}")
    # A player prop on one side and a non-prop market on the same subject
    # (the other side has no bet_type) is still a different contract.
    # Two leader markets worded differently ("QB Points Leader" / "Season Top
    # QB") are one contract, not prop-vs-plain (mirrors matcher.py).
    both_leader = "stat_leader" in a.actions and "stat_leader" in b.actions
    if not both_leader and (a.bet_type == "prop") != (b.bet_type == "prop") and (
        (a.entities & b.entities)
        or (a.selected_names & b.selected_names)
        or (a.winner_subject & b.winner_subject)
    ):
        return _reject("player-prop vs non-prop on same subject")

    # Settlement-period mismatch: a halftime/1st-half result market vs a
    # full-match moneyline/spread/total market on the same fixture.
    halftime_a, halftime_b = _is_halftime(a.raw), _is_halftime(b.raw)
    if halftime_a != halftime_b:
        full_side_bet = b.bet_type if halftime_a else a.bet_type
        if full_side_bet in ("margin", "total", "moneyline"):
            return _reject(
                f"settlement-period mismatch: halftime vs full-match ({full_side_bet})"
            )
    half_a, half_b = _half_number(a.raw), _half_number(b.raw)
    if half_a is not None and half_b is not None and half_a != half_b:
        return _reject(f"settlement-period mismatch: half {half_a} vs half {half_b}")

    # Runner-up vs champion/winner — mutually exclusive tournament outcomes.
    if _is_runner_up_market(a.raw) != _is_runner_up_market(b.raw):
        return _reject("outcome-tier mismatch: runner-up on one side only")

    # Single-match winner vs whole-tournament champion — different contracts
    # even naming the same team.
    matchup_a, matchup_b = _matchup_signature(a.raw), _matchup_signature(b.raw)
    if (matchup_a is not None) != (matchup_b is not None):
        tourney_side = b.raw if matchup_a is not None else a.raw
        if _is_tournament_champion_market(tourney_side):
            return _reject("single-match vs tournament-champion mismatch")

    # Song/album duration vs chart-position/ranking — different measurements
    # of the same work.
    if _is_duration_market(a.raw) != _is_duration_market(b.raw) and (
        a.selected_names & b.selected_names
    ):
        return _reject("duration vs chart-position mismatch")

    # --- event-class gates ----------------------------------------------------
    if a.actions and b.actions and a.actions.isdisjoint(b.actions):
        return _reject(f"action mismatch: {sorted(a.actions)} vs {sorted(b.actions)}")
    # A chart-achievement market (#1 hit/song) on one side vs a non-chart market
    # on the same artist is a different contract — "Hit The Wall - Gracie Abrams"
    # (a song entry) vs "Gracie Abrams have a #1 hit" (run 35). The plain action
    # gate misses it because both share a spurious 'stat_prop' from "hit".
    if ("song_chart" in a.actions) != ("song_chart" in b.actions) and (a.entities & b.entities):
        return _reject("chart-achievement vs non-chart on same artist")
    # Trump's own not-yet-made "first endorsement" proxy bet vs a market
    # naming a specific candidate for the same race (run iteration-5).
    if _is_first_endorsement_market(a.raw) != _is_first_endorsement_market(b.raw):
        return _reject("actor-scoped subject mismatch: endorsement-proxy vs named-candidate")
    # Coin toss vs winning the match/tournament — both extract action 'win', so
    # the disjoint gate misses it; gate on the 'toss' marker explicitly (run 37).
    if ("toss" in a.actions) != ("toss" in b.actions):
        return _reject("coin-toss vs non-toss contract")
    if (
        a.political_actions
        and b.political_actions
        and a.political_actions.isdisjoint(b.political_actions)
    ):
        return _reject(
            f"political event mismatch: {sorted(a.political_actions)} vs {sorted(b.political_actions)}"
        )
    # One-sided removal wording: "impeached" vs "impeached AND removed from
    # office" are different bars (House vote vs Senate conviction) — surfaced
    # live as a phantom 41c arb signal.
    if ("removal" in a.actions) != ("removal" in b.actions):
        return _reject("outcome-bar mismatch: removal-from-office on one side only")
    # "Emergency" (unscheduled/crisis) is a distinct event-bar from the plain
    # event: "Fed EMERGENCY rate cut before 2027" vs "Fed cuts rates before 2027"
    # diverge — a scheduled cut settles the latter YES but the former NO — so they
    # are different contracts (run 56, surfaced as a ~15c phantom at full scale).
    a_emerg = re.search(r"\bemergency\b", a.raw.lower()) is not None
    b_emerg = re.search(r"\bemergency\b", b.raw.lower()) is not None
    if a_emerg != b_emerg:
        return _reject("event-qualifier mismatch: 'emergency' on one side only")
    if (
        "monetary_policy" in a.actions
        and "monetary_policy" in b.actions
        and a.monetary_direction
        and b.monetary_direction
        and a.monetary_direction.isdisjoint(b.monetary_direction)
    ):
        return _reject(
            f"monetary direction mismatch: {sorted(a.monetary_direction)} vs {sorted(b.monetary_direction)}"
        )
    # Rate LEVEL ("Fed Rate hit 5.0%") vs number-of-CUTS/CHANGES market — a
    # different measurement of the same underlying process, not the same bet.
    if _is_rate_count(a.raw) != _is_rate_count(b.raw):
        return _reject("rate-level vs number-of-changes mismatch")
    # bps bucket boundaries: same direction, non-overlapping magnitude ranges
    # ("25 bps decrease" exact vs "cut more than 25bps"; "50+ bps increase" vs
    # "hike 1-25bps").
    bps_a, bps_b = _bps_bucket(a.raw), _bps_bucket(b.raw)
    if bps_a and bps_b and bps_a[0] and bps_b[0] and bps_a[0] == bps_b[0]:
        _, lo_a, hi_a = bps_a
        _, lo_b, hi_b = bps_b
        if hi_a < lo_b or hi_b < lo_a:
            return _reject(f"bps bucket mismatch: {bps_a} vs {bps_b}")
    # Same actor + same verb, different OBJECT ("Will Trump nationalize
    # elections?" vs "Will Trump nationalize SpaceX?").
    avo_a, avo_b = _actor_verb_object(a.raw), _actor_verb_object(b.raw)
    if (
        avo_a and avo_b
        and avo_a[0] == avo_b[0]
        and avo_a[1] == avo_b[1]
        and avo_a[2].isdisjoint(avo_b[2])
    ):
        return _reject(
            f"different object of '{avo_a[1]}': {sorted(avo_a[2])} vs {sorted(avo_b[2])}"
        )

    # --- ordered head-to-head ("A beat B" vs "B beat A") ----------------------
    if a.beat_order and b.beat_order:
        if set(a.beat_order) == set(b.beat_order) and a.beat_order != b.beat_order:
            return _reject(
                f"reversed head-to-head: {a.beat_order} vs {b.beat_order} (winner/loser swapped)"
            )
        if a.beat_order != b.beat_order:
            return _reject(f"different head-to-head: {a.beat_order} vs {b.beat_order}")
        reasons.append(f"head-to-head aligned: {a.beat_order}")

    # --- time gates -----------------------------------------------------------
    price_market = "$" in a.raw or "$" in b.raw
    if not same_horizon:
        # Weekly-recurring market ("Top ... this week?") vs a market with a
        # materially different close horizon — same subject, different bet.
        if _is_weekly_recurring(a.raw) != _is_weekly_recurring(b.raw):
            return _reject("weekly-recurring vs longer-horizon mismatch")
        if a.years and b.years and a.years.isdisjoint(b.years):
            sports = "sports" in a.domains or "sports" in b.domains
            gap = min(abs(int(x) - int(y)) for x in a.years for y in b.years)
            if not (sports and gap == 1):
                return _reject(f"year mismatch: {sorted(a.years)} vs {sorted(b.years)}")
        if not price_market and a.months and b.months and a.months.isdisjoint(b.months):
            return _reject(f"month mismatch: {sorted(a.months)} vs {sorted(b.months)}")
        if not price_market:
            a_day = {s for s in a.time_scopes if s.startswith("day:")}
            b_day = {s for s in b.time_scopes if s.startswith("day:")}
            a_year_only = not a_day and not any(s.startswith("month:") for s in a.time_scopes) and bool(a.years)
            b_year_only = not b_day and not any(s.startswith("month:") for s in b.time_scopes) and bool(b.years)
            if (a_day and b_year_only) or (b_day and a_year_only):
                return _reject("deadline-scope mismatch: specific date vs calendar year, horizons differ")
            if a_day and b_day and a_day.isdisjoint(b_day):
                return _reject(f"deadline-day mismatch: {sorted(a_day)} vs {sorted(b_day)}")

    # --- numeric range buckets (GDP %, temperature, etc.) ---------------------
    # Two non-overlapping numeric buckets are different contracts ("GDP 2.0-2.5%"
    # vs "GDP 4.6-5.0%"); overlapping buckets stay matched ("78-79F" vs "78 to
    # 79", "2.0-2.5%" vs "2.1-2.5%"). Run 33.
    ra, rb = _num_range(a.raw), _num_range(b.raw)
    # Reject non-overlapping OR merely touching buckets: "1.5-2.0%" and "1.1-1.5%"
    # are ADJACENT outcome buckets (share only the 1.5 boundary), not the same
    # contract. Genuinely overlapping ranges (78-79 vs 78-79; 2.0-2.5 vs 2.1-2.5)
    # stay matched. (run 41)
    if ra and rb and (ra[1] <= rb[0] + 1e-9 or rb[1] <= ra[0] + 1e-9):
        return _reject(f"numeric range mismatch: {ra} vs {rb}")

    # Numeric bucket vs open-ended cumulative threshold: a two-sided range on
    # one side ("90-91F") against an "or below"/"or above" idiom on the OTHER
    # side (and only the other side — a side that is itself a range is not
    # cumulative) is always a different contract (iteration 6; see
    # _is_cumulative_bucket).
    if ra and not rb and _is_cumulative_bucket(b.raw):
        return _reject(f"numeric bucket vs cumulative threshold: {ra} vs open-ended ({b.raw!r})")
    if rb and not ra and _is_cumulative_bucket(a.raw):
        return _reject(f"numeric bucket vs cumulative threshold: open-ended ({a.raw!r}) vs {rb}")

    # Sports "Week N" scheduling bucket: a single week and a multi-week window
    # are different granularity contracts (iteration 6).
    wa, wb = _week_bucket(a.raw), _week_bucket(b.raw)
    if wa and wb and wa != wb:
        return _reject(f"week bucket mismatch: {wa} vs {wb}")

    # Explicit fiscal-period mismatch: an annual reading vs a single quarter
    # ("Negative GDP growth in 2026" vs "...GDP... in Q4 2026") are different
    # contracts when BOTH sides name an explicit period tag (iteration 6).
    fa, fb = _fiscal_period(a.raw), _fiscal_period(b.raw)
    if fa and fb and fa != fb:
        return _reject(f"fiscal period mismatch: {fa} vs {fb}")

    # Negative/contraction bucket vs an explicitly positive numeric bucket or
    # threshold are mutually-exclusive outcomes ("Negative GDP growth" vs "GDP
    # growth 4.6% to 5.0%", or vs "increase by more than 4.0%"). Run 34.
    # (Unambiguous downturn words only — not "decline".) Iteration 6: also
    # checks the one-sided numeric threshold, not just the two-sided range, so
    # "Negative GDP growth" vs "GDP will increase by more than X%" (no range
    # on either side) is caught too.
    _neg = re.compile(r"\b(negative|contraction|recession|shrinks?|shrinking|below zero|sub[- ]?zero)\b")
    neg_a, neg_b = bool(_neg.search(a.raw.lower())), bool(_neg.search(b.raw.lower()))
    if neg_a != neg_b:
        pos_range = rb if neg_a else ra
        other_thr = b.threshold if neg_a else a.threshold
        pos_thr = other_thr[1] if other_thr and other_thr[0] == "up" else None
        pos = pos_range[0] if pos_range else pos_thr
        if pos is not None and pos > 0:
            return _reject("direction mismatch: negative vs positive bucket/threshold")

    # --- threshold & settlement (with inversion detection) --------------------
    if a.threshold and b.threshold:
        if _threshold_equal(a.threshold, b.threshold):
            reasons.append(f"thresholds equal: {a.threshold}")
        else:
            same_level = (
                a.threshold[2] == b.threshold[2]
                and abs(a.threshold[1] - b.threshold[1]) / max(a.threshold[1], b.threshold[1], 1.0) <= 0.001
            )
            complement = same_level and a.threshold[0] != b.threshold[0] and {
                a.settlement, b.settlement
            } == {"touch", "hold"}
            if complement:
                inverted = True
                reasons.append(
                    f"complementary thresholds (inverted): {a.threshold} vs {b.threshold}"
                )
            else:
                return _reject(f"threshold mismatch: {a.threshold} vs {b.threshold}")

    if "$" in a.raw and "$" in b.raw and not inverted:
        if (a.settlement is None) != (b.settlement is None):
            return _reject(
                f"settlement-shape mismatch: {a.settlement or 'point'} vs {b.settlement or 'point'}"
            )

    # --- polarity (banned vs legal) -------------------------------------------
    if a.polarity != b.polarity and not inverted:
        # one side frames the negative state; if entities align this is the
        # antonym-cue inversion (TikTok banned vs operating legally)
        if a.entities & b.entities or _jaccard(a.tokens, b.tokens) >= min_similarity:
            inverted = True
            reasons.append("polarity flip with shared subject (inverted)")

    # Count-rung gate (pass 10): ladder outcomes whose count cutoffs are
    # disjoint are different rungs — "Above 68" vs "Above 66" in the SAME
    # senate-vote ladder reached token similarity 0.75 and was accepted, a
    # v2 false-positive. _numeric_threshold misses these because the bare
    # label carries no count noun, and the $/% threshold gate above doesn't
    # apply. Normalisation mirrors _numeric_threshold's count branch so
    # "more than 84.5" (85) still equals "at least 85" (85).
    ra_ = _rung_cutoffs(a.raw)
    rb_ = _rung_cutoffs(b.raw)
    # Tolerance of 1: "20+" (cutoff 20) and "over 20" (21) are the same rung
    # in venue convention (audited fixture PAIR-049); only a gap of 2+ marks
    # adjacent rungs ("Above 68" vs "Above 66").
    if ra_ and rb_ and min(abs(x - y) for x in ra_ for y in rb_) > 1:
        return _reject(f"rung mismatch: {sorted(ra_)} vs {sorted(rb_)}")

    # --- acceptance -------------------------------------------------------------
    sim = _jaccard(a.tokens, b.tokens)
    # Identical DISTINCTIVE outcome label is sufficient acceptance evidence
    # (pass 10, extending pass 8's philosophy that an exact label match
    # outranks name/person heuristics): "Gary" vs "Gary (Stephen Wilson Jr.)"
    # scored 0.29 because the Kalshi question text dominates the token sets,
    # but the label IS the contract — a disambiguating parenthetical is not
    # part of it. Every hard gate above (threshold, rung, range, week, fiscal,
    # settlement-source, time-scope) has already passed. The >= 4 bar keeps
    # generic "Yes"/"No" labels from accepting on identity alone.
    if not inverted and same_outcome:
        la = re.sub(r"[^a-z0-9]", "", _ascii_lower(re.sub(r"\(.*?\)", "", a.outcome_label)))
        if len(la) >= 4:
            reasons.append(f"identical outcome label {la!r}")
            return MatchDecision(True, inverted, max(sim, 0.5), reasons)
    if sim >= min_similarity:
        reasons.append(f"token similarity {sim:.2f} >= {min_similarity}")
        return MatchDecision(True, inverted, sim, reasons)

    # threshold-led bridge: equal strike + shared entity + same horizon
    if (
        a.threshold
        and b.threshold
        and (inverted or _threshold_equal(a.threshold, b.threshold))
        and (a.entities & b.entities)
        and same_horizon
    ):
        reasons.append(
            f"threshold-led bridge: shared entity {sorted(a.entities & b.entities)}, sim {sim:.2f}"
        )
        return MatchDecision(True, inverted, max(sim, 0.5), reasons)

    # proper-noun bridge: a shared MULTI-TOKEN named entity (a bill/act name) plus
    # same horizon justifies a modestly lower similarity bar. Recovers the "short
    # bill name vs verbose legal description" class (run 53): Polymarket "Housing
    # for the 21st Century Act" vs Kalshi's 40-word description that repeats the Act
    # name — identical contract, but the boilerplate drags token similarity to 0.29.
    # The shared name must be 2+ tokens, so generic boilerplate ("become law before
    # 2027") and single common words cannot trigger it, and DIFFERENT bills (whose
    # distinctive names differ) never share one. sim>=0.25 keeps it a modest relax.
    shared_named = {n for n in (a.selected_names & b.selected_names) if len(n.split()) >= 2}
    if shared_named and (a.entities & b.entities) and same_horizon and sim >= 0.25:
        reasons.append(
            f"proper-noun bridge: shared name {sorted(shared_named)}, sim {sim:.2f}"
        )
        return MatchDecision(True, inverted, max(sim, 0.5), reasons)

    # Single distinctive-name bridge: recovers single-word club/team names
    # ("Leverkusen", via matcher._bare_subject_name's _winner_subject
    # fallback) that don't produce an EXACT shared multi-token string with the
    # proper-noun bridge above (poly's "Bayer Leverkusen" vs Kalshi's bare
    # "Leverkusen" share no literal string, only a substring relationship).
    # bare_subject_names is intentionally NOT part of selected_names (kept out
    # of the hard mismatch veto above — a bare single token collides too
    # easily with an unrelated multi-word name glommed from an event-title
    # field); it is used here, acceptance-only.
    #
    # The overlap must run THROUGH a bare_subject_name specifically (not just
    # be present somewhere on that side) — requiring only "a.bare_subject_names
    # or b.bare_subject_names non-empty, then check ANY overlap between the
    # unioned sets" let an unrelated bare name (e.g. "team", from "MLB: Team
    # to win 100+ games") make a pair "eligible" for a bridge whose actual
    # overlap was really just two DIFFERENT LA teams sharing the bare
    # jurisdiction name "Los Angeles" ("Los Angeles Dodgers" vs "Los Angeles
    # FC") — a real bug caught while validating this bridge on the iter4 live
    # sample. Requiring the bare token itself to participate closes that gap.
    other_a = set(a.selected_names) | set(a.bare_subject_names)
    other_b = set(b.selected_names) | set(b.bare_subject_names)
    bare_bridges = (
        (set(a.bare_subject_names) and _names_overlap(set(a.bare_subject_names), other_b))
        or (set(b.bare_subject_names) and _names_overlap(set(b.bare_subject_names), other_a))
    )
    if bare_bridges and same_horizon and sim >= 0.15:
        reasons.append(
            f"single-entity bridge: {sorted(other_a)} vs {sorted(other_b)}, sim {sim:.2f}"
        )
        return MatchDecision(True, inverted, max(sim, 0.5), reasons)

    return MatchDecision(False, False, sim, reasons + [f"similarity {sim:.2f} below gate"])


def explain(poly: "MarketSnapshot", kalshi: "MarketSnapshot") -> MatchDecision:
    """One-call diagnostic: extract both specs and compare with reasons."""
    pe = _squash((getattr(poly, "extra", {}) or {}).get("event_title") or "")
    ke = _squash((getattr(kalshi, "extra", {}) or {}).get("event_title") or "")
    return match_spec(extract_spec(poly), extract_spec(kalshi),
                      same_event=bool(pe and ke and pe == ke))

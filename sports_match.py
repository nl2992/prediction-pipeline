"""
Structured sports-game matcher (Kalshi game markets <-> Polymarket moneylines).

Why a separate matcher
----------------------
The text matcher cannot pair game markets: Kalshi says "Denver wins" /
"Kansas City wins" while Polymarket says "Broncos vs. Chiefs" (outcomes
["Broncos", "Chiefs"]) — the titles share no tokens. Both venues, however,
encode the game structurally:

    Kalshi      KXNFLGAME-26SEP14DENKC            markets  ...-DEN / ...-KC
                KXMLBGAME-26SEP162140MIAAZ        (optional HHMM start, ET)
    Polymarket  nfl-den-kc-2026-09-15             market gameStartTime (UTC)
                epl-ful-mun-2026-09-20-{ful,draw,mun}   (soccer: 3 YES/NO markets)

Join key = (team set, start time). Safeguards, each motivated by a failure seen
in the live probe (docs/EXPANSION_PROPOSAL.md §2a):

* start time from PM ``gameStartTime`` — slug dates go stale when games are
  rescheduled (mls-sea-rsl-2026-04-12 starting 2026-09-24);
* doubleheaders / same teams on consecutive days: a join must be UNIQUE within
  the time window, otherwise it is skipped (precision over recall);
* league namespace: team codes repeat across leagues (MLS hou/cin vs NFL), so
  the Kalshi series -> Polymarket slug-prefix mapping is learned by majority vote
  over unambiguous joins and dissenting joins are dropped;
* outcome shape must agree: a Kalshi game WITH a tie market pairs only with a
  3-way (draw) Polymarket game and vice versa, so a regulation-time contract is
  never paired with one that includes extra time;
* outcome mapping by code (slug order / market slug suffix), with a
  name-token fallback only when codes differ.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - tzdata missing
    _ET = timezone(timedelta(hours=-4))

_MONTHS = {m: i + 1 for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_K_GAME_SERIES_RE = re.compile(r"(GAME|MATCH)$")
_K_EVENT_RE = re.compile(r"^[A-Z0-9]+-(\d\d)([A-Z]{3})(\d\d)(\d{4})?")
_PM_GAME_SLUG_RE = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)-([a-z0-9]+)-\d{4}-\d\d-\d\d$")
_TIE_CODES = {"tie", "draw"}

# Polymarket sometimes splits one game's markets across several EVENTS: the
# moneyline lives in the base event ("mls-vwh-dcu-2026-09-26") while extra
# lines live in sibling events named "<base slug>-<suffix>", which
# _PM_GAME_SLUG_RE rejects outright because the suffix runs past the date.
# Measured on a frozen full-catalog snapshot, these are the sibling suffixes
# whose contract classes _PM_LINE_CLASSES already knows how to match:
#   -more-markets        29,855 markets across 545 games (spreads, totals,
#                         soccer team totals, 1H/2H totals -- all classes
#                         _PM_LINE_CLASSES already handles); of those, 97
#                         games / 5,762 markets have a base game ALREADY
#                         joined to Kalshi, so those lines are unlocked as
#                         soon as the sibling is merged into the base game.
#   -halftime-result      1,846 markets (soccer_halftime_result)
#   -second-half-result   1,886 markets (soccer_second_half_result)
# Excluded on purpose: "-total-corners" (26,337 markets) and "-exact-score"
# (10,301 markets) have no Kalshi counterpart at all -- merging them would
# just bloat _PGame.lines with markets _match_lines can never pair.
_PM_SIBLING_SUFFIXES = frozenset({"more-markets", "halftime-result", "second-half-result"})
_PM_SIBLING_SLUG_RE = re.compile(
    r"^(.+)-(?:" + "|".join(re.escape(s) for s in _PM_SIBLING_SUFFIXES) + r")$")

# A Kalshi ticker with an HHMM start must be within this of PM gameStartTime;
# date-only tickers fall back to "same ET calendar day, +-1".
_TIME_TOLERANCE = timedelta(hours=3)


# Club boilerplate that one venue includes and the other omits ("FC Porto" /
# "Porto", "HC Pardubice" / "Dynamo Pardubice").
_GENERIC_NAME_TOKENS = frozenset({
    "fc", "sc", "cf", "hc", "sk", "fk", "cd", "ca", "ac", "afc", "sv", "vv", "rc",
    "cs", "bc", "bk", "ad", "club", "de", "la", "el", "the", "da", "do", "e", "y",
    # Swiss hockey club prefix: Kalshi "EHC Kloten" vs Polymarket "Kloten Flyers".
    "ehc",
})
# English/local spellings and abbreviations seen across the two venues.
_NAME_ALIASES = {
    "saint": "st", "utd": "united", "munich": "munchen", "prague": "praha",
    "as": "athletics",  # Kalshi "A's" (apostrophe stripped) vs PM "Athletics"
    # Swiss NL: Polymarket's "SCL Tigers" is SC Langnau Tigers.
    "scl": "langnau",
}
_FOLD = str.maketrans({"ø": "o", "æ": "ae", "ß": "ss", "đ": "d", "ł": "l", "ı": "i"})

# Kalshi's NFL yes_sub_title is inconsistent: some games use the nickname
# ("ATL Falcons", "DEN Broncos" -- ordinary _names_agree handles these), others
# use "<City words> <first letter of nickname>" ("New York G", "Los Angeles R").
# Measured: KXNFLGAME-26SEP21NYGLAR carries "New York G" / "Los Angeles R" for
# a Giants/Rams game, which shares no token with either nickname. The trailing
# letter is a real discriminator (G is Giants, not Jets) so it must be checked,
# not stripped.
#
# The city part is restricted to plain letter words (no "/", no "vs") as a
# precaution: on the live Kalshi catalog the bare shape "^.+\s[A-Z]$" also
# matches 211 unrelated titles (musicians "Lil Nas X", ballot measures
# "Amendment I", combined matchup strings "Denver vs Los Angeles C"). None of
# those can reach this function today -- only GAME/MATCH-series team names
# ever flow into _names_agree -- so this is defence in depth, not a fix for
# an observed failure, but it costs nothing to rule out the "vs"/"/" shapes.
_K_CITY_LETTER_RE = re.compile(r"^([A-Za-z]+(?:\s[A-Za-z]+)*)\s([A-Z])$")


def _city_letter_agrees(k_name: str, pm_name: str) -> bool:
    """"New York G" ~ "Giants" (nickname starts with G), but NOT ~ "Jets".
    Checked against every Polymarket token, not just the last, because the
    discriminating letter is sometimes the FIRST word of a multi-word
    nickname: Kalshi "Boston R" ~ PM "Red Sox" (R is "Red", not "Sox").

    Only checked as a fallback after the ordinary token match fails, and only
    against the Kalshi side (Polymarket never uses this shorthand). Callers
    that build a code map (``_code_map``) must additionally not let this
    create ambiguity against a name that already matched strictly -- see the
    two-pass fallback there, motivated by a measured false positive where the
    letter rule matched Kalshi "Los Angeles A" (the Angels) against PM
    "Athletics" and orphaned the whole Angels/Athletics game."""
    m = _K_CITY_LETTER_RE.match((k_name or "").strip())
    # "/" is already excluded by the regex's letters-and-spaces city group;
    # "vs" is plain letters, so it needs an explicit check.
    if not m or "vs" in m.group(1).lower().split():
        return False
    letter = m.group(2).lower()
    return any(t.startswith(letter) for t in _norm_tokens(pm_name))


def _norm_tokens(name: str) -> list[str]:
    import unicodedata
    n = (name or "").lower().translate(_FOLD)
    n = re.sub(r"^\s*reg(?:ulation)? time:\s*", "", n)          # Kalshi cup prefix
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    n = re.sub(r"[.'’]", "", n)                                    # "D.C." -> "dc"
    toks = [t for t in re.split(r"[^a-z0-9]+", n) if t]
    toks = [_NAME_ALIASES.get(t, t) for t in toks]
    return [t for t in toks if t not in _GENERIC_NAME_TOKENS] or toks


def _token_agrees(s: str, longer: list[str]) -> bool:
    for w in longer:
        if w.startswith(s) or (len(w) >= 2 and s.startswith(w)):
            return True
    # initials of consecutive words: "rb" ~ "red bulls", "se" ~ "south east"
    if 2 <= len(s) <= 4:
        for i in range(len(longer) - len(s) + 1):
            if "".join(w[0] for w in longer[i:i + len(s)]) == s:
                return True
    return False


def _names_agree_strict(k_name: str, pm_name: str) -> bool:
    """Every token of the SHORTER normalised name agrees with the longer one
    (prefix either way, or initials): "Los Angeles R" ~ "Los Angeles Rams",
    "Nippon Ham Fighters" ~ "Hokkaido Nippon-Ham Fighters", "New York G" !~
    "New York Jets". Ambiguity (both teams agreeing) is rejected by callers.

    This is the token-only half of ``_names_agree``, split out so
    ``_code_map`` can run it as a first, higher-confidence pass before ever
    trying the letter-shorthand fallback (see ``_city_letter_agrees``'s
    docstring for why: the letter rule alone can manufacture ambiguity
    against a name that already matches here)."""
    kt, pt = _norm_tokens(k_name), _norm_tokens(pm_name)
    if not kt or not pt:
        return False
    short, longer = (kt, pt) if len(kt) <= len(pt) else (pt, kt)
    return all(_token_agrees(t, longer) for t in short)


def _names_agree(k_name: str, pm_name: str) -> bool:
    """``_names_agree_strict`` plus the "<City> <Letter>" shorthand fallback.
    Safe for callers (like ``_subject_agrees``) that test one fixed pair of
    names rather than resolving a whole game's code map."""
    return _names_agree_strict(k_name, pm_name) or _city_letter_agrees(k_name, pm_name)


def _parse_pm_time(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace(" ", "T", 1).replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class _KGame:
    def __init__(self, event_ticker: str, series: str, markets: list):
        self.event_ticker = event_ticker
        self.series = series
        self.by_code = {m.market_id.rsplit("-", 1)[-1].lower(): m for m in markets}
        self.has_tie = any(c in _TIE_CODES for c in self.by_code)
        self.teams = frozenset(c for c in self.by_code if c not in _TIE_CODES)
        m = _K_EVENT_RE.match(event_ticker)
        y, mon, d, hhmm = 2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)), m.group(4)
        if hhmm:
            self.start = datetime(y, mon, d, int(hhmm[:2]), int(hhmm[2:]), tzinfo=_ET)
            self.day = None
        else:
            self.start = None
            self.day = datetime(y, mon, d).date()

    def name(self, code: str) -> str:
        m = self.by_code.get(code)
        return (m.extra.get("yes_sub_title") or "") if m else ""

    def time_ok(self, t: datetime) -> bool:
        if self.start is not None:
            return abs(t - self.start) <= _TIME_TOLERANCE
        return abs((t.astimezone(_ET).date() - self.day).days) <= 1


class _PGame:
    def __init__(self, slug: str, prefix: str, codes: tuple[str, str], markets: list):
        self.slug, self.prefix, self.codes = slug, prefix, codes
        self.two_way = None       # the single 2-outcome moneyline market, if any
        self.three_way: dict = {}  # code/"tie" -> YES/NO moneyline market
        self.start = None
        # PM sportsMarketType -> [(subject or None, line, snapshot)]; YES is the
        # named team covering, or "Over".
        self.lines: dict = {}
        for s in markets:
            ex = s.extra
            mtype = ex.get("sports_market_type")
            if mtype in _PM_LINE_CLASSES:
                self.start = self.start or _parse_pm_time(ex.get("game_start_time"))
                outs = ex.get("outcome_labels") or []
                line = _line_value(s)
                if line is None and _PM_LINE_CLASSES[mtype][1] != "team_noline":
                    continue
                kind = _PM_LINE_CLASSES[mtype][1]
                if kind == "team_noline":
                    # "Seattle Sounders FC" (groupItemTitle) + Yes/No outcomes.
                    if [o.lower() for o in outs] == ["yes", "no"] and (s.title or "").strip():
                        self.lines.setdefault(mtype, []).append((s.title.strip(), None, s))
                    continue
                if kind == "team_side" and len(outs) == 2:
                    # "Spread: Rays (-1.5)" with outcomes [Rays, Yankees]:
                    # token 0 wins if that team covers the line.
                    self.lines.setdefault(mtype, []).append((outs[0], line, s))
                elif kind in ("over_under", "team_ou", "player_ou") \
                        and [o.lower() for o in outs] == ["over", "under"]:
                    subject = _pm_line_subject(s, kind)
                    self.lines.setdefault(mtype, []).append((subject, line, s))
                continue
            if mtype != "moneyline":
                continue
            self.start = self.start or _parse_pm_time(ex.get("game_start_time"))
            outs = ex.get("outcome_labels") or []
            if len(outs) == 2 and [o.lower() for o in outs] != ["yes", "no"]:
                self.two_way = s
            else:
                suffix = (ex.get("market_slug") or "").rsplit("-", 1)[-1]
                code = "tie" if suffix in _TIE_CODES else suffix
                if code in codes or code == "tie":
                    self.three_way[code] = s

    @property
    def teams(self) -> frozenset:
        return frozenset(self.codes)


_K_EVENT_KIND_RE = re.compile(r"^([A-Z0-9]+?)(GAME|MATCH|SPREAD|TOTAL)-(.+)$")

# Polymarket contract class -> (Kalshi series suffix, subject kind).
# Both venues list these per game, so they attach to a game key the moneyline
# join has already verified. A class is listed ONLY when both venues resolve the
# same thing; Kalshi's KXNFLREC/KXMLBSB and Polymarket's corner/exact-score
# markets have no counterpart on the other side and stay unmatched by design.
_PM_LINE_CLASSES = {
    "spreads":                        ("SPREAD",     "team_side"),
    "totals":                         ("TOTAL",      "over_under"),
    "first_half_spreads":             ("1HSPREAD",   "team_side"),
    "second_half_spreads":            ("2HSPREAD",   "team_side"),
    "first_half_totals":              ("1HTOTAL",    "over_under"),
    "second_half_totals":             ("2HTOTAL",    "over_under"),
    "team_totals":                    ("TEAMTOTAL",  "team_ou"),
    "first_half_team_totals":         ("1HTEAMTOTAL", "team_ou"),
    "second_half_team_totals":        ("2HTEAMTOTAL", "team_ou"),
    # Soccer halves: Polymarket asks per team ("Seattle leading at halftime",
    # Yes/No), which is exactly Kalshi's "Seattle wins 1st half". Football and
    # basketball halves are NOT here: every Kalshi half-winner event carries a
    # TIE leg while Polymarket's 1H/2H moneyline is 2-way, so a draw settles
    # differently — the same shape mismatch the game join refuses.
    "soccer_halftime_result":         ("1H",         "team_noline"),
    "soccer_second_half_result":      ("2H",         "team_noline"),
    "baseball_player_hits_runs_rbis": ("HRR",        "player_ou"),
    "baseball_player_home_runs":      ("HR",         "player_ou"),
}

# Kalshi phrasings of the subject, per kind:
#   team_side  "LA Rams wins 1H by over 20.5 points"   -> LA Rams
#   team_ou    "Baltimore over 1.5 runs scored"        -> Baltimore
#   player_ou  "Ben Rice: 1+"                          -> Ben Rice
_K_SUBJECT_RE = {
    "team_side": re.compile(r"^(?P<s>.+?)\s+wins?\b", re.I),
    "team_noline": re.compile(r"^(?P<s>.+?)\s+wins?\b", re.I),
    "team_ou": re.compile(r"^(?P<s>.+?)\s+(?:over|under)\b", re.I),
    "player_ou": re.compile(r"^(?P<s>[^:]+):", re.I),
}


def _line_value(snap) -> float | None:
    """Polymarket's numeric line. The market slug encodes it unambiguously
    ("…-total-4pt5", "…-hrr-drake-baldwin-0pt5"); the title ("Spread -1.5",
    "O/U 7.5") is the fallback, because titles sometimes fall back to the
    whole question."""
    slug = (snap.extra or {}).get("market_slug") or ""
    m = re.search(r"-(\d+)pt(\d+)\b", slug)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"-(?:total|spread)(?:-(?:away|home))?-(\d+)\b", slug)
    if m:
        return float(m.group(1))
    m = re.search(r"(-?\d+(?:\.\d+)?)", snap.title or "")
    return abs(float(m.group(1))) if m else None


def _pm_line_subject(snap, kind: str) -> str | None:
    """Team or player a Polymarket O/U market is about: "Panthers O/U 10.5",
    "Drake Baldwin: Hits + Runs + RBIs O/U 0.5"."""
    title = snap.title or ""
    if kind == "team_ou":
        subj = re.split(r"\s+O/U\b", title, maxsplit=1)[0]
        # "Broncos 1H O/U 6.5" -> "Broncos"
        return re.sub(r"\s+(?:1H|2H|1st Half|2nd Half)$", "", subj.strip(), flags=re.I) or None
    if kind == "player_ou":
        return title.split(":", 1)[0].strip() or None
    return None


def _k_line_value(snap) -> float | None:
    """Kalshi's line: the strike ("1+ hits + runs + RBIs" carries floor 0.5,
    which is Polymarket's "O/U 0.5"), falling back to the wording."""
    strike = (snap.extra or {}).get("floor_strike")
    if strike is None:
        strike = (snap.extra or {}).get("cap_strike")
    if strike is not None:
        try:
            return float(strike)
        except (TypeError, ValueError):
            pass
    m = re.search(r"\b(?:over|more than)\s+([\d.]+)", snap.title or "", re.I)
    return float(m.group(1)) if m else None


def _k_line_events(k_snaps) -> dict:
    """Kalshi line/prop markets grouped by their own event ticker.

    Kalshi lists each class as its own event sharing the game key
    (KXMLSGAME-26SEP26VANDCU / KXMLSSPREAD-… / KXNFL1HTOTAL-…), so an exact
    event-ticker lookup keeps neighbouring classes apart.
    """
    out: dict = defaultdict(list)
    for s in k_snaps:
        if s.event_id:
            out[s.event_id].append(s)
    return out


def _team_equivalences(kg: "_KGame", pg: _PGame, cmap: dict) -> list:
    """(kalshi name, polymarket name) pairs the GAME join already verified, so a
    line market can say "Denver" on one venue and "Broncos" on the other."""
    out = []
    pm_names: dict = {}
    if pg.two_way is not None:
        outs = pg.two_way.extra.get("outcome_labels") or []
        pm_names = dict(zip(pg.codes, outs, strict=False))
    else:
        pm_names = {c: s.title for c, s in pg.three_way.items() if c != "tie"}
    for p_code, k_code in (cmap or {}).items():
        k_name, p_name = kg.name(k_code), pm_names.get(p_code)
        if k_name and p_name:
            out.append((k_name, p_name))
    return out


def _subject_agrees(k_subject: str, p_subject: str, equivalences: list) -> bool:
    if _names_agree(k_subject, p_subject):
        return True
    for k_name, p_name in equivalences:
        if _names_agree(k_subject, k_name) and _names_agree(p_subject, p_name):
            return True
    return False


def _match_lines(prefix: str, suffix: str, k_by_event: dict, pg: _PGame,
                 kg: "_KGame" = None, cmap: dict = None) -> list:
    """Pair every line/prop class for one already-verified game."""
    from matcher import MatchedPair, _close_delta_hours

    equivalences = _team_equivalences(kg, pg, cmap) if kg is not None else []
    pairs: list = []
    used_p: set[str] = set()
    for mtype, (k_suffix, kind) in _PM_LINE_CLASSES.items():
        p_entries = pg.lines.get(mtype) or []
        k_markets = k_by_event.get(f"{prefix}{k_suffix}-{suffix}") or []
        if not p_entries or not k_markets:
            continue
        reason = f"sports line key {prefix}{k_suffix}-{suffix} <-> {pg.slug}"
        for ks in k_markets:
            if kind == "team_noline" and (ks.market_id or "").upper().endswith("-TIE"):
                continue          # Kalshi's draw leg has no Polymarket counterpart
            k_line = None if kind == "team_noline" else _k_line_value(ks)
            if k_line is None and kind != "team_noline":
                continue
            k_subject = None
            if kind != "over_under":
                m = _K_SUBJECT_RE[kind].match((ks.extra or {}).get("yes_sub_title") or ks.title or "")
                if not m:
                    continue
                k_subject = m.group("s")
            for p_subject, p_line, ps in p_entries:
                if ps.market_id in used_p or (kind != "team_noline" and p_line != k_line):
                    continue
                if k_subject is not None and not (
                        p_subject and _subject_agrees(k_subject, p_subject, equivalences)):
                    continue
                used_p.add(ps.market_id)
                pairs.append(MatchedPair(
                    poly=ps, kalshi=ks, title_similarity=1.0,
                    close_delta_hours=_close_delta_hours(ps.close_time, ks.close_time),
                    confidence=0.98, match_source="sports", match_reason=reason))
                break
    return pairs


def _k_games(k_snaps) -> list[_KGame]:
    by_event: dict[str, list] = defaultdict(list)
    for s in k_snaps:
        by_event[s.event_id].append(s)
    games = []
    for et, snaps in by_event.items():
        series = snaps[0].extra.get("series_ticker") or et.split("-")[0]
        if not _K_GAME_SERIES_RE.search(series) or not _K_EVENT_RE.match(et):
            continue
        g = _KGame(et, series, snaps)
        if len(g.teams) == 2:
            games.append(g)
    return games


def _p_games(p_snaps) -> list[_PGame]:
    # Group by BASE game slug, not raw event_id: see _PM_SIBLING_SUFFIXES.
    # A snapshot's own event_id is left untouched (other code uses it for
    # pair identity) -- only this grouping key changes, and _PGame.slug is
    # built from the BASE slug so match_reason and pass 3's one-game-per-PM-
    # game dedupe keep working exactly as before.
    by_base: dict[str, list] = defaultdict(list)
    base_match: dict[str, re.Match] = {}
    for s in p_snaps:
        slug = s.event_id or ""
        m = _PM_GAME_SLUG_RE.match(slug)
        if m:
            by_base[slug].append(s)
            base_match.setdefault(slug, m)
            continue
        sm = _PM_SIBLING_SLUG_RE.match(slug)
        if not sm:
            continue
        base_slug = sm.group(1)
        bm = _PM_GAME_SLUG_RE.match(base_slug)
        if not bm:
            continue
        by_base[base_slug].append(s)
        base_match.setdefault(base_slug, bm)

    games = []
    for base_slug, snaps in by_base.items():
        m = base_match[base_slug]
        # A sibling's markets can list the same contract as the base event
        # (or, less commonly, two siblings could overlap); de-dup by
        # market_id before building the game so _PGame.lines never carries
        # the same market twice.
        seen: set[str] = set()
        deduped = []
        for s in snaps:
            if s.market_id in seen:
                continue
            seen.add(s.market_id)
            deduped.append(s)
        # A sibling can exist with no base event in the snapshot set (e.g. the
        # base game already closed/rolled off). We don't special-case that: a
        # sibling-only group has no moneyline/three-way market, so g.start
        # (possibly set from a line market's game_start_time) is not enough --
        # the guard below still drops it, correctly, since there's no
        # moneyline for the Kalshi join to anchor on.
        g = _PGame(base_slug, m.group(1), (m.group(2), m.group(3)), deduped)
        if g.start is not None and (g.two_way is not None or g.three_way):
            games.append(g)
    return games


def _code_prefix_bridge(kg_teams: frozenset, pg_codes: tuple) -> dict[str, str] | None:
    """Map PM code -> Kalshi code when one venue's code is a prefix of the
    other's for exactly one team of the pair, e.g. PM "la" / Kalshi "lar" for
    the Rams (measured: nfl-nyg-la-2026-09-22 vs KXNFLGAME-26SEP21NYGLAR).

    A bare prefix rule would be unsafe globally -- "la" alone is ambiguous
    across leagues and franchises (Lakers/Angels/Chargers/Rams) -- so this
    only fires within one already-candidate game, and only when the OTHER
    team's code matches exactly between the two venues. That exact match is
    what pins the game (and hence the league/franchise) down; the prefix
    relation on the remaining code is then safe to accept.
    """
    if kg_teams == frozenset(pg_codes):
        return {c: c for c in pg_codes}
    if len(pg_codes) != 2:
        return None
    a, b = pg_codes
    for exact_pm, other_pm in ((a, b), (b, a)):
        if exact_pm not in kg_teams:
            continue
        remaining = kg_teams - {exact_pm}
        if len(remaining) != 1:
            continue
        kc = next(iter(remaining))
        if len(kc) < 2 or len(other_pm) < 2:
            continue
        if kc.startswith(other_pm) or other_pm.startswith(kc):
            return {exact_pm: exact_pm, other_pm: kc}
    return None


def _code_map(kg: _KGame, pg: _PGame) -> dict[str, str] | None:
    """Map PM team code -> Kalshi team code, by code equality, the code-prefix
    bridge (one venue's code short by a suffix, other team pinned exactly), or
    names."""
    bridged = _code_prefix_bridge(kg.teams, pg.codes)
    if bridged:
        return bridged
    # Name fallback: PM team names from 2-way outcomes or 3-way market titles.
    names: dict[str, str] = {}
    if pg.two_way is not None:
        outs = pg.two_way.extra.get("outcome_labels") or []
        names = dict(zip(pg.codes, outs, strict=False))
    else:
        names = {c: s.title for c, s in pg.three_way.items() if c != "tie"}
    if len(names) != 2:
        return None
    # Two passes, strict token agreement before the letter-shorthand fallback.
    # Measured failure with a single combined pass: PM "Athletics" matched
    # Kalshi "Los Angeles A" (the Angels) via the letter rule ('a' ~
    # "athletics"[0]) as well as "A's" (the real Athletics) via strict tokens,
    # producing 2 hits -> the whole Angels/Athletics game was dropped. Running
    # strict agreement for every name FIRST, and only sending names with ZERO
    # strict hits into the letter fallback -- against Kalshi codes the strict
    # pass hasn't already claimed -- keeps "Athletics" pinned to 'ath' while
    # still letting a bare "Rams" get rescued against "Los Angeles R".
    mapping: dict[str, str] = {}
    used_k: set[str] = set()
    leftover: list[tuple[str, str]] = []
    for pc, pname in names.items():
        hits = [kc for kc in kg.teams if _names_agree_strict(kg.name(kc), pname)]
        if len(hits) == 1:
            mapping[pc] = hits[0]
            used_k.add(hits[0])
        elif len(hits) == 0:
            leftover.append((pc, pname))
        else:
            return None  # genuine strict ambiguity: not rescuable
    for pc, pname in leftover:
        remaining = kg.teams - used_k
        hits = [kc for kc in remaining if _city_letter_agrees(kg.name(kc), pname)]
        if len(hits) != 1:
            return None
        mapping[pc] = hits[0]
        used_k.add(hits[0])
    return mapping if len(set(mapping.values())) == 2 else None


def match_sports_games(k_snaps, p_snaps) -> list:
    """Return MatchedPair objects (``match_source="sports"``) for game moneylines."""
    from matcher import MatchedPair, _close_delta_hours

    kgs, pgs = _k_games(k_snaps), _p_games(p_snaps)
    k_lines = _k_line_events(k_snaps)
    by_teams: dict[frozenset, list[_PGame]] = defaultdict(list)
    by_day: dict = defaultdict(list)
    for pg in pgs:
        by_teams[pg.teams].append(pg)
        by_day[pg.start.astimezone(_ET).date()].append(pg)

    # Pass 1: candidate joins (unique within the time window, same outcome shape).
    # ``exact`` marks joins found by identical team codes (not the name fallback)
    # — they carry their own evidence and must not be dropped by the league
    # majority vote below (pass 10: a county one-day cup game legitimately
    # lives in Kalshi's KXODIMATCH series alongside internationals, and the
    # vote tie was dropping it).
    joins: list[tuple[_KGame, _PGame, dict, bool]] = []
    for kg in kgs:
        cands = [pg for pg in by_teams.get(kg.teams, ()) if kg.time_ok(pg.start)]
        exact = bool(cands)
        if not cands:  # name fallback: any PM game near the same time
            day = (kg.start.date() if kg.start else kg.day)
            pool = [pg for d in (day - timedelta(days=1), day, day + timedelta(days=1))
                    for pg in by_day.get(d, ()) if kg.time_ok(pg.start)]
            cands = [pg for pg in pool if _code_map(kg, pg)]
        cands = [pg for pg in cands if (pg.two_way is None) == kg.has_tie]
        if len(cands) != 1:
            continue
        cmap = _code_map(kg, cands[0])
        if cmap:
            joins.append((kg, cands[0], cmap, exact))

    # Pass 2: league namespace — drop joins dissenting from their series' majority.
    # A join at the TOP of its series' vote (leader or tied leader) is kept;
    # exact-code joins additionally survive a tie (pass 10: a county one-day
    # cup game legitimately lives in Kalshi's KXODIMATCH series alongside
    # internationals, and the arbitrary tie-break was dropping it). Fuzzy
    # name-fallback joins keep the strict unique-majority rule — that path is
    # where cross-league phantom joins (MLS hou/cin vs NFL hou/cin) occur, and
    # an exact-code join in the MINORITY is still dropped (a shared team-code
    # pair across leagues within the time window).
    votes: dict[str, Counter] = defaultdict(Counter)
    for kg, pg, _, _ in joins:
        votes[kg.series][pg.prefix] += 1
    kept: list[tuple[_KGame, _PGame, dict]] = []
    for kg, pg, cm, exact in joins:
        v = votes[kg.series]
        top = v.most_common(2)
        is_leader = v[pg.prefix] == top[0][1]
        is_tie = len(top) > 1 and top[0][1] == top[1][1]
        if is_leader and (exact or not is_tie):
            kept.append((kg, pg, cm))
    joins = kept

    # Pass 3: one PM game per Kalshi game and vice versa; emit contract pairs.
    pairs, used_p = [], set()
    for kg, pg, cmap in joins:
        if pg.slug in used_p:
            continue
        used_p.add(pg.slug)
        if pg.two_way is not None:
            # One market; its token 0 is the FIRST slug team. PM "NO" is the
            # other team, so one pair covers both arb directions.
            first = pg.codes[0]
            ks = kg.by_code.get(cmap[first])
            ps = pg.two_way
            outs = ps.extra.get("outcome_labels") or []
            if ks is None or len(outs) != 2:
                continue
            ps.title = f"{outs[0]} (vs {outs[1]})"
            ps.extra["yes_outcome"] = outs[0]
            legs = [(ps, ks)]
        else:
            legs = []
            for code, ps in pg.three_way.items():
                kcode = "tie" if code == "tie" else cmap.get(code)
                ks = kg.by_code.get(kcode) or (kg.by_code.get("draw") if code == "tie" else None)
                if ks is not None:
                    legs.append((ps, ks))
        for ps, ks in legs:
            pairs.append(MatchedPair(
                poly=ps, kalshi=ks, title_similarity=1.0,
                close_delta_hours=_close_delta_hours(ps.close_time, ks.close_time),
                confidence=0.99, match_source="sports",
                match_reason=f"sports key {kg.event_ticker} <-> {pg.slug}",
            ))
        # Spreads and totals ride on the game key this join just verified, so
        # they inherit its team mapping, league vote and time check.
        km = _K_EVENT_KIND_RE.match(kg.event_ticker)
        if km:
            pairs.extend(_match_lines(km.group(1), km.group(3), k_lines, pg, kg, cmap))
    return pairs

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

# A Kalshi ticker with an HHMM start must be within this of PM gameStartTime;
# date-only tickers fall back to "same ET calendar day, +-1".
_TIME_TOLERANCE = timedelta(hours=3)


# Club boilerplate that one venue includes and the other omits ("FC Porto" /
# "Porto", "HC Pardubice" / "Dynamo Pardubice").
_GENERIC_NAME_TOKENS = frozenset({
    "fc", "sc", "cf", "hc", "sk", "fk", "cd", "ca", "ac", "afc", "sv", "vv", "rc",
    "cs", "bc", "bk", "ad", "club", "de", "la", "el", "the", "da", "do", "e", "y",
})
# English/local spellings and abbreviations seen across the two venues.
_NAME_ALIASES = {
    "saint": "st", "utd": "united", "munich": "munchen", "prague": "praha",
    "as": "athletics",  # Kalshi "A's" (apostrophe stripped) vs PM "Athletics"
}
_FOLD = str.maketrans({"ø": "o", "æ": "ae", "ß": "ss", "đ": "d", "ł": "l", "ı": "i"})


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


def _names_agree(k_name: str, pm_name: str) -> bool:
    """Every token of the SHORTER normalised name agrees with the longer one
    (prefix either way, or initials): "Los Angeles R" ~ "Los Angeles Rams",
    "Nippon Ham Fighters" ~ "Hokkaido Nippon-Ham Fighters", "New York G" !~
    "New York Jets". Ambiguity (both teams agreeing) is rejected by callers."""
    kt, pt = _norm_tokens(k_name), _norm_tokens(pm_name)
    if not kt or not pt:
        return False
    short, longer = (kt, pt) if len(kt) <= len(pt) else (pt, kt)
    return all(_token_agrees(t, longer) for t in short)


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
        self.spreads: list = []   # (team label, line, snapshot) — YES = that team covers
        self.totals: list = []    # (line, snapshot)             — YES = Over
        for s in markets:
            ex = s.extra
            mtype = ex.get("sports_market_type")
            if mtype in ("spreads", "totals"):
                self.start = self.start or _parse_pm_time(ex.get("game_start_time"))
                line = _line_value(s)
                outs = ex.get("outcome_labels") or []
                if line is None:
                    continue
                if mtype == "spreads" and len(outs) == 2:
                    # "Spread: Rays (-1.5)" with outcomes [Rays, Yankees]:
                    # token 0 wins if that team covers the line.
                    self.spreads.append((outs[0], line, s))
                elif mtype == "totals" and [o.lower() for o in outs] == ["over", "under"]:
                    self.totals.append((line, s))
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
_K_SPREAD_RE = re.compile(r"^(?P<team>.+?)\s+wins? by (?:more than|over)\s+(?P<line>[\d.]+)", re.I)
_K_TOTAL_RE = re.compile(r"\b(?:over|more than)\s+(?P<line>[\d.]+)\b", re.I)


def _line_value(snap) -> float | None:
    """Polymarket's numeric line. The market slug encodes it unambiguously
    ("…-total-4pt5", "…-spread-away-1pt5"); the title ("Spread -1.5", "O/U 7.5")
    is the fallback, and titles sometimes fall back to the full question."""
    slug = (snap.extra or {}).get("market_slug") or ""
    m = re.search(r"-(\d+)pt(\d+)\b", slug)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"-(?:total|spread)(?:-(?:away|home))?-(\d+)\b", slug)
    if m:
        return float(m.group(1))
    m = re.search(r"(-?\d+(?:\.\d+)?)", snap.title or "")
    return abs(float(m.group(1))) if m else None


def _k_line_events(k_snaps) -> dict:
    """Kalshi SPREAD/TOTAL markets keyed by (series prefix, game-key suffix).

    Kalshi lists them as their own events sharing the game's key:
    KXMLSGAME-26SEP26VANDCU / KXMLSSPREAD-… / KXMLSTOTAL-…. Keying on the
    EXACT series prefix keeps first-half and team-total variants
    (KXNFL1HTEAMTOTAL) out — those are different contracts.
    """
    out: dict = defaultdict(lambda: {"SPREAD": [], "TOTAL": []})
    for s in k_snaps:
        m = _K_EVENT_KIND_RE.match(s.event_id or "")
        if not m or m.group(2) not in ("SPREAD", "TOTAL"):
            continue
        out[(m.group(1), m.group(3))][m.group(2)].append(s)
    return out


def _match_lines(prefix: str, suffix: str, k_lines: dict, pg: _PGame, kg: _KGame) -> list:
    """Pair spread and total contracts for one already-verified game."""
    from matcher import MatchedPair, _close_delta_hours

    pairs = []
    bucket = k_lines.get((prefix, suffix))
    if not bucket:
        return pairs
    reason = f"sports line key {prefix}*-{suffix} <-> {pg.slug}"

    used_p: set[str] = set()
    for ks in bucket["SPREAD"]:
        m = _K_SPREAD_RE.match((ks.extra or {}).get("yes_sub_title") or ks.title or "")
        if not m:
            continue
        k_team, k_line = m.group("team"), float(m.group("line"))
        for p_team, p_line, ps in pg.spreads:
            if ps.market_id in used_p or p_line != k_line or not _names_agree(k_team, p_team):
                continue
            used_p.add(ps.market_id)
            pairs.append(MatchedPair(
                poly=ps, kalshi=ks, title_similarity=1.0,
                close_delta_hours=_close_delta_hours(ps.close_time, ks.close_time),
                confidence=0.98, match_source="sports", match_reason=reason))
            break

    for ks in bucket["TOTAL"]:
        m = _K_TOTAL_RE.search((ks.extra or {}).get("yes_sub_title") or ks.title or "")
        if not m:
            continue
        k_line = float(m.group("line"))
        for p_line, ps in pg.totals:
            if ps.market_id in used_p or p_line != k_line:
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
    by_event: dict[str, list] = defaultdict(list)
    for s in p_snaps:
        by_event[s.event_id].append(s)
    games = []
    for slug, snaps in by_event.items():
        m = _PM_GAME_SLUG_RE.match(slug or "")
        if not m:
            continue
        g = _PGame(slug, m.group(1), (m.group(2), m.group(3)), snaps)
        if g.start is not None and (g.two_way is not None or g.three_way):
            games.append(g)
    return games


def _code_map(kg: _KGame, pg: _PGame) -> dict[str, str] | None:
    """Map PM team code -> Kalshi team code, by code equality or names."""
    if kg.teams == pg.teams:
        return {c: c for c in pg.codes}
    # Name fallback: PM team names from 2-way outcomes or 3-way market titles.
    names: dict[str, str] = {}
    if pg.two_way is not None:
        outs = pg.two_way.extra.get("outcome_labels") or []
        names = dict(zip(pg.codes, outs, strict=False))
    else:
        names = {c: s.title for c, s in pg.three_way.items() if c != "tie"}
    if len(names) != 2:
        return None
    mapping = {}
    for pc, pname in names.items():
        hits = [kc for kc in kg.teams if _names_agree(kg.name(kc), pname)]
        if len(hits) != 1:
            return None
        mapping[pc] = hits[0]
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
    joins: list[tuple[_KGame, _PGame, dict]] = []
    for kg in kgs:
        cands = [pg for pg in by_teams.get(kg.teams, ()) if kg.time_ok(pg.start)]
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
            joins.append((kg, cands[0], cmap))

    # Pass 2: league namespace — drop joins dissenting from their series' majority.
    votes: dict[str, Counter] = defaultdict(Counter)
    for kg, pg, _ in joins:
        votes[kg.series][pg.prefix] += 1
    joins = [(kg, pg, cm) for kg, pg, cm in joins
             if votes[kg.series].most_common(1)[0][0] == pg.prefix]

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
            pairs.extend(_match_lines(km.group(1), km.group(3), k_lines, pg, kg))
    return pairs

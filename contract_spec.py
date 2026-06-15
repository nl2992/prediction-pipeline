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
documented in PIPELINE_REDESIGN.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from matcher import (
    _ascii_lower,
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
    raw: str = field(repr=False, default="")


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


def _polarity(text: str) -> bool:
    low = _ascii_lower(text)
    for neg, _pos in _INVERSION_ANTONYMS:
        if re.search(neg, low):
            return True
    return False


def _beat_order(text: str) -> tuple[str, str] | None:
    m = _BEAT_RE.search(text)
    if not m:
        return None
    win = frozenset(_tokens(m.group(1)))
    lose = frozenset(_tokens(m.group(2)))
    if not win or not lose:
        return None
    return (" ".join(sorted(win)), " ".join(sorted(lose)))


def extract_spec(snap: "MarketSnapshot") -> ContractSpec:
    text = _contract_text(snap)
    return ContractSpec(
        tokens=frozenset(_tokens(text)),
        entities=frozenset(_named_entities(text)),
        winner_subject=frozenset(_winner_subject(text)),
        selected_names=frozenset(_selected_names(text)),
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


def match_spec(
    a: ContractSpec,
    b: ContractSpec,
    min_similarity: float = 0.30,
) -> MatchDecision:
    """Compare two ContractSpecs field by field.

    Hard gates reject with an explicit reason; complementary fields flip the
    pair to inverted instead of rejecting; acceptance requires either token
    similarity over the gate or the threshold-led bridge (equal strike + shared
    entity + same horizon).
    """
    reasons: list[str] = []
    inverted = False

    dh = _close_delta_hours(a.close_time, b.close_time)
    same_horizon = dh is not None and dh <= 72.0

    # --- identity gates -----------------------------------------------------
    if a.domains and b.domains and a.domains.isdisjoint(b.domains):
        return _reject(f"domain mismatch: {sorted(a.domains)} vs {sorted(b.domains)}")
    if a.jurisdictions and b.jurisdictions and a.jurisdictions.isdisjoint(b.jurisdictions):
        return _reject(f"jurisdiction mismatch: {sorted(a.jurisdictions)} vs {sorted(b.jurisdictions)}")
    if a.orgs and b.orgs and a.orgs.isdisjoint(b.orgs):
        return _reject(f"org mismatch: {sorted(a.orgs)} vs {sorted(b.orgs)}")
    if a.products and b.products and a.products.isdisjoint(b.products):
        return _reject(f"product mismatch: {sorted(a.products)} vs {sorted(b.products)}")
    if a.winner_subject and b.winner_subject and not _names_overlap(
        set(a.winner_subject), set(b.winner_subject)
    ):
        return _reject(
            f"winner-subject mismatch: {sorted(a.winner_subject)} vs {sorted(b.winner_subject)}"
        )
    if a.selected_names and b.selected_names and not _names_overlap(
        set(a.selected_names), set(b.selected_names)
    ):
        return _reject(
            f"selected-name mismatch: {sorted(a.selected_names)} vs {sorted(b.selected_names)}"
        )

    # --- sports bet-type gate -------------------------------------------------
    # A moneyline (win), a totals/spread line, and a player stat-prop are
    # DIFFERENT contracts even on the same team/player. This is the structural
    # fix for the run-12 phantom-arb flood ("Sweden 1st Half O/U 0.5" vs "Will
    # Sweden win the 1st Half?", "Cody Gakpo" vs "Cody Gakpo: 2+ assists").
    if a.bet_type and b.bet_type and a.bet_type != b.bet_type:
        return _reject(f"bet-type mismatch: {a.bet_type} vs {b.bet_type}")
    # A player prop on one side and a non-prop market on the same subject
    # (the other side has no bet_type) is still a different contract.
    if (a.bet_type == "prop") != (b.bet_type == "prop") and (
        (a.entities & b.entities)
        or (a.selected_names & b.selected_names)
        or (a.winner_subject & b.winner_subject)
    ):
        return _reject("player-prop vs non-prop on same subject")

    # --- event-class gates ----------------------------------------------------
    if a.actions and b.actions and a.actions.isdisjoint(b.actions):
        return _reject(f"action mismatch: {sorted(a.actions)} vs {sorted(b.actions)}")
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

    # --- acceptance -------------------------------------------------------------
    sim = _jaccard(a.tokens, b.tokens)
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

    return MatchDecision(False, False, sim, reasons + [f"similarity {sim:.2f} below gate"])


def explain(poly: "MarketSnapshot", kalshi: "MarketSnapshot") -> MatchDecision:
    """One-call diagnostic: extract both specs and compare with reasons."""
    return match_spec(extract_spec(poly), extract_spec(kalshi))

"""Pure comparison of two settlement-rule texts for known dangerous
differences (Phase 2d).

Context: a live audit found that the four highest-edge "arbitrage" pairs on
Kalshi vs. Polymarket were not arbitrage at all -- the market TITLES matched
but the actual SETTLEMENT RULES differed in a way that breaks the arbitrage
(e.g. one venue pays ties proportionally, the other picks a single tiebreak
winner). Title-level guards (matcher.py, semantic_scope.py) can't see this,
because the difference lives in the rules text, not the title.

This module holds no I/O -- ``compare_rules`` is a pure function of two
strings. Fetching and caching the rule text lives in ``evidence.py``.

SECURITY NOTE: rule text is fetched from public venue APIs and is UNTRUSTED
DATA. Every function here treats it as opaque text to pattern-match against;
nothing here (or in evidence.py) executes, evals, or follows a URL found
inside it.

Each ``kind`` below is deliberately conservative: it only fires when BOTH
sides carry an identifiable, and DIFFERING, policy for that same question
(e.g. "how are ties resolved?"). If a side's text doesn't mention the
question at all, or mentions both candidate policies (ambiguous extraction),
no flag is raised for that kind -- a false negative is far cheaper than a
false positive here, since a human reviews the output.

``bucket_vs_threshold`` reimplements the same idea as (a hypothetical)
``semantic_scope.bucket_threshold_conflict`` -- checked against
``origin/main`` during Phase 2d and not present there yet, so it is
implemented locally here rather than imported.
"""

from __future__ import annotations

import re

Severity = str  # "high" | "medium" | "low"

# ---------------------------------------------------------------------------
# Pattern tables
#
# Each kind maps to exactly two mutually-exclusive "policies". A side's text
# is classified into one of them only when precisely one of the two patterns
# matches (both matching, or neither matching, is treated as "no identifiable
# policy" for that side and the kind is skipped for this pair).
# ---------------------------------------------------------------------------

# tie_break: one side names a specific tiebreak criterion, the other pays
# proportionally on a tie. Only bites when a tie actually happens, hence
# "medium" rather than "high".
_TIE_BREAK_POLICIES: list[tuple[str, re.Pattern]] = [
    ("tiebreaker", re.compile(
        r"\b(?:tiebreaker|tie[- ]break(?:s|ing)?|lower earned run average|"
        r"higher earned run average|alphabetic\w*|resolves?\s+to\s+the\s+tied)\b",
        re.I,
    )),
    ("proportional", re.compile(
        r"\b(?:proportional(?:ly)?\s+payout|resolves?\s+proportionally|"
        r"1\s*/\s*(?:the\s+)?number\s+of\s+tied)\b",
        re.I,
    )),
]

# ranking_basis: one side ranks candidates by seats won, the other by raw
# vote totals -- these can disagree (a party can win more votes but fewer
# seats), so a title-level "2nd place" match can silently refer to two
# different underlying orderings.
_RANKING_BASIS_POLICIES: list[tuple[str, re.Pattern]] = [
    ("seats", re.compile(
        r"\bnumber of seats\b|\bseat count\b|(?<!not )\bseats won\b",
        re.I,
    )),
    ("votes", re.compile(
        r"\bvote count\b|\bvalid-vote\b|\bpopular vote\b|\bnot seats won\b",
        re.I,
    )),
]

# event_definition: one side resolves on a mere photograph/video of the
# parties together, the other requires an actual in-person meeting. A bare
# "does not qualify" is too generic on its own (it shows up in unrelated
# exclusion clauses), so instead of matching it alone, it's only accepted as
# in_person_meeting evidence when it's clearly about a photo/photograph not
# counting (e.g. "a group photo ... does not qualify") -- exactly the phrase
# that flags a contract explicitly rejecting mere-photo evidence in favor of
# an actual meeting.
_EVENT_DEFINITION_POLICIES: list[tuple[str, re.Pattern]] = [
    ("photographed_together", re.compile(
        r"\bphotographed or videotaped\b|\bsame frame\b|\bseen together\b|"
        r"\bphotographed together\b",
        re.I,
    )),
    ("in_person_meeting", re.compile(
        r"\btrilateral meeting\b|\bmeet in person\b|\bphysically present\b|"
        r"\b(?:group\s+)?photo(?:graph)?(?:ed)?\b.{0,120}?\b(?:does not|will not)\s+"
        r"(?:qualify|count)\b",
        re.I,
    )),
]

# bucket_vs_threshold: one side is an exact-value bucket (e.g. "be 0.2%",
# "exactly 5", "between X and Y"), the other an open-ended threshold
# ("above 6000", "at least 3"). A bucket and an open threshold covering the
# same nominal number are NOT equivalent contracts (the bucket loses on any
# value above or below it; the threshold only cares about one direction).
#
# Both patterns REQUIRE a numeric value directly attached to the keyword --
# bare "exactly"/"above"/"at least"/"more than" show up constantly in
# ordinary rule prose with no bucket/threshold meaning at all ("at least one
# credible source", "exactly as reported", "above-mentioned", "the
# above-listed sources"), and would otherwise make this the noisiest, not the
# most conservative, of the four checks. A trailing 4-digit 19xx/20xx year or
# a HH:MM time is excluded from counting as "the value" -- "above 2026" or
# "at least 11:59" are dates/times, not thresholds.
_NOT_YEAR_OR_TIME = r"(?!(?:19|20)\d{2}\b)(?!\d{1,2}:\d{2}\b)"
_NUMERIC_UNIT = r"(?:%|k|m|bn|million|billion|cases|points|mph)"

_BUCKET_THRESHOLD_POLICIES: list[tuple[str, re.Pattern]] = [
    ("exact_bucket", re.compile(
        rf"\bbe\s+(?:exactly\s+)?-?\d+(?:\.\d+)?\s*%|"
        rf"\bexactly\s+{_NOT_YEAR_OR_TIME}-?\d+(?:\.\d+)?\b|"
        rf"\bbetween\s+-?[\d.]+%?\s+and\s+-?[\d.]+%?\b",
        re.I,
    )),
    ("open_threshold", re.compile(
        # "over" is deliberately excluded from the bare (no-unit) form -- it
        # overwhelmingly means "across" in rule prose ("over 1-month
        # periods", "over the following year"), not "greater than". It's
        # still accepted when a numeric unit follows, where that reading
        # isn't plausible (e.g. "over 6000 cases").
        rf"\b(?:above|more than|greater than(?:\s+or\s+equal\s+to)?|at least)\s+"
        rf"\$?-?{_NOT_YEAR_OR_TIME}\d[\d,.]*\s*{_NUMERIC_UNIT}?\b|"
        rf"\bover\s+\$?-?{_NOT_YEAR_OR_TIME}\d[\d,.]*\s*{_NUMERIC_UNIT}\b",
        re.I,
    )),
]

_POLICY_LABELS = {
    "tiebreaker": "a named tiebreaker criterion",
    "proportional": "a proportional payout on ties",
    "seats": "seats won",
    "votes": "vote count",
    "photographed_together": "being photographed/videotaped together",
    "in_person_meeting": "an actual in-person meeting",
    "exact_bucket": "an exact-value bucket",
    "open_threshold": "an open-ended threshold",
}

_KIND_QUESTIONS = {
    "tie_break": "how ties are resolved",
    "ranking_basis": "how candidates are ranked",
    "event_definition": "what event qualifies",
    "bucket_vs_threshold": "what range of values counts",
}

# (kind, severity, policy table)
_RULE_CHECKS: list[tuple[str, Severity, list[tuple[str, re.Pattern]]]] = [
    ("tie_break", "medium", _TIE_BREAK_POLICIES),
    ("ranking_basis", "high", _RANKING_BASIS_POLICIES),
    ("event_definition", "high", _EVENT_DEFINITION_POLICIES),
    ("bucket_vs_threshold", "high", _BUCKET_THRESHOLD_POLICIES),
]

_EXCERPT_LIMIT = 200
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _truncate(s: str, limit: int = _EXCERPT_LIMIT) -> str:
    s = s.strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _excerpt_for(text: str, pattern: re.Pattern) -> str:
    """The shortest human-checkable evidence for why ``pattern`` matched:
    the first matching sentence (<=200 chars), or, if the match spans a
    sentence-split boundary, a window around the raw match."""
    for sentence in _split_sentences(text):
        if pattern.search(sentence):
            return _truncate(sentence)
    match = pattern.search(text)
    if not match:
        return ""
    start = max(0, match.start() - 80)
    end = min(len(text), match.end() + 80)
    return _truncate(text[start:end])


def _classify_policy(
    text: str, policies: list[tuple[str, re.Pattern]]
) -> tuple[str, re.Pattern] | None:
    """Classify ``text`` into exactly one of ``policies``. Returns None when
    zero or more-than-one policy pattern matches (no identifiable, or
    ambiguous, policy)."""
    matched = [(name, pattern) for name, pattern in policies if pattern.search(text)]
    return matched[0] if len(matched) == 1 else None


def _policy_flag(
    kind: str, severity: Severity, kalshi_text: str, poly_text: str,
    policies: list[tuple[str, re.Pattern]],
) -> dict | None:
    kalshi_policy = _classify_policy(kalshi_text, policies)
    poly_policy = _classify_policy(poly_text, policies)
    if kalshi_policy is None or poly_policy is None:
        return None
    if kalshi_policy[0] == poly_policy[0]:
        return None  # same policy on both sides -- not a conflict

    kalshi_label = _POLICY_LABELS.get(kalshi_policy[0], kalshi_policy[0])
    poly_label = _POLICY_LABELS.get(poly_policy[0], poly_policy[0])
    detail = (
        f"On {_KIND_QUESTIONS[kind]}: Kalshi's rules read as {kalshi_label}, "
        f"Polymarket's read as {poly_label} -- these are different policies."
    )
    return {
        "kind": kind,
        "severity": severity,
        "detail": detail,
        "kalshi_excerpt": _excerpt_for(kalshi_text, kalshi_policy[1]),
        "poly_excerpt": _excerpt_for(poly_text, poly_policy[1]),
    }


def compare_rules(kalshi_text: str | None, poly_text: str | None) -> list[dict]:
    """Compare two settlement-rule texts and return a list of conflict flags.

    Each flag is ``{kind, severity, detail, kalshi_excerpt, poly_excerpt}``.
    Returns ``[]`` when either text is missing/empty, or when no kind finds a
    conservative, unambiguous conflict.
    """
    if not kalshi_text or not poly_text:
        return []
    flags: list[dict] = []
    for kind, severity, policies in _RULE_CHECKS:
        flag = _policy_flag(kind, severity, kalshi_text, poly_text, policies)
        if flag is not None:
            flags.append(flag)
    return flags

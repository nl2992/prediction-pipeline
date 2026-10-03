"""Evaluation harness for the dashboard Ask assistant (Phase 4b).

Question answered: can the assistant explain each known rule mismatch from the
cited settlement rules, and does it avoid inventing mismatches on genuine pairs?

    python -m tools.eval_ask                 # offline, deterministic ScriptedProvider
    python -m tools.eval_ask --live          # live DeepSeek (needs DEEPSEEK_API_KEY)
    python -m tools.eval_ask --out res.json  # also write JSON results

Each case gets a fresh temp SQLite DB seeded with one completed scan containing
the case's pair and market_evidence rows from the captured rule-text fixtures,
so no venue network access is needed. Exit code: 0 all pass, 1 any failure,
2 ``--live`` without a key.

Grading is deterministic keyword/regex matching (see ``grade``); it is a
smoke-level check, not a semantic judge. Known limit: the negative-case phrase
check only has a small negation guard (``_asserts_mismatch``), so a negated
phrase far from its negation word can still be flagged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python tools/eval_ask.py` as well as -m
    sys.path.insert(0, str(ROOT))

import ask  # noqa: E402
import ask_provider  # noqa: E402
import rule_diff  # noqa: E402
import store  # noqa: E402

CASES_PATH = ROOT / "tests" / "fixtures" / "ask_eval" / "cases.json"
RULE_TEXTS_DIR = ROOT / "tests" / "fixtures" / "rule_texts"

BOOK_POLY = {"bids": [[0.40, 500], [0.39, 500]], "asks": [[0.45, 500], [0.46, 500]]}
BOOK_KALSHI = {"bids": [[0.50, 500], [0.49, 500]], "asks": [[0.55, 500], [0.56, 500]]}

# Phrases that assert a rule mismatch. Used only for negative (genuine-pair) cases.
MISMATCH_PHRASES = ("not a real arbitrage", "not the same", "mismatch", "do not match", "different rules")
_NEGATION_RE = re.compile(r"\b(no|not|without|never|nor|isn't|aren't|doesn't|don't|zero)\b[^.\n]{0,40}$", re.I)
_CITE_RE = re.compile(r"\[\^([^\]\s]{1,80})\]")


# ---------------------------------------------------------------------------
# Loading / seeding
# ---------------------------------------------------------------------------

def load_cases(path: Path | str = CASES_PATH) -> list[dict]:
    return json.loads(Path(path).read_text())["cases"]


def load_rule_texts(case: dict) -> dict:
    return json.loads((RULE_TEXTS_DIR / case["rule_texts"]).read_text())


def seed_case(case: dict) -> str:
    """Seed the (already env-selected) store: one completed scan with the case's
    pair plus cached evidence for both venues. Returns the pair_key."""
    store.init_db()
    pair = dict(case["pair"], poly_book=BOOK_POLY, kalshi_book=BOOK_KALSHI)
    scan_id = store.start_scan("fast")
    store.finish_scan(scan_id, [pair], {})
    rt = load_rule_texts(case)
    store.save_evidence("kalshi", pair["kalshi_ticker"], rt["kalshi"]["rules_text"], rt["kalshi"].get("source_url"))
    store.save_evidence("polymarket", pair["poly_id"], rt["polymarket"]["rules_text"],
                        rt["polymarket"].get("source_url"))
    return store.pair_key(pair)


# ---------------------------------------------------------------------------
# Scripted provider (tests the harness/grader, NOT the model)
# ---------------------------------------------------------------------------

class ScriptedProvider:
    """Calls get_pair_evidence for the case's pair, then answers from the
    returned rule_flags / rule excerpts, citing the r* ids it was handed."""

    model = "scripted"

    def __init__(self, pair_key: str):
        self.pair_key = pair_key

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if not tool_msgs:
            return {"content": "", "model": self.model, "tool_calls": [
                {"id": "c1", "name": "get_pair_evidence", "arguments": {"pair_key": self.pair_key}}]}
        raw = tool_msgs[-1]["content"]
        body = raw[raw.index("\n") + 1: raw.rindex("\n</tool_result>")]
        res = json.loads(body)
        k, p = (res.get("kalshi") or {}), (res.get("poly") or {})
        kc, pc = k.get("cite"), p.get("cite")
        flags = res.get("rule_flags") or []
        if not flags:
            text = ("The settlement rules for the two legs were compared and no rule conflicts "
                    f"were flagged, so the pair looks like a genuine arbitrage. Kalshi rules [^{kc}], "
                    f"Polymarket rules [^{pc}].")
        else:
            lines = ["Not a real arbitrage: the settlement rules differ."]
            for f in flags:
                lines.append(f"- **{f['kind']}**: {f['detail']}")
                lines.append(f"  - Kalshi: \"{f['kalshi_excerpt']}\" [^{kc}]")
                lines.append(f"  - Polymarket: \"{f['poly_excerpt']}\" [^{pc}]")
            text = "\n".join(lines)
        return {"content": text, "tool_calls": [], "model": self.model}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _asserts_mismatch(answer: str) -> list[str]:
    """Mismatch-assertion phrases present in ``answer`` and not directly negated
    (a negation word within 40 chars before the phrase, same sentence)."""
    low = answer.lower()
    hits = []
    for phrase in MISMATCH_PHRASES:
        for m in re.finditer(re.escape(phrase), low):
            if phrase == "not a real arbitrage" or not _NEGATION_RE.search(low[max(0, m.start() - 40): m.start()]):
                hits.append(phrase)
                break
    return hits


def grade(case: dict, response: dict) -> dict:
    """Pure grading of one ``ask.run_ask`` response against a case.

    Checks (all must pass): status ok; rule citations (one per venue for
    mismatch cases, >=1 for negatives); every must_mention_any group hit
    (mismatch cases); no un-negated mismatch phrase (negative cases);
    get_pair_evidence/search_rules called; every [^id] in the answer is a
    returned citation; fixture rule_diff flag kinds match the case.
    """
    exp = case["expected"]
    answer = response.get("answer_markdown") or ""
    low = answer.lower()
    cites = response.get("citations") or []
    rule_cites = [c for c in cites if c.get("kind") == "rule"]
    venues = {c.get("venue") for c in rule_cites}
    checks: dict[str, bool] = {}

    checks["status_ok"] = response.get("status") == "ok"
    if exp["mismatch"]:
        checks["rule_citation_each_venue"] = {"kalshi", "polymarket"} <= venues
        checks["must_mention_all_groups"] = all(
            any(alt.lower() in low for alt in group) for group in exp.get("must_mention_any") or [])
    else:
        checks["rule_citation_any"] = len(rule_cites) >= 1
        if exp.get("must_not_claim_mismatch"):
            checks["no_mismatch_claim"] = not _asserts_mismatch(answer)
    checks["used_evidence_tool"] = any(
        t.get("name") in ("get_pair_evidence", "search_rules") for t in response.get("tool_calls") or [])
    known = {c.get("id") for c in cites}
    checks["citation_ids_valid"] = all(i in known for i in _CITE_RE.findall(answer))

    rt = load_rule_texts(case)
    kinds = {f["kind"] for f in rule_diff.compare_rules(rt["kalshi"]["rules_text"], rt["polymarket"]["rules_text"])}
    want = set(exp.get("expected_flag_kinds") or [])
    allowed = want | set(exp.get("allowed_extra_flag_kinds") or [])
    checks["fixture_flag_kinds"] = want <= kinds <= allowed
    return {"passed": all(checks.values()), "checks": checks}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_case(case: dict, provider_factory) -> dict:
    """Run one case in a fresh temp DB; ``provider_factory(pair_key)`` -> provider."""
    prev = os.environ.get("PRED_DASHBOARD_DB")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["PRED_DASHBOARD_DB"] = str(Path(tmp) / "eval.db")
        try:
            pair_key = seed_case(case)
            response = ask.run_ask(case["question"], [], provider_factory(pair_key))
        finally:
            if prev is None:
                os.environ.pop("PRED_DASHBOARD_DB", None)
            else:
                os.environ["PRED_DASHBOARD_DB"] = prev
    return {"id": case["id"], "response": response, **grade(case, response)}


def run_all(cases: list[dict], provider_factory) -> list[dict]:
    return [run_case(c, provider_factory) for c in cases]


def format_table(results: list[dict]) -> str:
    rows = [("case", "passed", "failed checks")]
    for r in results:
        failed = [k for k, v in r["checks"].items() if not v]
        rows.append((r["id"], "PASS" if r["passed"] else "FAIL", ", ".join(failed) or "-"))
    w = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = ["  ".join(c.ljust(w[i]) for i, c in enumerate(row)).rstrip() for row in rows]
    lines.insert(1, "  ".join("-" * x for x in w))
    n = sum(r["passed"] for r in results)
    lines.append(f"\n{n}/{len(results)} passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the Ask assistant against known rule mismatches.")
    ap.add_argument("--live", action="store_true", help="use live DeepSeek instead of the scripted provider")
    ap.add_argument("--cases", default=str(CASES_PATH))
    ap.add_argument("--out", default=None, help="write JSON results here")
    args = ap.parse_args(argv)

    if args.live:
        provider = ask_provider.get_provider()
        if provider is None:
            print("DEEPSEEK_API_KEY not set")
            return 2

        def factory(_pk):
            return provider
    else:
        factory = ScriptedProvider

    results = run_all(load_cases(args.cases), factory)
    print(format_table(results))
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"mode": "live" if args.live else "scripted", "results": results}, indent=1, default=str))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

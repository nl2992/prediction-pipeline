"""Tests for tools/eval_ask.py (Phase 4b). No network, no TestClient."""
from __future__ import annotations

import copy

import pytest

from tools import eval_ask

CASES = {c["id"]: c for c in eval_ask.load_cases()}


def _resp(answer, *, status="ok", venues=("kalshi", "polymarket"), tools=("get_pair_evidence",)):
    cites = [{"id": f"r{i + 1}", "kind": "rule", "venue": v, "excerpt": "x"} for i, v in enumerate(venues)]
    return {"status": status, "answer_markdown": answer, "citations": cites,
            "tool_calls": [{"name": t, "args": {}, "ok": True, "summary": ""} for t in tools]}


GOOD_MLB = ("Not a real arbitrage. In a tie Kalshi pays proportionally [^r1] while Polymarket "
            "uses the lower ERA [^r2].")


def test_cases_cover_all_fixtures():
    assert set(CASES) == {"mlb", "quebec", "trump_xi_putin", "cpi_core", "measles", "hurricane_category"}
    for c in CASES.values():
        assert eval_ask.load_rule_texts(c)["kalshi"]["market_id"] == c["pair"]["kalshi_ticker"]


def test_grade_passes_good_mismatch():
    g = eval_ask.grade(CASES["mlb"], _resp(GOOD_MLB))
    assert g["passed"], g
    assert all(g["checks"].values())


@pytest.mark.parametrize("mutate,check", [
    (lambda r: r.update(status="error"), "status_ok"),
    (lambda r: r.update(citations=[c for c in r["citations"] if c["venue"] == "kalshi"]),
     "rule_citation_each_venue"),
    (lambda r: r.update(answer_markdown="Not a real arbitrage; rules differ [^r1][^r2]."),
     "must_mention_all_groups"),
    (lambda r: r.update(tool_calls=[{"name": "search_pairs"}]), "used_evidence_tool"),
    (lambda r: r.update(answer_markdown=GOOD_MLB + " [^r9]"), "citation_ids_valid"),
])
def test_grade_mismatch_failures(mutate, check):
    r = _resp(GOOD_MLB)
    mutate(r)
    g = eval_ask.grade(CASES["mlb"], r)
    assert not g["passed"] and g["checks"][check] is False


def test_grade_mention_groups_need_every_group():
    # only the "tie" group is hit, ERA/proportional group is missing
    g = eval_ask.grade(CASES["mlb"], _resp("Ties differ [^r1][^r2]."))
    assert g["checks"]["must_mention_all_groups"] is False
    g = eval_ask.grade(CASES["mlb"], _resp("Tie rule: earned run average vs split [^r1][^r2]."))
    assert g["checks"]["must_mention_all_groups"] is True


def test_grade_negative_pass_and_fail():
    ok = _resp("No conflicts found in the rules [^r1][^r2].")
    assert eval_ask.grade(CASES["measles"], ok)["passed"]
    # one rule citation is enough for negatives
    assert eval_ask.grade(CASES["measles"], _resp("Rules agree [^r1].", venues=("kalshi",)))["passed"]
    bad = _resp("This is a mismatch between the venues [^r1][^r2].")
    g = eval_ask.grade(CASES["measles"], bad)
    assert not g["passed"] and g["checks"]["no_mismatch_claim"] is False
    none = _resp("Looks fine.", venues=())
    assert eval_ask.grade(CASES["measles"], none)["checks"]["rule_citation_any"] is False


def test_grade_negative_negation_guard():
    r = _resp("There is no mismatch in the rules [^r1][^r2].")
    assert eval_ask.grade(CASES["hurricane_category"], r)["checks"]["no_mismatch_claim"] is True


def test_grade_flag_kinds_check():
    bad = copy.deepcopy(CASES["cpi_core"])
    bad["expected"]["expected_flag_kinds"] = ["tie_break"]
    g = eval_ask.grade(bad, _resp("exactly 0.2% bucket vs above threshold [^r1][^r2]"))
    assert g["checks"]["fixture_flag_kinds"] is False


def test_offline_end_to_end_all_cases_pass():
    results = eval_ask.run_all(list(CASES.values()), eval_ask.ScriptedProvider)
    failed = {r["id"]: [k for k, v in r["checks"].items() if not v] for r in results if not r["passed"]}
    assert not failed, failed
    assert len(results) == len(CASES)
    assert "6/6 passed" in eval_ask.format_table(results)


def test_main_live_without_key_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(eval_ask.ask_provider, "get_provider", lambda: None)
    assert eval_ask.main(["--live"]) == 2
    assert "DEEPSEEK_API_KEY not set" in capsys.readouterr().out


def test_main_scripted_writes_json(tmp_path, capsys):
    out = tmp_path / "res.json"
    assert eval_ask.main(["--out", str(out)]) == 0
    assert out.exists() and '"mode": "scripted"' in out.read_text()


def test_grade_mismatch_must_conclude_mismatch():
    bad = _resp("Kalshi ranks by seats and votes differ on Polymarket, but this is a real arbitrage [^r1][^r2].")
    g = eval_ask.grade(CASES["quebec"], bad)
    assert g["checks"]["must_mention_all_groups"] is True
    assert g["checks"]["concludes_mismatch"] is False and not g["passed"]
    good = _resp("This is not a real arbitrage because Polymarket ranks by seats while Kalshi ranks "
                 "by votes [^r1][^r2].")
    g = eval_ask.grade(CASES["quebec"], good)
    assert g["checks"]["concludes_mismatch"] is True and g["passed"], g

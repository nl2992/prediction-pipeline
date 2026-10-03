"""Tests for ask_tools (Phase 3a). No network; tools called directly."""
from __future__ import annotations

import json

import pytest

import ask_tools
import rule_search
import store
from tests._ask_seed import MLB, seed


@pytest.fixture
def seeded():
    return seed()


def _key(p):
    return store.pair_key(p)


def _run(**args):
    return [x["pair_key"] for x in ask_tools.run_tool("search_pairs", args)["pairs"]]


def test_schemas_are_openai_function_format():
    names = {t["name"] for t in ask_tools.TOOLS}
    assert names == {"search_pairs", "get_pair_evidence", "calculate_budget", "compare_scans", "search_rules"}
    for t in ask_tools.TOOLS:
        assert t["description"] and t["parameters"]["type"] == "object"
        assert t["parameters"]["additionalProperties"] is False
    json.dumps(ask_tools.TOOLS)


def test_run_tool_never_raises():
    assert ask_tools.run_tool("nope", {})["ok"] is False
    assert ask_tools.run_tool("search_pairs", "bad")["ok"] is False
    assert ask_tools.run_tool(None, None)["ok"] is False


def test_search_pairs_default_sort_and_provenance(seeded):
    s1, s2, pairs = seeded
    r = ask_tools.run_tool("search_pairs", {})
    assert r["ok"] and r["status"] == "ok"
    assert r["scan"]["id"] == s2 and r["scan"]["mode"] == "fast" and r["scan"]["finished_at"]
    assert r["inputs"]["limit"] == 10 and r["inputs"]["sort"] == "net"
    assert [x["pair_key"] for x in r["pairs"]] == [_key(pairs["p1"]), _key(pairs["p2"]), _key(pairs["p4"])]
    top = r["pairs"][0]
    assert top["net"] == 0.08 and top["review"] == "mismatch"
    assert top["days_to_settle"] == pytest.approx(10, abs=0.1)
    assert [f["kind"] for f in top["rule_flags"]] == ["tie_break"]
    assert r["pairs"][1]["rule_flags"] is None  # no cached evidence -> unknown
    assert r["pairs"][2]["review"] == "match" and r["pairs"][1]["review"] == "unreviewed"


def test_search_pairs_filters(seeded):
    _, _, pairs = seeded
    assert _run(category="politics") == [_key(pairs["p2"])]
    assert _run(review_state="unreviewed") == [_key(pairs["p2"])]
    assert _run(review_state="mismatch") == [_key(pairs["p1"])]
    assert _run(min_depth_contracts=200) == [_key(pairs["p4"])]
    assert _run(min_net=0.05) == [_key(pairs["p1"])]
    assert _run(max_days_to_settle=5) == [_key(pairs["p2"])]
    assert _run(has_rule_flags=True) == [_key(pairs["p1"])]
    assert _key(pairs["p1"]) not in _run(has_rule_flags=False)
    assert _run(sort="close")[0] == _key(pairs["p2"])
    assert _run(sort="exec_contracts")[0] == _key(pairs["p4"])
    assert len(_run(limit=1)) == 1


def test_search_pairs_validation_and_clamp(seeded):
    for bad in ({"bogus": 1}, {"review_state": "maybe"}, {"min_net": "x"}, {"limit": "5"},
                {"has_rule_flags": "yes"}, {"sort": "random"}, {"min_net": True}):
        r = ask_tools.run_tool("search_pairs", bad)
        assert r["ok"] is False and r["error"], bad
    assert ask_tools.run_tool("search_pairs", {"limit": 9999})["inputs"]["limit"] == 50
    assert ask_tools.run_tool("search_pairs", {"limit": -3})["inputs"]["limit"] == 1


def test_search_pairs_no_scan():
    store.init_db()
    r = ask_tools.run_tool("search_pairs", {})
    assert r["ok"] and r["scan"] is None and r["status"] == "no_scan" and r["pairs"] == []


def test_get_pair_evidence_flags(seeded):
    _, s2, pairs = seeded
    r = ask_tools.run_tool("get_pair_evidence", {"pair_key": _key(pairs["p1"])})
    assert r["ok"] and r["status"] == "ok" and r["scan"]["id"] == s2
    assert len(r["kalshi"]["rules_text"]) <= 2000 and len(r["poly"]["rules_text"]) <= 2000
    assert r["kalshi"]["source_url"] and r["kalshi"]["hash"] and r["kalshi"]["stale"] is False
    assert [f["kind"] for f in r["rule_flags"]] == ["tie_break"]


def test_get_pair_evidence_truncation(seeded):
    _, _, pairs = seeded
    store.save_evidence("kalshi", "K2", "x" * 5000, None)
    store.save_evidence("polymarket", "poly2", "y" * 10, None)
    r = ask_tools.run_tool("get_pair_evidence", {"pair_key": _key(pairs["p2"])})
    assert len(r["kalshi"]["rules_text"]) == 2000 and r["kalshi"]["truncated"] is True
    assert r["poly"]["truncated"] is False


def test_get_pair_evidence_unknown_pair_and_validation(seeded):
    r = ask_tools.run_tool("get_pair_evidence", {"pair_key": "nope|nope"})
    assert r["ok"] and r["status"] == "pair_not_found"
    assert ask_tools.run_tool("get_pair_evidence", {})["ok"] is False
    assert ask_tools.run_tool("get_pair_evidence", {"pair_key": "a|b", "x": 1})["ok"] is False


def test_get_pair_evidence_missing(seeded, monkeypatch):
    import evidence

    def offline(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(evidence, "_http_get_json", offline)
    _, _, pairs = seeded
    r = ask_tools.run_tool("get_pair_evidence", {"pair_key": _key(pairs["p4"])})
    assert r["status"] == "evidence_missing" and r["kalshi"] is None and r["poly"] is None


def test_calculate_budget_ok_and_matches_compute(seeded):
    import server
    _, s2, pairs = seeded
    p1 = pairs["p1"]
    r = ask_tools.run_tool("calculate_budget", {"pair_key": _key(p1), "usd": 500})
    assert r["ok"] and r["status"] == "ok"
    assert r["books_from_scan_id"] == s2 and r["books_age_seconds"] >= 0
    assert r["inputs"] == {"pair_key": _key(p1), "usd": 500} and r["scan"]["id"] == s2
    direct = server._compute_book_arb(server._book_from_lists(p1["poly_book"]),
                                      server._book_from_lists(p1["kalshi_book"]), budgets=(500,))
    assert r["best_direction"] == direct["best_direction"]
    for d, res in r["directions"].items():
        assert res["by_budget"] == direct["directions"][d]["by_budget"]
        assert "curve" not in res and "ladders" not in res
    json.dumps(r)


def test_calculate_budget_books_unavailable_and_errors(seeded):
    _, _, pairs = seeded
    r = ask_tools.run_tool("calculate_budget", {"pair_key": _key(pairs["p2"]), "usd": 100})
    assert r["ok"] and r["status"] == "books_unavailable"
    r = ask_tools.run_tool("calculate_budget", {"pair_key": "x|y", "usd": 100})
    assert r["status"] == "pair_not_found"
    for bad in ({"pair_key": "a|b", "usd": 0}, {"pair_key": "a|b", "usd": 100001},
                {"pair_key": "a|b", "usd": "5"}, {"pair_key": "a|b"}, {"usd": 5}):
        assert ask_tools.run_tool("calculate_budget", bad)["ok"] is False, bad


def test_compare_scans_default_and_explicit(seeded):
    s1, s2, pairs = seeded
    r = ask_tools.run_tool("compare_scans", {})
    assert r["ok"] and r["status"] == "ok" and r["inputs"] == {"a": s1, "b": s2}
    assert r["scan"]["id"] == s2 and r["scan_a"]["id"] == s1
    assert r["added"] == ["K4|poly4-tok"] and r["removed"] == ["K3|poly3-tok"]
    assert r["added_count"] == 1 and r["removed_count"] == 1
    assert [c["pair_key"] for c in r["top_changed"]] == [_key(pairs["p1"])]
    assert r["top_changed"][0]["delta"] == 0.03
    assert ask_tools.run_tool("compare_scans", {"a": s1, "b": s2})["added"] == r["added"]
    rev = ask_tools.run_tool("compare_scans", {"a": s2, "b": s1})
    assert rev["added"] == r["removed"]


def test_compare_scans_edge_cases(seeded):
    s1, _, _ = seeded
    assert ask_tools.run_tool("compare_scans", {"a": 999, "b": s1})["status"] == "scan_not_found"
    assert ask_tools.run_tool("compare_scans", {"b": s1})["status"] == "no_previous_scan"
    assert ask_tools.run_tool("compare_scans", {"a": "1"})["ok"] is False


def test_search_rules_ok_and_unavailable(seeded, monkeypatch):
    r = ask_tools.run_tool("search_rules", {"q": "earned run average", "limit": 99})
    assert r["ok"] and r["status"] == "ok" and r["inputs"]["limit"] == 10
    assert r["results"] and r["results"][0]["venue"] in ("kalshi", "polymarket")
    assert ask_tools.run_tool("search_rules", {"q": "x" * 201})["ok"] is False
    assert ask_tools.run_tool("search_rules", {"q": "x", "venue": "other"})["ok"] is False
    assert ask_tools.run_tool("search_rules", {})["ok"] is False

    def boom(*a, **k):
        raise rule_search.SearchUnavailable("no fts")

    monkeypatch.setattr(rule_search, "search", boom)
    r = ask_tools.run_tool("search_rules", {"q": MLB["expected_kind"]})
    assert r["ok"] and r["status"] == "unavailable"

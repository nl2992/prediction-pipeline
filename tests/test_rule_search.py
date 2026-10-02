"""Tests for rule_search.py / market_evidence_fts (Phase 4a). Routes are
called as plain functions (CI has no httpx for TestClient)."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

import rule_search
import server
import store

FIXTURES = Path(__file__).parent / "fixtures" / "rule_texts"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _fts_count() -> int:
    with closing(store.connect()) as conn:
        return conn.execute("SELECT count(*) FROM market_evidence_fts").fetchone()[0]


def _drop_fts():
    with closing(store.connect()) as conn, conn:
        for t in ("ai", "ad", "au"):
            conn.execute(f"DROP TRIGGER market_evidence_fts_{t}")
        conn.execute("DROP TABLE market_evidence_fts")


@pytest.fixture(autouse=True)
def _db():
    store.init_db()


def _seed_fixture_rules():
    for name in ("mlb", "quebec"):
        fx = _fixture(name)
        for venue, key in (("kalshi", "kalshi"), ("polymarket", "polymarket")):
            m = fx[key]
            store.save_evidence(venue, m["market_id"], m["rules_text"], m["source_url"])


def test_schema_version_bumped():
    assert store.SCHEMA_VERSION == 4


def test_triggers_sync_insert_update_delete():
    store.save_evidence("kalshi", "K1", "alpha bravo", None)
    assert _fts_count() == 1
    store.save_evidence("kalshi", "K1", "alpha bravo", None)  # confirm, no dup
    assert _fts_count() == 1
    with closing(store.connect()) as conn, conn:
        conn.execute("UPDATE market_evidence SET rules_text = 'charlie delta' WHERE market_id = 'K1'")
    assert rule_search.search("alpha") == []
    assert len(rule_search.search("charlie")) == 1
    with closing(store.connect()) as conn, conn:
        conn.execute("DELETE FROM market_evidence WHERE market_id = 'K1'")
    assert _fts_count() == 0
    assert rule_search.search("charlie") == []


def test_backfill_on_migration_from_v3():
    store.save_evidence("kalshi", "K1", "settles on hurricane landfall", None)
    store.save_evidence("polymarket", "P1", "settles on measles cases", None)
    _drop_fts()
    with closing(store.connect()) as conn, conn:
        conn.execute("UPDATE schema_version SET version = 3")
    store.init_db()
    with closing(store.connect()) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == store.SCHEMA_VERSION
    assert _fts_count() == 2
    assert [r["market_id"] for r in rule_search.search("hurricane")] == ["K1"]
    store.init_db()  # idempotent
    assert _fts_count() == 2


@pytest.mark.parametrize("q", [
    '"', "NEAR(", "*", "-", "OR", "col:term", 'unbalanced "quote', '"a" "b', "x" * 1000,
    "rules_text:foo", "foo OR bar", "(((", "^foo", "a AND NOT b",
])
def test_hostile_queries_do_not_raise(q):
    store.save_evidence("kalshi", "K1", "foo bar baz OR NEAR", None)
    rule_search.search(q)  # must not raise


def test_operators_are_literal_not_syntax():
    store.save_evidence("kalshi", "K1", "foo only", None)
    store.save_evidence("kalshi", "K2", "bar only", None)
    # "foo OR bar" is an AND of three literal words -> matches neither
    assert rule_search.search("foo OR bar") == []
    assert rule_search.build_match("col:term") == '"col" AND "term"'
    assert rule_search.build_match("") == ""
    assert rule_search.search("") == []
    assert rule_search.search('"') == []


def test_build_match_rules():
    assert rule_search.build_match('"tie proportional" seats') == '"tie proportional" AND "seats"'
    assert rule_search.build_match("3.5% rate.") == '"3.5%" AND "rate"'
    many = " ".join(f"w{i}" for i in range(30))
    assert rule_search.build_match(many).count(" AND ") == 11
    assert len(rule_search.build_match("x " * 500).split(" AND ")) <= 12


def test_porter_stemming():
    store.save_evidence("kalshi", "K1", "If the teams ties after extra innings", None)
    assert len(rule_search.search("tied")) == 1


def test_ranking_prefers_more_relevant():
    store.save_evidence("kalshi", "LOW", "one mention of tariff among many many other unrelated words " * 3, None)
    store.save_evidence("kalshi", "HIGH", "tariff tariff tariff", None)
    res = rule_search.search("tariff")
    assert [r["market_id"] for r in res] == ["HIGH", "LOW"]


def test_snippet_markers():
    store.save_evidence("kalshi", "K1", "The market resolves Yes if the tariff is enacted.", None)
    snip = rule_search.search("tariff")[0]["snippet"]
    assert "«tariff»" in snip
    assert "<" not in snip


def test_latest_row_per_market_only():
    store.save_evidence("kalshi", "K1", "old wording zebra", None)
    store.save_evidence("kalshi", "K1", "new wording giraffe", None)
    assert rule_search.search("zebra") == []
    assert len(rule_search.search("giraffe")) == 1


def test_venue_filter_and_limit():
    store.save_evidence("kalshi", "K1", "common phrase", None)
    store.save_evidence("polymarket", "P1", "common phrase", None)
    assert len(rule_search.search("common")) == 2
    res = rule_search.search("common", venue="polymarket")
    assert [r["venue"] for r in res] == ["polymarket"]
    assert len(rule_search.search("common", limit=1)) == 1


def test_pair_keys_resolution_uses_latest_completed_scan():
    store.save_evidence("kalshi", "KT", "unique kalshi text", None)
    store.save_evidence("polymarket", "PX", "unique poly text", None)
    old = store.start_scan("full")
    store.finish_scan(old, [{"kalshi_ticker": "KT", "poly_id": "PX", "poly_token_id": "OLD"}], None)
    new = store.start_scan("full")
    store.finish_scan(new, [{"kalshi_ticker": "KT", "poly_id": "PX", "poly_token_id": "NEW"}], None)
    running = store.start_scan("full")  # never finished: ignored
    k = rule_search.search("kalshi")[0]
    assert k["pair_keys"] == ["KT|NEW"]
    p = rule_search.search("poly")[0]
    assert p["pair_keys"] == ["KT|NEW"]
    assert set(k) == {"venue", "market_id", "content_hash", "snippet", "source_url", "fetched_at", "pair_keys"}
    assert running  # silence unused


def test_pair_keys_capped_at_five():
    store.save_evidence("kalshi", "KT", "capped", None)
    s = store.start_scan("full")
    store.finish_scan(s, [{"kalshi_ticker": "KT", "poly_token_id": f"T{i}"} for i in range(9)], None)
    assert len(rule_search.search("capped")[0]["pair_keys"]) == 5


def test_real_fixtures():
    _seed_fixture_rules()
    mlb = _fixture("mlb")["kalshi"]["market_id"]
    assert mlb in {r["market_id"] for r in rule_search.search("tie proportional")}
    quebec = _fixture("quebec")["polymarket"]["market_id"]
    assert quebec in {r["market_id"] for r in rule_search.search("seats")}


def test_route_ok_clamps_and_errors(monkeypatch):
    _seed_fixture_rules()
    body = json.loads(server.api_evidence_search(q="seats", limit=999).body)
    assert body["query"] == "seats" and body["results"]
    seen = {}
    monkeypatch.setattr(rule_search, "search", lambda q, limit, venue: seen.update(limit=limit) or [])
    server.api_evidence_search(q="x", limit=0)
    assert seen["limit"] == 1
    server.api_evidence_search(q="x", limit=999)
    assert seen["limit"] == 50
    with pytest.raises(server.HTTPException) as ei:
        server.api_evidence_search(q="x" * 201)
    assert ei.value.status_code == 422

    def boom(q, limit, venue):
        raise rule_search.SearchUnavailable("no fts5")
    monkeypatch.setattr(rule_search, "search", boom)
    resp = server.api_evidence_search(q="x")
    assert resp.status_code == 503 and json.loads(resp.body) == {"error": "no fts5"}


def test_search_unavailable_when_table_missing():
    _drop_fts()
    with pytest.raises(rule_search.SearchUnavailable):
        rule_search.search("anything")

"""Full-text search over cached settlement-rule text (Phase 4a).

Backed by the ``market_evidence_fts`` FTS5 table (see store._ensure_fts).
User input is never passed to FTS5 as syntax: it is reduced to quoted
phrase terms joined with implicit AND.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing

import store

MAX_QUERY_CHARS = 200
MAX_TERMS = 12
MAX_PAIR_KEYS = 5

_SEGMENT_RE = re.compile(r'"([^"]*)"|([^"]+)')
_WORD_RE = re.compile(r"(?:[^\W_]|[%.])+")


class SearchUnavailable(RuntimeError):
    """FTS5 index is not available in this sqlite build."""


def _words(text: str) -> list[str]:
    out = []
    for w in _WORD_RE.findall(text):
        w = w.strip(".")
        if any(c.isalnum() for c in w):
            out.append(w)
    return out


def _word_term(w: str) -> str:
    # Porter quirk: "ties"/"tied" stem to "ti" but "tie" keeps its "e", so a
    # plain "tie" query would miss them. Accept the e-less form as well.
    if len(w) >= 3 and w.lower().endswith("ie"):
        return f'("{w}" OR "{w[:-1]}")'
    return f'"{w}"'


def build_match(q: str) -> str:
    """Sanitize ``q`` into a safe FTS5 MATCH expression ('' if no terms)."""
    terms: list[str] = []
    for m in _SEGMENT_RE.finditer((q or "")[:MAX_QUERY_CHARS]):
        if m.group(1) is not None:
            ws = _words(m.group(1))
            if ws:
                terms.append('"' + " ".join(ws) + '"')
        else:
            terms.extend(_word_term(w) for w in _words(m.group(2)))
        if len(terms) >= MAX_TERMS:
            break
    return " AND ".join(terms[:MAX_TERMS])


def _pair_keys(conn: sqlite3.Connection, venue: str, market_id: str) -> list[str]:
    col = "kalshi_ticker" if venue == "kalshi" else "poly_id" if venue == "polymarket" else None
    if col is None:
        return []
    rows = conn.execute(
        f"""SELECT ps.pair_key FROM pair_snapshots ps
            WHERE ps.{col} = ?
              AND ps.scan_id = (
                SELECT MAX(p2.scan_id) FROM pair_snapshots p2
                JOIN scans s ON s.id = p2.scan_id
                WHERE s.status = 'completed' AND p2.{col} = ?)
            ORDER BY ps.rank IS NULL, ps.rank, ps.pair_key LIMIT ?""",
        (market_id, market_id, MAX_PAIR_KEYS),
    ).fetchall()
    return [r["pair_key"] for r in rows]


def search(q: str, limit: int = 20, venue: str | None = None) -> list[dict]:
    match = build_match(q)
    if not match:
        return []
    sql = """
        SELECT e.venue, e.market_id, e.content_hash, e.source_url, e.fetched_at,
               snippet(market_evidence_fts, 0, '«', '»', '…', 12) AS snippet
        FROM market_evidence_fts
        JOIN market_evidence e ON e.rowid = market_evidence_fts.rowid
        WHERE market_evidence_fts MATCH ?
          AND NOT EXISTS (
            SELECT 1 FROM market_evidence n
            WHERE n.venue = e.venue AND n.market_id = e.market_id
              AND (n.fetched_at > e.fetched_at
                   OR (n.fetched_at = e.fetched_at AND n.rowid > e.rowid)))
    """
    params: list = [match]
    if venue:
        sql += " AND e.venue = ?"
        params.append(venue)
    sql += " ORDER BY bm25(market_evidence_fts) LIMIT ?"
    params.append(max(1, int(limit)))
    with closing(store.connect()) as conn:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'market_evidence_fts'"
        ).fetchone()
        if has is None:
            raise SearchUnavailable("FTS5 rule search is unavailable in this sqlite build")
        rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["pair_keys"] = _pair_keys(conn, d["venue"], d["market_id"])
            results.append(d)
        return results

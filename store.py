"""SQLite persistence for the dashboard (Phase 2a): scans, pair snapshots,
market evidence (schema only for now) and human reviews.

stdlib ``sqlite3`` only — no ORM, no new dependency. Every function opens its
own connection (FastAPI's sync routes run each request in a threadpool, so
connections must not be shared across threads) and closes it before
returning, except where multiple statements need one transaction (bulk
snapshot insert in ``finish_scan``).

DB path: env ``PRED_DASHBOARD_DB``, else ``data/dashboard.db`` (relative to
this file's directory, matching ``server.py``'s ``STATIC_DIR`` pattern). The
``data/`` directory is created on first use.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

_VALID_SCAN_STATUS = {"running", "completed", "failed", "cancelled"}
_VALID_SCAN_MODE = {"full", "fast"}
_VALID_VERDICT = {"match", "mismatch", "uncertain"}

# A full scan carries ~12-14k pairs, each with poly_book/kalshi_book (top 30
# levels/side) — leaving those inline in pair_json makes a single scan's
# snapshot ~50-100MB, and keep=50 would put the DB in the GB range. Only the
# BOOKS_KEEP most recent completed scans per mode retain books_json; older
# scans keep every other field (rank, prices, edges, matcher reasons, ...)
# but their order-book ladders are NULLed out after the newer scan lands.
BOOKS_KEEP_PER_MODE = 2
_BOOK_FIELDS = ("poly_book", "kalshi_book")


def _default_db_path() -> Path:
    return Path(__file__).parent / "data" / "dashboard.db"


def db_path() -> Path:
    """Resolves the DB path fresh on every call (honours ``PRED_DASHBOARD_DB``
    changing between calls, e.g. in tests that monkeypatch the env var)."""
    override = os.environ.get("PRED_DASHBOARD_DB")
    return Path(override) if override else _default_db_path()


def connect() -> sqlite3.Connection:
    """One connection per call. WAL mode (concurrent readers alongside a
    writer) + foreign keys (pair_snapshots -> scans ON DELETE CASCADE).

    auto_vacuum=INCREMENTAL is set for a brand-new DB file *before* WAL mode
    is enabled — sqlite only honours a changed auto_vacuum mode on a fresh,
    table-less database, and (empirically) that window closes as soon as WAL
    mode is turned on, even with no tables yet. Getting this order wrong
    silently leaves auto_vacuum at its default (NONE), so freed pages (e.g.
    from ``_trim_books_json``) are never returned to the OS — see init_db().
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not path.exists()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if is_new_file:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, coldef: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def init_db() -> None:
    """Idempotent schema creation. Safe to call on every startup.

    A fresh DB gets all columns via CREATE TABLE directly; an existing DB
    (created under SCHEMA_VERSION 1) is migrated in place with ALTER TABLE
    ADD COLUMN — sqlite3's ADD COLUMN is a metadata-only op on existing
    tables, so this is cheap even on a DB with years of scan history.

    A brand-new DB file gets ``auto_vacuum=INCREMENTAL`` from connect() (see
    its docstring) so that ``PRAGMA incremental_vacuum`` after trimming
    books_json (see ``_trim_books_json``) actually shrinks the file on disk
    instead of leaving freed pages sitting in sqlite's internal freelist.
    """
    with closing(connect()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            )
        """)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] < SCHEMA_VERSION:
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

        conn.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at          TEXT NOT NULL,
                finished_at         TEXT,
                status              TEXT NOT NULL CHECK (status IN ('running','completed','failed','cancelled')),
                mode                TEXT NOT NULL CHECK (mode IN ('full','fast')),
                params_json         TEXT,
                coverage_json       TEXT,
                error               TEXT,
                pair_count          INTEGER,
                arb_count           INTEGER,
                summary_json        TEXT,
                duplicate_pair_keys INTEGER
            )
        """)
        # v1 -> v2 migration for a DB created before duplicate_pair_keys existed.
        _add_column_if_missing(conn, "scans", "duplicate_pair_keys", "INTEGER")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pair_snapshots (
                scan_id       INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                pair_key      TEXT NOT NULL,
                rank          INTEGER,
                kalshi_ticker TEXT,
                poly_token_id TEXT,
                poly_id       TEXT,
                category      TEXT,
                net           REAL,
                exec_profit   REAL,
                exec_contracts REAL,
                close_time    TEXT,
                pair_json     TEXT NOT NULL,
                books_json    TEXT,
                PRIMARY KEY (scan_id, pair_key)
            )
        """)
        # v1 -> v2 migration: poly_book/kalshi_book move out of pair_json into
        # this nullable column (see BOOKS_KEEP_PER_MODE above); old rows just
        # keep their books inline in pair_json and read back unchanged.
        _add_column_if_missing(conn, "pair_snapshots", "books_json", "TEXT")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pair_snapshots_scan_rank
                ON pair_snapshots(scan_id, rank)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_evidence (
                venue        TEXT NOT NULL,
                market_id    TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                rules_text   TEXT,
                source_url   TEXT,
                fetched_at   TEXT,
                PRIMARY KEY (venue, market_id, content_hash)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_key          TEXT NOT NULL,
                verdict           TEXT NOT NULL CHECK (verdict IN ('match','mismatch','uncertain')),
                reason            TEXT,
                reviewer          TEXT NOT NULL DEFAULT 'local',
                created_at        TEXT NOT NULL,
                kalshi_rules_hash TEXT,
                poly_rules_hash   TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reviews_pair_key ON reviews(pair_key)
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pair_key(pair: dict) -> str:
    """MUST stay in lockstep with static/index.html's pairKey():
    kalshi_ticker + '|' + (poly_token_id or poly_id or '')."""
    poly_side = pair.get("poly_token_id") or pair.get("poly_id") or ""
    kalshi_ticker = pair.get("kalshi_ticker") or ""
    return f"{kalshi_ticker}|{poly_side}"


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------

def start_scan(mode: str, params: dict | None = None) -> int:
    if mode not in _VALID_SCAN_MODE:
        raise ValueError(f"invalid scan mode: {mode!r}")
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO scans (started_at, status, mode, params_json)
               VALUES (?, 'running', ?, ?)""",
            (_now(), mode, json.dumps(params or {})),
        )
        return cur.lastrowid


def _split_books(pair: dict) -> tuple[dict, dict | None]:
    """Pulls poly_book/kalshi_book (top-30-levels/side ladders, the bulk of a
    pair's byte size) out of the pair dict. Returns (slim_pair, books_or_None)
    — books is None (not an empty dict) when neither field was present, so a
    pair that never carried books round-trips with no books_json at all."""
    books = {k: pair[k] for k in _BOOK_FIELDS if k in pair}
    if not books:
        return pair, None
    slim = {k: v for k, v in pair.items() if k not in _BOOK_FIELDS}
    return slim, books


def _reattach_books(pair: dict, books_json: str | None) -> dict:
    if not books_json:
        return pair
    books = json.loads(books_json)
    return {**pair, **books}


def finish_scan(scan_id: int, pairs: list[dict], summary: dict | None, coverage: dict | None = None) -> None:
    """Bulk-inserts pair_snapshots and marks the scan completed, in one
    transaction. ``pairs`` order is preserved as ``rank`` (0-based index into
    the original list — a dropped duplicate leaves a gap, never a renumbering).

    Pairs sharing the same pair_key() would collide on pair_snapshots' PRIMARY
    KEY(scan_id, pair_key) and abort the whole insert; instead the first
    occurrence (lowest rank) is kept, later ones are dropped and counted in
    ``scans.duplicate_pair_keys`` so a discover-side regression is visible
    rather than silently failing persistence.
    """
    arb_count = sum(1 for p in pairs if (p.get("arb_net_profit") or 0) > 0)

    seen: set[str] = set()
    rows = []
    duplicate_count = 0
    for rank, pair in enumerate(pairs):
        key = pair_key(pair)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        slim, books = _split_books(pair)
        rows.append((
            scan_id,
            key,
            rank,
            pair.get("kalshi_ticker"),
            pair.get("poly_token_id"),
            pair.get("poly_id"),
            pair.get("category"),
            pair.get("arb_net_accurate") if pair.get("arb_net_accurate") is not None else pair.get("arb_net_profit"),
            pair.get("exec_profit"),
            pair.get("exec_contracts"),
            pair.get("kalshi_close") or pair.get("close_time") or pair.get("poly_close"),
            json.dumps(slim),
            json.dumps(books) if books is not None else None,
        ))

    with closing(connect()) as conn, conn:
        conn.execute(
            """UPDATE scans SET finished_at = ?, status = 'completed',
                   pair_count = ?, arb_count = ?, summary_json = ?, coverage_json = ?,
                   duplicate_pair_keys = ?
               WHERE id = ?""",
            (_now(), len(pairs), arb_count,
             json.dumps(summary) if summary is not None else None,
             json.dumps(coverage) if coverage is not None else None,
             duplicate_count,
             scan_id),
        )
        conn.execute("DELETE FROM pair_snapshots WHERE scan_id = ?", (scan_id,))
        conn.executemany(
            """INSERT INTO pair_snapshots
                   (scan_id, pair_key, rank, kalshi_ticker, poly_token_id, poly_id,
                    category, net, exec_profit, exec_contracts, close_time,
                    pair_json, books_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        mode_row = conn.execute("SELECT mode FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if mode_row is not None:
            _trim_books_json(conn, mode_row["mode"], keep=BOOKS_KEEP_PER_MODE)

        # Reclaim the pages just freed by trimming/deleting -- a no-op unless
        # the DB is in auto_vacuum=INCREMENTAL mode (set on creation by
        # init_db()); without this, NULLing books_json leaves freed pages in
        # sqlite's freelist for reuse but never shrinks the file on disk.
        try:
            conn.execute("PRAGMA incremental_vacuum")
        except sqlite3.OperationalError:
            pass


def _trim_books_json(conn: sqlite3.Connection, mode: str, keep: int) -> None:
    """NULLs out books_json for pair_snapshots belonging to all but the
    ``keep`` most recent completed scans of ``mode`` — the order-book ladders
    are the bulk of a snapshot's size and are only useful for restoring the
    UI's most recent view; every other field (rank, prices, edges, matcher
    reasons, ...) is retained indefinitely."""
    rows = conn.execute(
        "SELECT id FROM scans WHERE status = 'completed' AND mode = ? ORDER BY id DESC",
        (mode,),
    ).fetchall()
    stale_ids = [r["id"] for r in rows[keep:]]
    if not stale_ids:
        return
    placeholders = ",".join("?" * len(stale_ids))
    conn.execute(
        f"UPDATE pair_snapshots SET books_json = NULL WHERE scan_id IN ({placeholders})",
        stale_ids,
    )


def fail_scan(scan_id: int, error: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            """UPDATE scans SET finished_at = ?, status = 'failed', error = ?
               WHERE id = ?""",
            (_now(), error, scan_id),
        )


def _scan_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json")) if d.get("params_json") else None
    d["coverage"] = json.loads(d.pop("coverage_json")) if d.get("coverage_json") else None
    d["summary"] = json.loads(d.pop("summary_json")) if d.get("summary_json") else None
    return d


def _load_pairs(conn: sqlite3.Connection, scan_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT pair_json, books_json FROM pair_snapshots WHERE scan_id = ? ORDER BY rank ASC",
        (scan_id,),
    ).fetchall()
    return [_reattach_books(json.loads(r["pair_json"]), r["books_json"]) for r in rows]


def latest_scan(mode: str | None = None, status: str = "completed") -> dict | None:
    with closing(connect()) as conn:
        query = "SELECT * FROM scans WHERE status = ?"
        params: list[Any] = [status]
        if mode is not None:
            query += " AND mode = ?"
            params.append(mode)
        query += " ORDER BY id DESC LIMIT 1"
        row = conn.execute(query, params).fetchone()
        if row is None:
            return None
        result = _scan_row_to_dict(row)
        result["pairs"] = _load_pairs(conn, result["id"])
        return result


def list_scans(limit: int = 20) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_scan_row_to_dict(r) for r in rows]


def get_scan(scan_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if row is None:
            return None
        result = _scan_row_to_dict(row)
        result["pairs"] = _load_pairs(conn, scan_id)
        return result


def prune_scans(keep: int = 20) -> int:
    """Deletes the oldest completed/failed scans beyond ``keep`` (by id,
    newest first). Running/cancelled scans are never pruned by this. Returns
    the number of scans deleted."""
    with closing(connect()) as conn, conn:
        rows = conn.execute(
            """SELECT id FROM scans WHERE status IN ('completed','failed')
               ORDER BY id DESC""",
        ).fetchall()
        stale_ids = [r["id"] for r in rows[keep:]]
        if not stale_ids:
            return 0
        placeholders = ",".join("?" * len(stale_ids))
        conn.execute(f"DELETE FROM scans WHERE id IN ({placeholders})", stale_ids)
        return len(stale_ids)


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------

def add_review(pair_key_: str, verdict: str, reason: str | None = None,
               reviewer: str = "local", kalshi_rules_hash: str | None = None,
               poly_rules_hash: str | None = None) -> dict:
    if not pair_key_:
        raise ValueError("pair_key is required")
    if verdict not in _VALID_VERDICT:
        raise ValueError(f"invalid verdict: {verdict!r}")
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            """INSERT INTO reviews
                   (pair_key, verdict, reason, reviewer, created_at,
                    kalshi_rules_hash, poly_rules_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (pair_key_, verdict, reason, reviewer, _now(),
             kalshi_rules_hash, poly_rules_hash),
        )
        row = conn.execute("SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def current_reviews(pair_keys: list[str] | None = None) -> dict[str, dict]:
    """Latest review per pair_key (by created_at, ties broken by id). Limited
    to 500 pairs when ``pair_keys`` is not given."""
    with closing(connect()) as conn:
        if pair_keys is not None:
            if not pair_keys:
                return {}
            placeholders = ",".join("?" * len(pair_keys))
            rows = conn.execute(
                f"""SELECT r.* FROM reviews r
                    INNER JOIN (
                        SELECT pair_key, MAX(created_at) AS max_created, MAX(id) AS max_id
                        FROM reviews WHERE pair_key IN ({placeholders})
                        GROUP BY pair_key
                    ) latest
                    ON r.pair_key = latest.pair_key
                       AND r.created_at = latest.max_created
                       AND r.id = latest.max_id""",
                pair_keys,
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT r.* FROM reviews r
                   INNER JOIN (
                       SELECT pair_key, MAX(created_at) AS max_created, MAX(id) AS max_id
                       FROM reviews GROUP BY pair_key
                   ) latest
                   ON r.pair_key = latest.pair_key
                      AND r.created_at = latest.max_created
                      AND r.id = latest.max_id
                   LIMIT 500""",
            ).fetchall()
        return {r["pair_key"]: dict(r) for r in rows}


def review_history(pair_key_: str) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM reviews WHERE pair_key = ? ORDER BY created_at ASC, id ASC",
            (pair_key_,),
        ).fetchall()
        return [dict(r) for r in rows]

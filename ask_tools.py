"""Read-only, typed tools for the Ask assistant (Phase 3a).

Model-agnostic: nothing here calls an LLM. Each tool has an OpenAI
function-calling schema (``TOOLS``) and a Python implementation that validates
its arguments strictly (unknown keys / bad types rejected, limits clamped) and
returns a JSON-serializable dict that ALWAYS carries provenance::

    {"scan": {"id", "finished_at", "mode"} | None, "inputs": {...validated args}}

Safety: tools only read the local store (plus ``get_pair_evidence``, which may
fetch venue rule text exactly like ``/api/pairs/{key}/evidence``). There is no
SQL, shell, file, order-placement or live-book capability. ``run_tool`` never
raises: failures come back as ``{"ok": False, "error": ...}``.

Rule/market text returned by these tools is UNTRUSTED evidence, never
instructions (see ask.py for how it is fenced off from the model).
"""
from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable

import rule_search
import store
from rule_diff import compare_rules

logger = logging.getLogger(__name__)

RULES_TEXT_CAP = 2000
MAX_SEARCH_LIMIT = 50
DEFAULT_SEARCH_LIMIT = 10
MAX_RULE_SEARCH_LIMIT = 10
COMPARE_LIST_CAP = 50
COMPARE_CHANGED_CAP = 20
USD_MIN, USD_MAX = 1, 100_000
REVIEW_STATES = ("match", "mismatch", "uncertain", "unreviewed")
SORTS = ("net", "exec_profit", "exec_contracts", "close")
VENUES = ("kalshi", "polymarket")
_PAIR_KEY_MAX = 400
_REVIEW_CHUNK = 400


class ToolError(ValueError):
    """Invalid tool arguments (reported to the model as ok=false)."""


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "search_pairs",
        "description": (
            "Search matched Kalshi/Polymarket pairs in the latest completed scan, joined with "
            "human review verdicts. Returns net edge, executable profit/contracts, close dates, "
            "days to settle and (when rule text is cached) settlement-rule conflict flags."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Exact category name."},
                "review_state": {"type": "string", "enum": list(REVIEW_STATES)},
                "min_depth_contracts": {"type": "number", "description": "Minimum executable contracts."},
                "min_net": {"type": "number", "description": "Minimum net edge per $1 contract."},
                "max_days_to_settle": {"type": "number", "description": "Max days until the earlier close."},
                "has_rule_flags": {"type": "boolean", "description": "Only pairs with (true) / without (false) rule-conflict flags; unknown counts as not flagged."},
                "sort": {"type": "string", "enum": list(SORTS), "description": "Default net (desc); close sorts soonest first."},
                "limit": {"type": "integer", "description": f"1-{MAX_SEARCH_LIMIT}, default {DEFAULT_SEARCH_LIMIT}."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_pair_evidence",
        "description": (
            "Settlement-rule text (each venue truncated to 2000 chars), source URL, fetch time, "
            "hash, staleness and rule-conflict flags for one pair."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pair_key": {"type": "string", "description": "kalshi_ticker|poly id, exactly as returned by search_pairs."},
                "refresh": {"type": "boolean", "description": "Force a re-fetch of the rule text."},
            },
            "required": ["pair_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "calculate_budget",
        "description": (
            "What a USD budget buys on one pair, walking the STORED order books of the latest scan "
            "(never live books). Returns status books_unavailable if the books were trimmed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pair_key": {"type": "string"},
                "usd": {"type": "number", "description": f"{USD_MIN}-{USD_MAX}."},
            },
            "required": ["pair_key", "usd"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compare_scans",
        "description": (
            "Diff two completed scans: pairs added/removed and the largest net-edge changes. "
            "Defaults to the latest completed scan versus the one before it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "Older scan id."},
                "b": {"type": "integer", "description": "Newer scan id."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_rules",
        "description": "Full-text keyword search over cached settlement-rule text. Returns highlighted snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Keywords (max 200 chars)."},
                "limit": {"type": "integer", "description": f"1-{MAX_RULE_SEARCH_LIMIT}, default 5."},
                "venue": {"type": "string", "enum": list(VENUES)},
            },
            "required": ["q"],
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _check_keys(args: Any, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(unknown)}")
    missing = sorted(k for k in required if args.get(k) is None)
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")
    return {k: v for k, v in args.items() if v is not None}


def _num(args: dict, key: str) -> float | None:
    if key not in args:
        return None
    v = args[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        raise ToolError(f"{key} must be a finite number")
    return float(v)


def _int(args: dict, key: str) -> int | None:
    if key not in args:
        return None
    v = args[key]
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        raise ToolError(f"{key} must be an integer")
    return v


def _bool(args: dict, key: str) -> bool | None:
    if key not in args:
        return None
    if not isinstance(args[key], bool):
        raise ToolError(f"{key} must be a boolean")
    return args[key]


def _str(args: dict, key: str, max_len: int, choices: tuple[str, ...] | None = None) -> str | None:
    if key not in args:
        return None
    v = args[key]
    if not isinstance(v, str):
        raise ToolError(f"{key} must be a string")
    v = v.strip()
    if not v or len(v) > max_len:
        raise ToolError(f"{key} must be 1-{max_len} characters")
    if choices is not None and v not in choices:
        raise ToolError(f"{key} must be one of: {', '.join(choices)}")
    return v


def _clamp(v: int | None, lo: int, hi: int, default: int) -> int:
    if v is None:
        return default
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _scan_meta(scan: dict | None) -> dict | None:
    if not scan:
        return None
    return {"id": scan.get("id"), "finished_at": scan.get("finished_at"), "mode": scan.get("mode")}


def _net(pair: dict) -> float | None:
    v = pair.get("arb_net_accurate")
    if v is None:
        v = pair.get("arb_net_profit")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _days_to_settle(pair: dict, now: datetime) -> float | None:
    closes = [t for t in (_parse_ts(pair.get("kalshi_close")), _parse_ts(pair.get("poly_close"))) if t]
    if not closes:
        return None
    return round((min(closes) - now).total_seconds() / 86400.0, 2)


def _close_str(pair: dict) -> str | None:
    return pair.get("kalshi_close") or pair.get("close_time") or pair.get("poly_close")


def _cached_rule_flags(pair: dict) -> list[dict] | None:
    """Flags from CACHED evidence only; None when either side isn't cached."""
    k_id, p_id = pair.get("kalshi_ticker"), pair.get("poly_id")
    if not k_id or not p_id:
        return None
    k_ev = store.latest_evidence("kalshi", k_id)
    p_ev = store.latest_evidence("polymarket", p_id)
    if k_ev is None or p_ev is None:
        return None
    flags = compare_rules(k_ev.get("rules_text"), p_ev.get("rules_text"))
    return [{"kind": f.get("kind"), "severity": f.get("severity"), "detail": f.get("detail")} for f in flags]


def _reviews_for(keys: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(keys), _REVIEW_CHUNK):
        out.update(store.current_reviews(keys[i:i + _REVIEW_CHUNK]))
    return out


def _latest_scan() -> dict | None:
    return store.latest_scan()


# ---------------------------------------------------------------------------
# Tool: search_pairs
# ---------------------------------------------------------------------------

_SORT_KEYS: dict[str, Callable[[dict], Any]] = {
    "net": lambda r: -(r["net"] if r["net"] is not None else float("-inf")),
    "exec_profit": lambda r: -(r["exec_profit"] if r["exec_profit"] is not None else float("-inf")),
    "exec_contracts": lambda r: -(r["exec_contracts"] if r["exec_contracts"] is not None else float("-inf")),
    "close": lambda r: (r["_close_ts"] is None, r["_close_ts"] or 0),
}


def search_pairs(args: dict) -> dict:
    a = _check_keys(args, {"category", "review_state", "min_depth_contracts", "min_net",
                           "max_days_to_settle", "has_rule_flags", "sort", "limit"})
    category = _str(a, "category", 200)
    review_state = _str(a, "review_state", 20, REVIEW_STATES)
    min_depth = _num(a, "min_depth_contracts")
    min_net = _num(a, "min_net")
    max_days = _num(a, "max_days_to_settle")
    has_flags = _bool(a, "has_rule_flags")
    sort = _str(a, "sort", 20, SORTS) or "net"
    limit = _clamp(_int(a, "limit"), 1, MAX_SEARCH_LIMIT, DEFAULT_SEARCH_LIMIT)
    inputs = {"category": category, "review_state": review_state, "min_depth_contracts": min_depth,
              "min_net": min_net, "max_days_to_settle": max_days, "has_rule_flags": has_flags,
              "sort": sort, "limit": limit}

    scan = _latest_scan()
    if scan is None:
        return {"scan": None, "inputs": inputs, "status": "no_scan", "total_candidates": 0, "pairs": []}

    now = _now()
    pairs = scan["pairs"]
    keyed = [(store.pair_key(p), p) for p in pairs]
    reviews = _reviews_for([k for k, _ in keyed])

    rows: list[dict] = []
    for key, p in keyed:
        if category is not None and p.get("category") != category:
            continue
        net = _net(p)
        if min_net is not None and (net is None or net < min_net):
            continue
        contracts = p.get("exec_contracts")
        if min_depth is not None and not (isinstance(contracts, (int, float)) and contracts >= min_depth):
            continue
        days = _days_to_settle(p, now)
        if max_days is not None and (days is None or days > max_days):
            continue
        rev = reviews.get(key)
        verdict = rev["verdict"] if rev else "unreviewed"
        if review_state is not None and verdict != review_state:
            continue
        closes = [t for t in (_parse_ts(p.get("kalshi_close")), _parse_ts(p.get("poly_close"))) if t]
        rows.append({
            "pair_key": key,
            "kalshi_title": p.get("kalshi_title"),
            "poly_title": p.get("poly_title"),
            "category": p.get("category"),
            "net": net,
            "exec_profit": p.get("exec_profit"),
            "exec_contracts": contracts,
            "kalshi_close": p.get("kalshi_close"),
            "poly_close": p.get("poly_close"),
            "days_to_settle": days,
            "review": verdict,
            "rule_flags": None,
            "_close_ts": min(closes).timestamp() if closes else None,
        })

    rows.sort(key=_SORT_KEYS[sort])
    total = len(rows)

    out: list[dict] = []
    by_key = dict(keyed)
    for r in rows:
        if len(out) >= limit:
            break
        flags = _cached_rule_flags(by_key[r["pair_key"]])
        r["rule_flags"] = flags
        flagged = bool(flags)
        if has_flags is True and not flagged:
            continue
        if has_flags is False and flagged:
            continue
        r.pop("_close_ts", None)
        out.append(r)

    return {"scan": _scan_meta(scan), "inputs": inputs, "status": "ok",
            "total_candidates": total, "pairs": out}


# ---------------------------------------------------------------------------
# Tool: get_pair_evidence
# ---------------------------------------------------------------------------

def _evidence_side(side: dict | None) -> dict | None:
    if side is None:
        return None
    text = side.get("rules_text") or ""
    return {
        "rules_text": text[:RULES_TEXT_CAP],
        "truncated": len(text) > RULES_TEXT_CAP,
        "source_url": side.get("source_url"),
        "fetched_at": side.get("fetched_at"),
        "hash": side.get("hash"),
        "stale": bool(side.get("stale")),
    }


def get_pair_evidence(args: dict) -> dict:
    a = _check_keys(args, {"pair_key", "refresh"}, {"pair_key"})
    pair_key = _str(a, "pair_key", _PAIR_KEY_MAX)
    refresh = bool(_bool(a, "refresh"))
    inputs = {"pair_key": pair_key, "refresh": refresh}
    scan = _scan_meta(_latest_scan_meta_only())

    import server  # lazy: server imports ask -> ask_tools

    payload = server.pair_evidence_payload(pair_key, refresh)
    if payload is None:
        return {"scan": scan, "inputs": inputs, "status": "pair_not_found", "pair_key": pair_key}
    kalshi, poly = _evidence_side(payload["kalshi"]), _evidence_side(payload["poly"])
    status = "ok" if (kalshi and poly) else "evidence_missing"
    if kalshi is None and poly is None:
        status = "evidence_missing"
    return {"scan": scan, "inputs": inputs, "status": status, "pair_key": pair_key,
            "kalshi": kalshi, "poly": poly, "rule_flags": payload["rule_flags"]}


def _latest_scan_meta_only() -> dict | None:
    """Latest completed scan row without loading its pairs."""
    with closing(store.connect()) as conn:
        row = conn.execute(
            "SELECT id, finished_at, mode FROM scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Tool: calculate_budget
# ---------------------------------------------------------------------------

def calculate_budget(args: dict) -> dict:
    a = _check_keys(args, {"pair_key", "usd"}, {"pair_key", "usd"})
    pair_key = _str(a, "pair_key", _PAIR_KEY_MAX)
    usd = _num(a, "usd")
    if not (USD_MIN <= usd <= USD_MAX):
        raise ToolError(f"usd must be between {USD_MIN} and {USD_MAX}")
    usd_val: int | float = int(usd) if usd.is_integer() else usd
    inputs = {"pair_key": pair_key, "usd": usd_val}

    scan = _latest_scan()
    if scan is None:
        return {"scan": None, "inputs": inputs, "status": "no_scan"}
    meta = _scan_meta(scan)
    pair = next((p for p in scan["pairs"] if store.pair_key(p) == pair_key), None)
    if pair is None:
        return {"scan": meta, "inputs": inputs, "status": "pair_not_found", "pair_key": pair_key}
    if not (pair.get("poly_book") or pair.get("kalshi_book")):
        return {"scan": meta, "inputs": inputs, "status": "books_unavailable", "pair_key": pair_key}

    import server  # lazy

    result = server._compute_book_arb(
        server._book_from_lists(pair.get("poly_book")),
        server._book_from_lists(pair.get("kalshi_book")),
        budgets=(usd_val,),
    )
    # Drop the bulky depth curve / ladder dumps; fills and breakeven are what answers need.
    directions = {
        d: {k: v for k, v in res.items() if k not in ("curve", "ladders")}
        for d, res in result["directions"].items()
    }
    finished = _parse_ts(scan.get("finished_at"))
    age = round((_now() - finished).total_seconds(), 1) if finished else None
    return {"scan": meta, "inputs": inputs, "status": "ok", "pair_key": pair_key,
            "budget_key": str(usd_val), "directions": directions,
            "best_direction": result["best_direction"],
            "books_from_scan_id": scan["id"], "books_age_seconds": age}


# ---------------------------------------------------------------------------
# Tool: compare_scans
# ---------------------------------------------------------------------------

def _completed_scan_ids(mode: str | None = None) -> list[int]:
    with closing(store.connect()) as conn:
        sql, params = "SELECT id FROM scans WHERE status = 'completed'", []
        if mode is not None:
            sql += " AND mode = ?"
            params.append(mode)
        return [r["id"] for r in conn.execute(sql + " ORDER BY id DESC", params).fetchall()]


def _net_map(scan: dict) -> dict[str, float | None]:
    return {store.pair_key(p): _net(p) for p in scan["pairs"]}


def compare_scans(args: dict) -> dict:
    a_args = _check_keys(args, {"a", "b"})
    a_id, b_id = _int(a_args, "a"), _int(a_args, "b")
    inputs = {"a": a_id, "b": b_id}

    if b_id is None:
        ids = _completed_scan_ids()
        if not ids:
            return {"scan": None, "inputs": inputs, "status": "no_scan"}
        b_id = ids[0]
    b = store.get_scan(b_id)
    if b is None or b.get("status") != "completed":
        return {"scan": None, "inputs": inputs, "status": "scan_not_found", "missing": b_id}
    if a_id is None:
        earlier = [i for i in _completed_scan_ids(b.get("mode")) if i < b_id]
        if not earlier:
            return {"scan": _scan_meta(b), "inputs": inputs, "status": "no_previous_scan"}
        a_id = earlier[0]
    a = store.get_scan(a_id)
    if a is None or a.get("status") != "completed":
        return {"scan": _scan_meta(b), "inputs": inputs, "status": "scan_not_found", "missing": a_id}

    inputs = {"a": a_id, "b": b_id}
    na, nb = _net_map(a), _net_map(b)
    added = sorted(set(nb) - set(na))
    removed = sorted(set(na) - set(nb))
    changed = []
    for key in set(na) & set(nb):
        if na[key] is None or nb[key] is None:
            continue
        delta = round(nb[key] - na[key], 6)
        if delta:
            changed.append({"pair_key": key, "net_a": na[key], "net_b": nb[key], "delta": delta})
    changed.sort(key=lambda r: (-abs(r["delta"]), r["pair_key"]))
    return {
        "scan": _scan_meta(b), "scan_a": _scan_meta(a), "inputs": inputs, "status": "ok",
        "added_count": len(added), "removed_count": len(removed), "changed_count": len(changed),
        "added": added[:COMPARE_LIST_CAP], "removed": removed[:COMPARE_LIST_CAP],
        "top_changed": changed[:COMPARE_CHANGED_CAP],
    }


# ---------------------------------------------------------------------------
# Tool: search_rules
# ---------------------------------------------------------------------------

def search_rules(args: dict) -> dict:
    a = _check_keys(args, {"q", "limit", "venue"}, {"q"})
    q = _str(a, "q", rule_search.MAX_QUERY_CHARS)
    limit = _clamp(_int(a, "limit"), 1, MAX_RULE_SEARCH_LIMIT, 5)
    venue = _str(a, "venue", 20, VENUES)
    inputs = {"q": q, "limit": limit, "venue": venue}
    scan = _scan_meta(_latest_scan_meta_only())
    try:
        results = rule_search.search(q, limit=limit, venue=venue)
    except rule_search.SearchUnavailable:
        return {"scan": scan, "inputs": inputs, "status": "unavailable", "results": []}
    rows = [{
        "venue": r.get("venue"), "market_id": r.get("market_id"), "snippet": r.get("snippet"),
        "source_url": r.get("source_url"), "fetched_at": r.get("fetched_at"),
        "hash": r.get("content_hash"), "pair_keys": r.get("pair_keys") or [],
    } for r in results]
    return {"scan": scan, "inputs": inputs, "status": "ok", "results": rows}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_IMPLS: dict[str, Callable[[dict], dict]] = {
    "search_pairs": search_pairs,
    "get_pair_evidence": get_pair_evidence,
    "calculate_budget": calculate_budget,
    "compare_scans": compare_scans,
    "search_rules": search_rules,
}


def run_tool(name: str, args: Any) -> dict:
    """Run one tool. Never raises: errors become ``{"ok": False, "error": ...}``."""
    impl = _IMPLS.get(name) if isinstance(name, str) else None
    if impl is None:
        return {"ok": False, "error": f"unknown tool: {name!r}"}
    try:
        result = impl(args)
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception("ask tool %s failed", name)
        return {"ok": False, "error": "tool failed internally"}
    result.setdefault("ok", True)
    return result

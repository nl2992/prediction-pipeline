"""Read-only digest of the DeepSeek verifier's verdict log (ai_verify.jsonl).

The alerter appends one JSON object per verdict; nothing read it back until now.
This turns that append-only log into actionable signal — most usefully the
RECURRING FLAGGED PAIRS: pairs the AI keeps judging NOT identical even though the
rule matcher keeps proposing them. Those are likely matcher false-positives worth
fixing upstream (matcher v1 / contract_spec v2).

Pure stdlib, read-only: no network, no API calls, no effect on the alerting path.
CLI: python ai_verify_report.py [path-to-ai_verify.jsonl]
"""
from __future__ import annotations

import json
import pathlib
import sys

_LOG = pathlib.Path(__file__).resolve().parent / "ai_verify.jsonl"


def load_verdicts(path: pathlib.Path | str = _LOG) -> list[dict]:
    """Return all verdict rows from the JSONL log (missing file -> []). Malformed
    lines are skipped so one bad write never breaks the report."""
    out: list[dict] = []
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _pair(v: dict) -> tuple[str, str]:
    return (v.get("poly") or "", v.get("kalshi") or "")


# Known test sentinels written before the test-isolation fix (#5) — never real pairs.
_TEST_SENTINELS = frozenset({"PHANTOM", "REAL", "A", "B", "C", "D"})


def _is_test(key: tuple[str, str]) -> bool:
    return key[0] in _TEST_SENTINELS


def summarize(verdicts: list[dict]) -> dict:
    """Aggregate the verdict rows into totals + CURRENTLY flagged pairs.

    A pair whose LATEST verdict is same=True is resolved and must not be reported as
    a current matcher false-positive — counting all-time flags let fixed pairs (e.g.
    House races resolved by the v3 prompt) dominate (#12). So we key off each pair's
    latest verdict by ts and only list pairs still flagged, with their historical
    flag count for context. Returns: total, unique_pairs, n_confirmed, n_flagged,
    pct_confirmed, flagged_pairs ({poly, kalshi, count, reason, last_ts}, currently
    flagged only), recent_flags (latest flagged verdicts, newest first)."""
    total = len(verdicts)
    pairs: dict[tuple[str, str], dict] = {}
    n_confirmed = 0
    for v in verdicts:
        key = _pair(v)
        e = pairs.setdefault(key, {"poly": key[0], "kalshi": key[1], "count": 0,
                                   "latest_ts": "", "latest_same": None,
                                   "latest_same_event": None, "reason": ""})
        same = bool(v.get("same"))
        if same:
            n_confirmed += 1
        else:
            e["count"] += 1
        ts = v.get("ts", "")
        if ts >= e["latest_ts"]:          # rows are ~chronological; track the latest
            e["latest_ts"] = ts
            e["latest_same"] = same
            e["latest_same_event"] = v.get("same_event")
            e["last_ts"] = ts
            if not same:
                e["reason"] = v.get("reason", "")
    n_flagged = total - n_confirmed
    # Currently flagged = latest verdict is different, excluding test sentinels.
    flagged_pairs = sorted(
        (e for k, e in pairs.items() if e["latest_same"] is False and not _is_test(k)),
        key=lambda e: (e["count"], e.get("last_ts", "")), reverse=True)
    # Partition by failure mode (both fields are in the verdict log): a different
    # underlying event is a MATCHER false-positive; same event but different
    # settlement is a CORRECT enforce drop, not a matcher bug (#14).
    matcher_false_positives = [e for e in flagged_pairs if e["latest_same_event"] is False]
    settlement_mismatches = [e for e in flagged_pairs if e["latest_same_event"] is not False]
    recent_flags = sorted(
        (v for v in verdicts if not v.get("same") and not _is_test(_pair(v))),
        key=lambda v: v.get("ts", ""), reverse=True)
    return {
        "total": total,
        "unique_pairs": len(pairs),
        "n_confirmed": n_confirmed,
        "n_flagged": n_flagged,
        "pct_confirmed": (100.0 * n_confirmed / total) if total else 0.0,
        "flagged_pairs": flagged_pairs,
        "matcher_false_positives": matcher_false_positives,
        "settlement_mismatches": settlement_mismatches,
        "recent_flags": recent_flags,
    }


def format_report(s: dict, recent_n: int = 8) -> str:
    lines = [
        "DeepSeek verifier - verdict-log digest",
        "=" * 44,
        f"verdicts logged : {s['total']}  ({s['unique_pairs']} unique pairs)",
        f"confirmed same  : {s['n_confirmed']}  ({s['pct_confirmed']:.0f}%)",
        f"flagged different: {s['n_flagged']}",
    ]
    def _group(title: str, pairs: list[dict]) -> None:
        if not pairs:
            return
        lines.append("")
        lines.append(title)
        for e in pairs:
            lines.append(f"  [{e['count']}x] {e['poly'][:40]} <-> {e['kalshi'][:40]}")
            if e.get("reason"):
                lines.append(f"        reason: {e['reason'][:90]}")
    _group("Different-event pairs (MATCHER false-positives — fix upstream):",
           s["matcher_false_positives"])
    _group("Same-event, different-settlement (correct enforce drops, not matcher bugs):",
           s["settlement_mismatches"])
    rf = s["recent_flags"][:recent_n]
    if rf:
        lines.append("")
        lines.append(f"Most recent flags (newest first, up to {recent_n}):")
        for v in rf:
            lines.append(f"  {v.get('ts','')[:19]}  {(v.get('poly') or '')[:36]} <-> {(v.get('kalshi') or '')[:36]}")
    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else _LOG
    verdicts = load_verdicts(path)
    print(format_report(summarize(verdicts)))

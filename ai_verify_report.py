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


def summarize(verdicts: list[dict]) -> dict:
    """Aggregate the verdict rows into totals + recurring flagged pairs.

    Returns: total, unique_pairs, n_confirmed, n_flagged, pct_confirmed,
    flagged_pairs (list of {poly, kalshi, count, reason(latest), last_ts} sorted
    by count desc then most recent), recent_flags (latest flags, newest first)."""
    total = len(verdicts)
    pairs: dict[tuple[str, str], dict] = {}
    n_confirmed = 0
    flagged_latest: dict[tuple[str, str], dict] = {}
    for v in verdicts:
        key = _pair(v)
        pairs.setdefault(key, {})
        if v.get("same"):
            n_confirmed += 1
        else:
            e = flagged_latest.get(key, {"poly": key[0], "kalshi": key[1], "count": 0})
            e["count"] += 1
            # rows are appended chronologically, so the last seen is the latest
            e["reason"] = v.get("reason", "")
            e["last_ts"] = v.get("ts", "")
            flagged_latest[key] = e
    n_flagged = total - n_confirmed
    # most-frequently-flagged first; break ties newest-first
    flagged_pairs = sorted(flagged_latest.values(),
                           key=lambda e: (e["count"], e.get("last_ts", "")), reverse=True)
    recent_flags = sorted((v for v in verdicts if not v.get("same")),
                          key=lambda v: v.get("ts", ""), reverse=True)
    return {
        "total": total,
        "unique_pairs": len(pairs),
        "n_confirmed": n_confirmed,
        "n_flagged": n_flagged,
        "pct_confirmed": (100.0 * n_confirmed / total) if total else 0.0,
        "flagged_pairs": flagged_pairs,
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
    fp = s["flagged_pairs"]
    if fp:
        lines.append("")
        lines.append("Recurring flagged pairs (likely matcher false-positives):")
        for e in fp:
            lines.append(f"  [{e['count']}x] {e['poly'][:40]} <-> {e['kalshi'][:40]}")
            if e.get("reason"):
                lines.append(f"        reason: {e['reason'][:90]}")
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

"""Read-only digest of the emitted-signal log (alert_signals.jsonl).

The alerter appends every emailed arb here; nothing read it back. This turns it
into decision support for manual execution: which pairs RECUR (persistent
opportunities worth the effort) and which have been RICHEST (max net edge seen).

Pure stdlib, read-only: no network, no effect on the alerting path.
CLI: python signal_report.py [path-to-alert_signals.jsonl]
"""
from __future__ import annotations

import json
import pathlib
import sys

_LOG = pathlib.Path(__file__).resolve().parent / "alert_signals.jsonl"


def load_signals(path: pathlib.Path | str = _LOG) -> list[dict]:
    """All signal rows from the JSONL log (missing file -> []); malformed lines skipped."""
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


def _net(s: dict) -> float:
    try:
        return float(s.get("net_accurate") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def summarize(signals: list[dict], top_n: int = 10) -> dict:
    """Group emitted signals by pair (the canonical ``key``) and rank them.

    Returns: total, unique_pairs, most_recurring (by count desc), richest (by the
    max net edge ever seen for the pair). Each entry: {poly, kalshi, count, max_net,
    latest_net, latest_ts}."""
    pairs: dict = {}
    for s in signals:
        key = s.get("key") or (s.get("poly_title", ""), s.get("kalshi_title", ""))
        net = _net(s)
        ts = s.get("ts", "")
        e = pairs.setdefault(key, {
            "poly": s.get("poly_title", ""), "kalshi": s.get("kalshi_title", ""),
            "count": 0, "max_net": 0.0, "latest_net": 0.0, "latest_ts": "",
        })
        e["count"] += 1
        e["max_net"] = max(e["max_net"], net)
        if ts >= e["latest_ts"]:          # rows ~chronological; keep the latest
            e["latest_ts"] = ts
            e["latest_net"] = net
    vals = list(pairs.values())
    most_recurring = sorted(vals, key=lambda e: (e["count"], e["max_net"]), reverse=True)[:top_n]
    richest = sorted(vals, key=lambda e: e["max_net"], reverse=True)[:top_n]
    return {"total": len(signals), "unique_pairs": len(pairs),
            "most_recurring": most_recurring, "richest": richest}


def format_report(s: dict) -> str:
    lines = ["Emitted-signal log digest", "=" * 40,
             f"signals logged : {s['total']}  ({s['unique_pairs']} unique pairs)"]
    if s["most_recurring"]:
        lines.append("")
        lines.append("Most recurring arbs (persistent opportunities):")
        for e in s["most_recurring"]:
            lines.append(f"  [{e['count']}x] max {e['max_net']*100:.1f}c, latest {e['latest_net']*100:.1f}c  "
                         f"{e['poly'][:30]} <-> {e['kalshi'][:30]}")
    if s["richest"]:
        lines.append("")
        lines.append("Richest arbs (max net edge seen):")
        for e in s["richest"]:
            lines.append(f"  {e['max_net']*100:5.1f}c [{e['count']}x]  "
                         f"{e['poly'][:30]} <-> {e['kalshi'][:30]}")
    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else _LOG
    print(format_report(summarize(load_signals(path))))

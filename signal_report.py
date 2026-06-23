"""Read-only digest of the emitted-signal log (alert_signals.jsonl).

The alerter appends every emailed arb here; nothing read it back. This turns it
into decision support for manual execution: which pairs RECUR (persistent
opportunities worth the effort) and which have been RICHEST (max net edge seen).

Pure stdlib, read-only: no network, no effect on the alerting path.
CLI: python signal_report.py [path-to-alert_signals.jsonl]
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys

_LOG = pathlib.Path(__file__).resolve().parent / "alert_signals.jsonl"
_RECENT_HOURS_DEFAULT = 24.0   # "act on" window for the best-annualised view


def _age_hours(ts: str, now: _dt.datetime) -> float | None:
    """Hours between an ISO ts and ``now`` (UTC); None if missing/unparseable."""
    if not ts:
        return None
    try:
        d = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return (now - d).total_seconds() / 3600.0
    except (ValueError, AttributeError):
        return None


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


def _annualised(signal: dict) -> float:
    """Annualised return of a signal via the alerter's own horizon logic (net x 365
    / days to the later close), so this digest ranks by the SAME metric the alerter
    prioritises. 0.0 if it can't be computed."""
    try:
        from alerter import _settle_horizon
        return _settle_horizon(signal)[0] or 0.0
    except Exception:
        return 0.0


def summarize(signals: list[dict], top_n: int = 10,
              now: _dt.datetime | None = None,
              recent_hours: float = _RECENT_HOURS_DEFAULT) -> dict:
    """Group emitted signals by pair (the canonical ``key``) and rank them.

    Returns: total, unique_pairs, most_recurring (count desc) and richest (max net
    edge) over ALL time, and best_annualised — the alerter's priority metric,
    restricted to pairs SEEN within ``recent_hours`` so it lists currently-actionable
    arbs, not vanished ones (#78). Each entry carries age_hours (since last seen)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    pairs: dict = {}
    for s in signals:
        key = s.get("key") or (s.get("poly_title", ""), s.get("kalshi_title", ""))
        net = _net(s)
        ts = s.get("ts", "")
        e = pairs.setdefault(key, {
            "poly": s.get("poly_title", ""), "kalshi": s.get("kalshi_title", ""),
            "count": 0, "max_net": 0.0, "latest_net": 0.0, "latest_ts": "",
            "annualised": 0.0,
        })
        e["count"] += 1
        e["max_net"] = max(e["max_net"], net)
        if ts >= e["latest_ts"]:          # rows ~chronological; keep the latest
            e["latest_ts"] = ts
            e["latest_net"] = net
            e["annualised"] = _annualised(s)   # annualised of the latest snapshot
    vals = list(pairs.values())
    for e in vals:
        e["age_hours"] = _age_hours(e["latest_ts"], now)
    most_recurring = sorted(vals, key=lambda e: (e["count"], e["max_net"]), reverse=True)[:top_n]
    richest = sorted(vals, key=lambda e: e["max_net"], reverse=True)[:top_n]
    # "act on" view: only pairs seen within the window are current opportunities.
    current = [e for e in vals if e["age_hours"] is not None and e["age_hours"] <= recent_hours]
    best_annualised = sorted(current, key=lambda e: e["annualised"], reverse=True)[:top_n]
    return {"total": len(signals), "unique_pairs": len(pairs), "recent_hours": recent_hours,
            "most_recurring": most_recurring, "richest": richest,
            "best_annualised": best_annualised}


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
    rh = s.get("recent_hours", _RECENT_HOURS_DEFAULT)
    if s.get("best_annualised"):
        lines.append("")
        lines.append(f"Best by annualised return — currently actionable (seen in last {rh:.0f}h):")
        for e in s["best_annualised"]:
            age = e.get("age_hours")
            seen = f", {age:.0f}h ago" if isinstance(age, (int, float)) else ""
            lines.append(f"  ~{e['annualised']*100:4.0f}% ann · latest {e['latest_net']*100:.1f}c{seen}  "
                         f"{e['poly'][:30]} <-> {e['kalshi'][:30]}")
    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else _LOG
    print(format_report(summarize(load_signals(path))))

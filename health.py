"""One-glance production health for the arb alerter — read-only.

Confirming the pipeline is alive used to require manual forensics (grep
alerter_cron.log by line number, check file mtimes, reconstruct cycles). This
parses the tail of alerter_cron.log + ai_verify.jsonl and prints a short status:
last scan, last email and how many scans since (vs the realert window), the
verifier heartbeat (#8) — flagging the silent-no-op case where emails went out
but the verifier never ran — and any recent CYCLE ERROR.

Pure stdlib, read-only: no network, no effect on the alerting path.
CLI: python health.py
"""
from __future__ import annotations

import pathlib
import re
import statistics
import sys

_SCAN_PAIRS_RE = re.compile(r"scan done.*?,\s*(\d+)\s+pairs,")  # only the scan-done line

# discover.py's per-venue "[coverage]" lines (see _print_kalshi_coverage /
# _print_polymarket_coverage), e.g.:
#   "      [coverage] Kalshi: 105,895/105,895 open markets ingested
#    (100.0%, incl. 18 orphans) · held out of matching: ... · excluded: ...
#    · sweep=fresh"
#   "      [coverage] Polymarket: 166,203/166,203 open markets ingested
#    (100.0%, incl. 442 orphans) · excluded: ... · sweep=fresh  [PARTIAL CATALOG]"
# Tolerant of either venue's line shape (Kalshi has an extra "held out of
# matching:" clause Polymarket doesn't); only the fields this module cares
# about (ingested/open/pct/sweep/partial) are captured.
_COVERAGE_RE = re.compile(
    r"\[coverage\]\s+(Kalshi|Polymarket):\s*"
    r"([\d,]+)/([\d,]+)\s+open markets ingested\s*"
    r"\(([\d.]+%|n/a)[^)]*\).*?"
    r"sweep=(\S+?)(?:\s*\[PARTIAL CATALOG\])?\s*$"
)
_SWEEP_OK = {"off", "cached", "fresh"}

BASE = pathlib.Path(__file__).resolve().parent
_LOG = BASE / "alerter_cron.log"
_VERDICTS = BASE / "ai_verify.jsonl"
# A cycle is ~90 log lines; the realert window is 6h (~25 cycles), so span enough
# lines that the last email is still visible (else "scans since email" is wrong).
_TAIL_LINES = 2600
# The scheduled task fires every 30 min. If the log file hasn't been written in
# this long, the task is almost certainly not firing (disabled, machine asleep,
# crash before logging) — content-based checks miss this since the stale lines
# remain. >2h ≈ 4 missed cycles (#126).
_LOG_STALE_HOURS = 2.0


def _tail(path: pathlib.Path, n: int, block: int = 1_200_000) -> list[str]:
    """Return the last ``n`` lines, reading only the final ``block`` bytes instead
    of the whole file — alerter_cron.log is unbounded (no rotation) and health.py
    runs often, so loading it all each time scales badly (#23). ``block`` (~1.2MB)
    comfortably holds far more than n short log lines."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - block))
            data = f.read()
    except Exception:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > block and lines:
        lines = lines[1:]          # first line is likely partial — drop it
    return lines[-n:]


def _parse_coverage_line(line: str) -> dict | None:
    """Parse one discover.py "[coverage]" line into a small dict, or None if
    the line doesn't match (old log format predating the coverage lines, or
    an unrelated line)."""
    m = _COVERAGE_RE.search(line)
    if not m:
        return None
    venue, ingested_s, open_s, pct_s, sweep = m.groups()
    pct = float(pct_s.rstrip("%")) if pct_s != "n/a" else None
    return {
        "venue": venue,
        "ingested": int(ingested_s.replace(",", "")),
        "open": int(open_s.replace(",", "")),
        "pct": pct,
        "sweep": sweep,
        "partial_catalog": "[PARTIAL CATALOG]" in line,
    }


def _coverage_degraded(cov: dict) -> bool:
    """True when a venue's coverage line signals a real gap: ingestion below
    100% (the line's own 1-decimal rounding already gives a tiny, printed
    tolerance — anything short of an exact "100.0%" is a real shortfall), a
    partial catalog, or a partial/failed sweep. "off" and "cached" sweeps are
    fine — they mean the sweep didn't need to run this cycle, not that it failed."""
    if cov.get("partial_catalog"):
        return True
    # Only the documented failure states degrade; an unrecognised future sweep
    # token is left alone rather than guessed at.
    if cov.get("sweep") in ("partial", "failed"):
        return True
    pct = cov.get("pct")
    if pct is not None and pct < 100.0:
        return True
    return False


def summarize_log(lines: list[str]) -> dict:
    """Scan recent log lines for lifecycle markers. Returns a dict describing the
    most recent scan/email/heartbeat/error and counts since the last email."""
    last_email_idx = -1
    last_email_subject = None
    last_scan = None
    last_heartbeat = None
    recent_cycle_error = None
    error_idx = -1
    email_fail = None
    email_fail_idx = -1
    scan_pair_counts: list[int] = []
    coverage_by_venue: dict[str, dict] = {}
    for i, ln in enumerate(lines):
        if "[coverage]" in ln:
            cov = _parse_coverage_line(ln)
            if cov:
                # Keep the LAST occurrence of each venue — i.e. the most
                # recent cycle's line for that venue.
                coverage_by_venue[cov["venue"]] = cov
        if "scan done" in ln:
            last_scan = ln.strip()
            m = _SCAN_PAIRS_RE.search(ln)
            if m:
                scan_pair_counts.append(int(m.group(1)))
        elif "EMAIL FAILED" in ln:          # delivery failure (SMTP/auth) — checked
            email_fail = ln.split("EMAIL FAILED:", 1)[-1].strip()  # before EMAILED
            email_fail_idx = i
        elif "EMAILED" in ln:
            last_email_idx = i
            # subject is the bracketed "[Pred-Arb] ..." portion
            j = ln.find("[Pred-Arb]")
            last_email_subject = ln[j:].strip() if j >= 0 else ln.strip()
        elif "AI verify:" in ln:
            last_heartbeat = ln.split("AI verify:", 1)[1].strip()
        elif "CYCLE ERROR" in ln:
            recent_cycle_error = ln.split("CYCLE ERROR:", 1)[-1].strip()
            error_idx = i
    scans_since_email = sum(
        1 for ln in lines[last_email_idx + 1:] if "scan done" in ln) if last_email_idx >= 0 else None
    # "recent" = within the last 200 lines of the window
    error_is_recent = error_idx >= 0 and error_idx >= len(lines) - 200
    email_fail_recent = email_fail_idx >= 0 and email_fail_idx >= len(lines) - 200
    # Real silent-no-op signals (the heartbeat prints BEFORE the email in a cycle,
    # so "heartbeat after the email" is the wrong test — it false-WARNs, #10):
    #   * the latest heartbeat reports an absent key (verification skipped), or
    #   * emails went out but NO heartbeat appears anywhere in the window.
    key_absent = bool(last_heartbeat) and "ABSENT" in last_heartbeat
    emails_without_heartbeat = last_email_subject is not None and last_heartbeat is None
    # Verifier-API-failure: key present and N checked, but 0 confirmed AND 0 flagged
    # means every verify() failed open (DeepSeek down / rate-limited / bad key) — the
    # verifier is effectively dead while key=present hides it (#45). Normally
    # confirmed+flagged == checked, so this state is unambiguous.
    verifier_api_failing = False
    if last_heartbeat and not key_absent:
        m = re.search(r"(\d+) checked, (\d+) confirmed, (\d+) flagged", last_heartbeat)
        if m:
            checked, confirmed, flagged = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            verifier_api_failing = checked > 0 and (confirmed + flagged) == 0
    # Partial-data scan detection (adaptive): the latest scan's matched-pair count
    # collapsing far below the recent median signals a degraded catalog fetch (#29).
    last_scan_pairs = scan_pair_counts[-1] if scan_pair_counts else None
    scan_pairs_median = statistics.median(scan_pair_counts) if scan_pair_counts else None
    scan_pairs_low = (
        len(scan_pair_counts) >= 3 and scan_pairs_median >= 100
        and last_scan_pairs < 0.5 * scan_pairs_median)
    # Coverage SLO (#iteration-3 task D): DEGRADED when a venue's most recent
    # cycle reports < 100% ingestion, a partial catalog, or a partial/failed
    # sweep. Missing coverage lines (old log format, or a log with no cycles
    # yet) must NOT degrade — reported as "n/a" instead.
    coverage_degraded = any(_coverage_degraded(c) for c in coverage_by_venue.values())
    return {
        "last_scan": last_scan,
        "last_scan_pairs": last_scan_pairs,
        "scan_pairs_median": scan_pairs_median,
        "scan_pairs_low": scan_pairs_low,
        "last_email_subject": last_email_subject,
        "scans_since_email": scans_since_email,
        "last_heartbeat": last_heartbeat,
        "key_absent": key_absent,
        "verifier_api_failing": verifier_api_failing,
        "emails_without_heartbeat": emails_without_heartbeat,
        "recent_cycle_error": recent_cycle_error if error_is_recent else None,
        "recent_email_failure": email_fail if email_fail_recent else None,
        "coverage_by_venue": coverage_by_venue,
        "coverage_degraded": coverage_degraded,
    }


def overall_ok(s: dict) -> bool:
    """True unless there's a real problem. DEGRADED on: verifier key absent, emails
    with no heartbeat (#8 class), a recent cycle error, or NO scan seen at all
    (pipeline not running). Normal quiet (idle verifier / no recent email within the
    realert window) stays OK — those aren't faults."""
    return not (s.get("key_absent")
                or s.get("verifier_api_failing")
                or s.get("emails_without_heartbeat")
                or s.get("recent_cycle_error") is not None
                or s.get("recent_email_failure") is not None
                or s.get("scan_pairs_low")
                or s.get("log_stale")
                or s.get("coverage_degraded")
                or s.get("last_scan") is None)


def format_health(s: dict, verdicts_count: int, verdicts_mtime: str | None) -> str:
    def mark(ok: bool) -> str:
        return "OK  " if ok else "WARN"
    status = "OK" if overall_ok(s) else "DEGRADED"
    lines = ["Arb alerter - health", "=" * 40, f"STATUS: {status}", "-" * 40]
    lines.append(f"[{mark(bool(s['last_scan']))}] last scan: {s['last_scan'] or 'none seen'}")
    age = s.get("log_age_hours")
    if age is not None:
        lines.append(f"[{mark(not s.get('log_stale'))}] log freshness: written {age:.1f}h ago"
                     + (f" — STALE (>{_LOG_STALE_HOURS:.0f}h), is the task firing?" if s.get("log_stale") else ""))
    if s.get("last_scan_pairs") is not None:
        med = s.get("scan_pairs_median")
        lines.append(f"[{mark(not s.get('scan_pairs_low'))}] scan size: "
                     f"{s['last_scan_pairs']} pairs (recent median {med:.0f})"
                     + (" — COLLAPSED, partial-data scan?" if s.get("scan_pairs_low") else ""))
    if s["last_email_subject"]:
        since = s["scans_since_email"]
        lines.append(f"[OK  ] last email: {s['last_email_subject']}")
        lines.append(f"        scans since last email: {since} "
                     f"(quiet is normal within the 6h realert window)")
    else:
        lines.append("[WARN] no email seen in the recent window")
    if s["key_absent"]:
        lines.append(f"[WARN] verifier: {s['last_heartbeat']} — key not resolving (see #8)")
    elif s.get("verifier_api_failing"):
        lines.append(f"[WARN] verifier: {s['last_heartbeat']} — all checks failed (API down/rate-limited?)")
    elif s["emails_without_heartbeat"]:
        lines.append("[WARN] emails went out but no verifier heartbeat in window — is the AI gate running? (see #8)")
    elif s["last_heartbeat"]:
        lines.append(f"[OK  ] verifier: {s['last_heartbeat']}")
    else:
        lines.append("[OK  ] verifier idle (no email-worthy cycle in window)")
    lines.append(f"[{mark(s['recent_cycle_error'] is None)}] "
                 f"cycle errors: {s['recent_cycle_error'] or 'none recent'}")
    lines.append(f"[{mark(s.get('recent_email_failure') is None)}] "
                 f"email delivery: {'FAILED — ' + s['recent_email_failure'] if s.get('recent_email_failure') else 'ok (no recent failures)'}")
    coverage = s.get("coverage_by_venue") or {}
    if coverage:
        parts = []
        for venue in ("Kalshi", "Polymarket"):
            c = coverage.get(venue)
            if not c:
                continue
            pct = f"{c['pct']:.1f}%" if c["pct"] is not None else "n/a"
            tag = "  [PARTIAL CATALOG]" if c["partial_catalog"] else ""
            parts.append(f"{venue} {c['ingested']:,}/{c['open']:,} ({pct}, sweep={c['sweep']}){tag}")
        lines.append(f"[{mark(not s.get('coverage_degraded'))}] coverage: " + " · ".join(parts))
    else:
        lines.append("[OK  ] coverage: n/a (no [coverage] lines in window — old log format?)")
    fresh = f"{verdicts_count} rows, last write {verdicts_mtime}" if verdicts_mtime else f"{verdicts_count} rows"
    lines.append(f"[OK  ] ai_verify.jsonl: {fresh}")
    return "\n".join(lines)


def _verdicts_info() -> tuple[int, str | None]:
    try:
        import datetime
        with _VERDICTS.open(encoding="utf-8", errors="replace") as f:
            n = sum(1 for _ in f)
        # UTC to match every other timestamp in the report (scans/heartbeats are
        # UTC); a bare fromtimestamp() would show local time and mislead (#136).
        mtime = datetime.datetime.fromtimestamp(
            _VERDICTS.stat().st_mtime, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
        return n, mtime
    except Exception:
        return 0, None


def _log_age_hours(path: pathlib.Path) -> float | None:
    """Hours since the log file was last written, or None if it is missing."""
    try:
        import time
        return (time.time() - path.stat().st_mtime) / 3600.0
    except OSError:
        return None


def build_report(log_path: pathlib.Path | str = _LOG) -> tuple[str, bool]:
    """Convenience entry: load → summarize → format. Returns (text, overall_ok) so
    callers (CLI, ops.py) don't repeat the tail/summarize/info plumbing."""
    path = pathlib.Path(log_path)
    summary = summarize_log(_tail(path, _TAIL_LINES))
    # Freshness check: content-based signals can't see a task that stopped firing
    # (the stale lines remain). The log file's mtime can. (#126)
    age = _log_age_hours(path)
    summary["log_age_hours"] = age
    summary["log_stale"] = age is not None and age > _LOG_STALE_HOURS
    n, mtime = _verdicts_info()
    return format_health(summary, n, mtime), overall_ok(summary)


if __name__ == "__main__":
    log_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else _LOG
    text, ok = build_report(log_path)
    print(text)
    sys.exit(0 if ok else 1)  # scriptable: 0 OK, 1 DEGRADED

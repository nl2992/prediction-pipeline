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

BASE = pathlib.Path(__file__).resolve().parent
_LOG = BASE / "alerter_cron.log"
_VERDICTS = BASE / "ai_verify.jsonl"
# A cycle is ~90 log lines; the realert window is 6h (~25 cycles), so span enough
# lines that the last email is still visible (else "scans since email" is wrong).
_TAIL_LINES = 2600


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
    for i, ln in enumerate(lines):
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
                or s.get("last_scan") is None)


def format_health(s: dict, verdicts_count: int, verdicts_mtime: str | None) -> str:
    def mark(ok: bool) -> str:
        return "OK  " if ok else "WARN"
    status = "OK" if overall_ok(s) else "DEGRADED"
    lines = ["Arb alerter - health", "=" * 40, f"STATUS: {status}", "-" * 40]
    lines.append(f"[{mark(bool(s['last_scan']))}] last scan: {s['last_scan'] or 'none seen'}")
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
    fresh = f"{verdicts_count} rows, last write {verdicts_mtime}" if verdicts_mtime else f"{verdicts_count} rows"
    lines.append(f"[OK  ] ai_verify.jsonl: {fresh}")
    return "\n".join(lines)


def _verdicts_info() -> tuple[int, str | None]:
    try:
        import datetime
        n = sum(1 for _ in _VERDICTS.open(encoding="utf-8", errors="replace"))
        mtime = datetime.datetime.fromtimestamp(_VERDICTS.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return n, mtime
    except Exception:
        return 0, None


if __name__ == "__main__":
    log_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else _LOG
    summary = summarize_log(_tail(log_path, _TAIL_LINES))
    n, mtime = _verdicts_info()
    print(format_health(summary, n, mtime))
    sys.exit(0 if overall_ok(summary) else 1)  # scriptable: 0 OK, 1 DEGRADED

"""
Automated arb alerter — full scan every N minutes, email on executable signals.

Every interval (default 30 min) this runs the full organic cross-platform scan
(discover, with live orderbooks), computes two-leg arb economics for every
matched pair under BOTH fee models (conservative flat 7% and Kalshi's real
0.07·p·(1−p) taker fee), and — when a pair's best net edge under the accurate
fee clears the alert threshold — immediately emails the configured recipients
with the pair, direction legs, prices, net edge, and direct links to both
markets.

Run:
    python alerter.py --once          # single scan+alert cycle
    python alerter.py                 # loop forever, every 30 min
    python alerter.py --interval 900  # custom interval (seconds)

Email configuration (alert_config.json next to this file — gitignored, or env):
    {
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "smtp_user": "youraddress@gmail.com",
      "smtp_pass": "<gmail app password>",
      "from_addr": "youraddress@gmail.com",
      "recipients": ["hyutong88@gmail.com", "nl2992@columbia.edu"]
    }
Env vars ALERT_SMTP_HOST / ALERT_SMTP_PORT / ALERT_SMTP_USER / ALERT_SMTP_PASS /
ALERT_FROM / ALERT_RECIPIENTS (comma-separated) override the file. Without
credentials the alerter still runs: signals are printed and appended to
alert_signals.jsonl, and each cycle warns that email is unconfigured.

Trigger vs content: an email is TRIGGERED on change — a signal is considered
fresh only if it is new, its net edge improved by >= 0.5c, or the last alert for
it is older than --realert-hours (default 6). But when an email goes out, it
lists EVERY currently-executable pair (net of fees strictly positive), not just
the changed one — the operator wants the full set of runnable arbs each time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from arb import kalshi_taker_fee

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "alert_config.json"
STATE_FILE = BASE / "alert_state.json"
SIGNALS_FILE = BASE / "alert_signals.jsonl"

DEFAULT_RECIPIENTS = ["hyutong88@gmail.com", "nl2992@columbia.edu"]
FLAT_FEE = 0.07

# When launched headless (Windows Task Scheduler via pythonw.exe), there is no
# console: sys.stdout/sys.stderr are None and every print() would raise. Redirect
# both to a rolling log file so scheduled runs are silent yet still auditable.
if sys.stdout is None or sys.stderr is None:
    _cron_log = open(BASE / "alerter_cron.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _cron_log
    if sys.stderr is None:
        sys.stderr = _cron_log

# Full-catalog scans surface foreign/special-char titles (↓ ° é). Printing them
# to a Windows cp1252 console raises UnicodeEncodeError and would abort the whole
# cycle (no email). Make stdout/stderr replace unencodable chars instead.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except Exception:
        pass

# Adaptive ingestion cap. Run 11 (MATCHER_VALIDATION_LOG.md) showed the old
# fixed cap of 200 Kalshi events silently dropped ~178 true pairs that live in
# events ranked >200. Each scan now targets at least TARGET_SURVIVABLE positive
# net-of-fees ("survivable") arbs, progressively widening the event cap through
# CAP_LADDER until the target is met or the last rung (1500) is reached.
# Event cap 200 -> 500 (run 30) -> 1500 (run 38, operator decision). Run-38 recall
# probe showed cap=500 missed ~1775 real diverse pairs (OPEC/Bitcoin-gold/Messi/
# BTTS) living in events ranked 500-1500. Precision fixes (runs 13-37) made the
# guarded top mostly-real, so 1500 is now safe for coverage. To keep the inbox
# sane, emails are capped to the TOP_N richest (see signals_to_send). Re-check
# MATCHER_VALIDATION_LOG.md (runs 12, 30, 38) before changing.
TARGET_SURVIVABLE = 50
CAP_LADDER = (1500,)
# Email only the N richest (by net-of-fees edge) per cycle — full-catalog scans
# surface ~335 survivable arbs; the operator wants the richest, not all of them.
TOP_N = 50
# Minimum best-level depth (shares/contracts) on both executed legs. Run 21
# measured cap=200: a floor of 20 keeps 20 of 25 arbs, dropping ~5 illiquid/dust
# signals — operator opted to email only depth-backed, executable arbs.
MIN_DEPTH = 20.0


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[alerter] WARNING: could not parse {CONFIG_FILE.name}: {exc}")
    env = os.environ
    cfg.setdefault("smtp_host", env.get("ALERT_SMTP_HOST", "smtp.gmail.com"))
    cfg.setdefault("smtp_port", int(env.get("ALERT_SMTP_PORT", "587")))
    if env.get("ALERT_SMTP_USER"):
        cfg["smtp_user"] = env["ALERT_SMTP_USER"]
    if env.get("ALERT_SMTP_PASS"):
        cfg["smtp_pass"] = env["ALERT_SMTP_PASS"]
    if env.get("ALERT_FROM"):
        cfg["from_addr"] = env["ALERT_FROM"]
    if env.get("ALERT_RECIPIENTS"):
        cfg["recipients"] = [a.strip() for a in env["ALERT_RECIPIENTS"].split(",") if a.strip()]
    cfg.setdefault("recipients", DEFAULT_RECIPIENTS)
    cfg.setdefault("from_addr", cfg.get("smtp_user", ""))
    return cfg


def email_configured(cfg: dict) -> bool:
    return bool(cfg.get("smtp_user") and cfg.get("smtp_pass"))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

def _kalshi_url(p: dict) -> str:
    series = (p.get("kalshi_series_ticker") or (p.get("kalshi_event_ticker") or "").split("-")[0] or "").lower()
    if not series:
        return ""
    slug_src = p.get("kalshi_event_title") or p.get("kalshi_title") or series
    slug = re.sub(r"[^a-z0-9]+", "-", slug_src.lower()).strip("-") or series
    ticker = p.get("kalshi_ticker") or ""
    return f"https://kalshi.com/markets/{series}/{slug}" + (f"?ticker={ticker}" if ticker else "")


def _poly_url(p: dict) -> str:
    slug = p.get("poly_slug")
    return f"https://polymarket.com/event/{slug}" if slug else ""


def compute_signals(pairs: list[dict], min_edge: float,
                    require_v2: bool = True, max_edge: float = 0.25,
                    min_size: float = 0.0) -> list[dict]:
    """Two-leg arb economics for every priced pair; keep net >= min_edge.

    Both directions, both fee models. ``net_accurate`` (Kalshi's real
    0.07·p·(1−p) taker fee on the Kalshi leg; Polymarket CLOB is fee-free)
    drives the alert decision; the flat worst-case 7% figure rides along for
    context.

    Precision guards (added after run-12: widening the scan to 1500 events
    surfaced ~600 phantom "arbs" — e.g. "Cody Gakpo" matched to "Cody Gakpo:
    2+ assists?", different contracts with a ~95c phantom edge):
      * ``require_v2`` — only trust pairs the independent v2 contract_spec engine
        endorses (``v2_match is True``); v2 rejects name-vs-name+prop and
        opposite-event mismatches on settlement-shape/threshold grounds.
      * ``max_edge`` — a net edge above this (default 25c) between two identical
        binary contracts on liquid venues does not exist; it is the signature of
        a mismatch or a one-sided/stale book, so it is dropped.
      * ``min_size`` — minimum best-level depth (shares/contracts) on BOTH legs
        actually executed in a direction. 0 (default) disables it; >0 drops
        illiquid/one-sided books whose "edge" is unexecutable. Buying PM YES
        takes the PM ask; buying Kalshi NO hits the Kalshi YES bid; buying Kalshi
        YES takes the Kalshi ask; buying PM NO hits the PM YES bid.
    """
    signals: list[dict] = []
    for p in pairs:
        if require_v2 and p.get("v2_match") is not True:
            continue  # independent referee does not confirm same contract
        pb, pa = p.get("poly_bid"), p.get("poly_ask")
        kb, ka = p.get("kalshi_bid"), p.get("kalshi_ask")
        pbs, pas = p.get("poly_bid_size"), p.get("poly_ask_size")
        kbs, kas = p.get("kalshi_bid_size"), p.get("kalshi_ask_size")
        best = None
        if (pa is not None and kb is not None
                and (pas or 0) >= min_size and (kbs or 0) >= min_size):
            k_no = round(1.0 - kb, 6)
            gross = round(1.0 - (pa + k_no), 6)
            cand = {
                "direction": "buy YES on Polymarket + buy NO on Kalshi",
                "legs": f"PM YES @ {pa}  |  Kalshi NO @ {k_no}",
                "gross": gross,
                "net_accurate": round(gross - kalshi_taker_fee(k_no), 6),
                "net_flat7": round(gross - FLAT_FEE, 6),
            }
            best = cand
        if (ka is not None and pb is not None
                and (kas or 0) >= min_size and (pbs or 0) >= min_size):
            p_no = round(1.0 - pb, 6)
            gross = round(1.0 - (ka + p_no), 6)
            cand = {
                "direction": "buy YES on Kalshi + buy NO on Polymarket",
                "legs": f"Kalshi YES @ {ka}  |  PM NO @ {p_no}",
                "gross": gross,
                "net_accurate": round(gross - kalshi_taker_fee(ka), 6),
                "net_flat7": round(gross - FLAT_FEE, 6),
            }
            if best is None or cand["net_accurate"] > best["net_accurate"]:
                best = cand
        if best is None or best["net_accurate"] < min_edge:
            continue
        if best["net_accurate"] > max_edge:
            continue  # implausible edge => mismatch / one-sided book (phantom)
        signals.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "poly_title": p.get("poly_title"),
            "kalshi_title": p.get("kalshi_title"),
            "poly_url": _poly_url(p),
            "kalshi_url": _kalshi_url(p),
            "poly_bid": pb, "poly_ask": pa, "kalshi_bid": kb, "kalshi_ask": ka,
            "confidence": p.get("confidence"),
            "v2_match": p.get("v2_match"),
            # Full ladders for the executable-depth charts (top-30/side from
            # discover); stripped before persisting to the signal log.
            "poly_book": p.get("poly_book"),
            "kalshi_book": p.get("kalshi_book"),
            **best,
            "key": f"{p.get('poly_id')}|{p.get('kalshi_ticker')}|{best['direction']}",
        })
    signals.sort(key=lambda s: -s["net_accurate"])
    return signals


def filter_new(signals: list[dict], state: dict, realert_hours: float, improve_step: float = 0.005) -> list[dict]:
    now = time.time()
    fresh = []
    for s in signals:
        prev = state.get(s["key"])
        if prev is None or s["net_accurate"] >= prev.get("net", -1) + improve_step \
                or now - prev.get("ts", 0) > realert_hours * 3600:
            fresh.append(s)
    return fresh


def signals_to_send(signals: list[dict], state: dict, realert_hours: float,
                    min_net: float = 0.0, top_n: int = TOP_N) -> tuple[list[dict], list[dict]]:
    """Decide what to email.

    Trigger on CHANGE (any new/improved signal vs last scan); when we email,
    send the ``top_n`` richest currently-executable pairs (net of fees strictly
    above ``min_net``), sorted by net edge. Full-catalog scans surface hundreds
    of survivable arbs, so the operator wants the richest, not all of them
    (run 38). ``top_n=0`` means no cap (all positive-net).

    Returns ``(to_email, fresh)`` — the richest top_n when something changed in
    that set, else empty (no trigger → no email).
    """
    positive = sorted((s for s in signals if s["net_accurate"] > min_net),
                      key=lambda s: -s["net_accurate"])
    if top_n:
        positive = positive[:top_n]
    fresh = filter_new(positive, state, realert_hours)
    if not fresh:
        return [], []
    return positive, fresh


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _exec_block(s: dict) -> tuple[str, bytes | None, str | None]:
    """Return (html_fragment, png_bytes_or_None, cid_or_None) for one signal's
    executable-depth analysis: a profit-by-budget line plus the order-book chart.

    Degrades gracefully: if the books are absent or matplotlib is unavailable the
    chart is omitted and (where possible) the numbers still render as text.
    """
    try:
        from arb_charts import executable_summary, make_arb_chart, BUDGETS
    except Exception:
        return "", None, None
    summ = executable_summary(s)
    if not summ:
        return "", None, None
    _, res = summ
    by = res["by_budget"]
    # Headline = the LARGEST realistic budget tier ($5k/market), not the unbounded
    # full-book "max": on illiquid markets the deep book is thin/stale, so the
    # unbounded figure (e.g. 92k contracts / $2,393 needing ~$90k of capital on an
    # obscure longshot) overstates what is actually executable. The $5k/market cap
    # equals the full depth whenever the book is shallower than that.
    cap = by[max(BUDGETS)]
    budget_cells = " &nbsp;|&nbsp; ".join(
        f"${b/1000:g}k&rarr;<b>${by[b].profit:,.0f}</b> ({by[b].contracts:,.0f}c)"
        for b in BUDGETS)
    text = (f"""
        <tr><td>Executable (≤ $5k/market)</td><td><b>{cap.contracts:,.0f}</b> arbable contract-pairs &nbsp;|&nbsp; """
            f"""net profit <b>${cap.profit:,.0f}</b> (ROI {cap.roi*100:.2f}%) &nbsp;|&nbsp; """
            f"""VWAP PM {cap.vwap_a:.3f} / Kalshi {cap.vwap_b:.3f}</td></tr>
        <tr><td>Profit by budget</td><td>{budget_cells}</td></tr>""")
    png = make_arb_chart(s)
    if not png:
        return text, None, None
    cid = f"chart{s['key'].__hash__() & 0xffffffff:x}"
    text += (f'<tr><td colspan="2"><img src="cid:{cid}" '
             f'alt="order-book depth + profit chart" style="max-width:640px;width:100%"></td></tr>')
    return text, png, cid


def build_email(signals: list[dict]) -> tuple[str, str, list[tuple[str, bytes]]]:
    n = len(signals)
    top = signals[0]
    subject = (f"[Pred-Arb] {n} executable signal{'s' if n != 1 else ''} — "
               f"best net edge {top['net_accurate']*100:.2f}c/$1")
    rows = []
    images: list[tuple[str, bytes]] = []
    for s in signals:
        exec_html, png, cid = _exec_block(s)
        if png and cid:
            images.append((cid, png))
        rows.append(f"""
        <tr><td colspan="2" style="padding-top:18px;border-top:1px solid #eee"><b>{s['poly_title']}</b> &harr; <b>{s['kalshi_title']}</b></td></tr>
        <tr><td>Direction</td><td>{s['direction']}</td></tr>
        <tr><td>Legs</td><td>{s['legs']}</td></tr>
        <tr><td>Net edge (real Kalshi fee)</td><td><b>{s['net_accurate']*100:.2f}c per $1</b> (gross {s['gross']*100:.2f}c, worst-case-7%-fee net {s['net_flat7']*100:.2f}c)</td></tr>
        <tr><td>Quotes</td><td>PM {s['poly_bid']}/{s['poly_ask']} &nbsp; Kalshi {s['kalshi_bid']}/{s['kalshi_ask']}</td></tr>{exec_html}
        <tr><td>Polymarket</td><td><a href="{s['poly_url']}">{s['poly_url']}</a></td></tr>
        <tr><td>Kalshi</td><td><a href="{s['kalshi_url']}">{s['kalshi_url']}</a></td></tr>
        <tr><td>Matcher</td><td>confidence {s['confidence']} | v2 agrees: {s['v2_match']}</td></tr>""")
    html = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px">
    <p>Automated full cross-platform scan found <b>{n}</b> executable arb signal{'s' if n != 1 else ''}
    at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.</p>
    <p style="color:#555;font-size:12px">Each pair shows the executable picture walked against the live
    order book: how many contract-pairs are actually arbable, the VWAP per leg, and net profit for a
    per-market budget of $1k/$2k/$2.5k/$5k. The chart's shaded region is the arbable depth before the
    combined cost crosses the $1 break-even.</p>
    <table cellspacing="0" cellpadding="4">{''.join(rows)}</table>
    <p style="color:#888;font-size:12px">Edges are per $1 of payout, after Kalshi's real
    0.07·p·(1−p) taker fee (Polymarket CLOB fee-free). Verify depth/slippage before executing.
    Sent by alerter.py on the prediction-pipeline scanner.</p>
    </body></html>"""
    return subject, html, images


def send_email(cfg: dict, subject: str, html: str,
               images: list[tuple[str, bytes]] | None = None) -> None:
    # multipart/related wraps the HTML (in an alternative part) together with any
    # inline CID images, so Gmail renders the charts inline.
    root = MIMEMultipart("related")
    # UTF-8 throughout — titles can contain non-ASCII (é, °, ↓) at full scale;
    # default us-ascii would raise on send.
    root["Subject"] = Header(subject, "utf-8")
    root["From"] = cfg["from_addr"]
    root["To"] = ", ".join(cfg["recipients"])
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html", "utf-8"))
    root.attach(alt)
    for cid, png in (images or []):
        img = MIMEImage(png, _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        root.attach(img)
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as srv:
        srv.starttls()
        srv.login(cfg["smtp_user"], cfg["smtp_pass"])
        srv.sendmail(cfg["from_addr"], cfg["recipients"], root.as_string())


# ---------------------------------------------------------------------------
# Scan cycle
# ---------------------------------------------------------------------------

def adaptive_scan(min_edge: float, target: int = TARGET_SURVIVABLE,
                  caps: tuple[int, ...] = CAP_LADDER,
                  min_size: float = MIN_DEPTH) -> tuple[list, list, int]:
    """Scan, progressively widening the Kalshi event cap until at least ``target``
    survivable (positive net-of-fees) arbs are found, or the last cap is reached.

    Returns ``(pairs, signals, cap_used)``. Higher caps are supersets, so the
    last scan's results are the richest; we stop early at the first cap that
    already clears the target to save time.
    """
    from discover import discover
    pairs: list = []
    signals: list = []
    cap_used = caps[-1]
    for cap in caps:
        pairs = discover(category="all", days=730, min_sim=0.30, show_prices=True,
                         max_events_to_search=cap, catalog_cache_ttl=1200)
        signals = compute_signals(pairs, min_edge=min_edge, min_size=min_size)
        survivable = sum(1 for s in signals if s["net_accurate"] > 0)
        cap_used = cap
        print(f"[alerter] cap={cap}: {len(pairs)} pairs, {survivable} survivable arb(s) "
              f"(target {target})", flush=True)
        if survivable >= target:
            break
    return pairs, signals, cap_used


def run_cycle(min_edge: float, realert_hours: float, dry_run: bool = False,
              min_size: float = MIN_DEPTH) -> int:
    cfg = load_config()
    t0 = time.time()
    print(f"[alerter] {datetime.now(timezone.utc).strftime('%H:%M:%S')}Z full scan starting…", flush=True)
    pairs, signals, cap_used = adaptive_scan(min_edge, min_size=min_size)
    survivable = sum(1 for s in signals if s["net_accurate"] > 0)
    print(f"[alerter] scan done in {time.time()-t0:.0f}s — cap={cap_used}, {len(pairs)} pairs, "
          f"{survivable} survivable arb(s) (>= {min_edge*100:.2f}c net)", flush=True)

    state = load_state()
    # Trigger on change; email EVERY positive-net (after-fees) pair, not just the
    # changed one. `fresh` is only used to decide whether to send this cycle.
    to_email, fresh = signals_to_send(signals, state, realert_hours)
    if not to_email:
        return 0

    # Persist a lean record — the full order-book ladders are large and only
    # needed in-memory for the charts.
    with SIGNALS_FILE.open("a", encoding="utf-8") as f:
        for s in to_email:
            lean = {k: v for k, v in s.items() if k not in ("poly_book", "kalshi_book")}
            f.write(json.dumps(lean) + "\n")
    for s in to_email:
        flag = " (new/changed)" if s in fresh else ""
        print(f"[alerter] SIGNAL net={s['net_accurate']*100:.2f}c  {s['poly_title'][:40]!r} <-> {s['kalshi_title'][:40]!r}{flag}")

    subject, html, images = build_email(to_email)
    if dry_run:
        print(f"[alerter] DRY RUN — would email {cfg['recipients']}: {subject} "
              f"({len(to_email)} pairs, {len(images)} charts)")
    elif email_configured(cfg):
        try:
            send_email(cfg, subject, html, images)
            print(f"[alerter] EMAILED {cfg['recipients']}: {subject} ({len(to_email)} pairs)", flush=True)
        except Exception as exc:
            print(f"[alerter] EMAIL FAILED: {exc} — signal logged to {SIGNALS_FILE.name}", flush=True)
    else:
        print(f"[alerter] EMAIL NOT CONFIGURED — create {CONFIG_FILE.name} (see module docstring). "
              f"Signal logged to {SIGNALS_FILE.name}.", flush=True)

    now = time.time()
    for s in to_email:
        state[s["key"]] = {"net": s["net_accurate"], "ts": now}
    save_state(state)
    return len(to_email)


def main() -> None:
    ap = argparse.ArgumentParser(description="Scheduled full-scan arb email alerter")
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between scans (default 300; catalogs are cached 20 min, quotes always fresh)")
    ap.add_argument("--min-edge", type=float, default=0.0001,
                    help="min net edge (accurate fee) to alert, in $ per $1 payout "
                         "(default 0.0001 = any strictly positive edge after fees)")
    ap.add_argument("--realert-hours", type=float, default=6.0,
                    help="re-email an unchanged signal after this many hours (default 6)")
    ap.add_argument("--min-size", type=float, default=MIN_DEPTH,
                    help=f"min best-level depth on both legs (default {MIN_DEPTH:g}; 0 = no liquidity filter)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="never send email; print instead")
    args = ap.parse_args()

    cfg = load_config()
    print(f"[alerter] recipients: {cfg['recipients']} | email configured: {email_configured(cfg)} "
          f"| min edge: {args.min_edge*100:.1f}c | interval: {args.interval}s", flush=True)

    while True:
        try:
            run_cycle(args.min_edge, args.realert_hours, dry_run=args.dry_run,
                      min_size=args.min_size)
        except Exception as exc:
            print(f"[alerter] CYCLE ERROR: {exc}", flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

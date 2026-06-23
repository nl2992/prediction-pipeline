# Operations — the production arbitrage alerter

This document covers the subsystem that runs continuously in production: the
scheduled email **alerter**, the **AI settlement-equivalence verifier**, and the
read-only **operator tools**. (For the core pipeline — ingest, matching, arb
detection, execution — see `README.md`.)

## What runs

A Windows Scheduled Task, **`PredArbAlerter`**, runs `alerter.py` in a loop:
scan both exchanges → match markets → find two-leg arbs → email the good ones.
Each scan takes ~13–15 min; the task relaunches periodically (fresh process picks
up code changes on restart).

```powershell
# inspect the task
Get-ScheduledTask -TaskName PredArbAlerter | Get-ScheduledTaskInfo
```

Run one cycle manually (does not interfere with the scheduled task's own state if
you use `--dry-run`):

```bash
python alerter.py --once --dry-run     # scan + build email, print instead of send
```

## What gets emailed

- **Threshold:** only pairs with a net edge **> 3%** after fees (default `MIN_NET_EMAIL = 0.03`;
  override with `"min_net_email"` in `alert_config.json`). The subject/intro show the active threshold.
  Net edge = `1 − legA − legB − Kalshi fee`; Polymarket CLOB is fee-free, Kalshi's
  taker fee is `0.07·p·(1−p)`.
- **Ranking:** richest-first by **annualised return** = `net × 365 / days`, where
  `days` is the horizon to the **later** of the two legs' close dates (capital is
  locked until both resolve). A small edge settling soon can outrank a big edge
  settling years out. The subject leads with the top pair's annualised figure.
- **Freshness badges:** each pair is tagged **NEW** (never alerted) or
  **↑ IMPROVED** (net edge up since last alert); persistent pairs re-sent only
  because the realert window (default 6h) elapsed get no badge.
- **Execution detail:** per pair, the email shows executable depth ("up to N
  contract-pairs"), net profit by stake tier ($1k/$2k/$2.5k/$5k), VWAP fill
  prices, the settlement date + annualised return, and an order-book depth chart.
- **AI check row:** ✓ "verified identical event & settlement" (the only ones that
  survive enforce) or, in shadow mode, ⚠ the AI's caveat.

Guardrails: `max_edge = 0.25` (a >25c edge between two identical binaries is a
mismatch/stale book, dropped); `MIN_DEPTH = 20` best-level depth on both legs;
`TOP_N = 50` richest pairs per email.

## AI settlement-equivalence verifier (`ai_verify.py`)

An optional DeepSeek second opinion on whether two matched markets really resolve
on the **identical** event AND settlement. Verdict:
`{same, same_event, settlement_same, poly_settlement, kalshi_settlement, reason}`.

- **API key:** read from the `DEEPSEEK_API_KEY` env var. On Windows, `resolve_api_key()`
  falls back to `HKCU\Environment` (where `setx` persists it) because a freshly
  spawned scheduled-task process does **not** inherit a setx'd user var into
  `os.environ`. The key is never stored in the repo.
- **Modes** (`ai_verify_mode` in `alert_config.json`): `shadow` logs verdicts only;
  `enforce` (current) drops pairs judged to settle differently. A **mass-drop
  guard** keeps all pairs if >60% of a cycle would be dropped (API/prompt anomaly),
  so a misbehaving verifier never sends a near-empty email.
- **Prompt v3:** treats a ~1-year gap between the two contractual expiries as the
  placeholder pattern (same race, not a different cycle); judges settlement on the
  question text, not the expiry dates. Bumping `_PROMPT_VERSION` invalidates the
  disk cache.
- **Caching:** verdicts are cached in-process and on disk (`ai_verify_cache.json`,
  14-day TTL) keyed on prompt version + the two market texts, to spare the API.
- **Audit log:** every verdict is appended to `ai_verify.jsonl`.
- **Heartbeat:** each cycle logs `AI verify: mode=…, key=present/ABSENT, N checked,
  C confirmed, D flagged` so a silent no-op is visible.

```bash
python ai_verify.py "market A text" "market B text"   # manual one-off check
```

## Operator tools

### `health.py` — one-glance status (scriptable)

```bash
python health.py        # prints STATUS: OK|DEGRADED + details; exits 0 (OK) / 1 (DEGRADED)
python health.py || notify-someone   # usable as a watchdog
```

Parses the tail of `alerter_cron.log` + `ai_verify.jsonl`: last scan, last email and
scans-since (vs the realert window), the verifier heartbeat (with a silent-no-op
check), recent `CYCLE ERROR`, and verdict-log freshness. **DEGRADED** only on real
faults — verifier `key=ABSENT`, emails-without-heartbeat, a recent cycle error, or
no scan seen at all; normal quiet periods stay OK.

### `ai_verify_report.py` — verdict-log digest (matcher QA)

```bash
python ai_verify_report.py
```

Summarises `ai_verify.jsonl`: total/% confirmed, and currently-flagged pairs keyed
off each pair's **latest** verdict (resolved pairs drop off; ghosts older than
~12h age out). Flagged pairs are split into:

- **Different-event pairs (MATCHER false-positives)** — fix upstream (e.g. the
  BRICS↔OPEC org-mismatch fix in `matcher.py`).
- **Same-event, different-settlement** — correct enforce drops, not matcher bugs.

### `signal_report.py` — emitted-arb digest (opportunity intelligence)

```bash
python signal_report.py
```

Digests `alert_signals.jsonl` (every emailed arb) for manual-execution decisions,
in three views: **most recurring** (persistent opportunities, by count), **richest**
(max net edge ever seen), and **best by annualised return** — the alerter's priority
metric, restricted to pairs seen in the last 24h so it lists only currently-actionable
arbs (each with its "Nh ago" age).

### `ops.py` — unified dashboard

```bash
python ops.py        # health + opportunities + matcher QA in one view; exits 0/1 on health
```

One command composing `health.py`, `signal_report.py`, and `ai_verify_report.py` —
the full operational picture (is it working? what should I act on? any matcher
issues?). Exits 0 when healthy / 1 when degraded, so it works as a watchdog too.

## Failure handling

- A `CYCLE ERROR` (uncaught exception in a scan cycle) is logged **and** emails a
  degradation alert to the operator, with a 1-hour cooldown so repeated faults
  don't spam. The alert path is best-effort and never raises.
- The verifier fails **open**: any API/parse error returns no opinion and the pair
  is kept, so a flaky DeepSeek never drops a real arb or blocks an email.

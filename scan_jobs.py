"""Phase 2c: a single-flight background scan job runner.

Full scans take 10-20 minutes; the dashboard used to block a whole request
on that (docs/FRONTEND_REVIEW.md finding 6). This module runs one scan at a
time on a daemon ``threading.Thread``, exposes its live progress (stage,
recent progress lines) to poll, and supports cooperative cancellation —
without ever redirecting ``sys.stdout`` for the rest of the process (other
threads/requests keep writing to the real stdout untouched).

Design
------
Single-flight: ``start()`` is a no-op (returns the existing job, ``started:
False``) whenever a job is already running — there is never more than one
scan thread alive at a time.

Progress capture without hijacking stdout globally: ``discover()`` reports
progress via bare ``print()`` calls (``"[k/4] ..."``). Redirecting
``sys.stdout`` for the whole process while a background scan runs would
also swallow output from the main thread (e.g. another request's logging,
or a REPL). Instead, ``sys.stdout`` is wrapped exactly once, at first use,
by ``_ThreadRoutingStdout`` — a proxy that checks ``threading.get_ident()``
on every write: writes made from the currently-registered worker thread are
captured into that job's line buffer (and parsed for "[k/4] ..." stage
markers); writes from every other thread pass straight through to the
original stdout untouched.

Cancellation: discover() has no cancel hook and this module must not modify
discover.py. Cancellation is therefore cooperative and coarse-grained: once
``cancel()`` sets the job's cancel flag, the SAME stdout proxy raises
``ScanCancelled`` the next time the worker thread writes to stdout (i.e. the
next time discover() calls ``print()``). discover() has no top-level
try/except around its body between progress prints, so the exception
unwinds cleanly out of ``discover()`` and is caught here, where the job and
the store row are both marked 'cancelled'. Because discover() only prints
between its four top-level stages (not per-market), a cancel requested
mid-stage will not actually stop the scan until that stage's ingestion/
matching pass finishes and the next stage prints — this is a known
limitation of a print-based cancellation hook.
"""

from __future__ import annotations

import re
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

import store

_STAGE_RE = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)")
_LOG_TAIL_MAXLEN = 20


class ScanCancelled(Exception):
    """Raised inside the worker thread (via the stdout proxy) once a cancel
    has been requested and the worker next writes a progress line."""


# ---------------------------------------------------------------------------
# Thread-routing stdout proxy — installed once, process-wide.
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_job: dict[str, Any] | None = None
_thread: threading.Thread | None = None
_cancel_event: threading.Event | None = None
_worker_ident: int | None = None

_orig_stdout = None
_proxy_installed = False


class _ThreadRoutingStdout:
    """Drop-in ``sys.stdout`` replacement. Writes from the registered worker
    thread are captured into the running job's log/stage state (and can
    raise ``ScanCancelled``); every other thread's writes pass through to
    the real stdout unchanged."""

    def __init__(self, original):
        self._original = original

    def write(self, s: str) -> int:
        with _lock:
            is_worker = _worker_ident is not None and threading.get_ident() == _worker_ident
            should_cancel = is_worker and _cancel_event is not None and _cancel_event.is_set()
        if is_worker:
            _capture_line(s)
            if should_cancel:
                raise ScanCancelled("scan cancelled")
            return len(s)
        return self._original.write(s)

    def flush(self) -> None:
        self._original.flush()

    def isatty(self) -> bool:
        try:
            return self._original.isatty()
        except Exception:
            return False

    def __getattr__(self, name):
        return getattr(self._original, name)


def _install_stdout_proxy() -> None:
    global _orig_stdout, _proxy_installed
    if _proxy_installed:
        return
    _orig_stdout = sys.stdout
    sys.stdout = _ThreadRoutingStdout(_orig_stdout)
    _proxy_installed = True


def _capture_line(chunk: str) -> None:
    """Buffers partial writes into whole lines, appends each completed line
    to the running job's ``log_tail`` (bounded to the last 20), and parses
    ``"[k/4] ..."`` markers into ``stage``/``stage_index``. Must be called
    with ``_lock`` NOT already held by this thread's caller path other than
    via write(), which does not hold the lock while calling this."""
    with _lock:
        if _job is None:
            return
        buf = _job["_partial"] + chunk
        parts = buf.split("\n")
        _job["_partial"] = parts[-1]
        for line in parts[:-1]:
            line = line.rstrip("\r")
            if not line.strip():
                continue
            _job["log_tail"].append(line)
            m = _STAGE_RE.match(line.strip())
            if m:
                idx, total, desc = int(m.group(1)), int(m.group(2)), m.group(3).strip()
                _job["stage_index"] = idx
                _job["stage"] = f"{idx}/{total} {desc}".rstrip()


# ---------------------------------------------------------------------------
# Job state helpers
# ---------------------------------------------------------------------------

def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    """A JSON-serializable snapshot of a job dict, without internal bookkeeping
    fields (``_partial``) and with ``log_tail`` as a plain list."""
    return {
        "scan_id": job["scan_id"],
        "mode": job["mode"],
        "params": job["params"],
        "status": job["status"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "stage": job["stage"],
        "stage_index": job["stage_index"],
        "log_tail": list(job["log_tail"]),
        "error": job["error"],
        "pair_count": job["pair_count"],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start(mode: str, params: dict | None = None) -> dict:
    """Starts a background scan job, unless one is already running (in which
    case the running job is returned with ``started: False`` — never starts
    a second concurrent scan)."""
    if mode not in ("full", "fast"):
        raise ValueError(f"invalid scan mode: {mode!r}")
    params = dict(params or {})
    _install_stdout_proxy()

    global _job, _thread, _cancel_event
    with _lock:
        if _job is not None and _job["status"] == "running":
            return {"job": _public_job(_job), "started": False}

        try:
            scan_id = store.start_scan(mode, params)
        except Exception:
            scan_id = None

        job: dict[str, Any] = {
            "scan_id": scan_id,
            "mode": mode,
            "params": params,
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": None,
            "stage": None,
            "stage_index": 0,
            "log_tail": deque(maxlen=_LOG_TAIL_MAXLEN),
            "error": None,
            "pair_count": None,
            "_partial": "",
        }
        cancel_event = threading.Event()
        _job = job
        _cancel_event = cancel_event

        thread = threading.Thread(
            target=_worker, args=(job, cancel_event, mode, params), daemon=True,
        )
        _thread = thread

    thread.start()
    return {"job": _public_job(job), "started": True}


def status() -> dict | None:
    """The current (running) or most-recently-finished job, or None if no
    job has ever been started since process startup."""
    with _lock:
        if _job is None:
            return None
        return _public_job(_job)


def cancel() -> dict | None:
    """Requests cancellation of the running job. Returns the job snapshot
    (still 'running' at this instant — the transition to 'cancelled' happens
    asynchronously, once the worker thread's next stdout write observes the
    flag) or None if nothing is running."""
    with _lock:
        if _job is None or _job["status"] != "running":
            return None
        if _cancel_event is not None:
            _cancel_event.set()
        return _public_job(_job)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker(job: dict[str, Any], cancel_event: threading.Event, mode: str, params: dict) -> None:
    global _worker_ident
    with _lock:
        _worker_ident = threading.get_ident()

    scan_id = job["scan_id"]
    category = params.get("category", "all")
    min_sim = params.get("min_sim", 0.30)
    max_events = params.get("max_events")
    days = params.get("days")
    show_prices = mode == "full"

    try:
        # Deferred import: server.py imports this module at startup, so a
        # top-level `from server import ...` here would be circular.
        from server import _execute_scan

        result = _execute_scan(
            scan_id, category=category, min_sim=min_sim,
            max_events=max_events, show_prices=show_prices, days=days,
        )

        with _lock:
            if cancel_event.is_set():
                _finish_locked(job, "cancelled", scan_id=scan_id)
                return
            if result.get("error"):
                _finish_locked(job, "failed", scan_id=scan_id, error=result["error"])
            else:
                job["pair_count"] = result.get("count")
                _finish_locked(job, "completed", scan_id=scan_id)

    except ScanCancelled:
        with _lock:
            _finish_locked(job, "cancelled", scan_id=scan_id)

    except Exception as exc:  # worker must never crash the process
        with _lock:
            _finish_locked(job, "failed", scan_id=scan_id, error=str(exc))

    finally:
        with _lock:
            _worker_ident = None


def _finish_locked(job: dict[str, Any], terminal_status: str, scan_id: int | None,
                    error: str | None = None) -> None:
    """Marks ``job`` terminal and mirrors that outcome onto the store row.
    Caller must hold ``_lock``. A scan that already reached a terminal store
    status (e.g. _execute_scan already called store.fail_scan on discover
    raising a non-cancellation error) is left alone by cancel_scan's own
    WHERE status='running' guard."""
    job["status"] = terminal_status
    job["error"] = error
    job["finished_at"] = _now_iso()
    if scan_id is None:
        return
    try:
        if terminal_status == "cancelled":
            store.cancel_scan(scan_id)
        elif terminal_status == "failed" and error is not None:
            store.fail_scan(scan_id, error)
        # 'completed' is already persisted by _execute_scan (store.finish_scan).
    except Exception:
        pass

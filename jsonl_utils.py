"""Shared helpers for the append-only JSONL audit logs (signals, verdicts).

Centralises the size-bounding logic used by the alerter (alert_signals.jsonl,
ai_verify.jsonl) and the monitor (signals.jsonl) so there's one tested
implementation instead of copies (#32).
"""
from __future__ import annotations

import os
import pathlib


def cap_jsonl(path, max_bytes: int, keep_rows: int, tag: str = "jsonl") -> None:
    """Bound an append-only JSONL file: once it exceeds ``max_bytes``, rewrite
    keeping only the last ``keep_rows`` lines. The rewrite is ATOMIC (temp file +
    os.replace) so a crash or a scheduled-task kill landing mid-write can't
    corrupt/truncate the log (#27). Best-effort — never raises, so log maintenance
    can't break the caller's cycle (#24)."""
    path = pathlib.Path(path)
    tmp = None
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= keep_rows:
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines[-keep_rows:]) + "\n", encoding="utf-8")
        os.replace(tmp, path)            # atomic swap; leaves no partial file
        print(f"[{tag}] trimmed {path.name} to last {keep_rows} rows", flush=True)
    except Exception:
        try:
            if tmp is not None and tmp.exists():
                tmp.unlink()             # don't leave a stray .tmp behind
        except Exception:
            pass

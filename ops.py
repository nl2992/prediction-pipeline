"""Unified operator dashboard — one command for the full operational picture:

  1. health   : is the alerter running / verifier alive / delivery ok? (health.py)
  2. opportunities : which arbs are actionable / recurring / richest? (signal_report.py)
  3. matcher QA: any current matcher false-positives? (ai_verify_report.py)

Read-only; composes the existing tools. Exits 0 if healthy, 1 if degraded, so it
stays usable as a watchdog (`python ops.py || notify`).
"""
from __future__ import annotations

import sys

import ai_verify_report
import health
import signal_report


def main() -> int:
    health_text, ok = health.build_report()
    print(health_text)
    print()
    print(signal_report.format_report(signal_report.summarize(signal_report.load_signals())))
    print()
    print(ai_verify_report.format_report(ai_verify_report.summarize(ai_verify_report.load_verdicts())))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

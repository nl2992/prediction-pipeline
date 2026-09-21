"""Screenshot the two still images the README's demo section embeds:

    docs/img/dashboard-summary.png    -- SUMMARY view (F5): which pairs are
                                          arbable, and where.
    docs/img/dashboard-book-scan.png  -- BOOK SCAN modal (F4) open on a
                                          selected pair: top-of-book vs the
                                          depth-walked, fee-accurate fill.

tools/record_demo.py produces the animated walkthrough (docs/img/demo.gif);
this script only produces the two static stills above -- the roles don't
overlap and neither tool writes the other's files.

The default path drives a real FAST SCAN against a running server:

    .venv/bin/python -m uvicorn server:app --port 8000 &
    .venv/bin/python -m tools.capture_dashboard

Pass --payload to replay a saved /api/scan response (``{"pairs": [...],
"summary": {...}}``, e.g. a recorded real scan) instead of waiting on a live
one -- the request to /api/scan/fast is intercepted in the browser and
fulfilled from the file, so the server still needs to be running (it serves
the static page and /api/book-arb, which the BOOK SCAN screenshot needs) but
no live scan is required:

    .venv/bin/python -m tools.capture_dashboard --payload demo_payload.json

Needs Playwright (a dev-only dependency, not required to run the pipeline):

    pip install playwright && playwright install chromium
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "img"


class CaptureError(RuntimeError):
    """A screenshot target never appeared -- fail loudly, don't guess."""


def _load_payload(path: pathlib.Path) -> dict:
    data = json.loads(path.read_text())
    if "pairs" not in data or "summary" not in data:
        raise CaptureError(
            f"{path}: expected an /api/scan-shaped payload with 'pairs' and "
            f"'summary' keys, got {sorted(data.keys())}")
    data.setdefault("count", len(data["pairs"]))
    data.setdefault("elapsed", 0)
    data.setdefault("scanned_at", datetime.now(timezone.utc).isoformat())
    data.setdefault(
        "arb_count",
        sum(1 for p in data["pairs"] if (p.get("arb_net_profit") or 0) > 0))
    return data


def capture(row: int, url: str, scan_timeout_s: int, width: int, height: int,
            payload_path: pathlib.Path | None) -> int:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright missing: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    payload = None
    if payload_path is not None:
        try:
            payload = _load_payload(payload_path)
        except (OSError, json.JSONDecodeError, CaptureError) as exc:
            print(f"capture_dashboard: bad --payload: {exc}", file=sys.stderr)
            return 2

    OUT.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height},
                                     device_scale_factor=2)

            if payload is not None:
                # Intercept the FAST SCAN request only -- /api/book-arb stays
                # real, since it needs no network (it walks the poly_book/
                # kalshi_book ladders the payload already carries) and is
                # exactly what the second screenshot exercises.
                page.route("**/api/scan/fast**",
                            lambda route: route.fulfill(json=payload))

            page.goto(url, wait_until="networkidle")

            page.get_by_text("FAST SCAN").first.click()
            try:
                # Waiting on row count is NOT enough -- the empty-state row
                # ("No pairs match…") and the "SCANNING MARKETS…" overlay both
                # satisfy it, which screenshots a mid-scan spinner. The status
                # bar's #pair-count only leaves "—"/0 once results are
                # rendered, so wait on that instead.
                page.wait_for_function(
                    "() => { const t = (document.querySelector('#pair-count')||{}).textContent || '';"
                    "        return /^[0-9,]+$/.test(t.trim()) && parseInt(t.replace(/,/g,''),10) > 0; }",
                    timeout=scan_timeout_s * 1000)
            except PlaywrightTimeoutError as exc:
                raise CaptureError(
                    "#pair-count never became a positive number -- the scan "
                    "never completed (or the payload/live scan returned zero "
                    "pairs)") from exc
            page.wait_for_timeout(400)

            # ── SUMMARY view ────────────────────────────────────────────
            page.evaluate("showView('summary')")
            try:
                page.wait_for_selector("#summary-view", state="visible", timeout=5000)
                page.wait_for_function(
                    "() => { const b = document.getElementById('summary-body');"
                    "        return b && !b.textContent.includes('Run a scan first'); }",
                    timeout=5000)
            except PlaywrightTimeoutError as exc:
                raise CaptureError(
                    "SUMMARY view never rendered scan data (#summary-body "
                    "still shows the empty state)") from exc
            page.evaluate("document.getElementById('summary-body').scrollTop = 0")
            page.wait_for_timeout(200)
            summary_path = OUT / "dashboard-summary.png"
            page.screenshot(path=str(summary_path))
            print(f"wrote {summary_path}")

            # ── BOOK SCAN modal on the first pair ───────────────────────
            page.evaluate("showView('pairs')")
            try:
                page.wait_for_selector("#pairs-body tr[data-idx]", timeout=5000)
            except PlaywrightTimeoutError as exc:
                raise CaptureError("PAIRS view has no rows to select") from exc
            page.evaluate(f"selectRow({row})")
            page.evaluate("runBookArb()")
            try:
                page.wait_for_selector("#book-arb-modal.show", state="visible",
                                        timeout=10000)
            except PlaywrightTimeoutError as exc:
                raise CaptureError(
                    "#book-arb-modal never opened -- BOOK SCAN failed on the "
                    "first pair (no order-book depth and no ticker/token id "
                    "for a live fallback?)") from exc
            page.wait_for_timeout(300)
            book_scan_path = OUT / "dashboard-book-scan.png"
            page.screenshot(path=str(book_scan_path))
            print(f"wrote {book_scan_path}")

            browser.close()
    except (CaptureError, PlaywrightError) as exc:
        print(f"capture_dashboard: {exc}", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--row", type=int, default=0,
                    help="which pairs-table row to open BOOK SCAN on. The default "
                         "row is whatever sorts first, which is often a huge-edge "
                         "stale-book artifact -- a poor illustration. Pick a row "
                         "whose top-of-book edge visibly overstates the "
                         "depth-walked fill; that is the point of the panel.")
    ap.add_argument("--payload", type=pathlib.Path, default=None,
                     help="JSON file shaped like /api/scan's response "
                          "({'pairs': [...], 'summary': {...}}); replayed "
                          "instead of running a live scan")
    ap.add_argument("--scan-timeout", type=int, default=900,
                    help="seconds to wait for the scan to fill the table")
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--height", type=int, default=1050)
    a = ap.parse_args()
    return capture(a.row, a.url, a.scan_timeout, a.width, a.height, a.payload)


if __name__ == "__main__":
    sys.exit(main())

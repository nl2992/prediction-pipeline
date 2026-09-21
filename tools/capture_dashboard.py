"""Screenshot the live dashboard for the README demo.

The images in README.md are generated, not hand-taken, so they can be refreshed
after a UI change and so the numbers in them are always a real scan rather than
a mock-up:

    python -m uvicorn server:app --port 8000 &
    python -m tools.capture_dashboard

Writes docs/img/dashboard-*.png. The scan is a real full-catalog run against
both live APIs, so allow several minutes for it -- ``--scan-timeout`` bounds the
wait and the script fails loudly rather than quietly screenshotting an empty
table.

Needs Playwright (a dev-only dependency, not required to run the pipeline):

    pip install playwright && playwright install chromium
"""
from __future__ import annotations

import argparse
import pathlib
import sys

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "img"


def capture(url: str, scan_timeout_s: int, width: int, height: int) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright missing: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height},
                                device_scale_factor=2)
        page.goto(url, wait_until="networkidle")
        page.screenshot(path=str(OUT / "dashboard-idle.png"))
        print(f"wrote {OUT / 'dashboard-idle.png'}")

        # A real scan walks both full catalogs. Waiting on the row count is
        # NOT enough -- the empty-state row ("No pairs match…") and the
        # "SCANNING MARKETS…" overlay both satisfy it, which screenshotted a
        # mid-scan spinner. The status bar's #pair-count only leaves "—"/0 once
        # results are rendered, so wait on that.
        page.get_by_text("FAST SCAN").first.click()
        page.wait_for_function(
            "() => { const t = (document.querySelector('#pair-count')||{}).textContent || '';"
            "        return /^[0-9,]+$/.test(t.trim()) && parseInt(t.replace(/,/g,''),10) > 0; }",
            timeout=scan_timeout_s * 1000)
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "dashboard-pairs.png"))
        print(f"wrote {OUT / 'dashboard-pairs.png'}")

        rows = page.locator("tbody tr")
        if rows.count():
            rows.first.click()
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "dashboard-detail.png"))
            print(f"wrote {OUT / 'dashboard-detail.png'}")

        page.click("#nav-signals")
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "dashboard-signals.png"))
        print(f"wrote {OUT / 'dashboard-signals.png'}")
        browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--scan-timeout", type=int, default=900,
                    help="seconds to wait for the scan to fill the table")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    a = ap.parse_args()
    return capture(a.url, a.scan_timeout, a.width, a.height)


if __name__ == "__main__":
    sys.exit(main())

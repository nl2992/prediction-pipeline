"""Record the dashboard walkthrough used as the README demo.

Produces two artefacts from one scripted run:

    docs/img/demo.gif   inline in the README (GitHub renders GIFs, not mp4/webm)
    docs/demo.webm      the full-motion capture, linked from the README

Usage:
    python -m uvicorn server:app --port 8000 &
    python -m tools.record_demo                      # runs a live FAST SCAN
    python -m tools.record_demo --payload scan.json  # replay a saved scan

``--payload`` takes a JSON file of ``{"pairs": [...], "summary": {...}}`` — the
shape ``/api/scan`` returns. A live scan walks both full catalogs and takes
several minutes, which makes for a poor demo, so the committed recording
replays a SAVED REAL SCAN: the numbers on screen are genuine, only the waiting
is skipped. The README says so.

Needs Playwright and Pillow (dev-only, not needed to run the pipeline):

    pip install playwright Pillow && playwright install chromium
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
IMG = ROOT / "docs" / "img"

# (action, caption, hold) — hold is how many GIF frames the step lingers for,
# so a viewer can actually read a panel before it moves on.
LOAD_JS = """(d) => {
    allPairs = d.pairs;
    if (typeof lastSummary !== 'undefined') lastSummary = d.summary;
    if (typeof renderSummary === 'function') renderSummary(d.summary);
    renderPairs();
}"""


def _frames_to_gif(frames: list[bytes], out: pathlib.Path, width: int, ms: int) -> None:
    import io

    from PIL import Image

    imgs = []
    for raw in frames:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        # The terminal palette is tiny (dark bg, orange, green, red), so an
        # adaptive 64-colour palette is visually lossless and keeps the GIF small.
        imgs.append(im.quantize(colors=64, method=Image.MEDIANCUT))
    imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=ms, loop=0,
                 optimize=True, disposal=2)


def record(url: str, payload: pathlib.Path | None, width: int, height: int,
           gif_width: int, frame_ms: int) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright missing: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    IMG.mkdir(parents=True, exist_ok=True)
    data = json.loads(payload.read_text()) if payload else None
    frames: list[bytes] = []

    def shoot(pg, hold: int = 1) -> None:
        raw = pg.screenshot()
        frames.extend([raw] * hold)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": width, "height": height},
                                  record_video_dir=str(ROOT / "docs"),
                                  record_video_size={"width": width, "height": height})
        pg = ctx.new_page()
        pg.goto(url, wait_until="networkidle")
        pg.wait_for_timeout(900)
        shoot(pg, 3)                                   # 1. idle, both venues green

        if data:
            pg.evaluate(LOAD_JS, data)
        else:
            pg.get_by_text("FAST SCAN").first.click()
            pg.wait_for_function(
                "() => { const t=(document.querySelector('#pair-count')||{}).textContent||'';"
                "        return /^[0-9,]+$/.test(t.trim()) && parseInt(t.replace(/,/g,''),10)>0; }",
                timeout=900_000)
        pg.wait_for_timeout(700)
        shoot(pg, 4)                                   # 2. matched pairs

        # 3. sort by executable dollars — "where can I actually deploy?"
        for key in ("exec_profit", "arb_net_accurate"):
            pg.evaluate(f"typeof sortBy === 'function' && sortBy({json.dumps(key)})")
            pg.wait_for_timeout(500)
            shoot(pg, 3)

        # 4. switch population
        for gate in ("all", "alerter_gate"):
            pg.evaluate(f"typeof setGate === 'function' && setGate('{gate}')")
            pg.wait_for_timeout(500)
            shoot(pg, 3)

        # 5. the summary: how many are arbable, and where is the alpha.
        # The view scrolls an inner container (#summary-body), NOT the window —
        # scrolling the window here silently does nothing and the recording
        # never reaches the top-15 table.
        pg.evaluate("showView('summary')")
        pg.wait_for_timeout(900)
        shoot(pg, 5)
        steps = pg.evaluate(
            "() => { const e = document.querySelector('#summary-body');"
            "        return e ? Math.ceil((e.scrollHeight - e.clientHeight) / 420) : 0; }")
        for n in range(1, min(steps, 5) + 1):
            pg.evaluate(f"document.querySelector('#summary-body').scrollTop = {n * 420}")
            pg.wait_for_timeout(650)
            shoot(pg, 4)
        pg.evaluate("const e=document.querySelector('#summary-body'); if(e) e.scrollTop=0;")

        # 6. depth-walked book scan on one pair
        pg.evaluate("showView('pairs')")
        pg.wait_for_timeout(500)
        pg.evaluate("selectRow(0)")
        pg.wait_for_timeout(400)
        shoot(pg, 2)
        pg.evaluate("runBookArb()")
        try:
            pg.wait_for_selector("#book-arb-modal.show", timeout=30_000)
        except Exception:
            print("warning: BOOK SCAN did not open", file=sys.stderr)
        pg.wait_for_timeout(900)
        shoot(pg, 6)
        pg.evaluate("typeof closeBookArb === 'function' && closeBookArb()")
        pg.wait_for_timeout(400)
        shoot(pg, 2)

        video = pg.video
        ctx.close()
        browser.close()
        if video:
            src = pathlib.Path(video.path())
            dst = ROOT / "docs" / "demo.webm"
            src.replace(dst)
            print(f"wrote {dst} ({dst.stat().st_size // 1024} KB)")

    gif = IMG / "demo.gif"
    _frames_to_gif(frames, gif, gif_width, frame_ms)
    print(f"wrote {gif} ({gif.stat().st_size // 1024} KB, {len(frames)} frames)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--payload", type=pathlib.Path,
                    help="saved /api/scan JSON to replay instead of scanning live")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--gif-width", type=int, default=900)
    ap.add_argument("--frame-ms", type=int, default=260)
    a = ap.parse_args()
    return record(a.url, a.payload, a.width, a.height, a.gif_width, a.frame_ms)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Pixel-compare every slide step of real decks under two engines.

Each deck is served read-only from where it lives; requests for its engine files
are answered from the chosen engine directory by Playwright routing, so no deck is
copied or edited. Differing steps are saved as base/new PNG pairs.

    python3 tools/compare_engines.py --base-sha 777bda0 DECK [DECK ...]
    python3 tools/compare_engines.py --base-engine /tmp/old/engine --new-engine engine DECK ...

DECK is an index.html, a deck directory, or a deck alias. Default --new-engine is
this repo's engine/. Rerun a deck whose only differences are a few pixels: one-off
capture noise does not repeat.
"""
import argparse
import io
import mimetypes
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from _common import ensure_tool_python  # noqa: E402

ensure_tool_python(("playwright", "PIL"), __file__)

from PIL import Image, ImageChops  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import ws_deck  # noqa: E402
from _common import DeckServer, wait_for_deck_ready_sync  # noqa: E402

NO_ANIM = "*,*::before,*::after{transition:none!important;animation:none!important}"


def route_engine(engine_dir):
    def handler(route):
        rel = route.request.url.split("?")[0].split("/engine/", 1)[1]
        p = engine_dir / rel
        if not p.is_file():
            return route.continue_()
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        route.fulfill(status=200, body=p.read_bytes(),
                      headers={"Content-Type": ctype, "Cache-Control": "no-store"})
    return handler


def capture(browser, index, engine_dir):
    page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
    page.route("**/engine/**", route_engine(engine_dir))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    shots = {}
    with DeckServer(index) as served:
        page.goto(served.url, wait_until="load")
        wait_for_deck_ready_sync(page, None, timeout_ms=60000)
        page.add_style_tag(content=NO_ANIM)
        plan = page.evaluate("() => WS.slides.map(s => WS.slideSteps.get(s) || 0)")
        for i, n in enumerate(plan):
            for s in range(n + 1):
                page.evaluate("""([i, s]) => { WS.goToSlide(i, s);
                    document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} }); }""", [i, s])
                page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                shots[(i + 1, s)] = page.screenshot()
    page.close()
    return shots, errors


def extract_engine(sha, dest):
    """Unpack engine/ as of commit ``sha`` into ``dest`` and return its path."""
    archive = subprocess.run(["git", "-C", str(REPO), "archive", sha, "engine"],
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return dest / "engine"


def diff_steps(base_shots, new_shots, out):
    """Return one line per step that differs; save each differing pair as PNGs in ``out``."""
    diffs = []
    for slide, step in sorted(set(base_shots) | set(new_shots)):
        key = (slide, step)
        if key not in base_shots or key not in new_shots:
            diffs.append(f"{slide}:{step} only under one engine")
            continue
        base_img = Image.open(io.BytesIO(base_shots[key])).convert("RGB")
        new_img = Image.open(io.BytesIO(new_shots[key])).convert("RGB")
        box = ImageChops.difference(base_img, new_img).getbbox()
        if box:
            out.mkdir(parents=True, exist_ok=True)
            base_img.save(out / f"s{slide}-{step}-base.png")
            new_img.save(out / f"s{slide}-{step}-new.png")
            diffs.append(f"{slide}:{step} differs in {box} (pngs in {out})")
    return diffs


def compare_deck(browser, index, base_engine, new_engine, out_root):
    """Print the comparison of one deck; return True when anything differs."""
    base_shots, base_errors = capture(browser, index, base_engine)
    new_shots, new_errors = capture(browser, index, new_engine)
    diffs = diff_steps(base_shots, new_shots, out_root / index.parent.name)
    added_errors = [e for e in new_errors if e not in base_errors]
    print(f"{index.parent.name}: {len(base_shots)} / {len(new_shots)} steps, {len(diffs)} differ, "
          f"{len(added_errors)} new page errors")
    for line in diffs[:15] + [f"ERR {e[:200]}" for e in added_errors[:5]]:
        print("    " + line)
    return bool(diffs or added_errors)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("decks", nargs="+")
    base = ap.add_mutually_exclusive_group(required=True)
    base.add_argument("--base-sha", help="commit of this repo whose engine/ is the baseline")
    base.add_argument("--base-engine", type=Path, help="baseline engine directory")
    ap.add_argument("--new-engine", type=Path, default=REPO / "engine")
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "compare_engines")
    args = ap.parse_args()

    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        base_engine = args.base_engine or extract_engine(args.base_sha, Path(tmp))
        base_engine, new_engine = base_engine.resolve(), args.new_engine.resolve()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for deck in args.decks:
                index = ws_deck.resolve_deck(deck)
                if compare_deck(browser, index, base_engine, new_engine, args.out):
                    failed = True
            browser.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

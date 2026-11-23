#!/usr/bin/env python3
"""Morph geometry regression for data-morph (tests/fixtures/step-model/morph-deck.html).

Opens the fixture deck in Chromium and WebKit (the engine behind Safari and Presto) at several
window sizes and shapes, each with the engine's own scaling (CSS transform: scale) and with
Presto's (CSS zoom on every slide, via applyPrestoViewport). It steps 1 -> 2 -> 3 and back
again, pausing every animation at fixed fractions of the morph and measuring each moving element:
SVG elements in their own SVG user units (from the SVG's rendered box and viewBox, independent of
the engine's CTM maths), HTML boxes and the cross-SVG pair in slide px. It requires:
  - at rest, every element sits where the fixture draws it;
  - at every paused moment, the moving element's centre and size equal the straight-line blend of
    the old and new rest boxes at the animation's eased progress (so t=0 is exactly the old box);
  - the old element is hidden during the move (no ghost), the new one fully shown;
  - stepping back mid-move starts the reverse move from where the element is at that moment;
  - goToSlide (jumps, tools), reduced motion and WS.prepareForExport never animate and land on
    the rest positions at once.
Exit status 1 on any failure. Run with the WebSlides export venv (Playwright with WebKit):
    ~/.venvs/webslides-export/bin/python tests/step_morph_check.py [--browsers chromium,webkit] [--quick]
"""
import argparse
import functools
import http.server
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
DECK = "tests/fixtures/step-model/morph-deck.html"
SIZES = [(1280, 720), (1920, 1080), (1000, 900), (1600, 700), (800, 450)]
QUICK_SIZES = [(1280, 720), (1000, 900), (800, 450)]
MODES = ["scale", "zoom"]
TOL = 0.5                      # SVG user units or slide px
FRACTIONS = [0.0, 0.25, 0.5, 0.75]

# Rest boxes [cx, cy, w, h]; None skips a dimension (stroked lines, text widths).
REST = {
    1: {"f-e1": [100, 100, 16, 16], "f-e2": [100, 200, 16, 16], "f-ci1": [160, 100, None, 80],
        "f-grp": [235, 270, 70, 40], "f-x": [740, 400, 20, 20], "ha": [160, 560, 120, 40]},
    2: {"t-e1": [500, 150, 8, 8], "t-e2": [450, 280, 16, 16], "t-ci1": [500, 150, None, 60],
        "t-grp": [635, 270, 70, 40], "o-x": [960, 200, 40, 40], "hb": [1020, 630, 240, 60]},
    3: {"u-e1": [700, 50, 24, 24]},
}
# Moves by step change: (old element, new element)
MOVES = {
    (1, 2): [("f-e1", "t-e1"), ("f-e2", "t-e2"), ("f-ci1", "t-ci1"), ("f-lab", "t-lab"),
             ("f-grp", "t-grp"), ("f-x", "o-x"), ("ha", "hb")],
    (2, 3): [("t-e1", "u-e1")],
}
LABEL_SHIFT = (440, 30)        # t-lab is f-lab moved by (300 + 140, 30)

MEASURE_JS = r"""
() => {
  const svgFor = name => [...document.querySelectorAll('svg[data-ws-original-img]')]
      .find(s => s.getAttribute('data-ws-original-img').includes(name));
  const main = svgFor('morph.svg'), other = svgFor('morph-other.svg');
  const slide = WS.slides[0], sr = slide.getBoundingClientRect(), sk = 1280 / sr.width;
  const userBox = (svg, el) => {
    const r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal, b = el.getBoundingClientRect();
    const kx = vb.width / r.width, ky = vb.height / r.height;
    return [vb.x + ((b.left + b.right) / 2 - r.left) * kx, vb.y + ((b.top + b.bottom) / 2 - r.top) * ky,
            b.width * kx, b.height * ky];
  };
  const slideBox = el => { const b = el.getBoundingClientRect();
    return [((b.left + b.right) / 2 - sr.left) * sk, ((b.top + b.bottom) / 2 - sr.top) * sk, b.width * sk, b.height * sk]; };
  const byId = (svg, id) => svg.querySelector('[id$="-' + id + '"]');
  const out = {};
  ['f-e1', 'f-e2', 'f-ci1', 'f-lab', 'f-grp', 't-e1', 't-e2', 't-ci1', 't-lab', 't-grp', 'u-e1']
      .forEach(id => { out[id] = userBox(main, byId(main, id)); });
  out['f-x'] = slideBox(byId(main, 'f-x'));
  out['o-x'] = slideBox(byId(other, 'o-x'));
  out['ha'] = slideBox(document.getElementById('ha'));
  out['hb'] = slideBox(document.getElementById('hb'));
  const op = el => +getComputedStyle(el).opacity;
  out.opacity = {};
  ['f-e1', 't-e1', 'u-e1', 'f-x'].forEach(id => { out.opacity[id] = op(byId(main, id)); });
  out.opacity['o-x'] = op(byId(other, 'o-x'));
  out.opacity.ha = op(document.getElementById('ha'));
  out.opacity.hb = op(document.getElementById('hb'));
  return out;
}
"""

# Morph (transform) animations by target id; progress is the eased fraction.
MORPHS_JS = r"""
() => document.getAnimations()
  .filter(a => a.effect && a.effect.target && a.effect.getKeyframes().some(k => k.transform))
  .map(a => ({ id: a.effect.target.id.replace(/^ws-svg\d+-/, ''), progress: a.effect.getComputedTiming().progress,
               duration: a.effect.getComputedTiming().duration, state: a.playState }))
"""

PAUSE_JS = "(f) => document.getAnimations().forEach(a => { const d = a.effect.getComputedTiming().duration; a.pause(); a.currentTime = f * d; })"
FINISH_JS = "() => document.getAnimations().forEach(a => a.finish())"


def serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(REPO)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def lerp(a, b, p):
    return [None if x is None or y is None else x + (y - x) * p for x, y in zip(a, b)]


def dev(got, want):
    return max((abs(g - w) for g, w in zip(got, want) if w is not None), default=0.0)


class Config:
    def __init__(self, page, label):
        self.pg, self.label, self.fail = page, label, []
        self.rest = {}

    def check(self, what, value, limit=TOL):
        if value > limit:
            self.fail.append(f"{self.label}: {what} off by {value:.3f}")
        return value

    def measure(self):
        return self.pg.evaluate(MEASURE_JS)

    def rest_boxes(self, step):
        m = self.measure()
        out = {}
        for name, want in REST.get(step, {}).items():
            self.check(f"step {step} rest {name}", dev(m[name], want))
            out[name] = m[name]
        if step == 1:
            out["f-lab"] = m["f-lab"]
        if step == 2:
            want = [m["f-lab"][0] + LABEL_SHIFT[0], m["f-lab"][1] + LABEL_SHIFT[1], m["f-lab"][2], m["f-lab"][3]]
            self.check("step 2 rest t-lab", dev(m["t-lab"], want))
            out["t-lab"] = m["t-lab"]
        self.rest[step] = out
        return m

    def no_morphs(self, what):
        running = [a for a in self.pg.evaluate(MORPHS_JS) if a["state"] != "finished"]
        if running:
            self.fail.append(f"{self.label}: {what} started morphs {running}")


def run_config(name, opener, url, w, h, mode, reduced=False):
    page = opener.new_page() if reduced else opener.new_page(viewport={"width": w, "height": h})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.evaluate("WS.ready.then(() => true)")
    if mode == "zoom":
        page.evaluate("window.applyPrestoEditViewport()")
    cfg = Config(page, f"{name} {w}x{h} {mode}" + (" reduced-motion" if reduced else ""))
    page.evaluate("WS.goToSlide(0, 3)")
    cfg.rest_boxes(3)
    page.evaluate("WS.goToSlide(0, 2)")
    cfg.no_morphs("goToSlide")
    cfg.rest_boxes(2)
    page.evaluate("WS.goToSlide(0, 1)")
    cfg.rest_boxes(1)

    def move(direction, src, dst):
        page.evaluate("WS.nextStep()" if direction == "fwd" else "WS.prevStep()")
        pairs = MOVES[(min(src, dst), max(src, dst))]
        if direction == "back":
            pairs = [(b, a) for a, b in pairs]
        morphs = {a["id"]: a for a in page.evaluate(MORPHS_JS)}
        missing = [new for _old, new in pairs if new not in morphs]
        if missing:
            cfg.fail.append(f"{cfg.label}: {direction} {src}->{dst} no morph on {missing}")
        for f in FRACTIONS:
            page.evaluate(PAUSE_JS, f)
            m = cfg.measure()
            progress = {a["id"]: a["progress"] for a in page.evaluate(MORPHS_JS)}
            for old, new in pairs:
                if new not in progress:
                    continue
                want = lerp(cfg.rest[src][old], cfg.rest[dst][new], progress[new])
                if old in ("f-ci1", "t-ci1"):
                    want[2] = None
                if old.endswith("lab"):
                    want[2] = want[3] = None
                cfg.check(f"{direction} {src}->{dst} {new} at t={f}", dev(m[new], want))
                if old in m["opacity"] and m["opacity"][old] > 0.01:
                    cfg.fail.append(f"{cfg.label}: {old} still visible ({m['opacity'][old]}) while {new} moves")
                if new in m["opacity"] and m["opacity"][new] < 0.99:
                    cfg.fail.append(f"{cfg.label}: {new} not fully shown ({m['opacity'][new]}) while moving")
        page.evaluate(FINISH_JS)
        cfg.rest_boxes(dst)

    if reduced:
        page.evaluate("WS.nextStep()")
        cfg.no_morphs("reduced motion")
        cfg.rest_boxes(2)
        page.close()
        return cfg, errors

    move("fwd", 1, 2)
    move("fwd", 2, 3)
    page.evaluate("WS.nextStep()")          # on to slide 1
    page.evaluate("WS.prevStep()")          # back lands on step 3, no animation
    cfg.no_morphs("prevStep into a slide")
    page.evaluate(FINISH_JS)
    cfg.rest_boxes(3)
    move("back", 3, 2)
    move("back", 2, 1)

    # Reverse mid-move: the reverse starts where the element is at that moment
    page.evaluate("WS.nextStep()")
    page.evaluate(PAUSE_JS, 0.5)
    mid = cfg.measure()["t-e1"]
    page.evaluate("WS.prevStep()")
    page.evaluate(PAUSE_JS, 0.0)
    cfg.check("reverse mid-move start of f-e1", dev(cfg.measure()["f-e1"], mid))
    page.evaluate(FINISH_JS)
    cfg.rest_boxes(1)

    # Export mode: no animations at all, positions exact at once
    page.evaluate("WS.prepareForExport()")
    for action, step in (("WS.nextStep()", 2), ("WS.nextStep()", 3), ("WS.prevStep()", 2), ("WS.prevStep()", 1)):
        page.evaluate(action)
        cfg.no_morphs(f"export mode {action}")
        cfg.rest_boxes(step)
    page.close()
    return cfg, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--browsers", default="chromium,webkit")
    ap.add_argument("--quick", action="store_true", help="three window sizes instead of five")
    args = ap.parse_args()
    srv = serve()
    url = f"http://127.0.0.1:{srv.server_address[1]}/{DECK}"
    failures = []
    with sync_playwright() as p:
        for name in args.browsers.split(","):
            browser = getattr(p, name).launch()
            for (w, h) in (QUICK_SIZES if args.quick else SIZES):
                for mode in MODES:
                    cfg, errors = run_config(name, browser, url, w, h, mode)
                    failures += cfg.fail + [f"{cfg.label}: page error {e}" for e in errors]
                    print(f"{cfg.label}: {'ok' if not cfg.fail and not errors else f'{len(cfg.fail) + len(errors)} problem(s)'}")
            context = browser.new_context(reduced_motion="reduce", viewport={"width": 1280, "height": 720})
            cfg, errors = run_config(name, context, url, 1280, 720, "scale", reduced=True)
            failures += cfg.fail + errors
            print(f"{name} reduced motion: {'ok' if not cfg.fail and not errors else 'problems'}")
            context.close()
            browser.close()
    srv.shutdown()
    if failures:
        print("FAIL\n  " + "\n  ".join(failures[:60]))
        if len(failures) > 60:
            print(f"  ... and {len(failures) - 60} more")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

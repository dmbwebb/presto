#!/usr/bin/env python3
"""Presto's slide-frame layout (presto/edit-layout.js) in WebKit and Chromium.

The same probe as tests/chrome-edit-preview-layout.js
(tests/engine-redesign-layout-probe.js) runs on examples/kitchen-sink in
Playwright's Chromium and WebKit and in the system WebKit (Presto's), for
tall, wide and square windows, in edit mode (232px rail, 72px step strip,
28px padding, with and without the 360px inspector) and audience mode: the
section nav's top on the slide's top edge, the footer's bottom on its bottom
edge, both the slide's width, the progress bar along its top, light canvas
or letterbox, appendix slides highlighting no section.

It also checks that nothing the layout writes can reach a deck file: Presto
saves slide bodies only (the visual editor's serializeSection; the section
tag comes from the file), so every slide must serialise identically before
and after the layout, with no layout property in it.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DECK = REPO / "examples" / "kitchen-sink" / "index.html"
LAYOUT_JS = REPO / "presto" / "edit-layout.js"
PROBE_JS = REPO / "tests" / "engine-redesign-layout-probe.js"
WYSIWYG_JS = REPO / "presto" / "wysiwyg.js"
HARNESS_SRC = REPO / "tests" / "engine-redesign-wkharness.swift"
sys.path.insert(0, str(REPO / "tools"))

from _common import DeckServer  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - system python without Playwright
    sync_playwright = None

SIZES = [("tall", 900, 1130), ("wide", 1800, 820), ("square", 1100, 1100)]
EDIT = {"mode": "edit", "insetLeft": 232, "insetTop": 0, "insetBottom": 72, "padding": 28}
CONFIGS = [dict(EDIT, insetRight=0), dict(EDIT, insetRight=360), {"mode": "audience"}]

SLIDES_JS = "WS.ready.then(() => [0, 2, 3, WS.slides.indexOf(WS.appendixSlides[0])])"

RUN_JS = """async (configs) => {
    await WS.ready;
    const slides = [0, 2, 3, WS.slides.indexOf(WS.appendixSlides[0])];
    const out = [];
    for (const cfg of configs) out.push(await window.__prestoLayoutProbe(Object.assign({ slides }, cfg)));
    return out;
}"""


OVERVIEW_JS = """async (cfg) => {
    PrestoLayout.apply(cfg);
    WS.toggleOverview();
    await new Promise(r => setTimeout(r, 100));
    const item = document.querySelector('.ws-overview-item');
    const thumb = item.querySelector('.ws-overview-slide');
    const out = {
        background: getComputedStyle(document.body).backgroundColor,
        overflowY: getComputedStyle(document.body).overflowY,
        scrollable: document.body.scrollHeight - document.body.clientHeight,
        frameHidden: getComputedStyle(document.getElementById('presto-slide-frame')).display === 'none',
        itemW: item.getBoundingClientRect().width, // the engine scales clones to the card's offsetWidth
        thumbW: thumb.getBoundingClientRect().width
    };
    WS.toggleOverview();
    return out;
}"""


def failures_of(outs):
    return [f for out in outs for f in out["failures"]]


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class PlaywrightLayoutTests(unittest.TestCase):
    def run_engine(self, engine):
        with sync_playwright() as pw, DeckServer(DECK) as server:
            try:
                browser = getattr(pw, engine).launch()
            except Exception as exc:  # noqa: BLE001 - browser build not installed
                self.skipTest(f"Playwright {engine} unavailable: {exc}")
            try:
                page = browser.new_page(viewport={"width": SIZES[0][1], "height": SIZES[0][2]})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(server.url)
                page.wait_for_function("document.documentElement.dataset.wsReady === '1'", timeout=30000)
                page.add_script_tag(content=LAYOUT_JS.read_text(encoding="utf-8"))
                page.add_script_tag(content=PROBE_JS.read_text(encoding="utf-8"))
                checked = 0
                for label, w, h in SIZES:
                    page.set_viewport_size({"width": w, "height": h})
                    outs = page.evaluate(RUN_JS, CONFIGS)
                    self.assertEqual([], failures_of(outs), f"{engine} {label} {w}x{h}")
                    self.assertEqual({"width": w, "height": h}, outs[0]["viewport"])
                    checked += sum(len(o["results"]) for o in outs)
                self.assertEqual(len(SIZES) * len(CONFIGS) * 4, checked)
                overview = page.evaluate(OVERVIEW_JS, dict(EDIT, insetRight=0))
                self.assertEqual("rgb(231, 234, 238)", overview["background"], f"{engine}: light overview")
                self.assertIn(overview["overflowY"], ("auto", "scroll"), f"{engine}: overview scrolls")
                self.assertGreater(overview["scrollable"], 0, f"{engine}: overview taller than the window")
                self.assertTrue(overview["frameHidden"], f"{engine}: no slide frame over the overview")
                self.assertAlmostEqual(overview["thumbW"], overview["itemW"], delta=2,
                                       msg=f"{engine}: thumbnails fill their cards (Presto's zoom dropped)")
                self.assertEqual([], errors)
            finally:
                browser.close()

    def test_chromium(self):
        self.run_engine("chromium")

    def test_webkit(self):
        self.run_engine("webkit")

    def test_layout_never_reaches_saved_slides(self):
        with sync_playwright() as pw, DeckServer(DECK) as server:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(server.url)
                page.wait_for_function("document.documentElement.dataset.wsReady === '1'", timeout=30000)
                page.add_script_tag(content=WYSIWYG_JS.read_text(encoding="utf-8"))
                serialize = "() => WS.slides.map(s => window.__prestoWysiwyg.serializeSection(s))"
                page.wait_for_function("!!(window.__prestoWysiwyg && window.__prestoWysiwyg.serializeSection)")
                before = page.evaluate(serialize)
                page.add_script_tag(content=LAYOUT_JS.read_text(encoding="utf-8"))
                for cfg in (dict(EDIT, insetRight=360), {"mode": "audience", "viewportWidth": 960, "viewportHeight": 540}):
                    geometry = page.evaluate("cfg => PrestoLayout.apply(cfg)", cfg)
                    self.assertLess(geometry["scale"], 1, "compensation active")
                    for i in range(len(before)):
                        page.evaluate("i => WS.goToSlide(i, 'end')", i)
                    after = page.evaluate(serialize)
                    self.assertEqual(before, after, f"{cfg['mode']}: slide bodies unchanged by the layout")
                    leaked = [s for s in after if any(k in s for k in ("--ws-katex", "presto-layout", "presto-slide-frame", "zoom:"))]
                    self.assertEqual([], leaked)
                # The deck container and slide boxes carry the layout, and are never serialised
                outer = page.evaluate("() => WS.slides[3].outerHTML.slice(0, 300)")
                self.assertIn("zoom", outer, "the layout lives on the slide's own box (not saved: Presto keeps the file's tag)")
            finally:
                browser.close()


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "needs macOS and swiftc")
class SystemWebKitLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.harness = Path(cls.tmp.name) / "wkharness"
        subprocess.run(["swiftc", "-framework", "WebKit", "-framework", "Cocoa", str(HARNESS_SRC), "-o", str(cls.harness)],
                       check=True, capture_output=True, text=True, timeout=300)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def harness_run(self, url, script, width, height, extra=()):
        path = Path(self.tmp.name) / "script.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([str(self.harness), "--url", url, "--script", str(path), "--width", str(width),
                               "--height", str(height), "--timeout", "90", *extra],
                              capture_output=True, text=True, timeout=120)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        if line is None:
            self.fail(f"harness failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr[-2000:]}")
        return json.loads(line[len("RESULT "):])

    def test_layout_in_system_webkit(self):
        script = ("const slides = await " + SLIDES_JS + ";\nconst out = [];\n"
                  "for (const cfg of " + json.dumps(CONFIGS) + ") out.push(await window.__prestoLayoutProbe(Object.assign({ slides }, cfg)));\n"
                  "return JSON.stringify(out);")
        with DeckServer(DECK) as server:
            for label, w, h in SIZES:
                outs = self.harness_run(server.url, script, w, h,
                                        ("--inject", str(LAYOUT_JS), "--inject", str(PROBE_JS), "--block-network"))
                self.assertEqual([], failures_of(outs), f"system WebKit {label} {w}x{h}")
                self.assertEqual({"width": w, "height": h}, outs[0]["viewport"])
                self.assertEqual(len(CONFIGS) * 4, sum(len(o["results"]) for o in outs))

    def test_offline_deck_over_file_url(self):
        # Presto's own route: file:// with network blocked. KaTeX comes from
        # the engine copy, the theme fonts from engine/fonts.
        script = """
            await WS.ready; await document.fonts.ready;
            const k = WS.slides.flatMap(s => Array.from(s.querySelectorAll('.katex')));
            return JSON.stringify({ katex: k.length, family: k.length ? getComputedStyle(k[0]).fontFamily : '',
                fallback: document.querySelectorAll('[data-ws-katex-fallback]').length,
                faces: Array.from(document.fonts).filter(f => f.status === 'loaded').map(f => f.family.replace(/["']/g, '')) });
        """
        url = DECK.as_uri()
        offline = self.harness_run(url, script, 1280, 720, ("--read-access", str(REPO), "--block-network"))
        self.assertGreater(offline["katex"], 10)
        self.assertIn("KaTeX_Main", offline["family"])
        self.assertEqual(3, offline["fallback"], "two scripts and the stylesheet from the engine copy")
        for face in ("KaTeX_Main", "KaTeX_Math", "Lato"):
            self.assertIn(face, offline["faces"])


if __name__ == "__main__":
    unittest.main()

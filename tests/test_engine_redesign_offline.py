#!/usr/bin/env python3
"""Decks render offline: vendored KaTeX fallback and local theme fonts.

Loads examples/kitchen-sink (which links KaTeX from jsDelivr like Duncan's
decks) in headless Chromium and WebKit with every request off 127.0.0.1
aborted, and with only one of the KaTeX files failing. The "online" baseline
answers the CDN requests from engine/vendor/katex (byte-identical files), so
no test touches the network. Checks: WS.ready resolves, every formula is
rendered with KaTeX's stylesheet in its original cascade position, the KaTeX
and Lato faces load, and nothing asks Google Fonts for anything.
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DECK = REPO / "examples" / "kitchen-sink" / "index.html"
VENDOR = REPO / "engine" / "vendor" / "katex"
sys.path.insert(0, str(REPO / "tools"))

from _common import DeckServer  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - system python without Playwright
    sync_playwright = None

CONTENT_TYPES = {".css": "text/css", ".js": "application/javascript", ".woff2": "font/woff2",
                 ".woff": "font/woff", ".ttf": "font/ttf"}
CDN = re.compile(r"^https://cdn\.jsdelivr\.net/npm/katex@0\.16\.21/dist/(.+?)(?:[?#].*)?$")

STATE_JS = """async () => {
    await WS.ready;
    await document.fonts.ready;
    const slides = WS.slides;
    const katex = slides.flatMap(s => Array.from(s.querySelectorAll('.katex')));
    const first = katex[0];
    const raw = slides.filter(s => /\\$[^$\\s][^$]*\\$/.test(Array.from(s.querySelectorAll('p, li, td, div'))
        .filter(el => !el.closest('.katex, code, pre, aside')).map(el => el.childNodes.length ? Array.from(el.childNodes)
        .filter(n => n.nodeType === 3).map(n => n.data).join('') : '').join(' '))).length;
    const faces = Array.from(document.fonts).filter(f => f.status === 'loaded').map(f => f.family.replace(/["']/g, '') + ' ' + f.style + ' ' + f.weight);
    return {
        ready: document.documentElement.dataset.wsReady === '1',
        katexLoaded: !!(window.katex && window.katex.render),
        katexCount: katex.length,
        errors: slides.reduce((n, s) => n + s.querySelectorAll('.katex-error').length, 0),
        unrendered: raw,
        fontFamily: first ? getComputedStyle(first).fontFamily : null,
        fontSizeRatio: first ? parseFloat(getComputedStyle(first).fontSize) / parseFloat(getComputedStyle(first.parentElement).fontSize) : null,
        mathmlHidden: first ? getComputedStyle(first.querySelector('.katex-mathml')).position === 'absolute' : null,
        fallbackScripts: document.querySelectorAll('script[data-ws-katex-fallback]').length,
        fallbackCss: document.querySelectorAll('link[data-ws-katex-fallback]').length,
        faces: faces,
        latoUpright: document.fonts.check('400 20px Lato') && document.fonts.check('700 20px Lato'),
    };
}"""


def serve_vendor(route):
    m = CDN.match(route.request.url)
    path = VENDOR / m.group(1) if m else None
    if path and path.is_file():
        route.fulfill(path=str(path), headers={"content-type": CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
                                               "access-control-allow-origin": "*"})
    else:
        route.abort()


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class OfflineDeckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls.server = DeckServer(DECK)
        cls.server.__enter__()
        cls.browsers = {"chromium": cls._pw.chromium.launch()}
        try:
            cls.browsers["webkit"] = cls._pw.webkit.launch()
        except Exception:  # noqa: BLE001 - WebKit build not installed
            pass
        cls.baseline = {name: cls.load(name, cls.online) for name in cls.browsers}

    @classmethod
    def tearDownClass(cls):
        for browser in cls.browsers.values():
            browser.close()
        cls.server.__exit__(None, None, None)
        cls._pw.stop()

    # Routing: everything that is not the local deck server is decided here.
    @staticmethod
    def online(route):
        serve_vendor(route)

    @staticmethod
    def offline(route):
        route.abort("internetdisconnected")

    @classmethod
    def failing(cls, name):
        def handler(route):
            if route.request.url.split("?")[0].endswith(name):
                route.abort("connectionfailed")
            else:
                serve_vendor(route)
        return handler

    @classmethod
    def load(cls, browser_name, external):
        page = cls.browsers[browser_name].new_page(viewport={"width": 1280, "height": 720})
        requests = []
        page.on("request", lambda r: requests.append(r.url))
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route(re.compile(r"^(?!http://127\.0\.0\.1[:/]).*"), external)
        try:
            page.goto(cls.server.url)
            page.wait_for_function("document.documentElement.dataset.wsReady === '1'", timeout=30000)
            state = page.evaluate(STATE_JS)
        finally:
            page.close()
        state["pageErrors"] = errors
        state["googleRequests"] = [u for u in requests if "fonts.googleapis" in u or "fonts.gstatic" in u]
        return state

    def check(self, browser_name, state, fallback_scripts, fallback_css, deck_errors=0):
        base = self.baseline[browser_name]
        label = f"{browser_name}: "
        self.assertTrue(base["katexCount"] > 10, label + "kitchen-sink baseline renders maths")
        # deck_errors: thrown by the deck's own onload handler before the engine runs
        self.assertEqual(deck_errors, len(state["pageErrors"]), label + f"page errors {state['pageErrors']}")
        self.assertTrue(state["ready"], label + "WS.ready resolved")
        self.assertTrue(state["katexLoaded"], label + "KaTeX available")
        self.assertEqual(base["katexCount"], state["katexCount"], label + "every formula rendered")
        self.assertEqual(base["errors"], state["errors"], label + "no new KaTeX errors")
        self.assertEqual(base["unrendered"], state["unrendered"], label + "no $...$ left as text")
        self.assertIn("KaTeX_Main", state["fontFamily"], label + "KaTeX stylesheet applied")
        self.assertTrue(state["mathmlHidden"], label + "MathML copy hidden by KaTeX's stylesheet")
        self.assertAlmostEqual(base["fontSizeRatio"], state["fontSizeRatio"], places=3,
                               msg=label + "theme .katex size still wins (cascade order kept)")
        self.assertEqual(fallback_scripts, state["fallbackScripts"], label + "fallback scripts")
        self.assertEqual(fallback_css, state["fallbackCss"], label + "fallback stylesheet")
        self.assertIn("KaTeX_Main normal 400", state["faces"], label + "KaTeX font loaded")
        self.assertTrue(state["latoUpright"], label + "Lato 400 and 700 available")
        self.assertTrue(any(f.startswith("Lato ") for f in state["faces"]), label + "a local Lato face loaded")
        self.assertEqual([], state["googleRequests"], label + "no Google Fonts requests")

    def test_online_baseline_needs_no_fallback(self):
        for name, state in self.baseline.items():
            self.check(name, state, 0, 0)

    def test_fully_offline(self):
        for name in self.browsers:
            self.check(name, self.load(name, self.offline), 2, 1)

    def test_only_the_stylesheet_fails(self):
        for name in self.browsers:
            self.check(name, self.load(name, self.failing("katex.min.css")), 0, 1)

    def test_only_katex_js_fails(self):
        # auto-render loads but captured no katex, so the deck's own onload call
        # throws (one page error the engine cannot prevent); both files are
        # reloaded from the engine copy and the deck's call is run again.
        for name in self.browsers:
            self.check(name, self.load(name, self.failing("katex.min.js")), 2, 0, deck_errors=1)

    def test_only_auto_render_fails(self):
        for name in self.browsers:
            self.check(name, self.load(name, self.failing("auto-render.min.js")), 1, 0)


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class NoKatexDeckTests(unittest.TestCase):
    def test_deck_without_katex_is_left_alone(self):
        deck = REPO / "tests" / "fixtures" / "step-model" / "relative-deck.html"
        with sync_playwright() as pw, DeckServer(deck) as server:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.route(re.compile(r"^(?!http://127\.0\.0\.1[:/]).*"), lambda r: r.abort())
            page.goto(server.url)
            page.wait_for_function("document.documentElement.dataset.wsReady === '1'", timeout=30000)
            self.assertEqual(0, page.evaluate("document.querySelectorAll('[data-ws-katex-fallback]').length"))
            self.assertFalse(page.evaluate("!!window.katex"))
            browser.close()


if __name__ == "__main__":
    unittest.main()

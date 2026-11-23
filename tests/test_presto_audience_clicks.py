#!/usr/bin/env python3
"""Presto's audience-window click guard, run against the real engine in Chrome.

The guard is JavaScript that Presto.swift injects into the audience window. It
must keep plain slide clicks from reaching deck scripts while letting links and
buttons reach the engine, which keeps the appendix return stack (contract C3).

Presto loads the audience deck without `?mode=audience` and sets `WS.mode`
after the engine has started, so the engine's normal-mode click handlers are
bound. The clicks the guard lets through (link widgets and their labels) must
still never advance the slide.
"""
import json
import os
import re
import threading
import unittest
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the non-foreground runner provides it
    sync_playwright = None

PROJECT_DIR = Path(__file__).resolve().parents[1]
PRESTO_SWIFT = PROJECT_DIR / "presto" / "Presto.swift"
PRESTO_INJECTION_SWIFT = PROJECT_DIR / "presto" / "PrestoInjection.swift"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DECK_PATH = "/examples/kitchen-sink/index.html"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _audience_loaded_block():
    """The audience page script (AppDelegate.audienceCoreJS) and the selector."""
    source = PRESTO_SWIFT.read_text(encoding="utf-8")
    injection = PRESTO_INJECTION_SWIFT.read_text(encoding="utf-8")
    start = injection.index("func audienceCoreJS() -> String {")
    return source, injection[start:]


def audience_click_guard_js():
    source, block = _audience_loaded_block()
    selector = re.search(r'static let audienceClickThroughSelector =\s*"([^"]+)"', source).group(1)
    click_start = block.index("document.addEventListener('click', function(e) {")
    click_end = block.index("}, true);", click_start) + len("}, true);")
    js = block[click_start:click_end]
    return js.replace(r"\(jsStringLiteral(AppDelegate.audienceClickThroughSelector))", json.dumps(selector))


def audience_mode_js():
    """The statements Presto runs first after the audience page loads."""
    _, block = _audience_loaded_block()
    start = block.index('"""') + len('"""')
    return block[start:block.index("\\(", start)]


# Link widgets the guard lets through, added to a main slide with two or more
# steps. Each sits on top (absolute, high z-index) so real mouse clicks land on
# the element itself.
INSTALL_WIDGETS_JS = """() => {
    const source = WS.mainSlides.find(s => (WS.slideSteps.get(s) || 0) >= 2);
    const index = WS.slides.indexOf(source);
    const appendix = WS.appendixSlides[0].id;
    const wrap = document.createElement('div');
    wrap.innerHTML = `
        <div class="appendix-links" style="position:absolute;left:80px;top:300px;z-index:50">
            <span id="t-appendix-label">Appendix:</span>
            <a class="ws-appendix-link" href="#${appendix}">Details</a>
        </div>
        <div class="header-links" style="top:300px;bottom:auto;right:80px;z-index:50">
            <span id="t-header-label">See also:</span>
            <a class="ws-appendix-link" href="#${appendix}">More</a>
        </div>
        <div id="t-widget" data-ws-link
             style="position:absolute;left:520px;top:300px;z-index:50;padding:8px;border:1px solid #999">Deck widget</div>`;
    Array.from(wrap.children).forEach(el => source.appendChild(el));
    document.getElementById('t-widget').addEventListener('click', () => WS.goToSlide(index, 0));
    return index;
}"""

STATE_JS = "() => [WS.currentIndex, WS.currentStep]"


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class AudienceClickGuardTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(PROJECT_DIR)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}{DECK_PATH}"

    @contextmanager
    def audience_page(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROME, headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 720})
                # Like Presto: no ?mode=audience; the mode is set once the page has loaded.
                page.goto(self.url, wait_until="load")
                page.wait_for_function("document.documentElement.dataset.wsReady === '1'")
                page.evaluate(audience_mode_js())
                page.evaluate(audience_click_guard_js())
                yield page
            finally:
                browser.close()

    def test_presto_sets_audience_mode_after_load(self):
        self.assertIn("WS.mode = 'audience';", audience_mode_js())

    def test_appendix_links_reach_the_engine_and_plain_clicks_stay_swallowed(self):
        with self.audience_page() as page:
            self.assertEqual(page.evaluate("WS.mode"), "audience")
            result = page.evaluate(
                """() => {
                    window.__deckScriptSawClick = false;
                    document.addEventListener('click', () => { window.__deckScriptSawClick = true; });
                    const link = document.querySelector('.slide .ws-appendix-link');
                    const source = WS.slides.indexOf(link.closest('.slide'));
                    WS.goToSlide(source, 'end');
                    link.click();
                    const afterLink = {
                        depth: WS.returnStack.length,
                        inAppendix: WS.getState().isAppendix,
                    };
                    WS.returnFromAppendix();
                    window.__deckScriptSawClick = false;
                    const text = WS.slides[source].querySelector('h1, h2, p');
                    text.click();
                    return {
                        source,
                        afterLink,
                        backAt: WS.currentIndex,
                        plainClickReachedDeck: window.__deckScriptSawClick,
                    };
                }"""
            )

        self.assertEqual(result["afterLink"], {"depth": 1, "inAppendix": True}, result)
        self.assertEqual(result["backAt"], result["source"], result)
        self.assertFalse(result["plainClickReachedDeck"], result)

    def test_clicks_let_through_to_link_widgets_never_advance(self):
        with self.audience_page() as page:
            index = page.evaluate(INSTALL_WIDGETS_JS)
            cases = [
                # (name, start step, click target, expected step afterwards)
                ("label inside .appendix-links", 1, "#t-appendix-label", 1),
                ("label inside .header-links", 1, "#t-header-label", 1),
                ("[data-ws-link] widget whose handler goes to step 0", 2, "#t-widget", 0),
                ("plain slide title", 1, f".slide[data-ws-index='{index}'] h2", 1),
            ]
            observed, expected = {}, {}
            for name, start, target, step in cases:
                page.evaluate(f"WS.goToSlide({index}, {start})")
                page.click(target)
                observed[name] = page.evaluate(STATE_JS)
                expected[name] = [index, step]

        self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()

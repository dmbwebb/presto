"""Real mouse and keyboard input for the edit chrome and the redesigned visual
editor, in headless Chromium and WebKit (the system WebKit's engine family).

Covers the link card (click selects, Go to slide, the Back chip, ⌘-click), the
equation editor (double-click, typing, ⌘↩, Escape), the Text box action and
BACKLOG 9, the Escape order, the step strip, the rail, theme colours and the
"Appears at step" chip. Set PRESTO_WYSIWYG_JS / PRESTO_EDIT_CHROME_JS to test
other copies of the scripts.
"""
import os
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WYSIWYG_JS = Path(os.environ.get('PRESTO_WYSIWYG_JS', ROOT / 'presto/wysiwyg.js'))
CHROME_JS = Path(os.environ.get('PRESTO_EDIT_CHROME_JS', ROOT / 'presto/edit-chrome.js'))
LAYOUT_JS = ROOT / 'presto/edit-layout.js'

# KaTeX stand-in with the markup the serializer reads; throws on unbalanced
# braces when asked to (like KaTeX's ParseError). The CDN copy is not needed.
KATEX_STUB = r"""
window.katex = { render: function (tex, el, opts) {
    var depth = 0;
    for (var i = 0; i < tex.length; i++) { if (tex[i] === '{') depth++; if (tex[i] === '}') depth--; }
    if (opts && opts.throwOnError && depth !== 0) throw new Error("KaTeX parse error: Expected '}'");
    var esc = String(tex).replace(/&/g, '&amp;').replace(/</g, '&lt;');
    var inner = '<span class="katex"><span class="katex-mathml"><math><semantics><mrow></mrow>' +
        '<annotation encoding="application/x-tex">' + esc + '</annotation></semantics></math></span>' +
        '<span class="katex-html" aria-hidden="true">' + esc + '</span></span>';
    el.innerHTML = opts && opts.displayMode ? '<span class="katex-display">' + inner + '</span>' : inner;
} };
"""

DECK = """
<div class="ws-deck" data-section-nav>
  <section class="slide title"><h1>E2E deck</h1></section>
  <section class="slide section-header"><h1>Main</h1></section>
  <section class="slide content" id="work">
    <h2>Working slide</h2>
    <p id="para">A paragraph to colour</p>
    <ul><li id="b1" data-step="1">One</li><li id="b2" data-step="2">Two</li></ul>
    <div class="eqbox" style="background:#f3f3f3"><span class="katex-display" id="eq"><span class="katex"><span class="katex-mathml"><math><semantics><mrow></mrow><annotation encoding="application/x-tex">a + b</annotation></semantics></math></span><span class="katex-html" aria-hidden="true">a+b</span></span></span></div>
    <div class="header-links bl"><a class="ws-appendix-link" id="lnk" href="#apx">Details</a></div>
  </section>
  <section class="slide content"><h2>Next slide</h2><p>Body</p></section>
  <section class="slide appendix content" id="apx"><h2>Appendix: details</h2><p>More</p></section>
</div>
"""


class EditChromeRealInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browsers = [
            cls.playwright.chromium.launch(headless=True),
            cls.playwright.webkit.launch(headless=True),
        ]

    @classmethod
    def tearDownClass(cls):
        for browser in cls.browsers:
            browser.close()
        cls.playwright.stop()

    def open_page(self, browser):
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.set_default_timeout(4000)
        self.addCleanup(page.close)
        page.set_content('<html><head></head><body>' + DECK + '</body></html>')
        for css in ("engine/webslides.css", "engine/themes/academic.css", "presto/ui-tokens.css", "presto/edit-chrome.css"):
            page.add_style_tag(path=str(ROOT / css))
        page.add_script_tag(content=KATEX_STUB)
        page.add_script_tag(path=str(ROOT / "engine/webslides.js"))
        page.evaluate("() => WS.ready")
        if LAYOUT_JS.exists():
            page.add_script_tag(path=str(LAYOUT_JS))
        page.add_script_tag(path=str(CHROME_JS))
        page.add_script_tag(path=str(WYSIWYG_JS))
        page.evaluate("""() => {
            if (window.PrestoLayout) PrestoLayout.apply({mode: 'edit', insetLeft: 232, insetBottom: 72, padding: 28});
            else applyPrestoEditViewport({railWidth: 232});
            window.saved = [];
            window.chromeMsgs = [];
            PrestoEditChrome.onMessage = m => chromeMsgs.push(m);
            PrestoEditChrome.install({railVisible: true, ghostFuture: true});
            __prestoWysiwyg.onMessage = m => { if (m.type === 'wysiwygChanged') saved.push(m); };
            __prestoWysiwyg.enable();
            WS.goToSlide(2, 0);
        }""")
        page.wait_for_timeout(120)
        return page

    def center(self, page, selector):
        box = page.locator(selector).first.bounding_box()
        self.assertIsNotNone(box, selector)
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def click(self, page, selector, **kw):
        x, y = self.center(page, selector)
        page.mouse.click(x, y, **kw)
        page.wait_for_timeout(60)

    def state(self, page):
        return page.evaluate("__prestoWysiwyg.state()")

    def test_link_card_go_and_back(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser)
                page.evaluate("WS.goToSlide(2, 1)")
                self.click(page, "#lnk")
                self.assertEqual(self.state(page)["selectionKind"], "link")
                self.assertTrue(page.locator("#presto-link-card").is_visible())
                self.assertFalse(page.locator("#presto-wysiwyg-toolbar").is_visible())
                self.assertIn("Appendix: details", page.locator("#presto-link-card .pec-lc-t b").inner_text())
                self.click(page, "#presto-link-card .pec-go")
                self.assertEqual(page.evaluate("WS.currentIndex"), 4)
                chip = page.locator("#presto-back-chip")
                self.assertTrue(chip.is_visible())
                self.assertEqual(chip.inner_text().strip(), "Back to 3 · Working slide")
                self.click(page, "#presto-back-chip")
                self.assertEqual(page.evaluate("[WS.currentIndex, WS.currentStep]"), [2, 1])
                self.assertFalse(chip.is_visible())

    def test_command_click_goes_straight_there(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser)
                x, y = self.center(page, "#lnk")
                page.keyboard.down("Meta")
                page.mouse.click(x, y)
                page.keyboard.up("Meta")
                page.wait_for_timeout(80)
                self.assertEqual(page.evaluate("WS.currentIndex"), 4)
                self.assertFalse(self.state(page)["hasSelection"])
                self.assertEqual(page.evaluate("WS.returnStack.length"), 0)
                self.assertTrue(page.locator("#presto-back-chip").is_visible())

    def test_equation_double_click_edit_and_cancel(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser)
                x, y = self.center(page, "#eq")
                page.mouse.dblclick(x, y)
                page.wait_for_timeout(80)
                textarea = page.locator("#presto-wysiwyg-equation textarea")
                self.assertTrue(textarea.is_visible())
                self.assertEqual(textarea.input_value(), "a + b")
                page.keyboard.press("Meta+A")
                page.keyboard.type("\\frac{x")
                self.assertTrue(page.locator("#presto-wysiwyg-equation .pw-eq-apply").is_disabled())
                self.assertIn("Expected", page.locator("#presto-wysiwyg-equation .pw-eq-error").inner_text())
                page.keyboard.type("}{2}")
                page.keyboard.press("Meta+Enter")
                page.wait_for_timeout(60)
                self.assertEqual(page.locator("#presto-wysiwyg-equation").count(), 0)
                self.assertIn("$$\\frac{x}{2}$$", page.evaluate("saved.at(-1).html"))
                # Escape cancels a second edit without saving.
                count = page.evaluate("saved.length")
                self.click(page, "#work .katex-display")
                page.evaluate("PrestoEdit.editEquation()")
                page.keyboard.type(" + 1")
                page.keyboard.press("Escape")
                self.assertEqual(page.locator("#presto-wysiwyg-equation").count(), 0)
                self.assertEqual(page.evaluate("saved.length"), count)
                self.assertTrue(self.state(page)["hasSelection"])
                page.keyboard.press("Escape")
                self.assertFalse(self.state(page)["hasSelection"])

    def test_text_box_and_accidental_double_click(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser)
                page.evaluate("PrestoEdit.addTextBox()")
                page.keyboard.type("Fresh note")
                page.keyboard.press("Escape")
                self.assertIn("Fresh note", page.evaluate("saved.at(-1).html"))
                self.assertEqual(page.evaluate("PrestoEdit.historyState().undoLabel"), "Undo Add Text Box")
                page.keyboard.press("Escape")
                before = page.evaluate("[saved.length, document.querySelectorAll('#work .text-box').length]")
                slide = page.locator("#work").bounding_box()
                page.mouse.dblclick(slide["x"] + slide["width"] * 0.85, slide["y"] + slide["height"] * 0.55)
                page.wait_for_timeout(60)
                self.assertTrue(self.state(page)["textEditing"])
                page.keyboard.press("Escape")
                after = page.evaluate("[saved.length, document.querySelectorAll('#work .text-box').length]")
                self.assertEqual(after, before)

    def test_escape_order_with_real_keys(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser)
                self.click(page, "#para")
                self.click(page, "#presto-wysiwyg-toolbar .pw-more")
                self.assertEqual(self.state(page)["popover"], "more")
                page.keyboard.press("Escape")
                self.assertIsNone(self.state(page)["popover"])
                self.assertTrue(self.state(page)["hasSelection"])
                page.keyboard.press("Enter")
                self.assertTrue(self.state(page)["textEditing"])
                page.keyboard.press("Escape")
                self.assertFalse(self.state(page)["textEditing"])
                self.assertTrue(self.state(page)["hasSelection"])
                page.keyboard.press("Escape")
                self.assertFalse(self.state(page)["hasSelection"])
                page.evaluate("WS.toggleOverview()")
                page.keyboard.press("Escape")
                self.assertFalse(page.evaluate("document.body.classList.contains('ws-overview-active')"))

    def test_step_strip_rail_and_toggle(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser)
                self.assertEqual(page.locator("#presto-step-strip .pec-step").all_inner_texts(), ["0", "1", "2"])
                self.click(page, "#presto-step-strip .pec-step >> nth=2")
                self.assertEqual(page.evaluate("WS.currentStep"), 2)
                self.click(page, ".pec-th[data-index='3'] .pec-pic")
                self.assertEqual(page.evaluate("WS.currentIndex"), 3)
                self.click(page, ".pec-th[data-index='2'] .pec-pic")
                self.click(page, "#presto-step-strip .pec-ghost .pec-switch")
                self.assertEqual(self.state(page)["hiddenStepOpacity"], 0)
                self.assertTrue(page.evaluate("chromeMsgs.some(m => m.type === 'ghostFutureChanged' && m.on === false)"))

    def test_theme_colour_and_appears_chip(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser)
                self.click(page, "#para")
                self.click(page, "#presto-wysiwyg-toolbar .pw-color")
                self.click(page, "#presto-wysiwyg-colors .pw-swatch[title='Heading']")
                self.assertEqual(page.evaluate("document.getElementById('para').style.color"), "var(--color-heading)")
                self.click(page, "#presto-wysiwyg-toolbar .pw-appears")
                self.click(page, "#presto-wysiwyg-steps .pw-item >> text=At a new step (3)")
                self.assertEqual(page.evaluate("document.getElementById('para').dataset.step"), "3")
                self.assertEqual(page.locator("#presto-wysiwyg-toolbar .pw-chip").inner_text(), "step 3")
                self.assertEqual(page.locator("#presto-step-strip .pec-step").count(), 4)
                page.evaluate("PrestoEdit.undo()")
                self.assertFalse(page.evaluate("document.getElementById('para').hasAttribute('data-step')"))


if __name__ == '__main__':
    unittest.main()

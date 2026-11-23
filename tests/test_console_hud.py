#!/usr/bin/env python3
"""One-screen show controls (presto/present-hud.{js,css}) in Playwright WebKit.

The HUD is injected into real decks, so besides the bar's own behaviour
(show on pointer movement, hide after 2.5 s unless hovered, messages, corner
timer, hint toast) these tests pin its isolation: no layout change, slide
links and engine keys still work, HUD clicks never reach the deck, and deck
CSS cannot restyle it.
"""
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the local Python
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
HUD_JS = ROOT / "presto" / "present-hud.js"
HUD_CSS = ROOT / "presto" / "present-hud.css"
TOKENS_CSS = ROOT / "presto" / "ui-tokens.css"

DECK = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>HUD fixture</title>
<link rel="stylesheet" href="/repo/engine/webslides.css">
<style>
  /* Hostile deck rules that must not reach the HUD. */
  button { background: rgb(255, 0, 0); padding: 40px; font-size: 30px; border: 4px solid lime; }
  div { border: 5px solid rgb(0, 128, 0); }
  span { letter-spacing: 9px; }
  svg { width: 200px; height: 200px; }
  b { color: rgb(0, 0, 255); }
  rect { fill: rgb(255, 0, 0); stroke-width: 9px; }
  path { stroke: rgb(255, 0, 0); }
</style>
<script defer src="/repo/engine/webslides.js"></script></head>
<body><div class="ws-deck">
<section class="slide content" id="first"><h2>First</h2><p><a id="jump" href="#app-a">To the appendix</a></p></section>
<section class="slide content"><h2>Second</h2></section>
<section class="slide content"><h2>Third</h2></section>
<section class="slide appendix content" id="app-a"><h2>Appendix A</h2></section>
</div></body></html>
"""


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class HUDTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        deck_dir = Path(cls.tmp.name)
        (deck_dir / "index.html").write_text(DECK, encoding="utf-8")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                path = path.split("?", 1)[0].split("#", 1)[0]
                if path.startswith("/repo/"):
                    return str(ROOT / path[len("/repo/"):])
                return str(deck_dir / path.lstrip("/"))

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.webkit.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def open(self, one_screen=True, css=True, tokens=True, install=True):
        page = self.browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_default_timeout(6000)
        self.addCleanup(page.close)
        page.add_init_script("""
            window.__msgs = [];
            window.__deckClicks = 0;
            window.webkit = { messageHandlers: { presto: { postMessage(m) { window.__msgs.push(m); } } } };
            document.addEventListener('click', () => { window.__deckClicks += 1; });
        """)
        page.goto(self.base + "/index.html")
        page.evaluate("() => WS.ready")
        # Inject the way Presto does: tokens, then the HUD stylesheet, then the script.
        if tokens:
            page.add_style_tag(content=TOKENS_CSS.read_text())
        if css:
            page.add_style_tag(content=HUD_CSS.read_text())
        page.add_script_tag(content=HUD_JS.read_text())
        if install:
            page.evaluate("o => PrestoHUD.install(o)", {"oneScreen": one_screen})
            page.evaluate("PrestoHUD.setState({slideNumber: 20, total: 38, isAppendix: false, elapsedMs: 832000, timerRunning: false})")
        return page

    @staticmethod
    def bar_shown(page):
        return page.evaluate("""() => {
            const bar = document.getElementById('presto-hud-bar');
            if (!bar) return null;
            const cs = getComputedStyle(bar);
            return bar.classList.contains('presto-hud-visible') && cs.visibility === 'visible';
        }""")

    @staticmethod
    def messages(page):
        return page.evaluate("window.__msgs")

    def test_bar_appears_on_pointer_movement_and_hides_after_two_and_a_half_seconds(self):
        page = self.open()
        self.assertFalse(self.bar_shown(page))
        page.mouse.move(200, 200)
        page.mouse.move(260, 240)
        self.assertTrue(self.bar_shown(page))
        page.wait_for_timeout(1800)
        self.assertTrue(self.bar_shown(page))
        page.wait_for_timeout(1200)
        self.assertFalse(self.bar_shown(page))
        self.assertEqual(page.eval_on_selector("#presto-hud-bar", "b => getComputedStyle(b).pointerEvents"), "none")

    def test_bar_stays_while_hovered_and_hides_after_leaving(self):
        page = self.open()
        page.mouse.move(100, 100)
        page.mouse.move(120, 120)
        box = page.eval_on_selector("#presto-hud-bar", "b => { const r = b.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }")
        page.mouse.move(box["x"], box["y"], steps=4)
        page.wait_for_timeout(3200)
        self.assertTrue(self.bar_shown(page))
        page.mouse.move(400, 200, steps=4)
        page.wait_for_timeout(2900)
        self.assertFalse(self.bar_shown(page))

    def test_mousemove_without_real_movement_does_not_show_the_bar(self):
        page = self.open()
        page.evaluate("""() => window.addEventListener('mousemove', e => {
            if (e.isTrusted) window.__lastPointer = {screenX: e.screenX, screenY: e.screenY, clientX: e.clientX, clientY: e.clientY};
        }, true)""")
        page.mouse.move(300, 300)
        page.mouse.move(310, 310)
        page.wait_for_timeout(2800)
        self.assertFalse(self.bar_shown(page))
        # WebKit re-sends mousemove at the same spot when slide content changes.
        page.evaluate("() => WS.nextStep()")
        page.evaluate("""() => {
            const p = Object.assign({bubbles: true}, window.__lastPointer);
            document.body.dispatchEvent(new MouseEvent('mousemove', p));
            p.screenX += 1;
            document.body.dispatchEvent(new MouseEvent('mousemove', p));
        }""")
        self.assertFalse(self.bar_shown(page))
        page.mouse.move(340, 330)
        self.assertTrue(self.bar_shown(page))

    def test_buttons_post_messages_and_never_reach_the_deck(self):
        page = self.open()
        page.mouse.move(100, 100)
        page.mouse.move(140, 140)
        before = page.evaluate("WS.currentIndex")
        for action in ("prev", "next", "grid", "edit", "end"):
            page.click("#presto-hud-bar .presto-hud-%s" % action)
        self.assertEqual([m["type"] + ":" + m.get("direction", "") for m in self.messages(page)],
                         ["hudNavigate:prev", "hudNavigate:next", "hudOverview:", "hudEditHere:", "hudEnd:"])
        self.assertEqual(page.evaluate("WS.currentIndex"), before)
        self.assertEqual(page.evaluate("window.__deckClicks"), 0)
        self.assertNotEqual(page.evaluate("document.activeElement && document.activeElement.tagName"), "BUTTON")

    def test_counter_time_appendix_and_corner_timer(self):
        page = self.open()
        page.mouse.move(100, 100)
        page.mouse.move(140, 140)
        self.assertEqual(page.inner_text("#presto-hud-bar .presto-hud-count").split(), ["20", "/", "38"])
        self.assertEqual(page.inner_text("#presto-hud-bar .presto-hud-time"), "13:52")
        page.evaluate("PrestoHUD.setState({slideNumber: 'A3', isAppendix: true})")
        self.assertEqual(page.inner_text("#presto-hud-bar .presto-hud-count").strip(), "A3")

        self.assertFalse(page.is_visible("#presto-hud-timer"))
        page.evaluate("PrestoHUD.setState({timerVisible: true})")
        self.assertTrue(page.is_visible("#presto-hud-timer"))
        self.assertEqual(page.inner_text("#presto-hud-timer"), "13:52")
        page.evaluate("PrestoHUD.setState({elapsedMs: 5000, timerRunning: true})")
        page.wait_for_function("document.getElementById('presto-hud-timer').textContent === '00:06'", timeout=3000)
        page.evaluate("PrestoHUD.setState({timerRunning: false})")
        frozen = page.inner_text("#presto-hud-timer")
        page.wait_for_timeout(1300)
        self.assertEqual(page.inner_text("#presto-hud-timer"), frozen)
        page.evaluate("PrestoHUD.setState({timerVisible: false})")
        page.wait_for_function("getComputedStyle(document.getElementById('presto-hud-timer')).visibility === 'hidden'",
                               timeout=2000)

    def test_hud_binds_no_keys(self):
        page = self.open()
        page.evaluate("PrestoHUD.setState({timerVisible: false})")
        page.keyboard.press("t")
        page.keyboard.press("T")
        self.assertFalse(page.is_visible("#presto-hud-timer"))
        before = page.evaluate("WS.currentIndex")
        page.keyboard.press("ArrowRight")
        self.assertEqual(page.evaluate("WS.currentIndex"), before + 1)
        self.assertEqual(self.messages(page), [])

    def test_external_display_audience_gets_no_bar_and_no_timer(self):
        page = self.open(one_screen=False)
        page.mouse.move(100, 100)
        page.mouse.move(200, 200)
        page.evaluate("PrestoHUD.setState({timerVisible: true})")
        self.assertIsNone(self.bar_shown(page))
        self.assertEqual(page.evaluate("document.getElementById('presto-hud-timer')"), None)
        page.evaluate("PrestoHUD.hint('Press Escape again to end the presentation', 2000)")
        self.assertTrue(page.is_visible("#presto-hud-hint"))
        # Switching an installed one-screen HUD to an external audience removes the bar.
        page2 = self.open(one_screen=True)
        page2.evaluate("PrestoHUD.install({oneScreen: false})")
        self.assertIsNone(self.bar_shown(page2))
        self.assertEqual(page2.evaluate("document.querySelectorAll('#presto-hud-root').length"), 1)

    def test_install_is_idempotent(self):
        page = self.open()
        page.evaluate("PrestoHUD.install({oneScreen: true}); PrestoHUD.install({oneScreen: true});")
        counts = page.evaluate("""() => ({
            roots: document.querySelectorAll('#presto-hud-root').length,
            bars: document.querySelectorAll('#presto-hud-bar').length,
            hints: document.querySelectorAll('#presto-hud-hint').length
        })""")
        self.assertEqual(counts, {"roots": 1, "bars": 1, "hints": 1})
        page.add_script_tag(content=HUD_JS.read_text())
        self.assertEqual(page.evaluate("document.querySelectorAll('#presto-hud-bar').length"), 1)

    def test_hint_toast_is_light_and_times_out(self):
        page = self.open()
        page.evaluate("PrestoHUD.hint('Press Escape again to end the presentation', 500)")
        self.assertTrue(page.is_visible("#presto-hud-hint"))
        self.assertEqual(page.inner_text("#presto-hud-hint"), "Press Escape again to end the presentation")
        bg = page.eval_on_selector("#presto-hud-hint", "h => getComputedStyle(h).backgroundColor")
        self.assertTrue(bg.startswith("rgba(255, 255, 255"), bg)
        page.wait_for_function("!document.getElementById('presto-hud-hint').classList.contains('presto-hud-visible')", timeout=3000)

    def test_hint_sits_at_the_bottom_centre_above_the_control_bar(self):
        # At the top it covered the deck's section bar and the slide title.
        page = self.open()
        page.mouse.move(200, 200)
        page.mouse.move(260, 240)
        self.assertTrue(self.bar_shown(page))
        page.evaluate("PrestoHUD.hint('Press Escape again to end the presentation', 2000)")
        page.wait_for_timeout(250)
        geo = page.evaluate("""() => {
            const h = document.getElementById('presto-hud-hint').getBoundingClientRect();
            const b = document.getElementById('presto-hud-bar').getBoundingClientRect();
            return {top: h.top, bottom: h.bottom, centre: h.left + h.width / 2, barTop: b.top,
                    barCentre: b.left + b.width / 2, vh: innerHeight, vw: innerWidth};
        }""")
        self.assertAlmostEqual(geo["centre"], geo["vw"] / 2, delta=1)
        self.assertAlmostEqual(geo["bottom"], geo["vh"] - 80, delta=2)
        self.assertLessEqual(geo["bottom"], geo["barTop"] - 6, "the hint sits just above the bar, not on it")
        self.assertGreater(geo["top"], geo["vh"] * 0.75, "the hint stays clear of the section bar and title")
        # Without the stylesheet the inline fallback keeps the same place.
        bare = self.open(css=False)
        bare.evaluate("PrestoHUD.hint('Press Escape again to end the presentation', 2000)")
        bottom = bare.eval_on_selector("#presto-hud-hint", "h => innerHeight - h.getBoundingClientRect().bottom")
        self.assertAlmostEqual(bottom, 80, delta=2)

    def test_hint_and_bar_sit_above_the_black_screen(self):
        page = self.open()
        # Presto's black-out cover (AppDelegate.blackoutJS), made hit-testable here.
        page.evaluate("""() => {
            const cover = document.createElement('div');
            cover.id = 'presto-blackout';
            cover.style.cssText = 'position:fixed;inset:0;background:#000;z-index:2147483646;pointer-events:none';
            document.body.appendChild(cover);
            cover.style.pointerEvents = 'auto';
        }""")
        page.evaluate("PrestoHUD.hint('Press Escape again to end the presentation', 2000)")
        on_top = page.evaluate("""() => {
            const hint = document.getElementById('presto-hud-hint');
            hint.style.pointerEvents = 'auto';
            const r = hint.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            return !!hit && hint.contains(hit);
        }""")
        self.assertTrue(on_top, "the Escape hint must show over the black screen")
        self.assertFalse(self.bar_shown(page))
        page.mouse.move(200, 200)
        page.mouse.move(260, 240)
        self.assertTrue(self.bar_shown(page))
        bar_on_top = page.evaluate("""() => {
            const bar = document.getElementById('presto-hud-bar');
            const r = bar.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            return !!hit && bar.contains(hit);
        }""")
        self.assertTrue(bar_on_top, "a pointer move shows the bar over the black screen")

    def test_hud_does_not_change_slide_layout_or_body(self):
        page = self.open(install=False)
        before = page.evaluate("""() => ({
            slide: JSON.stringify(document.querySelector('.slide.ws-active').getBoundingClientRect()),
            bodyChildren: document.body.children.length,
            scroll: [document.documentElement.scrollWidth, document.documentElement.scrollHeight]
        })""")
        page.evaluate("PrestoHUD.install({oneScreen: true}); PrestoHUD.hint('x', 3000); PrestoHUD.setState({timerVisible: true});")
        page.mouse.move(10, 10)
        page.mouse.move(50, 50)
        after = page.evaluate("""() => ({
            slide: JSON.stringify(document.querySelector('.slide.ws-active').getBoundingClientRect()),
            bodyChildren: document.body.children.length,
            scroll: [document.documentElement.scrollWidth, document.documentElement.scrollHeight]
        })""")
        self.assertEqual(before, after)
        self.assertEqual(page.evaluate("document.getElementById('presto-hud-root').parentNode === document.documentElement"), True)
        ids = page.evaluate("""[...document.querySelectorAll('#presto-hud-root, #presto-hud-root *')]
            .flatMap(e => [e.id, e.getAttribute('class')]).filter(Boolean)""")
        self.assertIn("presto-hud-bar", ids)
        for name in ids:
            for part in str(name).split():
                self.assertTrue(part.startswith("presto-hud-"), part)

    def test_slide_links_still_work_with_the_bar_shown(self):
        page = self.open()
        page.mouse.move(100, 100)
        page.mouse.move(140, 140)
        self.assertTrue(self.bar_shown(page))
        page.click("#jump")
        page.wait_for_function("WS.currentIndex === 3")

    def test_deck_css_cannot_restyle_the_hud(self):
        page = self.open()
        page.mouse.move(100, 100)
        page.mouse.move(140, 140)
        styles = page.evaluate("""() => {
            const btn = document.querySelector('#presto-hud-bar .presto-hud-next');
            const svg = btn.querySelector('svg');
            const bar = document.getElementById('presto-hud-bar');
            const num = document.querySelector('#presto-hud-bar .presto-hud-count b');
            const cs = getComputedStyle(btn);
            const rect = document.querySelector('#presto-hud-bar .presto-hud-grid rect');
            const path = document.querySelector('#presto-hud-bar .presto-hud-next path');
            return {rectW: rect.getBoundingClientRect().width, rectFill: getComputedStyle(rect).fill,
                    rectStroke: getComputedStyle(rect).strokeWidth, pathStroke: getComputedStyle(path).stroke,
                    bg: cs.backgroundColor, padLeft: cs.paddingLeft, fontSize: cs.fontSize, border: cs.borderTopWidth,
                    svgW: svg.getBoundingClientRect().width, barBorder: getComputedStyle(bar).borderTopWidth,
                    barH: bar.getBoundingClientRect().height, numColor: getComputedStyle(num).color,
                    endColor: getComputedStyle(document.querySelector('.presto-hud-end')).color,
                    spacing: getComputedStyle(document.querySelector('.presto-hud-time')).letterSpacing};
        }""")
        self.assertGreater(styles["rectW"], 3)
        self.assertEqual(styles["rectFill"], "none")
        self.assertIn(styles["rectStroke"], ("1.8px", "1.8"))
        self.assertEqual(styles["pathStroke"], "rgb(28, 37, 50)")
        self.assertEqual(styles["bg"], "rgba(0, 0, 0, 0)")
        self.assertEqual(styles["padLeft"], "8px")
        self.assertEqual(styles["fontSize"], "13px")
        self.assertEqual(styles["border"], "0px")
        self.assertEqual(styles["svgW"], 16)
        self.assertEqual(styles["barBorder"], "0px")
        self.assertLess(styles["barH"], 50)
        self.assertEqual(styles["numColor"], "rgb(28, 37, 50)")
        self.assertEqual(styles["endColor"], "rgb(212, 70, 44)")
        self.assertIn(styles["spacing"], ("normal", "0px"))

    def test_literal_fallbacks_without_tokens(self):
        page = self.open(tokens=False)
        page.mouse.move(100, 100)
        page.mouse.move(140, 140)
        self.assertEqual(page.eval_on_selector(".presto-hud-end", "e => getComputedStyle(e).color"), "rgb(212, 70, 44)")
        self.assertTrue(self.bar_shown(page))

    def test_missing_stylesheet_hides_the_bar_but_keeps_the_escape_hint(self):
        page = self.open(css=False)
        page.mouse.move(100, 100)
        page.mouse.move(140, 140)
        self.assertEqual(page.eval_on_selector("#presto-hud-bar", "b => getComputedStyle(b).display"), "none")
        page.evaluate("PrestoHUD.hint('Press Escape again to end the presentation', 2000)")
        self.assertTrue(page.is_visible("#presto-hud-hint"))
        self.assertEqual(page.eval_on_selector("#presto-hud-hint", "h => getComputedStyle(h).position"), "fixed")


if __name__ == "__main__":
    unittest.main()

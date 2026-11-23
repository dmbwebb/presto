#!/usr/bin/env python3
"""Presenter console (presto/presenter.{html,js,css}).

Pace maths and the pure helpers run in Node against presenter.js; the page
itself runs in Playwright WebKit against a small generated deck: ready and
live phases, every console* message, the timer controls, talk length, the
Sections card, the appendix, the hint toast, blackout and the stage layout.
"""
import functools
import http.server
import json
import subprocess
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the local Python
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
PRESENTER_JS = ROOT / "presto" / "presenter.js"
PRESENTER_HTML = ROOT / "presto" / "presenter.html"

NODE_HARNESS = """
const fs = require('fs');
const vm = require('vm');
const element = () => ({
  textContent: '', title: '', value: '', style: {}, hidden: false, disabled: false,
  classList: { add() {}, remove() {} },
  addEventListener() {}, querySelector() { return null; },
  clientWidth: 1280, clientHeight: 720
});
const elements = new Map();
const context = {
  window: {},
  document: {
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    querySelectorAll() { return []; },
    addEventListener() {},
    body: { style: {} }
  },
  MessageChannel: class { constructor() { this.port1 = {}; this.port2 = { postMessage() {} }; } },
  setTimeout() { return 1; }, clearTimeout() {},
  Date, URLSearchParams, console
};
context.window.addEventListener = () => {};
vm.createContext(context);
vm.runInContext(fs.readFileSync(%s, 'utf8'), context);
const I = context.window.PrestoConsole._internals;
const MIN = 60000;
""" % json.dumps(str(PRESENTER_JS))


def run_node(body):
    proc = subprocess.run(["node", "-e", NODE_HARNESS + textwrap.dedent(body)],
                          text=True, capture_output=True, check=True)
    return json.loads(proc.stdout)


class PaceMathTests(unittest.TestCase):
    def pace(self, **kw):
        return run_node("console.log(JSON.stringify(I.computePace(%s)));" % json.dumps(kw))

    def progress(self, number, step, max_step, total):
        return run_node("console.log(JSON.stringify(I.slideProgress(%d, %d, %d, %d)));"
                        % (number, step, max_step, total))

    def test_slide_progress_counts_steps_as_fractions_of_their_slide(self):
        # Slide 3 of 10 with steps 0..3: four equal shares of the third tenth.
        p = self.progress(3, 2, 3, 10)
        self.assertAlmostEqual(p["start"], 0.25)
        self.assertAlmostEqual(p["end"], 0.275)
        first = self.progress(1, 0, 0, 10)
        self.assertEqual((first["start"], first["end"]), (0, 0.1))
        last = self.progress(10, 0, 0, 10)
        self.assertAlmostEqual(last["end"], 1.0)

    def test_slide_progress_clamps_impossible_positions(self):
        self.assertEqual(self.progress(40, 9, 2, 10), self.progress(10, 2, 2, 10))
        self.assertIsNone(run_node("console.log(JSON.stringify(I.slideProgress(0, 0, 0, 10)));"))
        self.assertIsNone(run_node("console.log(JSON.stringify(I.slideProgress(3, 0, 0, null)));"))

    def test_no_pace_without_a_talk_length(self):
        for minutes in (None, 0, -5, 601, 4.5, "45"):
            with self.subTest(minutes=minutes):
                p = self.pace(elapsedMs=5 * 60000, talkMinutes=minutes,
                              progress={"start": 0.5, "end": 0.6}, timerStarted=True, nowMs=0)
                self.assertFalse(p["hasTalk"])
                self.assertEqual(p["tone"], "none")
                self.assertEqual(p["label"], "")

    def test_ahead_when_the_share_was_reached_before_it_was_due(self):
        # 10 slides, 10 minutes: slide 6 is due from 5:00; arriving at 2:00 is 3 min ahead.
        p = self.pace(elapsedMs=2 * 60000, talkMinutes=10,
                      progress={"start": 0.5, "end": 0.6}, timerStarted=True, nowMs=0)
        self.assertEqual((p["tone"], p["minutes"], p["label"]), ("ahead", 3, "3 min ahead of pace"))
        self.assertAlmostEqual(p["position"], 0.5)
        self.assertAlmostEqual(p["timeFraction"], 0.2)

    def test_behind_only_after_the_share_should_have_ended(self):
        p = self.pace(elapsedMs=8 * 60000, talkMinutes=10,
                      progress={"start": 0.5, "end": 0.6}, timerStarted=True, nowMs=0)
        self.assertEqual((p["tone"], p["minutes"], p["label"]), ("behind", -2, "2 min behind pace"))

    def test_on_pace_inside_the_share_and_within_thirty_seconds_of_it(self):
        for elapsed_s in (300, 330, 360, 280, 385):
            with self.subTest(elapsed_s=elapsed_s):
                p = self.pace(elapsedMs=elapsed_s * 1000, talkMinutes=10,
                              progress={"start": 0.5, "end": 0.6}, timerStarted=True, nowMs=0)
                self.assertEqual((p["tone"], p["label"]), ("on", "On pace"))
        p = self.pace(elapsedMs=265 * 1000, talkMinutes=10,
                      progress={"start": 0.5, "end": 0.6}, timerStarted=True, nowMs=0)
        self.assertEqual((p["tone"], p["label"]), ("ahead", "1 min ahead of pace"))

    def test_start_of_talk_is_on_pace(self):
        p = self.pace(elapsedMs=10000, talkMinutes=45,
                      progress={"start": 0, "end": 1 / 36}, timerStarted=True, nowMs=0)
        self.assertEqual(p["tone"], "on")

    def test_no_pace_before_the_timer_runs_or_without_a_main_position(self):
        p = self.pace(elapsedMs=0, talkMinutes=45, progress={"start": 0.5, "end": 0.6},
                      timerStarted=False, nowMs=0)
        self.assertEqual(p["tone"], "none")
        self.assertAlmostEqual(p["position"], 0.5)
        p = self.pace(elapsedMs=60000, talkMinutes=45, progress=None, timerStarted=True, nowMs=0)
        self.assertEqual((p["tone"], p["position"]), ("none", None))
        self.assertTrue(p["hasTalk"])

    def test_ends_at_and_overtime(self):
        p = self.pace(elapsedMs=10 * 60000, talkMinutes=45, progress=None, timerStarted=True,
                      nowMs=1_000_000)
        self.assertEqual(p["endsAtMs"], 1_000_000 + 35 * 60000)
        self.assertEqual(p["overtimeMs"], 0)
        over = self.pace(elapsedMs=50 * 60000, talkMinutes=45, progress=None, timerStarted=True, nowMs=0)
        self.assertEqual(over["overtimeMs"], 5 * 60000)
        self.assertEqual(over["timeFraction"], 1)

    def test_elapsed_follows_the_swift_timer_model(self):
        result = run_node("""
            const t = I.timerElapsed({running: true, elapsedMs: 5000, startedAtMs: 100000}, 160000);
            const paused = I.timerElapsed({running: false, elapsedMs: 5000, startedAtMs: null}, 999999);
            const clockSkew = I.timerElapsed({running: true, elapsedMs: 0, startedAtMs: 200}, 100);
            console.log(JSON.stringify([t, paused, clockSkew]));
        """)
        self.assertEqual(result, [65000, 5000, 0])

    def test_duration_and_clock_formats(self):
        result = run_node("""
            console.log(JSON.stringify([0, 59999, 768000, 45 * MIN, 3600000, 5025000, -5, NaN]
                .map(I.formatDuration).concat([I.formatClockTime(new Date(2026, 10, 3, 15, 30).getTime()),
                                               I.formatClockTime(new Date(2026, 10, 3, 9, 5).getTime())])));
        """)
        self.assertEqual(result, ["00:00", "00:59", "12:48", "45:00", "1:00:00", "1:23:45", "00:00",
                                  "00:00", "15:30", "09:05"])

    def test_talk_length_field_parsing(self):
        result = run_node("""
            console.log(JSON.stringify(['', '  ', ' 45 ', '1', '600', '0', '601', 'abc', '4.5', '1000']
                .map(I.parseTalkInput)));
        """)
        self.assertEqual([r["ok"] for r in result], [True, True, True, True, True, False, False, False, False, False])
        self.assertEqual([r["minutes"] for r in result[:5]], [None, None, 45, 1, 600])


class ConsoleModelTests(unittest.TestCase):
    SECTIONS = [{"title": "Intro", "first": 0, "last": 5},
                {"title": "Data", "first": 6, "last": 8},
                {"title": "Average fadeout", "first": 9, "last": 23}]

    def rows(self, **state):
        return run_node("""
            const st = I.normalizeState(%s, 0);
            console.log(JSON.stringify(I.buildSectionRows(st, %s)));
        """ % (json.dumps(state), json.dumps(state.pop("_anchor", None))))

    def test_sections_mark_done_current_and_upcoming_with_ranges(self):
        rows = self.rows(sections=self.SECTIONS, slideIndex=17, mainSlideCount=24, totalSlides=31, _anchor=17)
        self.assertEqual([(r["title"], r["status"], r["range"], r["index"]) for r in rows], [
            ("Intro", "done", "1\u20136", 0),
            ("Data", "done", "7\u20139", 6),
            ("Average fadeout", "now", "9 of 15", None),
            ("Appendix", "next", "A1\u2013A7", 24),
        ])

    def test_appendix_row_is_current_while_in_the_appendix_and_main_rows_keep_the_last_place(self):
        rows = self.rows(sections=self.SECTIONS, slideIndex=26, mainSlideCount=24, totalSlides=31,
                         isAppendix=True, _anchor=17)
        self.assertEqual([r["status"] for r in rows], ["done", "done", "next", "now"])
        self.assertEqual(rows[-1]["range"], "3 of 7")
        self.assertIsNone(rows[-1]["index"])

    def test_single_slide_sections_and_no_appendix(self):
        rows = self.rows(sections=[{"title": "Only", "first": 0, "last": 0},
                                   {"title": "Rest", "first": 1, "last": 4}],
                         slideIndex=0, mainSlideCount=5, totalSlides=5, _anchor=0)
        self.assertEqual([(r["status"], r["range"]) for r in rows], [("now", "1"), ("next", "2\u20135")])

    def test_normalize_state_drops_malformed_fields(self):
        st = run_node("""
            console.log(JSON.stringify(I.normalizeState({
              phase: 'bogus', slideIndex: -1, step: 'x', maxStep: 2.5, mainSlideCount: 0,
              talkMinutes: 9999, isAppendix: 'yes', blackout: 1,
              sections: [{title: 'B', first: 5, last: 7}, {title: 'A', first: 0, last: 4}, {first: 3, last: 1}, null],
              displays: [{id: '724', name: 'LG UltraFine'}, {id: null, name: 'x'}, {id: 3}],
              audienceDisplayId: '724',
              timer: {running: true, elapsedMs: -4}
            }, 5000)));
        """)
        self.assertEqual(st["phase"], "ready")
        self.assertEqual((st["slideIndex"], st["step"], st["maxStep"]), (0, 0, 0))
        self.assertIsNone(st["mainSlideCount"])
        self.assertIsNone(st["talkMinutes"])
        self.assertFalse(st["isAppendix"])
        self.assertFalse(st["blackout"])
        self.assertEqual([s["title"] for s in st["sections"]], ["A", "B"])
        self.assertEqual(st["displays"], [{"id": "724", "name": "LG UltraFine"}, {"id": 3, "name": "Display"}])
        self.assertEqual(st["timer"], {"running": True, "elapsedMs": 0, "startedAtMs": 5000})

    def test_counter_and_next_hint(self):
        result = run_node("""
            const s = (o) => I.normalizeState(Object.assign({mainSlideCount: 36, totalSlides: 59}, o), 0);
            console.log(JSON.stringify([
              I.counterParts(s({slideIndex: 17, slideNumber: 18})),
              I.counterParts(s({slideIndex: 37, isAppendix: true, appendixLabel: 'A2'})),
              I.counterParts(s({slideIndex: 38, isAppendix: true})),
              I.nextHintText(s({slideIndex: 17, slideNumber: 18, step: 1, maxStep: 2})),
              I.nextHintText(s({slideIndex: 17, slideNumber: 18, step: 2, maxStep: 2})),
              I.nextHintText(s({slideIndex: 35, slideNumber: 36})),
              I.nextHintText(s({slideIndex: 58, isAppendix: true}))
            ]));
        """)
        self.assertEqual(result[0], {"num": "18", "total": "/ 36"})
        self.assertEqual(result[1], {"num": "Appendix", "total": "A2"})
        self.assertEqual(result[2], {"num": "Appendix", "total": "A3"})
        self.assertTrue(result[3].startswith("Next: step 2 of 2"))
        self.assertTrue(result[4].startswith("Next: slide 19"))
        self.assertEqual(result[5], "Last main slide")
        self.assertEqual(result[6], "Last slide")

    def test_presenter_page_has_no_notes_and_no_next_slide_preview(self):
        html = PRESENTER_HTML.read_text(encoding="utf-8")
        self.assertEqual(html.count("<iframe"), 1)
        self.assertNotIn("notes", html.lower())
        self.assertNotIn("next-slide", html.lower())
        js = PRESENTER_JS.read_text(encoding="utf-8")
        self.assertNotIn("getNotes", js)
        self.assertNotIn(".zoom", js)


FIXTURE_DECK = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Fixture talk</title>
<link rel="stylesheet" href="/repo/engine/webslides.css">
<script defer src="/repo/engine/webslides.js"></script></head>
<body><div class="ws-deck" data-section-nav>
<section class="slide title"><h1>Fixture talk</h1></section>
<section class="slide content"><h2>Motivation</h2></section>
<section class="slide section-header"><h1>Methods</h1></section>
<section class="slide content"><h2>Model</h2><p data-step="1">one</p><p data-step="2">two</p></section>
<section class="slide content"><h2>Results<br>in <span class="katex"><span class="katex-mathml">MATHML</span><span class="katex-html">full</span></span></h2></section>
<section class="slide section-header"><h1>Conclusion</h1></section>
<section class="slide content"><h2>Summary</h2></section>
<section class="slide appendix content" id="app-a"><h2>Appendix A</h2></section>
<section class="slide appendix content" id="app-b"><h2>Appendix B</h2></section>
</div>
<div id="ws-footer"><span id="ws-footer-left">Fixture &middot; 2026</span><span id="ws-footer-right"></span></div>
</body></html>
"""

SECTIONS = [{"title": "Intro", "first": 0, "last": 1},
            {"title": "Methods", "first": 2, "last": 4},
            {"title": "Conclusion", "first": 5, "last": 6}]


def live_state(**over):
    state = {
        "phase": "live", "deckTitle": "Fixture talk", "slideIndex": 3, "step": 1, "maxStep": 2,
        "mainSlideCount": 7, "totalSlides": 9, "slideNumber": 4, "isAppendix": False,
        "appendixLabel": None, "slideTitle": "Model", "sections": SECTIONS, "talkMinutes": 14,
        "timer": {"running": False, "elapsedMs": 4 * 60000, "startedAtMs": None},
        "displays": [{"id": "5", "name": "Projector"}], "audienceDisplayId": "5", "blackout": False,
    }
    state.update(over)
    return state


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class ConsolePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        deck_dir = Path(cls.tmp.name)
        (deck_dir / "index.html").write_text(FIXTURE_DECK, encoding="utf-8")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                path = path.split("?", 1)[0].split("#", 1)[0]
                if path.startswith("/repo/"):
                    return str(ROOT / path[len("/repo/"):])
                if path.startswith("/deck/"):
                    return str(deck_dir / path[len("/deck/"):])
                return str(deck_dir / "missing")

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

    def open(self, width=1440, height=848):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        page.set_default_timeout(8000)
        self.addCleanup(page.close)
        page.add_init_script("""
            window.__msgs = [];
            window.webkit = { messageHandlers: { presto: { postMessage(m) { window.__msgs.push(m); } } } };
        """)
        page.goto(self.base + "/repo/presto/presenter.html")
        page.evaluate("url => initPresenter(url)", self.base + "/deck/index.html")
        page.wait_for_function("window.__msgs.some(m => m.type === 'ready')")
        page.evaluate("() => document.getElementById('current-slide').contentWindow.WS.ready")
        return page

    @staticmethod
    def set_state(page, state):
        page.evaluate("s => PrestoConsole.setState(s)", state)

    @staticmethod
    def messages(page, type_):
        return page.evaluate("t => window.__msgs.filter(m => m.type === t)", type_)

    @staticmethod
    def text(page, selector):
        return page.eval_on_selector(selector, "el => el.textContent")

    @staticmethod
    def visible(page, selector):
        return page.is_visible(selector)

    def test_cards_wait_for_the_first_state_then_show_the_ready_phase(self):
        page = self.open()
        self.assertEqual(page.eval_on_selector("#side", "el => getComputedStyle(el).visibility"), "hidden")
        self.set_state(page, live_state(phase="ready", displays=[], talkMinutes=None))
        self.assertEqual(page.eval_on_selector("#side", "el => getComputedStyle(el).visibility"), "visible")
        self.assertTrue(self.visible(page, "#ready-card"))
        self.assertFalse(self.visible(page, "#clock-card"))
        self.assertTrue(self.visible(page, "#one-screen-note"))
        self.assertFalse(self.visible(page, "#audience-row"))
        self.assertEqual(self.text(page, "#ready-deck-title"), "Fixture talk")
        self.assertEqual(page.input_value("#talk-minutes"), "")

    def test_start_posts_once_and_waits_for_presto_to_go_live(self):
        page = self.open()
        self.set_state(page, live_state(phase="ready"))
        page.click("#start-btn")
        page.click("#start-btn", force=True)
        self.assertEqual(len(self.messages(page, "consoleStart")), 1)
        self.assertEqual(self.text(page, "#start-btn-label"), "Starting\u2026")
        self.assertTrue(self.visible(page, "#ready-card"))
        self.set_state(page, live_state())
        self.assertFalse(self.visible(page, "#ready-card"))
        self.assertTrue(self.visible(page, "#clock-card"))

    def test_audience_display_controls(self):
        page = self.open()
        self.set_state(page, live_state(phase="ready"))
        self.assertTrue(self.visible(page, "#audience-name"))
        self.assertEqual(self.text(page, "#audience-name"), "Projector")
        self.assertFalse(self.visible(page, "#audience-select"))
        self.assertFalse(self.visible(page, "#one-screen-note"))
        page.click("#swap-btn")
        self.assertEqual(len(self.messages(page, "consoleSwapScreens")), 1)

        self.set_state(page, live_state(phase="ready", audienceDisplayId="5",
                                        displays=[{"id": "5", "name": "Projector"},
                                                  {"id": "9", "name": "LG UltraFine"}]))
        self.assertTrue(self.visible(page, "#audience-select"))
        self.assertEqual(page.eval_on_selector("#audience-select", "s => s.selectedOptions[0].textContent"), "Projector")
        page.select_option("#audience-select", index=1)
        self.assertEqual(self.messages(page, "consoleSetAudienceDisplay"),
                         [{"type": "consoleSetAudienceDisplay", "id": "9"}])

    def test_ready_card_rows_have_hairlines_between_them_and_none_under_the_last(self):
        # [top, bottom] border widths of the visible rows: talk length, then the
        # audience screen (two screens) or the one-screen note.
        page = self.open()
        for displays, expected in (([{"id": "5", "name": "Projector"}], [[1, 0], [1, 0]]), ([], [[1, 0], [0, 0]])):
            with self.subTest(displays=len(displays)):
                self.set_state(page, live_state(phase="ready", displays=displays))
                lines = page.evaluate("""() => {
                    const px = (el, side) => parseFloat(getComputedStyle(el)['border' + side + 'Width']) || 0;
                    const box = document.querySelector('.ready-rows');
                    const rows = [...box.children].filter(el => el.offsetParent !== null);
                    return {rows: rows.map(el => [px(el, 'Top'), px(el, 'Bottom')]),
                            container: [px(box, 'Top'), px(box, 'Bottom')]};
                }""")
                self.assertEqual(lines, {"rows": expected, "container": [0, 0]})

    def test_talk_length_field_commits_reports_editing_and_rejects_bad_values(self):
        page = self.open()
        self.set_state(page, live_state(phase="ready", talkMinutes=None))
        page.click("#talk-minutes")
        self.assertEqual(self.messages(page, "consoleTextEditing")[-1]["active"], True)
        page.keyboard.type("50")
        page.keyboard.press("Enter")
        self.assertEqual(self.messages(page, "consoleSetTalkMinutes"), [{"type": "consoleSetTalkMinutes", "minutes": 50}])
        self.assertEqual(self.messages(page, "consoleTextEditing")[-1]["active"], False)

        page.click("#talk-minutes")
        page.keyboard.press("Meta+a")
        page.keyboard.type("700")
        page.keyboard.press("Enter")
        self.assertEqual(len(self.messages(page, "consoleSetTalkMinutes")), 1)
        self.assertIn("invalid", page.get_attribute("#talk-minutes", "class"))
        page.keyboard.press("Escape")
        self.assertEqual(page.input_value("#talk-minutes"), "50")

        page.click("#talk-minutes")
        page.keyboard.press("Meta+a")
        page.keyboard.press("Backspace")
        page.click("#ready-deck-title")
        self.assertEqual(self.messages(page, "consoleSetTalkMinutes")[-1]["minutes"], None)

        # A later state from Presto does not overwrite the field while it is being typed in.
        page.click("#talk-minutes")
        page.keyboard.type("3")
        self.set_state(page, live_state(phase="ready", talkMinutes=45))
        self.assertEqual(page.input_value("#talk-minutes"), "3")

    def test_live_clock_shows_elapsed_talk_length_pace_and_progress(self):
        page = self.open()
        # 7 main slides in 14 minutes; slide 4 step 1 of 0..2 is due 6:40 to 7:20.
        self.set_state(page, live_state())
        self.assertEqual(self.text(page, "#timer"), "04:00")
        self.assertEqual(self.text(page, "#talk-of"), "of 14:00")
        self.assertEqual(self.text(page, "#pace-text"), "3 min ahead of pace")
        self.assertIn("ahead", page.get_attribute("#pace-text", "class"))
        self.assertRegex(self.text(page, "#ends-at"), r"^Ends at \d\d:\d\d$")
        fill = page.eval_on_selector("#pace-fill", "el => parseFloat(el.style.width)")
        marker = page.eval_on_selector("#pace-marker", "el => parseFloat(el.style.left)")
        self.assertAlmostEqual(fill, 4 / 14 * 100, places=1)
        self.assertAlmostEqual(marker, (3 + 1 / 3) / 7 * 100, places=1)
        self.assertEqual(self.text(page, "#clock-label"), "Paused")
        self.assertIn("paused", page.get_attribute("#timer", "class"))

        self.set_state(page, live_state(timer={"running": False, "elapsedMs": 7 * 60000, "startedAtMs": None}))
        self.assertEqual(self.text(page, "#pace-text"), "On pace")
        self.set_state(page, live_state(timer={"running": False, "elapsedMs": 9 * 60000, "startedAtMs": None}))
        self.assertEqual(self.text(page, "#pace-text"), "2 min behind pace")
        self.assertIn("behind", page.get_attribute("#pace-text", "class"))
        self.set_state(page, live_state(timer={"running": False, "elapsedMs": 17 * 60000, "startedAtMs": None}))
        self.assertEqual(self.text(page, "#ends-at"), "Over by 3 min")
        self.assertIn("over", page.get_attribute("#pace-bar", "class"))

    def test_running_timer_ticks_from_the_swift_start_time(self):
        page = self.open()
        now = page.evaluate("Date.now()")
        self.set_state(page, live_state(timer={"running": True, "elapsedMs": 60000, "startedAtMs": now - 5000}))
        self.assertEqual(self.text(page, "#timer"), "01:05")
        page.wait_for_function("document.getElementById('timer').textContent === '01:07'", timeout=4000)
        self.assertEqual(self.text(page, "#timer-btn"), "Pause")
        self.assertEqual(self.text(page, "#clock-label"), "Elapsed")

    def test_timer_buttons_post_actions_and_reset_needs_a_second_click(self):
        page = self.open()
        self.set_state(page, live_state())
        self.assertEqual(self.text(page, "#timer-btn"), "Resume")
        page.click("#timer-btn")
        self.assertEqual(self.text(page, "#timer-btn"), "Pause")
        page.click("#timer-btn")
        self.assertEqual(self.text(page, "#timer-btn"), "Resume")
        self.assertEqual([m["action"] for m in self.messages(page, "consoleTimer")], ["start", "pause"])

        page.click("#timer-reset")
        self.assertEqual([m["action"] for m in self.messages(page, "consoleTimer")], ["start", "pause"])
        self.assertEqual(self.text(page, "#timer-reset"), "Confirm reset")
        page.click("#timer-reset")
        self.assertEqual(self.messages(page, "consoleTimer")[-1]["action"], "reset")
        self.assertEqual(self.text(page, "#timer"), "00:00")
        self.assertEqual(self.text(page, "#timer-btn"), "Start")
        self.assertTrue(page.eval_on_selector("#timer-reset", "b => b.disabled"))

    def test_reset_confirmation_expires(self):
        page = self.open()
        page.evaluate("() => { window.__realNow = Date.now; }")
        self.set_state(page, live_state())
        page.click("#timer-reset")
        page.wait_for_function("document.getElementById('timer-reset').textContent === 'Reset'", timeout=5000)
        page.click("#timer-reset")
        self.assertEqual(self.messages(page, "consoleTimer"), [])

    def test_clock_does_not_shift_the_layout_as_it_ticks(self):
        page = self.open()
        widths, lefts = set(), set()
        for elapsed in (0, 61000, 11 * 60000 + 48000, 59 * 60000 + 59000):
            self.set_state(page, live_state(timer={"running": False, "elapsedMs": elapsed, "startedAtMs": None}))
            box = page.eval_on_selector("#timer", "el => el.getBoundingClientRect().width")
            left = page.eval_on_selector("#talk-of", "el => el.getBoundingClientRect().left")
            widths.add(round(box, 1))
            lefts.add(round(left, 1))
        self.assertEqual(len(widths), 1, widths)
        self.assertEqual(len(lefts), 1, lefts)

    def test_live_talk_length_can_be_set_from_the_clock(self):
        page = self.open()
        self.set_state(page, live_state(talkMinutes=None))
        self.assertEqual(self.text(page, "#talk-of"), "Set talk length")
        self.assertFalse(self.visible(page, "#pace-bar"))
        self.assertFalse(self.visible(page, "#clock-row"))
        page.click("#talk-of")
        self.assertTrue(self.visible(page, "#talk-minutes-live"))
        page.keyboard.type("30")
        page.keyboard.press("Enter")
        self.assertEqual(self.messages(page, "consoleSetTalkMinutes")[-1]["minutes"], 30)
        self.assertEqual(self.text(page, "#talk-of"), "of 30:00")
        self.assertTrue(self.visible(page, "#pace-bar"))
        self.assertFalse(self.visible(page, "#talk-minutes-live"))

    def test_sections_card_shows_progress_and_jumps(self):
        page = self.open()
        self.set_state(page, live_state())
        rows = page.eval_on_selector_all(
            "#sections-list li",
            "els => els.map(li => [li.className, li.querySelector('.t').textContent, li.querySelector('em').textContent, li.getAttribute('data-index')])")
        self.assertEqual(rows, [
            ["done", "Intro", "1\u20132", "0"],
            ["now", "Methods", "2 of 3", None],
            ["next", "Conclusion", "6\u20137", "5"],
            ["next appendix", "Appendix", "A1\u2013A2", "7"],
        ])
        page.click("#sections-list li >> text=Intro")
        page.click("#sections-list li >> text=Appendix")
        page.click("#sections-list li >> text=Methods")
        self.assertEqual(self.messages(page, "consoleNavigate"), [
            {"type": "consoleNavigate", "index": 0, "step": 0},
            {"type": "consoleNavigate", "index": 7, "step": 0},
        ])

    def test_appendix_visit_keeps_main_progress_and_pace(self):
        page = self.open()
        self.set_state(page, live_state())
        self.set_state(page, live_state(slideIndex=8, step=0, maxStep=0, slideNumber=None, isAppendix=True,
                                        appendixLabel="A2", slideTitle="Appendix B"))
        self.assertEqual(self.text(page, "#slide-counter-num"), "Appendix")
        self.assertEqual(self.text(page, "#slide-counter-total"), "A2")
        statuses = page.eval_on_selector_all("#sections-list li", "els => els.map(li => li.className)")
        self.assertEqual(statuses, ["done", "next", "next", "now appendix"])
        self.assertEqual(page.eval_on_selector("#sections-list li.appendix em", "e => e.textContent"), "2 of 2")
        self.assertEqual(self.text(page, "#pace-text"), "3 min ahead of pace")
        self.assertEqual(self.text(page, "#next-hint"), "Last slide")

    def test_presenter_update_drives_counter_dots_and_title_without_navigating_from_set_state(self):
        page = self.open()
        self.set_state(page, live_state(slideIndex=0, step=0, maxStep=0, slideNumber=1, slideTitle=""))
        page.evaluate("""() => {
            const w = document.getElementById('current-slide').contentWindow;
            w.WS.goToSlide(3, 1);
            presenterUpdate(w.WS.getState());
        }""")
        self.assertEqual(self.text(page, "#slide-counter-num"), "4")
        self.assertEqual(self.text(page, "#slide-counter-total"), "/ 7")
        self.assertEqual(self.text(page, "#slide-title"), "Model")
        dots = page.eval_on_selector_all("#step-dots b", "bs => bs.map(b => b.className + ':' + b.textContent)")
        self.assertEqual(dots, ["done:0", "on:1", ":2"])
        page.evaluate("""() => {
            const w = document.getElementById('current-slide').contentWindow;
            w.WS.goToSlide(4, 0);
            presenterUpdate(w.WS.getState());
        }""")
        # The heading is "Results<br>in <KaTeX>": a space for the break, no MathML copy.
        self.assertEqual(self.text(page, "#slide-title"), "Results in full")
        self.assertFalse(self.visible(page, "#step-dots"))

        # setState renders a position but never moves the preview iframe.
        self.set_state(page, live_state(slideIndex=6, slideNumber=7, step=0, maxStep=0, slideTitle="Summary"))
        self.assertEqual(page.evaluate("document.getElementById('current-slide').contentWindow.WS.currentIndex"), 4)
        self.assertEqual(self.text(page, "#slide-counter-num"), "7")
        self.assertEqual(self.messages(page, "presenterNavigated")[-1]["index"], 4)

    def test_hint_toast_and_blackout(self):
        page = self.open()
        self.set_state(page, live_state(blackout=True))
        self.assertTrue(self.visible(page, "#blackout-veil"))
        page.click("#blackout-off")
        self.assertEqual(self.messages(page, "consoleBlackout"), [{"type": "consoleBlackout", "on": False}])
        self.assertFalse(self.visible(page, "#blackout-veil"))

        page.evaluate("PrestoConsole.hint('Press Escape again to end the presentation', 400)")
        self.assertEqual(self.text(page, "#console-hint"), "Press Escape again to end the presentation")
        self.assertIn("visible", page.get_attribute("#console-hint", "class"))
        page.wait_for_function("!document.getElementById('console-hint').classList.contains('visible')", timeout=3000)

    PREVIEW_CHROME_JS = """() => {
        const f = document.getElementById('current-slide');
        const d = f.contentDocument;
        const box = el => { const r = el.getBoundingClientRect(); return {l: r.left, t: r.top, w: r.width, b: r.bottom}; };
        const footer = d.getElementById('ws-footer');
        const bar = d.getElementById('ws-progress');
        return {mode: f.contentWindow.WS.mode, vw: f.contentWindow.innerWidth, vh: f.contentWindow.innerHeight,
                footerShown: getComputedStyle(footer).display !== 'none', barShown: getComputedStyle(bar).display !== 'none',
                footer: box(footer), bar: box(bar), right: d.getElementById('ws-footer-right').textContent,
                styles: d.querySelectorAll('#presto-console-preview-chrome').length};
    }"""

    def test_preview_shows_the_deck_footer_and_progress_line_on_the_slide_frame(self):
        # The engine's preview mode hides both; the console shows them in its
        # own iframe so the slide looks as the audience sees it.
        page = self.open()
        self.set_state(page, live_state())
        page.evaluate("""() => {
            const w = document.getElementById('current-slide').contentWindow;
            w.WS.goToSlide(3, 0);
            presenterUpdate(w.WS.getState());
        }""")
        page.wait_for_function("""() => {
            const bar = document.getElementById('current-slide').contentDocument.getElementById('ws-progress');
            return Math.abs(bar.getBoundingClientRect().width - 1280 * 4 / 7) < 1;
        }""")
        geo = page.evaluate(self.PREVIEW_CHROME_JS)
        self.assertEqual(geo["mode"], "preview")
        self.assertEqual((geo["vw"], geo["vh"]), (1280, 720))
        self.assertTrue(geo["footerShown"] and geo["barShown"], geo)
        self.assertAlmostEqual(geo["footer"]["b"], 720, delta=0.5, msg="footer on the slide's bottom edge")
        self.assertAlmostEqual(geo["footer"]["l"], 0, delta=0.5)
        self.assertAlmostEqual(geo["footer"]["w"], 1280, delta=0.5, msg="footer exactly the slide's width")
        self.assertAlmostEqual(geo["bar"]["t"], 0, delta=0.5, msg="progress line on the slide's top edge")
        self.assertAlmostEqual(geo["bar"]["l"], 0, delta=0.5)
        self.assertEqual(geo["right"], "4 / 7")
        self.assertEqual(geo["styles"], 1)

        # The slide grid keeps them hidden.
        page.evaluate("document.getElementById('current-slide').contentWindow.WS.toggleOverview()")
        geo = page.evaluate(self.PREVIEW_CHROME_JS)
        self.assertFalse(geo["footerShown"] or geo["barShown"], geo)

        # A reload of the iframe (a refresh) shows them again, once.
        page.evaluate("document.getElementById('current-slide').contentWindow.location.reload()")
        page.wait_for_function("""() => {
            const w = document.getElementById('current-slide').contentWindow;
            return w.WS && w.document.documentElement.dataset.wsReady === '1' && !w.WS.overviewActive &&
                   !!w.document.getElementById('presto-console-preview-chrome');
        }""")
        geo = page.evaluate(self.PREVIEW_CHROME_JS)
        self.assertEqual(geo["mode"], "preview")
        self.assertTrue(geo["footerShown"] and geo["barShown"], geo)
        self.assertEqual(geo["styles"], 1)

    def test_stage_fits_a_transform_scaled_16_9_frame_at_every_window_shape(self):
        for width, height in ((1440, 848), (1000, 700), (1728, 1050), (1300, 600)):
            with self.subTest(size=(width, height)):
                page = self.open(width, height)
                self.set_state(page, live_state())
                geo = page.evaluate("""() => {
                    const f = document.getElementById('slide-frame').getBoundingClientRect();
                    const s = document.getElementById('stage').getBoundingClientRect();
                    const b = document.getElementById('stagebar').getBoundingClientRect();
                    const iframe = document.getElementById('current-slide');
                    return {fw: f.width, fh: f.height, fl: f.left, fr: f.right, ft: f.top, fb: f.bottom,
                            sl: s.left, sr: s.right, st: s.top, sb: s.bottom, bw: b.width, bb: b.bottom,
                            transform: iframe.style.transform, zoom: getComputedStyle(iframe).zoom,
                            scrollW: document.documentElement.scrollWidth, scrollH: document.documentElement.scrollHeight};
                }""")
                self.assertAlmostEqual(geo["fw"] / geo["fh"], 16 / 9, delta=0.01)
                self.assertGreaterEqual(geo["fl"], geo["sl"] - 0.5)
                self.assertLessEqual(geo["fr"], geo["sr"] + 0.5)
                self.assertGreaterEqual(geo["ft"], geo["st"] - 0.5)
                self.assertLessEqual(geo["bb"], geo["sb"] + 0.5)
                self.assertAlmostEqual(geo["bw"], geo["fw"], delta=0.5)
                self.assertTrue(geo["transform"].startswith("scale("), geo["transform"])
                self.assertIn(geo["zoom"], ("1", "normal"))
                self.assertLessEqual(geo["scrollW"], width)
                self.assertLessEqual(geo["scrollH"], height)
                page.close()


if __name__ == "__main__":
    unittest.main()

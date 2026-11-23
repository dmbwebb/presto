#!/usr/bin/env python3
"""Inspector (presto/editor.{html,js,css}) in Playwright WebKit.

Covers what the Node stubs in test_editor_js.py cannot: the highlighted pre
still wraps exactly like the textarea above it (so the caret stays aligned)
now that it draws a line-number gutter, the visible save-error state and its
retry, and the Refine with AI composer: Cmd-Return sends, Return is a new
line, earlier changes and Undo last come from the unified history, focus()
and Cmd-J focus it, Cmd-I does not, its focus is posted as refineTextEditing,
and the close button posts inspectorClose.
"""
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the local Python
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
EDITOR_HTML = ROOT / "presto" / "editor.html"

LONG_BODY = """
    <h2>3. What we estimate</h2>
    <div class="eqbox">
        $$Y_{ijt} = \\alpha_j + \\beta_j\\, t_{ijt} + \\theta_{ij} + \\varepsilon_{ijt}$$
    </div>

    <ul style="margin-top:28px">
        <li data-step="1"><b>Average fadeout</b> &mdash; mean movement toward zero, SD per year</li>
\t\t<li data-step="2">averyveryveryveryveryveryveryveryveryveryveryverylongwordwithoutanybreakopportunityatall</li>
    </ul>
    <!-- a comment that is long enough to wrap onto a second line in a 360 pixel inspector -->
"""


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class InspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.webkit.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open(self, width=360, height=848, body=LONG_BODY, start_line=None):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        page.set_default_timeout(5000)
        self.addCleanup(page.close)
        page.add_init_script("""
            window.__msgs = [];
            window.webkit = { messageHandlers: { editor: { postMessage(m) { window.__msgs.push(m); } } } };
        """)
        page.goto(EDITOR_HTML.as_uri())
        page.evaluate("a => setContent('<section class=\"slide content v-center\">', a[0], '</section>', 17, 18, 38, false, a[1])",
                      [body, start_line])
        return page

    @staticmethod
    def messages(page, type_=None):
        msgs = page.evaluate("window.__msgs")
        return [m for m in msgs if type_ is None or m["type"] == type_]

    def test_highlighting_wraps_exactly_like_the_textarea(self):
        bodies = [LONG_BODY, "", "\n", "one\n\n\nfour\n", "x" * 400, "a\tb\t\tc\n" * 30, "$x$ and $$y$$ <b>z</b>\n"]
        for width in (360, 300, 480):
            for body in bodies:
                with self.subTest(width=width, body=body[:20]):
                    page = self.open(width=width, body=body)
                    geo = page.evaluate("""() => {
                        const ta = document.getElementById('editor');
                        const pre = document.getElementById('highlighting');
                        const cs = [getComputedStyle(ta), getComputedStyle(pre)];
                        return {ta: ta.scrollHeight, pre: pre.scrollHeight,
                                padding: cs.map(c => [c.paddingLeft, c.paddingTop, c.paddingRight].join()),
                                font: cs.map(c => [c.fontFamily, c.fontSize, c.lineHeight, c.whiteSpace, c.tabSize].join()),
                                width: [ta.clientWidth, pre.clientWidth],
                                lines: document.querySelectorAll('#highlighting-content .hl-line').length,
                                logical: ta.value.split('\\n').length};
                    }""")
                    self.assertEqual(geo["ta"], geo["pre"])
                    self.assertEqual(geo["padding"][0], geo["padding"][1])
                    self.assertEqual(geo["font"][0], geo["font"][1])
                    self.assertEqual(geo["width"][0], geo["width"][1])
                    self.assertEqual(geo["lines"], geo["logical"])
                    page.close()

    def test_typing_keeps_the_pre_text_identical_to_the_textarea(self):
        page = self.open()
        page.click("#editor")
        page.keyboard.press("Meta+ArrowDown")
        page.keyboard.type("\n<p>new & <i>line</i> $a_b$</p>")
        texts = page.evaluate("""() => [document.getElementById('editor').value,
            [...document.querySelectorAll('#highlighting-content .hl-line')].map(l => l.textContent.replace(/\\u200B/g, '')).join('\\n')]""")
        self.assertEqual(texts[0], texts[1])
        self.assertEqual(self.messages(page, "editorDirty")[-1]["dirty"], True)

    def test_gutter_numbers_lines_from_the_file_line_and_widens_for_digits(self):
        page = self.open(body="a\nb\nc", start_line=998)
        numbers = page.evaluate("""() => [...document.querySelectorAll('#highlighting-content .hl-line')]
            .map(l => getComputedStyle(l, '::before').content)""")
        self.assertEqual(numbers, ["counter(line)"] * 3)
        reset = page.eval_on_selector("#highlighting-content", "e => e.style.counterReset")
        self.assertEqual(reset, "line 997")
        gutter = page.eval_on_selector("#editor-container", "e => e.style.getPropertyValue('--gutter')")
        self.assertEqual(gutter, "calc(4ch + 22px)")
        page.evaluate("setContent('<section class=\"slide\">', 'x', '</section>', 2, 3, 38)")
        self.assertEqual(page.eval_on_selector("#highlighting-content", "e => e.style.counterReset"), "line 0")

    def test_section_wrapper_lines_are_quiet_and_not_editable(self):
        page = self.open()
        info = page.evaluate("""() => {
            const open = document.getElementById('section-open');
            const cs = getComputedStyle(open);
            return {text: open.textContent, color: cs.color, select: cs.webkitUserSelect || cs.userSelect,
                    editable: open.isContentEditable, close: document.getElementById('section-close').textContent};
        }""")
        self.assertEqual(info["text"], '<section class="slide content v-center">')
        self.assertEqual(info["close"], "</section>")
        self.assertEqual(info["color"], "rgb(169, 175, 184)")
        self.assertEqual(info["select"], "none")
        self.assertFalse(info["editable"])

    def test_save_error_is_visible_until_the_slide_saves(self):
        page = self.open()
        self.assertFalse(page.is_visible("#save-error"))
        page.evaluate("""() => {
            const ta = document.getElementById('editor');
            ta.value += '<p>edit</p>';
            ta.dispatchEvent(new Event('input'));
            flushPendingChange();
        }""")
        save_id = self.messages(page, "editorChanged")[-1]["saveId"]
        self.assertEqual(page.inner_text("#save-status"), "Saving…")
        page.evaluate("id => saveFailed(id, 'Save failed: the disk is full')", save_id)
        self.assertTrue(page.is_visible("#save-error"))
        self.assertEqual(page.inner_text("#save-error-text"), "Save failed: the disk is full")
        self.assertEqual(page.inner_text("#save-status"), "Not saved")
        page.wait_for_timeout(300)  # the status colour eases in
        self.assertEqual(page.eval_on_selector("#save-status", "s => getComputedStyle(s).color"), "rgb(212, 70, 44)")
        self.assertTrue(page.evaluate("getPendingEditorChange().dirty"))

        page.click("#save-retry")
        retry = self.messages(page, "editorChanged")[-1]
        self.assertNotEqual(retry["saveId"], save_id)
        self.assertEqual(page.inner_text("#save-status"), "Saving…")
        self.assertFalse(page.is_visible("#save-error"))
        page.evaluate("id => saveConfirmed(id)", retry["saveId"])
        self.assertEqual(page.inner_text("#save-status"), "Saved")
        self.assertFalse(page.is_visible("#save-error"))
        self.assertFalse(page.evaluate("getPendingEditorChange().dirty"))

    def test_refine_composer_sends_with_cmd_return_only(self):
        page = self.open()
        self.assertTrue(page.is_visible("#refine-form"))
        page.click("#refine-input")
        page.keyboard.type("Put the two equations")
        page.keyboard.press("Enter")
        page.keyboard.type("side by side")
        self.assertEqual(self.messages(page, "refineRequested"), [])
        page.keyboard.press("Meta+Enter")
        self.assertEqual(self.messages(page, "refineRequested"),
                         [{"type": "refineRequested", "instructions": "Put the two equations\nside by side"}])
        self.assertEqual(page.input_value("#refine-input"), "")
        page.click("#refine-submit")
        self.assertEqual(len(self.messages(page, "refineRequested")), 1)
        page.fill("#refine-input", "Shrink the box")
        page.click("#refine-submit")
        self.assertEqual(self.messages(page, "refineRequested")[-1]["instructions"], "Shrink the box")

    def test_earlier_changes_and_undo_last_follow_the_unified_history(self):
        page = self.open()
        self.assertFalse(page.is_visible("#refine-history-toggle"))
        self.assertTrue(page.is_disabled("#refine-undo"))
        self.assertFalse(page.is_visible("#refine-redo"))
        page.evaluate("""() => prestoRefine.setState({slideIndex: 17, running: null, queued: [],
            history: [{id: 'a', prompt: 'Lighter box', at: Date.UTC(2026, 9, 28, 9, 5)},
                      {id: 'b', prompt: 'Align bullets', at: '2026-10-28T09:20:00Z'}],
            canUndo: true, canRedo: false, undoLabel: 'Undo Refine', redoLabel: 'Redo', lastError: null})""")
        self.assertEqual(page.inner_text("#refine-history-toggle"), "2 earlier changes on this slide")
        self.assertFalse(page.is_disabled("#refine-undo"))
        self.assertEqual(page.get_attribute("#refine-undo", "title"), "Undo Refine")
        page.click("#refine-undo")
        self.assertEqual(self.messages(page, "refineUndo"), [{"type": "refineUndo"}])

        page.click("#refine-history-toggle")
        items = page.eval_on_selector_all(".refine-history-item .refine-history-text", "els => els.map(e => e.textContent)")
        self.assertEqual(items, ["Align bullets", "Lighter box"])
        self.assertEqual(page.get_attribute("#refine-history-toggle", "aria-expanded"), "true")

        page.evaluate("""() => prestoRefine.setState({slideIndex: 17, history: [{id: 'a', prompt: 'Lighter box', at: 0}],
            canUndo: false, canRedo: true, redoLabel: 'Redo Refine'})""")
        self.assertEqual(page.inner_text("#refine-history-toggle"), "1 earlier change on this slide")
        self.assertTrue(page.is_visible("#refine-redo"))
        page.click("#refine-redo")
        self.assertEqual(self.messages(page, "refineRedo"), [{"type": "refineRedo"}])

        # Presto's own shape still works; unapplied and failed entries are not "changes".
        page.evaluate("""() => prestoRefine.setState({history: [
            {instructions: 'one', timestamp: '2026-10-28T09:00:00Z', applied: true, status: 'done'},
            {instructions: 'two', timestamp: '2026-10-28T09:01:00Z', applied: false, status: 'done'},
            {instructions: 'three', timestamp: '2026-10-28T09:02:00Z', applied: false, status: 'error'}]})""")
        self.assertEqual(page.inner_text("#refine-history-toggle"), "1 earlier change on this slide")

    def test_running_queued_and_error_states(self):
        page = self.open()
        page.evaluate("""() => prestoRefine.setState({running: {instructions: 'Side by side'},
            queued: [{instructions: 'Shrink'}], lastError: 'Refine failed: rate limited'})""")
        self.assertTrue(page.is_visible("#refine-status .refine-spinner"))
        self.assertEqual(page.inner_text("#refine-status-text"), "Refining (+1 queued): Side by side")
        # A long instruction is cut with an ellipsis after the queued count, never through it.
        page.evaluate("""() => prestoRefine.setState({running: {instructions:
            'Put the two equations side by side and shrink the box so the bullets fit below it'},
            queued: [{instructions: 'Shrink'}], lastError: 'Refine failed: rate limited'})""")
        cut = page.evaluate("""() => {
            const el = document.getElementById('refine-status-text');
            const range = document.createRange();
            range.selectNodeContents(el);
            const text = el.firstChild;
            const end = text.textContent.indexOf(':') + 1;
            range.setEnd(text, end);
            return {clipped: el.scrollWidth > el.clientWidth + 1,
                    countRight: range.getBoundingClientRect().right, boxRight: el.getBoundingClientRect().right};
        }""")
        self.assertTrue(cut["clipped"], "the long instruction is truncated at 360 px")
        self.assertLess(cut["countRight"], cut["boxRight"] - 20, "the queued count stays readable")
        self.assertTrue(page.is_visible("#refine-error"))
        self.assertEqual(page.inner_text("#refine-error-text"), "Refine failed: rate limited")
        self.assertNotIn("⏳", page.inner_text("#refine-bar"))
        page.click("#refine-error-dismiss")
        self.assertEqual(self.messages(page, "refineDismissError"), [{"type": "refineDismissError"}])
        page.evaluate("() => prestoRefine.setState({queued: [{instructions: 'a'}, {instructions: 'b'}]})")
        self.assertEqual(page.inner_text("#refine-status-text"), "2 changes queued")
        self.assertFalse(page.is_visible("#refine-error"))
        page.evaluate("() => prestoRefine.setState({})")
        self.assertFalse(page.is_visible("#refine-status"))

    def test_refine_focus_paths_and_no_cmd_i(self):
        page = self.open()
        page.click("#editor")
        page.keyboard.press("Meta+i")
        self.assertEqual(page.evaluate("document.activeElement.id"), "editor")
        page.keyboard.press("Meta+j")
        self.assertEqual(page.evaluate("document.activeElement.id"), "refine-input")
        page.click("#editor")
        page.evaluate("prestoRefine.focus()")
        self.assertEqual(page.evaluate("document.activeElement.id"), "refine-input")
        page.click("#editor")
        page.evaluate("focusRefine()")
        self.assertEqual(page.evaluate("document.activeElement.id"), "refine-input")

    def test_refine_prompt_reports_its_focus_so_cmd_z_stays_in_it(self):
        page = self.open()
        page.click("#refine-input")
        self.assertEqual(self.messages(page, "refineTextEditing")[-1], {"type": "refineTextEditing", "active": True})
        page.click("#editor")
        self.assertEqual(self.messages(page, "refineTextEditing")[-1], {"type": "refineTextEditing", "active": False})
        page.keyboard.press("Meta+j")
        self.assertEqual(self.messages(page, "refineTextEditing")[-1]["active"], True)

    def test_refine_is_pinned_at_the_foot_and_close_posts_inspector_close(self):
        page = self.open(height=700)
        geo = page.evaluate("""() => {
            const bar = document.getElementById('refine-bar').getBoundingClientRect();
            return {bottom: bar.bottom, h: innerHeight, scroll: document.documentElement.scrollHeight};
        }""")
        self.assertAlmostEqual(geo["bottom"], geo["h"], delta=1)
        self.assertLessEqual(geo["scroll"], geo["h"])
        self.assertEqual(page.inner_text("#toolbar .tab.on"), "Source")
        page.click("#inspector-close")
        self.assertEqual(self.messages(page, "inspectorClose"), [{"type": "inspectorClose"}])

    def test_inspector_is_light(self):
        page = self.open()
        colours = page.evaluate("""() => ['body', '#highlighting', '#refine-bar', '#toolbar']
            .map(s => getComputedStyle(document.querySelector(s)).backgroundColor)""")
        for colour in colours:
            channels = [int(c) for c in colour[colour.index("(") + 1:colour.index(")")].split(",")[:3]]
            self.assertGreater(min(channels), 240, colour)


if __name__ == "__main__":
    unittest.main()

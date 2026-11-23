#!/usr/bin/env python3
import json
import subprocess
import textwrap
import unittest
from pathlib import Path


EDITOR_JS = Path(__file__).resolve().parents[1] / "presto" / "editor.js"
EDITOR_HTML = EDITOR_JS.with_name("editor.html")
EDITOR_CSS = EDITOR_JS.with_name("editor.css")


class EditorJSTests(unittest.TestCase):
    def run_node(self, script: str):
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=EDITOR_JS.parents[1],
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def test_source_sidebar_exposes_focus_and_accessibility_hooks(self):
        js_source = EDITOR_JS.read_text()
        html_source = EDITOR_HTML.read_text()

        self.assertIn("window.focusEditor = function", js_source)
        self.assertIn("editor.focus()", js_source)
        self.assertIn('aria-label="HTML source for the current slide"', html_source)
        self.assertIn('role="status"', html_source)
        self.assertIn('aria-live="polite"', html_source)

    def test_source_sidebar_layout_adapts_at_compact_widths(self):
        # The inspector is 360px wide: the Refine head row wraps, the composer
        # fills the width, and below 300px the actions stack on their own row.
        css_source = EDITOR_CSS.read_text()
        head = css_source[css_source.index("#refine-head {"):]
        self.assertIn("flex-wrap: wrap", head[:head.index("}")])
        composer = css_source[css_source.index("#refine-input {"):]
        self.assertIn("width: 100%", composer[:composer.index("}")])
        self.assertIn("@media (max-width: 300px)", css_source)
        self.assertIn("flex-basis: 100%", css_source)

    def test_full_section_fallback_preserves_gt_inside_opening_attribute(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener() {{}},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener() {{}},
            }};

            eval(source);
            window.setContent('<section class="slide" data-title="A > B"><h1>Title</h1></section>', 0, 1, 1);
            console.log(JSON.stringify({{
              opening: element('section-open').textContent,
              body: element('editor').value,
              closing: element('section-close').textContent,
            }}));
            """
        )
        result = self.run_node(script)
        self.assertEqual('<section class="slide" data-title="A > B">', result["opening"])
        self.assertEqual("<h1>Title</h1>", result["body"])
        self.assertEqual("</section>", result["closing"])

    def test_save_failed_keeps_pending_change_dirty(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener() {{}},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener() {{}},
            }};

            eval(source);
            window.setContent('<section class="slide"><h1>Title</h1></section>', 0, 1, 1);
            element('editor').value = '<h1>Edited</h1>';
            window.saveFailed('Save failed');
            console.log(JSON.stringify(window.getPendingEditorChange()));
            """
        )
        result = self.run_node(script)
        self.assertTrue(result["dirty"])
        self.assertEqual("<h1>Edited</h1>", result["html"])

    def test_set_content_rejects_malformed_slide_index_without_overwriting_editor(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const warnings = [];
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener() {{}},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener() {{}},
            }};
            global.console = {{
              warn(...args) {{ warnings.push(args.join(' ')); }},
              log(...args) {{ process.stdout.write(args.join(' ')); }},
            }};

            eval(source);
            window.setContent('<section class="slide">', '<h1>Good</h1>', '</section>', 0, 1, 2);
            window.setContent('<section class="slide">', '<h1>Bad</h1>', '</section>', '1', 2, 2);
            window.setContent('<section class="slide">', '<h1>Also bad</h1>', '</section>', -1, 2, 2);

            console.log(JSON.stringify({{
              editorValue: element('editor').value,
              label: element('slide-label').textContent,
              pending: window.getPendingEditorChange(),
              warnings,
            }}));
            """
        )
        result = self.run_node(script)
        self.assertEqual("<h1>Good</h1>", result["editorValue"])
        self.assertEqual("Slide 1 / 2", result["label"])
        self.assertEqual(0, result["pending"]["index"])
        self.assertEqual("<h1>Good</h1>", result["pending"]["html"])
        self.assertFalse(result["pending"]["dirty"])
        self.assertEqual(2, sum("ignored malformed editor content" in warning for warning in result["warnings"]))

    def test_set_content_rejects_impossible_slide_number_or_total_without_overwriting_editor(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const warnings = [];
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener() {{}},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener() {{}},
            }};
            global.console = {{
              warn(...args) {{ warnings.push(args.join(' ')); }},
              log(...args) {{ process.stdout.write(args.join(' ')); }},
            }};

            eval(source);
            window.setContent('<section class="slide">', '<h1>Good</h1>', '</section>', 0, 1, 2);
            window.setContent('<section class="slide">', '<h1>Bad number</h1>', '</section>', 1, 3, 2);
            window.setContent('<section class="slide">', '<h1>Bad index</h1>', '</section>', 2, 2, 2);

            console.log(JSON.stringify({{
              editorValue: element('editor').value,
              label: element('slide-label').textContent,
              pending: window.getPendingEditorChange(),
              warnings,
            }}));
            """
        )
        result = self.run_node(script)
        self.assertEqual("<h1>Good</h1>", result["editorValue"])
        self.assertEqual("Slide 1 / 2", result["label"])
        self.assertEqual(0, result["pending"]["index"])
        self.assertEqual("<h1>Good</h1>", result["pending"]["html"])
        self.assertFalse(result["pending"]["dirty"])
        self.assertEqual(2, sum("ignored malformed editor content" in warning for warning in result["warnings"]))

    def test_set_content_rejects_mismatched_slide_index_and_display_number(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const warnings = [];
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener() {{}},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener() {{}},
            }};
            global.console = {{
              warn(...args) {{ warnings.push(args.join(' ')); }},
              log(...args) {{ process.stdout.write(args.join(' ')); }},
            }};

            eval(source);
            window.setContent('<section class="slide">', '<h1>Good</h1>', '</section>', 0, 1, 3);
            window.setContent('<section class="slide">', '<h1>Wrong label</h1>', '</section>', 0, 2, 3);
            window.setContent('<section class="slide">', '<h1>Wrong index</h1>', '</section>', 2, 2, 3);

            console.log(JSON.stringify({{
              editorValue: element('editor').value,
              label: element('slide-label').textContent,
              pending: window.getPendingEditorChange(),
              warnings,
            }}));
            """
        )
        result = self.run_node(script)
        self.assertEqual("<h1>Good</h1>", result["editorValue"])
        self.assertEqual("Slide 1 / 3", result["label"])
        self.assertEqual(0, result["pending"]["index"])
        self.assertEqual("<h1>Good</h1>", result["pending"]["html"])
        self.assertFalse(result["pending"]["dirty"])
        self.assertEqual(2, sum("ignored malformed editor content" in warning for warning in result["warnings"]))

    def test_late_save_confirmation_does_not_clear_new_unsaved_edit(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const listeners = {{}};
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener(event, handler) {{
                  listeners[id + ':' + event] = handler;
                }},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener(event, handler) {{
                listeners['document:' + event] = handler;
              }},
            }};

            eval(source);
            window.setContent('<section class="slide"><h1>Title</h1></section>', 0, 1, 1);
            element('editor').value = '<h1>First edit</h1>';
            listeners['editor:input']();
            window.flushPendingChange();

            element('editor').value = '<h1>Second edit</h1>';
            listeners['editor:input']();
            window.saveConfirmed();

            console.log(JSON.stringify(window.getPendingEditorChange()));
            """
        )
        result = self.run_node(script)
        self.assertTrue(result["dirty"])
        self.assertEqual("<h1>Second edit</h1>", result["html"])

    def test_flush_pending_change_does_not_post_clean_buffer(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const messages = [];
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener() {{}},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener() {{}},
            }};
            global.window.webkit = {{
              messageHandlers: {{
                editor: {{ postMessage(message) {{ messages.push(message); }} }}
              }}
            }};

            eval(source);
            window.setContent('<section class="slide"><h1>Title</h1></section>', 0, 1, 1);
            const flushed = window.flushPendingChange();
            console.log(JSON.stringify({{
              flushed,
              saveMessageCount: messages.filter((message) => message.type === 'editorChanged').length,
              status: element('save-status').textContent,
              pending: window.getPendingEditorChange()
            }}));
            """
        )
        result = self.run_node(script)
        self.assertFalse(result["flushed"])
        self.assertEqual(0, result["saveMessageCount"])
        self.assertEqual("", result["status"])
        self.assertFalse(result["pending"]["dirty"])

    def test_out_of_order_save_confirmation_only_clears_matching_save_id(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const listeners = {{}};
            const messages = [];
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener(event, handler) {{
                  listeners[id + ':' + event] = handler;
                }},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener(event, handler) {{
                listeners['document:' + event] = handler;
              }},
            }};
            global.window.webkit = {{
              messageHandlers: {{
                editor: {{ postMessage(message) {{ messages.push(message); }} }}
              }}
            }};

            eval(source);
            window.setContent('<section class="slide"><h1>Title</h1></section>', 0, 1, 1);
            element('editor').value = '<h1>First edit</h1>';
            listeners['editor:input']();
            window.flushPendingChange();
            const firstSaveId = messages[messages.length - 1].saveId;

            element('editor').value = '<h1>Second edit</h1>';
            listeners['editor:input']();
            window.flushPendingChange();
            const secondSaveId = messages[messages.length - 1].saveId;

            window.saveConfirmed(firstSaveId);
            const afterOldConfirm = window.getPendingEditorChange();

            element('editor').value = '<h1>Second edit</h1>';
            listeners['editor:input']();
            window.flushPendingChange();
            const replacementSaveId = messages[messages.length - 1].saveId;
            window.saveConfirmed(replacementSaveId);
            const afterMatchingConfirm = window.getPendingEditorChange();

            console.log(JSON.stringify({{
              firstSaveId,
              secondSaveId,
              replacementSaveId,
              afterOldConfirm,
              afterMatchingConfirm
            }}));
            """
        )
        result = self.run_node(script)
        self.assertIsInstance(result["firstSaveId"], int)
        self.assertIsInstance(result["secondSaveId"], int)
        self.assertNotEqual(result["firstSaveId"], result["secondSaveId"])
        self.assertTrue(result["afterOldConfirm"]["dirty"])
        self.assertEqual("<h1>Second edit</h1>", result["afterOldConfirm"]["html"])
        self.assertFalse(result["afterMatchingConfirm"]["dirty"])

    def test_late_save_failure_for_confirmed_save_id_is_ignored(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const listeners = {{}};
            const messages = [];
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener(event, handler) {{
                  listeners[id + ':' + event] = handler;
                }},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener(event, handler) {{
                listeners['document:' + event] = handler;
              }},
            }};
            global.window.webkit = {{
              messageHandlers: {{
                editor: {{ postMessage(message) {{ messages.push(message); }} }}
              }}
            }};

            eval(source);
            window.setContent('<section class="slide"><h1>Title</h1></section>', 0, 1, 1);
            element('editor').value = '<h1>Saved edit</h1>';
            listeners['editor:input']();
            window.flushPendingChange();
            const saveId = messages[messages.length - 1].saveId;
            window.saveConfirmed(saveId);
            window.saveFailed(saveId, 'Late failure');
            console.log(JSON.stringify({{
              pending: window.getPendingEditorChange(),
              status: element('save-status').textContent,
              errorText: element('save-error-text').textContent
            }}));
            """
        )
        result = self.run_node(script)
        self.assertFalse(result["pending"]["dirty"])
        self.assertNotEqual("Not saved", result["status"])
        self.assertNotEqual("Late failure", result["errorText"])

    def test_late_save_failure_for_previous_slide_does_not_dirty_current_slide(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const listeners = {{}};
            const messages = [];
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener(event, handler) {{
                  listeners[id + ':' + event] = handler;
                }},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener(event, handler) {{
                listeners['document:' + event] = handler;
              }},
            }};
            global.window.webkit = {{
              messageHandlers: {{
                editor: {{ postMessage(message) {{ messages.push(message); }} }}
              }}
            }};

            eval(source);
            window.setContent('<section class="slide"><h1>Slide 1</h1></section>', 0, 1, 2);
            element('editor').value = '<h1>Edited slide 1</h1>';
            listeners['editor:input']();
            window.flushPendingChange();
            const saveId = messages[messages.length - 1].saveId;

            window.setContent('<section class="slide"><h1>Slide 2 clean</h1></section>', 1, 2, 2);
            window.saveFailed(saveId, 'Late failure for slide 1');

            console.log(JSON.stringify({{
              pending: window.getPendingEditorChange(),
              status: element('save-status').textContent,
              errorText: element('save-error-text').textContent
            }}));
            """
        )
        result = self.run_node(script)
        self.assertFalse(result["pending"]["dirty"])
        self.assertEqual("<h1>Slide 2 clean</h1>", result["pending"]["html"])
        self.assertNotEqual("Not saved", result["status"])
        self.assertNotEqual("Late failure for slide 1", result["errorText"])

    def test_legacy_save_failure_after_slide_switch_does_not_dirty_clean_slide(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const listeners = {{}};
            const messages = [];
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener(event, handler) {{
                  listeners[id + ':' + event] = handler;
                }},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener(event, handler) {{
                listeners['document:' + event] = handler;
              }},
            }};
            global.window.webkit = {{
              messageHandlers: {{
                editor: {{ postMessage(message) {{ messages.push(message); }} }}
              }}
            }};

            eval(source);
            window.setContent('<section class="slide"><h1>Slide 1</h1></section>', 0, 1, 2);
            element('editor').value = '<h1>Edited slide 1</h1>';
            listeners['editor:input']();
            window.flushPendingChange();

            window.setContent('<section class="slide"><h1>Slide 2 clean</h1></section>', 1, 2, 2);
            window.saveFailed('Legacy failure for an earlier save');

            console.log(JSON.stringify({{
              pending: window.getPendingEditorChange(),
              status: element('save-status').textContent,
              errorText: element('save-error-text').textContent
            }}));
            """
        )
        result = self.run_node(script)
        self.assertFalse(result["pending"]["dirty"])
        self.assertEqual("<h1>Slide 2 clean</h1>", result["pending"]["html"])
        # A legacy (id-less) failure is still shown: short status plus the full message.
        self.assertEqual("Not saved", result["status"])
        self.assertEqual("Legacy failure for an earlier save", result["errorText"])

    def test_navigation_flush_can_mark_current_buffer_clean_after_swift_save(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const listeners = {{}};
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener(event, handler) {{
                  listeners[id + ':' + event] = handler;
                }},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener(event, handler) {{
                listeners['document:' + event] = handler;
              }},
            }};

            eval(source);
            window.setContent('<section class="slide"><h1>Slide 1</h1></section>', 0, 1, 1);
            element('editor').value = '<h1>Saved through navigation flush</h1>';
            listeners['editor:input']();
            const pending = window.getPendingEditorChange();
            window.markEditorSaved(pending.index, pending.html);
            window.saveFailed('Late legacy failure after successful Swift save');

            console.log(JSON.stringify({{
              pending: window.getPendingEditorChange(),
              status: element('save-status').textContent,
              errorText: element('save-error-text').textContent
            }}));
            """
        )
        result = self.run_node(script)
        self.assertFalse(result["pending"]["dirty"])
        self.assertEqual("<h1>Saved through navigation flush</h1>", result["pending"]["html"])
        self.assertEqual("Not saved", result["status"])
        self.assertEqual("Late legacy failure after successful Swift save", result["errorText"])

    def test_malformed_refine_state_does_not_enable_controls_or_render_undefined(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                hidden: false,
                disabled: false,
                style: {{}},
                appendChild(child) {{
                  this.children = this.children || [];
                  this.children.push(child);
                }},
                addEventListener() {{}},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              createElement(tag) {{
                return {{
                  tag,
                  textContent: '',
                  title: '',
                  className: '',
                  hidden: false,
                  classList: {{ add() {{}} }},
                  appendChild() {{}},
                }};
              }},
              addEventListener() {{}},
            }};

            eval(source);
            window.prestoRefine.setState({{
              running: 'bad',
              queued: 'bad',
              history: 'bad',
              canUndo: 'yes',
              canRedo: 1,
              lastError: {{ message: 'bad' }},
            }});

            console.log(JSON.stringify({{
              statusHidden: element('refine-status').hidden,
              statusText: element('refine-status-text').textContent,
              errorHidden: element('refine-error').hidden,
              errorText: element('refine-error-text').textContent,
              undoDisabled: element('refine-undo').disabled,
              redoDisabled: element('refine-redo').disabled,
            }}));
            """
        )
        result = self.run_node(script)
        self.assertTrue(result["statusHidden"])
        self.assertEqual("", result["statusText"])
        self.assertTrue(result["errorHidden"])
        self.assertEqual("", result["errorText"])
        self.assertTrue(result["undoDisabled"])
        self.assertTrue(result["redoDisabled"])

    def test_refine_history_invalid_timestamp_renders_blank_time(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            function node(tag) {{
              return {{
                tag,
                textContent: '',
                title: '',
                className: '',
                hidden: false,
                disabled: false,
                innerHTML: '',
                children: [],
                style: {{}},
                classList: {{ add() {{}} }},
                appendChild(child) {{ this.children.push(child); }},
                addEventListener() {{}},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }};
            }}
            function element(id) {{
              return elements[id] || (elements[id] = node(id));
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              createElement: node,
              addEventListener() {{}},
            }};

            eval(source);
            element('refine-history-panel').hidden = false;
            window.prestoRefine.setState({{
              history: [{{
                status: 'done',
                applied: true,
                instructions: 'Improve slide',
                timestamp: 'not-a-date',
              }}],
            }});

            const item = element('refine-history-list').children[0];
            const time = item.children[2];
            console.log(JSON.stringify({{ time: time.textContent }}));
            """
        )
        result = self.run_node(script)
        self.assertEqual("", result["time"])

    def test_tab_with_multiline_selection_indents_lines_without_deleting_selection(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const listeners = {{}};
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener(event, handler) {{
                  listeners[id + ':' + event] = handler;
                }},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener(event, handler) {{
                listeners['document:' + event] = handler;
              }},
            }};

            eval(source);
            window.setContent('<section class="slide">', '<p>One</p>\\n<p>Two</p>', '</section>', 0, 1, 1);
            const editor = element('editor');
            editor.selectionStart = 0;
            editor.selectionEnd = editor.value.length;

            let prevented = false;
            listeners['editor:keydown']({{
              key: 'Tab',
              shiftKey: false,
              preventDefault() {{ prevented = true; }},
            }});

            console.log(JSON.stringify({{
              prevented,
              value: editor.value,
              selectionStart: editor.selectionStart,
              selectionEnd: editor.selectionEnd,
              pending: window.getPendingEditorChange(),
            }}));
            """
        )
        result = self.run_node(script)
        self.assertTrue(result["prevented"])
        self.assertEqual("    <p>One</p>\n    <p>Two</p>", result["value"])
        self.assertEqual(0, result["selectionStart"])
        self.assertEqual(len("    <p>One</p>\n    <p>Two</p>"), result["selectionEnd"])
        self.assertTrue(result["pending"]["dirty"])

    def test_shift_tab_with_multiline_selection_unindents_lines(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const source = fs.readFileSync({str(EDITOR_JS)!r}, 'utf8');

            const elements = {{}};
            const listeners = {{}};
            function element(id) {{
              return elements[id] || (elements[id] = {{
                id,
                value: '',
                textContent: '',
                innerHTML: '',
                className: '',
                style: {{}},
                addEventListener(event, handler) {{
                  listeners[id + ':' + event] = handler;
                }},
                focus() {{}},
                selectionStart: 0,
                selectionEnd: 0,
                scrollTop: 0,
                scrollLeft: 0,
              }});
            }}

            global.window = global;
            global.document = {{
              activeElement: null,
              getElementById: element,
              addEventListener(event, handler) {{
                listeners['document:' + event] = handler;
              }},
            }};

            eval(source);
            window.setContent('<section class="slide">', '    <p>One</p>\\n    <p>Two</p>', '</section>', 0, 1, 1);
            const editor = element('editor');
            editor.selectionStart = 0;
            editor.selectionEnd = editor.value.length;

            let prevented = false;
            listeners['editor:keydown']({{
              key: 'Tab',
              shiftKey: true,
              preventDefault() {{ prevented = true; }},
            }});

            console.log(JSON.stringify({{
              prevented,
              value: editor.value,
              selectionStart: editor.selectionStart,
              selectionEnd: editor.selectionEnd,
              pending: window.getPendingEditorChange(),
            }}));
            """
        )
        result = self.run_node(script)
        self.assertTrue(result["prevented"])
        self.assertEqual("<p>One</p>\n<p>Two</p>", result["value"])
        self.assertEqual(0, result["selectionStart"])
        self.assertEqual(len("<p>One</p>\n<p>Two</p>"), result["selectionEnd"])
        self.assertTrue(result["pending"]["dirty"])


if __name__ == "__main__":
    unittest.main()

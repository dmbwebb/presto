import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EditorTransactionTests(unittest.TestCase):
    def run_editor(self, scenario):
        harness = """
const fs = require('fs');
const vm = require('vm');
const elements = {};
function element(id) {
    return elements[id] || (elements[id] = {
        value: '', textContent: '', innerHTML: '', className: '', style: {},
        selectionStart: 0, selectionEnd: 0, scrollTop: 0, scrollLeft: 0,
        listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; },
        focus() {}
    });
}
global.window = global;
global.document = {getElementById: element, addEventListener() {}, activeElement: null};
global.setTimeout = () => 1;
global.clearTimeout = () => {};
const messages = [];
window.webkit = {messageHandlers: {editor: {postMessage(m) { messages.push(m); }}}};
vm.runInThisContext(fs.readFileSync('presto/editor.js', 'utf8'));
setContent('<section class="slide">', '<h1>Original</h1>', '</section>', 0, 1, 2);
const editor = element('editor');
function type(html) { editor.value = html; editor.listeners.input(); }
"""
        result = subprocess.run(["node", "-e", harness + scenario], cwd=ROOT,
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_delayed_visual_mirror_preserves_new_source_typing(self):
        result = self.run_editor("""
type('<h1>New typing</h1>');
setContent('<section class="slide">', '<h1>Old mirror</h1>', '</section>', 0, 1, 2);
console.log(JSON.stringify(getPendingEditorChange()));
""")
        self.assertEqual(result["html"], "<h1>New typing</h1>")
        self.assertTrue(result["dirty"])

    def test_reading_pending_change_does_not_acknowledge_it(self):
        result = self.run_editor("""
type('<h1>Unsaved</h1>');
const first = getPendingEditorChange();
const second = getPendingEditorChange();
markEditorSaved(0, first.html);
console.log(JSON.stringify({first, second, confirmed: getPendingEditorChange()}));
""")
        self.assertTrue(result["first"]["dirty"])
        self.assertTrue(result["second"]["dirty"])
        self.assertFalse(result["confirmed"]["dirty"])

    def test_save_failure_retains_the_unacknowledged_buffer(self):
        result = self.run_editor("""
type('<h1>Unsaved</h1>');
flushPendingChange();
saveFailed('Disk unavailable');
console.log(JSON.stringify(getPendingEditorChange()));
""")
        self.assertTrue(result["dirty"])

    def test_explicit_disk_reload_can_discard_a_dirty_buffer(self):
        result = self.run_editor("""
type('<h1>Local edit</h1>');
setContent('<section class="slide">', '<h1>External edit</h1>', '</section>', 0, 1, 2, true);
console.log(JSON.stringify(getPendingEditorChange()));
""")
        self.assertFalse(result["dirty"])
        self.assertEqual(result["html"], "<h1>External edit</h1>")

    def test_source_dirty_state_precedes_debounced_save_and_blur_flushes(self):
        result = self.run_editor("""
type('<h1>Unsaved</h1>');
const dirtyNotice = messages[messages.length - 1];
editor.listeners.blur();
const save = messages[messages.length - 1];
markEditorSaved(0, save.html);
console.log(JSON.stringify({dirtyNotice, save, cleanNotice: messages[messages.length - 1]}));
""")
        self.assertEqual(result["dirtyNotice"]["type"], "editorDirty")
        self.assertTrue(result["dirtyNotice"]["dirty"])
        self.assertEqual(result["save"]["type"], "editorChanged")
        self.assertFalse(result["cleanNotice"]["dirty"])


if __name__ == "__main__":
    unittest.main()

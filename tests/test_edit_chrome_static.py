#!/usr/bin/env python3
"""Static checks for the edit chrome and the visual editor's shell-facing API.

Behaviour lives in tests/edit-chrome-tests.html, tests/wysiwyg-redesign-tests.html,
tests/wysiwyg-tests.html and tests/chrome-wysiwyg-e2e.js. These pin the names
that cross the shell boundary (CONTRACT.md) and the light-only rule.
"""
import re
import subprocess
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PRESTO = PROJECT / "presto"
CHROME_JS = PRESTO / "edit-chrome.js"
CHROME_CSS = PRESTO / "edit-chrome.css"
WYSIWYG_JS = PRESTO / "wysiwyg.js"
GUIDES_JS = PRESTO / "wysiwyg-guides.js"

# Dark editor chrome colours that must never come back (light only).
DARK_CHROME = ["#252526", "#3c3c3c", "#1e1e1e", "#202020", "#151515", "#d4d4d4", "#C0582B"]


class EditChromeSourceTests(unittest.TestCase):
    def test_files_exist_and_parse(self):
        for path in (CHROME_JS, WYSIWYG_JS, GUIDES_JS):
            subprocess.run(["node", "--check", str(path)], check=True)
        self.assertTrue(CHROME_CSS.exists())

    def test_edit_chrome_api(self):
        source = CHROME_JS.read_text()
        self.assertIn("window.PrestoEditChrome = {", source)
        for name in ["install:", "rebuild:", "refreshThumbnail:", "setRailVisible:", "goToSlide:",
                     "back:", "setGhostFuture:", "resolveLink:", "followLink:", "closePopover:"]:
            self.assertIn(name, source, f"PrestoEditChrome missing {name}")

    def test_edit_chrome_messages(self):
        source = CHROME_JS.read_text()
        for msg in ["'editChromeReady'", "'sidebarNavigate'", "'ghostFutureChanged'"]:
            self.assertIn(msg, source)
        # Geometry comes from edit-layout.js.
        self.assertIn("'presto:layout'", source)

    def test_prestoedit_api(self):
        source = WYSIWYG_JS.read_text()
        block = source[source.index("window.PrestoEdit = {"):]
        block = block[:block.index("};")]
        for name in ["addTextBox:", "undo:", "redo:", "historyState:", "recordExternalChange:",
                     "refineHistory:", "editEquation:", "handleEscape:"]:
            self.assertIn(name, block, f"PrestoEdit missing {name}")

    def test_wysiwyg_messages(self):
        source = WYSIWYG_JS.read_text()
        for needle in ["type: 'undoState'", "selectionKind:", "sectionHTML:", "expectSectionHTML:",
                       "'ghostFutureChanged'", "origin: 'history'"]:
            self.assertIn(needle, source, f"wysiwyg.js missing {needle}")

    def test_thumbnails_scale_with_transform_not_zoom(self):
        source = CHROME_JS.read_text()
        self.assertIn("canvas.style.transform = 'scale(", source)
        self.assertNotRegex(source, r"\.style\.zoom\s*=")

    def test_light_only(self):
        for path in (CHROME_JS, CHROME_CSS, WYSIWYG_JS, GUIDES_JS):
            text = path.read_text()
            for colour in DARK_CHROME:
                self.assertNotIn(colour.lower(), text.lower(), f"{path.name} still uses dark chrome colour {colour}")

    def test_tokens_have_fallbacks(self):
        # Injected surfaces must work before ui-tokens.css arrives.
        for path in (CHROME_CSS, WYSIWYG_JS, GUIDES_JS):
            text = path.read_text()
            bare = re.findall(r"var\(--p-[a-z0-9-]+\)", text)
            self.assertEqual(bare, [], f"{path.name} uses tokens without fallbacks: {bare[:5]}")

    def test_old_top_right_panel_removed(self):
        source = WYSIWYG_JS.read_text()
        for gone in ["presto-step-visibility", "presto-source-toggle", "presto-pdf-export", "pw-save-dot"]:
            self.assertNotIn(gone, source)


if __name__ == "__main__":
    unittest.main()

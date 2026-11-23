#!/usr/bin/env python3
"""Static wiring tests for the Presto WYSIWYG editing layer.

Behavioral coverage lives in tests/wysiwyg-tests.html (browser),
tests/chrome-wysiwyg-e2e.js (real-input E2E), and
tests/test_presto_features.swift (WKWebView). These tests pin the wiring
between the pieces so a partial refactor cannot silently disconnect them.
"""
import subprocess
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
WYSIWYG_JS = PROJECT / "presto" / "wysiwyg.js"
PRESTO_SWIFT = PROJECT / "presto" / "Presto.swift"
ENGINE_JS = PROJECT / "engine" / "webslides.js"


class WysiwygSourceTests(unittest.TestCase):
    def test_wysiwyg_js_exists_and_parses(self):
        self.assertTrue(WYSIWYG_JS.exists())
        subprocess.run(["node", "--check", str(WYSIWYG_JS)], check=True)

    def test_wysiwyg_js_exposes_public_api(self):
        source = WYSIWYG_JS.read_text()
        for name in [
            "window.__prestoWysiwyg",
            "enable:",
            "saveConfirmed:",
            "saveFailed:",
            "serializeCurrentSlide:",
            "setSourceSidebarVisible:",
            "setPDFExportAvailable:",
            "toggleSourceSidebar",
            "exportPDFRequested",
            "wysiwygChanged",
            "wysiwygEditingState",
            "wysiwygWillEdit",
        ]:
            self.assertIn(name, source, f"wysiwyg.js missing {name}")

    def test_serializer_restores_engine_transforms(self):
        source = WYSIWYG_JS.read_text()
        self.assertIn("data-ws-original-img", source)
        self.assertIn("data-ws-original-html", source)
        self.assertIn("katex", source)
        self.assertIn("ws-visible", source)


class EngineStashTests(unittest.TestCase):
    def test_engine_stashes_original_img_for_inlined_svg(self):
        source = ENGINE_JS.read_text()
        self.assertIn("svg.setAttribute('data-ws-original-img', img.outerHTML)", source)

    def test_engine_stashes_original_table_html(self):
        source = ENGINE_JS.read_text()
        self.assertIn("data-ws-original-html", source)


class SwiftWiringTests(unittest.TestCase):
    def setUp(self):
        self.source = "\n".join(
            (PROJECT / "presto" / name).read_text()
            for name in [
                "Presto.swift",
                "PrestoWindow.swift",
                "PrestoPresent.swift",
                "PrestoInjection.swift",
                "PrestoMenus.swift",
            ]
        )

    def test_editor_preview_injects_wysiwyg_layer(self):
        self.assertIn("injectEditPreview(into: webView, generation: generation)", self.source)
        self.assertIn('read("wysiwyg.js")', self.source)

    def test_editor_preview_injects_guides_module(self):
        self.assertIn("wysiwyg-guides.js", self.source)

    def test_message_handlers_wired(self):
        for needle in [
            'case "wysiwygChanged":',
            'case "wysiwygEditingState":',
            'case "wysiwygWillEdit":',
            'case "toggleSourceSidebar":',
            'case "exportPDFRequested":',
            "handleWysiwygChanged(html:",
            "flushEditorPaneForWysiwyg()",
            "toggleEditorSourceSidebar()",
            "requestPDFExport()",
        ]:
            self.assertIn(needle, self.source, f"Presto.swift missing {needle}")

    def test_keyboard_monitor_gated_by_wysiwyg_state(self):
        self.assertIn("wysiwygTextEditingActive || wysiwygHasSelection", self.source)
        self.assertIn("!eventTargetsEditorWebView(event)", self.source)

    def test_wysiwyg_save_skips_preview_reload(self):
        # A WYSIWYG save must not patch the preview DOM the user is editing.
        self.assertIn(
            "handleEditorChanged(html: html, index: index, reloadPreview: false)",
            self.source,
        )

    def test_wysiwyg_flags_reset_on_session_boundaries(self):
        self.assertGreaterEqual(self.source.count("wysiwygTextEditingActive = false"), 3)


class HarnessRegistrationTests(unittest.TestCase):
    def test_browser_suite_runs_wysiwyg_fixture(self):
        runner = (PROJECT / "tests" / "run_browser_tests.py").read_text()
        self.assertIn("wysiwyg-tests.html", runner)
        self.assertIn("WYSIWYG_TEST_RESULTS", runner)

    def test_nonforeground_suite_runs_wysiwyg_e2e(self):
        script = (PROJECT / "tests" / "run-nonforeground-tests.sh").read_text()
        self.assertIn("chrome-wysiwyg-e2e.js", script)

    def test_e2e_harness_parses(self):
        subprocess.run(
            ["node", "--check", str(PROJECT / "tests" / "chrome-wysiwyg-e2e.js")],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()

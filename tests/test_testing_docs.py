#!/usr/bin/env python3
import unittest
from pathlib import Path


TESTING_DOC = Path(__file__).resolve().parents[1] / "tests" / "TESTING.md"


class TestingDocsTests(unittest.TestCase):
    def test_browser_test_docs_match_automated_runner(self):
        docs = TESTING_DOC.read_text(encoding="utf-8")
        self.assertIn("bash tests/run-tests.sh", docs)
        self.assertIn("bash tests/run-nonforeground-tests.sh", docs)
        self.assertIn("python3 tools/serve.py examples/kitchen-sink --no-open", docs)
        self.assertIn("automated", docs.lower())
        self.assertIn("225 assertions", docs)
        self.assertNotIn("219 assertions", docs)
        self.assertNotIn("214 assertions", docs)
        self.assertNotIn("150 assertions", docs)
        self.assertNotIn("148 assertions", docs)
        self.assertNotIn("147 assertions", docs)
        self.assertNotIn("142 assertions", docs)
        self.assertNotIn("138 assertions", docs)
        self.assertNotIn("122 assertions", docs)
        self.assertIn("225/225 tests passed", docs)
        self.assertNotIn("219/219 tests passed", docs)
        self.assertNotIn("150/150 tests passed", docs)
        self.assertNotIn("148/148 tests passed", docs)
        self.assertIn("engine-layout-tests.html", docs)
        self.assertIn("39/39 passed", docs)
        self.assertIn("24/24 passed", docs)
        self.assertNotIn("29/29 passed", docs)
        self.assertNotIn("147/147 tests passed", docs)
        self.assertNotIn("142/142 tests passed", docs)
        self.assertNotIn("138/138 tests passed", docs)
        self.assertNotIn("125/125 tests passed", docs)
        self.assertNotIn("totalSlides: 21, mainSlides: 19", docs)
        self.assertIn('verify the timer shows "02:00"', docs)
        self.assertNotIn('timer shows "00:02"', docs)
        self.assertIn("TABLE_STEP_TESTS: passed", docs)
        self.assertIn("Layout Geometry", docs)
        self.assertIn("chrome-edit-preview-layout.js", docs)
        self.assertIn("edit and preview layout modes", docs)
        self.assertIn("Presto Edit Transition Performance", docs)
        self.assertIn("presto-edit-transition-perf.js", docs)
        self.assertIn("at least 5x faster", docs)
        self.assertIn("PRESTO_ALLOW_FOREGROUND_TESTS=1 bash tests/run-app-tests.sh", docs)
        self.assertIn("exits with a skip by default", docs)
        self.assertIn("Malformed Deck Structure", docs)
        self.assertIn("malformed-deck-tests.html", docs)
        self.assertIn("malformed-deck-missing.html", docs)
        self.assertIn("malformed-deck-nested.html", docs)
        self.assertIn("malformed-deck-empty.html", docs)
        self.assertIn("malformed-deck-nonsection.html", docs)
        self.assertIn("WebSlides deck structure error", docs)
        self.assertNotIn("66 assertions", docs)

    def test_nonforeground_runner_omits_native_app_e2e(self):
        runner = TESTING_DOC.with_name("run-nonforeground-tests.sh").read_text(encoding="utf-8")
        self.assertIn("Non-foreground verification suite", runner)
        self.assertIn("python3 -m unittest discover", runner)
        self.assertIn("swiftc -framework WebKit -framework Cocoa -framework PDFKit", runner)
        self.assertIn("tests/test_refine_subprocess.swift", runner)
        self.assertIn('"$REFINE_TEST_BINARY"', runner)
        self.assertIn("presto/RefineSubprocess.swift", runner)
        self.assertNotIn("-framework Quartz", runner)
        self.assertIn('TMP_BINARY="$BUILD_DIR/Presto"', runner)
        self.assertIn("bash tests/run-tests.sh", runner)
        self.assertIn("node tests/presto-edit-transition-perf.js", runner)
        self.assertIn("swift tests/test_presto_features.swift", runner)
        self.assertIn("tools/export_pdf.py", runner)
        self.assertNotIn("run-app-tests.sh", runner)
        self.assertNotIn("open \"$APP_BUNDLE\"", runner)
        self.assertNotIn("cliclick", runner)
        self.assertNotIn("System Events", runner)

    def test_nonforeground_runner_compiles_without_publishing_to_app_bundle(self):
        runner = TESTING_DOC.with_name("run-nonforeground-tests.sh").read_text(encoding="utf-8")

        self.assertIn("BUILD_DIR=", runner)
        self.assertIn("TMP_BINARY=", runner)
        self.assertIn('-o "$TMP_BINARY"', runner)
        self.assertIn('test -x "$TMP_BINARY"', runner)
        self.assertNotIn("APP_BINARY=", runner)
        self.assertNotIn('mv "$TMP_BINARY"', runner)
        self.assertNotIn("presto/app/Presto.app/Contents/MacOS/Presto", runner)
        self.assertNotIn("-o presto/app/Presto.app/Contents/MacOS/Presto", runner)


if __name__ == "__main__":
    unittest.main()

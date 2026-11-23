#!/usr/bin/env python3
"""Runs tests/step_morph_check.py (data-morph geometry in Chromium and WebKit, at several
window sizes, with engine scaling and with Presto's CSS zoom) under the WebSlides export venv."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "tools"))
from _common import EXPORT_PYTHON  # noqa: E402

CHECK = TESTS_DIR / "step_morph_check.py"


class StepMorphGeometryTests(unittest.TestCase):
    def test_morph_geometry_in_chromium_and_webkit(self):
        python = Path(os.environ.get("WEBSLIDES_EXPORT_PYTHON", str(EXPORT_PYTHON)))
        if not python.exists():
            self.skipTest(f"WebSlides export venv missing at {python}")
        probe = subprocess.run(
            [str(python), "-c", "from playwright.sync_api import sync_playwright as s\n"
             "with s() as p: p.webkit.launch().close()"],
            capture_output=True, text=True, timeout=120)
        if probe.returncode != 0:
            self.skipTest("Playwright WebKit is not installed for the export venv "
                          f"({python} -m playwright install webkit)")
        result = subprocess.run([str(python), str(CHECK)], capture_output=True, text=True, timeout=600)
        self.assertEqual(0, result.returncode, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()

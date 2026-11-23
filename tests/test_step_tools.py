#!/usr/bin/env python3
"""Tools see engine-resolved reveal ranges for relative steps ("+", "=", "+:N"):
the Beamer export (overlay specs and inline-SVG variants) and the flat PDF reveal state."""
import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "step-model" / "relative-deck.html"
sys.path.insert(0, str(REPO / "tools"))

import export_beamer  # noqa: E402
from _common import DeckServer, wait_for_deck_ready_sync  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


@unittest.skipIf(export_beamer.MISSING, f"missing packages: {export_beamer.MISSING}")
class RelativeStepsBeamerTests(unittest.TestCase):
    def test_overlay_specs_and_svg_variants_use_resolved_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "beamer"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                export_beamer.main([str(FIXTURE), "--output", str(out)])
            tex = (out / "main.tex").read_text(encoding="utf-8")
        self.assertIn(r"\item<2-> First point", tex)
        self.assertIn(r"\item<3-3> Second point", tex)
        self.assertIn(r"\item<4-> Third point", tex)
        # The inlined SVG's groups are "+" (web steps 4 and 5): three variants
        variants = re.findall(r"\\only<([^>]+)>\{\\includegraphics\[[^\]]*\]\{figures/inline_svg_\d+_s\d\}\}", tex)
        self.assertEqual(["1-4", "5-5", "6-"], variants)
        # data-step="=" shares the second dot's step
        self.assertIn(r"\begin{uncoverenv}<6->", tex)


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class RelativeStepsFlatPdfTests(unittest.TestCase):
    def test_flat_reveal_state_hides_ended_relative_ranges_and_clears_fades(self):
        from export_pdf import FLAT_REVEAL_JS

        with DeckServer(FIXTURE) as served, sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 720})
                page.goto(served.url)
                wait_for_deck_ready_sync(page)
                page.evaluate("WS.goToSlide(0, 'end')")
                faded_before = page.evaluate("document.querySelectorAll('.ws-faded').length")
                page.evaluate(FLAT_REVEAL_JS)
                state = page.evaluate("""() => ({
                    second: getComputedStyle(document.getElementById('second')).visibility,
                    third: getComputedStyle(document.getElementById('third')).visibility,
                    faded: document.querySelectorAll('.ws-faded').length,
                })""")
            finally:
                browser.close()
        self.assertEqual(1, faded_before)
        self.assertEqual({"second": "hidden", "third": "visible", "faded": 0}, state)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Smoke test: export the tools fixture deck to Beamer without compiling."""
import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "tools-deck" / "index.html"
sys.path.insert(0, str(REPO / "tools"))

import export_beamer  # noqa: E402


@unittest.skipIf(export_beamer.MISSING, f"missing packages: {export_beamer.MISSING}")
class ExportBeamerSmokeTests(unittest.TestCase):
    def test_fixture_deck_exports_one_frame_per_slide_with_svg_overlays(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "beamer"
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                export_beamer.main([str(FIXTURE), "--output", str(out)])
            tex = (out / "main.tex").read_text(encoding="utf-8")
            report = (out / "_review" / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("WARNING: Deck did not become ready", stderr.getvalue())
        self.assertEqual(16, tex.count(r"\begin{frame}"))
        # The inlined SVG's three reveal steps become four overlay variants.
        variants = re.findall(r"\\only<[^>]+>\{\\includegraphics\[[^\]]*\]\{figures/inline_svg_\d+_s\d\}\}", tex)
        self.assertEqual(4, len(variants))
        self.assertIn("steps=3", report)


if __name__ == "__main__":
    unittest.main()

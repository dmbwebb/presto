#!/usr/bin/env python3
"""Regression check for tools/export_pptx.py on a small synthetic deck.

    ~/.venvs/webslides-export/bin/python tests/pptx_export_check.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from pptx import Presentation

REPO = Path(__file__).resolve().parent.parent

DECK = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Check</title>
<link rel="stylesheet" href="engine/webslides.css"><link rel="stylesheet" href="engine/themes/academic.css"></head><body>
<div class="ws-deck">
<section class="slide content" id="one"><h2>Reveals</h2>
  <ul><li>Always shown</li><li data-step="1">First click</li><li data-step="2">Second click</li></ul>
  <p data-step="3">A paragraph on the third click</p>
  <aside class="notes">Speaker note</aside>
</section>
<section class="slide content" id="two"><h2>Native table</h2>
  <table><thead><tr><th>Arm</th><th>Effect</th></tr></thead><tbody><tr><td>A</td><td>0.10</td></tr><tr><td>B</td><td>0.20</td></tr></tbody></table>
  <a class="ws-appendix-link" href="#one">Back to one</a>
</section>
<section class="slide content" id="three"><h2>Plot</h2>
  <svg viewBox="0 0 600 200" width="600" height="200">
    <line x1="100" y1="10" x2="100" y2="150" stroke="#888" stroke-width="2"/>
    <g data-step="1">
      <line x1="120" y1="40" x2="220" y2="40" stroke="#CF7A30" stroke-width="4"/><circle cx="170" cy="40" r="8" fill="#CF7A30"/>
      <text x="230" y="46" font-size="18" fill="#CF7A30">0.35</text>
      <line x1="150" y1="90" x2="270" y2="90" stroke="#3F74B5" stroke-width="4"/><circle cx="210" cy="90" r="8" fill="#3F74B5"/>
      <text x="280" y="96" font-size="18" fill="#3F74B5">0.55</text>
    </g>
    <text x="100" y="180" font-size="16" text-anchor="middle">0</text><text x="300" y="180" font-size="16" text-anchor="middle">1</text>
  </svg>
</section>
</div><script src="engine/webslides.js"></script></body></html>"""


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "engine").symlink_to(REPO / "engine")
        (root / "index.html").write_text(DECK, encoding="utf-8")
        output = root / "out.pptx"
        subprocess.run([sys.executable, str(REPO / "tools" / "export_pptx.py"), str(root / "index.html"), "--output", str(output)], check=True)
        prs = Presentation(output)
        slides = list(prs.slides)
        assert len(slides) == 3, f"expected one PowerPoint slide per deck slide, got {len(slides)}"
        for slide in slides:
            assert slide.shapes.title is not None and slide.shapes.title.text_frame.text, "every slide needs a real title"
            assert not any(shape.shape_type == 13 for shape in slide.shapes), "no slide may be a picture"
        first = slides[0]._element.xml
        assert first.count('nodeType="clickEffect"') == 3, "three reveal steps must be three clicks"
        assert "<p:pRg st=" in first, "list items must reveal paragraph by paragraph"
        assert slides[0].notes_slide.notes_text_frame.text == "Speaker note"
        assert any(getattr(s, "has_table", False) for s in slides[1].shapes), "tables must be native"
        assert "hlinksldjump" in slides[1]._element.xml, "in-deck links must survive"
        charts = [s for s in slides[2].shapes if getattr(s, "has_chart", False)]
        assert len(charts) == 1, "the dot-and-whisker marks must become one native chart"
        assert charts[0].chart.part.chart_workbook.xlsx_part is not None, "charts need an editable data sheet"
    print("pptx export check passed")


if __name__ == "__main__":
    main()

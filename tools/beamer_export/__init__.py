"""WebSlides -> LaTeX Beamer exporter (package used by tools/export_beamer.py).

Modules:
- extract.js: browser-side DOM walk (run inside the deck page via Playwright)
- emitter.py: semantic JSON tree -> Beamer LaTeX
- tables.py:  HTML table -> booktabs / nicematrix, Overleaf .tex table loader
- assets.py:  figures (copy / SVG->PDF), table files, screenshots
- review.py:  report.md, contact sheet, README.md, LaTeX log parsing
- texutil.py: escaping, Unicode, colours, units

Interpreter: ~/.venvs/webslides-export/bin/python (playwright + chromium, beautifulsoup4,
lxml, pillow, pypdf). LaTeX: TinyTeX/TeX Live with beamer, lato, libertinus-type1,
tcolorbox, nicematrix, adjustbox, appendixnumberbeamer, siunitx, booktabs, tikz.
"""

__version__ = '0.2.0'

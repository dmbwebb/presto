# WebSlides → Beamer Export

Use this when a coauthor needs an editable LaTeX version of a web deck, or when
debugging `tools/export_beamer.py`. The export is one-way: regenerate from the
HTML when the web deck changes; nothing flows back.

## Commands

```bash
# skill wrapper (resolves aliases, runs the export venv, passes the deck's tex_dir)
python3 ~/.claude/skills/webslides/scripts/export_beamer.py <deck-alias> --output /path/to/out --compile --zip

# engine tool directly
~/.venvs/webslides-export/bin/python <engine>/tools/export_beamer.py \
    /path/to/deck/index.html --output /path/to/out --tex-dir "/path/to/paper tables" --compile --zip
```

Flags: `--tables auto|tex|html` (auto = use the paper's `.tex` when a
same-basename file exists AND its column count matches the slide table),
`--slides 1-28,40`, `--raster-slides 5,9` (force the screenshot fallback),
`--shots final|all|needed`, `--scale N`. Exit code 2 on any error.

Output dir: `main.tex`, `figures/`, `tables/`, `README.md` (the Overleaf
project; pdflatex twice), `main.pdf` (with `--compile`), `_review/` (report.md,
tree.json, page PNGs, web screenshots, `contact_sheet_N.png`). `--zip` writes
`<out>/../<deckname>_overleaf.zip` with only the project files. Write the output
OUTSIDE the deck folder (the tool refuses a path inside it), for example to
`presentations/beamer_exports/<deck>/` beside the `webslides/` folder.

Environment: venv `~/.venvs/webslides-export` (Python 3.12: playwright 1.60 +
chromium, beautifulsoup4, lxml, pypdf, pillow); TinyTeX with beamer, lato,
tcolorbox, nicematrix, adjustbox, appendixnumberbeamer, libertinus-type1
(`tlmgr update --self` first if `tlmgr install` refuses). `pdftoppm` from
`~/.local/bin` for the contact sheets.

## How it maps (so you can predict the output)

- Headless Chromium renders the deck; every slide is extracted at its last
  step with computed styles and rects (`beamer_export/extract.js`). The browser
  is the CSS engine, so deck-specific classes and inline styles are honoured
  automatically; 1px = 0.354pt (1280×720 ≙ 160×90mm).
- Title slide, section/subsection headers (with the engine's TOC), `h2` →
  `\frametitle` with the accent rule, footer with `author · venue` and frame
  numbers, `\insertsectionnavigationhorizontal` for the top nav.
- `ul/ol/li` → itemize/enumerate; `data-step="n"` → `\item<n+1->`, blocks →
  `uncoverenv`, closed ranges (`"a-b"`) → `\only`; stacked reveal groups
  (elements sharing a grid cell) → `overlayarea` + `\only`; `data-dim` → grey
  text/opacity; `data-highlight` → background swap (no glow).
- `.columns`, `.table-commentary`, and any grid/flex-row container → beamer
  `columns` with measured widths; `.box*`, `.treatment-arm`, `.toc-node` and
  other bg/border blocks → `tcolorbox`; absolutely positioned items
  (`.header-links`, `.ws-back-link`, popups, cover photos) → tikz overlay nodes
  at exact slide coordinates.
- Images: PNG/JPEG copied; SVG (incl. inlined `data-inline` SVGs with internal
  steps) printed to vector PDF by Chromium, one variant per visibility state.
- Tables: `\input{tables/<name>.tex}` inside `adjustbox`; from the paper's
  `.tex` when it matches, else HTML → booktabs. Tables carrying
  `data-overlays` become `NiceTabular` with highlight boxes drawn in
  `\CodeAfter` and are written per slide (`table1a_slide18.tex`).
- `a.ws-appendix-link` → `\hyperlink{<id>}{\wsbutton{...}}` pills; each frame
  is `label=<slide id>`; `a.ws-back-link` → hyperlink to the first referring
  slide's last overlay (Beamer has no return stack).
- Fonts: Lato (pdflatex `lato` package) for body and headings (the theme's
  default since 2026-10); a deck that sets a serif heading font gets
  Libertinus; tables and pills follow the computed font (Lato by default).
- Fallback: `--raster-slides` (or automatic on an emitter error / empty text)
  includes the slide screenshots, one per step, in a `[plain]` frame.

## Known limitations

- Line breaking differs from the browser (TeX metrics, substituted serif), so
  a long heading can wrap and push content off the frame: read the "Overfull
  vbox" list in `_review/report.md`, not just the error count.
- Not reproduced: box shadows, gradients, per-corner radii, letter-spacing,
  transitions, hover, `<video>`/`<iframe>` (an iframe of a local table page is
  converted; anything else becomes a placeholder).
- Legible pill buttons are slightly larger than the web's, so pill rows can
  wrap one line earlier.
- An `<ol data-reveal="each">` came out with no item numbers (known bug, not
  yet fixed): use `<ul>` or check numbered lists on the contact sheet.
- Editing is easy (text, `\textbf`, colours, adding items reflow), but the
  `.tex` carries explicit `\wsfs{size}{leading}` and `\vspace` values copied
  from the web layout; restructuring a slide is manual.
- Blank or broken images in the source deck are reproduced faithfully (CSAE
  slides 22/24/25 carry blank PNGs left over from a PowerPoint import).

## Review checklist before sharing

1. `_review/report.md`: compile OK, Overfull vbox frames, raster fallbacks,
   table sources (`.tex` vs HTML), constructs listed as lost.
2. Open every `_review/contact_sheet_N.png` (10 slides per sheet, web left,
   Beamer right) and look for missing text, wrong order, overflow.
3. Click a few appendix pills and Back buttons in `main.pdf`.
4. Upload the `_overleaf.zip` to Overleaf (New Project → Upload Project) and
   share that project; send the PDF too for presenting.

## Gotchas (LaTeX/Beamer/Playwright)

- Beamer `overprint` breaks when a part contains `\item<spec>` (the item's own
  `\onslide` is read as a part delimiter): use `overlayarea` + `\only`.
- `\usebeamerfont*` resets to the class `\normalsize`, so lists silently drop
  to 10/12pt: redefine `\normalsize` (and friends) to the deck base;
  `\itemsep` must be set in `\@listi`/`\@listii`, not in the itemize template.
- Beamer `column` and tcolorbox reset `\rightskip`: re-assert `\raggedright`
  (the web never justifies). tcolorbox `parbox=false` inside frames errors
  with `\thetcolorboxnumber`; `[skins]` suffices, `[most]` needs tikzfill.
- Absolutely positioned overlays must be emitted in
  `\par\nointerlineskip\hbox{...}`; separate paragraphs each add a blank line
  (phantom overflow). In a beamer column `\vfill` does nothing; centre a flex
  column with a fixed-height `\begin{minipage}[c][H][c]`.
- An in-flow `\includegraphics` of full slide height overflows a `[t]` frame
  (text area is 252pt): cap flow images at the text height; cover-fit images
  go through the clipped tikz overlay whatever their computed display.
- Highlight boxes on scaled tables: `NiceTabular` + `\CodeAfter` survives
  `adjustbox` scaling and `\only`; tikzmark `remember picture` marks do not.
  A table used on two slides with different `data-overlays` must be written
  per slide or the second slide's `\only<3->` adds phantom pages to the first.
- pdflatex console output is 8-bit: `subprocess.run(..., text=True)` raises
  UnicodeDecodeError on `·`; pass `errors='replace'`.
- Overleaf `.tex` tables can diverge from the slide HTML (table1a.tex has 7
  columns vs 5 on the slide; hr_table.tex different numbers): hence the
  column-count check in `--tables auto`.
- Flex/grid children are blockified in computed style; treat inline tags
  inside flex rows of links (`.footnote`, `.header-links`) as inline, but keep
  grid children as cells. `getComputedStyle` cannot reveal `margin-top:auto`;
  scan matched CSS rules. `.katex-mathml` holds a hidden duplicate of every
  formula; read the TeX from its annotation and skip the rest.
- Chromium `page.pdf()` of an SVG with `@page{size:<viewBox>}` gives real
  vector output; element screenshots include the painted background, so
  transparent layers need a wrapper page with `omit_background`.
- bs4 `decompose()` leaves descendant Tags with `attrs=None`.

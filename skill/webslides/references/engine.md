# WebSlides Engine And Tools

Use this for validation, serving, export, Presto, and engine-level debugging.

## Engine Repo

`<engine>`: `$WEBSLIDES_HOME`, otherwise the path printed by
`python3 ~/.claude/skills/webslides/scripts/_engine.py` (see SKILL.md § Locations).

Key paths:

- `engine/webslides.js` — navigation, reveals, appendix, numbering, section nav, mode API
- `engine/webslides.css` — core layout, scaling, print, mode styles
- `engine/themes/academic.css` — default academic theme and reusable slide layouts
- `tools/` — serve, validate, export, screenshot, table conversion, PDF/SVG helpers
- `presto/` — Presto, the macOS presenter/editor app (Decks, Edit, Present in one window)
- `examples/kitchen-sink/` — reference deck with features

## Commands

Serve a deck:

```bash
python3 <engine>/tools/serve.py /path/to/deck --no-open
```

Without `--no-open` it opens a browser window on the user's screen.

Validate links and file references:

```bash
python3 <engine>/tools/validate_links.py /path/to/deck/index.html
```

Check overflow:

```bash
python3 <engine>/tools/check_overflow.py /path/to/deck/index.html --all --strict
```

Check style (the slide rules in `references/style.md`; errors exit 1, `--strict` makes warnings fail):

```bash
python3 <engine>/tools/check_style.py /path/to/deck/index.html
python3 <engine>/tools/check_style.py /path/to/deck --slides 3,7-9,appendix-id --json
```

Errors: text below 14px (effective size after table scaling, CSS transforms
and SVG viewBox scaling), text clipped inside an overflow:hidden box, content
over the footer text or slide number, failed table/SVG loads (`WS.loadErrors`),
`.katex-error`, content under the pinned appendix buttons (`.header-links`) or
back link (`.ws-back-link`) in the bottom-right corner (fix by shortening or
moving the content up, or moving the buttons). The engine's failed-SVG error
box is reported once as a load error, never as small text. Warnings: main-slide text at 14-20px outside `.footnote, .cite,
.source, .table-note, sup, sub` and appendix/header links; appendix text below
14px; more than 70 words visible at once; bullets over 20 words; acronyms used
before being spelled out (built-in list of common ones; add deck-specific ones
with `<div class="ws-deck" data-known-acronyms="CSAE, MHM">`); top-heavy content
slides (bottom 40% of the space under the title empty; title, outline, section,
thanks, photo and split-image slides are exempt); raster images shown at
under 1.5 image pixels per slide pixel; decks that shrink fonts with their own
scripts. Notes: declared `data-steps` (ignored; delete it). The same footer
overlap on three or more slides is reported once, deck-wide (a theme problem).

Every tool works as `python3 tools/<tool>.py`: tools that need Playwright
(check_overflow, check_style, export_pdf, screenshot_slide, export_beamer)
re-run themselves under `~/.venvs/webslides-export` when the current
interpreter lacks it, and print the rebuild command if that venv is missing
(override the venv with `WEBSLIDES_EXPORT_PYTHON`). The pure-Python tools run
under system python3 3.9. The skill wrappers do the same.

Do not use `~/.venvs/webslides-tools`: it is Python 3.9 and fails on the
tools' `int | None` annotations. Rebuild the export venv if missing:
`~/.local/bin/python3.12 -m venv ~/.venvs/webslides-export && ~/.venvs/webslides-export/bin/pip install playwright beautifulsoup4 lxml pypdf pillow`
(then `~/.venvs/webslides-export/bin/python -m playwright install chromium` if the browser cache is missing).

Run links, overflow and style in one go via the skill wrapper:

```bash
python3 ~/.claude/skills/webslides/scripts/validate_deck.py <deck-name-or-index>
```

Fast single-slide loop (`tools/ws.py`; the skill's `scripts/ws.py` forwards to it).
Slides are deck positions (appendix included), ranges, ids (`id`, else `data-id`,
else title slug; printed by `ws outline`), or a unique title substring.

- `ws check DECK --slides N [--steps 3,4|all|last] [--diff] [--pdf]` runs every
  step of the chosen slides through the link, overflow, style and layout checks
  (one line per problem, then one summary line) and captures the `--steps`
  (default `last`) at scale 1 in `/tmp/ws/<deck-key>/`. Each capture becomes the
  baseline in `~/.cache/webslides/shots/<deck-key>/<slide-id>/`; `--diff` (or
  `ws diff`) prints `no visual change` or writes a crop of the changed region
  (`--before` stacks old over new); the first run writes the full image.
- `ws layout DECK N [--step S] [--marks] [--json]` reports overlapping text (HTML,
  KaTeX, SVG `<text>`, also across inlined SVGs), text outside the slide or
  clipped by a box or SVG edge, overflow and small text; `--marks` adds SVG text
  over small data marks.
- `ws get DECK N [--out f]` prints the section as in the file with its line range
  and a hash; `ws put DECK N f --expect-hash H` replaces only that section
  (atomic; refused if that section changed since `get`).
- `ws steps DECK N insert-after K | delete K [--merge prev] | swap A B [--dry-run]`
  renumbers one slide's transitions (`data-step`, `data-highlight`, `data-dim`,
  `data-class` suffixes, `data-fade-after`, `data-focus` ranges,
  `data-reveal-start`) and prints a before/after map. `+`/`=`/`data-reveal`
  values are left alone and reported if they drift. Inlined SVGs and tables
  change only with `--include-svg` (`--html-only` accepts the mismatch); it names
  the generator script to edit when an SVG is generated.

Stats: `python3 tools/check_stats.py deck/index.html` lists every `data-stat` /
`\stat{}` with its value and slides, flags MISSING (exit 1) and placeholders that
differ from the files; `--json`; `--stats-dir DIR` for a deck that declares none.
Run it after every paper refresh. `WS.stats` holds filled values; `WS.loadErrors`
types now include `stat` and `math`. Tools serve from a wider root when the stats
source sits outside the deck tree (`serve.py` serves it but does not watch it).

List slides:

```bash
python3 <engine>/tools/get_slide.py /path/to/deck/index.html --list
```

Screenshot a slide:

```bash
python3 <engine>/tools/screenshot_slide.py /path/to/deck/index.html 12 --step 0
python3 <engine>/tools/screenshot_slide.py /path/to/deck 3 7 9-12 --final-only --contact-sheet /tmp/sheet.png
```

Screenshots load the full deck, so they show the real footer, slide number,
section nav and JS-drawn charts, and step counts come from the engine.

Export PDF:

```bash
python3 <engine>/tools/export_pdf.py /path/to/deck/index.html --output talk.pdf
python3 <engine>/tools/export_pdf.py /path/to/deck/index.html --flat --output talk-flat.pdf
```

Export to Beamer (an Overleaf-ready LaTeX project, for coauthors who edit in
LaTeX rather than WebSlides):

```bash
python3 ~/.claude/skills/webslides/scripts/export_beamer.py <deck-alias> --output /path/to/out --compile --zip
python3 ~/.claude/skills/webslides/scripts/export_beamer.py <deck-alias> --output out --compile --slides 1-28 --raster-slides 14
```

The wrapper runs `tools/export_beamer.py` under `~/.venvs/webslides-export`
(Playwright + bs4 + lxml + pypdf + Pillow; Python 3.12) and passes the deck's
`tex_dir` from `deck_locations.yaml` as `--tex-dir` (the folder holding the
paper's original `table*.tex`, reused when a slide table has a same-basename
`.tex` with matching columns). Output: `main.tex`, `figures/`, `tables/`,
`README.md` (the Overleaf project), `main.pdf`, and `_review/` (report.md with
per-slide coverage and Overfull-vbox overflow list, page PNGs, web-vs-Beamer
contact sheet); `--zip` writes `<deckname>_overleaf.zip` beside the output dir
containing only the project files. Details, mapping rules, and gotchas:
`references/beamer_export.md`.

PDF/SVG conversion:

```bash
python3 <engine>/tools/pdf2svg.py input.pdf output.svg
python3 <engine>/tools/pdf2svg.py figures/*.svg
```

## Presto

Presto is the macOS presenter/editor app: one window with three modes, Decks,
Edit and Present (⌘1, ⌘2, ⌘3), light only.

```bash
bash <engine>/presto/app/install.sh
open -a Presto                                        # Decks
open -a Presto --args /path/to/deck/index.html        # Present, ready (Start or ⌘↩ goes live)
open -a Presto --args --edit /path/to/deck/index.html # Edit, at the deck's last position
open -a Presto --args --play /path/to/deck/index.html # live at once from slide 1
bash <engine>/tests/run-nonforeground-tests.sh
PRESTO_ALLOW_FOREGROUND_TESTS=1 bash <engine>/tests/run-app-tests.sh
```

Use the non-foreground suite as the default gate. The full app runner takes
keyboard focus and deliberately requires `PRESTO_ALLOW_FOREGROUND_TESTS=1`.
Crash-recovery, dual-screen simulation, and longer soak commands are documented
in the repo's `tests/TESTING.md`; the design is described in `presto/DESIGN.md`.

Decks mode lists recent decks, the decks in this skill's
`references/deck_locations.yaml` (read only) and folders the user adds, grouped
by project; Continue reopens the last deck at the slide and step where it was left.
Present opens the presenter console ready, with a Start button and the talk
length, which Presto stores per deck in its own settings, never in the deck
file. With a projector the audience sees only the slide and the laptop shows
the console (current slide, timer, pace, sections; no speaker notes and no
next-slide preview); with one screen, Start makes the window full screen with
a small control bar that appears when the pointer moves. Ending a talk returns
to Edit or Decks at the slide and step it ended on.

Speaker notes live in `<aside class="notes">` inside a slide and are readable
through `WS.getNotes(index)`; Presto does not show them.

Present mode: Escape first closes the slide grid if it is open, and returns
from an appendix with one press; otherwise it ends the talk only on a second
press within 2 seconds (a light "Press Escape again to end the presentation"
toast shows in the console, or on the slide with one screen). While live, ⌘Q
also needs a second press. Clicking an appendix or back button, or choosing a
slide in the overview, in the console's slide moves the audience too, and
appendix/back buttons clicked in the audience window keep the return stack.

Presto lays every slide out with `presto/edit-layout.js` (`PrestoLayout`):
the section nav and footer sit on the slide's own top and bottom edges at the
slide's width in edit, audience and preview modes, and the letterbox is the
slide's background. In the edit preview Enter and Backspace never step the
slide. Appendix slides highlight no section in the section nav (they used to
highlight the last one).

## Validation Rule

After editing any deck, run all three (or `scripts/validate_deck.py`, which runs them):

```bash
python3 <engine>/tools/validate_links.py /path/to/deck/index.html
python3 <engine>/tools/check_overflow.py /path/to/deck/index.html --all --strict
python3 <engine>/tools/check_style.py /path/to/deck/index.html
```

After engine or theme changes, sweep every deck in `deck_locations.yaml` for
regressions with `python3 tools/compare_engines.py --base-sha <base-sha> DECK ...`
(aliases work; `--new-engine DIR` for a worktree's engine). It serves each real
deck read-only, routes its engine requests to the base or new engine, and
pixel-compares every step, saving differing pairs; nothing is copied or edited.
Rerun a deck with a few-pixel difference once, since capture noise does not
repeat. For intended visual changes, also diff the error lists from
`check_style.py --json` and `check_overflow.py --all` (same finding can reword
when element counts or sizes change). Theme layout changes tend to hit decks
whose CSS leaned on the old behaviour.

After broad structural edits, also run:

```bash
python3 <engine>/tools/get_slide.py /path/to/deck/index.html --list
```

## Engine API

Functions exposed on `window.WS` after init:

- `WS.goToSlide(index, step)`, `WS.nextStep()`, `WS.prevStep()`, `WS.nextSlide()`, `WS.prevSlide()`, `WS.refitTables()`
- `WS.goToAppendix(id)`, `WS.returnFromAppendix()` (alias `WS.goBack()`), `WS.toggleOverview()`
- `WS.getState()` returns slide, step, appendix, and numbering state
- `WS.getNotes(slideIndex)` returns speaker notes HTML
- `WS.on('navigate', callback)` registers navigation listeners
- `WS.slideSteps.get(slideElement)` is the authoritative max step of a slide (a
  Map keyed by slide element; the section's own `data-steps` attribute is ignored)
- `WS.parseRevealRange(str)` parses a reveal range exactly as the engine does:
  `"3"` and `"3+"` give `{start: 3, end: Infinity}`, `"2-4"` gives `{start: 2, end: 4}`,
  empty gives `{start: 1, end: Infinity}`, anything malformed (including `"3-"`) gives `null`
- `WS.applyRevealShortcuts(slideElement)` expands `data-reveal="each"` lists on a slide
  (the engine does this itself at init; Presto calls it after replacing a slide)
- `WS.refreshSlideSteps(slideOrIndex)` re-resolves a slide whose DOM changed in
  place (editor undo/redo, a rewritten `data-step`): regenerates `data-reveal="each"`
  steps, re-resolves relative steps, fades and focus, updates `WS.slideSteps`,
  re-applies the current step (clamped) and fires `navigate` if the step or the
  count changed; idempotent; returns the new max step or `null`. Explicit steps
  set on a generated list item must drop its `data-ws-auto-step` marker.
- `WS.getExportInfo()`, `WS.getExportLinks()`, `WS.prepareForExport()` serve the export tools
- `WS.getRevealRange(el, attr)` returns the engine-resolved range of an element's
  `data-step` (default), `fade-after`, `highlight`, `dim`, `class` (adds `className`)
  or `focus` (array with `selector`); `end` is Infinity when open. Tools use this
  (`_common.wsElRange`) instead of parsing, since `+`/`=` need slide context.
  `WS.parseRevealRange("+")` is `null` (no slide context).
- `WS.describeSteps(i)` returns a per-step summary (appear, disappear, fade, unfade,
  focus, unfocus, morph, classes, highlight, dim, plus warnings); `ws outline --steps`
  prints it
- Morphs are Web Animations started only by `WS.nextStep()`/`WS.prevStep()`;
  `goToSlide` and `prepareForExport` never animate

Readiness: `WS.ready` is a Promise that resolves to `WS` (it never rejects) once
inline SVGs are inlined, `data-table-src` tables are loaded, math-rendered, fitted
and overlaid, `document.fonts.ready` has settled (bounded at 5 s), images beside
tables have loaded, and step counts are final. At that moment the engine also sets
`<html data-ws-ready="1">`. Tools and tests wait on it rather than on timers
(all tools do, via `tools/_common.py`, falling back to polling on older engines).

Load errors: `WS.loadErrors` lists failed loads as
`{type: "table" | "svg", src, detail, slideIndex}`. A failed table shows
"Failed to load table: ..." in its container; a failed `<img data-inline>` SVG is
replaced by a red dashed box reading "Could not load SVG: <src> (<detail>)" that
keeps the authored `<img>` in `data-ws-original-img`, so a Presto save writes the
original tag back.

Modes:

- `normal` — ordinary keyboard/click navigation. A click on a slide advances one
  step, except on links, buttons, form controls, `.appendix-links`,
  `.header-links` and `[data-ws-link]` widgets (labels and gaps included). The
  mode is checked per click, so a host that sets `WS.mode` after load (Presto's
  audience window does) turns click-advance off.
- `audience` — external-display mode, no local navigation
- `preview` — thumbnail mode, no footer/progress

URL params: `?slide=N&step=S` for initial navigation.

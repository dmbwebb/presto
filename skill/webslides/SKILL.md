---
name: webslides
description: |
  Create and edit web-based academic presentations using the WebSlides engine.
  Use when: (1) Creating new slide decks, (2) Converting Beamer/PPTX to web slides,
  (3) Editing existing WebSlides decks. Triggers: "webslides", "web slides",
  "html slides", "web presentation", "convert to webslides".
---

# WebSlides

If `references/local.md` exists, read it first: it holds this machine's paths and the presenter's own details and preferences, and it overrides the general guidance below where they differ.

Web-based academic presentation workflow. The deck source is HTML; the central
WebSlides repo provides the engine and tools; real research decks should live in
their project directories.

## Progressive Disclosure

Load only the references needed for the task:

- `references/style.md` — applied-micro talk structure, slide style, bullet rules, typography, reveal preferences
- `references/authoring.md` — WebSlides HTML patterns, title slides, section nav, figure/table layouts, appendix buttons, progressive reveal syntax
- `references/engine.md` — commands, validation, export, dev server, Presto, tool behavior
- `references/gotchas.md` — operational mistakes and engine/deck gotchas
- `references/beamer_export.md` — WebSlides→Beamer export: mapping rules, limitations, review checklist, LaTeX/Beamer gotchas
- `references/pptx_export.md` — WebSlides→editable PowerPoint: what becomes what, limits, how to verify in real PowerPoint
- `references/deck_locations.yaml` — your deck aliases and project-owned paths (personal; start from `references/deck_locations.example.yaml`)

For ordinary edits, start with this file plus `scripts/locate_deck.py`. Read
`style.md` for content rewrites, `authoring.md` for slide markup/reveals, and
`engine.md` only for tooling/debugging.

## Locations

- `<engine>` — the WebSlides engine repo (the folder containing `engine/webslides.js`): `$WEBSLIDES_HOME` if set, otherwise the path printed by `python3 ~/.claude/skills/webslides/scripts/_engine.py` (which also reads `scripts/local.json` and, when the skill is installed from the repo's `skill/webslides/`, finds the repo itself). Commands below that start with `<engine>/` need this path substituted.
- Skill docs: `~/.claude/skills/webslides`
- Deck manifest: `references/deck_locations.yaml` (optional; without it, pass deck folders or `index.html` paths instead of aliases)

Resolve named decks before editing:

```bash
python3 ~/.claude/skills/webslides/scripts/locate_deck.py <deck-alias>
```

## Core Workflow

1. Locate the deck with `scripts/locate_deck.py` or use the explicit path from the user.
2. Read the deck's `index.html` and any relevant project instructions.
3. Load the smallest relevant reference doc from `references/`.
4. Edit the deck, keeping reusable layouts in the theme rather than one-off inline CSS when possible.
5. Ensure the final non-appendix slide is a simple `Thank you!` slide with the presenter's email.
   For edits to a few slides, use `ws` (`scripts/ws.py`, DECK may be an alias) instead of grepping `index.html` and re-running the full suite after every tweak:
   ```bash
   WS=~/.claude/skills/webslides/scripts/ws.py
   python3 $WS outline DECK [--steps]                 # N #id [0-max] title; --steps: one line per step (+shown -hidden ~faded)
   python3 $WS get DECK 12 --out /tmp/s12.html        # the <section> as in the file, line range and hash
   python3 $WS put DECK 12 /tmp/s12.html --expect-hash H
   python3 $WS check DECK --slides 12 --steps all --diff   # links, overflow, style, layout on those slides (~1.5 s); crops of what changed
   python3 $WS layout DECK 12 --step 3                # overlapping or clipped text, small text, including SVG <text>
   python3 $WS steps DECK 12 insert-after 2 --dry-run # renumber one slide's steps (delete N, swap A B); names the figure generator if an SVG must change
   ```
   Slides are addressed by number, stable `#id` (survives renumbering), or a unique title substring. Read only the diff crops `ws check --diff` writes; "no visual change" needs no image. Prefer `data-step="+"`/`"="`, `data-fade-after` and `data-focus` (`references/authoring.md` § Progressive Reveal) over hand-numbered steps, so adding a step never means renumbering. Export the PDF once at the end (`ws check --pdf` or `scripts/export_pdf.py`), not after each edit.
6. After broad structural edits, list/inspect slides with `scripts/get_slide.py` (deck-alias-aware wrapper around the engine tool):
   ```bash
   python3 ~/.claude/skills/webslides/scripts/get_slide.py <deck-alias> --list
   python3 ~/.claude/skills/webslides/scripts/get_slide.py <deck-alias> 5 6 7
   python3 ~/.claude/skills/webslides/scripts/get_slide.py <deck-alias> appendix-pubbias
   ```
   `get_slide.py` accepts both integer slide numbers and HTML `id` anchors; prefixing ids with `#` is optional.
7. Validate with `scripts/validate_deck.py` before reporting completion. It runs
   link validation, the overflow check and the style check (`tools/check_style.py`):
   fix every error, and fix or consciously accept every warning on the slides you
   touched (text under 20px, over 70 words visible at once, bullets over 20 words,
   undefined acronyms, top-heavy slides, low-resolution images). Report the
   remaining warnings on untouched slides to the user rather than fixing them unasked.
   If the deck binds stats (`data-stats-dir`), also run
   `python3 <engine>/tools/check_stats.py <deck>`.
8. Last step, before reporting a finished round of edits (not after each micro-edit): screenshot the slides you changed as one contact sheet
   (every reveal step) and look at it with the Read tool against
   `references/style.md`: density, jargon, text size, alignment, figure sizing,
   and whether each step reveals the visual and its explanation together. Fix
   what you find before reporting.

```bash
python3 ~/.claude/skills/webslides/scripts/validate_deck.py <deck-alias>
python3 ~/.claude/skills/webslides/scripts/screenshot_slide.py <deck-alias> 5-8 appendix-balance --contact-sheet /tmp/changed.png
```

### Other deck-alias-aware wrappers

```bash
# Every reveal step of slide 25, or one step
python3 ~/.claude/skills/webslides/scripts/screenshot_slide.py <deck-alias> 25
python3 ~/.claude/skills/webslides/scripts/screenshot_slide.py <deck-alias> 25 --step 2 --output /tmp/s.png
# Check a batch of edits in one look: one labelled grid of slides (or every step)
python3 ~/.claude/skills/webslides/scripts/screenshot_slide.py <deck-alias> 5-12 appendix-balance --contact-sheet /tmp/sheet.png
python3 ~/.claude/skills/webslides/scripts/screenshot_slide.py <deck-alias> all --final-only --contact-sheet /tmp/deck.png

# Export deck to PDF (one page per reveal step; --flat for one page per slide)
python3 ~/.claude/skills/webslides/scripts/export_pdf.py <deck-alias> --output talk.pdf

# Export deck to editable PowerPoint (native text, tables, charts and click animations; also takes a Claude Slides project folder)
python3 ~/.claude/skills/webslides/scripts/export_pptx.py <deck-alias> --output talk.pptx
# Picture version for presenting on an unfamiliar computer (one image slide per reveal step; nothing editable)
python3 ~/.claude/skills/webslides/scripts/export_pptx.py <deck-alias> --output talk.pptx --images

# Export deck to a Beamer/Overleaf project (for coauthors who edit in LaTeX)
python3 ~/.claude/skills/webslides/scripts/export_beamer.py <deck-alias> --output /path/to/out --compile --zip
```

Screenshots load the full deck, so they show the real footer, slide number,
section nav and JS-drawn charts. Slide numbers are deck positions (appendix
included).

Beamer export is one-way (edits in the .tex never flow back to the HTML deck);
regenerate from the HTML and re-share when the web deck changes. Review
`_review/contact_sheet.png` and the overflow list in `_review/report.md` before
sending. See `references/beamer_export.md`.

For tools that don't take a deck (latex_table.py, pdf2svg.py, unflatten_table.py, serve.py),
call the engine repo directly at `<engine>/tools/`.

## New Decks

Create research decks under the owning project's presentation folder, not under
the WebSlides engine repo:

```text
<project-root>/presentations/webslides/<deck-name>/
  index.html
  figures/
  tables/
```

The parent `presentations/webslides/` directory should contain an `engine`
symlink to `<engine>/engine`, so deck
HTML can use:

```html
<link rel="stylesheet" href="../engine/webslides.css">
<link rel="stylesheet" href="../engine/themes/academic.css">
<script defer src="../engine/webslides.js"></script>
```

Add the deck to `references/deck_locations.yaml` when created or moved (create
the file from `deck_locations.example.yaml` if it does not exist yet; Presto's
Decks mode lists the decks from it, grouped by `project_id`).

## Minimal Deck Template

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=1280">
    <title>Talk Title</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.css">
    <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.js"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/contrib/auto-render.min.js"
        onload="renderMathInElement(document.body, {
            delimiters: [{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}]
        });"></script>
    <link rel="stylesheet" href="../engine/webslides.css">
    <link rel="stylesheet" href="../engine/themes/academic.css">
    <script defer src="../engine/webslides.js"></script>
</head>
<body>
<div class="ws-deck" data-section-nav>
    <section class="slide title">
        <h1>Paper Title</h1>
        <div class="subtitle">Subtitle or paper description</div>
        <div class="author"><b>Author Name</b>, with Coauthor A and Coauthor B</div>
        <div class="venue">Conference Name</div>
        <div class="date">Month Year</div>
    </section>
    <section class="slide thanks">
        <h1>Thank you!</h1>
        <div class="email">presenter@example.org</div>
    </section>
</div>
<div id="ws-footer">
    <span id="ws-footer-left">Author · Conference</span>
    <span id="ws-footer-right"></span>
</div>
</body>
</html>
```

## Non-Negotiables

- The academic theme's defaults are the house style (pure white, Lato, 46px titles, 28px body). Use its layout classes (`references/authoring.md` § Theme Layouts) instead of copying CSS from older decks. Lato is deliberate: the engine theme and Beamer exporter use it, and changing it would re-flow existing decks.
- Main substantive text should almost never be below 20px; split, simplify, or move detail to appendix instead of shrinking.
- Avoid italics in slides.
- Use bold short bullet lead-ins selectively, not whole bold bullets.
- Use simultaneous gradual reveal: reveal visual elements and their explanatory text at the same step.
- For many graphs, show axes/context first, then reveal data marks.
- Most bullets and short slide sentences do not need terminal full stops.
- Put author and coauthors on one title-slide line: `<div class="author"><b>Presenter Name</b>, with Coauthor Names</div>`.
- Always include a final non-appendix `Thank you!` slide with the presenter's email (name and email are in `references/local.md` when it exists; otherwise ask).
- Run `scripts/validate_deck.py` (links, overflow and style) and look at a contact sheet of the changed slides after editing any deck.

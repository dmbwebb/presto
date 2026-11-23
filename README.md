# Presto

An app and a skill for building and presenting actually-good slides with LLMs.

Slides are plain HTML files, so an AI coding agent such as [Claude Code](https://docs.claude.com/claude-code) can write and edit them directly. This repo has three parts:

- **WebSlides engine** (`engine/`): a small, dependency-free HTML slide engine and an academic theme. It supports step-by-step reveals, appendix slides with back buttons, section navigation, slide numbers, KaTeX maths, regression tables and SVG figures.
- **Claude Code skill** (`skill/webslides/`): tells Claude how to write good talks (structure, slide style, figures, tables, reveals) and how to check its own work using the tools below.
- **Presto** (`presto/`): a macOS app with three modes. Decks lists your talks, Edit is a visual editor with an AI "refine this slide" command, and Present is a presenter console with a timer, pace and sections that drives a second screen.

The `tools/` folder holds the command-line tools: serve a deck, check links, overflow and style, take slide screenshots, and export to PDF (one page per reveal step, or one per slide), editable PowerPoint, or Beamer LaTeX.

## Install

Requirements: macOS 13 or later for Presto, Python 3.10 or later, the Xcode Command Line Tools (`xcode-select --install`) to build Presto, Poppler for PDF figure conversion (`brew install poppler`), and Node.js for the browser tests.

```bash
git clone https://github.com/dmbwebb/presto.git
cd presto

# Python tools (export, screenshots, checks) use Playwright's Chromium
python3 -m venv ~/.venvs/webslides-export
~/.venvs/webslides-export/bin/pip install -r requirements.txt
~/.venvs/webslides-export/bin/playwright install chromium

# Claude Code skill
mkdir -p ~/.claude/skills
ln -s "$PWD/skill/webslides" ~/.claude/skills/webslides

# Presto (compiles from source and installs to /Applications)
bash presto/app/install.sh
```

The skill's scripts find this repo through the symlink. If you copy the skill somewhere else instead, set `WEBSLIDES_HOME` to the absolute path of this repo. Reinstalling Presto over an existing copy needs the `trash` command (`brew install trash`).

## Use

Ask Claude Code for a deck ("make a 20-minute webslides talk from this paper"), or start from the examples:

```bash
python3 tools/serve.py examples/kitchen-sink        # every slide type, in the browser
open -a Presto --args --edit "$PWD/examples/kitchen-sink/index.html"
```

Keep your own decks outside this repo, for example `<project>/presentations/webslides/<talk>/index.html`, with an `engine` symlink in `presentations/webslides/` that points at this repo's `engine/` folder. Decks then link `../engine/webslides.css`, `../engine/themes/academic.css` and `../engine/webslides.js`.

Common commands:

```bash
python3 tools/validate_links.py DECK/index.html
python3 tools/check_overflow.py DECK/index.html --all --strict
python3 tools/screenshot_slide.py DECK/index.html 5-8 --contact-sheet /tmp/sheet.png
~/.venvs/webslides-export/bin/python tools/export_pdf.py DECK/index.html --output talk.pdf
~/.venvs/webslides-export/bin/python tools/export_pptx.py DECK/index.html --output talk.pptx
```

Presto's Refine command calls the `claude` command-line tool, so it needs Claude Code installed and logged in. Presto looks for decks listed in `~/.claude/skills/webslides/references/deck_locations.yaml` (see `deck_locations.example.yaml` there), plus recent decks and folders you add.

## Tests

```bash
WEBSLIDES_PYTHON=~/.venvs/webslides-export/bin/python bash tests/run-nonforeground-tests.sh
```

`tests/TESTING.md` describes the full suite, including the app tests that take keyboard focus.

## Licence

MIT, see `LICENSE`. The bundled KaTeX (`engine/vendor/katex`) and fonts (`engine/fonts`) keep their own licences.

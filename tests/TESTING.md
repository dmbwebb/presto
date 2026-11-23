# Presto & WebSlides Engine Tests

Run native foreground tests on the Field Mac, not the Big Mac, where their
windows and keystrokes interrupt Duncan's work. Verify source parity first
and pass `PRESTO_APP_EXECUTABLE` to the exact rebuilt bundle. The
non-foreground suite can run on either Mac.

## Quick Start

```bash
# Run the broad suite that should not foreground Presto or steal keyboard focus
bash tests/run-nonforeground-tests.sh

# Run automated engine/browser fixtures in headless Chrome via Playwright
bash tests/run-tests.sh

# Run native app E2E only when focus stealing is acceptable
PRESTO_ALLOW_FOREGROUND_TESTS=1 bash tests/run-app-tests.sh

# Run the crash-focused edit/present lifecycle stress directly (10 cycles by default)
PRESTO_ALLOW_FOREGROUND_TESTS=1 bash tests/run-presto-lifecycle-stress.sh

# Serve a deck without opening/foregrounding Chrome
python3 tools/serve.py examples/kitchen-sink --no-open

# Compile and install Presto app
bash presto/app/install.sh

# Open a deck in Presto's ready console (add --edit for Edit, --play to go live at once)
open -a Presto --args "$PWD/examples/kitchen-sink/index.html"
```

## Test Tiers

### Cotypist native corrections

`python3 -m unittest tests.test_native_cotypist` compiles the production
`CotypistEditing` helper into a disposable app and changes only its expected
sender bundle identity to the test app. Thirteen native correction bursts
exercise the shipped Source and Refine textareas and the real visual editor:
end-of-line, multiline, existing suffix, selection, accent and emoji edits,
caret positions, native Undo/Redo, source-save messages and committed visual
history. The window stays offscreen, uses nonpersistent WebKit storage and
never posts global keys, opens a real deck or submits a Refine request. Ordinary
events and modified shortcuts must bypass the helper; the local-monitor check
also pins forwarding of ordinary key-up events and interception before slide
navigation.

`COTYPIST_TEST_BASELINE=1` removes the ordered helper in the test build and must
fail its handled-path assertion. This is a regression check for the native
editing path, not a claim to reproduce the timing-sensitive corruption in a
background window. The foreground reproduction belongs on the Field Mac with
real Cotypist, an explicitly visible correction and a disposable deck.

### Native save and editor transactions

**Files**: `tests/test_presto_transactions.swift`, `tests/test_presto_shell_modes.swift`, `tests/test_editor_transactions.py`, `tests/wysiwyg-transaction-tests.html`, `tests/test_presto_audience_clicks.py`
**Run**: `bash tests/run-nonforeground-tests.sh`

The Swift runner compiles the production `AppDelegate` with `PRESTO_TESTING`
and substitutes only disk, WebKit, and window boundaries for save tests. It also
uses real temporary files and offscreen WKWebViews for automatic refresh. Its checks cover
failed-write retry, blocked teardown, cross-deck save ownership, pending source
buffers, JavaScript failures, malformed slide replacements, stale AI results,
Refine results recorded in the unified undo history, manual edits after AI
undo/redo (the history's version check stops an undo overwriting them), offscreen preview updates, external-write
conflicts at the actual save boundary, the deck file watcher against a real
file (Presto's own atomic save, outside atomic and in-place writes, sync-client
replacement, delete then recreate), the per-slide three-way merge and the
backups it leaves, backups of whichever version a merge choice drops (a failed
backup stops the choice), UTC backup names that keep their order across clock
changes and second boundaries, PDF export waiting for `WS.ready` (with the
`WS.slides` fallback for older engines) within 30 s, the edit preview and a
reloaded audience window restoring a table step once `WS.ready` resolves, the
edit chrome installed once after the visual editor with Presto's saved rail
and faint-steps settings (and `PrestoLayout` given the edit insets), and the presentation Escape guard and
presenter-driven audience navigation. Refresh checks also cover identical
replacements, delayed file recreation, incomplete writes, unsaved-edit protection,
rendered updates in Edit and Present modes, slide/reveal position, second-screen
synchronization, and retention of the presenter timer. The readiness checks run Presto's
injected JavaScript in JavaScriptCore against a scripted deck page; every test
app subclasses `StubbedUIApp`, so no alert or panel can open, and a 120-second
watchdog fails the binary instead of letting a modal alert hang it. `test_presto_audience_clicks.py` runs the audience-window
click guard from Presto.swift against kitchen-sink in headless Chrome, loaded
the way Presto does (no `?mode=audience`; `WS.mode` set after load): appendix
links must reach the engine and push the return stack; plain slide clicks must
not reach deck scripts; clicks on `.appendix-links` / `.header-links` labels and
`[data-ws-link]` widgets must not advance. The Python tests execute `editor.js`
against a small DOM stub to check dirty-buffer preservation and save handshakes.
The five additional browser checks cover active-session undo/redo and an
unblurred font-size value at native Save.

The same binary includes `tests/test_presto_shell_modes.swift`: mode
transitions keep the edit views and flush first; a failed save keeps Edit
open; ending a talk returns to its origin at the slide and step it ended on;
live guards (⌘W, ⌘2, mode segments, ⌘Q needing a second press); Escape in
Edit never ends the session and goes back from a link jump when nothing is
open; the live two-press guard, with an open slide grid closing first; ⌘Z
routing (the Refine prompt keeps its own); Play shortcuts; menu validation per
state; the Edit toolbar's "Headline · Occasion" title; the inspector split on
real edit surfaces (hidden at creation and after closing with its divider
hidden and the canvas filling the window, shown at its saved width); real go-live and teardown rounds (field focus reset, per-show WebContent
recovery, retired views blanked and kept one round, sheets held until the talk
ends, Swap before Start, the one-screen show's full-screen options, the
position recorded on quit); a Refine, a nudge and two ⌘Z against the real edit
preview; the audience-window plan for one display, forced dual screen, two and
three displays; the console state; outline (slide titles read `<br>` as a
space and drop KaTeX's MathML copy) and black-out scripts in
JavaScriptCore; the injected-file status; and the production injection
against the real audience (edit-layout.js, present-hud.js), edit preview,
presenter.html and decks.html pages in offscreen WebKit. The runner compiles
both Swift test files with `-D PRESTO_TESTING` and every source in its
`APP_SOURCES` list (`presto/RefineSubprocess.swift`, `PDFSnapshot.swift`,
`DeckLibrary.swift`, `Presto.swift`, `PrestoWindow.swift`,
`PrestoPresent.swift`, `PrestoInjection.swift`, `PrestoMenus.swift`). The
binary must run from the repo root (it loads `examples/kitchen-sink` and
`presto/*.html`) and takes about 30 s.

**Expected**: `TRANSACTION_TESTS: 300 passed, 0 failed`

These checks are complemented by the real-input Chrome harness, which checks
keyboard focus after leaving the font field. CDP Windows virtual key codes
must not be copied into `nativeVirtualKeyCode` on macOS.

### Tier 1: Engine API Tests (browser)

**File**: `tests/engine-api-tests.html`
**Run**: `bash tests/run-tests.sh`

Tests 225 assertions covering:
- WS object exposure (slides, mainSlides, appendixSlides, totalMainSlides)
- Readiness: `WS.ready` resolves to `WS` and sets `<html data-ws-ready="1">`; `WS.loadErrors` records failed table and SVG loads with their slide index, and failed SVGs show a visible error box that keeps the authored `<img>`
- Inline SVG replacement, invalid-SVG rejection, and ID/reference/style-selector prefixing
- Navigation function exposure (goToSlide, nextStep, prevStep, nextSlide, prevSlide, goToAppendix, returnFromAppendix, goBack, toggleOverview, parseRevealRange)
- `WS.parseRevealRange()` semantics (open, bounded, empty and malformed ranges)
- Reveal shortcut `data-reveal="each"`: generated steps (nested items included), `data-reveal-start`, explicit steps kept, `data-ws-auto-step` markers, step counts and export info
- getState() API (index, step, maxSteps, totalSlides, mainSlides, isAppendix, slideNumber, returnStackDepth)
- Navigation: goToSlide with step values (0, 2, 'end', invalid/fractional)
- Navigation: nextStep/prevStep crossing slide boundaries
- Navigation: nextSlide/prevSlide
- Appendix/navigation hashes with return stack, encoded targets, and deep links to revealed content
- Appendix return stack on every route: `.ws-appendix-link` buttons, plain `<a href="#id">` links and `hashchange` push the return position; back links and `WS.goBack()` pop it
- Slide clicks: a plain click advances in normal mode; clicks inside `.appendix-links`, `.header-links` or `[data-ws-link]` never do, and nothing advances once `WS.mode` is changed after load
- Speaker notes: getNotes() for slides with/without notes, HTML in notes, out-of-range indices
- Notes hidden from display (aside.notes display: none)
- Event system: navigate event fires for slide changes and reveal-step changes, with correct state and listener failures that do not break navigation
- Mode system: default mode is 'normal' and invalid mode normalizes to normal
- Boundary conditions: prev at first slide, next at last slide, out-of-range goToSlide, strict malformed index handling, step clamping
- Overview toggle and reveal-state thumbnails
- Presto edit preview: the retired rail (installPrestoEditSidebar draws nothing and hands over to PrestoEditChrome), applyPrestoEditViewport delegating to PrestoLayout, Enter/Backspace never stepping in the edit preview, and the viewport guards for invalid transient layout metrics

**Expected**: Runner shows `225/225 tests passed`

### Tier 1b: Engine Table Layout Tests (browser)

**File**: `tests/engine-layout-tests.html` (also run as `engine-layout-tests.html?inline-only`, which removes every `data-table-src` slide before the engine starts)
**Run**: `bash tests/run-tests.sh`

Waits for `WS.ready` (which must outlast a KaTeX renderer that appears only after 1.2 s), then checks table fitting geometry: a scaled table followed by a note keeps its last row inside the clipping container and the note on the slide; KaTeX math inside a loaded table is rendered (with a renderer that appears only after the tables load); a small table grows within the 1.3 cap without pushing later content off the slide, and `data-no-grow` keeps base size; inline `data-overlays` are built and revealed even when the deck has no external tables. Table metrics are fixed in the fixture so theme font changes do not move the checks. The "Theme layouts" slides use plain tables under the real theme rules: a table above a `.fig-plot` never squeezes the figure (or grows into its share), a tall table inside `.fig-notes > .fig-plot` shrinks to stay inside the content area, a pinned `.footnote` does not stop a small table growing, and neither a grown table nor a footnote runs under pinned `.header-links` buttons.

**Expected**: Runner shows `39/39 passed` (default) and `24/24 passed` (inline only)

### Tier 1c: Tools Behaviour Tests (headless Chromium)

**Fixture**: `tests/fixtures/tools-deck/` (ranged reveals, steps inside an
inline SVG, external-table steps, a `data-reveal="each"` list, tiny text, a
clipped table, an undefined acronym, a low-resolution image, footer overlap, a
KaTeX error, a stale `data-steps`).
**Files**: `tests/test_check_style.py`, `tests/test_check_overflow.py`,
`tests/test_export_pdf.py`, `tests/test_screenshot_slide.py`,
`tests/test_export_beamer.py`, `tests/test_common.py`
**Run**: `bash tests/run-nonforeground-tests.sh` (part of `python3 -m unittest discover`; the runner also runs `tools/check_style.py` on kitchen-sink)

They check style findings per fixture slide, screenshot and stepped-PDF page
counts against engine steps, flat-PDF text (early ranged reveals hidden, SVG and
table steps shown), `WS.ready` waits, load-error failures, button overlap
(content under pinned `.header-links` / `.ws-back-link`), a missing SVG giving
exactly one load-error, outline slides exempt from top-heavy, a Beamer export without
`--compile`, and that every tool runs as `/usr/bin/python3 tools/<tool>.py`.

### Tier 1d: Step model, morph, ws CLI and svgkit

- **Step model** (`tests/step-model-tests.html`, in `run-tests.sh`): relative
  steps (`+`, `=`, `+:N`), `data-fade-after`, `data-focus`, `data-morph`,
  `WS.getRevealRange` and `WS.describeSteps`, and `WS.refreshSlideSteps` after
  in-place edits (relative steps edited twice, reveal lists renumbered,
  idempotence, clamping and the navigate event). The runner expects `65/65 passed`.
- **Morph geometry**: `~/.venvs/webslides-export/bin/python tests/step_morph_check.py`
  (Chromium and WebKit, five window sizes, engine scale and Presto-style zoom,
  positions in SVG user units mid-move and at rest, forward and back; also run by
  `tests/test_step_morph.py`). `tests/test_step_tools.py` checks the Beamer and
  flat-PDF exports of relative steps.
- **ws CLI** (`tests/test_ws.py`, fixture `tests/fixtures/ws-deck/`, copied to a
  temp dir before any write): range parsing, slide addressing and aliases, outline
  format, `get`/`put` round trips (CRLF kept, stale hash refused), `steps`
  insert/delete/swap/dry-run and the SVG flags, diff cropping, and browser runs of
  outline, layout, check and diff. Browser and image cases skip without Playwright
  or Pillow.
- **svgkit** (`tests/test_svgkit.py`, stdlib only, also runs under python3.9):
  deterministic output, attribute passthrough, escaping, arrow and bracket
  geometry, atomic and no-op writes, `--only`/`--list`/`--check`/`--json`, and that
  `examples/svgkit-demo/figures` are up to date.

- **Deck content** (`tests/test_deck_content.py`, fixture `tests/fixtures/content-deck/`):
  stats binding and SVG math, including Chromium and WebKit geometry and the
  Presto save round-trip; serves KaTeX from `WEBSLIDES_KATEX_DIST` or a local
  `node_modules` copy, else the CDN, and skips math checks when KaTeX is unavailable.

`run-tests.sh` needs a Python with Playwright first on `PATH`
(`PATH=~/.venvs/webslides-export/bin:$PATH`); `test_pdf2svg` needs poppler's
`pdftocairo` on `PATH`, which `run-nonforeground-tests.sh` adds.

### Tier 1e: Offline decks and KaTeX under zoom

**Run**: `bash tests/run-nonforeground-tests.sh` (unittest discovery). The system-WebKit tests here and in Tier 2c open an offscreen, borderless window that never takes focus, and skip when `swiftc` is missing.

- **Offline** (`tests/test_engine_redesign_offline.py`): kitchen-sink in Playwright Chromium and WebKit with every non-local request aborted, and with only `katex.min.css`, only `katex.min.js` or only `auto-render.min.js` failing. The engine loads its vendored KaTeX (`engine/vendor/katex/`), repairs a failed stylesheet in place (the theme's `.katex` size still wins), re-runs the deck's own auto-render call, and `WS.ready` resolves with every formula rendered; Lato and the KaTeX faces load locally and nothing asks Google Fonts. The online baseline answers CDN requests from the vendored files, so the test never touches the network.
- **KaTeX under zoom, system WebKit** (`tests/test_engine_redesign_katex_zoom.py`): compiles `tests/engine-redesign-wkharness.swift`, loads `tests/fixtures/engine-redesign-katex/` and applies `PrestoLayout` at slide scales 0.5, 0.62, 0.75, 0.9, 1.3 and 1.6. Every glyph of inline maths, display maths, `\tau_\alpha^2`, `\mathbb{E}[\gamma_j]`, fractions, roots, a table cell and a slide patched in after load must stay within 3 CSS px of its scale-1 position and the subscript drop within 0.08 of its scale-1 value. Without the compensation the system WebKit moves glyphs 17 to 26 px and the drop falls from 0.31 to 0.02.

**Expected**: both files `OK` (6 tests each).

### Tier 2: Mode System Tests (browser)

**File**: `tests/mode-tests.html`

The automated runner covers:

| URL | Tests |
|-----|-------|
| `?mode=preview` | body has ws-mode-preview class, footer hidden, progress hidden, API works |
| `?mode=audience` | body has ws-mode-audience class, footer visible, API works |
| `?slide=2&step=1` | starts at slide 2 step 1 (1-indexed) |
| `?slide=abc&step=bad&mode=garbage` | bad params normalize without duplicate active slides |
| (no params) | normal mode, no mode classes |

### Tier 2b: External Table Reveal Tests (browser)

**File**: `tests/table-step-tests.html`
**Run**: `bash tests/run-tests.sh`

Verifies that async-loaded external table rows contribute to `maxStep` and apply reveal visibility after navigation.

**Expected**: Runner shows `TABLE_STEP_TESTS: passed`

### Tier 2c: Layout Geometry Tests (browser)

**Files**: `tests/chrome-edit-preview-layout.js` (Chrome), `tests/test_engine_redesign_layout.py` (Playwright Chromium and WebKit, and the system WebKit), shared probe `tests/engine-redesign-layout-probe.js`
**Run**: `bash tests/run-tests.sh` (the Chrome harness, edit and preview layout modes); the Python test runs in `bash tests/run-nonforeground-tests.sh`

Injects `presto/edit-layout.js` the way Presto does and checks `PrestoLayout.apply` on kitchen-sink's title, section-header, content and appendix slides for tall (900x1130), wide (1800x820), square (1100x1100) and laptop (1440x900) windows: edit mode with the 232px rail, 72px step strip and 28px padding, with and without the 360px inspector; audience mode; preview mode on `?mode=preview`. Each slide must be centred in the canvas at 16:9; the section nav's top must equal the slide's top and the footer's bottom the slide's bottom, both exactly the slide's width; the progress bar runs along the slide's top edge as a fraction of its width; the edit canvas is light (`--p-canvas`) with the slide shadow and the audience letterbox is the slide's own background; appendix slides highlight no section; `--ws-katex-zoom` is set on `.ws-deck` only below scale 1; no other slide leaks through. The Python test also checks that every slide serialises identically through the visual editor before and after the layout (nothing the layout writes can reach a deck file), and loads kitchen-sink over `file://` with the network blocked in the system WebKit (vendored KaTeX and local fonts).

**Expected**: `PASS content:` and `PASS section-header:` lines and `PASS 32 slide layouts (edit)` / `(preview)`; the runner shows `edit layout geometry` and `preview layout geometry`.

### Tier 2d: Malformed Deck Structure Tests (browser)

**Files**: `tests/malformed-deck-tests.html`, `tests/malformed-deck-missing.html`, `tests/malformed-deck-nested.html`, `tests/malformed-deck-empty.html`, `tests/malformed-deck-nonsection.html`
**Run**: `bash tests/run-tests.sh`

Verifies that the engine fails loudly on missing, multiple, nested, empty, or non-`section` slide roots instead of silently using an ambiguous slide set.

**Expected**: Runner shows `Malformed deck structure` with the expected `WebSlides deck structure error`.

### Tier 2e: Presto Edit Transition Performance (browser)

**File**: `tests/presto-edit-transition-perf.js`
**Run**: `node tests/presto-edit-transition-perf.js`

Generates a heavy temporary edit-mode deck, measures full deck reload transition time, in-page slide switching time, and dirty editor slide switching time, and fails unless direct in-page switching is at least 5x faster than a full reload and the dirty-editor switch at least 3x faster (`PRESTO_DIRTY_MIN_SPEEDUP`) and under 150 ms. Since the theme's fonts are local a reload takes about 60 ms instead of 90, so the dirty ratio (an editor flush round trip of about 12 ms) sits near 5x and is held to 3x. Set `PRESTO_TRANSITION_DECK=/path/to/deck/index.html` to run the same timing test against a real deck.

**Expected**: Runner prints `Presto edit transition speedup: ...x` and `Presto dirty editor transition speedup: ...x`.

### Tier 2f: WYSIWYG Editing Tests (browser)

**Files**: `tests/wysiwyg-tests.html` (387 behavioral tests for `presto/wysiwyg.js`: selection model including overlapping-object cycling with visible-step priority, text sessions with owner-captured commits, structural/formatting and list ops, per-slide undo/redo, serialization round-trips for SVG/table/KaTeX and the `data-reveal` shortcut (generated `data-step`/`data-ws-auto-step` stripped on save), message protocol, the light chrome (no dark top-right panel; the PDF and source actions are API only), ⇧⌘O and `ghostFutureChanged`, new text boxes that leave no trace unless typed into (BACKLOG 9), resize math, future-step visibility, watchdog), `tests/wysiwyg-redesign-tests.html` (178 tests: the contextual format bar per selected kind, equation editing in place with a KaTeX stand-in, theme colour and box swatches, the "Appears at step" chip, reveal-each renumbering, the unified history with outside changes (`recordExternalChange`, `sectionHTML`/`expectSectionHTML` saves, refusal rollback, `refineHistory`, a visual edit on top of an outside change undoing to its exact bytes, undoState re-posted when the Refine count changes), the Escape order, link targets and labels, and `textEditing` for in-page fields), `tests/edit-chrome-tests.html` (113 tests for `presto/edit-chrome.js`: the rail grouped by section with folding, numbers, reveal dots, current ring, keyboard navigation and thumbnails that refresh after edits and patches (transform-scaled, final reveal state, no duplicate ids), the step strip and ghost toggle (shown off while disabled on a slide without steps, its saved state back once enabled), the zoom label and `presto:layout` geometry, the link card, Change target, the Back chip and its lifetime, ⌘-click, slide titles that read `<br>` as a space and drop KaTeX's MathML copy) and `tests/wysiwyg-guides-tests.html` (81 tests for the `presto/wysiwyg-guides.js` snapping/readout module).
**Run**: `bash tests/run-tests.sh` (all four are registered in `run_browser_tests.py`)

Static wiring is pinned by `python3 tests/test_wysiwyg_js.py` and `tests/test_edit_chrome_static.py` (the PrestoEdit and PrestoEditChrome APIs, the messages, transform-scaled thumbnails, light-only colours, token fallbacks).

Run in WebKit rather than the Chrome runner, `wysiwyg-tests.html` has one known failure, "triple-click selects the whole block" (WebKit puts whitespace around a `<br>` in the selection string); it fails at base f6bee17 too.

**Expected**: Runner shows `387/387 passed`, `178/178 passed`, `113/113 passed` and `81/81 passed`.

### Tier 2g: WYSIWYG Real-Input E2E (Chrome CDP)

`tests/test_wysiwyg_lists.py` also drives real keyboard input and toolbar clicks
in headless Chromium and WebKit. It covers repeated Enter-created bullets,
splitting formatted text and selections, nested and numbered lists, empty
items, Shift-Enter, the Add bullet button, undo, saved HTML, and reveal-list
serialization. Run it with `~/.venvs/webslides-export/bin/python -m unittest
discover -s tests -p test_wysiwyg_lists.py`; the non-foreground suite includes it.
Set `PRESTO_WYSIWYG_JS` to test another copy of the editor script.

`tests/test_edit_chrome_e2e.py` drives real mouse and keyboard input in headless Chromium and WebKit against the edit chrome and the redesigned editor: a link click opens the card, Go to slide and the Back chip return to the same slide and step, ⌘-click goes straight there, double-click opens Edit TeX (parse errors disable ⌘↩; Escape cancels), the Text box action and an accidental double-click, the Escape order, the step strip, the rail and its ghost toggle, theme colours and the "Appears at step" chip. The non-foreground suite includes it. Set `PRESTO_WYSIWYG_JS` or `PRESTO_EDIT_CHROME_JS` to test other copies.

**Files**: `tests/chrome-wysiwyg-e2e.js`, `tests/chrome-guides-e2e.js`
**Run**: `node tests/chrome-wysiwyg-e2e.js` (also part of `run-nonforeground-tests.sh`); `node tests/chrome-guides-e2e.js`. If the system Chrome is mid-update or CDP becomes unresponsive, set `CHROME` to a healthy Chrome-for-Testing executable; individual CDP requests fail after 30 seconds instead of hanging indefinitely.

Drives TRUSTED input events (CDP `Input` domain) against the exact edit-mode environment Presto creates (edit-layout.js when present, else the engine viewport with the rail inset, then the edit chrome and the injected `wysiwyg.js` **and** `wysiwyg-guides.js`): select, drag with snapping (Alt bypasses — precision scenarios hold Alt), all four resize handles, text sessions, toolbar, nudge coalescing, ⌘-arrow navigation, cross-slide session safety, artifact-free save sweep. Trusted input catches capture-phase ordering bugs that synthetic `dispatchEvent` fixtures cannot (three shipped-bug near-misses on 2026-07-15). `PRESTO_WYSIWYG_DECK=<path>` runs the deck-agnostic core against any real deck (deck-specific scenarios skip).

**Expected**: `WYSIWYG_E2E: passed` / `GUIDES_E2E: passed`; screenshots in `/tmp/webslides-wysiwyg-e2e.png`, `/tmp/webslides-guides-e2e.png`.

### Tier 2h: Decks mode (deck library and the Decks page)

**Files**: `tests/test_deck_library.swift`, `tests/decks-tests.html`, `tests/test_decks_page.py`
**Run**: `bash tests/run-nonforeground-tests.sh` (all three; `decks-tests.html` also runs in `bash tests/run-tests.sh`), or directly:

    swiftc -warnings-as-errors presto/DeckLibrary.swift tests/test_deck_library.swift -o /tmp/test_deck_library && /tmp/test_deck_library
    ~/.venvs/webslides-export/bin/python -m unittest tests.test_decks_page -v

`test_deck_library.swift` compiles `presto/DeckLibrary.swift` on its own and runs
146 checks in a fresh temporary directory with an injected UserDefaults suite,
so Presto's real Application Support folder and defaults are never touched:
the one-time migration of the old `recentDecks` list, atomic writes (a new
inode per save, no leftovers, unchanged state not rewritten), a missing,
corrupt or partly wrong store, an unwritable store, the registry's real
structure (quoted values, comments, CRLF, aliases, missing `root`) and its
absence, added folders scanned one or two levels deep (hidden, `node_modules`
and symlinked folders skipped), decks that vanish and come back, Google Drive
paths with spaces, project names (`long-run` becomes "Long-run"), last
position with slide labels and titles (appendix slides "A1"...), talk minutes,
pins, Remove from Recent, thumbnails (scaled to 800 px, keyed by a path hash,
stale after the deck's mtime changes, served as data URLs), the last-position
thumbnail, cloud placeholders (never read; dataless folders never listed),
parsing limits, and 200-deck speed. A final read-only pass parses the real
`deck_locations.yaml` and its decks (skip with `DECK_LIBRARY_REAL_REGISTRY=0`).

`decks-tests.html` loads the real `presto/decks.html` into a same-origin
iframe with a stubbed `presto` message handler and runs 122 checks: empty
state (its "Add folder…" worded like the sidebar), sidebar counts and order, the Continue hero, grid order and card
labels, placeholders and the no-layout-jump rule, one-at-a-time
`decksNeedThumbnail` requests (missing before stale, never cloud-only, timeout),
pointer and keyboard actions (arrows across hero and grid, Return edits,
⌘Return presents, sidebar navigation), the context menus and the messages they
post, views, search, file drops, malformed models, escaping, and date labels.
`test_decks_page.py` runs that page in Chromium and WebKit, then renders a
realistic library in WebKit at the mockup's size and checks geometry against
`presto/design/mockups-2026-10-28/decks.png`, that no chrome is dark, that
the drop target hides a hovered card's ring and Edit/Present buttons (real
pointer hover), and render speed with 400 decks. `DECKS_SCREENSHOT_DIR=/some/dir` also saves
screenshots (default, hover, menu, search, drop, placeholders, empty, narrow)
and a side-by-side with the mockup.

**Expected**: `DeckLibrary tests: 146 passed, 0 failed`; `Ran 6 tests ... OK` (one skipped without `DECKS_SCREENSHOT_DIR`); the Chrome runner shows `122/122 passed`.

### Tier 3: Presenter console, one-screen controls and inspector (browser)

**Files**: `tests/test_console_presenter.py`, `tests/test_console_hud.py`, `tests/test_console_inspector.py` (Playwright WebKit plus Node), with the older stub-DOM checks in `tests/test_presenter_js.py`, `tests/test_editor_js.py` and `tests/test_editor_transactions.py`
**Run**: `bash tests/run-nonforeground-tests.sh` (unittest discovery); one file alone with `~/.venvs/webslides-export/bin/python -m unittest tests.test_console_presenter`

- **Presenter console** (`presto/presenter.{html,js,css}`): pace maths (the share of the main deck a position covers, steps as fractions of their slide, ahead only when reached before due, behind only after the share should have ended, on pace within 30 s, no pace without a talk length or before the timer runs, appendix visits keep the last main position, overtime), duration and clock formats, talk-length parsing, state normalization, the Sections card (done with a tick, current "k of m", upcoming ranges, appendix last, click to jump, current row not clickable), counter and next-step hint, the ready phase (Start posts `consoleStart` once, one-screen note, audience name plus Swap, hairlines between the ready card's rows and none under the last, the display picker posting `consoleSetAudienceDisplay`), the talk-length field (`consoleSetTalkMinutes`, `consoleTextEditing` on focus and blur, invalid values rejected, Presto's state never overwrites the field mid-typing), the live clock (Swift timer model, Pause/Resume labels, two-click Reset), no layout shift as the clock ticks, blackout veil, hint toast, `setState` never navigating the preview iframe, the deck's footer and progress line shown in the console's `?mode=preview` iframe on the slide's bottom and top edges (hidden in the slide grid, back after a reload; the engine's preview mode itself still hides them), and a transform-scaled 16:9 stage at four window shapes. A generated nine-slide deck is served locally, so no network is needed.
- **One-screen controls** (`presto/present-hud.{js,css}`): the bar shows on real pointer movement only (WebKit's same-spot mousemove after a slide change is ignored), hides 2.5 s later unless hovered, posts `hudNavigate`/`hudOverview`/`hudEditHere`/`hudEnd`, never lets its clicks reach the deck, binds no keys, leaves slide layout and `<body>` untouched, keeps slide links working, resists hostile deck CSS (buttons, divs, spans, svg, rect, path), uses literal fallbacks without the tokens, hides the bar but keeps the Escape hint when its stylesheet is missing, puts the hint at the bottom centre just above the bar (clear of the section bar and title, with or without the stylesheet), keeps the hint (and a pointer-shown bar) above the black screen, and installs nothing but the hint on an external-display audience.
- **Inspector** (`presto/editor.{html,js,css}`): the highlighted pre wraps exactly like the textarea (scroll heights, padding, font and width match at 300, 360 and 480 px for long, empty, tabbed and maths-heavy bodies), typing keeps the two texts identical, the line-number gutter starts at `bodyStartLine` and widens for four digits, quiet non-editable `<section>` lines, the save-error banner and Try again, the Refine composer (Cmd-Return sends, Return is a new line, empty does nothing), earlier changes and Undo last/Redo from both history shapes, running/queued/error states (the queued count before the instructions, so a long one is cut after it), `prestoRefine.focus()`/`focusRefine()`/Cmd-J focus Refine while Cmd-I does not, `refineTextEditing` on the prompt's focus and blur, `inspectorClose`, and light surfaces only.

**Expected**: all three files `OK` (35, 16 and 12 tests).

### Tier 4: Presto Swift App E2E Tests

**Automated smoke**: `bash tests/run-app-tests.sh`

This command exits with a skip by default so routine verification does not
foreground Presto or interrupt normal Mac work. To run the full app E2E gate,
use `PRESTO_ALLOW_FOREGROUND_TESTS=1 bash tests/run-app-tests.sh`; that mode
deliberately foregrounds Presto, uses the real app bundle, System Events, and
window clicks/keypresses. For routine work in parallel, run
`bash tests/run-nonforeground-tests.sh`.

The app harness launches the bundled app into Decks, opens a deck by CLI into the ready console, plays one live (`--play`) and checks that a one-screen show fills the display, that a single Escape does not end it and a double Escape returns to Decks, presents again from the ready console (View > Present, then ⌘↩), drives Presentation menu navigation with logged WebSlides state assertions, opens Edit by CLI for the kitchen sink and an external project deck, checks the installed app launches into Decks, and runs the lifecycle stress: in one exact owned process it goes live from Edit (alternating Play from Current Slide and Present then ⌘↩), ends the talk (alternating Escape twice and End Presentation; in forced dual-screen mode also closing the audience window), checks Edit returns without being rebuilt, toggles the HTML source, goes to Decks and back, closes the window (Presto keeps running), opens a second deck through File > Open… and Go To Folder (it opens in Edit) and plays it, restores Presto's pre-test defaults, and checks for new crash reports.

Over SSH (the usual route to the Field Mac) CGWindowList returns no window
titles, because the SSH session has no Screen Recording permission, so the
harness finds windows by title through System Events (Accessibility) and uses
CoreGraphics only for window bounds. The Last Slide check derives its target
from the deck: the last non-appendix `<section class="slide">` in
`examples/kitchen-sink/index.html`, at the final step the engine reports
(`PrestoTestNavigation` lines carry `maxSteps`). The cross-deck step opens the
second deck through File > Open… and Go To Folder; that sheet opens
prefilled with the last path anyone went to on the Mac and will hand a non-HTML
file to Presto, so the harness checks the field holds `PRESTO_SWITCH_DECK`
before pressing Return and confirms the panel once.

The same lifecycle runner can expose both presenter roles on one physical
display and deliberately invoke WebKit's production recovery callback:

```bash
PRESTO_ALLOW_FOREGROUND_TESTS=1 \
PRESTO_TEST_FORCE_DUAL_SCREEN=1 \
PRESTO_TEST_SIMULATE_WEB_CONTENT_TERMINATION=audience,presenter,editor-preview,editor,export \
PRESTO_TEST_WEB_CONTENT_TERMINATION_DELAY_MS=1500 \
PRESTO_E2E_CYCLES=6 \
bash tests/run-presto-lifecycle-stress.sh
```

This verifies audience/console state synchronization, alternates ending the
talk with Escape twice and by closing the audience window, reloads each recovered view once, restores the audience
slide/step and the editor's last saved slide, then restarts an interrupted PDF
export from page one and validates the resulting PDF. A connected projector is
still required to validate physical screen assignment and cable disconnects.

Set `PRESTO_E2E_CYCLES=25` for a longer release stress run. Use
`PRESTO_STRESS_DECK=/path/to/index.html` to exercise a specific real deck such
as Mada NBER SI without modifying it. Set `PRESTO_SWITCH_DECK=/path/to/index.html`
to choose the second deck used by the cross-deck file-panel regression.

**Prerequisites for physical dual-screen coverage**: Compile with `bash presto/app/install.sh` and connect an external display. Use the forced dual-screen command above for deterministic one-display regression testing.

#### Test 1: Decks

1. Launch: `open -a Presto`
2. Verify: one light window opens in Decks: Recent, Pinned, the registry's projects and added folders in the sidebar, the Continue panel for the last deck and a grid of the other decks
3. Click "Open…" (⌘O) → NSOpenPanel appears
4. Select `examples/kitchen-sink/index.html`
5. Verify: the deck opens in Edit at its last position

#### Test 2: Recent Decks

1. Quit and relaunch Presto
2. Verify: kitchen-sink appears under Recent and in the Continue panel, with the slide you left off on
3. Click Continue editing (or press Return on its card)
4. Verify: Edit opens at the slide and step you left; Present on a card (or ⌘Return) opens the ready console instead

#### Test 3: CLI Launch

```bash
open -a Presto --args /path/to/webslides/examples/kitchen-sink/index.html
```
Verify: Skips Decks and opens the ready console (Start button, talk length, audience screen). `--args --edit <deck>` opens Edit; `--args --play <deck>` goes live at once.

#### Test 4: Dual-Screen Presentation (requires external display)

1. Connect external display
2. Launch Presto and open a deck in Present; check the ready console, then click Start (or press ⌘↩)
3. Verify:
   - **Audience window**: covers the projector completely (no title bar, menu bar or Dock); the laptop shows the console with timer, pace and sections
   - **Presenter console**: the main window on the laptop opens ready: the current slide large, "1 / 20", a Start button, the talk length field and the audience screen. After Start: elapsed time, "of <talk length>", the pace bar, "Ends at", ahead/behind of pace, and the Sections card. No notes, no next-slide preview.
4. Unplug the projector: the audience window hides and the console stays live; plug it back in: the audience view returns at the current slide

#### Test 5: Navigation Sync

With presentation running:

| Key | Expected |
|-----|----------|
| Right arrow | Both windows advance one step |
| Left arrow | Both windows go back one step |
| Space | Same as right arrow |
| Right arrow (past last step on slide) | Both advance to next slide |
| Left arrow (at step 0) | Both go to previous slide, last step |
| Home | Both go to slide 1 |
| End | Both go to last main slide |
| Escape (in appendix) | Both return from appendix |
| Escape (not in appendix) | First press shows "Press Escape again to end the presentation" as a light toast at the top centre of the console (on one screen, at the bottom centre of the slide, just above the control bar); a second press within 2 s ends the presentation and returns to where it started (Edit or Decks) |
| Click an appendix button in the console's slide | Both windows jump to the appendix; Escape returns both |
| O, then click a slide in the console's overview | Both windows move to that slide |
| O | Overview toggles in the console's current-slide iframe |

#### Test 6: Timer

1. Before Start the console shows the ready card, not a clock
2. Start: the timer starts at 00:00 (it resumes, never resets, on later go-lives of the same deck session); the clock card's pause and reset controls are visible (no double-click)
3. Wait 2+ minutes, verify the timer shows "02:00" and, with a talk length set, a pace line ("On pace" or "n min ahead of pace" / "n min behind pace")
4. Click Pause: the digits grey out, the label reads Paused, the button says Resume
5. Click Resume: the timer continues from where it stopped
6. Click Reset once: it changes to "Confirm reset" for 3 s; click again to reset to 00:00

#### Test 7: Single-Screen Mode

1. Disconnect external display
2. Launch Presto and open a deck in Present: the ready console notes that Start fills this screen
3. Verify: Start makes the window itself full screen showing only the deck; the control bar appears when the pointer moves and hides 2.5 s later; T toggles a corner timer; leaving full screen ends the show
4. Verify: Keyboard and Presentation menu navigation work without a console

#### Test 8: Menu Bar

- File > Open… (⌘O, opens in Edit), Open Recent, Add Folder to Decks…, Export PDF… (⌘E)
- View > Decks ⌘1 / Edit ⌘2 / Present ⌘3, Show Slide Rail, Show Inspector ⌥⌘I, HTML Source ⇧⌘X, Show Later Steps Faintly ⇧⌘O, Reload ⌘R
- Edit > Undo/Redo (one history), Add Text Box ⌥⌘T, Edit Equation, Refine with AI… ⌘J
- Presentation > Play from Start ⇧⌘↩, Play from Current Slide ⌘↩, Next/Previous Step, First/Last Slide, Swap Displays, Black Screen (B), End Presentation ⌘. (only while presenting)
- ⌘W closes the window and Presto keeps running; ⌘Q quits

#### Test 9: Backward Compatibility

1. Open kitchen-sink deck directly in Chrome: `open examples/kitchen-sink/index.html`
2. Verify: All existing features work (navigation, overview, appendix, reveals)
3. Verify: Speaker notes are not visible anywhere on the slide
4. Verify: URL hash navigation still works (#slide-5)

#### Test 10: Mode System in Browser

1. `index.html?mode=preview` → no footer, no progress bar, no keyboard/click nav
2. `index.html?mode=audience` → footer visible, no keyboard/click nav
3. `index.html?slide=5&step=2` → starts at slide 5, step 2
4. `index.html` (no params) → normal mode, everything works as before

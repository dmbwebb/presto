# WebSlides Gotchas

Use this only when debugging, moving decks, or making engine-level changes.

## Deck Ownership

Research decks should live in their owning project directories. The central
WebSlides repo owns the engine, tools, examples, and scratch decks. Keeping
research decks in the engine repo creates cross-project dirty worktrees and
increases the risk of committing unrelated content changes.

Before moving decks inside a cloud-synced folder or between cloud folders, check
for online-only placeholders with `find <deck> -type f -size 0`. Copy, verify file
counts, then trash the old source.

Presto no longer writes refine-history files (the `*.refine_history.json`
sidecar was retired on 28 Oct 2026; Refine changes now sit in the editor's undo
history). Do not commit old ones or exported presentation PDFs as canonical
deck source unless the user explicitly asks.

When moving decks into a repo with broad ignore rules, verify that image, video,
and table assets are actually staged. Use explicit paths and `git add -f` only
for required deck assets that are ignored by project-wide rules.

## Serving And Paths

Validation/export/screenshot tools should serve decks over local HTTP, not
`file://`, because Playwright/headless Chromium and WKWebView reject or mishandle
some `file://` fetches.

External project decks should have an `engine` symlink in their
`presentations/webslides/` parent directory and use `../engine/...` links from
the deck HTML.

Google Drive can sync an external deck's `engine` symlink with an empty target
(`engine ->`); fix by trashing it and recreating it to point at
`<engine>/engine` on each affected machine.

The dev server historically served from the WebSlides repo root because decks
used `../../engine/` paths. For external project decks, serve from the nearest
ancestor containing `engine/webslides.js`.

## KaTeX And Offline Decks

Decks link KaTeX from jsDelivr; when that fails (offline, captive Wi-Fi) the
engine loads its own copy of the same version from `engine/vendor/katex/` and
re-runs the deck's auto-render call, so keep the standard three-line KaTeX head
(the `onload` call on the auto-render script is what the fallback re-runs).
Presto blocks the jsDelivr KaTeX request in every web view, so it always uses
the vendored copy. In a browser, a CDN request that hangs rather than fails
still delays the page until it times out.

## CSS And Layout

Never set `position: relative` or another `position` on `.slide` or slide-type
rules such as `.slide.section-header` and `.slide.photo`. The engine relies on
`.slide` being absolutely positioned.

Prefer reusable theme classes over inline font-size/layout overrides. Inline
font-size changes often break when the theme changes.

Theme CSS must set `html, body { background }` to match `--color-bg`; otherwise
the engine default can show a black flash between transitions.

Non-active slides need `visibility: hidden` to prevent bleed-through.

Reusable layouts belong in `engine/themes/academic.css`, not in deck-specific
CSS, when they are likely to recur.

A content block with `flex: 1` fills the height left below the title whether
or not it is the slide's last child (the old `:last-child { flex-grow: 0 }`
rule was removed 2026-10). On `.v-center` slides every direct child keeps its
natural height (`flex: 0 1 auto`), so the auto margins can centre it; a block
that should fill the slide does not belong on a `.v-center` slide. Decks that
worked around the old bug with `flex: none` keep working. `check_style.py`
flags slides that still come out top-heavy.

Theme rules apply to every element with the same class, including deck-local
ones. Before adding a theme class, grep the decks' `index.html` files (specific
files, not a recursive Drive grep) for the name; generic names such as `.note`,
`.notes` (speaker notes), `.so`, `.q`, `.swap`, `.takeaway` and `.plot` are
already used by decks.

The theme ships Lato 400 and 700, upright and italic, as local files (no
Google Fonts request; decks work offline), so `font-weight: 500` renders as
400 and 600 as 700. Italic text uses the real Lato italics (before 28 Oct 2026
browsers slanted the upright face).

The theme prepends "Source: " to `.source` via `::before`, so write only the
credit ("Partner Organisation"), not "Photo: ...". On a full-bleed or split photo
slide, a pinned `.source` and a bottom caption collide; move one of them.

## Reveal Editing

Declared `data-steps` on a `<section>` is ignored by the engine and every tool
(they count steps after inline SVGs and tables load, including steps inside
`<img data-inline>` SVGs). Delete it rather than maintain it; `check_style.py`
lists the stale ones.

## SVGs And Figures

Never inline large SVG markup directly in deck HTML. Save it in `figures/` and
use `data-inline` when internal reveal steps are needed.

`pdftocairo` produces cleaner SVGs than Inkscape for R/Stata PDF output.
`pdf2svg.py` strips white page backgrounds by default.

Animating SVG elements between steps: use `data-morph`, never hand-written CSS
transforms. WebKit with CSS zoom (Presto) multiplies px inside CSS transforms on
SVG elements, and resolves `translate(%)` on `<text>` against the text box, not
the view box, so hand-built morphs drift with window size. The engine
calibrates per element; `tests/step_morph_check.py` is the regression check.

In a deck that loads KaTeX, a pair of `$` in SVG text is math (as in HTML):
escape currency as `\$`. The academic theme's `.accent` class no longer touches
KaTeX's own `\hat`/`\bar` spans (it used to make them bold and accent-coloured);
a deck-level `.accent` rule is reset inside maths too. The Beamer HTML-table
converter garbles KaTeX in loaded tables; use `--tex-dir` tables.

A `low-res-image` warning on a photo or video still: no upscaler is installed
locally, but the Real-ESRGAN macOS release
(`github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip`)
runs `realesrgan-ncnn-vulkan -n realesrgan-x4plus -m <models>` 4x in about 20 s
on the GPU and sharpens without inventing faces. For a video still, first pick
the sharpest nearby frame (edge variance); hazy video frames also need
`ImageOps.autocontrast` plus a little colour boost.

Machine-generated SVGs need semantic grouping before `data-step` reveal works.
For clear diagrams, rebuild as HTML/CSS instead of wrapping many generated paths.

JS-drawn charts (D3 from a script at the end of `<body>`): draw them in a
synchronous script that runs before the deferred `webslides.js` initialises, so
`data-step` on chart groups is counted. `screenshot_slide.py` loads the full
deck, so these charts render in its screenshots. A tooltip
`div` appended to `<body>` outside `.ws-deck` makes `export_pdf.py` fail with
"PDF page count mismatch"; hide it with `@media print { display: none }`.

## Tables

Tables in presentations should usually be external HTML files loaded from
`tables/` via `data-table-src`, not inlined into `index.html`.

Use `tools/latex_table.py` and related table tools rather than hand-formatting
large regression tables. It treats every row before the first `\midrule` or
`\hline` as header (merged by `--merge-header`), so when wrapping a paper's
row-only `.tex` fragment in a tabular, put `\midrule` after the header rows.
For decks that reuse a paper's tables, keep a deck-local `code/make_tables.py`
that wraps, trims columns/rows and regenerates the appendix tables from the
paper's current `.tex`, so a paper revision is one rerun.

Table fitting measures the slide's real free space (sibling margins included),
and tables inside a `.table-container` no longer carry their own margin, so the
old padding-instead-of-margins and `.table-container { flex-shrink: 0 }`
workarounds are no longer needed (before the 2026-10-27 fix every scaled table
lost about 23px, its last row, and `check_overflow.py` could not see it;
`check_style.py` now flags clipped rows). While measuring, the fitter stops
filler items from growing, so `.columns` or `.table-commentary` (`flex: 1`) do
not hide free space, but zero-basis fill items such as `.fig-plot` and
`.fig-notes` (`flex: 1 1 0`) keep their own height: a table never grows into a
figure's share, and a tall table inside `.fig-notes > .fig-plot` shrinks to stay
on the slide. Auto margins (a `.footnote` or `.source` pinned with
`margin-top: auto`, `.v-center` centring) do not count as used space. It grows a
small table up to 1.3x when it is the only table on its slide; add
`data-no-grow` to the `.table-container` to keep the base size.

## Presto

Presto's edit mode picks up outside edits to the open deck (agents, cloud
sync), including after its own saves. If Presto holds unsaved edits when the
file changes, it merges slide by slide: slides changed only on disk take the
disk version, slides changed only in Presto keep Presto's, and only slides (or
the deck head) changed on both sides prompt, by slide number, with Keep My
Versions / Use Disk Versions / Reload from Disk. During a live text edit the
merge waits until the text box is left. Before Presto overwrites bytes it did
not write itself, it saves the disk version to
`~/Library/Application Support/Presto/backups/<deck-id>/<timestamp>.html`
(newest 30 per deck); a discarded unsaved version is saved there with a
`-presto-unsaved` suffix. An agent editing the same slide the user has open still
produces a prompt, so prefer editing other slides while they work.

Known bug, fix proposed: each Presto save wraps inline math in one more `<span>`
(`<span>$\gamma$</span>` becomes `<span><span>$\gamma$</span></span>`), so an Edit
`old_string` copied before the user saved no longer matches. Re-read the lines first.
The fix is for Presto's serializer to unwrap the host span it adds around KaTeX.

In Swift/WKWebView, avoid `NSWindow.close()` during event handling; use
`orderOut()` to hide windows.

WKWebView variables should be optional, callbacks should use `[weak self]`, and
script message handlers should be removed before cleanup.

GUI-launched macOS apps do not inherit the shell PATH. Presto should probe
candidate binary paths directly and set an enriched process environment.

WKWebView `fetch()` silently fails for `file://` URLs. Presto injects an
XHR-based `fetch()` polyfill at document start.

Use CSS `zoom` rather than `transform: scale()` for external-display rendering;
`transform` can cause visible blur on slide changes.

Below zoom 1 the system WebKit misplaces KaTeX sub- and superscripts (Chromium
and Playwright WebKit are fine). `PrestoLayout` compensates with
`--ws-katex-zoom` / `--ws-katex-unzoom` on `.ws-deck`; anything else that
shrinks slides (thumbnails, clones) must use `transform` or set the same two
properties on its own container. Never fix it with inline styles on equations:
the visual editor saves them.

WebKit SVG compositing with `zoom` can mishandle `data-step` opacity reveals.
Inject `svg [data-step] { will-change: opacity; }` when needed.

## Engine Debugging

When debugging behavior, first check the deck's `<head>` for inline script
overrides. Decks can monkey-patch `WS` functions or rebuild navigation.

When checking data attribute presence, use `el.hasAttribute('data-foo')`, not
truthiness of `el.dataset.foo`; empty attributes are falsy.

`parseMaxStep` must scan `data-class` as well as `data-step`,
`data-highlight`, and `data-dim`.

Rendering bugs seen only in Presto usually live in the system WebKit, which
can differ from Playwright's WebKit build (KaTeX under zoom, SVG baselines).
Reproduce them in an offscreen WKWebView (`tests/engine-redesign-wkharness.swift`)
and measure geometry with JS: `takeSnapshot` of an offscreen or occluded window
comes back blank.

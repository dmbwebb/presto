# WebSlides Authoring Reference

Use this when editing slide HTML, reveals, section navigation, figures, tables,
appendix links, or recurring layout patterns.

## Section Navigation

Add `data-section-nav` to `.ws-deck` to enable the persistent section header.
The engine reads `<h1>` text from `section-header` slides. Only top-level
sections appear in the top nav.

Slides before the first explicit section header automatically belong to an
implicit Intro section. Override only when needed:

```html
<div class="ws-deck" data-section-nav data-intro-section="Opening">
```

Section-header slides get an auto-injected section TOC unless you write your own
`<ul class="section-toc">`.

```html
<section class="slide section-header">
    <h1>Section Title</h1>
</section>
```

Subsections inherit the parent section and appear in the section-header TOC only
when the current section is active:

```html
<section class="slide subsection-header">
    <h1>Subsection Name</h1>
</section>
```

Subsections are lettered (A, B) in the TOC by default; `data-subsection-numbers="decimal"` on
`.ws-deck` numbers them 3.1, 3.2 under section 3 (the implicit Intro counts as section 1). Name a
subsection without repeating its section ("Results", not "Average fadeout: results"): the
subsection slide already shows the parent section as an eyebrow above the h1. To keep section-header
slides to the top-level list, hide their expanded subsections in the deck CSS:
`.slide.section-header .section-toc .ws-subsection-list { display: none; }`.

## Title Slide

Use one author/coauthor line. Only the presenting author's name is bold.

```html
<section class="slide title">
    <h1>Paper Title</h1>
    <div class="subtitle">Subtitle or paper description</div>
    <div class="author"><b>Presenter Name</b>, with Coauthor A and Coauthor B</div>
    <div class="venue">Conference Name</div>
    <div class="date">Month Year</div>
</section>
```

## Common Layouts

Default content slide:

```html
<section class="slide content">
    <h2>Slide Title</h2>
    <ul>
        <li>Always visible</li>
        <li data-step="1"><strong>Lead-in</strong> - revealed point</li>
    </ul>
</section>
```

Callout (step-in annotation box): `<div class="callout" data-step="2">…</div>`,
`callout-lg` for a bigger box with bigger text, optional `.callout-title`; over a
figure add `style="position:absolute; left:..; top:.."`.

Signpost: `<section class="slide subsection-header signpost"><h1>…</h1></section>`
keeps the nav entry and eyebrow but shows no contents list (replaces the
`<ul class="section-toc" style="display:none">` trick); `<section class="slide signpost">`
is a divider outside the nav, with an optional `.section-subtitle`.

Results preview (one column):
`<ol class="results-list"><li data-step="1"><b>Headline</b><span class="detail">One line</span></li></ol>`.

Two columns:

```html
<div class="columns">
<div class="columns wide-left">
<div class="columns wide-right">
```

Table plus commentary:

```html
<div class="table-commentary table-left wide-table">
    <div class="table-container" data-table-src="tables/table.html"></div>
    <div class="commentary">
        <ul>
            <li>Commentary bullet</li>
        </ul>
    </div>
</div>
```

The engine fits every table container on load (inline tables too): it shrinks a
table so nothing on the slide overflows, and grows a small table into free space,
up to 1.3x, when it is the only table on its slide and has room in its container's
width. Growth never takes space from a `.fig-plot` figure on the same slide, and
a pinned `.footnote` does not stop it. Add `data-no-grow` to the `.table-container` to keep a table at its base
size. Math in tables loaded with `data-table-src` (e.g. `$\beta_1$` from
`tools/latex_table.py`) is rendered with the deck's KaTeX after the table loads.

Vertically centered sparse content:

```html
<section class="slide content v-center">
    <h2>Title stays at top</h2>
    <p>Content is centered below the title</p>
</section>
```

Split-image slide:

```html
<section class="slide split-image">
    <div class="split-text">
        <h2>Title</h2>
        <ul><li>Text content</li></ul>
    </div>
    <div class="split-img">
        <img src="figures/photo.jpg" alt="Description">
    </div>
</section>
```

Full-bleed photo slide: the engine hides the footer, progress bar and section nav on any `.slide.photo` (or any slide with `data-no-chrome`).

```html
<section class="slide photo">
    <img src="figures/photo.jpg" alt="Description">
</section>
```

## Theme Layouts

The theme's defaults (Lato, 46px titles without underline, 28px body, appendix
buttons bottom-right), so new decks need little
or no deck CSS. Prefer these classes to deck-local copies such as `.bd-fig` or
`.tf`.

Figure with side notes. The figure fills the height left below the title;
`.wide` (740px) and `.narrow` (540px) change the figure column (default 690px).

```html
<div class="fig-notes">
    <div class="fig-plot"><img src="figures/x.svg" data-inline alt="..."></div>
    <div class="side-notes">
        <div class="side-note" data-step="1"><b>Lead</b>: short note
            <span class="cite">Author (2024)</span></div>
        <div class="side-note so-what" data-step="2">The implication</div>
    </div>
</div>
```

`.fig-plot` on its own is a figure that fills the remaining height and width;
a `<div class="caption">` inside it sits under the figure.

- "So what" line (left rule in the accent colour): add `.so-what` to any block.
- Research question box: `<div class="question-box"><b>Question</b>: ...</div>`.
- Takeaway box (one per slide; tint follows the deck accent):
  `<div class="takeaway-box">...</div>`, optionally with a `<ul>` inside.

Stacked swap: blocks that replace each other in one spot, using ranged steps.

```html
<div class="swap-stack">
    <div class="side-note" data-step="1-2">First version</div>
    <div class="side-note" data-step="3+">Replacement</div>
</div>
```

Thank-you slide (vertically centred, 60px title, 30px email):

```html
<section class="slide thanks">
    <h1>Thank you!</h1>
    <div class="email">presenter@example.org</div>
</section>
```

Small text: the only sanctioned homes for text under 20px on main slides
(minimum 14px), shared with `tools/check_style.py`'s allowlist: `.footnote` /
`.source` (pinned to the slide bottom, 14px), `.cite` (20px faint reference
under a bullet or note), `.table-note` (19px note under a table).

## Progressive Reveal

| Attribute | Effect |
|-----------|--------|
| `data-step="2"` | Appears at step 2 and stays visible |
| `data-step="2-4"` | Visible only during steps 2 through 4 |
| `data-step="2+"` | Explicit "appears at 2 and stays" |
| `data-highlight="2"` | Yellow highlight at step 2 |
| `data-dim="2"` | Fades to muted opacity at step 2 and later |
| `data-class="className:2"` | Adds class at step 2 and later |
| `data-reveal="each"` (on `ul`/`ol`) | Every item, sub-bullets included, gets its own step |
| `data-step="+"` | One step after the largest earlier `data-step` start on the slide (document order, inlined SVGs included); `"="` same step as the previous `data-step`; `"+:2"` / `"=:2"` last two steps |
| `data-fade-after="3"` (or `"+"`) | Shown normally, dimmed (`ws-faded`, `--ws-fade-opacity`, default 0.25) from step 3; `"+"` = when the next item appears |
| `data-focus="3:.a; 5-6:#b"` | On any container (slide, div, svg, g, or the `<img data-inline>`): during the range, everything inside that is not the selector (or around it) is dimmed; a bare `3` means step 3 onwards, so write `3-3` for one step only; one focus per container, the latest-starting active part wins |
| `data-morph="KEY"` | Elements sharing KEY on a slide are one object: when the shown one changes on next/prev step it moves from the old place to the new (`--ws-morph-duration`, `--ws-morph-easing`); jumps, exports and reduced motion land instantly. Use `display:inline-block` for HTML |

Prefer `+`/`=` and `data-focus`/`data-fade-after` over hand-numbered steps and
ad hoc `data-class` fade classes: inserting a point then needs no renumbering.
For "fade everything else at step N", put `data-focus="N:.the-thing"` on the
figure or slide. To renumber an existing slide's absolute steps, use
`ws steps` (SKILL.md Core Workflow), never hand edits. `data-step="-3"` is not
a valid range (the engine never shows it); write `data-step="1-3"`.

Reveal every bullet in turn without numbering them by hand:

```html
<ul data-reveal="each">
    <li>First point</li>                      <!-- next free step -->
    <li>Second point
        <ul><li>Sub-point</li></ul>          <!-- sub-bullets get their own step -->
    </li>
    <li data-step="1">Kept at step 1</li>     <!-- explicit steps are kept -->
</ul>
<ol data-reveal="each" data-reveal-start="3"> ... </ol>
```

Items get steps in document order, nested items included, starting one past the
highest step used earlier on the slide (or at `data-reveal-start`). Items with
their own `data-step` keep it and do not use up a number. Ancestors' steps are
not counted as "earlier", so give a list inside a `data-step` wrapper an
explicit `data-reveal-start`. The engine writes the generated `data-step` plus a
`data-ws-auto-step` marker at load; Presto strips both when saving, so the
source keeps the shortcut.

Use the same `data-step` for text and visual elements that should appear
together.

```html
<li data-step="1"><strong>What changes</strong> - annotation text</li>
<g data-step="1" id="matching-visual-layer">...</g>
```

For axes-first graph reveals, keep axes and labels always visible, then put bars,
points, lines, and intervals in a later step.

## Inline SVG

To use `data-step` inside an external SVG, save the SVG in `figures/` and mark
the image as inline:

```html
<img src="figures/chart.svg" data-inline alt="Chart">
```

The SVG should have standalone `xmlns` attributes and `data-step` on internal
`<g>` groups.

### Numbers from files and math in figures

Bind numbers to the paper's stats files instead of typing them: declare the
source on the deck root, `<div class="ws-deck" data-stats-dir="../../../outputs/stats">`
(one `NAME.tex` per value; path relative to the deck) and/or
`data-stats-src="stats.json"` (`{"NAME": value}`, wins over the directory). Then
write `<span data-stat="NAME">placeholder</span>` (also on SVG `<text>`/`<tspan>`
and inside loaded tables) or `\stat{NAME}` inside `$...$`. Keep the current value
as the placeholder: Presto saves the placeholder, never the filled value.
`data-stat-round="N"` rounds every number in the value. TeX in the files is
cleaned for slides (`17.5\%` to 17.5%, `5{,}000` to 5,000, `-0.06` to −0.06).
Missing names show a red `[NAME?]` and fail `validate_deck`;
`tools/check_stats.py` lists every stat with its value.

Math in SVG figures: write `$\beta$`, `$\hat\mu_{it}$`, `$\frac{1}{2}$` straight
into `<text>` (figures inlined with `<img data-inline>` or inline `<svg>`); the
engine typesets it with KaTeX as native SVG text at the label's size, colour,
anchor and baseline. Do not hand-write KaTeX-font tspans. Write `\$` for a
literal dollar; `data-no-math` on the figure turns this off. Needs the deck's
KaTeX. Fractions and roots inside a stepped `<tspan>` follow the `<text>`; put
the step on the `<text>` or a `<g>`.

### Figures from Python (svgkit)

Hand-built figures use `tools/svgkit.py` in the engine repo (stdlib only, any
venv; `examples/svgkit-demo/make_figures.py` shows it). Draw in slide px at the
figure's display width (a `.fig-notes.wide` plot column is 740 px) so text sizes
are real. Register one function per figure with `@figure("name")` and end the
script with `run_cli(out="../figures")`; then `python3 make_x.py --only name`
regenerates one figure, `--list` lists them, `--check` exits 1 if outputs are
stale. Writes skip identical bytes, so Presto reloads only what changed. Pass
`step=`, `fade_after=`, `focus=`, `morph=`, `dclass=`, `label=` on `svg.g(...)`
or any element; `label` is what `ws outline` shows. Use `svg.arrow` (tips land
on the points; `start=True` for double-headed, `bend=` to curve),
`svg.bracket(kind="square"|"curly", text=...)` (`flip=True` swaps sides),
`svg.callout(...)`, `svg.errorbar`, `svg.label` (halo text), `svg.axis_x/axis_y`.
Text may contain `$...$` (the engine renders it); with `mi()`/`mr()` concatenate
with `+`, not f-strings, which escape the markup.

For diagrams with clear structure, rebuilding as HTML/CSS is usually easier
than hand-wrapping generated SVG paths. Reserve SVG wrapping for coefficient
plots or graphics where the original figure geometry matters.

## Boxes

```html
<div class="box">
    <div class="box-title">Definition</div>
    Content with $equations$
</div>
```

Boxes should not bold all body text by default. Use `.box-title` for labels and
`<strong>` only for deliberate emphasis.

For result-preview style boxes, box titles can be all caps, grey, and the same
size as ordinary bullet text.

## Appendix Links

Inline appendix buttons:

```html
<div class="appendix-links">
    <a class="ws-appendix-link" href="#appendix-balance">Balance</a>
</div>
```

Pinned appendix buttons (the theme places `.header-links` bottom-right, clear of
long titles; the class name is historical):

```html
<div class="header-links">
    <a class="ws-appendix-link" href="#appendix-spec">Specification</a>
</div>
```

Appendix slides:

```html
<section class="slide appendix content" id="appendix-balance">
    <h2>Balance Table</h2>
    <a class="ws-back-link" href="#">&larr; Back</a>
</section>
```

The back button is also pinned bottom-right; both use Lato. A deck that moves
`.header-links` back to the top must set `bottom: auto` as well as `top`,
otherwise the box stretches between the two.

The button band overlaps the bottom ~25px of the content area. On a slide that
also has a table or a pinned `.footnote`/`.source`, the theme ends the content
area above the band (`padding-bottom: 78px`), so a grown table or a long
footnote never runs under the buttons. Over figures and text the band keeps the
full height; keep text out of the bottom-right corner there.
Keep the band to one row of buttons. Give any scripted click widget the
`data-ws-link` attribute so clicking it never advances the slide and Presto's
audience window lets the click through.

Every route into an appendix slide remembers where you came from: the
`.ws-appendix-link` buttons, any plain `<a href="#appendix-id">` link, and a
`#appendix-id` hash change after load. `.ws-back-link`, Escape and `WS.goBack()`
return there (at the same reveal step). Opening a deck directly at
`index.html#appendix-id` does not record a return position.

## Equations

Use KaTeX delimiters:

- Inline: `$Y_i(1) - Y_i(0)$`
- Display: `$$\mathbb{E}[Y(1) - Y(0)] = \tau$$`

Avoid literal dollar amounts like `$85` in slide text because KaTeX can parse
them as math. Use `85 USD` or wrap text to avoid the delimiter.

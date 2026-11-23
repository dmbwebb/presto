# WebSlides Style Guidance

This is the canonical style guide for WebSlides academic talks. The
talk-structure guidance adapts Jesse M. Shapiro's "How to Give an Applied Micro
Talk" notes: https://shapiro.scholars.harvard.edu/notes-and-lectures

## Talk Structure

Use coarse top-level sections for the talk's major narrative blocks. Most
applied-micro talks should have an implicit Intro plus a small number of
explicit sections such as Data, Main Results, Robustness, and Conclusion. Do not
make every methodological step a top-level section; use ordinary slide titles
for "Step 1", "Step 2", and heterogeneity unless the talk truly needs a section
break.

When section navigation is enabled, opening slides before the first explicit
section are treated as Intro by default. Do not add a visible Intro divider
unless it helps the audience.

Use subsections only when a top-level section contains multiple substantial
clusters, such as separate result families. Avoid subsection headers for one-off
transition slides or single results.

Shortening a talk to fit a slot: the overrun tends to be spoken elaboration on
setup slides, not slide count. Section headers, photo slides and bridge or
roadmap slides cost seconds and help the audience, so they usually stay. Save
time by making text-heavy slides sparser (one sentence where a column of facts
was), moving secondary detail such as a data or measurement slide to the
appendix behind a button, and cutting what the speaker says beyond the slide;
never make a slide denser to save a slide.

General-audience talks (award sessions, public events) tend to work as photo-led
slides with a few words each, but the wording should stay literal: say what was
measured and in which direction, and avoid metaphors, punch lines and narrative
"zingers" in both slide text and the spoken script. Label outcomes by their
research name (stigma, not shame), and avoid a bare signed number under a
negatively valenced label, which reads as the bad thing rising.

## Opening

Assume the audience does not yet care about the topic. Use the first one or two
slides to motivate the problem with a real decision, striking fact, or policy
question. Do not open with literature review or the history of the field.

State a clear research question. Strong questions are motivated by economics and
the real world: a counterfactual, an important parameter, or a theoretical
prediction. Avoid questions that are only "apply method X to setting Y."

Always include a results preview slide near the start of academic talks, after
motivation/contribution and before design details. Assume someone leaves early:
they should still know the main result.

## Data And Strategy

State clearly where each variable comes from and what is measured. Prevent
audience confusion about source, unit, timing, geography, or reliability.

Hide plumbing on main slides. Describe the resulting sample, measure, or source
of variation rather than narrating every processing step.

For empirical strategy slides, be explicit enough to make the design credible:
show the key equation when it is the object of the slide, define every variable,
state the identification assumption, and name the bottom-line statistic.

Put formulas, construction details, acronyms, and robustness machinery in
service of the claim. If a detail is not needed to understand the main slide,
move it to an appendix button, caption, or technical box.

## Results

Prefer figures over tables where possible. Make headline-results slides
figure-first: give the graph most of the slide and put only one or two headline
numbers in compact side cards.

Do not make secondary patterns the headline. Correlations, mechanisms,
diagnostics, and robustness checks can be revealed after the main result or
moved to appendix.

Mechanism sections tend to need explicit signposting, even in a 15-minute talk:
a short bridge slide that poses the question ("why did X move?") and names the
candidate channels, a roadmap slide listing the channels to be tested, then one
slide per channel with the channel number in its title, each ending on a
one-line link back to the main outcome. Hedged interpretive lines ("possibly
because ...") are welcome on results the audience will want explained. A recap
slide that repeats the roadmap is usually cut.

Secondary comparisons (for example a sub-arm versus the main arm across many
outcomes) tend to read better as a few short bullets plus the explanation the
audience will ask for than as a dense multi-row figure.

Explain statistical quantities in audience terms: "percent of the initial effect
lost per year", "standard-deviation units per year", "95% interval",
"correlation between initial size and fadeout."

Write robustness slides as decisions, not inventories. Each bullet should say
what threat the check addresses and whether the inference changes.

Be precise about correction direction. If a correction increases fadeout, say so
directly.

Conclusion slides should reveal data-backed takeaways before agenda items. The
policy implication should be one of the takeaways, not a trailing paragraph.

## Slide Design

Use pure white background and Lato for main text unless the deck deliberately
overrides the academic theme. The theme's defaults implement the reference look
below, so new decks should use the theme's layout classes (authoring.md, Theme
Layouts) rather than copy CSS from older decks.
Title-slide subtitle, author, venue, and date must be comfortably readable.

Main-slide text should almost never be below 20px. This includes bullets,
paragraphs, boxes, table cells and headers, table commentary, captions, side
notes, diagram labels, and chart annotations. Only `.footnote`, `.source`,
`.cite`, `.table-note` and navigation chrome may be smaller, and never below
14px. `tools/check_style.py` (run by `validate_deck.py`) measures the rendered
size, including scaled tables and SVG labels.

Avoid italics in slides, including subtitles, captions, metadata, footnotes, and
emphasis. Use plain text, bold, color, spacing, or shorter wording.

Avoid muted footer lines that carry content (sample sizes, survey timing, data
notes). Make the content a bullet, or move it to an appendix slide.

Text should sit balanced in the space below the title rather than bunched at the
top; for sparse slides use the `v-center` layout (see gotchas.md on `flex: 1`
blocks).

Use bold short bullet lead-ins selectively for longer bullets. The lead-in is
usually two to four words followed by a dash and normal-weight explanation.
Bold only the lead-in phrase, not the whole sentence.

Most slide bullets and short slide sentences should not end with full stops.
Keep terminal punctuation for multi-sentence bullets, quotations, footnotes,
appendix text, or places where it materially improves readability.

Spell out jargon before using it, or replace it with plain English. Prefer
"reweighting on observed study features" over "IPW", "funding shock" over
"Bartik IV", and "reported standard error" over "SE" on main slides. The style
check flags acronyms used before they are spelled out; list ones this audience
knows in `data-known-acronyms` on `.ws-deck`.

Avoid dense nested bullets on main slides. Use one short bullet per idea and
move subcases to reveals or appendices.

One title per slide: no eyebrow or kicker labels above the h2 (a "double
header" leaves the listener unsure which line to read). Put signposting in the
title itself, e.g. "1. Attendance rises sharply", or on a separate
roadmap slide.

Use appendix buttons as escape valves when a claim needs a table, construction
formula, full specification, or caveat. When several bullets each have their own
appendix detail, the button tends to work best inline at the end of its bullet
rather than in a row of buttons in the corner, and each such bullet should say
what was done and what it found, not only name the check.

Contribution slides tend to land best as bullets, not a table: a field label, a
few-word description of what that literature does, a generous citation line,
then a `→` line saying what this paper adds. Credit the prior work before
claiming the addition, and let what the reader can now infer carry the claim
rather than dataset size or priority.

## Reference Deck And Figure Busyness

The reference layout: Lato headings without the title underline, larger type (h2 about 46px, body 28px), a figure with two or three short right-hand notes, one left-bordered takeaway per slide, and appendix buttons bottom-right. Avoid teal green as the central colour; pick an accent that clashes with no data colour in the deck. A navy accent alone reads as too grey: colour bold text, kickers and → arrows with a lighter tint of the accent (for example `#3D6390` on a `#2C3E5A` accent), keeping data colours on bold words that name a figure's series. Avoid crimson, olive, ochre, yellow and brown as data colours; rose `#D1495B`, deep teal `#00798C`, soft purple `#7E57C2` and terracotta `#C8553D` work well.

Figures and slides tend to come out too busy. Before finishing, check every reveal step for overlapping labels, legends that could be direct labels, and text inside the figure that the speaker could say instead. Where a figure carries several ideas, either cut elements or add steps: one new idea per step, and fade (or hide) annotations and series that are no longer the point of that step. Keep axes and axis titles muted and small, and data labels short and next to the mark they name. For policy audiences, coefficient plots tend to work best stripped down: the estimates printed beside each row, a line at zero, no axis ticks, tick labels or gridlines, and units in a short footnote. When several arms share one plot, reveal one arm per step and dim the others.

## Bullet Discipline

Bullets are headlines, not sentences. Strip articles, hedges, and connective tissue until each line scans in one glance. If a bullet runs past one line, cut before adding sub-bullets. First drafts tend to run too dense: lean lightly towards less, with about four short bullets per slide, and leave confidence intervals and secondary numbers to the chart, a small note, or the appendix. Tight `claim ⇒ implication` pairs land harder than full sentences: `Biased beliefs ⇒ under-investment`; `Correct beliefs but still no take-up ⇒ credit constraints`; `Avoids survey-demand / experimenter-effect confounds`.

The style check warns above 70 words visible at once and above 20 words per
bullet (the densest slides of well-received decks reach about 69 words and
19-word bullets).

Mark a bullet's rhetorical role with a bold, accent-coloured lead-in label followed by a spaced em-dash — the question being asked, the hypothesis, the setting, a step in the design. Pick the labels that fit the deck rather than enforcing a fixed vocabulary, and reuse them consistently within a deck. For example: `Hypothesis — information gaps limit take-up`; `Setting — cash-transfer RCT in rural villages`.

Use a leading `→` (or `⇒`) for the so-what line beneath an evidence block, citation list, or two-premise argument. It marks the contribution or implication, not just another bullet, so reserve it for that role: `→ Tests whether households act on information they cannot verify`; `Higher take-up with reminders than without ⇒ inattention`.

## Reveal Style

Default to one new bullet per reveal step. A five-bullet slide expands into roughly five steps. Group a parent bullet with its sub-bullets in a single step only when the sub-bullets are immediate evidence for a claim the parent has just made — drip-feeding evidence the audience could already evaluate is worse than dumping it.

Use simultaneous gradual reveal when a diagram or chart has accompanying
annotation text: reveal the relevant visual element and the explanatory bullet
in the same `data-step`.

For gradual reveals of charts, often show the visual frame first: axes, labels,
gridlines, reference lines, and contextual annotations. Then reveal the data
marks on the next step. This works especially well for histograms, scatterplots,
and forest plots.

Avoid making the audience read ahead or guess which part of a graphic changed.
The reveal sequence should walk them through the visual.

When a slide pairs a figure with text the figure does not show (for example
implementation facts beside an outcome plot), keep the whole figure, axes
included, hidden until its turn, and dim the earlier text once the figure
appears; an empty frame beside unrelated text reads as if the two belong
together. In generated SVGs, put the frame group on a later `data-step` too.

When a slide holds more than one chart, reveal them one at a time and series by
series: the first chart's first series, then its comparison series, and only
then the second chart (kept fully hidden until its turn), again series by series.

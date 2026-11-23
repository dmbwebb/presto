# Presto design

Presto's look was redesigned on 28 October 2026 from the approved mockups in
`presto/design/mockups-2026-10-28/` (open `index.html`; the PNGs are crops of
each screen). This file explains the design in words. Read it before changing
anything a person sees, and update it when a change alters the design.

## Principles

- **Light only.** The app forces the light (aqua) appearance. There is no dark
  mode and no black chrome anywhere. Black appears only as content: the
  black-out screen during a talk and photo slides.
- **The slide is the hero.** Every surface frames the slide the way the
  audience will see it: the deck's section bar on the slide's top edge, its
  footer on the bottom edge, both exactly the slide's width, at every window
  shape. Chrome sits around the slide, never on it.
- **One palette.** Every Presto surface uses the `--p-*` variables in
  `presto/ui-tokens.css`: a pale grey canvas (`#E7EAEE`), a lighter panel tone
  (`#F3F4F6`) for the rail and sidebar, white surfaces for cards, popovers and
  the inspector, hairline borders, ink `#1C2532`, one blue accent (`#2F6BD8`)
  for selection, the current place and the primary action, red for live and
  destructive, green for saved and ahead of pace, amber for behind pace.
  Surfaces injected into a deck page use the variables with literal fallbacks
  and never style slide content.
- **Type.** The system font (SF) for controls; New York for deck titles in
  Decks and the toolbar; tabular figures wherever numbers change (clock,
  counters, zoom).
- **Depth.** Slides carry a soft two-layer shadow on the canvas. Popovers and
  the format bar carry `--p-shadow-pop`; cards carry a hairline plus a faint
  shadow. Corners are 12px on cards and 7px on controls.
- **Quiet text.** No taglines or helper sentences where the layout can say it.

## One window, three modes

The main window holds Decks, Edit and Present, switched by the segmented
control at the left of the native toolbar or ⌘1, ⌘2, ⌘3. Edit and Present are
disabled until a deck is open. While a talk is live the Decks and Edit
segments, ⌘1, ⌘2 and ⌘W are disabled; End in the toolbar or Escape twice ends
the talk and returns to the mode and the slide and step you came from.

The toolbar changes with the mode:

- **Decks:** mode control, search field, Open….
- **Edit:** rail toggle, mode control, the deck title in New York with the save
  status beside it, Undo, Redo, Text box, Export PDF, inspector toggle, and
  the blue Play from here button (⌘↩).
- **Present:** mode control, a centred status ("Ready", or a red dot with
  "Live on <display>"), Swap screens, Black out (B), End.

## Decks

The window opens here when no deck is open. A sidebar lists Recent, Pinned,
the projects from the WebSlides deck registry with counts, added folders, Add
folder…, and the engine's kitchen-sink deck. The main area starts with a
Continue panel for the last deck: its first slide, title in New York, project
and last edit, slide and step counts, "You left off on slide 18, <title>" with
a small picture of that slide, Continue editing (primary) and Present from
start. Below it, a grid of the other decks sorted by last edited. Each card
shows the deck's real first slide (or its title on a white 16:9 card until a
thumbnail exists, at the same size so nothing moves), a short name taken from
the occasion part of the deck's title, and the project and edit date. Hover or
focus shows Edit and Present on the card; a right click offers Reveal in
Finder, Pin and Remove from Recent. Arrow keys move between cards, Return
edits, ⌘Return presents. Dropping a deck folder or `index.html` anywhere on the
window opens it in Edit.

## Edit

- **Rail** (232px, panel tone, left): slides grouped under the deck's own
  sections, each with a foldable header and its slide range, the appendix
  last. Numbers sit beside the thumbnails, not on them; small dots mark reveal
  steps; the current slide has a 2.5px accent ring. Thumbnails are live copies
  of the slides, scaled with transforms, and refresh after every edit.
- **Canvas:** the pale grey canvas with the slide centred on its frame and
  shadow. A step strip sits under the slide (Reveal 0..n as small buttons, the
  current one filled with the accent, and the "Show later steps faintly"
  switch), and a zoom label sits at the bottom right.
- **Selection:** a 1.5px accent outline with white square handles. A white
  format bar appears beside the selection without covering it and shows only
  the controls for what is selected: text styling, theme colour swatches and a
  colour picker for text; box background swatches for boxes; Edit TeX for an
  equation; an "Appears" chip for every element that says when it is revealed
  and changes it.
- **Equations** edit in place: a popover with the TeX source above a live
  preview; errors show inline and keep Apply disabled; ⌘↩ applies; Escape
  cancels.
- **Links** (appendix buttons and other in-deck links): a click selects the
  link and opens a card with the target slide's picture and title, Go to
  slide, Edit label and Change target… (a searchable list, appendix first).
  ⌘-click goes straight to the target. After any jump a white "Back to 18 ·
  <title>" chip at the top of the canvas returns to the slide and step you
  came from; it stays until you go back or move on twice.
- **Inspector** (right, white, hidden by default; ⌥⌘I, or ⇧⌘X for the
  source): the slide's HTML with light syntax highlighting and real file line
  numbers, a visible "Not saved" state with the error and Try again, and
  Refine with AI pinned at the foot (⌘J focuses it, ⌘↩ sends), with the
  number of earlier refinements and Undo last.
- **Undo:** one ⌘Z history covers visual edits, committed source edits and
  Refine; menu titles say what will be undone ("Undo Typing", "Undo Refine").
  While text is being typed, ⌘Z undoes the typing step by step.
- **Escape** closes a popover, then ends typing, then deselects, then leaves
  the overview, then goes back from a link jump. It never leaves the editor.

## Present

Present opens the console in a ready state: the deck title, a large Start
button, the talk length (minutes, stored per deck by Presto, never in the
deck), and the audience display, or a quiet note that Start fills this screen
when there is only one.

- **Two screens, like PowerPoint:** the audience display shows only the slide,
  covering the whole display with no title bar, menu bar or Dock; the laptop
  shows the console: the current slide large, framed as the audience sees it
  (section bar, footer and progress line), its number and title, the step
  dots, the clock card (elapsed time in large tabular figures, "of 45:00", a
  progress bar with a pace marker, "Ends at", and "n min ahead of pace" in
  green or "behind pace" in amber) and the Sections card (finished sections
  ticked, the current one highlighted with "k of m", the appendix last; click
  to jump). There are no speaker notes and no next-slide preview. Reset needs
  a second click. Unplugging the projector keeps the console live; plugging
  it back restores the audience view.
- **One screen:** Start makes the window full screen showing only the slide.
  The slide sits at the top of the screen (on both the one-screen and the
  audience display), so a display taller than 16:9 leaves its spare height
  below the slide, never above the section bar; that space matches the slide's
  own background. Moving the
  pointer shows a small light control bar (previous, number, next, time,
  slide grid, edit this slide, End) that hides after 2.5 seconds; T toggles a
  corner timer.
- **Ending:** Escape first closes the slide grid if it is open; otherwise it
  shows "Press Escape again to end the presentation" as a light toast (above
  the black screen too; on one screen at the bottom centre, just above the
  control bar, so it never covers the section bar or the slide title), and a
  second Escape within two seconds ends the talk. While live, ⌘Q also needs a second press. On one screen the menu bar
  and Dock stay hidden for the whole show.

## Icon

A light icon: a white slide on a pale surface with a small accent mark, so it
reads well in the Dock beside Portal and Clear.

# WebSlides → Editable PowerPoint

Use this when collaborators edit in PowerPoint. The export is one-way: once they edit the
file, that PowerPoint is the master for that talk.

```bash
python3 ~/.claude/skills/webslides/scripts/export_pptx.py <deck-alias> --output talk.pptx
python3 ~/.claude/skills/webslides/scripts/export_pptx.py <claude-slides-project-dir> --output talk.pptx
```

Engine code: `tools/export_pptx.py` and `tools/pptx_export/` in the WebSlides repo. Regression
check: `~/.venvs/webslides-export/bin/python tests/pptx_export_check.py`.

## What becomes what

- Each deck slide becomes one PowerPoint slide. Headings are real title placeholders.
- Text and bullets are native text boxes. A painted box with one text block is one shape.
- HTML tables are native tables. Rows revealed at different steps become separate tables.
- SVG figures become grouped native shapes with editable text.
- Dot-and-whisker rows and horizontal bars with numeric labels become real charts. Edit Data
  opens a sheet: changing an estimate or interval moves the mark, but the label beside it is typed
  text and must be edited by clicking on it.
- Each reveal step is one click, using Appear, Disappear and Transparency. List items reveal
  paragraph by paragraph.
- A step where new content replaces a large block (10% of the slide or more) starts a new
  PowerPoint slide. Small swapped cards stay on the slide, stacked, with exit and entrance effects.
- Deck fonts are embedded, and theme fonts and colours are set from the deck.
- Appendix buttons, Back buttons, speaker notes, footers and live slide numbers are kept.

## Limits

- PowerPoint can only animate a group as a whole, so objects are grouped only if they reveal together.
- Not exported: the top section navigation, box shadows, hover effects and `data-morph` motion.
- Video, iframes, canvas and very complex SVGs (over 1,500 nodes) become pictures.
- Maths becomes editable text with sub- and superscripts, not PowerPoint equations.
- Chart labels are typed text on purpose: "Value From Cells" labels (`CELLRANGE` fields) printed as the literal `[CELLRANGE]` in PowerPoint for Mac's PDF output.
- Claude Slides images are stored as `/_blob/<hash>`: list them in `assets/blob_map.json` beside
  the `project/` folder, as `{"<hash>": "file.png"}`.
- Known bug, fix proposed in `charts.py`: `_dot_marks` also accepts a from-to row (two dots joined by a
  line with an arrowhead) and a partial set of rows, and writes wrong numbers to the Edit Data sheet. After
  every export, compare each embedded workbook with the slide. Until fixed, make the row's line colour
  differ from its marker's by one hex step: the figure then exports as grouped shapes.
- Each SVG `<text>` line becomes its own text box, and side notes stay separate boxes that do not push each
  other down. For a file collaborators will edit, tidy this after export with a small deck-local
  post-processing script (python-pptx) that merges split labels, names graph pieces for the Selection Pane,
  and combines side notes into one box with by-paragraph animation (retarget `p:spTgt` to `p:txEl/p:pRg`,
  and set `build="p"` in `p:bldLst`).

## Verifying

Open the file in real PowerPoint: LibreOffice substitutes fonts and misplaces text. Script
PowerPoint with the file inside `~/Library/Containers/com.microsoft.Powerpoint/Data/` (no sandbox
prompt), launch it with `open -g -j`, save a PDF, and also save a copy as .pptx. Compare the PDF
with the web slides, and confirm the saved copy has the same animation effects on every slide.
For AppleScript saves, pass the destination as `(POSIX file "/absolute/path/check.pdf")`
and use the presentation's exact name, including `.pptx`. Passing a POSIX path as plain
text returned success without creating the PDF; the file object produced it (18 Nov 2026).
Scripting PowerPoint over SSH on another Mac can hang (osascript calls time out); run it locally.

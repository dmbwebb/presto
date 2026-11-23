# WebSlides Tables Guide

Working with regression/results tables in WebSlides decks. Covers converting LaTeX tables, fixing common issues, and hand-crafting HTML tables.

## LaTeX Table Converter

Convert LaTeX `.tex` table files (from Stata/R output or hand-written) to WebSlides HTML:

```bash
python3 <engine>/tools/latex_table.py table.tex [options]
```

**Presentation flags:**
- `--merge-header` — collapse multi-row headers into single cells with `<br>`
- `--coef-se` — merge coefficient + SE row pairs into single cells
- `--compact` — tighter padding for dense tables
- `--font-size 16px` — override size
- `-o file.html` — write to file instead of stdout

**Overlay flags** (1-indexed rows/cols):
- `--highlight-col 2:1` — highlight column 2 at step 1
- `--highlight-row 3:2` — highlight row 3 at step 2
- `--highlight-cell 1,2:1` — highlight cell at step 1
- `--dim-col 2:2` — dim column 2 at step 2
- `--step-row 6:2` — reveal row 6 at step 2 (hidden before)
- `--step-col 3:1` — reveal column 3 at step 1
- Step ranges: `2-4` (visible only during steps 2-4)

**Supports:** `\begin{tabular}`, `tabularx`, booktabs (`\toprule`/`\midrule`/`\bottomrule`/`\cmidrule`), `\hline`, `\multicolumn`, `\tabularnewline`, `\num{}` (siunitx), raw rows without `\begin{tabular}` (e.g. iebaltab output). Auto-wraps significance stars in `<span class="stars">` and SEs in `<span class="se">`.

**Caveat:** Raw rows without `\begin{tabular}` AND without column headers (e.g. just data rows with `&` separators) may not parse correctly — the converter needs column structure to build `<thead>`. For these, write the HTML table manually.

**Test deck:** `examples/latex-table-test/index.html`

### Flattened table bug

When the LaTeX source has a **header wrapper** (column names defined in `main.tex`, not in the generated `.tex` file), `latex_table.py` produces a "flattened" table: all rows packed into a single `<thead>` row using `<br>` tags. Use `unflatten_table.py` to fix these.

## Unflatten Table

Fix tables where `latex_table.py` packed all rows into a single `<thead>` row using `<br>` tags.

```bash
# Detect flattened tables in a deck
python3 <engine>/tools/unflatten_table.py --detect decks/<deck>/tables/

# Unflatten with column headers
python3 <engine>/tools/unflatten_table.py tables/broken.html \
    --headers "Col A" "Col B" "Col C"

# With spanning group headers
python3 <engine>/tools/unflatten_table.py tables/broken.html \
    --headers "Col A" "Col B" "Col C" "Col D" \
    --groups "Group 1:2" "Group 2:2"

# Balance tables (mean/SE pairs + trailing p-value columns)
python3 <engine>/tools/unflatten_table.py tables/balance.html \
    --headers "Total" "Control" "Treat" "P-val" --balance 2
```

How it works: splits each `<th>` cell on `<br>`, merges SE elements (`<span class="se">...`) with preceding coefficients, then reconstructs as proper `<thead>` + `<tbody>`.

### Finding missing column headers

When column headers are in the LaTeX wrapper (not the generated `.tex` file), the HTML table will have no descriptive headers. To find them: grep the Overleaf project for a unique value from the table (e.g. the observation count `1,963`) to identify the source `.tex` file, then search for `\input{that_file}` to find the wrapper with `\multicolumn` and column name definitions.

### Double-dash negatives

LaTeX sources sometimes encode negative signs as `--` (en-dash). After converting or unflattening, check for `--0.` in the output HTML and replace with `-0.`. Quick check: `grep -l '\-\-0\.' decks/<deck>/tables/*.html`.

### Common multicolumn bug

Another `latex_table.py` failure mode: a `\multicolumn` row gets a `colspan` spanning ALL columns, then the actual column names appear as EXTRA `<th>` cells overflowing the table. Fix: replace the broken row with proper `<th>` cells matching the column count.

## Regression Table Pattern (Manual)

For hand-crafted HTML tables (not from LaTeX), use this pattern:

```html
<table>
    <thead>
        <tr>
            <th></th>
            <th data-highlight="1">Model 1</th>
            <th data-highlight="2">Model 2</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td>Treatment</td>
            <td data-highlight="1">0.15<span class="stars">**</span><br><span class="se">(0.06)</span></td>
            <td data-highlight="2">0.12<span class="stars">*</span><br><span class="se">(0.07)</span></td>
        </tr>
    </tbody>
</table>
```

## Table Slide Layouts

### Table + Commentary (table on one side, text on the other)
```html
<section class="slide content">
    <h2>Title</h2>
    <div class="table-commentary table-left">   <!-- 3:2 split, table on left -->
    <div class="table-commentary table-right">   <!-- 2:3 split, table on right -->
    <div class="table-commentary">               <!-- equal width -->
        <div class="table-container" data-table-src="tables/table.html"></div>
        <div class="commentary">
            <ul>
                <li>Commentary bullet</li>
                <li data-step="1">Revealed commentary</li>
            </ul>
        </div>
    </div>
</section>
```

**Dense tables (6+ columns):** Add `wide-table` class for a 7:3 split: `<div class="table-commentary table-left wide-table">`. For even wider, use inline `style="grid-template-columns: 4fr 1fr"` or drop commentary below.

**Vertically centered commentary:** Add `commentary-center` to vertically center it.

## External Table Loading

The engine natively handles `data-table-src` — no per-deck loader script needed:

```html
<div class="table-container" data-table-src="tables/table1a.html"></div>
```

## Table Overlays (`data-overlays`)

Draw highlight boxes around table columns or rows, synced to reveal steps. Add a `data-overlays` JSON attribute to the `.table-container` element:

```html
<div class="table-container" data-table-src="tables/results.html"
     data-overlays='[{"cols":[1],"step":"1"}, {"rows":[2,3],"step":"2","class":"blue"}]'></div>
```

Each entry in the array creates one overlay box:

| Key | Value | Description |
|-----|-------|-------------|
| `cols` | `[0]`, `[1,2]` | 0-indexed column indices (from the `thead tr` with the most `th` cells — safe with colspan group headers). Multiple = one spanning box. |
| `rows` | `[0]`, `[1,2]` | 0-indexed `tbody` row indices. Multiple = one spanning box. |
| `step` | `"1"`, `"0"`, `"0-0"` | Reveal step. `"0"` = always visible. Ranges work: `"0-0"` = visible only at step 0, `"1-2"` = visible steps 1–2 only. |
| `class` | `"blue"` | Optional extra CSS class. Default border is orange; `"blue"` gives teal. |

Use `cols` or `rows` per entry (not both). CSS classes: `.column-overlay` (default orange), `.column-overlay.blue` (teal), `.column-overlay.accent` (burnt sienna), `.column-overlay.red`, `.column-overlay.green`.

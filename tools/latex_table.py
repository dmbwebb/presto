#!/usr/bin/env python3
"""Convert LaTeX tables to WebSlides-compatible HTML with overlay support.

Usage:
    python3 tools/latex_table.py table1a.tex
    python3 tools/latex_table.py table1a.tex --highlight-col 2:1 --dim-col 2:3 --highlight-col 3:2
    python3 tools/latex_table.py table1a.tex --highlight-row 1:1 --step-row 5:2
    python3 tools/latex_table.py table1a.tex --highlight-cell 1,2:1 --dim-cell 1,2:3
    python3 tools/latex_table.py table1a.tex -o tables/table1a.html

Overlay flags (all use 1-indexed row/col numbers, matching what you see):
    --highlight-col  COL:STEP[-END]     Highlight entire column at step
    --highlight-row  ROW:STEP[-END]     Highlight entire row at step
    --highlight-cell ROW,COL:STEP[-END] Highlight specific cell at step
    --dim-col        COL:STEP[-END]     Dim entire column at step
    --dim-row        ROW:STEP[-END]     Dim entire row at step
    --dim-cell       ROW,COL:STEP[-END] Dim specific cell at step
    --step-row       ROW:STEP[-END]     Reveal row at step (hidden before)
    --step-col       COL:STEP[-END]     Reveal column at step (hidden before)

    STEP can be "2" (step 2+) or "2-4" (steps 2 through 4 only).

Other flags:
    -o, --output FILE   Write HTML to file instead of stdout
    --font-size SIZE    Override table font-size (e.g., "16px", "0.9em")
    --compact           Reduce cell padding for dense tables
    --no-stars          Don't wrap significance stars in <span class="stars">
    --no-se             Don't wrap standard errors in <span class="se">
    --caption TEXT       Add a caption above the table
    --merge-header      Merge multi-row headers into single cells with <br>
    --coef-se           Merge coefficient + SE rows into single cells with <br>
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# LaTeX parsing helpers
# ---------------------------------------------------------------------------

def strip_latex_wrappers(tex: str) -> str:
    """Remove outer wrappers that aren't part of the tabular content."""
    # Remove comments (but keep % in \%)
    tex = re.sub(r'(?<!\\)%.*', '', tex)

    # Strip \centering
    tex = tex.replace(r'\centering', '')

    # Strip \resizebox{...}{...}{ ... } — extract inner content
    tex = re.sub(r'\\resizebox\s*\{[^}]*\}\s*\{[^}]*\}\s*\{', '', tex)

    # Strip \adjustbox{...}{ ... }
    tex = re.sub(r'\\adjustbox\s*\{[^}]*\}\s*\{', '', tex)

    # Strip threeparttable wrapper
    tex = re.sub(r'\\begin\{threeparttable\}.*?\n', '', tex)
    tex = re.sub(r'\\end\{threeparttable\}', '', tex)

    # Strip tablenotes
    tex = re.sub(r'\\begin\{tablenotes\}.*?\\end\{tablenotes\}', '', tex, flags=re.DOTALL)

    # Strip \begin{table}...\end{table} wrapper but keep inner content
    tex = re.sub(r'\\begin\{table\}\s*(\[.*?\])?\s*', '', tex)
    tex = re.sub(r'\\end\{table\}', '', tex)

    # Strip \caption{...} and \label{...}
    tex = re.sub(r'\\caption\{[^}]*\}', '', tex)
    tex = re.sub(r'\\label\{[^}]*\}', '', tex)

    # Balance braces: remove trailing } that matched stripped \resizebox{ etc.
    # Count unmatched closing braces
    depth = 0
    result = []
    for ch in tex:
        if ch == '{':
            depth += 1
            result.append(ch)
        elif ch == '}':
            if depth > 0:
                depth -= 1
                result.append(ch)
            # else: skip unmatched closing brace
        else:
            result.append(ch)
    tex = ''.join(result)

    return tex.strip()


def extract_tabular(tex: str) -> tuple[str, str]:
    """Extract the tabular environment and its column spec.

    Returns (col_spec, body) where body is everything between
    \\begin{tabular}[opts]{spec} and \\end{tabular}.
    """
    # Brace-balanced extractor for the column-spec group, so p{4.5cm} doesn't
    # truncate at the inner }.
    def _balanced_col_spec(match_end: int) -> tuple[str, int] | None:
        i = match_end
        while i < len(tex) and tex[i] in ' \t\n':
            i += 1
        if i >= len(tex) or tex[i] != '{':
            return None
        i += 1
        start = i
        depth = 1
        while i < len(tex) and depth > 0:
            if tex[i] == '{':
                depth += 1
            elif tex[i] == '}':
                depth -= 1
                if depth == 0:
                    return tex[start:i], i + 1
            i += 1
        return None

    def _skip_optional_position(start: int) -> int:
        i = start
        while i < len(tex) and tex[i] in ' \t\n':
            i += 1
        if i >= len(tex) or tex[i] != '[':
            return start
        close_idx = tex.find(']', i + 1)
        if close_idx == -1:
            return start
        return close_idx + 1

    col_spec = None
    start = None
    for m in re.finditer(r'\\begin\{(tabular\*?|tabularx)\}', tex):
        env = m.group(1)
        if env == 'tabular':
            balanced = _balanced_col_spec(_skip_optional_position(m.end()))
        else:
            width = _balanced_col_spec(m.end())
            if width is None:
                continue
            balanced = _balanced_col_spec(_skip_optional_position(width[1]))

        if balanced is not None:
            col_spec, start = balanced
            break

    if col_spec is None:
        # No tabular environment — treat entire content as raw rows.
        # Infer column count from the first row with & separators.
        body = tex.strip()
        first_row = body.split('\n')[0]
        # Count & at depth 0
        ncols = 1
        depth = 0
        for ch in first_row:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == '&' and depth == 0:
                ncols += 1
        col_spec = 'l' + 'c' * (ncols - 1)
        return col_spec, body

    # Find matching \end{tabular}
    end_pattern = r'\\end\{tabular\*?\}|\\end\{tabularx\}'
    end_m = re.search(end_pattern, tex[start:])
    if not end_m:
        raise ValueError("No \\end{tabular} found")

    body = tex[start:start + end_m.start()]
    return col_spec, body


def parse_col_spec(spec: str) -> list[str]:
    """Parse LaTeX column specification into list of alignments.

    Handles: l, c, r, p{...}, S, d, D{}{}{}, X, @{}, |, *{n}{spec}
    Returns list like ['l', 'c', 'c', 'r', 'c', 'c']
    """
    def find_required_char(char: str, start: int) -> int:
        try:
            return spec.index(char, start)
        except ValueError as exc:
            raise ValueError(f"Malformed column specification near '{spec[start:]}'") from exc

    def read_balanced_group(start: int) -> tuple[str, int]:
        open_idx = find_required_char('{', start)
        depth = 1
        i = open_idx + 1
        while i < len(spec):
            if spec[i] == '{':
                depth += 1
            elif spec[i] == '}':
                depth -= 1
                if depth == 0:
                    return spec[open_idx + 1:i], i + 1
            i += 1
        raise ValueError(f"Malformed column specification near '{spec[start:]}'")

    alignments = []
    i = 0
    while i < len(spec):
        ch = spec[i]
        if ch in 'lcrXSd':
            alignments.append({'l': 'left', 'c': 'center', 'r': 'right',
                              'X': 'left', 'S': 'right', 'd': 'right'}[ch])
            i += 1
        elif ch == 'D':
            # D{.}{.}{n} — decimal column
            alignments.append('right')
            # Skip three brace groups
            for _ in range(3):
                _group, i = read_balanced_group(i)
        elif ch == 'p':
            # p{width}
            alignments.append('left')
            _group, i = read_balanced_group(i)
        elif ch == '*':
            # *{n}{spec} — repeat
            i += 1
            count_spec, i = read_balanced_group(i)
            try:
                n = int(count_spec)
            except ValueError as exc:
                raise ValueError(f"Malformed column specification near '*{{{count_spec}}}'") from exc
            inner_spec, i = read_balanced_group(i)
            alignments.extend(parse_col_spec(inner_spec) * n)
        elif ch in '@!<>':
            # @{decl} or !{decl} — skip
            _group, i = read_balanced_group(i)
        elif ch == '|' or ch == ' ':
            i += 1
        else:
            i += 1
    return alignments


def parse_rows(body: str) -> list[str]:
    """Split tabular body into rows, handling \\\\ and \\tabularnewline."""
    # Normalize line endings
    body = body.replace('\r\n', '\n')

    # Split on \\ (with optional [spacing]) or \tabularnewline
    # Be careful not to split on \\ inside braces
    rows = []
    current = []
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '{':
            depth += 1
            current.append(ch)
            i += 1
        elif ch == '}':
            depth -= 1
            current.append(ch)
            i += 1
        elif depth == 0 and body[i:].startswith('\\tabularnewline'):
            rows.append(''.join(current))
            current = []
            i += len('\\tabularnewline')
        elif depth == 0 and body[i:i+2] == '\\\\':
            rows.append(''.join(current))
            current = []
            i += 2
            # Skip optional [spacing] after \\
            rest = body[i:].lstrip()
            if rest.startswith('['):
                try:
                    end_bracket = rest.index(']')
                except ValueError as exc:
                    raise ValueError("Malformed row spacing after \\\\: missing closing ]") from exc
                i = body.index(']', i) + 1
        else:
            current.append(ch)
            i += 1

    # Last row (if no trailing \\)
    last = ''.join(current).strip()
    if last:
        rows.append(last)

    return [r.strip() for r in rows if r.strip()]


def parse_cells(row: str) -> list[str]:
    """Split a row into cells on &, respecting brace depth."""
    cells = []
    current = []
    depth = 0
    backslash_run = 0
    for ch in row:
        if ch == '{':
            depth += 1
            current.append(ch)
            backslash_run = 0
        elif ch == '}':
            depth -= 1
            current.append(ch)
            backslash_run = 0
        elif ch == '&' and depth == 0 and backslash_run % 2 == 0:
            cells.append(''.join(current).strip())
            current = []
            backslash_run = 0
        else:
            current.append(ch)
            if ch == '\\':
                backslash_run += 1
            else:
                backslash_run = 0
    cells.append(''.join(current).strip())
    return cells


# ---------------------------------------------------------------------------
# LaTeX → HTML content conversion
# ---------------------------------------------------------------------------

SAFE_ENTITY_RE = re.compile(r'&(lt|gt|le|ge|ne|times|amp|nbsp);')
SAFE_HTML_SENTINEL = "\0WS_SAFE_HTML_"
GENERATED_HTML_SENTINEL = "\0WS_GENERATED_HTML_"


def escape_preserving_generated_html(text: str, generated_html: list[str] | None = None) -> str:
    """Escape text content while preserving tags/entities generated by this tool."""
    protected = []

    def protect(pattern: re.Pattern, value: str) -> str:
        def repl(match):
            protected.append(match.group(0))
            return f"{SAFE_HTML_SENTINEL}{len(protected) - 1}\0"
        return pattern.sub(repl, value)

    text = protect(SAFE_ENTITY_RE, text)
    text = html.escape(text, quote=False)

    for idx, value in enumerate(protected):
        text = text.replace(f"{SAFE_HTML_SENTINEL}{idx}\0", value)
    if generated_html:
        for idx, value in enumerate(generated_html):
            text = text.replace(f"{GENERATED_HTML_SENTINEL}{idx}\0", value)
    return text


def protect_inline_math(text: str) -> tuple[str, list[str]]:
    """Temporarily protect inline math from non-math LaTeX command cleanup."""
    segments = []

    def repl(match):
        segments.append(match.group(0))
        return f"__WS_MATH_{len(segments) - 1}__"

    return re.sub(r'(?<!\\)\$[^$]+(?<!\\)\$', repl, text), segments


def restore_inline_math(text: str, segments: list[str]) -> str:
    for idx, value in enumerate(segments):
        text = text.replace(f"__WS_MATH_{idx}__", value)
    return text


def convert_cell_content(text: str, wrap_stars: bool = True, wrap_se: bool = True) -> str:
    """Convert LaTeX cell content to HTML."""
    text = text.strip()
    if not text:
        return ''

    generated_html = []

    def generated(value: str) -> str:
        generated_html.append(value)
        return f"{GENERATED_HTML_SENTINEL}{len(generated_html) - 1}\0"

    # \num{...} → just the number
    text = re.sub(r'\\num\{([^}]*)\}', r'\1', text)

    # \multicolumn handling is done at the row level, not here

    # \textit{...} → <em>...</em>
    text = re.sub(r'\\textit\{([^}]*)\}', lambda m: f'{generated("<em>")}{m.group(1)}{generated("</em>")}', text)

    # \textbf{...} → <strong>...</strong>
    text = re.sub(r'\\textbf\{([^}]*)\}', lambda m: f'{generated("<strong>")}{m.group(1)}{generated("</strong>")}', text)

    # \textsc{...} → <span style="font-variant:small-caps">...</span>
    text = re.sub(
        r'\\textsc\{([^}]*)\}',
        lambda m: generated('<span style="font-variant:small-caps">') + m.group(1) + generated('</span>'),
        text
    )

    # \emph{...} → <em>...</em>
    text = re.sub(r'\\emph\{([^}]*)\}', lambda m: f'{generated("<em>")}{m.group(1)}{generated("</em>")}', text)

    # \underline{...}
    text = re.sub(r'\\underline\{([^}]*)\}', lambda m: f'{generated("<u>")}{m.group(1)}{generated("</u>")}', text)

    # Math mode: $...$  →  content (strip $)
    # Handle simple inline math
    text = re.sub(r'\$<\$', '&lt;', text)
    text = re.sub(r'\$>\$', '&gt;', text)
    text = re.sub(r'\$\\leq\$', '&le;', text)
    text = re.sub(r'\$\\geq\$', '&ge;', text)
    text = re.sub(r'\$\\neq\$', '&ne;', text)
    text = re.sub(r'\$\\times\$', '&times;', text)
    text = re.sub(r'\$([^$]+)\$', r'$\1$', text)  # Keep KaTeX-compatible math
    text, math_segments = protect_inline_math(text)

    # \phantom{...} → empty
    text = re.sub(r'\\phantom\{[^}]*\}', '', text)

    # \hspace{...}, \vspace{...} → empty
    text = re.sub(r'\\[hv]space\*?\{[^}]*\}', '', text)

    # \quad, \qquad, \; \, \! → space
    text = re.sub(r'\\(?:quad|qquad|;|,|!)', ' ', text)

    # Significance stars: detect patterns like *** ** *
    # Must come before general cleanup
    if wrap_stars:
        text = re.sub(
            r'(\*{1,3})(?!\*)',
            lambda m: generated('<span class="stars">') + m.group(1) + generated('</span>'),
            text
        )

    # Standard errors in parentheses: (0.123) pattern
    # Must contain a decimal point to distinguish from column numbers like (1)
    if wrap_se:
        text = re.sub(
            r'\((\s*[\d,]*\.[\d]+\s*)\)',
            lambda m: generated('<span class="se">') + '(' + m.group(1) + ')' + generated('</span>'),
            text
        )

    # Clean up remaining LaTeX commands we don't need
    text = text.replace(r'\%', '%')
    text = text.replace(r'\$', '$')
    text = text.replace(r'\&', '&amp;')
    text = text.replace(r'\#', '#')
    text = text.replace(r'\_', '_')
    text = text.replace(r'\{', '{')
    text = text.replace(r'\}', '}')
    text = text.replace('~', '&nbsp;')

    # Remove remaining unknown commands (but keep their arguments)
    text = re.sub(r'\\[a-zA-Z]+\*?(?:\{([^}]*)\})?', lambda m: m.group(1) or '', text)
    text = restore_inline_math(text, math_segments)

    # Clean up extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    return escape_preserving_generated_html(text, generated_html)


# ---------------------------------------------------------------------------
# Row classification
# ---------------------------------------------------------------------------

class HRule:
    """Represents a horizontal rule (hline, toprule, midrule, bottomrule, cmidrule)."""
    def __init__(self, kind: str, col_start: int = 0, col_end: int = 0):
        self.kind = kind  # 'top', 'mid', 'bottom', 'cmidrule', 'hline'
        self.col_start = col_start  # 1-indexed, for cmidrule
        self.col_end = col_end


class DataRow:
    """A row of cell data, possibly with multicolumn cells."""
    def __init__(self, cells: list, is_header: bool = False):
        self.cells = cells  # list of Cell objects
        self.is_header = is_header


class Cell:
    """A single table cell."""
    def __init__(self, content: str, colspan: int = 1, align: str | None = None):
        self.content = content
        self.colspan = colspan
        self.align = align  # override alignment from multicolumn


def classify_rows(body: str, col_alignments: list[str],
                  wrap_stars: bool, wrap_se: bool) -> list:
    """Parse body into a list of HRule and DataRow objects."""
    raw_rows = parse_rows(body)
    elements = []

    for raw in raw_rows:
        stripped = raw.strip()
        if not stripped:
            continue

        # Check for horizontal rules at the start of the row
        # A row can contain rules AND data (e.g., "\hline Variable & ...")
        # Process rules first, then any remaining content

        remaining = stripped

        while remaining:
            remaining = remaining.strip()
            if not remaining:
                break

            if remaining.startswith(r'\toprule'):
                elements.append(HRule('top'))
                remaining = remaining[len(r'\toprule'):].strip()
            elif remaining.startswith(r'\bottomrule'):
                elements.append(HRule('bottom'))
                remaining = remaining[len(r'\bottomrule'):].strip()
            elif remaining.startswith(r'\midrule'):
                elements.append(HRule('mid'))
                remaining = remaining[len(r'\midrule'):].strip()
            elif remaining.startswith(r'\hline'):
                elements.append(HRule('hline'))
                remaining = remaining[len(r'\hline'):].strip()
            elif remaining.startswith(r'\cmidrule'):
                m = re.match(r'\\cmidrule\s*(?:\([lr]*\))?\s*\{(\d+)-(\d+)\}', remaining)
                if m:
                    elements.append(HRule('cmidrule', int(m.group(1)), int(m.group(2))))
                    remaining = remaining[m.end():].strip()
                else:
                    break
            elif remaining.startswith(r'\addlinespace'):
                # Skip spacing commands
                m = re.match(r'\\addlinespace\s*(?:\[[^\]]*\])?', remaining)
                remaining = remaining[m.end():].strip() if m else remaining[len(r'\addlinespace'):].strip()
            else:
                # This is data content
                break

        if not remaining:
            continue

        # Parse cells
        raw_cells = parse_cells(remaining)
        cells = []
        for cell_text in raw_cells:
            cell_text = cell_text.strip()

            # Check for \multicolumn{n}{align}{content}
            mc_match = re.match(r'\\multicolumn\{(\d+)\}\{([^}]*)\}\{(.*)\}$', cell_text, re.DOTALL)
            if mc_match:
                colspan = int(mc_match.group(1))
                mc_align_spec = mc_match.group(2).strip()
                mc_content = mc_match.group(3)

                # Parse alignment from multicolumn spec
                mc_align = None
                for ch in mc_align_spec:
                    if ch in 'lcr':
                        mc_align = {'l': 'left', 'c': 'center', 'r': 'right'}[ch]
                        break

                html_content = convert_cell_content(mc_content, wrap_stars, wrap_se)
                cells.append(Cell(html_content, colspan=colspan, align=mc_align))
            else:
                html_content = convert_cell_content(cell_text, wrap_stars, wrap_se)
                cells.append(Cell(html_content, colspan=1))

        if cells:
            elements.append(DataRow(cells))

    return elements


# ---------------------------------------------------------------------------
# Determine header vs body rows
# ---------------------------------------------------------------------------

def split_header_body(elements: list) -> tuple[list, list]:
    """Decide which rows are header rows and which are body rows.

    Heuristic: everything before the first midrule or second hline is header.
    """
    header_elements = []
    body_elements = []
    hline_count = 0
    in_header = True

    for el in elements:
        if in_header:
            if isinstance(el, HRule):
                if el.kind in ('mid', 'bottom'):
                    in_header = False
                    # Don't add this rule to header; it's the separator
                    continue
                elif el.kind == 'hline':
                    hline_count += 1
                    if hline_count >= 2:
                        in_header = False
                        continue
                header_elements.append(el)
            else:
                header_elements.append(el)
        else:
            body_elements.append(el)

    return header_elements, body_elements


def _expand_row_to_grid(cells: list, total_cols: int) -> list:
    """Expand a row's cells into a position-indexed grid.

    Returns a list of length total_cols where each entry is
    (content, colspan, align, is_span_leader) or None for positions
    covered by a preceding colspan.

    is_span_leader is True for the first position of a multi-column cell.
    """
    grid = [None] * total_cols
    pos = 0
    for cell in cells:
        if pos >= total_cols:
            break
        grid[pos] = (cell.content, cell.colspan, cell.align, cell.colspan > 1)
        # Fill spanned positions with a sentinel
        for offset in range(1, cell.colspan):
            if pos + offset < total_cols:
                grid[pos + offset] = ('__spanned__', 0, None, False)
        pos += cell.colspan
    return grid


def merge_header_rows(header_elements: list) -> list:
    """Merge multi-row headers into single rows by joining cell content with <br>.

    Correctly handles multicolumn cells by aligning on column *position*
    rather than cell index.  Rows that contain multicolumn spans (group
    headers) are emitted as separate rows; the remaining single-column
    label rows are merged together into one row.
    """
    data_rows = [el for el in header_elements if isinstance(el, DataRow)]
    other = [el for el in header_elements if not isinstance(el, DataRow)]

    if len(data_rows) <= 1:
        return header_elements

    # Find the column number row (e.g., "(1)  (2)  (3)") — keep it separate
    col_num_row = None
    label_rows = []
    for row in data_rows:
        # A row is a "column number row" if most non-empty cells match (N) pattern
        non_empty = [c for c in row.cells if c.content.strip()]
        if non_empty and all(re.match(r'^\(\d+\)(?:-\(\d+\))?$', c.content.strip()) for c in non_empty):
            col_num_row = row
        else:
            label_rows.append(row)

    if not label_rows:
        return header_elements

    # Separate rows that have multicolumn spans (group-header rows) from
    # rows that are purely single-column (individual label rows).
    group_rows = []
    single_rows = []
    for row in label_rows:
        has_mc = any(c.colspan > 1 for c in row.cells)
        if has_mc:
            group_rows.append(row)
        else:
            single_rows.append(row)

    # Determine total column positions from the tabular spec (use max span sum)
    all_rows = label_rows
    total_cols = max(sum(c.colspan for c in r.cells) for r in all_rows)

    # Merge single-column label rows by column position
    merged_row = None
    if single_rows:
        grids = [_expand_row_to_grid(r.cells, total_cols) for r in single_rows]
        merged_cells = []
        for pos in range(total_cols):
            parts = []
            align = None
            for grid in grids:
                entry = grid[pos]
                if entry and entry[0] != '__spanned__':
                    if entry[0].strip():
                        parts.append(entry[0].strip())
                    if entry[2] and not align:
                        align = entry[2]
            merged_cells.append(Cell('<br>'.join(parts), colspan=1, align=align))
        merged_row = DataRow(merged_cells, is_header=True)

    # Assemble result: rules, then group header rows (in order), column
    # numbers row, then merged single-column label row.
    result = other[:]
    for gr in group_rows:
        result.append(gr)
    if col_num_row:
        result.append(col_num_row)
    if merged_row:
        result.append(merged_row)
    return result


def merge_coef_se_rows(body_elements: list) -> list:
    """Merge coefficient rows with their SE rows into single cells.

    Detects pairs: a row with coefficients followed by a row where most cells
    are standard errors (wrapped in <span class="se">).
    """
    data_rows = [el for el in body_elements if isinstance(el, DataRow)]
    other = [el for el in body_elements if not isinstance(el, DataRow)]

    if len(data_rows) < 2:
        return body_elements

    merged = []
    i = 0
    while i < len(data_rows):
        row = data_rows[i]
        # Check if the NEXT row is an SE row
        if i + 1 < len(data_rows):
            next_row = data_rows[i + 1]
            se_cells = sum(1 for c in next_row.cells if '<span class="se">' in c.content)
            non_empty_next = sum(1 for c in next_row.cells if c.content.strip())
            # It's an SE row if most non-empty cells are SEs
            if non_empty_next > 0 and se_cells / non_empty_next >= 0.5:
                # Merge: combine each cell
                merged_cells = []
                for j in range(max(len(row.cells), len(next_row.cells))):
                    coef = row.cells[j].content if j < len(row.cells) else ''
                    se = next_row.cells[j].content if j < len(next_row.cells) else ''
                    if coef and se:
                        combined = f'{coef}<br>{se}'
                    else:
                        combined = coef or se
                    colspan = row.cells[j].colspan if j < len(row.cells) else 1
                    align = row.cells[j].align if j < len(row.cells) else None
                    merged_cells.append(Cell(combined, colspan=colspan, align=align))
                merged.append(DataRow(merged_cells))
                i += 2
                continue
        merged.append(row)
        i += 1

    return other + merged


# ---------------------------------------------------------------------------
# Overlay specification parsing
# ---------------------------------------------------------------------------

def parse_overlay_spec(spec: str, kind: str) -> dict:
    """Parse an overlay spec like '2:1' or '1,3:2-4'.

    kind is 'col', 'row', or 'cell'.
    Returns dict with keys: row, col, step.
    """
    parts = spec.split(':')
    if len(parts) != 2:
        raise ValueError(f"Invalid overlay spec '{spec}' — expected TARGET:STEP")

    target, step = parts

    def positive_int(raw: str, label: str) -> int:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{label} must be an integer — got '{raw}'") from exc
        if value < 1:
            raise ValueError(f"{label} must be >= 1 — got '{raw}'")
        return value

    def validate_step(raw: str) -> str:
        raw = raw.strip()
        if not re.fullmatch(r'\d+(?:\+|-\d+)?', raw):
            raise ValueError(f"Invalid overlay step '{raw}'")
        if raw.endswith('+'):
            positive_int(raw[:-1], 'Step')
        elif '-' in raw:
            start_raw, end_raw = raw.split('-', 1)
            start = positive_int(start_raw, 'Step')
            end = positive_int(end_raw, 'Step')
            if end < start:
                raise ValueError(f"Overlay step range must not go backwards — got '{raw}'")
        else:
            positive_int(raw, 'Step')
        return raw

    step = validate_step(step)

    if kind == 'cell':
        rc = target.split(',')
        if len(rc) != 2:
            raise ValueError(f"Cell spec needs ROW,COL — got '{target}'")
        return {'row': positive_int(rc[0], 'Row'), 'col': positive_int(rc[1], 'Column'), 'step': step}
    elif kind == 'row':
        return {'row': positive_int(target, 'Row'), 'col': None, 'step': step}
    elif kind == 'col':
        return {'row': None, 'col': positive_int(target, 'Column'), 'step': step}


def collect_overlays(args) -> dict:
    """Build a lookup: (row, col) -> {'highlight': step, 'dim': step, 'step': step}.

    row and col are 1-indexed. None means "all in that dimension".
    """
    overlays = {}  # keyed by (row|'*', col|'*')

    def add(attr, kind, specs):
        if not specs:
            return
        for spec in specs:
            parsed = parse_overlay_spec(spec, kind)
            row_key = parsed['row'] or '*'
            col_key = parsed['col'] or '*'
            key = (row_key, col_key)
            if key not in overlays:
                overlays[key] = {}
            overlays[key][attr] = parsed['step']

    add('highlight', 'col', args.highlight_col)
    add('highlight', 'row', args.highlight_row)
    add('highlight', 'cell', args.highlight_cell)
    add('dim', 'col', args.dim_col)
    add('dim', 'row', args.dim_row)
    add('dim', 'cell', args.dim_cell)
    add('step', 'row', args.step_row)
    add('step', 'col', args.step_col)

    return overlays


def get_cell_overlays(row_idx: int, col_idx: int, overlays: dict) -> dict:
    """Get combined overlay attributes for a specific cell (1-indexed)."""
    attrs = {}

    # Check: exact cell match
    key = (row_idx, col_idx)
    if key in overlays:
        attrs.update(overlays[key])

    # Check: row wildcard (entire row)
    key = (row_idx, '*')
    if key in overlays:
        for attr, step in overlays[key].items():
            if attr not in attrs:
                attrs[attr] = step

    # Check: col wildcard (entire column)
    key = ('*', col_idx)
    if key in overlays:
        for attr, step in overlays[key].items():
            if attr not in attrs:
                attrs[attr] = step

    return attrs


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def validate_font_size(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)(?:px|em|rem|%)', value):
        raise ValueError(f"Invalid font size '{value}'")
    return value


def generate_html(header_elements: list, body_elements: list,
                  col_alignments: list[str], overlays: dict,
                  font_size: str | None = None, compact: bool = False,
                  caption: str | None = None) -> str:
    """Generate the HTML table string."""
    lines = []
    style_parts = []
    if font_size:
        style_parts.append(f'font-size: {validate_font_size(font_size)}')
    if compact:
        style_parts.append('--table-padding: 4px 8px')

    style_attr = f' style="{"; ".join(style_parts)}"' if style_parts else ''
    lines.append(f'<table{style_attr}>')

    if caption:
        lines.append(f'  <caption>{html.escape(caption, quote=False)}</caption>')

    # --- thead ---
    header_data_rows = [el for el in header_elements if isinstance(el, DataRow)]
    header_rules = [el for el in header_elements if isinstance(el, HRule)]

    # Filter out header rows where all cells are empty
    header_data_rows = [r for r in header_data_rows if any(c.content.strip() for c in r.cells)]

    if header_data_rows:
        lines.append('  <thead>')
        for row in header_data_rows:
            lines.append('    <tr>')
            col_pos = 1
            for cell in row.cells:
                align = cell.align or (col_alignments[col_pos - 1] if col_pos - 1 < len(col_alignments) else 'center')
                style = f' style="text-align: {align}"' if align != 'left' else ''
                colspan_attr = f' colspan="{cell.colspan}"' if cell.colspan > 1 else ''

                # Overlay attributes for header cells
                overlay_attrs = ''
                ov = get_cell_overlays(0, col_pos, overlays)  # row 0 = header
                for attr in ('highlight', 'dim', 'step'):
                    if attr in ov:
                        overlay_attrs += f' data-{attr}="{ov[attr]}"'

                lines.append(f'      <th{colspan_attr}{style}{overlay_attrs}>{cell.content}</th>')
                col_pos += cell.colspan
            lines.append('    </tr>')

        # Check for cmidrule in header — add as a row with border-bottom on specific cells
        cmidrules = [el for el in header_rules if el.kind == 'cmidrule']
        if cmidrules:
            # We encode cmidrule info as a CSS class or inline style on the preceding header row
            # Actually, cmidrules typically appear between header rows, so we handle them
            # by adding border-bottom to specific th cells in the row above
            pass  # handled via CSS border on thead th already

        lines.append('  </thead>')

    # --- tbody ---
    body_data_rows = [el for el in body_elements if isinstance(el, DataRow)]

    # Filter out rows where all cells are empty
    body_data_rows = [r for r in body_data_rows if any(c.content.strip() for c in r.cells)]

    if body_data_rows:
        lines.append('  <tbody>')
        for row_idx_0, row in enumerate(body_data_rows):
            row_idx = row_idx_0 + 1  # 1-indexed for overlay matching

            # Check if entire row has a step overlay
            row_overlay = get_cell_overlays(row_idx, 0, overlays)  # col 0 = check row-level
            row_attrs = ''
            if ('*', '*') in overlays:
                pass  # not used
            # Row-level step (data-step on <tr>)
            row_step = overlays.get((row_idx, '*'), {}).get('step')
            if row_step:
                row_attrs += f' data-step="{row_step}"'

            lines.append(f'    <tr{row_attrs}>')
            col_pos = 1
            for cell in row.cells:
                align = cell.align or (col_alignments[col_pos - 1] if col_pos - 1 < len(col_alignments) else 'left')
                # First column often is left-aligned labels
                style = f' style="text-align: {align}"' if align != 'left' else ''
                colspan_attr = f' colspan="{cell.colspan}"' if cell.colspan > 1 else ''

                # Overlay attributes
                overlay_attrs = ''
                ov = get_cell_overlays(row_idx, col_pos, overlays)
                for attr in ('highlight', 'dim'):
                    if attr in ov:
                        overlay_attrs += f' data-{attr}="{ov[attr]}"'
                # step on individual cell (not row-level)
                if 'step' in ov and not row_step:
                    overlay_attrs += f' data-step="{ov["step"]}"'

                lines.append(f'      <td{colspan_attr}{style}{overlay_attrs}>{cell.content}</td>')
                col_pos += cell.colspan
            lines.append('    </tr>')
        lines.append('  </tbody>')

    lines.append('</table>')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def convert(tex_path: str, args) -> str:
    """Main conversion: read .tex file, parse, return HTML."""
    tex_file = Path(tex_path).expanduser()
    if not tex_file.exists():
        raise ValueError(f"{tex_file} not found")
    if tex_file.is_dir():
        raise ValueError(f"{tex_file} is a directory, not a LaTeX table file")
    try:
        tex = tex_file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read {tex_file}: {exc}") from exc

    # Strip outer wrappers
    tex = strip_latex_wrappers(tex)

    # Extract tabular
    col_spec, body = extract_tabular(tex)
    col_alignments = parse_col_spec(col_spec)

    # Parse into structured elements
    elements = classify_rows(body, col_alignments, not args.no_stars, not args.no_se)

    # Split header/body
    header_elements, body_elements = split_header_body(elements)

    # Optional merges
    if args.merge_header:
        header_elements = merge_header_rows(header_elements)
    if args.coef_se:
        body_elements = merge_coef_se_rows(body_elements)

    # Parse overlays
    overlays = collect_overlays(args)

    # Generate HTML
    html = generate_html(
        header_elements, body_elements, col_alignments, overlays,
        font_size=args.font_size, compact=args.compact, caption=args.caption
    )

    return html


def trash_path(path: Path) -> None:
    """Move an existing generated output to Trash before replacing it."""
    if not path.exists():
        return
    try:
        subprocess.run(["trash", str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Could not move existing output to Trash: {path}") from exc


def write_text_atomically(path: Path, content: str) -> None:
    """Write content through a same-directory temp file, then atomically replace path."""
    path = Path(path).expanduser()
    tmp_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
    except OSError:
        if tmp_path.exists():
            try:
                trash_path(tmp_path)
            except RuntimeError:
                pass
        raise


def write_output_file(path: Path, content: str) -> None:
    """Write generated HTML through a temp file before publishing it."""
    path = Path(path).expanduser()
    if path.exists() and path.is_dir():
        raise ValueError(f"Output path is a directory, not an HTML file: {path}")
    if path.suffix.lower() != ".html":
        raise ValueError(f"Output path is not an HTML file: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"could not prepare output directory {path.parent}: {exc}") from exc
    try:
        write_text_atomically(path, content)
    except OSError as exc:
        raise RuntimeError(f"could not write {path}: {exc}") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Convert LaTeX tables to WebSlides HTML',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('input', help='LaTeX .tex file containing a tabular environment')
    parser.add_argument('-o', '--output', help='Write HTML to this file instead of stdout')
    parser.add_argument('--font-size', help='Override font-size (e.g. "16px")')
    parser.add_argument('--compact', action='store_true', help='Reduce padding')
    parser.add_argument('--no-stars', action='store_true', help='Don\'t wrap significance stars')
    parser.add_argument('--no-se', action='store_true', help='Don\'t wrap standard errors')
    parser.add_argument('--caption', help='Add a table caption')
    parser.add_argument('--merge-header', action='store_true',
                        help='Merge multi-row headers into single cells with <br>')
    parser.add_argument('--coef-se', action='store_true',
                        help='Merge coefficient + SE rows into single cells with <br>')

    # Overlay flags
    parser.add_argument('--highlight-col', action='append', metavar='COL:STEP',
                        help='Highlight column at step (1-indexed)')
    parser.add_argument('--highlight-row', action='append', metavar='ROW:STEP',
                        help='Highlight row at step (1-indexed)')
    parser.add_argument('--highlight-cell', action='append', metavar='ROW,COL:STEP',
                        help='Highlight cell at step')
    parser.add_argument('--dim-col', action='append', metavar='COL:STEP',
                        help='Dim column at step')
    parser.add_argument('--dim-row', action='append', metavar='ROW:STEP',
                        help='Dim row at step')
    parser.add_argument('--dim-cell', action='append', metavar='ROW,COL:STEP',
                        help='Dim cell at step')
    parser.add_argument('--step-row', action='append', metavar='ROW:STEP',
                        help='Reveal row at step (hidden before)')
    parser.add_argument('--step-col', action='append', metavar='COL:STEP',
                        help='Reveal column at step (hidden before)')

    args = parser.parse_args(argv)

    try:
        html = convert(args.input, args)
    except ValueError as exc:
        parser.error(str(exc))

    if args.output:
        try:
            if Path(args.output).expanduser().resolve() == Path(args.input).expanduser().resolve():
                raise ValueError(f"Output path must not be the input table: {args.input}")
            write_output_file(Path(args.output), html)
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(html)


if __name__ == '__main__':
    main()

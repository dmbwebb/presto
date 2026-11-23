"""Table sources for the Beamer exporter.

- HTML table (as loaded by the engine from tables/*.html) -> booktabs tabular.
  When WebSlides `data-overlays` highlight boxes are requested the table is emitted as a
  nicematrix `NiceTabular` and the boxes are drawn in `\\CodeAfter` with beamer overlay
  specs; nicematrix draws them inside the table's own picture, so they survive adjustbox
  scaling (tikzmark-style marks would not).
- Original Overleaf .tex table loader (strips table/threeparttable wrappers).
"""
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from .texutil import escape_text, collapse_ws

# WebSlides overlay class -> palette colour name (see academic.css .column-overlay rules)
OVERLAY_COLOURS = {None: 'wsOrange', '': 'wsOrange', 'blue': 'wsAccentTwo', 'accent': 'wsAccent',
                   'red': 'wsRed', 'green': 'wsGreen'}


# ---------------------------------------------------------------- HTML -> tabular

def _cell_inline(node, log):
    """Convert cell content to a list of LaTeX 'lines' (split on <br>)."""
    lines = [[]]

    def rec(n):
        if isinstance(n, NavigableString):
            s = collapse_ws(str(n))
            if s:
                lines[-1].append(escape_text(s, log))
            return
        if not isinstance(n, Tag):
            return
        name = n.name.lower()
        if name == 'br':
            lines.append([])
            return
        pre, post = '', ''
        if name in ('b', 'strong'):
            pre, post = r'\textbf{', '}'
        elif name in ('em', 'i'):
            pre, post = r'\textit{', '}'
        elif name == 'sup':
            pre, post = r'\textsuperscript{', '}'
        elif name == 'sub':
            pre, post = r'\textsubscript{', '}'
        if pre:
            start_line = len(lines) - 1
            start_len = len(lines[-1])
            for c in n.children:
                rec(c)
            for li in range(start_line, len(lines)):
                s0 = start_len if li == start_line else 0
                seg = ''.join(lines[li][s0:])
                del lines[li][s0:]
                if seg:
                    lines[li].append(pre + seg + post)
        else:
            for c in n.children:
                rec(c)

    for c in node.children:
        rec(c)
    out = [''.join(parts).strip() for parts in lines]
    while len(out) > 1 and out[-1] == '':
        out.pop()
    return out


def _align_of(cell, default):
    st = cell.get('style', '') or ''
    m = re.search(r'text-align\s*:\s*(\w+)', st)
    if m:
        return {'center': 'c', 'right': 'r', 'left': 'l'}.get(m.group(1), default)
    return default


def parse_overlays(raw):
    """data-overlays JSON -> list of dicts (cols/rows/step/class) or []."""
    if not raw:
        return []
    try:
        cfg = json.loads(raw)
    except ValueError:
        return []
    return [c for c in cfg if isinstance(c, dict) and ('cols' in c or 'rows' in c)]


def html_table_to_tabular(html, log, name='', overlays=None, overlay_spec=None):
    """Return (tabular_tex, ncols).

    overlays: parsed data-overlays list; overlay_spec: callable mapping a WebSlides step
    string to a beamer overlay spec ('<2->'). A page with several <table>s (Panel A/B) is
    stacked in an outer single-column tabular, each preceded by the <p> label before it."""
    soup = BeautifulSoup(html, 'lxml')
    tables = soup.find_all('table')
    if not tables:
        raise ValueError('no <table> in html')
    if len(tables) > 1:
        if overlays:
            log.append(f'table {name}: data-overlays ignored on a multi-table page')
        parts = []
        ncols_first = None
        for t in tables:
            label = None
            prev = t.find_previous_sibling()
            if prev is not None and prev.name == 'p':
                label = escape_text(collapse_ws(prev.get_text()).strip(), log)
            tex, nc = _single_table_to_tabular(t, log)
            ncols_first = ncols_first or nc
            if label:
                parts.append('\\textbf{%s}\\\\[2pt]' % label)
            parts.append(tex + '\\\\[8pt]')
        body = '\n'.join(parts)
        if body.endswith('\\\\[8pt]'):
            body = body[:-len('\\\\[8pt]')]
        return '\\begin{tabular}{@{}l@{}}\n' + body + '\n\\end{tabular}', ncols_first
    return _single_table_to_tabular(tables[0], log, overlays=overlays, overlay_spec=overlay_spec)


def _single_table_to_tabular(table, log, overlays=None, overlay_spec=None):
    head_rows = []
    body_rows = []
    thead = table.find('thead')
    if thead:
        head_rows = thead.find_all('tr', recursive=False)
    tbodies = table.find_all('tbody')
    for tb in tbodies:
        body_rows.extend(tb.find_all('tr', recursive=False))
    if not thead and not tbodies:
        body_rows = table.find_all('tr', recursive=False)

    def row_width(tr):
        return sum(int(c.get('colspan', 1)) for c in tr.find_all(['td', 'th'], recursive=False))
    all_rows = head_rows + body_rows
    ncols = max((row_width(r) for r in all_rows), default=0)
    if ncols == 0:
        raise ValueError('empty table')

    aligns = ['l'] + ['c'] * (ncols - 1)
    for tr in body_rows:
        cells = tr.find_all(['td', 'th'], recursive=False)
        if row_width(tr) == ncols and len(cells) == ncols:
            aligns = [_align_of(c, 'l' if i == 0 else 'c') for i, c in enumerate(cells)]
            break

    use_nice = bool(overlays)
    env = 'NiceTabular' if use_nice else 'tabular'
    out = ['\\begin{%s}{%s}' % (env, ''.join(aligns)), '\\toprule']
    latex_row = 0                 # 1-based count of emitted LaTeX rows
    head_row_map = []             # per <thead> tr: (first latex row, last latex row)
    body_row_map = []             # per <tbody> tr: (first latex row, last latex row)

    def expand_row(tr, header):
        cells = tr.find_all(['td', 'th'], recursive=False)
        parsed = []
        col = 0
        for c in cells:
            span = int(c.get('colspan', 1))
            lines = _cell_inline(c, log)
            al = _align_of(c, aligns[col] if col < ncols else 'c')
            parsed.append({'span': span, 'lines': lines, 'align': al, 'col': col})
            col += span
        k = max((len(p['lines']) for p in parsed), default=1)
        rows = []
        cmid = []
        for li in range(k):
            segs = []
            for p in parsed:
                lines = p['lines']
                if header:
                    pad = k - len(lines)
                    txt = lines[li - pad] if li - pad >= 0 else ''
                else:
                    txt = lines[li] if li < len(lines) else ''
                if p['span'] > 1:
                    segs.append('\\multicolumn{%d}{%s}{%s}' % (p['span'], p['align'], txt))
                    if header and li == k - 1 and txt:
                        cmid.append('\\cmidrule(lr){%d-%d}' % (p['col'] + 1, p['col'] + p['span']))
                else:
                    segs.append(txt)
            width = sum(p['span'] for p in parsed)
            if width < ncols:
                segs.extend([''] * (ncols - width))
            rows.append(' & '.join(segs) + ' \\\\')
        return rows, cmid

    for i, tr in enumerate(head_rows):
        rows, cmid = expand_row(tr, header=True)
        head_row_map.append((latex_row + 1, latex_row + len(rows)))
        latex_row += len(rows)
        out.extend(rows)
        if cmid and i < len(head_rows) - 1:
            out.append(' '.join(cmid))
    if head_rows:
        out.append('\\midrule')
    for ti, tr in enumerate(body_rows):
        rows, _ = expand_row(tr, header=False)
        cells = tr.find_all(['td', 'th'], recursive=False)
        if len(cells) == 1 and int(cells[0].get('colspan', 1)) >= ncols and ti > 0:
            out.append('\\addlinespace')
        body_row_map.append((latex_row + 1, latex_row + len(rows)))
        latex_row += len(rows)
        out.extend(rows)
    out.append('\\bottomrule')

    if use_nice:
        # WebSlides indexes columns by the cells of the header row with the most cells
        # (engine createColumnOverlays); map those cell indexes to real column ranges.
        header_cells = []
        widest_header = max((tr.find_all(['th', 'td'], recursive=False) for tr in head_rows),
                            key=len, default=[])
        col = 1
        for c in widest_header:
            span = int(c.get('colspan', 1))
            header_cells.append((col, col + span - 1))
            col += span
        n_rows = latex_row
        lines = ['\\CodeAfter']
        for cfg in overlays:
            colour = OVERLAY_COLOURS.get(cfg.get('class'), 'wsOrange')
            spec = overlay_spec(cfg.get('step')) if (overlay_spec and cfg.get('step') is not None) else ''
            style = f'{colour}, line width=1.1pt, rounded corners=2.8pt'
            if 'cols' in cfg:
                idx = [int(x) for x in cfg['cols']]
                if not idx or min(idx) < 0 or max(idx) >= len(header_cells):
                    log.append(f'data-overlays: column index out of range {cfg}')
                    continue
                c1 = header_cells[min(idx)][0]
                c2 = header_cells[max(idx)][1]
                draw = f'\\tikz \\draw [{style}] (row-1-|col-{c1}) rectangle (row-{n_rows + 1}-|col-{c2 + 1});'
            else:
                idx = [int(x) for x in cfg['rows']]
                if not idx or min(idx) < 0 or max(idx) >= len(body_row_map):
                    log.append(f'data-overlays: row index out of range {cfg}')
                    continue
                r1 = body_row_map[min(idx)][0]
                r2 = body_row_map[max(idx)][1]
                draw = f'\\tikz \\draw [{style}] (row-{r1}-|col-1) rectangle (row-{r2 + 1}-|col-{ncols + 1});'
            lines.append(f'  \\only{spec}{{{draw}}}' if spec else '  ' + draw)
        out.extend(lines)
    out.append('\\end{%s}' % env)
    return '\n'.join(out), ncols


# ---------------------------------------------------------------- .tex loader

def load_tex_table(path: Path):
    """Return (tabular_tex, ncols_guess, notes) from an Overleaf table .tex, or None.

    Keeps only the tabular environment (drops table/threeparttable/center wrappers,
    captions, labels, tablenotes, resizebox)."""
    src = path.read_text(encoding='utf-8', errors='replace')
    start = src.find('\\begin{tabular}')
    end = src.rfind('\\end{tabular}')
    if start < 0 or end < 0:
        return None
    body = src[start:end + len('\\end{tabular}')]
    spec_m = re.match(r'\\begin\{tabular\}(\[[^\]]*\])?\{([^}]*)\}', body)
    ncols = None
    if spec_m:
        spec = spec_m.group(2)
        spec = re.sub(r'@\{[^}]*\}', '', spec)
        spec = re.sub(r'[|\s]', '', spec)
        spec = re.sub(r'\*\{(\d+)\}\{([^}]*)\}', lambda mm: mm.group(2) * int(mm.group(1)), spec)
        spec = re.sub(r'[pmb]\{[^}]*\}', 'l', spec)
        spec = re.sub(r'S\[[^\]]*\]', 'S', spec)
        ncols = len(re.findall(r'[lcrSXD]', spec))
    notes = [f'uses {macro}' for macro in ('\\sym', '\\tnote', '\\specialcell', '\\num', '\\makecell') if macro in body]
    return body, ncols, notes

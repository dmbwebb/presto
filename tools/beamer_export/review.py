"""Review artefacts: LaTeX log parsing, report.md, README.md, contact sheet."""
import re
import shutil
import subprocess
from pathlib import Path


# ---------------------------------------------------------------- LaTeX log

def parse_latex_log(log_path, frame_lines):
    """frame_lines: [(tex line no, 0-based slide index)] for the '%% ---- slide N' markers."""
    if not log_path.exists():
        return {'errors': ['no log'], 'overfull': {}, 'hbox': 0, 'pages': None}
    txt = log_path.read_text(encoding='utf-8', errors='replace')
    errors = re.findall(r'^! (.*)$', txt, re.M)

    def slide_for_line(ln):
        cur = None
        for tl, si in frame_lines:
            if tl <= ln:
                cur = si
            else:
                break
        return cur

    overfull = {}
    for m in re.finditer(r'Overfull \\vbox \(([\d.]+)pt too high\)[^\n]*?(?:detected at line (\d+)|has occurred while \\output is active)', txt):
        amt = float(m.group(1))
        ln = int(m.group(2)) if m.group(2) else None
        si = slide_for_line(ln) if ln else None
        if si is None:
            nums = re.findall(r'line (\d+)', txt[:m.start()][-4000:])
            if nums:
                si = slide_for_line(int(nums[-1]))
        overfull.setdefault(si, []).append(amt)
    pages = re.search(r'Output written on [^(]+\((\d+) pages', txt)
    return {'errors': errors, 'overfull': overfull, 'hbox': len(re.findall(r'Overfull \\hbox', txt)),
            'pages': int(pages.group(1)) if pages else None}


# ---------------------------------------------------------------- report.md

def dedupe(xs):
    seen, counts = [], {}
    for x in xs:
        if x not in counts:
            seen.append(x)
        counts[x] = counts.get(x, 0) + 1
    return [f'{x} (x{counts[x]})' if counts[x] > 1 else x for x in seen]


def write_report(path, deck_path, slides, emitter, compile_info, elapsed, command):
    L = ['# WebSlides -> Beamer export report\n',
         f'- Deck: `{deck_path}`',
         f'- Command: `{command}`',
         f'- Slides: {len(slides)} ({sum(1 for s in slides if not s["isAppendix"])} main + '
         f'{sum(1 for s in slides if s["isAppendix"])} appendix)']
    if compile_info:
        p = compile_info['parsed']
        L.append(f'- Compile: {"OK" if compile_info["rc"] == 0 else "FAILED (rc=%d)" % compile_info["rc"]}; pages: {p.get("pages")}')
        for e in p['errors'][:10]:
            L.append(f'  - LaTeX error: `{e}`')
        if p['overfull']:
            L.append('- Frames with Overfull \\vbox (vertical overflow): ' + ', '.join(
                f'slide {k + 1 if k is not None else "?"} ({max(v):.0f}pt)'
                for k, v in sorted(p['overfull'].items(), key=lambda kv: (kv[0] is None, kv[0]))))
        else:
            L.append('- No Overfull \\vbox (no vertical overflow detected)')
        L.append(f'- Overfull hbox count: {p["hbox"]}')
    L.append(f'- Elapsed: {elapsed:.0f}s\n')

    fallbacks = [(i, lg.fallback) for i, lg in emitter.logs.items() if lg.fallback]
    L.append('## Raster fallback slides\n')
    L.extend([f'- slide {i + 1}: {why}' for i, why in fallbacks] or ['- none'])
    L.append('')

    L.append('## Table sources\n')
    seen = set()
    for lg in emitter.logs.values():
        for name, src, note in lg.table_sources:
            if (name, src) in seen:
                continue
            seen.add((name, src))
            L.append(f'- `{name}`: {src.upper()}' + (f' -- {note}' if note else ''))
    if not seen:
        L.append('- none')
    L.append('')

    unknown = {}
    for i, lg in emitter.logs.items():
        for c in lg.unknown:
            unknown.setdefault(c, []).append(i + 1)
    L.append('## Deck-specific classes handled by the generic layout rules\n')
    L.extend([f'- `.{c}`: slides {", ".join(str(x) for x in sorted(set(sl))[:8])}' for c, sl in sorted(unknown.items())] or ['- none'])
    L.append('')

    L.append('## Per-slide log\n')
    for s in slides:
        lg = emitter.logs.get(s['index'])
        if lg is None:
            continue
        title = s.get('id') or ''
        L.append(f'### Slide {s["index"] + 1} {"(" + title + ")" if title else ""} [{" ".join(c for c in s["cls"] if c != "ws-active")}] steps={s["maxStep"]}')
        if lg.fallback:
            L.append(f'- RASTER FALLBACK: {lg.fallback}')
        L.extend(f'- ok: {m}' for m in dedupe(lg.handled))
        L.extend(f'- approx: {m}' for m in dedupe(lg.approx))
        L.extend(f'- LOST: {m}' for m in dedupe(lg.lost))
        L.append('')
    Path(path).write_text('\n'.join(L), encoding='utf-8')


# ---------------------------------------------------------------- README.md (for the Overleaf project)

def write_readme(path, deck_path, date_str, command, emitter, tables_from_tex, tables_from_html, has_nicematrix):
    pal = ', '.join(f'`{n}`' for n, _, src in emitter.pal.order if src != 'auto')[:600]
    L = [
        '# Beamer version of the WebSlides deck',
        '',
        f'Generated on {date_str} from `{deck_path}` with:',
        '',
        f'    {command}',
        '',
        'This folder is a complete Overleaf project: `main.tex`, `figures/`, `tables/`.',
        'Compile with pdflatex, twice (Overleaf does this by default; the second pass fixes',
        'the overlay positions and hyperlinks). No shell-escape is needed.',
        '',
        '## Editing the slides',
        '',
        '- One `\\begin{frame}` per web slide, in deck order; the comment above each frame gives the',
        '  web slide number and id. Plain text, `\\textbf{}`, `\\textcolor{...}{}` and `itemize` can be',
        '  edited freely.',
        '- Step-by-step reveals use standard beamer overlay syntax: `\\item<2->` (appears from the',
        '  second click), `\\begin{uncoverenv}<3->...\\end{uncoverenv}` (a block appears from the third',
        '  click, keeping its space before), `\\only<2>{...}` (shown only at click 2, e.g. the',
        '  alternating panels inside `overlayarea`). Remove the `<...>` to make something always visible.',
        '- `\\wsfs{a}{b}` sets the font size `a` pt with line spacing `b` pt (copied from the web',
        '  deck\'s pixel sizes: 26px on the web is 9.2pt here). It is safe to delete a `\\wsfs` or',
        '  change its numbers; the default text size is set in the preamble by `\\normalsize`.',
        '- `\\vspace{...}` between blocks reproduces the web spacing; `\\wsfill` is flexible space.',
        f'- Colours are named after the web deck\'s CSS variables, e.g. {pal or "`wsAccent`"};',
        '  they are defined at the top of `main.tex` and can be changed there.',
        '- Boxes are `tcolorbox` environments; columns are beamer `columns`; absolutely positioned',
        '  items (appendix buttons in the header, "Back" buttons, pop-ups) are tikz nodes at the',
        '  end of the frame.',
        '- Appendix buttons are `\\hyperlink{<appendix-id>}{\\wsbutton{Label}}`; each frame has',
        '  `label=<id>` so the links keep working if you reorder slides. "Back" buttons point at the',
        '  slide that links to that appendix page.',
        '',
        '## Tables',
        '',
        '- Tables live in `tables/*.tex` and are pulled in with `\\input`, scaled to fit with `adjustbox`.',
    ]
    if tables_from_tex:
        L.append('- Copied verbatim from the paper\'s Overleaf table files: ' + ', '.join(f'`{t}`' for t in tables_from_tex) + '.')
    if tables_from_html:
        L.append('- Converted from the deck\'s HTML tables (booktabs): ' + ', '.join(f'`{t}`' for t in tables_from_html) + '.')
    if has_nicematrix:
        L.append('- Tables with highlight boxes (a column or row outlined at a given click) use the')
        L.append('  `NiceTabular` environment (package nicematrix); the boxes are the `\\tikz \\draw` lines')
        L.append('  after `\\CodeAfter`. Delete a line to remove a box; the `<n>` after `\\only` is the click.')
    L += [
        '',
        '## Figures',
        '',
        '- `figures/` holds the deck\'s images; SVG figures were converted to vector PDF. A figure that',
        '  changes with clicks is included several times with `\\only<...>`.',
        '',
        '## Review material',
        '',
        '- `_review/` (not needed for Overleaf) has the exporter\'s `report.md` (what was approximated',
        '  or lost per slide), a contact sheet comparing every web slide with its Beamer page, and',
        '  the page renders.',
    ]
    Path(path).write_text('\n'.join(L) + '\n', encoding='utf-8')


# ---------------------------------------------------------------- contact sheet

def find_pdftoppm():
    for cand in (shutil.which('pdftoppm'), str(Path.home() / '.local/bin/pdftoppm'), '/opt/homebrew/bin/pdftoppm'):
        if cand and Path(cand).exists():
            return cand
    return None


def render_pages(pdf_path, out_dir, dpi=100, prefix='page'):
    """Render every PDF page to PNG with pdftoppm; returns sorted list of paths ([] if unavailable)."""
    tool = find_pdftoppm()
    if not tool:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([tool, '-r', str(dpi), '-png', str(pdf_path), str(out_dir / prefix)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return sorted(out_dir.glob(f'{prefix}-*.png'))


def contact_sheet(slides, shots, page_pngs, titles, out_dir, rows_per_sheet=10, col_w=520):
    """One row per slide: [label | web screenshot (final step) | Beamer final page].

    slides: list of slide dicts (index, maxStep); shots: {index: [png paths]} (last = final step);
    page_pngs: rendered PDF pages in order; titles: {index: text}. Returns list of written paths."""
    from PIL import Image, ImageDraw, ImageFont
    out_dir = Path(out_dir)
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 15)
        font_b = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 17)
    except OSError:
        font = font_b = ImageFont.load_default()
    # page index of each slide's final step
    first_page = {}
    p = 0
    for s in slides:
        first_page[s['index']] = p
        p += s['maxStep'] + 1
    row_h = int(col_w * 9 / 16)
    label_w = 190
    pad = 8
    sheet_w = label_w + 2 * col_w + 4 * pad
    chunks = [slides[i:i + rows_per_sheet] for i in range(0, len(slides), rows_per_sheet)]
    written = []
    for ci, chunk in enumerate(chunks):
        header_h = 40
        sheet = Image.new('RGB', (sheet_w, header_h + len(chunk) * (row_h + pad) + pad), 'white')
        d = ImageDraw.Draw(sheet)
        d.text((pad, 10), 'slide', fill='black', font=font_b)
        d.text((label_w + pad, 10), 'web (final step)', fill='black', font=font_b)
        d.text((label_w + col_w + 3 * pad, 10), 'beamer (final page)', fill='black', font=font_b)
        for ri, s in enumerate(chunk):
            y = header_h + ri * (row_h + pad)
            idx = s['index']
            label = f'{idx + 1}' + (' (app.)' if s.get('isAppendix') else '')
            d.text((pad, y + 4), label, fill='black', font=font_b)
            title = (titles.get(idx) or '')[:26]
            d.text((pad, y + 28), title, fill='#444444', font=font)
            d.text((pad, y + 48), f'steps: {s["maxStep"]}', fill='#888888', font=font)
            shot_list = shots.get(idx) or []
            fp = first_page[idx] + s['maxStep']
            cells = [Path(shot_list[-1]) if shot_list else None,
                     page_pngs[fp] if fp < len(page_pngs) else None]
            for k, pth in enumerate(cells):
                x = label_w + pad + k * (col_w + 2 * pad)
                if pth and Path(pth).exists():
                    im = Image.open(pth).convert('RGB')
                    im.thumbnail((col_w, row_h))
                    sheet.paste(im, (x, y))
                    d.rectangle([x, y, x + im.width - 1, y + im.height - 1], outline='#cccccc')
                else:
                    d.rectangle([x, y, x + col_w - 1, y + row_h - 1], outline='#cccccc')
                    d.text((x + 10, y + 10), 'missing', fill='#aa0000', font=font)
        name = 'contact_sheet.png' if len(chunks) == 1 else f'contact_sheet_{ci + 1}.png'
        path = out_dir / name
        sheet.save(path)
        written.append(path)
    return written

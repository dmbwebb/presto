"""Asset handling for the Beamer exporter: figures, tables, screenshots."""
import html
import re
import shutil
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

from . import tables as T
from .texutil import is_active, overlay_spec, parse_range


def safe_name(s):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', s).strip('_') or 'x'


class AssetManager:
    """Writes figures/ and tables/ into the output project; converts SVGs with Chromium."""

    def __init__(self, out_dir, deck_dir, page, tex_dir=None, tables_mode='auto', review_dir=None, katex_css=None):
        self.out = Path(out_dir)
        self.fig_dir = self.out / 'figures'
        self.tab_dir = self.out / 'tables'
        self.review_dir = Path(review_dir) if review_dir else self.out / '_review'
        self.shot_dir = self.review_dir / 'shots'
        for d in (self.fig_dir, self.tab_dir, self.shot_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.deck_dir = Path(deck_dir)
        self.page = page          # spare Playwright page used for SVG -> PDF printing
        self.tex_dir = Path(tex_dir) if tex_dir else None
        self.tables_mode = tables_mode
        self.shots = {}           # slide index -> [paths relative to out_dir]
        self._svg_counter = 0
        self._img_cache = {}
        # The deck's KaTeX stylesheet: inline SVGs whose $...$ the engine set
        # in KaTeX fonts need its @font-face rules when printed on their own.
        self.katex_css = katex_css

    # ------------------------------------------------------------------ images
    def _resolve(self, src):
        parsed = urllib.parse.urlparse(src or '')
        rel = urllib.parse.unquote(parsed.path)
        parts = Path(rel).parts
        candidates = [self.deck_dir / rel.lstrip('/')]
        for i in range(len(parts)):
            candidates.append(self.deck_dir / Path(*parts[i:]))
        return next((c for c in candidates if c.exists() and c.is_file()), None)

    def image(self, node, log):
        src_path = self._resolve(node.get('src'))
        if src_path is None:
            log.l(f'image not found on disk: {node.get("src")}')
            return 'figures/missing'
        if src_path.suffix.lower() == '.svg':
            return self.svg_file_to_pdf(src_path)
        key = str(src_path)
        if key in self._img_cache:
            return self._img_cache[key]
        dest = self.fig_dir / (safe_name(src_path.stem) + src_path.suffix.lower())
        if not dest.exists():
            shutil.copy2(src_path, dest)
        relp = f'figures/{dest.name}'
        self._img_cache[key] = relp
        return relp

    def svg_file_to_pdf(self, src_path):
        key = str(src_path)
        if key in self._img_cache:
            return self._img_cache[key]
        dest = self.fig_dir / (safe_name(src_path.stem) + '.pdf')
        self._print_svg(src_path.read_text(encoding='utf-8', errors='replace'), dest)
        relp = f'figures/{dest.stem}'
        self._img_cache[key] = relp
        return relp

    def svg(self, node, log, max_step):
        """Inline <svg>: one PDF per internal data-step visibility state. [(spec, path)]"""
        html = node['html']
        self._svg_counter += 1
        base = f'inline_svg_{self._svg_counter}'
        steps = [s for s in (parse_range(x) for x in node.get('steps', [])) if s]
        if not steps:
            dest = self.fig_dir / f'{base}.pdf'
            self._print_svg(html, dest)
            return [('', f'figures/{dest.stem}')]
        states = []
        for k in range(max_step + 1):
            vis = tuple(is_active(s, k) for s in steps)
            if not states or states[-1][1] != vis:
                states.append(([k], vis))
            else:
                states[-1][0].append(k)
        out = []
        for i, (ks, vis) in enumerate(states):
            soup = BeautifulSoup(html, 'xml')
            for el in soup.find_all(attrs={'data-step': True}):
                if getattr(el, 'decomposed', False) or el.attrs is None:
                    continue  # descendant of an already removed group
                rng = parse_range(el.get('data-step'))
                if rng and not is_active(rng, ks[0]):
                    el.decompose()
            dest = self.fig_dir / f'{base}_s{i}.pdf'
            self._print_svg(str(soup), dest)
            a, b = ks[0] + 1, ks[-1] + 1
            spec = f'<{a}-{b}>' if i < len(states) - 1 else f'<{a}->'
            out.append((spec, f'figures/{dest.stem}'))
        log.h(f'inline svg with internal data-step -> {len(states)} PDF variants')
        return out

    def _print_svg(self, svg_text, dest):
        m = re.search(r'viewBox="([^"]+)"', svg_text)
        if m:
            vb = [float(x) for x in re.split(r'[\s,]+', m.group(1).strip())]
            w, h = vb[2], vb[3]
        else:
            mw = re.search(r'<svg[^>]*\swidth="([\d.]+)', svg_text)
            mh = re.search(r'<svg[^>]*\sheight="([\d.]+)', svg_text)
            w = float(mw.group(1)) if mw else 800
            h = float(mh.group(1)) if mh else 600
        svg_text = re.sub(r'<svg\b', f'<svg style="width:{w}px;height:{h}px;display:block"', svg_text, count=1)
        katex = self.katex_css and 'KaTeX_' in svg_text
        fonts = f'<link rel="stylesheet" href="{html.escape(self.katex_css, quote=True)}">' if katex else ''
        page_html = (f'<!doctype html><html><head><meta charset="utf-8">{fonts}<style>@page{{margin:0;size:{w}px {h}px}}'
                     f'html,body{{margin:0;padding:0;background:transparent}}</style></head><body>{svg_text}</body></html>')
        self.page.set_content(page_html)
        if katex:
            self.page.evaluate('() => new Promise(r => requestAnimationFrame(r)).then(() => document.fonts.ready)')
        self.page.wait_for_timeout(50)
        self.page.pdf(path=str(dest), width=f'{w}px', height=f'{h}px', print_background=False,
                      margin={'top': '0', 'right': '0', 'bottom': '0', 'left': '0'}, prefer_css_page_size=True)

    # ------------------------------------------------------------------ tables
    def table(self, node, log, name, slide_no=None):
        """Write tables/<name>.tex; return (input path w/o ext, source 'tex'|'html', note).

        A table with data-overlays gets a per-slide file (the same HTML table can carry
        different highlight boxes on different slides)."""
        overlays = T.parse_overlays(node.get('overlays'))
        base = safe_name(name)
        if overlays and slide_no is not None:
            base = f'{base}_slide{slide_no}'
        dest = self.tab_dir / f'{base}.tex'
        html_ncols = None
        tabular_html = None
        if node.get('html'):
            try:
                tabular_html, html_ncols = T.html_table_to_tabular(node['html'], log.lost, name,
                                                                   overlays=overlays, overlay_spec=overlay_spec)
            except Exception as e:  # noqa: BLE001
                log.l(f'table {name}: HTML conversion failed: {e}')
        tex_candidate = (self.tex_dir / f'{name}.tex') if self.tex_dir else None
        use_tex = None
        note = ''
        if overlays and self.tables_mode != 'tex':
            note = 'data-overlays present -> HTML source (nicematrix) so the highlight boxes can be drawn'
        elif self.tables_mode in ('auto', 'tex') and tex_candidate and tex_candidate.exists():
            loaded = T.load_tex_table(tex_candidate)
            if loaded is None:
                note = f'{name}.tex exists but has no tabular environment (fragment) -> HTML used'
            else:
                body, ncols, notes = loaded
                if self.tables_mode == 'tex' or html_ncols is None or ncols == html_ncols:
                    use_tex = body
                    note = f'copied from {tex_candidate.name}' + (f' ({", ".join(notes)})' if notes else '')
                    if html_ncols is not None and ncols != html_ncols:
                        note += f' [WARNING: {ncols} cols vs {html_ncols} in the slide HTML]'
                    if overlays:
                        note += ' [data-overlays dropped: forced .tex source]'
                        log.l(f'table {name}: data-overlays not drawn (--tables tex)')
                else:
                    note = f'{name}.tex has {ncols} columns vs {html_ncols} in the slide HTML -> HTML used (--tables tex forces it)'
        if use_tex is not None:
            dest.write_text('% source: ' + str(tex_candidate) + '\n' + use_tex + '\n', encoding='utf-8')
            return f'tables/{base}', 'tex', note
        if tabular_html is None:
            raise RuntimeError('no usable table source')
        header = f'% converted from {name}.html by export_beamer.py'
        if overlays:
            header += ' (nicematrix: highlight boxes in \\CodeAfter)'
        dest.write_text(header + '\n' + tabular_html + '\n', encoding='utf-8')
        return f'tables/{base}', 'html', note

    # ------------------------------------------------------------------ screenshots
    def slide_shots(self, idx):
        return self.shots.get(idx, [])

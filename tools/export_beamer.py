#!/usr/bin/env python3
"""Export a WebSlides deck to a LaTeX Beamer project (for coauthors who work in Overleaf).

Run with the export venv (Playwright + Chromium, beautifulsoup4, lxml, pillow, pypdf):

    ~/.venvs/webslides-export/bin/python tools/export_beamer.py path/to/deck/index.html \\
        --output /path/to/out --tex-dir "/path/to/Overleaf paper folder" --compile --zip

Options:
    --output DIR          output project directory (created; existing generated files are trashed)
    --tex-dir DIR         folder with the paper's original LaTeX tables (same basename as tables/*.html)
    --tables auto|tex|html  auto (default): use the .tex when it exists and its column count matches
    --slides 1-28,40      only these slides (1-based, deck order incl. appendix)
    --raster-slides 5,9   force the screenshot fallback for these slides
    --compile             run pdflatex twice and parse the log
    --zip                 write <out>/../<deckname>_overleaf.zip (main.tex, figures/, tables/, README.md)
    --shots final|all|needed  web screenshots: final step of every slide (default), every step, or
                          only what the raster fallback needs
    --scale N             device scale factor for screenshots (default 1)

Output layout:
    <out>/main.tex, figures/, tables/, README.md     the Overleaf project (pdflatex twice)
    <out>/main.pdf                                   compiled with --compile
    <out>/_review/  report.md, tree.json, page-NNN.png, shots/, contact_sheet*.png

Pipeline: serve the deck over local HTTP from the nearest ancestor containing engine/webslides.js,
load it in headless Chromium, wait for tables/KaTeX/inline SVGs, then for every slide go to its
last step and run beamer_export/extract.js (computed styles + rects -> JSON). The emitter maps
that tree to Beamer (beamer_export/emitter.py); figures are copied or printed to vector PDF;
tables come from the paper's .tex or from the HTML. Exit code 2 on any error.
"""
import argparse
import datetime
import http.server
import json
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

if __name__ == '__main__':
    from _common import ensure_tool_python

    ensure_tool_python(('playwright', 'bs4', 'lxml', 'PIL'))

from _common import DeckNotReady, wait_for_deck_ready_sync, widen_root_for_stats  # noqa: E402

MISSING = []
try:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover
    MISSING.append('playwright (pip install playwright && playwright install chromium)')
    PlaywrightError = PlaywrightTimeoutError = RuntimeError
try:
    import bs4  # noqa: F401
    import lxml  # noqa: F401
except ImportError:  # pragma: no cover
    MISSING.append('beautifulsoup4 + lxml')
try:
    import PIL  # noqa: F401
except ImportError:  # pragma: no cover
    MISSING.append('pillow')

from beamer_export.texutil import Palette, SLIDE_H, SLIDE_W  # noqa: E402
from beamer_export.emitter import Emitter, walk_all, plain_text  # noqa: E402
from beamer_export.assets import AssetManager  # noqa: E402
from beamer_export import review  # noqa: E402

WEBSLIDES_ROOT = HERE.parent
EXTRACT_JS = (HERE / 'beamer_export' / 'extract.js').read_text(encoding='utf-8')
VENV_PY = Path.home() / '.venvs/webslides-export/bin/python'

DECK_JS = r"""() => {
    const deck = document.querySelector('.ws-deck');
    const varNames = new Set();
    for (const ss of document.styleSheets) {
        let rules;
        try { rules = ss.cssRules; } catch (e) { continue; }
        if (!rules) continue;
        for (const rule of rules) {
            if (!rule.style) continue;
            for (let i = 0; i < rule.style.length; i++) {
                const p = rule.style[i];
                if (p.startsWith('--color') || p.startsWith('--ws-highlight')) varNames.add(p);
            }
        }
    }
    const probe = document.createElement('span');
    document.body.appendChild(probe);
    const palette = {};
    for (const v of varNames) {
        probe.style.color = '';
        probe.style.color = 'var(' + v + ')';
        const c = getComputedStyle(probe).color;
        if (c) palette[v] = c;
    }
    probe.remove();
    const navItem = document.querySelector('.ws-section-nav-item');
    let navStyle = null;
    if (navItem) {
        const cs = getComputedStyle(navItem);
        navStyle = {fs: parseFloat(cs.fontSize), tt: cs.textTransform, color: cs.color};
    }
    const fl = document.getElementById('ws-footer-left');
    const btn = document.querySelector('.ws-deck a.ws-appendix-link, .ws-deck a.ws-back-link');
    let buttonStyle = null;
    if (btn) {
        const cs = getComputedStyle(btn);
        buttonStyle = {fs: parseFloat(cs.fontSize), color: cs.color, borderColor: cs.borderTopColor,
                       borderWidth: parseFloat(cs.borderTopWidth), radius: parseFloat(cs.borderTopLeftRadius),
                       pl: parseFloat(cs.paddingLeft), pt: parseFloat(cs.paddingTop), ff: cs.fontFamily};
    }
    return {
        title: document.title,
        hasSectionNav: !!(deck && deck.hasAttribute('data-section-nav')),
        introSection: deck ? deck.getAttribute('data-intro-section') : null,
        sections: (WS.sections || []).map(s => ({name: s.name, slideIndex: s.slideIndex})),
        palette: palette,
        navStyle: navStyle,
        buttonStyle: buttonStyle,
        footerLeft: fl ? fl.textContent : '',
        exportInfo: WS.getExportInfo(),
    };
}"""


# ---------------------------------------------------------------- shared conventions (as in export_pdf.py)
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def start_server(root_dir):
    httpd = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), lambda *a, **k: _QuietHandler(*a, directory=str(root_dir), **k))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def find_webslides_root(html_path):
    d = html_path.parent
    while True:
        if (d / 'engine' / 'webslides.js').exists():
            return widen_root_for_stats(html_path, d)
        if d.parent == d:
            return widen_root_for_stats(html_path, html_path.parent)
        d = d.parent


def resolve_deck_path(deck_arg):
    path = Path(deck_arg).expanduser().resolve()
    if path.is_dir():
        path = path / 'index.html'
    if path.exists():
        return path
    candidate = (WEBSLIDES_ROOT / deck_arg).expanduser().resolve()
    if candidate.is_dir():
        candidate = candidate / 'index.html'
    if candidate.exists():
        return candidate
    return path


def trash_path(path):
    path = Path(path)
    if not path.exists():
        return
    try:
        subprocess.run(['trash', str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f'Could not move existing output to Trash: {path}') from exc


def write_text_atomically(path, text):
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.tmp-{uuid.uuid4().hex}')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(path)


def parse_slide_list(s):
    if not s:
        return None
    out = set()
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


# ---------------------------------------------------------------- helpers
def build_referrers(slides):
    """appendix slide id -> (label of the first main slide linking to it, overlay of its last step)."""
    ref = {}
    for s in slides:
        label = s.get('id') or f"slide-{s['index'] + 1}"
        for n in walk_all(s['children']):
            if n.get('t') == 'el' and n.get('tag') == 'a':
                href = n.get('href') or n.get('attrs', {}).get('href') or ''
                if href.startswith('#') and len(href) > 1:
                    tid = href[1:]
                    if tid not in ref or (not s['isAppendix'] and ref[tid][2]):
                        ref[tid] = (label, s['maxStep'] + 1, s['isAppendix'])
    return {k: (v[0], v[1]) for k, v in ref.items()}


def slide_title(s):
    for n in walk_all(s['children']):
        if n.get('t') == 'el' and n.get('tag') in ('h1', 'h2'):
            return plain_text(n)
    return s.get('id') or ''


def compile_tex(out_dir, main='main.tex', runs=2):
    log_path = Path(out_dir) / (Path(main).stem + '.log')
    rc = 0
    for _ in range(runs):
        # pdflatex prints in TeX's own 8-bit encoding: never decode strictly
        proc = subprocess.run(['pdflatex', '-interaction=nonstopmode', '-halt-on-error', main],
                              cwd=str(out_dir), capture_output=True, text=True, encoding='utf-8', errors='replace')
        rc = proc.returncode
        if rc != 0:
            break
    return rc, log_path


def write_zip(out_dir, deck_name):
    zip_path = out_dir.parent / f'{deck_name}_overleaf.zip'
    tmp = zip_path.with_name(f'.{zip_path.name}.tmp-{uuid.uuid4().hex}')
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in ('main.tex', 'README.md'):
            p = out_dir / name
            if p.exists():
                zf.write(p, name)
        for sub in ('figures', 'tables'):
            for p in sorted((out_dir / sub).glob('*')):
                if p.is_file():
                    zf.write(p, f'{sub}/{p.name}')
    trash_path(zip_path)
    tmp.replace(zip_path)
    return zip_path


def clear_previous_output(out_dir):
    """Trash generated files from a previous run (never touches anything else in <out>)."""
    for name in ('main.tex', 'main.pdf', 'main.log', 'main.aux', 'main.nav', 'main.out', 'main.snm',
                 'main.toc', 'main.vrb', 'README.md', 'figures', 'tables', '_review'):
        trash_path(out_dir / name)


# ---------------------------------------------------------------- main export
def run_export(args, command):
    t0 = time.time()
    deck_path = resolve_deck_path(args.deck)
    if not deck_path.exists() or deck_path.suffix.lower() != '.html':
        raise ValueError(f'{deck_path} is not an existing .html deck')
    out_dir = Path(args.output).expanduser().resolve()
    if out_dir == deck_path.parent or out_dir in deck_path.parents:
        raise ValueError('Output directory must not be the deck folder (or one of its parents)')
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_previous_output(out_dir)
    review_dir = out_dir / '_review'
    review_dir.mkdir(parents=True, exist_ok=True)
    slide_filter = parse_slide_list(args.slides)
    raster = parse_slide_list(args.raster_slides) or set()

    root = find_webslides_root(deck_path)
    httpd = start_server(root)
    port = httpd.server_address[1]
    url = f'http://127.0.0.1:{port}/' + urllib.parse.quote(deck_path.relative_to(root).as_posix())
    print(f'Exporting {deck_path}')

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={'width': SLIDE_W, 'height': SLIDE_H}, device_scale_factor=args.scale)
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(url, wait_until='load')
                try:
                    # WS.ready when the engine has it (else the shared fallback poll),
                    # then fonts and images; deck JS errors are reported at the end.
                    wait_for_deck_ready_sync(page)
                except DeckNotReady as exc:
                    print(f'WARNING: {exc}; continuing', file=sys.stderr)
                page.evaluate('WS.prepareForExport()')
                page.evaluate("() => { if (WS.refitTables) return WS.refitTables(); }")
                page.wait_for_timeout(400)
                deck = page.evaluate(DECK_JS)
                info = deck['exportInfo']
                if not isinstance(info, list) or not info:
                    raise RuntimeError('No exportable slides found')
                print(f'  {len(info)} slides; section nav: {deck["hasSectionNav"]}; palette vars: {len(deck["palette"])}')

                aux = browser.new_page(viewport={'width': SLIDE_W, 'height': SLIDE_H})
                katex_css = page.evaluate(
                    "() => { const l = document.querySelector('link[rel~=\"stylesheet\"][href*=\"katex\"]'); return l ? l.href : null; }")
                assets = AssetManager(out_dir, deck_path.parent, aux, tex_dir=args.tex_dir,
                                      tables_mode=args.tables, review_dir=review_dir, katex_css=katex_css)

                def capture(i, steps):
                    shots = []
                    for k in steps:
                        page.evaluate('([i, s]) => WS.goToSlide(i, s)', [i, k])
                        page.wait_for_timeout(40)
                        pth = assets.shot_dir / f'slide_{i + 1:03d}_step{k}.png'
                        page.screenshot(path=str(pth))
                        shots.append(str(pth))
                    return shots

                slides = []
                for entry in info:
                    i, max_step = entry['index'], entry['maxStep']
                    if slide_filter and (i + 1) not in slide_filter:
                        continue
                    page.evaluate('([i, s]) => WS.goToSlide(i, s)', [i, max_step])
                    page.evaluate("() => { if (WS.refitTables) return WS.refitTables(); }")
                    page.wait_for_timeout(60)
                    slides.append(page.evaluate(EXTRACT_JS, {'slideIndex': i}))
                    if args.shots == 'all' or (i + 1) in raster:
                        assets.shots[i] = capture(i, range(max_step + 1))
                    elif args.shots == 'final':
                        assets.shots[i] = capture(i, [max_step])
                    if (i + 1) % 10 == 0:
                        print(f'  extracted {i + 1}/{len(info)}')
                write_text_atomically(review_dir / 'tree.json', json.dumps({'deck': deck, 'slides': slides}, indent=1))

                palette = Palette(deck['palette'])
                for nm, hx in (('wsAccent', 'B8612A'), ('wsHeading', '1E2A3A'), ('wsMuted', '8A8E96'), ('wsBorder', 'DDD9D2'),
                               ('wsBoxBg', 'F3F1ED'), ('wsHighlightColor', 'FFE4A8'), ('wsOrange', 'D98B48'),
                               ('wsAccentTwo', '3F73B8'), ('wsRed', 'C44536'), ('wsGreen', '3F73B8')):
                    palette.ensure(nm, hx)
                em = Emitter(deck, slides, palette, assets, {'raster': raster, 'slide_filter': slide_filter},
                             build_referrers(slides))
                tex = em.emit_document()
                # auto raster fallbacks need every step captured
                redo = False
                for i, lg in em.logs.items():
                    if lg.fallback and len(assets.shots.get(i, [])) < info[i]['maxStep'] + 1:
                        assets.shots[i] = capture(i, range(info[i]['maxStep'] + 1))
                        redo = True
                if redo:
                    tex = em.emit_document()
                # relative paths for the .tex
                for i, lst in list(assets.shots.items()):
                    assets.shots[i] = [str(Path(pth).relative_to(out_dir)) for pth in lst]
                if redo:
                    tex = em.emit_document()
                write_text_atomically(out_dir / 'main.tex', tex)
                if errors:
                    print('  deck JavaScript errors:', errors[:3], file=sys.stderr)
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    print(f'  wrote {out_dir / "main.tex"} ({len(tex.splitlines())} lines)')

    compile_info = None
    pages = []
    if args.compile:
        rc, log_path = compile_tex(out_dir)
        parsed = review.parse_latex_log(log_path, em.frame_lines)
        compile_info = {'rc': rc, 'parsed': parsed}
        ov = sorted((k + 1) if k is not None else -1 for k in parsed['overfull'])
        print(f'  pdflatex rc={rc}; pages={parsed.get("pages")}; errors={parsed["errors"][:3]}; overfull vbox frames={ov}')
        if rc == 0:
            pages = review.render_pages(out_dir / 'main.pdf', review_dir, dpi=100)
            if not pages:
                print('  WARNING: pdftoppm not found; no page renders / contact sheet', file=sys.stderr)

    # README + report + contact sheet
    tex_tables, html_tables = [], []
    for lg in em.logs.values():
        for name, src, _note in lg.table_sources:
            (tex_tables if src == 'tex' else html_tables).append(name)
    tex_tables = sorted(set(tex_tables))
    html_tables = sorted(set(html_tables))
    all_tables_tex = ''.join(p.read_text(encoding='utf-8') for p in (out_dir / 'tables').glob('*.tex'))
    review.write_readme(out_dir / 'README.md', deck_path, datetime.date.today().isoformat(), command, em,
                        tex_tables, html_tables, has_nicematrix='NiceTabular' in all_tables_tex)
    review.write_report(review_dir / 'report.md', deck_path, slides, em, compile_info, time.time() - t0, command)
    sheets = []
    if pages:
        titles = {s['index']: slide_title(s) for s in slides}
        shots_abs = {i: [str(out_dir / pth) for pth in lst] for i, lst in assets.shots.items()}
        sheets = review.contact_sheet(slides, shots_abs, pages, titles, review_dir)
        print(f'  contact sheet: {", ".join(str(s.name) for s in sheets)}')
    zip_path = None
    if args.zip:
        zip_path = write_zip(out_dir, deck_path.parent.name)
        print(f'  zip: {zip_path}')
    print(f'  report: {review_dir / "report.md"}')
    if compile_info and compile_info['rc'] != 0:
        raise RuntimeError('pdflatex failed: ' + '; '.join(compile_info['parsed']['errors'][:3]))
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('deck', help='Path to a deck index.html or deck directory.')
    ap.add_argument('--output', required=True, help='Output project directory.')
    ap.add_argument('--tex-dir', help='Folder with the original LaTeX tables (same basename as tables/*.html).')
    ap.add_argument('--tables', choices=['auto', 'tex', 'html'], default='auto')
    ap.add_argument('--slides', help='e.g. 1-28,40 (1-based, deck order incl. appendix).')
    ap.add_argument('--raster-slides', help='Force the screenshot fallback for these slides (1-based).')
    ap.add_argument('--compile', action='store_true', help='Run pdflatex twice.')
    ap.add_argument('--zip', action='store_true', help='Write <out>/../<deckname>_overleaf.zip.')
    ap.add_argument('--shots', choices=['all', 'needed', 'final'], default='final')
    ap.add_argument('--scale', type=float, default=1.0, help='Device scale factor for screenshots.')
    return ap.parse_args(argv)


def main(argv=None):
    if MISSING:
        print('ERROR: missing Python packages: ' + ', '.join(MISSING) +
              f'\n       run this tool with {VENV_PY}', file=sys.stderr)
        raise SystemExit(2)
    args = parse_args(argv)
    command = 'export_beamer.py ' + ' '.join(sys.argv[1:] if argv is None else argv)
    try:
        run_export(args, command)
    except (OSError, RuntimeError, ValueError, ImportError, PlaywrightError, PlaywrightTimeoutError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == '__main__':
    main()

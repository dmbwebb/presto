"""Semantic-tree -> Beamer emitter.

Input: the JSON produced by extract.js (one dict per slide) plus deck-level info.
Output: a LaTeX string (main.tex) and a per-slide log.

Layout philosophy: the browser has already done all the CSS work. We map computed
font sizes/colours/weights, and reproduce vertical rhythm from *measured* rects
(gaps between consecutive blocks become \\vspace; large flexible gaps become fills).
"""
import re
import statistics
import traceback
from dataclasses import dataclass, replace
from pathlib import Path

from .texutil import (PX_TO_PT, SLIDE_H, SLIDE_W, css_color_to_hex, escape_text, collapse_ws,
                      pt, mm, fmt, is_serif, is_mono, overlay_spec)

MIN_GAP = 1.5           # px: gaps smaller than this are ignored
SIZE_EPS = 1.0          # px: font-size differences below this are not worth a \wsfs


def vsp(px):
    """\\vspace for a measured px gap, rounded to 0.5pt to keep the .tex quiet."""
    v = round(px * PX_TO_PT * 2) / 2
    return f'\\vspace{{{fmt(v)}pt}}'


def step_spec(n, attr='data-step'):
    """Beamer overlay spec for a node's data-step / data-dim / data-highlight ('' if absent)."""
    return overlay_spec((n.get('attrs') or {}).get(attr))


@dataclass
class Ctx:
    fs: float = 26.0
    lh: float = 44.0
    fw: int = 400
    fst: str = 'normal'
    color: str = '2F3037'
    tt: str = 'none'
    serif: bool = False
    mono: bool = False
    width: float = 1144.0   # available width in px
    x: float = 68.0         # left edge of the available box (slide px)
    ta: str = 'left'
    depth: int = 0
    is_slide: bool = False

    def derive(self, st):
        return replace(self,
                       fs=st['fs'], lh=st['lh'], fw=st['fw'], fst=st['fst'],
                       color=css_color_to_hex(st['color'])[0] or self.color,
                       tt=st['tt'], serif=is_serif(st['ff']), mono=is_mono(st['ff']),
                       ta=st['ta'])


class SlideLog:
    def __init__(self):
        self.handled = []
        self.approx = []
        self.lost = []
        self.unknown = set()
        self.fallback = None
        self.table_sources = []

    def h(self, m): self.handled.append(m)
    def a(self, m): self.approx.append(m)
    def l(self, m): self.lost.append(m)


KNOWN_CLASSES = {
    'slide', 'title', 'content', 'section-header', 'subsection-header', 'appendix', 'figure', 'photo',
    'split-image', 'split-text', 'split-img', 'thanks', 'v-center', 'outline',
    'subtitle', 'author', 'coauthors', 'venue', 'date', 'section-number', 'section-subtitle',
    'section-toc', 'ws-toc-label', 'ws-subsection-list', 'ws-section-eyebrow', 'active',
    'columns', 'wide-left', 'wide-right', 'narrow-left', 'narrow-right',
    'box', 'result', 'warning', 'box-title', 'table-commentary', 'table-left', 'table-right',
    'wide-table', 'commentary', 'commentary-center', 'table-container', 'compact-table',
    'footnote', 'source', 'appendix-links', 'header-links', 'ws-appendix-link', 'ws-back-link',
    'accent', 'accent2', 'red', 'orange', 'green', 'blue', 'muted', 'bold', 'highlight',
    'caption', 'full', 'center', 'image-row', 'spacer', 'spacer-sm', 'spacer-md', 'spacer-lg',
    'katex', 'katex-display', 'stars', 'se', 'no-bullet', 'big-list', 'content-left', 'text-context',
    'notes',
}


class Emitter:
    def __init__(self, deck, slides, palette, assets, opts, referrers):
        self.deck = deck
        self.slides = slides
        self.pal = palette
        self.assets = assets        # AssetManager
        self.opts = opts
        self.referrers = referrers  # appendix id -> (referrer label, overlay)
        self.logs = {}
        self.frame_lines = []       # (tex line no, slide index) for log mapping
        self.ids = {s['id'] for s in slides if s.get('id')}
        self._compute_defaults()

    # ------------------------------------------------------------ deck defaults
    def _compute_defaults(self):
        content = [s for s in self.slides if 'content' in s['cls'] and not s['isAppendix']] or self.slides
        st = content[0]['style']
        self.base_fs = st['fs']
        self.base_lh = st['lh']
        self.base_color = css_color_to_hex(st['color'])[0] or '2F3037'
        self.pad = (st['pt'], st['pr'], st['pb'], st['pl'])
        # h2 defaults
        h2s = [c for s in content for c in s['children'] if c.get('tag') == 'h2']
        if h2s:
            h = h2s[0]
            self.h2 = dict(fs=h['style']['fs'], lh=h['style']['lh'], top=h['rect']['y'],
                           color=css_color_to_hex(h['style']['color'])[0] or '1E2A3A',
                           serif=is_serif(h['style']['ff']), fw=h['style']['fw'],
                           pb=h['style']['pb'], after=h.get('after'), h=h['rect']['h'])
            # most common h2 font size across content slides
            sizes = [c['style']['fs'] for c in h2s]
            self.h2['fs'] = statistics.mode(sizes)
            lhs = [c['style']['lh'] for c in h2s if c['style']['fs'] == self.h2['fs']]
            self.h2['lh'] = statistics.mode(lhs)
        else:
            self.h2 = dict(fs=38, lh=47.5, top=56, color='1E2A3A', serif=False, fw=700, pb=10, after=None, h=58)
        # list defaults (top-level li and nested li)
        li1, li2, gaps1 = [], [], []
        for s in content:
            for li, depth, gap in self._iter_li(s['children'], 0):
                (li1 if depth == 0 else li2).append(li)
                if depth == 0 and gap is not None:
                    gaps1.append(gap)
        def med(xs, key, default):
            vals = [key(x) for x in xs]
            return statistics.median(vals) if vals else default
        self.li = dict(fs=med(li1, lambda x: x['style']['fs'], self.base_fs),
                       lh=med(li1, lambda x: x['style']['lh'], self.base_lh),
                       gap=med(gaps1, lambda g: g, 18),
                       fs2=med(li2, lambda x: x['style']['fs'], self.base_fs * 0.92),
                       lh2=med(li2, lambda x: x['style']['lh'], self.base_lh * 0.92))
        # bullet colour
        bul = [li['before'] for li in li1 if li.get('before')]
        self.bullet_color = css_color_to_hex(bul[0]['color'])[0] if bul else None
        # list indents: text start relative to ul left
        ind1, ind2 = [], []
        for s in content:
            for ul in self._iter_lists(s['children'], 0):
                lis = [c for c in ul['children'] if c.get('tag') == 'li']
                if not lis:
                    continue
                tr = self._first_text_rect(lis[0])
                if tr:
                    (ind1 if ul['_depth'] == 0 else ind2).append(tr['x'] - ul['rect']['x'])
        self.indent1 = statistics.median(ind1) if ind1 else 30
        self.indent2 = statistics.median(ind2) if ind2 else 30
        # footer / nav
        self.footer_left = self.deck.get('footerLeft') or ''
        self.has_nav = bool(self.deck.get('hasSectionNav'))

    def _iter_li(self, nodes, depth):
        for n in nodes:
            if n.get('t') != 'el':
                continue
            if n['tag'] in ('ul', 'ol') and 'section-toc' not in n['cls']:
                lis = [c for c in n['children'] if c.get('tag') == 'li']
                for i, li in enumerate(lis):
                    gap = None
                    if i + 1 < len(lis):
                        gap = lis[i + 1]['rect']['y'] - (li['rect']['y'] + li['rect']['h'])
                    yield li, depth, gap
                    yield from self._iter_li(li.get('children', []), depth + 1)
            else:
                yield from self._iter_li(n.get('children', []), depth)

    def _iter_lists(self, nodes, depth):
        for n in nodes:
            if n.get('t') != 'el':
                continue
            if n['tag'] in ('ul', 'ol') and 'section-toc' not in n['cls']:
                n['_depth'] = depth
                yield n
                for li in n['children']:
                    yield from self._iter_lists(li.get('children', []), depth + 1)
            else:
                yield from self._iter_lists(n.get('children', []), depth)

    def _first_text_rect(self, node):
        for c in node.get('children', []):
            if c.get('t') == 'text' and c.get('rect'):
                return c['rect']
            if c.get('t') == 'el' and c.get('inline'):
                r = self._first_text_rect(c)
                if r:
                    return r
            elif c.get('t') == 'el':
                break
        return None

    # ------------------------------------------------------------ document
    def emit_document(self):
        out = []
        in_appendix = False
        intro_done = not self.has_nav
        for s in self.slides:
            if self.opts.get('slide_filter') and (s['index'] + 1) not in self.opts['slide_filter']:
                continue
            if s['isAppendix'] and not in_appendix:
                out.append('\n%% ==================== APPENDIX ====================')
                out.append('\\appendix\n\\wsappendixtrue')
                in_appendix = True
            if not intro_done and 'title' not in s['cls']:
                # slides before the first section header form the engine's implicit intro section
                if 'section-header' not in s['cls']:
                    out.append(f'\\section{{{escape_text(self.deck.get("introSection") or "Intro")}}}')
                intro_done = True
            out.append(self.emit_slide(s))
        out.append('\\end{document}')
        # preamble last: colour names are discovered while emitting slides
        tex = '\n'.join([self.preamble(), '\\begin{document}'] + out)
        # record tex line numbers of frame starts (for log mapping)
        self.frame_lines = []
        for ln, line in enumerate(tex.split('\n'), start=1):
            m = re.match(r'%% ---- slide (\d+)', line)
            if m:
                self.frame_lines.append((ln, int(m.group(1)) - 1))   # 0-based slide index
        return tex

    def preamble(self):
        # Evaluation order matters: pal.definitions() renders the colour list as the lines list is
        # built, so anything allocating a new palette name must run before that element (hence the
        # two templates below); the _*_preamble() sections are unpacked after it on purpose.
        button_macro = self._button_macro()
        frametitle_tpl = self._frametitle_template()
        lines = [
            '% Generated by export_beamer.py (WebSlides -> Beamer prototype)',
            '\\documentclass[aspectratio=169,10pt]{beamer}',
            '\\usepackage[T1]{fontenc}',
            '\\usepackage[utf8]{inputenc}',
            '\\usepackage{libertinus-type1}   % serif for headings (stand-in for Newsreader)',
            '\\usepackage[default]{lato}',
            '\\usepackage{textcomp,amssymb}',
            '\\usepackage{graphicx,booktabs,multirow,array,adjustbox,xcolor,siunitx,tikz,hyperref}',
            '\\usepackage[skins]{tcolorbox}',
            '\\usepackage{nicematrix}          % tables with highlight boxes (NiceTabular + \\CodeAfter)',
            '\\usepackage{appendixnumberbeamer}',
            '\\usetikzlibrary{calc}',
            '\\sisetup{group-separator={,},group-minimum-digits=4}',
            '\\providecommand{\\sym}[1]{\\ensuremath{^{#1}}}',
            '',
            '% ---- palette (from the deck\'s CSS variables)',
            self.pal.definitions(),
            '\\definecolor{wsDim}{HTML}{B0B3B8}',
            '',
            *self._font_preamble(),
            '',
            *self._theme_preamble(),
            *self._list_preamble(),
            '',
            '% ---- boxes',
            '\\tcbset{wsbox/.style={boxrule=0pt,arc=0pt,boxsep=0pt,before skip=0pt,after skip=0pt,'
            'left=6pt,right=6pt,top=4pt,bottom=4pt,colback=white,colframe=white,nobeforeafter,width=\\linewidth},'
            ' wsdim/.style={opacityback=0.45,opacityframe=0.45,opacitytext=0.45}}',
            '',
            '% ---- pill buttons (appendix links), drawn with tikz to match .ws-appendix-link',
            button_macro,
            '',
            '% ---- frame title: emulates the web h2 block (top offset, size, accent rule)',
            frametitle_tpl,
            '',
            '% ---- footer (zero height so slide geometry is untouched)',
            self._footline_template(),
            '',
            self._headline_template(),
            '',
            f'\\title{{{escape_text(self.deck.get("title") or "")}}}',
            '\\hypersetup{colorlinks=false,pdfborder={0 0 0}}',
        ]
        return '\n'.join(lines)

    def _font_preamble(self):
        """\\wsfs helper plus beamer's size commands rescaled to the deck's base font size."""
        base_size = f'\\wsfs{{{fmt(pt(self.base_fs))}}}{{{fmt(pt(self.base_lh))}}}'
        lines = [
            '% ---- font helpers: \\wsfs{<pt>}{<baselineskip pt>}',
            '\\newcommand{\\wsfs}[2]{\\fontsize{#1}{#2}\\selectfont}',
            f'\\newcommand{{\\wsbase}}{{{base_size}\\color{{{self.pal.name(self.base_color)}}}}}',
            '% beamer resets to \\normalsize in many templates (lists, columns); make it the deck base size',
        ]
        for name, scale in (('normalsize', 1.0), ('small', 0.85), ('footnotesize', 0.75),
                            ('scriptsize', 0.65), ('tiny', 0.5), ('large', 1.2), ('Large', 1.44)):
            lines.append(f'\\renewcommand{{\\{name}}}{{\\wsfs{{{fmt(pt(self.base_fs) * scale)}}}'
                         f'{{{fmt(pt(self.base_lh) * scale)}}}}}')
        lines += ['\\newcommand{\\wsfill}{\\vskip 0pt plus 1filll\\relax}',
                  '\\newif\\ifwsappendix']
        return lines

    def _theme_preamble(self):
        """Beamer theme settings taken from the deck's slide/heading styles."""
        h2 = self.h2
        return [
            '% ---- theme',
            '\\usetheme{default}',
            '\\setbeamertemplate{navigation symbols}{}',
            '\\setbeamertemplate{caption}{\\insertcaption}',
            f'\\setbeamercolor{{normal text}}{{fg={self.pal.name(self.base_color)},bg=white}}',
            f'\\setbeamercolor{{frametitle}}{{fg={self.pal.name(h2["color"])},bg=white}}',
            f'\\setbeamerfont{{frametitle}}{{size=\\wsfs{{{fmt(pt(h2["fs"]))}}}{{{fmt(pt(h2["lh"]))}}},series=\\bfseries}}',
            f'\\setbeamercolor{{structure}}{{fg={self.pal.name(self.bullet_color) if self.bullet_color else "wsAccent"}}}',
            '\\setbeamertemplate{itemize items}{\\raisebox{0.15ex}{\\scalebox{0.7}{\\textbullet}}}',
            '\\setbeamertemplate{enumerate items}[default]',
            f'\\setbeamercolor{{enumerate item}}{{fg={self.pal.name(h2["color"])}}}',
            f'\\setbeamercolor{{enumerate subitem}}{{fg={self.pal.name(h2["color"])}}}',
            '\\setbeamerfont{enumerate item}{series=\\bfseries}',
            f'\\setbeamersize{{text margin left={fmt(mm(self.pad[3]))}mm,text margin right={fmt(mm(self.pad[1]))}mm}}',
            '\\setlength{\\parskip}{0pt}',
            '\\raggedright',
            '\\hyphenpenalty=10000 \\exhyphenpenalty=10000   % the web deck never hyphenates',
            '\\setbeamertemplate{frame begin}{\\wsbase\\raggedright}',
        ]

    def _list_preamble(self):
        """List indents, fonts and item spacing, all measured from the deck."""
        sub_gap = fmt(pt(max(self.li['gap'] * 0.4, 2)))
        return [
            '% list geometry (measured from the deck)',
            f'\\setlength{{\\leftmargini}}{{{fmt(pt(self.indent1))}pt}}',
            f'\\setlength{{\\leftmarginii}}{{{fmt(pt(self.indent2))}pt}}',
            f'\\setlength{{\\leftmarginiii}}{{{fmt(pt(self.indent2))}pt}}',
            f'\\setbeamerfont{{itemize/enumerate body}}{{size=\\wsfs{{{fmt(pt(self.li["fs"]))}}}{{{fmt(pt(self.li["lh"]))}}}}}',
            f'\\setbeamerfont{{itemize/enumerate subbody}}{{size=\\wsfs{{{fmt(pt(self.li["fs2"]))}}}{{{fmt(pt(self.li["lh2"]))}}}}}',
            f'\\setbeamerfont{{itemize/enumerate subsubbody}}{{size=\\wsfs{{{fmt(pt(self.li["fs2"]))}}}{{{fmt(pt(self.li["lh2"]))}}}}}',
            '% item spacing = measured gap between consecutive <li> boxes (must live in \\@listi: beamer',
            '% applies the "body begin" template too early for \\itemsep set there to take effect)',
            '\\makeatletter',
            f'\\def\\@listi{{\\leftmargin\\leftmargini\\topsep 0pt\\parsep 0pt\\itemsep {fmt(pt(self.li["gap"]))}pt}}',
            f'\\def\\@listii{{\\leftmargin\\leftmarginii\\topsep 0pt\\parsep 0pt\\itemsep {sub_gap}pt}}',
            f'\\def\\@listiii{{\\leftmargin\\leftmarginiii\\topsep 0pt\\parsep 0pt\\itemsep {sub_gap}pt}}',
            '\\makeatother',
        ]

    def _frametitle_template(self):
        h2 = self.h2
        after = h2.get('after')
        rule = ''
        if after and after.get('w', 0) > 0 and after.get('h', 0) > 0:
            col = self.pal.name(css_color_to_hex(after['bg'])[0] or 'B8612A')
            rule = (f'\\nointerlineskip\\vskip {fmt(pt(h2["pb"] - after["h"]))}pt'
                    f'{{\\color{{{col}}}\\rule{{{fmt(mm(after["w"]))}mm}}{{{fmt(pt(after["h"]))}pt}}}}\\par')
        top = h2['top']
        fam = '\\rmfamily' if h2['serif'] else ''
        return ('% calibration constants (measured against the web deck): title sits 1.6pt too low and\n'
                '% beamer adds ~6.9pt of slack below the title block; both are removed here.\n'
                '\\newlength{\\wsFrameTopAdjust}\\setlength{\\wsFrameTopAdjust}{1.6pt}\n'
                '\\newlength{\\wsAfterTitleFix}\\setlength{\\wsAfterTitleFix}{6.9pt}\n'
                '\\setbeamertemplate{frametitle}{%\n'
                f'  \\vskip {fmt(pt(top))}pt\\vskip-\\wsFrameTopAdjust\n'
                f'  {{\\usebeamerfont{{frametitle}}\\usebeamercolor[fg]{{frametitle}}{fam}\\insertframetitle\\par}}%\n'
                f'  {rule}\n'
                '  \\vskip-\\wsAfterTitleFix\n'
                '}')

    def _button_macro(self):
        """Pill button like .ws-appendix-link, but kept legible in print: font >= 6pt and a
        darker border than the web's near-white one."""
        bs = self.deck.get('buttonStyle') or {}
        fs_pt = max(pt(bs.get('fs', 12)), 6.0)
        fg = self.pal.name(css_color_to_hex(bs.get('color', '#8A8E96'))[0] or '8A8E96')
        bc = self.pal.ensure('wsPillBorder', 'B8B2A6')
        bw = max(bs.get('borderWidth', 1.5), 1.5)
        rad = min(bs.get('radius', 16), 16)
        px_, py_ = max(bs.get('pl', 14), 10), max(bs.get('pt', 4), 3)
        fam = '\\ttfamily' if is_mono(bs.get('ff', 'monospace')) else ''
        return (f'\\newcommand{{\\wsbutton}}[1]{{\\tikz[baseline=(wsb.base)]\\node[draw={bc},line width={fmt(pt(bw))}pt,'
                f'rounded corners={fmt(pt(rad))}pt,inner xsep={fmt(pt(px_))}pt,inner ysep={fmt(pt(py_))}pt,text={fg},'
                f'font={fam}\\wsfs{{{fmt(fs_pt)}}}{{{fmt(fs_pt)}}}](wsb){{\\strut #1}};}}')

    def _footline_template(self):
        fl = escape_text(self.footer_left)
        return ('\\setbeamertemplate{footline}{%\n'
                '  \\vbox to 0pt{\\vss\\hbox to\\paperwidth{\\hspace{6mm}{\\wsfs{4.6}{5.5}\\color{wsMuted}'
                f'{fl}\\hfill\\ifwsappendix Appendix\\else\\insertframenumber\\,/\\,\\inserttotalframenumber\\fi}}\\hspace{{6mm}}}}\\vskip 3mm}}%\n'
                '}')

    def _headline_template(self):
        if not self.has_nav:
            return '\\setbeamertemplate{headline}{}'
        ns = self.deck.get('navStyle') or {}
        fs = ns.get('fs', 12)
        upper = ns.get('tt') == 'uppercase'
        return ('% section navigation strip (emulates the web deck\'s top nav)\n'
                '\\setbeamercolor{section in head/foot}{fg=wsHeading}\n'
                '\\setbeamercolor{section in head/foot shaded}{fg=wsMuted}\n'
                f'\\setbeamerfont{{section in head/foot}}{{size=\\wsfs{{{fmt(pt(fs))}}}{{{fmt(pt(fs * 1.2))}}}}}\n'
                '\\setbeamertemplate{section in head/foot}{\\textbf{' + ('\\MakeUppercase{\\insertsectionhead}' if upper else '\\insertsectionhead') + '}}\n'
                '\\setbeamertemplate{section in head/foot shaded}{\\color{wsMuted}' + ('\\MakeUppercase{\\insertsectionhead}' if upper else '\\insertsectionhead') + '}\n'
                '\\setbeamertemplate{headline}{%\n'
                '  \\vbox to 0pt{\\vskip 2.2mm\\hbox to\\paperwidth{\\hfil\\insertsectionnavigationhorizontal{.5\\paperwidth}{\\hfil}{\\hfil}\\hfil}\\vss}%\n'
                '}')

    # ------------------------------------------------------------ slides
    def emit_slide(self, s):
        log = SlideLog()
        self.logs[s['index']] = log
        self.cur = s
        self.overlays = []      # absolutely positioned content -> tikz nodes at frame end
        header = f"\n%% ---- slide {s['index'] + 1}" + (f" (#{s['id']})" if s.get('id') else '') + f" [{' '.join(s['cls'])}]"
        label = s.get('id') or f"slide-{s['index'] + 1}"
        forced = (s['index'] + 1) in self.opts.get('raster', set())
        if forced:
            return header + '\n' + self.raster_frame(s, label, log, reason='requested')
        try:
            body = self.slide_body(s, log)
            if s['innerText'] and not re.search(r'[A-Za-z0-9]', strip_tex(body)) and 'photo' not in s['cls']:
                raise RuntimeError('emitter produced no text for a slide with text')
        except Exception as e:  # noqa
            tb = traceback.format_exc().strip().splitlines()
            log.l(f'EMITTER ERROR: {type(e).__name__}: {e} @ ' + ' | '.join(tb[-3:-1]))
            return header + '\n' + self.raster_frame(s, label, log, reason=f'error: {e}')
        for c in s['cls']:
            if c not in KNOWN_CLASSES and not c.startswith('ws-'):
                log.unknown.add(c)
        return header + '\n' + body

    def raster_frame(self, s, label, log, reason):
        log.fallback = reason
        shots = self.assets.slide_shots(s['index'])
        lines = [f'\\begin{{frame}}[plain,label={label}]', '']
        for k, path in enumerate(shots):
            spec = f'<{k + 1}>' if len(shots) > 1 else ''
            lines.append(f'\\only{spec}{{\\begin{{tikzpicture}}[remember picture,overlay]'
                         f'\\node[anchor=north west,inner sep=0] at (current page.north west) '
                         f'{{\\includegraphics[width=\\paperwidth]{{{path}}}}};\\end{{tikzpicture}}}}')
        if not shots:
            lines.append('Raster fallback requested but no screenshot available.')
        lines.append('\\end{frame}')
        return '\n'.join(lines)

    # ------------------------------------------------------------ slide body
    def slide_body(self, s, log):
        cls = s['cls']
        st = s['style']
        label = s.get('id') or f"slide-{s['index'] + 1}"
        ctx = Ctx(fs=st['fs'], lh=st['lh'], fw=st['fw'], fst=st['fst'],
                  color=css_color_to_hex(st['color'])[0] or self.base_color, tt=st['tt'],
                  serif=is_serif(st['ff']), mono=is_mono(st['ff']),
                  width=SLIDE_W - st['pl'] - st['pr'], ta=st['ta'], is_slide=True)
        centered = st.get('jc') == 'center' and st.get('fd') != 'row'
        kids = [c for c in s['children'] if c.get('t') != 'ws']
        notes = [c for c in kids if c.get('t') == 'notes']
        kids = [c for c in kids if c.get('t') != 'notes']
        absolute = [c for c in kids if c.get('t') == 'el' and c['style']['position'] in ('absolute', 'fixed')]
        flow = [c for c in kids if c not in absolute]

        pre = []
        opts = ['t']
        if 'photo' in cls:
            return self.photo_slide(s, label, log)
        if 'title' in cls:
            opts = ['plain', 't']
        if 'section-header' in cls or 'subsection-header' in cls:
            pre.append(self._section_command(s, flow))

        frametitle = None
        if not centered and st.get('fd') != 'row':
            h2 = next((c for c in flow if c.get('tag') == 'h2'), None)
            if h2 is not None and flow and flow[0] is h2:
                frametitle = h2
                flow = flow[1:]

        body = []
        body.append(f"\\begin{{frame}}[{','.join(opts)},label={label}]")
        # slide-level font override
        if abs(st['fs'] - self.base_fs) > SIZE_EPS:
            body.append(f'\\wsfs{{{fmt(pt(st["fs"]))}}}{{{fmt(pt(st["lh"]))}}}')
        else:
            ctx = replace(ctx, fs=self.base_fs, lh=self.base_lh)
        if frametitle is not None:
            title_tex = self.inline_children(frametitle['children'], ctx.derive(frametitle['style']), log)
            size = ''
            if abs(frametitle['style']['fs'] - self.h2['fs']) > SIZE_EPS:
                size = f'\\wsfs{{{fmt(pt(frametitle["style"]["fs"]))}}}{{{fmt(pt(frametitle["style"]["lh"]))}}}'
            body.append(f'\\frametitle{{{size}{title_tex.strip()}}}')
            top_ref = frametitle['rect']['y'] + frametitle['rect']['h']
        else:
            # measured: text area of a [t] frame without title starts ~7px below the slide top,
            # a [plain,t] frame ~25px above it (zero-height headline/footline templates)
            top_ref = -25 if 'plain' in opts else 7

        after_auto = bool((s.get('after') or {}).get('autoTop'))
        ctx = replace(ctx, x=st['pl'])
        if st.get('fd') == 'row':
            # e.g. split-image: the slide itself is a row of columns
            body.append(self.columns_layout(s, flow, ctx, log))
        else:
            body.append(self.vertical_flow(flow, ctx, log, top_ref=top_ref,
                                           bottom_ref=SLIDE_H - st['pb'], centered=centered,
                                           is_slide=True, has_title=frametitle is not None,
                                           after_auto=after_auto))
        for a in absolute:
            self.absolute_node(a, ctx, log)
        if self.overlays:
            # zero-height box: absolutely positioned content must not add a line to the flow
            body.append('\\par\\nointerlineskip\\hbox{%\n' + '%\n'.join(self.overlays) + '}')
        for n in notes:
            body.append('\\note{' + escape_text(collapse_ws(n['text'])) + '}')
        body.append('\\end{frame}')
        return '\n'.join(pre + body)

    def _section_command(self, s, flow):
        """\\section / \\subsection for a section-header slide (starred in the appendix)."""
        h1 = next((c for c in flow if c.get('tag') == 'h1'), None)
        name = escape_text(plain_text(h1) if h1 else 'Section')
        if s['isAppendix']:
            return f'\\section*{{{name}}}'
        if 'subsection-header' in s['cls']:
            return f'\\subsection{{{name}}}'
        return f'\\section{{{name}}}'

    def photo_slide(self, s, label, log):
        img = next((c for c in walk_all(s['children']) if c.get('t') == 'img'), None)
        lines = [f'\\begin{{frame}}[plain,label={label}]']
        if img:
            path = self.assets.image(img, log)
            lines.append(self.cover_image_tikz(path, img, {'x': 0, 'y': 0, 'w': SLIDE_W, 'h': SLIDE_H}))
            log.h('photo slide: full-bleed cover image')
        cap = next((c for c in walk_all(s['children']) if c.get('t') == 'el' and 'photo-caption' in c['cls']), None)
        if cap:
            ctx = Ctx().derive(cap['style'])
            txt = self.inline_children(cap['children'], ctx, log)
            lines.append('\\begin{tikzpicture}[remember picture,overlay]\\node[anchor=south west,text width=\\paperwidth-16mm,'
                         f'inner sep=0] at ([xshift={fmt(mm(cap["style"]["pl"]))}mm,yshift={fmt(mm(cap["style"]["pb"]))}mm]current page.south west) '
                         f'{{\\color{{white}}\\wsfs{{{fmt(pt(cap["style"]["fs"]))}}}{{{fmt(pt(cap["style"]["lh"]))}}}{txt}}};\\end{{tikzpicture}}')
            log.a('photo caption: gradient background omitted')
        lines.append('\\end{frame}')
        return '\n'.join(lines)

    def cover_image_tikz(self, path, img, rect, spec=''):
        """object-fit: cover into rect (slide px) via tikz clip."""
        nw, nh = img.get('nw') or 1, img.get('nh') or 1
        box_ar = rect['w'] / rect['h']
        img_ar = nw / nh
        size = 'height' if img_ar > box_ar else 'width'
        dim = rect['h'] if size == 'height' else rect['w']
        # object-position (default 50% 50%) decides which part of the overflow is shown
        px_, py_ = 0.5, 0.5
        op = (img.get('style') or {}).get('objectPosition') or '50% 50%'
        parts = op.split()
        try:
            if len(parts) >= 1 and parts[0].endswith('%'):
                px_ = float(parts[0][:-1]) / 100
            if len(parts) >= 2 and parts[1].endswith('%'):
                py_ = float(parts[1][:-1]) / 100
        except ValueError:
            pass
        if size == 'height':
            scaled_w = rect['h'] * img_ar
            scaled_h = rect['h']
        else:
            scaled_w = rect['w']
            scaled_h = rect['w'] / img_ar
        left = rect['x'] - (scaled_w - rect['w']) * px_
        top = rect['y'] - (scaled_h - rect['h']) * py_
        return (f'\\only{spec}{{\\begin{{tikzpicture}}[remember picture,overlay]\n'
                f'  \\clip ([xshift={fmt(mm(rect["x"]))}mm,yshift=-{fmt(mm(rect["y"]))}mm]current page.north west) rectangle ++({fmt(mm(rect["w"]))}mm,-{fmt(mm(rect["h"]))}mm);\n'
                f'  \\node[anchor=north west,inner sep=0] at ([xshift={fmt(mm(left))}mm,yshift=-{fmt(mm(top))}mm]current page.north west) '
                f'{{\\includegraphics[{size}={fmt(mm(dim))}mm]{{{path}}}}};\n\\end{{tikzpicture}}}}')

    # ------------------------------------------------------------ vertical flow
    def vertical_flow(self, kids, ctx, log, top_ref=None, bottom_ref=None, centered=False,
                      is_slide=False, has_title=False, after_auto=False, flexy=False):
        """Emit kids stacked vertically, reproducing measured gaps.

        Gaps between consecutive blocks become \\vspace. Flexible space (flex
        justify-content:center, margin-top:auto items, flex-grow spacers, a ::after
        spacer) becomes \\wsfill, which is deterministic rather than guessed."""
        groups = self.group_inline_runs(kids, flexy)
        groups = self.merge_stacked(groups, log)
        out = []
        prev_bottom = top_ref
        rects = [g['rect'] for g in groups if g.get('rect')]
        first_top = min((r['y'] for r in rects), default=None)
        if centered:
            out.append('\\wsfill')
            prev_bottom = first_top
        for gi, g in enumerate(groups):
            r = g.get('rect')
            node = g.get('node') if g['kind'] == 'block' else None
            flex = (node or {}).get('flex') or {}
            is_spacer = node is not None and flex.get('grow', 0) > 0 and not (node.get('textLen') or node.get('children'))
            if is_spacer:
                out.append('\\wsfill')
                if r:
                    prev_bottom = r['y'] + r['h']
                continue
            if flex.get('autoTop'):
                out.append('\\wsfill')
            elif r and prev_bottom is not None:
                gap = r['y'] - prev_bottom
                if gap > MIN_GAP or (-60 < gap < -MIN_GAP):
                    out.append(vsp(gap))
            out.append(self.emit_group(g, ctx, log, flexy=flexy))
            if r:
                prev_bottom = r['y'] + r['h']
        if centered or after_auto:
            out.append('\\wsfill')
        return '\n'.join(x for x in out if x)

    def group_inline_runs(self, kids, flexy=False):
        """Group consecutive inline nodes into paragraph runs; blocks stand alone."""
        groups = []
        run = []
        def flush():
            nonlocal run
            if run:
                rect = union_rects([n.get('rect') for n in run])
                # drop whitespace-only runs
                if any(n.get('t') != 'ws' for n in run):
                    groups.append({'kind': 'run', 'nodes': run, 'rect': rect})
                run = []
        for k in kids:
            if k.get('t') == 'ws' or is_inlineish(k, flexy):
                run.append(k)
            else:
                flush()
                groups.append({'kind': 'block', 'node': k, 'rect': k.get('rect')})
        flush()
        return groups

    def merge_stacked(self, groups, log):
        out = []
        i = 0
        while i < len(groups):
            g = groups[i]
            stack = [g]
            j = i + 1
            while j < len(groups) and g.get('rect') and groups[j].get('rect') and overlap_frac(g['rect'], groups[j]['rect']) > 0.5 \
                    and (groups[j]['kind'] == 'block' and groups[j]['node'].get('attrs', {}).get('data-step') is not None):
                stack.append(groups[j])
                j += 1
            if len(stack) > 1 and all(s['kind'] == 'block' and s['node'].get('attrs', {}).get('data-step') is not None for s in stack):
                out.append({'kind': 'stack', 'items': stack, 'rect': union_rects([s['rect'] for s in stack])})
                log.h(f'stacked reveal group of {len(stack)} -> overprint')
                i = j
            else:
                out.append(g)
                i += 1
        return out

    def emit_group(self, g, ctx, log, flexy=False):
        if g['kind'] == 'run':
            txt = self.inline_run(g['nodes'], ctx, log, flexy=flexy)
            if not txt.strip():
                return ''
            return txt.strip() + '\\par'
        if g['kind'] == 'stack':
            # NB: beamer's overprint cannot be used here: an \item<spec> inside a part is
            # misread as a part delimiter. overlayarea with the measured stack height is robust.
            lines = [f'\\begin{{overlayarea}}{{\\linewidth}}{{{fmt(mm(g["rect"]["h"]))}mm}}']
            for it in g['items']:
                n = it['node']
                spec = step_spec(n)
                lines.append(f'\\only{spec}{{%')
                lines.append(self.emit_block(n, ctx, log, skip_step=True))
                lines.append('}')
            lines.append('\\end{overlayarea}')
            return '\n'.join(lines)
        return self.emit_block(g['node'], ctx, log)

    # ------------------------------------------------------------ blocks
    def emit_block(self, n, ctx, log, skip_step=False):
        t = n.get('t')
        if t == 'img':
            return self.figure(n, ctx, log)
        if t == 'svg':
            return self.svg_figure(n, ctx, log)
        if t == 'table':
            return self.table(n, ctx, log)
        if t in ('video', 'iframe', 'object', 'embed', 'canvas'):
            return self.embed(n, ctx, log)
        if t == 'math':
            return f'\\[{n["tex"]}\\]'
        if t == 'notes':
            return ''
        if t != 'el':
            return ''
        tag = n['tag']
        attrs = n.get('attrs', {})
        step = '' if skip_step else step_spec(n)
        dim = step_spec(n, 'data-dim')
        hl = step_spec(n, 'data-highlight')
        if 'data-class' in attrs:
            log.l(f'data-class="{attrs["data-class"]}" ignored')
        for c in n['cls']:
            if c not in KNOWN_CLASSES and not c.startswith('ws-'):
                log.unknown.add(c)

        if n['style']['position'] in ('absolute', 'fixed'):
            self.absolute_node(n, ctx, log)
            return ''

        nctx = ctx.derive(n['style'])
        inner_w = n['rect']['w'] - n['style']['pl'] - n['style']['pr'] - n['style']['bl'] - n['style']['br']
        nctx = replace(nctx, width=max(inner_w, 10), x=n['rect']['x'] + n['style']['pl'] + n['style']['bl'],
                       is_slide=False)
        narrow = ctx.is_slide and 0.3 * ctx.width < n['rect']['w'] < 0.97 * ctx.width and n['style']['position'] != 'absolute'

        if tag in ('ul', 'ol'):
            body = self.list_block(n, nctx, log)
        elif tag in ('h1', 'h2', 'h3', 'h4'):
            body = self.heading_block(n, nctx, log)
        elif tag == 'hr':
            body = '\\noindent\\rule{\\linewidth}{0.4pt}\\par'
        else:
            body = self.container(n, nctx, log)

        # boxed?
        spec = self.box_spec(n, ctx)
        if spec is not None:
            body = self.tcolorbox(n, spec, body, nctx, dim=dim, hl=hl, log=log,
                                  width_px=n['rect']['w'] if narrow else None)
            dim = ''
            hl = ''
        else:
            if dim:
                body = f'{{\\only{dim}{{\\color{{wsDim}}}}%\n{body}}}'
                log.a(f'data-dim on <{tag}>: text greyed via \\color (no opacity)')
            if hl:
                body = f'{{\\only{hl}{{\\colorlet{{wsHlBg}}{{wsHighlightColor}}}}%\n{body}}}'
                log.a(f'data-highlight on block <{tag}>: not reproduced (logged)')
        # block-level style wrapper (size / weight / colour / italic / mono) relative to the context
        if tag not in ('h1', 'h2', 'h3', 'h4'):
            decl = self.style_decls(nctx, ctx, include_size=tag not in ('ul', 'ol'))
            if decl:
                body = '{' + ''.join(decl) + '\n' + body + '}'
        # text-align:center only shows when the block spans its container; a shrink-to-fit
        # flex item (align-items:flex-start) hugging the left edge stays left-aligned
        shrunk = n['rect']['w'] < 0.9 * ctx.width and abs(n['rect']['x'] - ctx.x) < 4
        if n['style']['ta'] == 'center' and ctx.ta != 'center' and spec is None and not shrunk:
            body = '{\\centering\n' + body + '\\par}'
        if narrow and spec is None:
            body = f'\\begin{{minipage}}[t]{{{fmt(mm(n["rect"]["w"]))}mm}}\n{body}\n\\end{{minipage}}\\par'
        if step:
            body = f'\\begin{{uncoverenv}}{step}\n{body}\n\\end{{uncoverenv}}'
        return body

    def style_decls(self, nctx, ctx, include_size=True):
        """Declarations needed to go from ctx to nctx (bold, italic, colour, size, mono)."""
        decl = []
        if nctx.fw >= 600 and ctx.fw < 600:
            decl.append('\\bfseries')
        elif nctx.fw < 600 and ctx.fw >= 600:
            decl.append('\\mdseries')
        if nctx.fst == 'italic' and ctx.fst != 'italic':
            decl.append('\\itshape')
        if nctx.color != ctx.color:
            decl.append(f'\\color{{{self.pal.name(nctx.color)}}}')
        if include_size and abs(nctx.fs - ctx.fs) > SIZE_EPS:
            decl.append(f'\\wsfs{{{fmt(pt(nctx.fs))}}}{{{fmt(pt(nctx.lh))}}}')
        if nctx.mono and not ctx.mono:
            decl.append('\\ttfamily')
        if nctx.serif and not ctx.serif:
            decl.append('\\rmfamily')
        return decl

    def heading_block(self, n, ctx, log):
        txt = self.inline_children(n['children'], ctx, log).strip()
        fam = '\\rmfamily' if ctx.serif else ''
        bold = '\\bfseries' if ctx.fw >= 600 else ''
        col = self.pal.name(ctx.color)
        out = f'{{{fam}{bold}\\color{{{col}}}\\wsfs{{{fmt(pt(ctx.fs))}}}{{{fmt(pt(ctx.lh))}}}{txt}\\par}}'
        after = n.get('after')
        if after and after.get('w', 0) > 0 and after.get('h', 0) > 0 and css_color_to_hex(after.get('bg'))[0]:
            c = self.pal.name(css_color_to_hex(after['bg'])[0])
            # absolute ::after sits inside the heading's padding-bottom; a block ::after sits margin-top below
            if after.get('position') == 'absolute':
                skip = max(n['style']['pb'] - after['h'], 0)
            else:
                skip = after.get('mt', 0)
            out += f'\n\\nointerlineskip\\vskip {fmt(pt(skip))}pt{{\\color{{{c}}}\\rule{{{fmt(mm(after["w"]))}mm}}{{{fmt(pt(after["h"]))}pt}}}}\\par'
        return out

    # ------------------------------------------------------------ lists
    def list_block(self, n, ctx, log):
        tag = n['tag']
        lis = [c for c in n['children'] if c.get('tag') == 'li']
        if not lis:
            return self.container(n, ctx, log)
        # bullets?
        bullets = []
        for li in lis:
            b = li.get('before')
            has_marker = n['style'].get('lst') not in (None, 'none') or (b and b['content'] not in ('none', 'normal', '""', "''", '') and b['content'].strip('"') != '')
            bullets.append(bool(has_marker))
        any_box = any(self.box_spec(li, ctx) is not None for li in lis)
        if 'section-toc' in n['cls'] or (not any(bullets) and any_box):
            # plain vertical list (no markers): emit li's as blocks
            log.h('list without markers rendered as plain blocks')
            return self.vertical_flow(lis, ctx, log)
        env = 'enumerate' if tag == 'ol' else 'itemize'
        lines = [f'\\begin{{{env}}}']
        # per-list font/gap override vs deck default
        depth = ctx.depth
        dfs, dlh, dgap = (self.li['fs'], self.li['lh'], self.li['gap']) if depth == 0 else (self.li['fs2'], self.li['lh2'], max(self.li['gap'] * 0.4, 2))
        st0 = lis[0]['style']
        gaps = [lis[i + 1]['rect']['y'] - (lis[i]['rect']['y'] + lis[i]['rect']['h']) for i in range(len(lis) - 1)]
        gap = statistics.median(gaps) if gaps else dgap
        extra = ''
        if abs(st0['fs'] - dfs) > SIZE_EPS or abs(st0['lh'] - dlh) > 3:
            extra += f'\\wsfs{{{fmt(pt(st0["fs"]))}}}{{{fmt(pt(st0["lh"]))}}}'
        if abs(gap - dgap) > 4:
            extra += f'\\setlength{{\\itemsep}}{{{fmt(pt(max(gap, 0)))}pt}}'
        if extra:
            lines[0] += extra
        lctx = replace(ctx, depth=depth + 1, fs=st0['fs'] if extra.startswith('\\wsfs') else dfs,
                       lh=st0['lh'] if extra.startswith('\\wsfs') else dlh)
        for li, has_bullet in zip(lis, bullets):
            lines.append(self.list_item(li, lctx, log, has_bullet))
        lines.append(f'\\end{{{env}}}')
        return '\n'.join(lines)

    def list_item(self, li, ctx, log, has_bullet=True):
        step = step_spec(li)
        dim = step_spec(li, 'data-dim')
        ictx = ctx.derive(li['style'])
        ictx = replace(ictx, width=li['rect']['w'] - li['style']['pl'], x=li['rect']['x'] + li['style']['pl'],
                       fs=ctx.fs if abs(li['style']['fs'] - ctx.fs) <= SIZE_EPS else li['style']['fs'])
        for c in li['cls']:
            if c not in KNOWN_CLASSES and not c.startswith('ws-'):
                log.unknown.add(c)
        label = '' if has_bullet else '[]'
        head = f'\\item{step}{label}'
        # inline runs + nested blocks
        groups = self.group_inline_runs(li['children'])
        parts = []
        prev_bottom = None
        for g in groups:
            if g['kind'] == 'run':
                txt = self.inline_run(g['nodes'], ictx, log).strip()
                if txt:
                    parts.append(txt)
            else:
                node = g['node']
                if node.get('tag') in ('ul', 'ol'):
                    gap = None
                    if prev_bottom is not None and node.get('rect'):
                        gap = node['rect']['y'] - prev_bottom
                    sp = vsp(gap) if gap and gap > MIN_GAP else ''
                    parts.append(sp + '\n' + self.emit_block(node, ictx, log))
                else:
                    parts.append('\\par ' + self.emit_block(node, ictx, log))
            if g.get('rect'):
                prev_bottom = g['rect']['y'] + g['rect']['h']
        body = ' '.join(parts)
        # per-item font size (e.g. inline style font-size on li) relative to the list's font
        if abs(li['style']['fs'] - ctx.fs) > SIZE_EPS:
            body = f'{{\\wsfs{{{fmt(pt(li["style"]["fs"]))}}}{{{fmt(pt(li["style"]["lh"]))}}}{body}}}'
        if dim:
            body = f'{{\\only{dim}{{\\color{{wsDim}}}}{body}}}'
        return f'{head} {body}'

    # ------------------------------------------------------------ containers & layout
    def container(self, n, ctx, log):
        kids = list(n['children'])
        st = n['style']
        display = st['display']
        # flex rows of links/spans flow like text; grid items are real layout cells
        flexy = display in ('flex', 'inline-flex')
        blocks = [k for k in kids if not (k.get('t') == 'ws' or is_inlineish(k, flexy))]
        blocks = [b for b in blocks if not (b.get('t') == 'el' and b['style']['position'] in ('absolute', 'fixed'))]
        absolute = [b for b in kids if b.get('t') == 'el' and b['style']['position'] in ('absolute', 'fixed')]
        for a in absolute:
            self.absolute_node(a, ctx, log)
        kids = [k for k in kids if k not in absolute]
        # Layout classification
        is_row = (display in ('grid', 'inline-grid')) or (display in ('flex', 'inline-flex') and st.get('fd', 'row').startswith('row'))
        if is_row and len(blocks) >= 2:
            clusters = cluster_columns(blocks)
            widest = max(c['rect']['w'] for c in clusters)
            disjoint = not any(x_overlap(a['rect'], b['rect']) > 3 for a, b in zip(clusters, clusters[1:]))
            if len(clusters) >= 2 and disjoint and widest >= 0.25 * ctx.width:
                log.h(f'{display} container .{".".join(n["cls"]) or n["tag"]} -> columns x{len(clusters)}')
                return self.columns_layout(n, kids, ctx, log, clusters=clusters)
            if len(clusters) >= 2 and disjoint:
                # small side-by-side items (buttons, badges): inline row
                log.h(f'{display} container .{".".join(n["cls"]) or n["tag"]} -> inline row')
                return self.inline_row(n, kids, ctx, log)
        if is_row and len(blocks) == 1 and any(k.get('t') == 'text' for k in kids):
            # grid with text + one block (e.g. number badge + text) -> inline row
            return self.inline_row(n, kids, ctx, log)
        if is_row and not blocks:
            # flex row of inline-ish items (buttons in a footnote): one paragraph
            return self.inline_row(n, kids, ctx, log)
        # default vertical flow (measured gaps)
        return self.vertical_flow(kids, ctx, log, flexy=flexy)

    def inline_row(self, n, kids, ctx, log):
        parts = []
        flexy = n['style']['display'] in ('flex', 'inline-flex')
        for k in kids:
            if k.get('t') == 'ws':
                parts.append(' ')
            elif is_inlineish(k, flexy):
                parts.append(self.inline_run([k], ctx, log, flexy=flexy))
            else:
                # block child rendered inline: as a parbox of its width
                inner = self.emit_block(k, ctx, log)
                if k.get('t') == 'el' and self.is_circle(k):
                    parts.append(inner)
                else:
                    w = k['rect']['w']
                    parts.append(f'\\begin{{minipage}}[t]{{{fmt(mm(w))}mm}}{inner}\\end{{minipage}}')
                parts.append('\\hspace{4pt}')
        txt = ''.join(parts).strip()
        return txt + '\\par' if txt else ''

    def columns_layout(self, n, kids, ctx, log, clusters=None):
        blocks = [k for k in kids if k.get('t') in ('el', 'img', 'svg', 'table', 'video', 'iframe') and not k.get('inline')]
        blocks = [b for b in blocks if not (b.get('t') == 'el' and b['style']['position'] in ('absolute', 'fixed'))]
        if clusters is None:
            clusters = cluster_columns(blocks)
        st = n.get('style', {})
        align = 'c' if st.get('ai') == 'center' else 'T'
        total = ctx.width
        lines = [f'\\begin{{columns}}[{align},onlytextwidth]']
        for ci, c in enumerate(clusters):
            frac = min(c['rect']['w'] / total, 1.0)
            # pull the column inward if it has its own padding (e.g. .split-text)
            # beamer's column env resets \rightskip: re-assert ragged-right (the web never justifies)
            lines.append(f'\\begin{{column}}{{{fmt(frac)}\\linewidth}}\\raggedright')
            cctx = replace(ctx, width=c['rect']['w'], x=c['rect']['x'], is_slide=False)
            items = c['items']
            if len(items) == 1 and items[0].get('t') == 'el' and items[0]['style']['pl'] > 8 and self.box_spec(items[0], ctx) is None:
                it = items[0]
                mp_align = 'c' if align == 'c' else 't'
                lines.append(f'\\hspace*{{{fmt(pt(it["style"]["pl"]))}pt}}\\begin{{minipage}}[{mp_align}]{{\\dimexpr\\linewidth-{fmt(pt(it["style"]["pl"] + it["style"]["pr"]))}pt\\relax}}'
                             + ('\\centering' if it['style']['ta'] == 'center' else '\\raggedright'))
                ictx = replace(cctx.derive(it['style']), width=it['rect']['w'] - it['style']['pl'] - it['style']['pr'],
                               x=it['rect']['x'] + it['style']['pl'])
                inner_kids = list(it['children'])
                v_centered = it['style'].get('jc') == 'center' and it['style'].get('fd', '').startswith('column')
                if v_centered and it['rect']['h'] > 200:
                    # flex column with justify-content:center (e.g. .split-text): a fixed-height,
                    # vertically centred minipage reproduces the centring (fills do nothing in a column)
                    box_h = min(it['rect']['h'] - it['style']['pt'] - it['style']['pb'], SLIDE_H - 90)
                    lines[-1] = lines[-1].replace(f'\\begin{{minipage}}[{mp_align}]', f'\\begin{{minipage}}[c][{fmt(mm(box_h))}mm][c]', 1)
                    lines.append(self.vertical_flow(inner_kids, ictx, log))
                else:
                    lines.append(self.vertical_flow(inner_kids, ictx, log, centered=v_centered))
                # absolute children inside
                for a in it['children']:
                    if a.get('t') == 'el' and a['style']['position'] in ('absolute', 'fixed'):
                        self.absolute_node(a, ictx, log)
                lines.append('\\end{minipage}')
            else:
                lines.append(self.vertical_flow(items, cctx, log))
            lines.append('\\end{column}')
            if ci < len(clusters) - 1:
                lines.append('\\hfill')
        lines.append('\\end{columns}')
        return '\n'.join(lines)

    # ------------------------------------------------------------ boxes
    def box_spec(self, n, ctx):
        if n.get('t') != 'el':
            return None
        st = n['style']
        bg, _ = css_color_to_hex(st['bg'])
        borders = {}
        for side, w, c in (('left', st['bl'], st['blc']), ('right', st['br'], st['brc']),
                           ('top', st['bt'], st['btc']), ('bottom', st['bb'], st['bbc'])):
            hx, a = css_color_to_hex(c)
            if w > 0 and hx:
                borders[side] = (w, hx)
        if bg in (None, 'FFFFFF') and not borders:
            return None
        if n['tag'] in ('a',) or n.get('inline'):
            return None
        return {'bg': bg, 'borders': borders, 'radius': st['radius'],
                'pad': (st['pt'], st['pr'], st['pb'], st['pl'])}

    def is_circle(self, n):
        st = n['style']
        r = n['rect']
        return st['radius'] >= min(r['w'], r['h']) / 2 - 1 and abs(r['w'] - r['h']) < 4 and css_color_to_hex(st['bg'])[0] is not None

    def tcolorbox(self, n, spec, body, ctx, dim='', hl='', log=None, width_px=None):
        st = n['style']
        if self.is_circle(n):
            col = self.pal.name(spec['bg'])
            txt = self.inline_children(n['children'], ctx, log).strip()
            fs = fmt(pt(ctx.fs))
            return (f'\\tikz[baseline=(c.base)]\\node[circle,fill={col},text={self.pal.name(ctx.color)},inner sep=1pt,'
                    f'minimum size={fmt(mm(n["rect"]["w"]))}mm,font=\\bfseries\\wsfs{{{fs}}}{{{fs}}}](c){{{txt}}};')
        opts = ['wsbox']
        if spec['bg']:
            opts.append(f'colback={self.pal.name(spec["bg"])}')
        b = spec['borders']
        frame_col = None
        for side in ('left', 'top', 'right', 'bottom'):
            if side in b:
                w, hx = b[side]
                opts.append(f'{side}rule={fmt(pt(w))}pt')
                frame_col = frame_col or hx
        if frame_col:
            opts.append(f'colframe={self.pal.name(frame_col)}')
        if spec['radius'] > 0:
            opts.append(f'arc={fmt(pt(spec["radius"]))}pt')
        else:
            opts.append('sharp corners')
        top, right, bottom, left = spec['pad']
        opts.append(f'left={fmt(pt(left))}pt,right={fmt(pt(right))}pt,top={fmt(pt(top))}pt,bottom={fmt(pt(bottom))}pt')
        centred = st['ta'] == 'center' or (st['display'] == 'flex' and st.get('ai') == 'center' and st.get('fd', '').startswith('column'))
        opts.append('before upper=\\centering' if centred else 'before upper=\\raggedright')
        if st['display'] == 'flex' and st.get('fd', '').startswith('column') and st.get('jc') == 'center':
            opts.append(f'height from={fmt(mm(n["rect"]["h"]))}mm to 200mm,valign=center')
        pre = ''
        if dim:
            pre += f'\\only{dim}{{\\tcbset{{wsdim}}}}'
            if log: log.h('data-dim on box -> tcolorbox opacity on those overlays')
        if hl:
            pre += f'\\only{hl}{{\\tcbset{{colback=wsHighlightColor}}}}'
            if log: log.a('data-highlight on box -> background swap on those overlays (no glow)')
        if not body.strip():
            opts.append(f'height={fmt(mm(n["rect"]["h"]))}mm')
        if width_px:
            opts.append(f'width={fmt(mm(width_px))}mm')
        tex = f'\\begin{{tcolorbox}}[{",".join(opts)}]\n{body}\n\\end{{tcolorbox}}'
        if pre:
            tex = '{' + pre + '%\n' + tex + '}'
        return tex

    # ------------------------------------------------------------ absolute positioning
    def absolute_node(self, n, ctx, log):
        st = n['style']
        r = n['rect']
        step = step_spec(n)
        # back link
        if 'ws-back-link' in n['cls']:
            target = self.referrers.get(self.cur.get('id'))
            label = self.inline_children(n['children'], ctx, log).strip()
            if target:
                lab, ov = target
                link = f'\\hyperlink{{{lab}<{ov}>}}{{\\wsbutton{{{label}}}}}'
            else:
                link = f'\\hyperlink{{appendix-start}}{{\\wsbutton{{{label}}}}}'
                log.a('back link: no referrer found, points to appendix start')
            self.overlays.append(self.tikz_node(r, link, anchor='south west', y_from_bottom=True))
            log.h('back link -> \\wsbutton (hyperlink to the referring slide)')
            return
        if n.get('t') == 'img':
            path = self.assets.image(n, log)
            if st.get('objectFit') == 'cover':
                self.overlays.append(self.cover_image_tikz(path, n, r, spec=step))
            else:
                self.overlays.append(self.tikz_node(r, f'\\includegraphics[width={fmt(mm(r["w"]))}mm,height={fmt(mm(r["h"]))}mm,keepaspectratio]{{{path}}}', spec=step))
            log.h('absolutely positioned image -> tikz overlay')
            return
        nctx = ctx.derive(st)
        nctx = replace(nctx, width=r['w'] - st['pl'] - st['pr'])
        body = self.container(n, nctx, log) if n['tag'] not in ('ul', 'ol') else self.list_block(n, nctx, log)
        spec = self.box_spec(n, ctx)
        node_opts = []
        if spec:
            if spec['bg']:
                node_opts.append(f'fill={self.pal.name(spec["bg"])}')
            if spec['borders']:
                w, hx = next(iter(spec['borders'].values()))
                node_opts.append(f'draw={self.pal.name(hx)},line width={fmt(pt(w))}pt')
            if spec['radius']:
                node_opts.append(f'rounded corners={fmt(pt(spec["radius"]))}pt')
            node_opts.append(f'inner xsep={fmt(pt(st["pl"]))}pt,inner ysep={fmt(pt(st["pt"]))}pt')
            body_w = r['w'] - st['pl'] - st['pr']
        else:
            node_opts.append('inner sep=0pt')
            body_w = r['w']
        size = f'\\wsfs{{{fmt(pt(nctx.fs))}}}{{{fmt(pt(nctx.lh))}}}\\color{{{self.pal.name(nctx.color)}}}'
        content = f'{size}{body}'
        flexy = st['display'] in ('flex', 'inline-flex')
        inline_only = all(k.get('t') == 'ws' or is_inlineish(k, flexy) for k in n['children'])
        anchor = 'north west'
        if inline_only and spec is None:
            # e.g. .header-links: natural width, anchored to the side it hugs
            body_w = None
            content = content.replace('\\par', '')
            if st.get('jc') == 'flex-end' or r['x'] + r['w'] > SLIDE_W * 0.75 > r['x']:
                anchor = 'north east'
        self.overlays.append(self.tikz_node(r, content, anchor=anchor, extra=','.join(node_opts), text_width=body_w, spec=step))
        log.h(f'absolutely positioned <{n["tag"]}.{".".join(n["cls"])}> -> tikz overlay node')

    def tikz_node(self, r, content, anchor='north west', extra='', text_width=None, spec='', y_from_bottom=False):
        tw = f',text width={fmt(mm(text_width))}mm,align=flush left' if text_width else ''
        ex = (',' + extra) if extra else ''
        x = r['x'] + (r['w'] if anchor.endswith('east') else 0)
        y = r['y'] + (r['h'] if anchor.startswith('south') else 0)
        return (f'\\only{spec}{{\\begin{{tikzpicture}}[remember picture,overlay]\\node[anchor={anchor}{tw}{ex}] at '
                f'([xshift={fmt(mm(x))}mm,yshift=-{fmt(mm(y))}mm]current page.north west) '
                f'{{{content}}};\\end{{tikzpicture}}}}')

    # ------------------------------------------------------------ inline
    def inline_children(self, children, ctx, log):
        return self.inline_run(children, ctx, log)

    def inline_run(self, nodes, ctx, log, flexy=False):
        out = []
        prev_br = True  # treat start as after a break (suppress leading space)
        for i, n in enumerate(nodes):
            t = n.get('t')
            if t == 'ws':
                if not prev_br and out:
                    out.append(' ')
                continue
            if t == 'text':
                s = collapse_ws(n['s'])
                if prev_br:
                    s = s.lstrip()
                if not s:
                    continue
                if ctx.tt == 'uppercase':
                    s = s.upper()
                out.append(escape_text(s, log.lost if log else None))
                prev_br = False
                continue
            if t == 'math':
                out.append(f'\\[{n["tex"]}\\]' if n.get('display') else f'${n["tex"]}$')
                prev_br = False
                continue
            if t == 'br':
                # trailing <br> at end of run: drop
                rest = [m for m in nodes[i + 1:] if m.get('t') != 'ws']
                if not rest:
                    continue
                out.append('\\mbox{}\\\\' if prev_br else '\\\\')
                prev_br = True
                continue
            if t == 'img':
                out.append(self.figure(n, ctx, log, inline=True))
                prev_br = False
                continue
            if t == 'svg':
                out.append(self.svg_figure(n, ctx, log))
                prev_br = False
                continue
            if t == 'table':
                out.append(self.table(n, ctx, log))
                prev_br = True
                continue
            if t != 'el':
                continue
            if n['style']['position'] in ('absolute', 'fixed'):
                self.absolute_node(n, ctx, log)
                continue
            if not is_inlineish(n, flexy):
                # block inside an inline run: break the paragraph
                out.append('\\par ' + self.emit_block(n, ctx, log) + ' ')
                prev_br = True
                continue
            out.append(self.inline_element(n, ctx, log))
            prev_br = False
        return ''.join(out)

    def inline_element(self, n, ctx, log):
        tag = n['tag']
        st = n['style']
        attrs = n.get('attrs', {})
        for c in n['cls']:
            if c not in KNOWN_CLASSES and not c.startswith('ws-'):
                log.unknown.add(c)
        nctx = ctx.derive(st)
        inner = self.inline_run(n['children'], nctx, log)
        step = step_spec(n)
        dim = step_spec(n, 'data-dim')
        hl = step_spec(n, 'data-highlight')
        # links
        if tag == 'a':
            href = n.get('href') or attrs.get('href') or ''
            label = inner.strip()
            if 'ws-appendix-link' in n['cls'] or (href.startswith('#') and len(href) > 1):
                target = href[1:]
                if target in self.ids:
                    tex = f'\\hyperlink{{{target}}}{{\\wsbutton{{{label}}}}}'
                    log.h(f'appendix link -> {target}')
                else:
                    tex = f'\\wsbutton{{{label}}}'
                    log.l(f'link target #{target} not found in deck')
                return self.apply_step(tex, step)
            if href.startswith('http') or href.startswith('mailto'):
                return self.apply_step(f'\\href{{{href}}}{{{inner}}}', step)
        if n.get('rect') and n['rect']['w'] > 0 and self.is_circle(n):
            col = self.pal.name(css_color_to_hex(st['bg'])[0])
            fs = fmt(pt(nctx.fs))
            return (f'\\tikz[baseline=(c.base)]\\node[circle,fill={col},text={self.pal.name(nctx.color)},inner sep=1pt,'
                    f'minimum size={fmt(mm(n["rect"]["w"]))}mm,font=\\bfseries\\wsfs{{{fs}}}{{{fs}}}](c){{{inner.strip()}}};')
        decl = self.style_decls(nctx, ctx)
        bg, _ = css_color_to_hex(st['bg'])
        if bg and bg != 'FFFFFF' and n.get('inline') and tag != 'a':
            inner = f'\\colorbox{{{self.pal.name(bg)}}}{{{inner}}}'
        if len(decl) == 1 and decl[0] == '\\bfseries':
            tex = f'\\textbf{{{inner}}}'
        elif len(decl) == 1 and decl[0] == '\\itshape':
            tex = f'\\emph{{{inner}}}'
        elif len(decl) == 1 and decl[0].startswith('\\color'):
            tex = f'\\textcolor{{{self.pal.name(nctx.color)}}}{{{inner}}}'
        elif decl:
            tex = '{' + ''.join(decl) + ' ' + inner + '}'
        else:
            tex = inner
        if dim:
            tex = f'{{\\only{dim}{{\\color{{wsDim}}}}{tex}}}'
        if hl:
            tex = f'\\alt{hl}{{\\colorbox{{wsHighlightColor}}{{{tex}}}}}{{{tex}}}'
            log.h('inline data-highlight -> \\alt + \\colorbox')
        return self.apply_step(tex, step)

    def apply_step(self, tex, step):
        return f'\\uncover{step}{{{tex}}}' if step else tex

    # ------------------------------------------------------------ figures, tables, embeds
    def figure(self, n, ctx, log, inline=False):
        r = n['rect']
        path = self.assets.image(n, log)
        if n['style'].get('objectFit') == 'cover' and r['w'] > 0 and r['h'] > 0:
            step = step_spec(n)
            self.overlays.append(self.cover_image_tikz(path, n, r, spec=step))
            log.h('img object-fit:cover -> clipped tikz overlay')
            return ''
        w, h = r['w'], r['h']
        # an in-flow image can never be taller than the text area (a full-height <img> box
        # would overflow the frame by the headline/footline slack)
        h = min(h, SLIDE_H - 36)
        tex = f'\\includegraphics[width={fmt(mm(w))}mm,height={fmt(mm(h))}mm,keepaspectratio]{{{path}}}'
        op = (n.get('style') or {}).get('op', 1)
        if op is not None and op < 0.99:
            tex = f'{{\\pgfsetfillopacity{{{fmt(op)}}}{tex}\\pgfsetfillopacity{{1}}}}'
            log.a(f'image opacity {op} -> pgf fill opacity')
        step = step_spec(n)
        if inline:
            return self.apply_step(tex, step)
        centered = self.is_centered(n, ctx)
        if centered:
            tex = '{\\centering ' + tex + '\\par}'
        else:
            tex = tex + '\\par'
        if step:
            tex = f'\\begin{{uncoverenv}}{step}\n{tex}\n\\end{{uncoverenv}}'
        log.h(f'image {Path(path).name} ({fmt(mm(w))}x{fmt(mm(h))}mm)')
        return tex

    def is_centered(self, n, ctx):
        # centered if the parent text-align is center, or the rect is horizontally centred with slack
        if ctx.ta == 'center' or 'center' in n.get('cls', []):
            return True
        r = n.get('rect')
        if r and ctx.width:
            left = r['x'] - ctx.x
            right = (ctx.x + ctx.width) - (r['x'] + r['w'])
            if left > 4 and right > 4 and abs(left - right) < 6:
                return True
        return False

    def svg_figure(self, n, ctx, log):
        r = n['rect']
        variants = self.assets.svg(n, log, self.cur['maxStep'])
        parts = []
        for spec, path in variants:
            tex = f'\\includegraphics[width={fmt(mm(r["w"]))}mm,height={fmt(mm(r["h"]))}mm,keepaspectratio]{{{path}}}'
            parts.append(f'\\only{spec}{{{tex}}}' if spec else tex)
        tex = ''.join(parts)
        if self.is_centered(n, ctx):
            tex = '{\\centering ' + tex + '\\par}'
        else:
            tex += '\\par'
        step = step_spec(n)
        if step:
            tex = f'\\begin{{uncoverenv}}{step}\n{tex}\n\\end{{uncoverenv}}'
        log.h(f'svg figure -> PDF ({len(variants)} step variant(s))' if len(variants) > 1 else 'svg figure -> PDF')
        return tex

    def table(self, n, ctx, log):
        r = n['rect']
        src_name = Path(n['src']).stem if n.get('src') else f"inline_table_{self.cur['index'] + 1}"
        try:
            rel, source, note = self.assets.table(n, log, src_name, slide_no=self.cur['index'] + 1)
        except Exception as e:  # noqa
            log.l(f'table {src_name}: conversion failed ({e})')
            return f'\\textit{{[table {escape_text(src_name)} could not be converted]}}\\par'
        log.table_sources.append((src_name, source, note))
        if n.get('overlays'):
            if source == 'html':
                log.h(f'table {src_name}: data-overlays -> nicematrix highlight boxes with overlay specs')
            else:
                log.l(f'table {src_name}: data-overlays highlight boxes not reproduced ({n["overlays"]})')
        fs = n.get('tableFs') or 14
        padx = n.get('tdPadX', 14)
        tab_w = (n.get('tableRect') or r)['w']
        tab_h = (n.get('tableRect') or r)['h']
        tex = (f'\\begin{{adjustbox}}{{min width={fmt(mm(tab_w))}mm,max width=\\linewidth,max totalheight={fmt(mm(max(tab_h, 40)))}mm}}\n'
               f'{{\\wsfs{{{fmt(pt(fs))}}}{{{fmt(pt(fs * 1.25))}}}\\setlength{{\\tabcolsep}}{{{fmt(pt(padx))}pt}}\\renewcommand{{\\arraystretch}}{{1.15}}%\n'
               f'\\input{{{rel}}}}}\n'
               '\\end{adjustbox}\\par')
        if n.get('style', {}).get('ta') == 'center' or ctx.ta == 'center':
            tex = '{\\centering\n' + tex + '}'
        return tex

    def embed(self, n, ctx, log):
        r = n['rect']
        t = n['t']
        src = n.get('src') or ''
        if t == 'iframe' and src.endswith('.html'):
            html_path = self.assets.deck_dir / src
            if html_path.exists():
                fake = dict(n, t='table', src=src, html=html_path.read_text(encoding='utf-8'), tableFs=14, tdPadX=8,
                            tableRect=r)
                log.a(f'iframe {src}: converted its <table> directly')
                return self.table(fake, ctx, log)
        log.l(f'<{t}> {src}: not embeddable in PDF, placeholder box emitted')
        poster = ''
        if n.get('poster'):
            fake = {'t': 'img', 'src': n['poster'], 'rect': r, 'attrs': {}, 'style': n['style'], 'nw': 0, 'nh': 0}
            poster = f'\\includegraphics[width={fmt(mm(r["w"]))}mm,height={fmt(mm(r["h"]))}mm,keepaspectratio]{{{self.assets.image(fake, log)}}}\\par'
        return (f'\\begin{{tcolorbox}}[wsbox,colback=wsBoxBg,colframe=wsBorder,boxrule=0.4pt,width={fmt(mm(r["w"]))}mm,'
                f'height={fmt(mm(r["h"]))}mm,valign=center,halign=center]\n{poster}'
                f'{{\\color{{wsMuted}}[{t}: {escape_text(Path(src).name)} -- not embeddable in PDF]}}\n\\end{{tcolorbox}}')


# ---------------------------------------------------------------- helpers

INLINE_TAGS = {'a', 'span', 'b', 'strong', 'em', 'i', 'code', 'small', 'sup', 'sub', 'label', 'time',
               'abbr', 'cite', 'kbd', 'mark', 'u', 's'}


def is_inlineish(k, flexy=False):
    """Inline-level node? Flex/grid items are blockified by CSS, so inside such containers we
    fall back to the tag (a link or span with no block descendants is still 'inline')."""
    t = k.get('t')
    if t in ('text', 'math', 'br'):
        return True
    if t in ('el', 'img'):
        if k.get('inline'):
            return True
        if flexy and t == 'el' and k.get('tag') in INLINE_TAGS and k['style']['position'] not in ('absolute', 'fixed'):
            return not any(c.get('t') == 'el' and not c.get('inline') and c.get('tag') not in INLINE_TAGS
                           for c in k.get('children', []))
    return False


def walk_all(nodes):
    for n in nodes:
        yield n
        if isinstance(n, dict) and n.get('children'):
            yield from walk_all(n['children'])


def plain_text(n):
    if n is None:
        return ''
    if n.get('t') == 'text':
        return n['s']
    if n.get('t') == 'ws':
        return ' '
    if n.get('t') == 'math':
        return n['tex']
    return collapse_ws(''.join(plain_text(c) for c in n.get('children', []))).strip()


def union_rects(rects):
    rects = [r for r in rects if r and r.get('w', 0) > 0 and r.get('h', 0) > 0]
    if not rects:
        return None
    x1 = min(r['x'] for r in rects); y1 = min(r['y'] for r in rects)
    x2 = max(r['x'] + r['w'] for r in rects); y2 = max(r['y'] + r['h'] for r in rects)
    return {'x': x1, 'y': y1, 'w': x2 - x1, 'h': y2 - y1}


def x_overlap(a, b):
    left = max(a['x'], b['x']); right = min(a['x'] + a['w'], b['x'] + b['w'])
    return right - left


def overlap_frac(a, b):
    """Vertical overlap fraction of the smaller rect (0..1), requiring real horizontal overlap too."""
    if not a or not b:
        return 0
    top = max(a['y'], b['y']); bot = min(a['y'] + a['h'], b['y'] + b['h'])
    if bot <= top:
        return 0
    hmin = min(a['h'], b['h']) or 1
    xo = x_overlap(a, b)
    if xo < 3 or xo < 0.3 * min(a['w'], b['w']):
        return 0
    return (bot - top) / hmin


def cluster_columns(blocks):
    """Cluster blocks into horizontal columns by x-range overlap."""
    items = sorted([b for b in blocks if b.get('rect') and b['rect']['w'] > 0], key=lambda b: b['rect']['x'])
    clusters = []
    for b in items:
        r = b['rect']
        placed = False
        for c in clusters:
            cr = c['rect']
            left = max(cr['x'], r['x']); right = min(cr['x'] + cr['w'], r['x'] + r['w'])
            if right - left > 0.5 * min(cr['w'], r['w']):
                c['items'].append(b)
                c['rect'] = union_rects([cr, r])
                placed = True
                break
        if not placed:
            clusters.append({'items': [b], 'rect': dict(r)})
    for c in clusters:
        c['items'].sort(key=lambda b: b['rect']['y'])
    clusters.sort(key=lambda c: c['rect']['x'])
    return clusters


def strip_tex(s):
    s = re.sub(r'\\[a-zA-Z]+\*?(\[[^\]]*\])?', ' ', s)
    s = re.sub(r'[{}<>\-\d.,\s]+', ' ', s)
    return s

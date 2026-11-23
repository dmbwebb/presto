"""Text, colour, unit and step-range helpers for the WebSlides -> Beamer emitter."""
import re

SLIDE_W, SLIDE_H = 1280, 720
PX_TO_PT = 0.35433  # 1280px slide == 160mm paper  -> 1px = 0.125mm = 0.35433pt
PX_TO_MM = 0.125


def pt(px, nd=1):
    return round(px * PX_TO_PT, nd)


def mm(px, nd=2):
    return round(px * PX_TO_MM, nd)


def fmt(v):
    s = f"{v:.2f}".rstrip('0').rstrip('.')
    return s if s else '0'


# ---------------------------------------------------------------- WebSlides steps

def parse_range(raw):
    """WebSlides step range -> (start, end|None) in *web step* numbers, or None."""
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == '':
        return (1, None)
    if raw.endswith('+'):
        try:
            return (int(raw[:-1]), None)
        except ValueError:
            return None
    if '-' in raw:
        a, b = raw.split('-', 1)
        try:
            a, b = int(a), int(b)
        except ValueError:
            return None
        return (a, b) if b >= a else None
    try:
        return (int(raw), None)
    except ValueError:
        return None


def overlay_spec(raw):
    """data-step value -> beamer overlay spec like '<2->' / '<2-3>' ('' if unparsable/absent)."""
    r = parse_range(raw)
    if r is None:
        return ''
    a, b = r
    return f'<{a + 1}->' if b is None else f'<{a + 1}-{b + 1}>'


def is_active(rng, step):
    """Is a parsed step range visible at this web step?"""
    a, b = rng
    return step >= a and (b is None or step <= b)


# ---------------------------------------------------------------- colours

def css_color_to_hex(c, bg=(255, 255, 255)):
    """rgb()/rgba()/#hex/named -> ('RRGGBB', alpha) or (None, 0) if transparent."""
    if not c:
        return None, 0.0
    c = c.strip()
    if c == 'transparent':
        return None, 0.0
    m = re.match(r'rgba?\(([^)]+)\)', c)
    if m:
        parts = [p.strip() for p in re.split(r'[,\s/]+', m.group(1)) if p.strip()]
        r, g, b = [float(p) for p in parts[:3]]
        a = float(parts[3]) if len(parts) > 3 else 1.0
        if a <= 0.001:
            return None, 0.0
        if a < 0.999:
            # flatten against bg
            r = r * a + bg[0] * (1 - a)
            g = g * a + bg[1] * (1 - a)
            b = b * a + bg[2] * (1 - a)
        return '%02X%02X%02X' % (round(r), round(g), round(b)), a
    if c.startswith('#'):
        h = c[1:]
        if len(h) == 3:
            h = ''.join(ch * 2 for ch in h)
        return h[:6].upper(), 1.0
    named = {'white': 'FFFFFF', 'black': '000000', 'red': 'FF0000', 'gray': '808080', 'grey': '808080'}
    if c.lower() in named:
        return named[c.lower()], 1.0
    return None, 0.0


class Palette:
    """Maps hex colours to LaTeX colour names, seeding from CSS variables."""

    def __init__(self, css_vars):
        self.names = {}       # hex -> name
        self.order = []       # (name, hex, source)
        for var, value in css_vars.items():
            hx, a = css_color_to_hex(value)
            if not hx or hx in self.names:
                continue
            name = self._var_to_name(var)
            self.names[hx] = name
            self.order.append((name, hx, var))
        self.auto = 0

    @staticmethod
    def _var_to_name(var):
        base = var.replace('--color-', '').replace('--ws-', '').replace('--', '')
        parts = re.split(r'[-_]', base)
        words = {'2': 'Two', 'bg': 'Bg'}
        return 'ws' + ''.join(words.get(p, p.capitalize()) for p in parts if p)

    def name(self, hx):
        if hx is None:
            return None
        if hx not in self.names:
            self.auto += 1
            nm = f'wsColor{self.auto}'
            self.names[hx] = nm
            self.order.append((nm, hx, 'auto'))
        return self.names[hx]

    def ensure(self, name, hx):
        """Make sure a colour with this name exists (used for exporter defaults)."""
        if any(existing == name for existing, _, _ in self.order):
            return name
        if hx in self.names:
            return self.names[hx]
        self.names[hx] = name
        self.order.append((name, hx, 'default'))
        return name

    def definitions(self):
        lines = []
        for name, hx, src in self.order:
            lines.append(f'\\definecolor{{{name}}}{{HTML}}{{{hx}}}' + (f'  % {src}' if src != 'auto' else ''))
        return '\n'.join(lines)


# ---------------------------------------------------------------- text

_ESC = {
    '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#', '_': r'\_',
    '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}',
    '\\': r'\textbackslash{}',
}

_UNI = {
    ' ': '~', ' ': '~', ' ': r'\,', '​': '',
    '→': r'\ensuremath{\rightarrow}', '←': r'\ensuremath{\leftarrow}',
    '↑': r'\ensuremath{\uparrow}', '↓': r'\ensuremath{\downarrow}',
    '↳': r'\ensuremath{\hookrightarrow}', '⇒': r'\ensuremath{\Rightarrow}',
    '–': '--', '—': '---', '−': r'\ensuremath{-}',
    '‘': '`', '’': "'", '“': '``', '”': "''",
    '·': r'\textperiodcentered{}', '•': r'\textbullet{}',
    '…': r'\ldots{}', '≈': r'\ensuremath{\approx}',
    '≥': r'\ensuremath{\geq}', '≤': r'\ensuremath{\leq}', '≠': r'\ensuremath{\neq}',
    '×': r'\ensuremath{\times}', '±': r'\ensuremath{\pm}', '°': r'\textdegree{}',
    '✓': r'\checkmark{}', '✔': r'\checkmark{}', '✗': r'\ensuremath{\times}',
    '½': r'\textonehalf{}', '©': r'\textcopyright{}', '€': r'\texteuro{}',
    'α': r'\ensuremath{\alpha}', 'β': r'\ensuremath{\beta}', 'τ': r'\ensuremath{\tau}',
    'σ': r'\ensuremath{\sigma}', 'μ': r'\ensuremath{\mu}', 'Δ': r'\ensuremath{\Delta}',
    '∞': r'\ensuremath{\infty}',
}

_MATH_RE = re.compile(r'(?<!\\)\$([^$\n]{1,200}?)(?<!\\)\$')


def escape_text(s, log=None, allow_math=True):
    """Escape a text run for LaTeX. Balanced $...$ segments are passed through as math."""
    out = []
    pos = 0
    if allow_math:
        for m in _MATH_RE.finditer(s):
            out.append(_escape_plain(s[pos:m.start()], log))
            out.append('$' + m.group(1) + '$')
            pos = m.end()
    out.append(_escape_plain(s[pos:], log))
    return ''.join(out)


def _escape_plain(s, log=None):
    res = []
    for ch in s:
        if ch in _ESC:
            res.append(_ESC[ch])
        elif ch in _UNI:
            res.append(_UNI[ch])
        elif ord(ch) < 128 or ch.isalpha():
            # ASCII and accented Latin letters (utf8 inputenc + T1 handle them)
            res.append(ch)
        else:
            if log is not None:
                log.append(f'unmapped character U+{ord(ch):04X} ({ch!r}) replaced by ?')
            res.append('?')
    return ''.join(res)


def collapse_ws(s):
    # keep nbsp (handled by escape), collapse ordinary whitespace runs
    return re.sub(r'[ \t\r\n\f]+', ' ', s)


SERIF_FAMILIES = ('newsreader', 'georgia', 'times', 'serif', 'source serif', 'garamond', 'charter', 'palatino')
MONO_FAMILIES = ('mono', 'consolas', 'courier', 'menlo')


def _first_family(ff):
    return (ff or '').lower().split(',')[0].strip().strip('"\'')


def is_serif(ff):
    first = _first_family(ff)
    return any(n in first for n in SERIF_FAMILIES)


def is_mono(ff):
    first = _first_family(ff)
    return any(n in first for n in MONO_FAMILIES)

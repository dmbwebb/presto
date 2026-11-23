"""svgkit: a small SVG builder for WebSlides figures.

Figures are drawn in slide pixels (the deck is 1280 x 720), so a figure shown at its
natural width keeps 1 SVG unit = 1 px and text sizes are what the slide shows. Inline
the file with ``<img src="figures/x.svg" data-inline>`` and the engine reveals its
groups step by step.

Standard library only and Python 3.9 compatible, so a deck generator can import it
from any venv. From a deck at ``<project>/presentations/webslides/<deck>/code/``::

    import os, sys
    ENGINE = os.path.realpath(os.path.join(os.path.dirname(__file__), "../../engine"))
    sys.path.insert(0, os.path.join(os.path.dirname(ENGINE), "tools"))
    from svgkit import SVG, Scale, figure, run_cli

A generator registers one function per figure and ends with ``run_cli()``::

    @figure("walk_fit")
    def walk_fit():
        svg = SVG(760, 500)
        with svg.g(step="1", label="fitted line"):
            svg.polyline(points, stroke="accent2", width=4)
        return svg

    if __name__ == "__main__":
        run_cli(out="../figures")

which gives ``--only walk_fit,walk_*``, ``--list``, ``--out DIR``, ``--check`` and
``--json``. Files are written atomically and only when their bytes change (Presto
hot-reloads on mtime).

Every drawing method accepts the same optional attributes, passed through verbatim:
``id``, ``label`` (data-label, the name ``ws outline`` shows), ``step`` (data-step),
``dclass`` (data-class), ``fade_after`` (data-fade-after), ``focus`` (data-focus),
``morph`` (data-morph), ``stat`` (data-stat), ``cls`` (class), ``style``,
``transform``, ``opacity``; any other ``foo_bar=`` keyword becomes ``foo-bar=``.
Colours may be palette role names (``"accent"``, ``"muted"``, ...) or CSS colours.
"""
import argparse
import fnmatch
import json
import math
import numbers
import os
import re
import subprocess
import sys
import tempfile
import traceback
from contextlib import contextmanager
from html import escape

FONT = "Lato, Helvetica Neue, Arial, sans-serif"


# ---------------------------------------------------------------------------
# Colours

class Palette:
    """Colour roles. Defaults are the academic theme's (engine/themes/academic.css);
    pass overrides or extra roles: ``Palette(accent="#2C3E5A", lin="#2464b4")``."""

    DEFAULTS = dict(
        text="#2F3037", ink="#1E2A3A", accent="#B8612A", accent2="#3F73B8",
        muted="#6F747C", faint="#979BA2", border="#D9DDE3", grid="#E6E6E6",
        card="#F7F8F9", box="#F3F1ED", bg="#FFFFFF",
        point="#6B6F78",    # data marks that are not the point of the slide
        context="#C3C7CD",  # intervals and marks that are context
        red="#C44536", orange="#D98B48", green="#2D8A4E", blue="#4A7FBF", yellow="#C9961A",
    )

    def __init__(self, **roles):
        self.roles = dict(self.DEFAULTS, **roles)

    def __getattr__(self, name):
        try:
            return self.__dict__["roles"][name]
        except KeyError:
            raise AttributeError(name)

    def __call__(self, colour, default=None):
        """Resolve a role name or pass a CSS colour through; None gives the default role."""
        if colour is None:
            colour = default
        return self.roles.get(colour, colour) if isinstance(colour, str) else colour


ACADEMIC = Palette()


# ---------------------------------------------------------------------------
# Text and numbers

class Markup(str):
    """SVG markup that text() must not escape. Concatenating with a plain str escapes
    the plain part, so ``"Slope " + mi("β")`` stays safe. f-strings lose the type:
    use + or pass raw=True."""

    def __add__(self, other):
        return Markup(str.__add__(self, _esc(other)))

    def __radd__(self, other):
        return Markup(_esc(other) + str(self))


def _esc(s):
    return s if isinstance(s, Markup) else escape(str(s), quote=False)


def tspan(s, **kw):
    """A <tspan> with any attributes: ``tspan("labels", font_size=21, fill="#6F747C")``."""
    return Markup(f"<tspan {_attrs(kw)}>{_esc(s)}</tspan>")


def mi(s):
    """Math italic symbol in the deck's KaTeX font (needs the deck's KaTeX CSS)."""
    return Markup(f'<tspan font-family="KaTeX_Math" font-style="italic" font-weight="400">{_esc(s)}</tspan>')


def mr(s):
    """Upright math (operators, digits, brackets) in the deck's KaTeX font."""
    return Markup(f'<tspan font-family="KaTeX_Main" font-weight="400">{_esc(s)}</tspan>')


def mbb(s):
    """Blackboard bold, as \\mathbb in KaTeX."""
    return Markup(f'<tspan font-family="KaTeX_AMS">{_esc(s)}</tspan>')


def fmt(x):
    """A coordinate: at most two decimals, no trailing zeros, no negative zero."""
    if isinstance(x, numbers.Real) and not isinstance(x, numbers.Integral):
        s = f"{float(x):.2f}".rstrip("0").rstrip(".")
        return "0" if s == "-0" else s
    return str(x)


def num(v, spec="g"):
    """A tick label with a typographic minus."""
    return format(v, spec).replace("-", "−")


def nice_ticks(lo, hi, n=5):
    """About n round tick values covering [lo, hi]."""
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / max(n - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    out, v = [], first
    while v <= hi + step * 1e-9:
        out.append(round(v, 10) + 0.0)
        v += step
    return out


_WIDE = set("mwMW@%")
_NARROW = set("iljtfr.,:;'!|()[] ")


def text_width(s, size=20, bold=False):
    """Rough rendered width of a label in Lato (markup tags and $ stripped)."""
    plain = re.sub(r"<[^>]+>", "", str(s)).replace("$", "")
    em = 0.0
    for ch in plain:
        if ch in _WIDE:
            em += 0.82
        elif ch in _NARROW:
            em += 0.28
        elif ch.isupper():
            em += 0.64
        else:
            em += 0.53
    return em * size * (1.05 if bold else 1.0)


# ---------------------------------------------------------------------------
# Attributes

_ALIASES = {
    "cls": "class", "step": "data-step", "dclass": "data-class",
    "fade_after": "data-fade-after", "focus": "data-focus", "morph": "data-morph",
    "label": "data-label", "stat": "data-stat",
}


def _attrs(kw):
    out = []
    for k, v in kw.items():
        if v is None or v is False:
            continue
        name = _ALIASES.get(k) or k.rstrip("_").replace("_", "-")
        out.append(f'{name}="{escape(fmt(v), quote=True)}"')
    return " ".join(out)


def _points_path(pts):
    """Path data visiting ``pts`` in order: "Mx,y Lx,y ..."."""
    return "M" + " L".join(f"{fmt(x)},{fmt(y)}" for x, y in pts)


class Scale:
    """Linear map from data to pixels: ``sx = Scale(0, 24, 120, 740); sx(6)``."""

    def __init__(self, d0, d1, p0, p1):
        self.d0, self.d1, self.p0, self.p1 = d0, d1, p0, p1

    def __call__(self, v):
        return self.p0 + (v - self.d0) * (self.p1 - self.p0) / (self.d1 - self.d0)

    def invert(self, p):
        return self.d0 + (p - self.p0) * (self.d1 - self.d0) / (self.p1 - self.p0)

    @property
    def per_unit(self):
        return abs((self.p1 - self.p0) / (self.d1 - self.d0))

    def ticks(self, n=5):
        return nice_ticks(min(self.d0, self.d1), max(self.d0, self.d1), n)


# ---------------------------------------------------------------------------
# The builder

class SVG:
    """An SVG canvas in slide pixels. ``css`` is added in a <style> block (it becomes
    document-wide once inlined, so prefix its class names); ``**root`` are attributes
    of the <svg> element itself (e.g. ``dclass="wm-time:5"``, ``focus="3:.fit"``)."""

    def __init__(self, width, height, css="", palette=None, font=FONT, **root):
        self.w, self.h = width, height
        self.css = css
        self.pal = palette or ACADEMIC
        self.font = font
        self.root = root
        self.parts = []
        self.defs = []          # raw markup for <defs> (gradients, clip paths, patterns)
        self._depth = 0
        self._ids = set()

    # -- low level -------------------------------------------------------------
    def add(self, s):
        """Append raw markup; <g> opened or closed here counts toward close_g() and render()."""
        opened = len(re.findall(r"<g[\s>/]", s)) - len(re.findall(r"<g\b[^>]*/>", s))
        closed = s.count("</g>")
        if closed > opened:
            self._depth = max(0, self._depth - (closed - opened))
        self.parts.append(" " * self._depth + s)
        if opened > closed:
            self._depth += opened - closed

    def _claim_id(self, kw):
        ident = kw.get("id") or kw.get("id_")
        if ident is not None:
            if ident in self._ids:
                raise ValueError(f"duplicate id {ident!r} in figure")
            self._ids.add(ident)

    def _el(self, tag, geom, kw, content=None):
        self._claim_id(kw)
        a = _attrs(dict(geom, **kw))
        self.add(f"<{tag} {a}/>" if content is None else f"<{tag} {a}>{content}</{tag}>")

    def c(self, colour, default="text"):
        return self.pal(colour, default)

    # -- groups ------------------------------------------------------------------
    def open_g(self, **kw):
        self._claim_id(kw)
        a = _attrs(kw)
        self.add(f"<g {a}>" if a else "<g>")

    def close_g(self):
        if self._depth == 0:
            raise ValueError("close_g() without open_g()")
        self.add("</g>")

    @contextmanager
    def g(self, **kw):
        """``with svg.g(step="2-3", label="pooled"):`` groups everything drawn inside."""
        self.open_g(**kw)
        try:
            yield self
        finally:
            self.close_g()

    # -- primitives --------------------------------------------------------------
    def line(self, x1, y1, x2, y2, stroke=None, width=1.5, dash=None, cap=None, **kw):
        self._el("line", dict(x1=x1, y1=y1, x2=x2, y2=y2, stroke=self.c(stroke), stroke_width=width,
                              stroke_dasharray=dash, stroke_linecap=cap), kw)

    def circle(self, cx, cy, r, fill="point", stroke=None, width=None, **kw):
        self._el("circle", dict(cx=cx, cy=cy, r=r, fill=self.c(fill), stroke=self.c(stroke, None),
                                stroke_width=width), kw)

    def rect(self, x, y, w, h, fill="none", stroke=None, width=None, rx=None, dash=None, **kw):
        self._el("rect", dict(x=x, y=y, width=w, height=h, rx=rx, fill=self.c(fill),
                              stroke=self.c(stroke, None), stroke_width=width, stroke_dasharray=dash), kw)

    def path(self, d, fill="none", stroke=None, width=2, dash=None, cap="round", join="round", **kw):
        self._el("path", dict(d=d, fill=self.c(fill), stroke=self.c(stroke), stroke_width=width,
                              stroke_dasharray=dash, stroke_linecap=cap, stroke_linejoin=join), kw)

    def polyline(self, pts, **kw):
        self.path(_points_path(pts), **kw)

    def polygon(self, pts, fill="text", stroke="none", **kw):
        d = _points_path(pts) + " Z"
        kw.setdefault("width", None)
        kw.setdefault("cap", None)
        kw.setdefault("join", None)
        self.path(d, fill=fill, stroke=stroke, **kw)

    def text(self, x, y, s, size=20, fill=None, anchor=None, weight=None, baseline=None, rotate=None,
             halo=False, raw=False, line_height=1.25, **kw):
        """A label. ``s`` may hold $...$ math (left untouched for the engine), Markup from
        mi()/mr()/tspan(), or newlines for extra lines. ``halo=True`` (or a width) draws a
        background-coloured outline so the label stays legible over data."""
        content = s if (raw or isinstance(s, Markup)) else _esc(s)
        lines = content.split("\n")
        if len(lines) > 1:
            dy = fmt(size * line_height)
            content = lines[0] + "".join(f'<tspan x="{fmt(x)}" dy="{dy}">{ln}</tspan>' for ln in lines[1:])
        geom = dict(x=x, y=y, font_size=size, fill=self.c(fill), text_anchor=anchor if anchor != "start" else None,
                    font_weight=weight, dominant_baseline=baseline,
                    transform=f"rotate({fmt(rotate)} {fmt(x)} {fmt(y)})" if rotate is not None else None)
        if halo:
            geom.update(stroke=self.pal.bg, stroke_width=6 if halo is True else halo,
                        stroke_linejoin="round", paint_order="stroke")
        self._el("text", geom, kw, content)

    def label(self, x, y, s, **kw):
        """Direct label over data: text with a halo, bold by default."""
        kw.setdefault("halo", True)
        kw.setdefault("weight", 700)
        self.text(x, y, s, **kw)

    # -- annotations ---------------------------------------------------------------
    def arrow(self, x1, y1, x2, y2, color=None, width=3, head=None, start=False, end=True, bend=0,
              dash=None, **kw):
        """Arrow from (x1, y1) to (x2, y2) whose tips land exactly on the end points.
        ``start=True`` adds a head at the start (double-headed); ``bend`` curves the shaft
        sideways by that fraction of its length (positive bends to the left of travel)."""
        col = self.c(color, "ink")
        head = head or max(10.0, 3 * width + 3)
        half = 0.6 * head
        L = math.hypot(x2 - x1, y2 - y1)
        if L == 0:
            raise ValueError("arrow with zero length")
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        cx, cy = mx + bend * L * (y2 - y1) / L, my - bend * L * (x2 - x1) / L
        p0, p1 = (x1, y1), (x2, y2)

        def unit(ax, ay, bx, by):
            d = math.hypot(bx - ax, by - ay) or 1.0
            return (bx - ax) / d, (by - ay) / d

        u_end = unit(cx, cy, x2, y2) if bend else unit(x1, y1, x2, y2)
        u_start = unit(cx, cy, x1, y1) if bend else unit(x2, y2, x1, y1)
        inset = 0.7 * head
        s0 = (x1 - u_start[0] * inset, y1 - u_start[1] * inset) if start else p0
        s1 = (x2 - u_end[0] * inset, y2 - u_end[1] * inset) if end else p1
        with self.g(**kw):
            if L > inset * (int(start) + int(end)):
                if bend:
                    self.path(f"M{fmt(s0[0])},{fmt(s0[1])} Q{fmt(cx)},{fmt(cy)} {fmt(s1[0])},{fmt(s1[1])}",
                              stroke=col, width=width, dash=dash, cap="butt")
                else:
                    self.line(s0[0], s0[1], s1[0], s1[1], stroke=col, width=width, dash=dash)
            for on, tip, u in ((start, p0, u_start), (end, p1, u_end)):
                if on:
                    self._head(tip, u, head, half, col)

    def _head(self, tip, u, head, half, col):
        tx, ty = tip
        bx, by = tx - u[0] * head, ty - u[1] * head
        nx, ny = -u[1], u[0]
        notch = (tx - u[0] * head * 0.78, ty - u[1] * head * 0.78)
        self.polygon([(tx, ty), (bx + nx * half, by + ny * half), notch, (bx - nx * half, by - ny * half)], fill=col)

    def bracket(self, x1, y1, x2, y2, kind="square", depth=None, color=None, width=2.5, text=None, size=22,
                gap=10, weight=None, text_color=None, flip=False, **kw):
        """A square or curly bracket spanning (x1, y1)-(x2, y2), e.g. "these three analyses
        are one review". Its arms point at the bracketed things, which lie to the right of
        the direction of travel (top-to-bottom brackets things on its left; left-to-right
        brackets things below). ``flip=True`` swaps sides; ``text`` labels the outside."""
        col = self.c(color, "ink")
        L = math.hypot(x2 - x1, y2 - y1)
        if L == 0:
            raise ValueError("bracket with zero length")
        ux, uy = (x2 - x1) / L, (y2 - y1) / L
        nx, ny = uy, -ux                      # toward the label, away from the bracketed things
        if flip:
            nx, ny = -nx, -ny

        def P(s, t):
            return f"{fmt(x1 + s * ux + t * nx)},{fmt(y1 + s * uy + t * ny)}"

        d = depth or (16 if kind == "curly" else 12)
        if kind == "square":
            path = f"M{P(0, 0)} L{P(0, d)} L{P(L, d)} L{P(L, 0)}"
        elif kind == "curly":
            r = min(d / 2, L / 4)
            h = L / 2
            path = (f"M{P(0, 0)} Q{P(0, d / 2)} {P(r, d / 2)} L{P(h - r, d / 2)} Q{P(h, d / 2)} {P(h, d)}"
                    f" Q{P(h, d / 2)} {P(h + r, d / 2)} L{P(L - r, d / 2)} Q{P(L, d / 2)} {P(L, 0)}")
        else:
            raise ValueError(f"bracket kind must be 'square' or 'curly', not {kind!r}")
        with self.g(**kw):
            self.path(path, stroke=col, width=width, cap="round", join="round" if kind == "curly" else "miter")
            if text is not None:
                tx, ty = x1 + ux * L / 2 + nx * (d + gap), y1 + uy * L / 2 + ny * (d + gap)
                n_lines = str(text).count("\n")
                if abs(nx) >= abs(ny):
                    anchor = "start" if nx > 0 else "end"
                    ty += size * 0.35 - n_lines * size * 0.625
                else:
                    anchor = "middle"
                    ty = ty - n_lines * size * 1.25 if ny < 0 else ty + size * 0.8
                self.text(tx, ty, text, size=size, fill=text_color or col, anchor=anchor, weight=weight)

    def errorbar(self, x, y, lo, hi, color=None, width=2.2, cap=0, r=6.5, point=None, horizontal=False, **kw):
        """An interval from lo to hi (pixels) through the point at (x, y). Vertical by
        default; ``horizontal=True`` runs lo..hi along x. ``point`` colours the dot
        (default: the interval colour); ``r=0`` draws no dot; ``cap`` adds end ticks."""
        col = self.c(color, "context")
        with self.g(**kw):
            if horizontal:
                self.line(lo, y, hi, y, stroke=col, width=width)
                for e in (lo, hi) if cap else ():
                    self.line(e, y - cap / 2, e, y + cap / 2, stroke=col, width=width)
            else:
                self.line(x, lo, x, hi, stroke=col, width=width)
                for e in (lo, hi) if cap else ():
                    self.line(x - cap / 2, e, x + cap / 2, e, stroke=col, width=width)
            if r:
                self.circle(x, y, r, fill=point or ("point" if color is None else col))

    def callout(self, x, y, lines, w=None, title=None, size=22, title_size=None, pad=14, anchor="tl",
                fill="bg", stroke="accent", border=2, radius=10, color=None, line_height=1.3,
                point_to=None, pointer_gap=6, **kw):
        """A boxed note. (x, y) is the box's ``anchor`` corner ("tl", "tc", "tr", "cl", "c",
        "cr", "bl", "bc", "br"); ``lines`` is a string (newlines split) or a list;
        ``point_to=(px, py)`` adds an arrow from the box edge to that point. Returns the
        box as (x, y, w, h)."""
        rows = lines.split("\n") if isinstance(lines, str) else list(lines)
        ts = title_size or size
        lh = size * line_height
        h = 2 * pad + (ts * line_height if title else 0) + size + lh * (len(rows) - 1)
        if w is None:
            widths = [text_width(r, size) for r in rows] + ([text_width(title, ts, True)] if title else [])
            w = max(widths) + 2 * pad
        fx = {"l": 0, "c": 0.5, "r": 1}
        fy = {"t": 0, "c": 0.5, "b": 1}
        key = anchor if len(anchor) == 2 else ("cc" if anchor == "c" else anchor)
        bx, by = x - fx[key[1]] * w, y - fy[key[0]] * h
        with self.g(**kw):
            if point_to is not None:
                px, py = point_to
                cx, cy = bx + w / 2, by + h / 2
                dx, dy = px - cx, py - cy
                t = min(w / 2 / abs(dx) if dx else math.inf, h / 2 / abs(dy) if dy else math.inf)
                if t < 1:
                    L = math.hypot(dx, dy)
                    ex, ey = px - dx / L * pointer_gap, py - dy / L * pointer_gap
                    self.arrow(cx + dx * t, cy + dy * t, ex, ey, color=stroke, width=max(border, 2))
            self.rect(bx, by, w, h, fill=fill, stroke=stroke, width=border, rx=radius)
            tx = bx + pad
            ty = by + pad + size * 0.8
            if title:
                self.text(tx, by + pad + ts * 0.8, title, size=ts, fill=color or "ink", weight=700)
                ty += ts * line_height
            for row in rows:
                self.text(tx, ty, row, size=size, fill=color or "text")
                ty += lh
        return bx, by, w, h

    # -- axes ----------------------------------------------------------------------
    def axis_x(self, sc, y, ticks=None, labels=None, title=None, line=True, grid_to=None, size=20,
               color="muted", gap=28, title_gap=58, x0=None, x1=None, **kw):
        """Bottom axis at pixel y: baseline, tick labels, optional vertical gridlines up to
        ``grid_to`` and a centred title."""
        ticks = sc.ticks() if ticks is None else ticks
        x0 = sc.p0 if x0 is None else x0
        x1 = sc.p1 if x1 is None else x1
        with self.g(**kw):
            if grid_to is not None:
                for v in ticks:
                    self.line(sc(v), y, sc(v), grid_to, stroke="grid", width=1)
            if line:
                self.line(x0, y, x1, y, stroke=color, width=1.4)
            for i, v in enumerate(ticks):
                self.text(sc(v), y + gap, labels[i] if labels else num(v), size=size, fill=color, anchor="middle")
            if title:
                self.text((sc.p0 + sc.p1) / 2, y + title_gap, title, size=size, fill=color, anchor="middle")

    def axis_y(self, sc, x, ticks=None, labels=None, title=None, grid_to=None, zero_dash=True, line=False,
               size=20, color="muted", gap=12, title_x=None, **kw):
        """Left axis at pixel x: tick labels, horizontal gridlines to ``grid_to`` (the zero
        line dashed and darker), optional axis line and a rotated title at ``title_x``."""
        ticks = sc.ticks() if ticks is None else ticks
        with self.g(**kw):
            if grid_to is not None:
                for v in ticks:
                    if v == 0:
                        self.line(x, sc(v), grid_to, sc(v), stroke=color, width=1.4, dash="5 5" if zero_dash else None)
                    else:
                        self.line(x, sc(v), grid_to, sc(v), stroke="grid", width=1)
            if line:
                self.line(x, sc.p0, x, sc.p1, stroke=color, width=1.4)
            for i, v in enumerate(ticks):
                self.text(x - gap, sc(v) + size * 0.35, labels[i] if labels else num(v), size=size, fill=color,
                          anchor="end")
            if title:
                tx = x - 58 if title_x is None else title_x
                self.text(tx, (sc.p0 + sc.p1) / 2, title, size=size, fill=color, anchor="middle", rotate=-90)

    # -- output --------------------------------------------------------------------
    def render(self, name=None):
        if self._depth:
            raise ValueError(f"{self._depth} unclosed <g> group(s)")
        root = dict(xmlns="http://www.w3.org/2000/svg", width=self.w, height=self.h,
                    viewBox=f"0 0 {fmt(self.w)} {fmt(self.h)}", font_family=self.font,
                    data_svgkit=name)
        root.update(self.root)
        head = [f"<svg {_attrs(root)}>"]
        if self.css:
            head.append(f"<style>{self.css}</style>")
        if self.defs:
            head.append("<defs>" + "".join(self.defs) + "</defs>")
        return "\n".join(head + self.parts + ["</svg>"]) + "\n"

    def save(self, path, name=None):
        """Write atomically; returns False when the file already had these bytes."""
        return write_if_changed(path, self.render(name))


# ---------------------------------------------------------------------------
# Files

def write_if_changed(path, data):
    """Atomically replace ``path`` with ``data`` unless it already holds exactly those
    bytes (keeps the mtime, so Presto does not reload). Returns True if written."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    path = os.path.realpath(path)
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            subprocess.run(["trash", tmp], check=False)
        raise
    return True


def _encode(result, name):
    if isinstance(result, SVG):
        return result.render(name).encode("utf-8")
    if isinstance(result, bytes):
        return result
    if isinstance(result, str):
        return result.encode("utf-8")
    if isinstance(result, (dict, list)):
        return (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    raise TypeError(f"figure {name!r} returned {type(result).__name__}; return an SVG, str, bytes, dict or list")


_STEP_ATTRS = ("data-step", "data-fade-after", "data-focus", "data-morph", "data-class")


def scan_steps(data):
    """Distinct values of each step attribute in an SVG, in document order."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    out = {}
    for a in _STEP_ATTRS:
        seen = []
        for v in re.findall(rf'\s{a}="([^"]*)"', text):
            if v not in seen:
                seen.append(v)
        out[a] = seen
    return out


# ---------------------------------------------------------------------------
# Figure registry

class Figure:
    def __init__(self, name, func, filename):
        self.name, self.func, self.filename = name, func, filename
        self.doc = (func.__doc__ or "").strip().split("\n")[0]


REGISTRY = {}


def figure(name=None, filename=None, registry=None):
    """Register a figure function: ``@figure("walk_fit")`` writes walk_fit.svg from the
    SVG it returns. ``filename=`` names other outputs (a returned dict becomes JSON)."""
    reg = REGISTRY if registry is None else registry

    def deco(func):
        nm = name if isinstance(name, str) else func.__name__
        if nm in reg:
            raise ValueError(f"figure {nm!r} registered twice")
        reg[nm] = Figure(nm, func, filename or nm + ".svg")
        return func

    return deco(name) if callable(name) else deco


def _select(reg, only):
    if not only:
        return list(reg.values())
    pats = [p.strip() for chunk in only for p in chunk.split(",") if p.strip()]
    chosen, unknown = [], []
    for p in pats:
        hits = [f for n, f in reg.items() if fnmatch.fnmatchcase(n, p)]
        if not hits:
            unknown.append(p)
        chosen += [f for f in hits if f not in chosen]
    if unknown:
        raise SystemExit(f"error: no figure matches {', '.join(unknown)}; known: {', '.join(reg) or '(none)'}")
    return [f for f in reg.values() if f in chosen]


def _summary_line(r):
    if r["status"] == "error":
        return f'{r["name"]}  ERROR  {r["error"]}'
    s = r["steps"]
    bits = [f'steps {",".join(s["data-step"])}' if s["data-step"] else "no steps"]
    for a, lab in (("data-fade-after", "fade-after"), ("data-focus", "focus"), ("data-morph", "morph"),
                   ("data-class", "class")):
        if s[a]:
            bits.append(f'{lab} {",".join(s[a])}')
    return f'{r["name"]}  {r["path"]}  {r["bytes"] / 1024:.1f} kB  {r["status"]}  ' + "; ".join(bits)


def run_cli(argv=None, out=None, registry=None, prog=None):
    """Command line for a figure generator. ``out`` is the default output folder,
    relative to the generator script (so it works from any working directory)."""
    reg = REGISTRY if registry is None else registry
    main = sys.modules.get("__main__")
    script_dir = os.path.dirname(os.path.abspath(getattr(main, "__file__", None) or "."))
    default_out = os.path.join(script_dir, out) if out and not os.path.isabs(out) else (out or os.getcwd())
    ap = argparse.ArgumentParser(prog=prog, description="Regenerate figures (all by default).")
    ap.add_argument("--only", action="append", metavar="NAMES",
                    help="comma-separated figure names or globs (walk_fit,walk_*); repeatable")
    ap.add_argument("--list", action="store_true", help="list figures and exit")
    ap.add_argument("--out", default=default_out, help=f"output folder (default {os.path.relpath(default_out)})")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if any output would change")
    ap.add_argument("--json", action="store_true", help="print one JSON record per figure")
    args = ap.parse_args(argv)

    if args.list:
        for f in reg.values():
            print(f"{f.name}  {f.filename}" + (f"  {f.doc}" if f.doc else ""))
        return 0
    results = []
    for f in _select(reg, args.only):
        path = os.path.join(args.out, f.filename)
        rec = dict(name=f.name, path=os.path.relpath(path))
        try:
            data = _encode(f.func(), f.name)
            if args.check:
                try:
                    with open(path, "rb") as fh:
                        same = fh.read() == data
                except FileNotFoundError:
                    same = False
                status = "unchanged" if same else "would change"
            else:
                status = "written" if write_if_changed(path, data) else "unchanged"
            rec.update(status=status, bytes=len(data), steps=scan_steps(data))
        except Exception as e:
            traceback.print_exc()
            tb = traceback.extract_tb(e.__traceback__)[-1]
            rec.update(status="error", error=f"{type(e).__name__}: {e} ({os.path.basename(tb.filename)}:{tb.lineno})")
        results.append(rec)
        print(json.dumps(rec) if args.json else _summary_line(rec), flush=True)
    failed = any(r["status"] == "error" for r in results)
    stale = args.check and any(r["status"] == "would change" for r in results)
    code = 1 if failed or stale else 0
    if argv is None and code:
        sys.exit(code)
    return code


# ---------------------------------------------------------------------------
# Small helpers kept from the TCD deck's svgkit, so its generators port unchanged

def grid_y(svg, sc, x0, x1, ticks, labels=None, zero_dash=True, gap=12):
    svg.axis_y(sc, x0, ticks, labels=labels, grid_to=x1, zero_dash=zero_dash, gap=gap)


def axis_x(svg, sc, y, ticks, labels=None, title=None, line=True, x0=None, x1=None, title_dy=58):
    svg.axis_x(sc, y, ticks, labels=labels, title=title, line=line, x0=x0, x1=x1, title_gap=title_dy)


def y_title(svg, x, y, s):
    svg.text(x, y, s, size=20, fill="muted", anchor="middle", rotate=-90)


def read_stat(name, stats_dir):
    """A number saved by the paper's code as ``<stats_dir>/<name>.tex``."""
    with open(os.path.join(stats_dir, name + ".tex")) as f:
        return f.read().strip().rstrip("%").strip()

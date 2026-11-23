"""Figures for the svgkit demo deck (invented numbers, for illustration only).

    python3 make_figures.py                  # every figure
    python3 make_figures.py --only fit       # one figure
    python3 make_figures.py --list
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
from svgkit import SVG, Scale, figure, mi, run_cli  # noqa: E402

# (months after treatment, effect, standard error)
EST = [(0, 0.58, 0.10), (0, 0.42, 0.09), (1, 0.47, 0.11), (3, 0.40, 0.12), (6, 0.36, 0.14),
       (9, 0.30, 0.15), (12, 0.21, 0.16), (12, 0.30, 0.15), (18, 0.17, 0.18), (24, 0.08, 0.20)]
ALPHA, BETA = 0.49, -0.0165          # a line drawn by eye through the points, per month
W, H = 740, 480                      # the width of a .fig-notes.wide figure column: 1 unit = 1 px
sx = Scale(0, 24, 110, 712)
sy = Scale(-0.2, 0.9, 410, 30)


@figure("fit")
def fit():
    """Estimates by follow-up, the fitted line, its intercept and a callout."""
    svg = SVG(W, H, css=".demo-soft{opacity:.35;transition:opacity .4s}")
    svg.axis_y(sy, sx.p0 - 20, [0, 0.4, 0.8], labels=["0", "0.4", "0.8"], grid_to=sx.p1 + 10,
               title="Effect (SD)", title_x=34)
    svg.axis_x(sx, sy.p0 + 8, [0, 6, 12, 18, 24], title="Months after treatment", x0=sx.p0 - 20,
               x1=sx.p1 + 10)
    with svg.g(label="estimates", dclass="demo-soft:2-3"):
        for i, (t, y, se) in enumerate(EST):
            dx = (-5 if t in (0, 12) and i % 2 == 0 else 5 if t in (0, 12) else 0)
            svg.errorbar(sx(t) + dx, sy(y), sy(y - 1.96 * se), sy(y + 1.96 * se))
    with svg.g(step="1", label="fitted line"):
        svg.line(sx(0), sy(ALPHA), sx(24), sy(ALPHA + 24 * BETA), stroke="accent2", width=4.5, cap="round")
        svg.circle(sx(0), sy(ALPHA), 8, fill="bg", stroke="accent2", width=3.5)
    with svg.g(step="2-2", label="alpha"):
        svg.arrow(sx(0), sy(0), sx(0), sy(ALPHA) + 10, color="red", start=True)
        svg.label(sx(0) + 14, sy(ALPHA / 2) + 10, mi("α"), size=32, fill="red", weight=None)
    with svg.g(step="3", label="callout"):
        svg.callout(560, 110, "Half the effect\nis gone by 15 months", anchor="tc", size=22,
                    point_to=(sx(15), sy(ALPHA + 15 * BETA)))
    return svg


@figure("nesting")
def nesting():
    """Square and curly brackets: estimates within studies within one review."""
    svg = SVG(870, 200)
    rows = [("Study 1", ["0 mo", "6 mo", "12 mo"]), ("Study 2", ["0 mo", "12 mo"]), ("Study 3", ["3 mo"])]
    x = 30
    for k, (study, ests) in enumerate(rows):
        x0 = x
        with svg.g(label=study.lower(), step=str(k)):
            for e in ests:
                svg.rect(x, 60, 110, 46, fill="card", stroke="border", width=2, rx=8)
                svg.text(x + 55, 91, e, size=22, anchor="middle")
                x += 124
            svg.bracket(x0, 48, x - 14, 48, text=study, size=22, color="muted",
                        text_color="ink", weight=700)
        x += 40
    with svg.g(step="3", label="review bracket"):
        svg.bracket(30, 122, x - 54, 122, kind="curly", depth=18, flip=True, color="accent", width=3,
                    text="One review: several analyses, many estimates", size=24, text_color="ink")
    return svg


@figure("gallery")
def gallery():
    """Every arrow, bracket and callout variant, for checking marks at scale 1."""
    svg = SVG(1136, 500)
    svg.text(20, 30, "Arrows", size=22, weight=700, fill="muted")
    for i, w in enumerate([1.5, 2.5, 3, 4.5]):
        y = 70 + i * 44
        svg.arrow(20, y, 220, y, width=w)
        svg.arrow(260, y, 460, y, width=w, start=True, color="accent")
    svg.arrow(500, 60, 640, 200, width=3, color="accent2")
    svg.arrow(680, 200, 820, 60, width=3, bend=0.25, color="red")
    svg.arrow(860, 60, 860, 200, width=3, start=True, color="green")
    svg.arrow(900, 200, 1100, 60, width=2.5, bend=-0.3, dash="6 5")
    svg.text(20, 270, "Brackets", size=22, weight=700, fill="muted")
    svg.bracket(40, 340, 240, 340, text="square, up")
    svg.bracket(40, 410, 240, 410, kind="curly", flip=True, text="curly, down")
    svg.bracket(320, 290, 320, 470, kind="curly", text="curly,\nright")
    svg.bracket(560, 290, 560, 470, flip=True, text="square,\nleft")
    svg.callout(620, 300, "A callout box", title="Default")
    svg.callout(860, 290, ["Bigger box,", "bigger text"], size=30, title="Large", pad=18, border=3)
    return svg


if __name__ == "__main__":
    run_cli(out="figures")

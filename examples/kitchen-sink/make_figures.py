"""Figures for the kitchen-sink deck (invented numbers, for illustration only).

    python3 make_figures.py                  # every figure
    python3 make_figures.py --only coefplot  # one figure
    python3 make_figures.py --list
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))
from svgkit import SVG, Scale, figure, run_cli  # noqa: E402

# (outcome, [(effect, standard error) for arm A, arm B])
ROWS = [
    ("Learning", [(0.20, 0.07), (0.12, 0.05)]),
    ("Attendance", [(0.01, 0.06), (-0.02, 0.05)]),
    ("Wellbeing", [(0.13, 0.05), (0.12, 0.05)]),
    ("Physical health", [(-0.07, 0.05), (0.01, 0.04)]),
    ("Knowledge", [(0.42, 0.07), (0.48, 0.05)]),
    ("Behaviour", [(0.31, 0.07), (0.57, 0.06)]),
    ("School climate", [(0.05, 0.07), (0.18, 0.05)]),
]
ARMS = [("Arm A", "accent"), ("Arm B", "accent2")]
W, H = 900, 560
sx = Scale(-0.2, 0.8, 270, 860)
ROW_H, TOP = 62, 40


@figure("coefplot")
def coefplot():
    """Treatment effects on seven outcomes for two arms, with 95% intervals."""
    svg = SVG(W, H)
    bottom = TOP + ROW_H * len(ROWS)
    svg.axis_x(sx, bottom + 6, [-0.2, 0, 0.2, 0.4, 0.6, 0.8], title="Treatment effect (SD, 95% CI)",
               grid_to=TOP - 10)
    svg.line(sx(0), TOP - 10, sx(0), bottom, stroke="faint", width=1.5, dash="6 5")
    for i, (name, ests) in enumerate(ROWS):
        y = TOP + ROW_H * i + ROW_H / 2
        svg.text(sx.p0 - 20, y + 7, name, size=22, anchor="end")
        for k, ((est, se), (_, colour)) in enumerate(zip(ests, ARMS)):
            yy = y + (k - 0.5) * 18
            half_ci = 1.96 * se
            significant = abs(est) > half_ci
            svg.errorbar(sx(est), yy, sx(est - half_ci), sx(est + half_ci), color=colour,
                         horizontal=True, width=2.5, opacity=None if significant else 0.4)
    for k, (arm, colour) in enumerate(ARMS):
        x = sx(0.45) + k * 130
        svg.circle(x, 22, 7, fill=colour)
        svg.text(x + 14, 29, arm, size=20, fill=colour, weight=700)
    return svg


if __name__ == "__main__":
    run_cli(out=os.path.join(HERE, "figures"))

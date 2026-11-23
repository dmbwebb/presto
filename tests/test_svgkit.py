#!/usr/bin/env python3
"""Unit tests for tools/svgkit.py (standard library only; runs under python3.9 too)."""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import svgkit  # noqa: E402
from svgkit import SVG, Scale, figure, mi, mr, run_cli, write_if_changed  # noqa: E402

DEMO = ROOT / "examples" / "svgkit-demo"


def sample():
    svg = SVG(400, 300, dclass="wm-time:5")
    sx, sy = Scale(0, 24, 40, 380), Scale(0, 1, 260, 20)
    svg.axis_x(sx, 270, [0, 12, 24], title="Months")
    svg.axis_y(sy, 40, [0, 0.5, 1], grid_to=380)
    with svg.g(step="1", label="points", fade_after="3"):
        for t, y in [(0, 0.5), (12, 0.333333), (24, 0.1)]:
            svg.errorbar(sx(t), sy(y), sy(y - 0.1), sy(y + 0.1))
    svg.arrow(50, 50, 150, 150, start=True, id="a1")
    svg.bracket(300, 40, 300, 200, kind="curly", text="one review")
    svg.callout(200, 100, "Note\nsecond line", title="Title", point_to=(60, 250))
    return svg


class OutputTests(unittest.TestCase):
    def test_render_is_deterministic(self):
        self.assertEqual(sample().render("x"), sample().render("x"))
        svg = sample()
        self.assertEqual(svg.render(), svg.render())

    def test_root_and_header(self):
        out = sample().render("walk_fit")
        first = out.split("\n", 1)[0]
        self.assertTrue(first.startswith('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" '
                                         'viewBox="0 0 400 300"'), first)
        self.assertIn('data-svgkit="walk_fit"', first)
        self.assertIn('data-class="wm-time:5"', first)
        self.assertTrue(out.endswith("</svg>\n"))

    def test_coordinates_are_rounded_without_negative_zero(self):
        self.assertEqual(svgkit.fmt(1.23456), "1.23")
        self.assertEqual(svgkit.fmt(2.0), "2")
        self.assertEqual(svgkit.fmt(-0.001), "0")
        self.assertEqual(svgkit.fmt(7), "7")
        self.assertNotIn("0.333333", sample().render())

    def test_unclosed_group_and_duplicate_id_raise(self):
        svg = SVG(10, 10)
        svg.open_g(step="1")
        with self.assertRaises(ValueError):
            svg.render()
        svg = SVG(10, 10)
        svg.circle(1, 1, 1, id="dot")
        with self.assertRaises(ValueError):
            svg.rect(0, 0, 1, 1, id="dot")
        with self.assertRaises(ValueError):
            SVG(10, 10).close_g()

    def test_raw_group_markup_counts_toward_nesting(self):
        svg = SVG(10, 10)
        svg.add('<g data-step="2-2" data-class="wm-pull:2-2">')    # as the TCD generators do
        svg.circle(0, 0, 1)
        svg.add("<g/>")
        svg.close_g()
        out = svg.render()
        self.assertIn('\n <circle cx="0"', out)
        self.assertTrue(out.endswith("<g/>\n</g>\n</svg>\n"))


class AttributeTests(unittest.TestCase):
    def test_step_attributes_pass_through_verbatim(self):
        svg = SVG(100, 100)
        with svg.g(step="+", fade_after="=", focus="2-3:.fit, .x;4:#a1", morph="dot-1", dclass="fx-soft:2-3",
                   label="fit line", id="g1", stat="n_obs", data_custom="z"):
            svg.circle(1, 2, 3, step="+:2", label="one dot", morph="dot-2")
        out = svg.render()
        for frag in ['data-step="+"', 'data-fade-after="="', 'data-focus="2-3:.fit, .x;4:#a1"',
                     'data-morph="dot-1"', 'data-class="fx-soft:2-3"', 'data-label="fit line"', 'id="g1"',
                     'data-stat="n_obs"', 'data-custom="z"', 'data-step="+:2"', 'data-label="one dot"']:
            self.assertIn(frag, out)

    def test_every_compound_element_takes_common_attributes(self):
        svg = SVG(200, 200)
        svg.arrow(0, 0, 50, 50, step="1", label="arrow")
        svg.bracket(0, 60, 100, 60, step="2", label="bracket")
        svg.errorbar(20, 100, 90, 110, step="3", label="bar")
        svg.callout(100, 100, "x", step="4", label="callout")
        svg.axis_x(Scale(0, 1, 0, 100), 190, [0, 1], step="5", label="x axis")
        svg.axis_y(Scale(0, 1, 190, 0), 10, [0, 1], step="6", label="y axis")
        out = svg.render()
        for i, name in enumerate(["arrow", "bracket", "bar", "callout", "x axis", "y axis"], 1):
            self.assertIn(f'<g data-step="{i}" data-label="{name}">', out)

    def test_quotes_in_attribute_values_are_escaped(self):
        svg = SVG(10, 10)
        svg.rect(0, 0, 1, 1, focus='1:[data-label="a"]')
        self.assertIn('data-focus="1:[data-label=&quot;a&quot;]"', svg.render())

    def test_palette_roles_and_overrides(self):
        svg = SVG(10, 10, palette=svgkit.Palette(accent="#2C3E5A", lin="#2464b4"))
        svg.line(0, 0, 1, 1, stroke="accent")
        svg.line(0, 0, 1, 1, stroke="lin")
        svg.line(0, 0, 1, 1, stroke="#123456")
        svg.line(0, 0, 1, 1)
        out = svg.render()
        self.assertIn('stroke="#2C3E5A"', out)
        self.assertIn('stroke="#2464b4"', out)
        self.assertIn('stroke="#123456"', out)
        self.assertIn(f'stroke="{svgkit.Palette.DEFAULTS["text"]}"', out)


class TextTests(unittest.TestCase):
    def test_dollar_math_is_left_for_the_engine(self):
        svg = SVG(10, 10)
        svg.text(0, 0, r"$\alpha + \beta_{j}$ per year")
        self.assertIn(r">$\alpha + \beta_{j}$ per year</text>", svg.render())

    def test_plain_text_is_escaped_and_markup_is_not(self):
        svg = SVG(10, 10)
        svg.text(0, 0, "a < b & c")
        svg.text(0, 0, "Slope " + mi("β") + " < 0")
        svg.text(0, 0, mr("ln 2 / ") + mi("λ"))
        out = svg.render()
        self.assertIn(">a &lt; b &amp; c</text>", out)
        self.assertIn('>Slope <tspan font-family="KaTeX_Math" font-style="italic" font-weight="400">β</tspan> &lt; 0<',
                      out)
        self.assertIn('<tspan font-family="KaTeX_Main" font-weight="400">ln 2 / </tspan><tspan font-family="KaTeX_Math"',
                      out)

    def test_raw_and_halo_and_multiline(self):
        svg = SVG(10, 10)
        svg.text(5, 6, "<tspan>x</tspan>", raw=True)
        svg.text(5, 6, "top\nbottom", size=20, halo=True)
        out = svg.render()
        self.assertIn("><tspan>x</tspan></text>", out)
        self.assertIn('paint-order="stroke"', out)
        self.assertIn('>top<tspan x="5" dy="25">bottom</tspan></text>', out)


class GeometryTests(unittest.TestCase):
    @staticmethod
    def polygons(out):
        return [[tuple(map(float, p.split(","))) for p in re.findall(r"-?[\d.]+,-?[\d.]+", d)]
                for d in re.findall(r'<path d="(M[^"]*Z)"', out)]

    def test_arrow_tips_land_on_the_end_points(self):
        svg = SVG(200, 200)
        svg.arrow(10, 20, 110, 20)
        heads = self.polygons(svg.render())
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0][0], (110.0, 20.0))
        shaft = re.search(r'<line x1="10" y1="20" x2="([\d.]+)"', svg.render())
        self.assertLess(float(shaft.group(1)), 110)

        svg = SVG(200, 200)
        svg.arrow(50, 150, 50, 50, start=True, bend=0.2)
        heads = self.polygons(svg.render())
        self.assertEqual(sorted(h[0] for h in heads), [(50.0, 50.0), (50.0, 150.0)])
        with self.assertRaises(ValueError):
            svg.arrow(1, 1, 1, 1)

    def test_brackets_start_and_end_on_their_points_and_label_outside(self):
        for kind in ("square", "curly"):
            svg = SVG(300, 300)
            svg.bracket(100, 50, 100, 250, kind=kind, text="label")
            out = svg.render()
            d = re.search(r'<path d="([^"]*)"', out).group(1)
            pts = re.findall(r"-?[\d.]+,-?[\d.]+", d)
            self.assertEqual(pts[0], "100,50")
            self.assertEqual(pts[-1], "100,250")
            # top-to-bottom: the bracketed things are on the left, so the label is on the right
            self.assertRegex(out, r'<text x="1[2-9]\d(\.\d+)?" y="[\d.]+" font-size="22" fill="[^"]+">label</text>')
        svg = SVG(300, 300)
        svg.bracket(0, 100, 200, 100, flip=True, text="below")
        self.assertRegex(svg.render(), r'<text x="100" y="1[3-9]\d[^"]*"')
        with self.assertRaises(ValueError):
            svg.bracket(0, 0, 10, 0, kind="round")

    def test_callout_anchor_and_size(self):
        svg = SVG(400, 400)
        x, y, w, h = svg.callout(200, 100, ["one", "two"], w=120, anchor="tc", size=20, pad=10)
        self.assertEqual((x, y, w), (140, 100, 120))
        self.assertAlmostEqual(h, 2 * 10 + 20 + 20 * 1.3)
        x, y, w, h = svg.callout(300, 300, "x", w=50, anchor="br")
        self.assertEqual((x + w, y + h), (300, 300))

    def test_errorbar_and_scale(self):
        sc = Scale(0, 10, 100, 200)
        self.assertEqual(sc(5), 150)
        self.assertEqual(sc.invert(150), 5)
        self.assertEqual(svgkit.nice_ticks(0, 1, 5), [0.0, 0.25, 0.5, 0.75, 1.0])
        self.assertEqual(svgkit.num(-0.5), "−0.5")
        svg = SVG(100, 100)
        svg.errorbar(10, 50, 30, 70, cap=6, r=0)
        out = svg.render()
        self.assertEqual(out.count("<line"), 3)
        self.assertNotIn("<circle", out)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_is_atomic_and_skips_identical_bytes(self):
        path = self.dir / "sub" / "fig.svg"
        self.assertTrue(write_if_changed(str(path), "<svg/>\n"))
        self.assertEqual(path.read_text(), "<svg/>\n")
        mask = os.umask(0)
        os.umask(mask)
        self.assertEqual(path.stat().st_mode & 0o777, 0o666 & ~mask)
        old = time.time() - 1000
        os.utime(path, (old, old))
        self.assertFalse(write_if_changed(str(path), b"<svg/>\n"))
        self.assertEqual(int(path.stat().st_mtime), int(old))
        inode = path.stat().st_ino
        os.chmod(path, 0o640)
        self.assertTrue(write_if_changed(str(path), "<svg></svg>\n"))
        self.assertNotEqual(path.stat().st_ino, inode)      # replaced, never rewritten in place
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["fig.svg"])

    def test_svg_save_reports_changes(self):
        path = str(self.dir / "a.svg")
        self.assertTrue(sample().save(path))
        self.assertFalse(sample().save(path))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.reg = {}
        reg = self.reg
        self.calls = []

        @figure("walk_fit", registry=reg)
        def walk_fit():
            """The fitted line."""
            self.calls.append("walk_fit")
            svg = SVG(10, 10)
            with svg.g(step="1"):
                svg.circle(1, 1, 1, fade_after="3")
            with svg.g(step="2-2", focus="2:.x", morph="m"):
                svg.circle(2, 2, 1)
            return svg

        @figure("walk_morph", registry=reg)
        def walk_morph():
            self.calls.append("walk_morph")
            return SVG(10, 10)

        @figure("summary", filename="summary.json", registry=reg)
        def summary():
            self.calls.append("summary")
            return {"alpha": 0.5}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_cli(list(args) + ["--out", self.out], registry=self.reg)
        return code, out.getvalue(), err.getvalue()

    def test_only_filters_by_name_and_glob(self):
        code, out, _ = self.run_cli("--only", "walk_fit")
        self.assertEqual((code, self.calls), (0, ["walk_fit"]))
        self.assertEqual(os.listdir(self.out), ["walk_fit.svg"])
        self.calls.clear()
        self.run_cli("--only", "walk_*,summary")
        self.assertEqual(self.calls, ["walk_fit", "walk_morph", "summary"])
        self.calls.clear()
        self.run_cli("--only", "summary", "--only", "walk_morph")
        self.assertEqual(self.calls, ["walk_morph", "summary"])

    def test_unknown_name_is_an_explicit_error(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("--only", "nope")
        self.assertIn("no figure matches nope", str(cm.exception.code))
        self.assertIn("walk_fit", str(cm.exception.code))

    def test_summary_is_one_line_per_figure(self):
        code, out, _ = self.run_cli()
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^walk_fit  \S*walk_fit\.svg  [\d.]+ kB  written  steps 1,2-2; "
                                   r"fade-after 3; focus 2:\.x; morph m$")
        self.assertIn("walk_morph", lines[1])
        self.assertIn("no steps", lines[1])
        _, out, _ = self.run_cli("--only", "walk_fit")
        self.assertIn("  unchanged  ", out)
        self.assertEqual(Path(self.out, "summary.json").read_text(), '{\n  "alpha": 0.5\n}\n')

    def test_list_and_json(self):
        code, out, _ = self.run_cli("--list")
        self.assertEqual(self.calls, [])
        self.assertEqual(out.split("\n")[0], "walk_fit  walk_fit.svg  The fitted line.")
        _, out, _ = self.run_cli("--only", "walk_fit", "--json")
        import json
        rec = json.loads(out)
        self.assertEqual(rec["steps"]["data-step"], ["1", "2-2"])
        self.assertEqual(rec["status"], "written")

    def test_check_writes_nothing_and_flags_stale_outputs(self):
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(self.out), [])
        self.assertIn("would change", out)
        self.run_cli()
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 0)

    def test_a_failing_figure_is_reported_and_others_still_run(self):
        @figure("broken", registry=self.reg)
        def broken():
            raise RuntimeError("bad data")

        code, out, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"broken  ERROR  RuntimeError: bad data \(test_svgkit\.py:\d+\)")
        self.assertIn("Traceback", err)
        self.assertTrue(Path(self.out, "walk_fit.svg").exists())

    def test_duplicate_registration_raises(self):
        with self.assertRaises(ValueError):
            figure("walk_fit", registry=self.reg)(lambda: SVG(1, 1))


class DemoTests(unittest.TestCase):
    def test_demo_figures_are_up_to_date(self):
        res = subprocess.run([sys.executable, str(DEMO / "make_figures.py"), "--check"],
                             capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(len(res.stdout.strip().split("\n")), 3)


class ThemeTests(unittest.TestCase):
    def test_theme_classes_exist(self):
        css = (ROOT / "engine" / "themes" / "academic.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.slide\.signpost \.section-toc \{\s*display: none;")
        self.assertIn(".callout.callout-lg {", css)
        self.assertIn("ol.results-list", css)

    def test_check_style_treats_signposts_as_non_content(self):
        src = (ROOT / "tools" / "check_style.py").read_text(encoding="utf-8")
        line = re.search(r"NON_CONTENT_CLASSES = \{[^}]*\}", src).group(0)
        self.assertIn('"signpost"', line)


if __name__ == "__main__":
    unittest.main()

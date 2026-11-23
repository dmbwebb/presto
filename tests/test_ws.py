#!/usr/bin/env python3
"""Tests for tools/ws.py and its helpers (ws_deck, ws_steps, ws_browser).

Pure-Python tests run anywhere; image tests need Pillow and browser tests need
Playwright (both in ~/.venvs/webslides-export); they skip otherwise.
"""

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "ws-deck"
sys.path.insert(0, str(REPO / "tools"))

import ws  # noqa: E402
import ws_deck  # noqa: E402
import ws_steps  # noqa: E402

HAVE_PIL = importlib.util.find_spec("PIL") is not None
HAVE_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


def run_ws(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            ws.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class DeckCopy:
    """A temporary copy of the fixture deck beside an ``engine`` symlink, like a real project."""

    def __enter__(self):
        self.tmp = TemporaryDirectory(prefix="ws-deck-copy-")
        root = Path(self.tmp.name)
        (root / "engine").symlink_to(REPO / "engine")
        self.dir = root / "deck"
        shutil.copytree(FIXTURE, self.dir)
        index = self.dir / "index.html"
        index.write_bytes(index.read_bytes().replace(b"../../../engine/", b"../engine/"))
        self.index = index
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()


def attr_values(text, attr):
    import re

    return re.findall(rf'{attr}="([^"]*)"', text)


# ---------------------------------------------------------------- ranges
class RangeTests(unittest.TestCase):
    def test_parse_range_forms(self):
        p = ws_steps.parse_range
        self.assertEqual(("n", 3, None), p("3"))
        self.assertEqual(("n+", 3, None), p("3+"))
        self.assertEqual(("a-b", 2, 4), p("2-4"))
        self.assertEqual(("a-b", 4, 4), p("4-4"))
        self.assertEqual(("-b", None, 3), p("-3"))
        self.assertEqual(("a-b", 2, 4), p(" 2 - 4 "))
        self.assertEqual(("rel", "+", None), p("+"))
        self.assertEqual(("rel", "=", None), p("="))
        self.assertEqual(("rel", "+", 2), p("+:2"))
        self.assertIsNone(p("3-"))
        self.assertIsNone(p("4-2"))
        self.assertIsNone(p("x"))

    def test_format_range_keeps_form_and_spacing(self):
        f = ws_steps.format_range
        self.assertEqual("4", f("3", 4, None))
        self.assertEqual("4+", f("3+", 4, None))
        self.assertEqual("2-5", f("2-4", 2, 5))
        self.assertEqual(" 2 - 5 ", f(" 2 - 4 ", 2, 5))
        self.assertEqual("5-5", f("4-4", 5, 5))
        self.assertEqual("-4", f("-3", None, 4))
        self.assertEqual("+", f("+", 9, None))
        self.assertEqual("=:2", f("=:2", 9, None))


# ---------------------------------------------------------------- slide addressing
class AddressingTests(unittest.TestCase):
    def setUp(self):
        self.text = ws_deck.read_deck(FIXTURE / "index.html")
        self.slides = ws_deck.parse_deck(self.text)

    def test_keys_come_from_id_then_data_id_then_title_slug(self):
        keys = [s["key"] for s in self.slides]
        self.assertEqual(["ws-fixture-deck", "html-steps", "svg-steps", "layout-problems",
                          "plain-slide", "plain-slide-2"], keys)
        self.assertEqual("ws fixture deck", self.slides[0]["title"], "<br> becomes a space")

    def test_select_by_number_range_id_slug_inner_id_and_title(self):
        sel = lambda *specs: ws_deck.select_slides(list(specs), self.slides)  # noqa: E731
        self.assertEqual([1], sel("2"))
        self.assertEqual([1, 2, 3], sel("2-4"))
        self.assertEqual([1, 3, 4], sel("2,4-5"))
        self.assertEqual([1], sel("#html-steps"))
        self.assertEqual([2], sel("svg-steps"))
        self.assertEqual([5], sel("plain-slide-2"))
        self.assertEqual([4], sel("inner-target"))
        self.assertEqual([3], sel("layout PROB"))
        self.assertEqual(list(range(6)), sel("all"))
        self.assertEqual([1], sel("2", "html-steps"), "duplicates collapse")

    def test_bad_selections_explain_themselves(self):
        with self.assertRaisesRegex(ws_deck.DeckError, "out of range"):
            ws_deck.select_slides(["9"], self.slides)
        with self.assertRaisesRegex(ws_deck.DeckError, "matches 2 slide titles"):
            ws_deck.select_slides(["plain"], self.slides)
        with self.assertRaisesRegex(ws_deck.DeckError, "unknown slide"):
            ws_deck.select_slides(["nope"], self.slides)

    def test_line_ranges_and_offsets_match_the_file(self):
        s = self.slides[1]
        self.assertEqual(s["html"], self.text[s["start"]:s["end"]])
        lines = self.text.split("\n")
        self.assertTrue(lines[s["line_start"] - 1].lstrip().startswith("<section"))
        self.assertTrue(lines[s["line_end"] - 1].rstrip().endswith("</section>"))

    def test_resolve_deck_accepts_dir_file_and_alias(self):
        self.assertEqual((FIXTURE / "index.html").resolve(), ws_deck.resolve_deck(str(FIXTURE)))
        self.assertEqual((FIXTURE / "index.html").resolve(), ws_deck.resolve_deck(str(FIXTURE / "index.html")))
        with TemporaryDirectory() as tmp:
            locate = Path(tmp) / "locate_deck.py"
            locate.write_text(
                "import json, sys\n"
                f"print(json.dumps({{'id': sys.argv[1], 'index': {str(FIXTURE / 'index.html')!r}}}))\n",
                encoding="utf-8")
            with patch.object(ws_deck, "LOCATE_DECK", locate):
                self.assertEqual((FIXTURE / "index.html").resolve(), ws_deck.resolve_deck("some-alias"))
            locate.write_text("import json, sys\nprint(json.dumps({'root': sys.argv[1], 'index': 'x'}))\n",
                              encoding="utf-8")
            with patch.object(ws_deck, "LOCATE_DECK", locate):
                with self.assertRaisesRegex(ws_deck.DeckError, "no deck"):
                    ws_deck.resolve_deck("unknown-alias")

    def test_outline_line_format(self):
        line = ws.outline_line(self.slides[1], 4)
        self.assertEqual("  2 #html-steps  [0-4]  Steps in HTML", line)
        self.assertIn("[0]", ws.outline_line(self.slides[0], 0))


# ---------------------------------------------------------------- get / put
class GetPutTests(unittest.TestCase):
    def test_get_prints_the_section_exactly_and_the_hash_on_stderr(self):
        text = ws_deck.read_deck(FIXTURE / "index.html")
        slide = ws_deck.parse_deck(text)[1]
        code, out, err = run_ws("get", str(FIXTURE), "html-steps")
        self.assertEqual(0, code, err)
        self.assertEqual(slide["html"] + "\n", out)
        self.assertIn(f"hash {slide['hash']}", err)
        self.assertIn(f"lines {slide['line_start']}-{slide['line_end']}", err)

    def test_put_round_trip_is_byte_identical(self):
        with DeckCopy() as d:
            before = d.index.read_bytes()
            code, out, err = run_ws("get", str(d.dir), "3", "--out", str(d.dir / "s.html"))
            self.assertEqual(0, code, err)
            code, out, err = run_ws("put", str(d.dir), "3", str(d.dir / "s.html"))
            self.assertEqual(0, code, err)
            self.assertEqual(before, d.index.read_bytes())
            self.assertIn("#svg-steps  [0-3]", out)

    def test_put_replaces_only_that_section(self):
        with DeckCopy() as d:
            text = ws_deck.read_deck(d.index)
            slide = ws_deck.parse_deck(text)[3]
            new = slide["html"].replace("Layout problems", "Layout trouble")
            code, out, err = run_ws_stdin(new, "put", str(d.dir), "layout-problems", "-",
                                          "--expect-hash", slide["hash"])
            self.assertEqual(0, code, err)
            after = ws_deck.read_deck(d.index)
            self.assertEqual(text[:slide["start"]], after[:slide["start"]])
            self.assertEqual(text[slide["end"]:], after[slide["start"] + len(new):])
            self.assertIn("Layout trouble", out)
            self.assertEqual([], [p.name for p in d.dir.iterdir() if "ws-tmp" in p.name], "no temp files left")

    def test_put_refuses_a_stale_hash_and_bad_replacements(self):
        with DeckCopy() as d:
            before = d.index.read_bytes()
            slide = ws_deck.parse_deck(ws_deck.read_deck(d.index))[1]
            code, _out, err = run_ws_stdin(slide["html"], "put", str(d.dir), "2", "-", "--expect-hash", "0" * 12)
            self.assertEqual(2, code)
            self.assertIn("changed since `ws get`", err)
            for bad in ("<div>not a slide</div>", slide["html"] + slide["html"], slide["html"] + "<p>x</p>",
                        slide["html"][:-10]):
                code, _out, err = run_ws_stdin(bad, "put", str(d.dir), "2", "-")
                self.assertEqual(2, code, bad[:40])
            self.assertEqual(before, d.index.read_bytes())

    def test_put_keeps_crlf_line_endings_elsewhere(self):
        with DeckCopy() as d:
            crlf = d.index.read_bytes().replace(b"\n", b"\r\n")
            d.index.write_bytes(crlf)
            slide = ws_deck.parse_deck(ws_deck.read_deck(d.index))[5]
            new = slide["html"].replace("Same title", "Other words")
            code, _out, err = run_ws_stdin(new, "put", str(d.dir), "6", "-")
            self.assertEqual(0, code, err)
            after = d.index.read_bytes()
            self.assertEqual(crlf.replace(b"Same title", b"Other words"), after)


def run_ws_stdin(data, *argv):
    with patch.object(sys, "stdin", io.StringIO(data)):
        return run_ws(*argv)


# ---------------------------------------------------------------- steps
class StepsTests(unittest.TestCase):
    def renumber(self, d, *args):
        return run_ws("steps", str(d.dir), *args)

    def test_insert_after_shifts_every_attribute_kind(self):
        with DeckCopy() as d:
            before = ws_deck.read_deck(d.index)
            code, out, err = self.renumber(d, "html-steps", "insert-after", "2")
            self.assertEqual(0, code, out + err)
            after = ws_deck.read_deck(d.index)
            self.assertEqual(['1', '2-5', '-4', '4+', '5-5'], attr_values(after, "data-step")[:5])
            self.assertIn('data-class="soft:4 hide:5-5"', after)
            self.assertIn('data-fade-after="5"', after)
            self.assertIn('data-focus="2-4:.keep; 5:.other"', after)
            self.assertIn('data-highlight="5"', after)
            self.assertIn('data-dim="2"', after)
            self.assertIn('data-steps="5"', after)
            self.assertIn("steps 0-4 -> 0-5", out)
            self.assertIn("step 3 (new)", out)
            # Only the step numbers changed.
            self.assertEqual(len(before.splitlines()), len(after.splitlines()))
            changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
            self.assertEqual(6, len(changed))
            # Deleting the inserted step restores the file byte for byte.
            code, out, err = self.renumber(d, "html-steps", "delete", "3")
            self.assertEqual(0, code, out + err)
            self.assertEqual(before, ws_deck.read_deck(d.index))

    def test_delete_refuses_to_lose_a_step_only_element_unless_merged_back(self):
        with DeckCopy() as d:
            before = d.index.read_bytes()
            code, out, _err = self.renumber(d, "html-steps", "delete", "4")
            self.assertEqual(1, code)
            self.assertIn("would never be shown", out)
            self.assertIn("--merge prev", out)
            self.assertIn("nothing written", out)
            self.assertEqual(before, d.index.read_bytes())
            code, out, err = self.renumber(d, "html-steps", "delete", "4", "--merge", "prev")
            self.assertEqual(0, code, out + err)
            after = ws_deck.read_deck(d.index)
            # Step 4's changes now happen at step 3: what hid at 4 hides at 3.
            self.assertEqual(['1', '2-3', '-2', '3+', '3-3'], attr_values(after, "data-step")[:5])
            self.assertIn('data-class="soft:3 hide:3-3"', after)
            self.assertIn('data-focus="2-2:.keep; 3:.other"', after)
            self.assertIn('data-fade-after="3"', after)
            self.assertIn('data-steps="3"', after)

    def test_swap_trades_the_changes_of_two_steps(self):
        with DeckCopy() as d:
            code, out, err = self.renumber(d, "html-steps", "swap", "3", "1")
            self.assertEqual(0, code, out + err)
            after = ws_deck.read_deck(d.index)
            # "1" -> 3; "2-4" keeps 2 and hides at 5; "-3" hides at 4 (unchanged);
            # "3+" -> 1+; "4-4" unchanged.
            self.assertEqual(['3', '2-4', '-3', '1+', '4-4'], attr_values(after, "data-step")[:5])
            self.assertIn('data-class="soft:1 hide:4-4"', after)
            self.assertIn("map 1<>3", out)

    def test_dry_run_writes_nothing(self):
        with DeckCopy() as d:
            before = d.index.read_bytes()
            code, out, _err = self.renumber(d, "html-steps", "insert-after", "0", "--dry-run")
            self.assertEqual(0, code)
            self.assertIn("dry run", out)
            self.assertEqual(before, d.index.read_bytes())

    def test_relative_and_auto_steps_are_left_alone_and_reported(self):
        with DeckCopy() as d:
            before = d.index.read_bytes()
            # Nothing absolute follows step 1, so '+' and data-reveal steps would just close the gap.
            code, out, _err = self.renumber(d, "plain-slide", "insert-after", "1")
            self.assertEqual(1, code)
            self.assertIn('"Next two" (data-step=\'+\') now resolves to step 2, the map expects 3', out)
            self.assertIn("renumber themselves", out)
            self.assertEqual(before, d.index.read_bytes())

    def test_relative_steps_after_an_absolute_one_follow_it(self):
        with DeckCopy() as d:
            text = d.index.read_text(encoding="utf-8")
            d.index.write_text(text.replace('<p data-step="+">Next one</p>', '<p data-step="1">Next one</p>'),
                               encoding="utf-8")
            code, out, err = self.renumber(d, "plain-slide", "swap", "1", "2", "--dry-run")
            self.assertEqual(1, code, out + err)  # '+' after "1" cannot move to 1 by itself
            self.assertIn('"Next two" (data-step=\'+\') now resolves to step 3, the map expects 1', out)
            self.assertIn("nothing written", out)

    def test_inline_svg_needs_a_flag_and_names_its_generator(self):
        with DeckCopy() as d:
            svg = d.dir / "figures" / "fig.svg"
            svg_before = svg.read_bytes()
            html_before = d.index.read_bytes()
            code, out, _err = self.renumber(d, "svg-steps", "insert-after", "1")
            self.assertEqual(1, code)
            self.assertIn("rerun with --include-svg", out)
            self.assertEqual(html_before, d.index.read_bytes())
            code, out, err = self.renumber(d, "svg-steps", "insert-after", "1", "--include-svg")
            self.assertEqual(0, code, out + err)
            self.assertIn("generated by code/make_fig.py", out)
            svg_after = svg.read_text(encoding="utf-8")
            self.assertIn('data-step="1" data-class="fx-soft:4"', svg_after)
            self.assertIn('data-step="3-4"', svg_after)
            self.assertIn('<g data-step="4">', svg_after)
            self.assertEqual(len(svg_before), len(svg_after.encode("utf-8")))
            self.assertIn('data-step="1-3"', d.index.read_text(encoding="utf-8"))

    def test_html_only_leaves_the_svg_untouched(self):
        with DeckCopy() as d:
            svg = d.dir / "figures" / "fig.svg"
            svg_before = svg.read_bytes()
            code, out, err = self.renumber(d, "svg-steps", "insert-after", "1", "--html-only")
            self.assertEqual(0, code, out + err)
            self.assertEqual(svg_before, svg.read_bytes())
            self.assertIn("NOT changed", out)

    def test_static_model_counts_steps_per_the_contract(self):
        # Slide 5 uses "+" / "=" (pinned contract), which engines before the
        # steps work ignore; the static model resolves them.
        text = ws_deck.read_deck(FIXTURE / "index.html")
        slides = ws_deck.parse_deck(text)
        maxes = [ws_steps.build_model(FIXTURE / "index.html", text, s)["max"] for s in slides]
        self.assertEqual([0, 4, 3, 0, 4, 0], maxes)


# ---------------------------------------------------------------- diff cropping
@unittest.skipUnless(HAVE_PIL, "needs Pillow")
class DiffCropTests(unittest.TestCase):
    @staticmethod
    def png(draw=None, size=(1280, 720)):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", size, "white")
        if draw:
            draw(ImageDraw.Draw(img))
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()

    @staticmethod
    def size_of(data):
        from PIL import Image

        return Image.open(io.BytesIO(data)).size

    def test_identical_images_have_no_change(self):
        import ws_browser

        a = self.png()
        self.assertEqual((None, None), ws_browser.diff_crop(a, a))

    def test_one_change_is_cropped_with_a_margin(self):
        import ws_browser

        old = self.png()
        new = self.png(lambda d: d.rectangle([600, 300, 699, 349], fill="black"))
        crop, desc = ws_browser.diff_crop(old, new)
        m = ws_browser.DIFF_MARGIN
        self.assertEqual((100 + 2 * m, 50 + 2 * m), self.size_of(crop))
        self.assertEqual(f"{100 + 2 * m}x{50 + 2 * m} at {600 - m},{300 - m}", desc)

    def test_crop_is_clamped_at_the_image_edge(self):
        import ws_browser

        crop, desc = ws_browser.diff_crop(self.png(), self.png(lambda d: d.rectangle([0, 0, 9, 9], fill="red")))
        m = ws_browser.DIFF_MARGIN
        self.assertEqual((10 + m, 10 + m), self.size_of(crop))
        self.assertTrue(desc.endswith("at 0,0"))

    def test_far_apart_changes_become_separate_crops(self):
        import ws_browser

        def draw(d):
            d.rectangle([20, 20, 59, 39], fill="black")
            d.rectangle([1200, 650, 1239, 669], fill="black")

        crop, desc = ws_browser.diff_crop(self.png(), self.png(draw))
        w, h = self.size_of(crop)
        self.assertEqual(2, desc.count(" at "))
        self.assertLess(w * h, 1280 * 720 / 10, "two small crops, not the whole slide")

    def test_before_puts_the_old_crop_above_the_new(self):
        import ws_browser

        new = self.png(lambda d: d.rectangle([600, 300, 699, 349], fill="black"))
        crop, _desc = ws_browser.diff_crop(self.png(), new, before=True)
        single, _ = ws_browser.diff_crop(self.png(), new)
        self.assertGreater(self.size_of(crop)[1], 2 * self.size_of(single)[1])

    def test_tiny_antialiasing_noise_is_ignored(self):
        import ws_browser

        noisy = self.png(lambda d: d.point((640, 360), fill=(250, 250, 250)))
        self.assertEqual((None, None), ws_browser.diff_crop(self.png(), noisy))

    def test_size_change_reports_the_whole_image(self):
        import ws_browser

        _crop, desc = ws_browser.diff_crop(self.png(size=(640, 360)), self.png())
        self.assertEqual("1280x720 at 0,0", desc)


# ---------------------------------------------------------------- browser
@unittest.skipUnless(HAVE_PLAYWRIGHT and HAVE_PIL, "needs Playwright and Pillow")
class BrowserTests(unittest.TestCase):
    def test_outline_counts_steps_and_describes_them(self):
        code, out, err = run_ws("outline", str(FIXTURE), "--steps")
        self.assertEqual(0, code, err)
        lines = out.splitlines()
        self.assertEqual("  2 #html-steps  [0-4]  Steps in HTML", lines[1])
        # The engine's WS.describeSteps labels elements as tag "snippet".
        self.assertIn("      1: +p \"One\"", lines)
        self.assertTrue(any(line.startswith("      3: ") and '+fig.svg g "Labels"' in line
                            and "fig.svg g +fx-soft" in line for line in lines), out)
        self.assertIn("      4: +#only-four ~p \"Three on\" focus .other in div \"Keep Other\" "
                      "highlight #only-four", lines)
        self.assertTrue(any(line.startswith("  5 #plain-slide  [") for line in lines))
        self.assertTrue(any(line.strip().endswith('+li "Auto four"') for line in lines), out)

    def test_outline_uses_ws_describe_steps_when_the_engine_has_it(self):
        with DeckCopy() as d:
            text = d.index.read_text(encoding="utf-8")
            hook = ("<script>window.addEventListener('load', () => { WS.describeSteps = i => "
                    "[{step: 0}, {step: 1, appears: ['engine says one']}]; });</script>\n</body>")
            d.index.write_text(text.replace("</body>", hook), encoding="utf-8")
            code, out, err = run_ws("outline", str(d.dir), "2", "--steps")
            self.assertEqual(0, code, err)
            self.assertIn("1: appears: engine says one", out)

    def test_layout_reports_each_problem_kind_once(self):
        code, out, err = run_ws("layout", str(FIXTURE), "layout-problems", "--marks")
        self.assertEqual(1, code, err)
        for expected in ('overlap "Overlapping words here" <p.abs> x "Second text on top"',
                         'overlap labels.svg:"Clash one" <text> x labels.svg:"Clash two"',
                         'clipped "Hidden second line"', 'clipped labels.svg:"Cut at edge"',
                         'text outside slide "Past the right edge"', 'small text "Tiny note text"',
                         'text over mark labels.svg:"Over a dot"'):
            self.assertIn(expected, out)
        self.assertNotIn("overflow", out, "overflow from text already reported outside is folded in")

    def test_layout_of_a_clean_slide_is_ok_and_json_works(self):
        code, out, err = run_ws("layout", str(FIXTURE), "html-steps", "--step", "2")
        self.assertEqual(0, code, err)
        self.assertEqual("OK\n", out)
        code, out, err = run_ws("layout", str(FIXTURE), "4", "--json")
        data = json.loads(out)
        self.assertEqual(4, data[0]["slide"])
        self.assertTrue(any(p["kind"] == "overlap" for p in data[0]["problems"]))

    def test_check_then_diff(self):
        import ws_browser

        with DeckCopy() as d, TemporaryDirectory() as cache, TemporaryDirectory() as out_dir:
            with patch.object(ws_browser, "CACHE_ROOT", Path(cache)):
                code, out, err = run_ws("check", str(d.dir), "--slides", "html-steps", "--steps", "1,2",
                                        "--out", out_dir)
                self.assertEqual(0, code, out + err)  # warnings only
                summary = out.splitlines()[-1]
                self.assertTrue(summary.startswith("0 error(s), 2 warning(s): links, overflow, style, layout"), out)
                self.assertIn('2      warn  acronym "HTML" used before it is spelled out', out)
                self.assertIn("captured 2:1-2", summary)
                self.assertIn("sheet ", out)
                self.assertTrue(any(Path(out_dir).glob("sheet-2.png")))
                code, out, err = run_ws("check", str(d.dir), "--slides", "2", "--steps", "2", "--diff",
                                        "--out", out_dir)
                self.assertIn("2:2  no visual change", out)
                text = d.index.read_text(encoding="utf-8")
                d.index.write_text(text.replace(">Two to four<", ">Two to four, now longer<"), encoding="utf-8")
                code, out, err = run_ws("diff", str(d.dir), "2", "--steps", "1,2", "--out", out_dir)
                self.assertEqual(0, code, out + err)
                self.assertIn("2:1  no visual change", out)
                self.assertIn("2:2  changed", out)
                crop = Path(out_dir) / "s2-2-diff.png"
                self.assertTrue(crop.exists())
                from PIL import Image

                w, h = Image.open(crop).size
                self.assertLess(w * h, 1280 * 720 / 4)
                # First run on a slide without a baseline writes the full image.
                code, out, err = run_ws("diff", str(d.dir), "3", "--out", out_dir)
                self.assertIn("no baseline yet, full image", out)

    def test_check_reports_layout_problems_and_fails(self):
        with TemporaryDirectory() as cache, TemporaryDirectory() as out_dir:
            import ws_browser

            with patch.object(ws_browser, "CACHE_ROOT", Path(cache)):
                code, out, err = run_ws("check", str(FIXTURE), "4", "--no-shots", "--out", out_dir)
        self.assertEqual(1, code)
        self.assertIn("4      error overlap", out)
        self.assertIn("captured nothing", out)


if __name__ == "__main__":
    unittest.main()

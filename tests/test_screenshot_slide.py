#!/usr/bin/env python3
import contextlib
import hashlib
import io
import subprocess
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sys

REPO = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO / "tests" / "fixtures" / "tools-deck"
sys.path.insert(0, str(REPO / "tools"))
from screenshot_slide import (  # noqa: E402
    capture_deck,
    main,
    output_paths,
    png_dimensions,
    prepare_output_paths,
    resolve_deck_path,
    sheet_paths,
    validate_runtime_options,
    validate_screenshot_output,
)


def runtime_args(**overrides):
    values = dict(step=0, scale=1, timeout=1000, columns=4, thumb_width=480)
    values.update(overrides)
    return Namespace(**values)


def fixture_files():
    return sorted(p.relative_to(FIXTURE_DIR).as_posix() for p in FIXTURE_DIR.rglob("*") if p.is_file())


class ScreenshotSlideTests(unittest.TestCase):
    @staticmethod
    def fake_png(width=2560, height=1440, payload_size=2048):
        return (
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big")
            + b"IHDR"
            + width.to_bytes(4, "big")
            + height.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
            + b"\x00\x00\x00\x00"
            + b"0" * payload_size
        )

    def run_main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    # ------------------------------------------------------------ live deck
    def test_every_engine_step_is_captured_once_including_svg_and_table_steps(self):
        before = fixture_files()
        _infos, shots = capture_deck(FIXTURE_DIR / "index.html", ["ranged", "svg-steps", "table-steps"], scale=1)
        by_slide = {}
        for index, step, data in shots:
            by_slide.setdefault(index, []).append((step, hashlib.sha1(data).hexdigest()))
        # ranged: declared data-steps="5" is stale; the engine counts 3 steps
        self.assertEqual([0, 1, 2, 3], [s for s, _ in by_slide[1]])
        self.assertEqual(4, len(by_slide[2]))
        self.assertEqual(4, len({h for _, h in by_slide[2]}), "each SVG step should look different")
        self.assertEqual(3, len({h for _, h in by_slide[3]}), "each table row step should look different")
        self.assertEqual(before, fixture_files(), "nothing may be written into the deck folder")

    def test_reveal_each_list_steps_come_from_the_engine(self):
        infos, shots = capture_deck(FIXTURE_DIR / "index.html", ["reveal-each"], scale=1)
        self.assertEqual(4, infos[4]["maxStep"])
        self.assertEqual(5, len(shots))

    def test_screenshot_shows_the_real_footer_slide_number(self):
        _infos, [(_i, _s, numbered)] = capture_deck(FIXTURE_DIR / "index.html", ["clean"], scale=1)
        _infos, [(_i, _s, title)] = capture_deck(FIXTURE_DIR / "index.html", ["1"], scale=1)
        from PIL import Image

        def corner(data):
            img = Image.open(io.BytesIO(data)).convert("L")
            return img.crop((1150, 690, 1280, 720)).getextrema()

        self.assertLess(corner(numbered)[0], 200, "slide number should be drawn bottom right")
        self.assertGreater(corner(title)[0], 240, "title slides have no slide number")

    def test_cli_keeps_single_slide_single_step_and_prefix_outputs(self):
        with TemporaryDirectory() as tmp:
            exact = Path(tmp) / "exact.png"
            code, out, err = self.run_main(str(FIXTURE_DIR), "3", "--step", "2", "--output", str(exact), "--scale", "1")
            self.assertEqual(0, code, err)
            self.assertEqual((1280, 720), png_dimensions(exact))

            code, out, err = self.run_main(str(FIXTURE_DIR), "2", "--output", str(Path(tmp) / "pre.png"), "--scale", "1")
            self.assertEqual(0, code, err)
            self.assertIn("Slide 2: 4 step(s) (0-3)", out)
            self.assertEqual([f"pre_step_{s}.png" for s in range(4)], sorted(p.name for p in Path(tmp).glob("pre_*")))

    def test_cli_accepts_several_slides_and_writes_a_contact_sheet(self):
        with TemporaryDirectory() as tmp:
            sheet = Path(tmp) / "sheet.png"
            code, out, err = self.run_main(
                str(FIXTURE_DIR), "1", "clean", "8-9", "--final-only", "--contact-sheet", str(sheet),
                "--columns", "2", "--thumb-width", "320", "--scale", "1",
            )
            self.assertEqual(0, code, err)
            self.assertIn("4 slide(s), 4 capture(s)", out)
            self.assertIn("Contact sheet saved", out)
            width, height = png_dimensions(sheet)
            self.assertEqual(14 + 2 * (320 + 14), width)  # two columns
            self.assertEqual(40 + 2 * (26 + 180 + 14), height)  # two rows
            self.assertEqual(["sheet.png"], [p.name for p in Path(tmp).iterdir()])  # no stray PNGs

    def test_cli_rejects_out_of_range_step_with_engine_count(self):
        code, _out, err = self.run_main(str(FIXTURE_DIR), "2", "--step", "5")
        self.assertEqual(1, code)
        self.assertIn("step 5 out of range for slide 2 (it has steps 0-3)", err)

    def test_cli_rejects_unknown_slide(self):
        code, _out, err = self.run_main(str(FIXTURE_DIR), "no-such-slide", "--step", "0")
        self.assertEqual(1, code)
        self.assertIn("unknown slide", err)

    # ------------------------------------------------------------ output planning
    def test_output_paths_follow_the_cli_rules(self):
        one = [(1, 0), (1, 1)]
        self.assertEqual([Path("/tmp/slide_2_step_0.png"), Path("/tmp/slide_2_step_1.png")], output_paths(one))
        self.assertEqual([Path("/x/a.png")], output_paths([(1, 2)], "/x/a.png", one_step=True))
        self.assertEqual([Path("/x/a_step_0.png")], output_paths([(1, 0)], "/x/a.png"))
        self.assertEqual([Path("/x/p_step_0.png"), Path("/x/p_step_1.png")], output_paths(one, "/x/p"))
        many = [(0, 0), (3, 2)]
        self.assertEqual([Path("/x/p_slide_1_step_0.png"), Path("/x/p_slide_4_step_2.png")],
                         output_paths(many, "/x/p.png", one_step=True))

    def test_sheet_paths_split_large_captures(self):
        self.assertEqual([Path("/x/s.png")], sheet_paths("/x/s.png", 24))
        self.assertEqual([Path("/x/s.png"), Path("/x/s-2.png"), Path("/x/s-3.png")], sheet_paths("/x/s.png", 49))

    # ------------------------------------------------------------ retained helpers
    def test_resolve_deck_path_accepts_directory(self):
        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            index = deck_dir / "index.html"
            index.write_text("", encoding="utf-8")

            self.assertEqual(index.resolve(), resolve_deck_path(str(deck_dir)))

    def test_prepare_output_paths_creates_parent_directories(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "slide.png"
            prepare_output_paths([(0, output)])
            self.assertTrue(output.parent.is_dir())

    def test_prepare_output_paths_expands_home_directory(self):
        with TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            with patch.dict("os.environ", {"HOME": str(fake_home)}):
                prepare_output_paths([(0, Path("~/shots/slide.png"))])
                self.assertTrue((fake_home / "shots").is_dir())

    def test_prepare_output_paths_preserves_existing_outputs_until_capture_succeeds(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "slide.png"
            output.write_bytes(b"stale screenshot")
            with patch("screenshot_slide.trash_path") as trash_path, \
                 patch("screenshot_slide.subprocess.run",
                       side_effect=subprocess.CalledProcessError(1, ["trash", str(output)])):
                prepare_output_paths([(0, output)])
            self.assertEqual(b"stale screenshot", output.read_bytes())
        trash_path.assert_not_called()

    def test_prepare_output_paths_rejects_bad_targets_without_trashing(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "existing-output-dir"
            output_dir.mkdir()
            parent_file = Path(tmp) / "not-a-directory"
            parent_file.write_text("file", encoding="utf-8")
            duplicate = Path(tmp) / "slide.png"
            cases = [
                ([(0, output_dir)], "is a directory"),
                ([(0, Path(tmp) / "slide.txt")], "Output path is not a PNG file"),
                ([(0, parent_file / "slide.png")], "could not prepare output directory"),
                ([(0, duplicate), (1, duplicate)], "Duplicate screenshot output path"),
            ]
            with patch("screenshot_slide.trash_path") as trash_path:
                for outputs, message in cases:
                    with self.subTest(message=message), self.assertRaises(ValueError) as ctx:
                        prepare_output_paths(outputs)
                    self.assertIn(message, str(ctx.exception))
        trash_path.assert_not_called()

    def test_validate_screenshot_output_checks_size_type_and_dimensions(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                validate_screenshot_output(Path(tmp) / "missing.png")
            tiny = Path(tmp) / "tiny.png"
            tiny.write_bytes(b"bad")
            with self.assertRaises(ValueError):
                validate_screenshot_output(tiny)
            not_png = Path(tmp) / "not-a-real.png"
            not_png.write_bytes(b"not png" * 400)
            with self.assertRaisesRegex(ValueError, "not a PNG"):
                validate_screenshot_output(not_png)
            wrong = Path(tmp) / "wrong-size.png"
            wrong.write_bytes(self.fake_png(width=1280, height=720))
            with self.assertRaisesRegex(ValueError, "unexpected dimensions"):
                validate_screenshot_output(wrong, expected_width=2560, expected_height=1440)
            ok = Path(tmp) / "ok.png"
            png = self.fake_png()
            ok.write_bytes(png)
            self.assertEqual(len(png), validate_screenshot_output(ok))

    def test_validate_runtime_options_rejects_bad_values(self):
        cases = [
            (runtime_args(step=-1), "step must be >= 0"),
            (runtime_args(scale=0), "scale must be >= 1"),
            (runtime_args(timeout=-5), "timeout must be > 0"),
            (runtime_args(columns=0), "columns must be >= 1"),
            (runtime_args(thumb_width=10), "thumb-width must be >= 160"),
        ]
        for args, message in cases:
            with self.subTest(message=message), self.assertRaises(SystemExit) as ctx:
                validate_runtime_options(args)
            self.assertIn(message, str(ctx.exception))

    def test_main_reports_bad_inputs_without_traceback(self):
        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            (deck_dir / "index.html").mkdir()
            css = Path(tmp) / "deck.css"
            css.write_text("body {}", encoding="utf-8")
            cases = [
                (str(deck_dir), "is a directory"),
                (str(css), "is not an HTML file"),
                (str(Path(tmp) / "missing.html"), "not found"),
            ]
            for deck, message in cases:
                with self.subTest(message=message), patch("screenshot_slide.DeckServer") as server:
                    code, _out, err = self.run_main(deck, "1", "--step", "0")
                    server.assert_not_called()
                self.assertEqual(1, code)
                self.assertIn(message, err)
                self.assertNotIn("Traceback", err)

    def test_main_rejects_output_path_same_as_input_deck_without_trashing_it(self):
        deck = FIXTURE_DIR / "index.html"
        before = deck.read_bytes()
        with patch("screenshot_slide.trash_path") as trash_path:
            code, _out, err = self.run_main(str(deck), "1", "--step", "0", "--output", str(deck))
        self.assertEqual(1, code)
        self.assertIn("Output path must not be the input deck", err)
        self.assertEqual(before, deck.read_bytes())
        trash_path.assert_not_called()

    def test_main_reports_server_launch_failure_without_traceback(self):
        with patch("screenshot_slide.DeckServer", side_effect=OSError("server launch failed")):
            code, _out, err = self.run_main(str(FIXTURE_DIR), "1", "--step", "0")
        self.assertEqual(1, code)
        self.assertIn("server launch failed", err)
        self.assertNotIn("Traceback", err)

    def test_trash_not_rmtree_for_cleanup(self):
        source = (REPO / "tools" / "screenshot_slide.py").read_text(encoding="utf-8")
        self.assertIn('subprocess.run(["trash", str(path)]', source)
        self.assertNotIn("shutil.rmtree", source)
        self.assertNotIn("_screenshot_test_", source)


if __name__ == "__main__":
    unittest.main()

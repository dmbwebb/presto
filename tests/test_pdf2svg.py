#!/usr/bin/env python3
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from pdf2svg import main, make_transparent, parse_args, pdf_to_svg, transparentize_in_place, write_text_atomically  # noqa: E402


class PDF2SVGToolTests(unittest.TestCase):
    def test_make_transparent_removes_paired_white_rect_backgrounds(self):
        svg = (
            '<svg><rect width="100" height="100" fill="rgb(100%,100%,100%)">'
            '</rect><path fill="black" /></svg>'
        )

        result, n_rects, n_paths = make_transparent(svg)

        self.assertEqual(1, n_rects)
        self.assertEqual(0, n_paths)
        self.assertNotIn("<rect", result)
        self.assertIn('<path fill="black" />', result)

    def test_parse_args_rejects_unknown_flags(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            parse_args(["figure.pdf", "--bad-option"])

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("unrecognized arguments: --bad-option", stderr.getvalue())

    def test_main_exits_nonzero_when_conversion_input_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.pdf"
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["pdf2svg.py", str(missing)]):
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(1, ctx.exception.code)
        self.assertIn("does not exist", stdout.getvalue())

    def test_main_reports_missing_svg_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.svg"
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["pdf2svg.py", str(missing)]):
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(1, ctx.exception.code)
        self.assertIn("does not exist", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_main_rejects_mixed_pdf_and_svg_inputs_without_trashing_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "figure-a.pdf"
            other_pdf = Path(tmp) / "figure-b.pdf"
            svg = Path(tmp) / "existing.svg"
            pdf.write_bytes(b"%PDF")
            other_pdf.write_bytes(b"%PDF")
            svg.write_text("<svg></svg>", encoding="utf-8")
            stdout = io.StringIO()

            with patch.object(sys, "argv", ["pdf2svg.py", str(pdf), str(other_pdf), str(svg)]), \
                 patch("pdf2svg.pdf_to_svg") as pdf_to_svg:
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(1, ctx.exception.code)
        self.assertIn("Cannot mix PDF and SVG inputs", stdout.getvalue())
        pdf_to_svg.assert_not_called()

    def test_main_accepts_uppercase_pdf_extensions_in_batch_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "figure.PDF"
            pdf.write_bytes(b"%PDF")

            with patch.object(sys, "argv", ["pdf2svg.py", str(pdf)]), \
                 patch("pdf2svg.pdf_to_svg", return_value=Path(tmp) / "figure.svg") as convert:
                main()

        convert.assert_called_once()

    def test_main_accepts_uppercase_svg_extensions_for_in_place_transparency(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "figure.SVG"
            svg.write_text("<svg></svg>", encoding="utf-8")

            with patch.object(sys, "argv", ["pdf2svg.py", str(svg)]), \
                 patch("pdf2svg.transparentize_in_place", return_value=True) as transparentize:
                main()

        transparentize.assert_called_once_with(str(svg))

    def test_main_rejects_outdir_with_svg_inputs_before_in_place_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "figure.svg"
            outdir = Path(tmp) / "svgs"
            svg.write_text("<svg></svg>", encoding="utf-8")
            stdout = io.StringIO()

            with patch.object(sys, "argv", ["pdf2svg.py", str(svg), "--outdir", str(outdir)]), \
                 patch("pdf2svg.transparentize_in_place") as transparentize:
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(1, ctx.exception.code)
        self.assertIn("Cannot use --outdir with SVG inputs", stdout.getvalue())
        transparentize.assert_not_called()

    def test_main_rejects_page_with_svg_inputs_before_in_place_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "figure.svg"
            svg.write_text("<svg></svg>", encoding="utf-8")
            stdout = io.StringIO()

            with patch.object(sys, "argv", ["pdf2svg.py", str(svg), "--page", "2"]), \
                 patch("pdf2svg.transparentize_in_place") as transparentize:
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(1, ctx.exception.code)
        self.assertIn("Cannot use --page with SVG inputs", stdout.getvalue())
        transparentize.assert_not_called()

    def test_main_rejects_outdir_file_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "figure.pdf"
            outdir_file = Path(tmp) / "not-a-dir"
            pdf.write_bytes(b"%PDF")
            outdir_file.write_text("not a directory", encoding="utf-8")
            stdout = io.StringIO()

            with patch.object(sys, "argv", ["pdf2svg.py", str(pdf), "--outdir", str(outdir_file)]), \
                 patch("pdf2svg.pdf_to_svg") as pdf_to_svg:
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(1, ctx.exception.code)
        self.assertIn("Output directory path is a file", stdout.getvalue())
        pdf_to_svg.assert_not_called()

    def test_main_rejects_outdir_with_explicit_output_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "explicit.svg"
            outdir = Path(tmp) / "svgs"
            pdf.write_bytes(b"%PDF")
            stdout = io.StringIO()

            with patch.object(sys, "argv", ["pdf2svg.py", str(pdf), str(output), "--outdir", str(outdir)]), \
                 patch("pdf2svg.pdf_to_svg") as pdf_to_svg:
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(1, ctx.exception.code)
        self.assertIn("Cannot use --outdir with an explicit SVG output path", stdout.getvalue())
        pdf_to_svg.assert_not_called()

    def test_main_rejects_duplicate_batch_outputs_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_a = Path(tmp) / "a" / "figure.pdf"
            input_b = Path(tmp) / "b" / "figure.pdf"
            outdir = Path(tmp) / "svgs"
            input_a.parent.mkdir()
            input_b.parent.mkdir()
            input_a.write_bytes(b"%PDF")
            input_b.write_bytes(b"%PDF")
            stdout = io.StringIO()

            with patch.object(sys, "argv", ["pdf2svg.py", str(input_a), str(input_b), "--outdir", str(outdir)]), \
                 patch("pdf2svg.pdf_to_svg") as pdf_to_svg:
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(1, ctx.exception.code)
        self.assertIn("Duplicate output path", stdout.getvalue())
        self.assertIn("figure.svg", stdout.getvalue())
        pdf_to_svg.assert_not_called()

    def test_transparentize_in_place_rejects_svg_directory_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg_dir = Path(tmp) / "figure.svg"
            svg_dir.mkdir()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = transparentize_in_place(svg_dir)

        self.assertFalse(result)
        self.assertIn("is a directory", stdout.getvalue())

    def test_transparentize_in_place_reports_unreadable_svg_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "figure.svg"
            svg.write_bytes(b"\xff\xfe<svg>")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = transparentize_in_place(svg)

        self.assertFalse(result)
        self.assertIn("could not read", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_transparentize_in_place_rejects_malformed_svg_without_false_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "figure.svg"
            svg.write_text("not actually svg", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = transparentize_in_place(svg)

        self.assertFalse(result)
        self.assertIn("not a valid SVG", stdout.getvalue())
        self.assertNotIn("already transparent", stdout.getvalue())

    def test_transparentize_in_place_reports_write_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "figure.svg"
            svg.write_text('<svg><rect fill="rgb(100%,100%,100%)" /></svg>', encoding="utf-8")

            stdout = io.StringIO()
            with patch.object(Path, "write_text", side_effect=OSError("write failed")):
                with contextlib.redirect_stdout(stdout):
                    result = transparentize_in_place(svg)

        self.assertFalse(result)
        self.assertIn("could not write", stdout.getvalue())
        self.assertIn("write failed", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_transparentize_in_place_keeps_original_svg_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "figure.svg"
            original = '<svg><rect fill="rgb(100%,100%,100%)" /></svg>'
            svg.write_text(original, encoding="utf-8")

            stdout = io.StringIO()
            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with contextlib.redirect_stdout(stdout):
                    result = transparentize_in_place(svg)

            self.assertFalse(result)
            self.assertEqual(original, svg.read_text(encoding="utf-8"))
            self.assertIn("could not write", stdout.getvalue())
            self.assertIn("replace failed", stdout.getvalue())
            self.assertNotIn("Traceback", stdout.getvalue())

    def test_transparentize_in_place_expands_home_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            svg = fake_home / "figure.svg"
            svg.write_text('<svg><rect fill="rgb(100%,100%,100%)" /></svg>', encoding="utf-8")

            with patch.dict("os.environ", {"HOME": str(fake_home)}):
                self.assertTrue(transparentize_in_place("~/figure.svg"))

            self.assertNotIn("rgb(100%,100%,100%)", svg.read_text(encoding="utf-8"))

    def test_pdf_to_svg_preserves_stale_output_until_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")
            output.write_text("<svg>stale</svg>", encoding="utf-8")

            def write_svg(cmd, **_kwargs):
                Path(cmd[-1]).write_text("<svg>new</svg>", encoding="utf-8")

            with patch("pdf2svg.trash_path") as trash_path:
                with patch("pdf2svg.subprocess.run", side_effect=write_svg) as run:
                    pdf_to_svg(source, output, transparent=False)

            self.assertEqual("<svg>new</svg>", output.read_text(encoding="utf-8"))

        trash_path.assert_not_called()
        run.assert_called_once()
        self.assertNotEqual(str(output), run.call_args.args[0][-1])

    def test_pdf_to_svg_expands_home_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            source = fake_home / "figure.pdf"
            output = fake_home / "out" / "figure.svg"
            source.write_bytes(b"%PDF")

            def write_svg(cmd, **_kwargs):
                Path(cmd[-1]).write_text("<svg></svg>", encoding="utf-8")

            with patch.dict("os.environ", {"HOME": str(fake_home)}), \
                 patch("pdf2svg.shutil.which", return_value="/usr/local/bin/pdftocairo"), \
                 patch("pdf2svg.subprocess.run", side_effect=write_svg):
                result = pdf_to_svg("~/figure.pdf", "~/out/figure.svg", transparent=False)

            self.assertEqual(output, result)
            self.assertEqual("<svg></svg>", output.read_text(encoding="utf-8"))

    def test_pdf_to_svg_rejects_existing_output_directory_without_trashing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output_dir = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")
            output_dir.mkdir()

            with patch("pdf2svg.trash_path") as trash_path:
                with patch("pdf2svg.subprocess.run") as run:
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        result = pdf_to_svg(source, output_dir, transparent=False)

        self.assertIsNone(result)
        self.assertIn("Output path is a directory", stdout.getvalue())
        trash_path.assert_not_called()
        run.assert_not_called()

    def test_pdf_to_svg_rejects_non_svg_output_path_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.txt"
            source.write_bytes(b"%PDF")

            with patch("pdf2svg.subprocess.run") as run:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, transparent=False)

        self.assertIsNone(result)
        self.assertIn("Output path is not an SVG file", stdout.getvalue())
        run.assert_not_called()

    def test_pdf_to_svg_reports_output_parent_file_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            source.write_bytes(b"%PDF")
            parent_file = Path(tmp) / "not-a-directory"
            parent_file.write_text("file", encoding="utf-8")
            output = parent_file / "figure.svg"

            with patch("pdf2svg.subprocess.run") as run:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, transparent=False)

        self.assertIsNone(result)
        self.assertIn("could not prepare output directory", stdout.getvalue())
        run.assert_not_called()

    def test_pdf_to_svg_rejects_input_directory_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "figure.pdf"
            source_dir.mkdir()
            output = Path(tmp) / "figure.svg"

            with patch("pdf2svg.subprocess.run") as run:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source_dir, output, transparent=False)

        self.assertIsNone(result)
        self.assertIn("is a directory", stdout.getvalue())
        run.assert_not_called()

    def test_pdf_to_svg_rejects_output_same_as_input_without_trashing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            source.write_bytes(b"%PDF")

            with patch("pdf2svg.trash_path") as trash_path, \
                 patch("pdf2svg.subprocess.run") as run:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, source, transparent=False)

            self.assertIsNone(result)
            self.assertIn("Output path must not be the input PDF", stdout.getvalue())
            trash_path.assert_not_called()
            run.assert_not_called()
            self.assertTrue(source.exists())

    def test_pdf_to_svg_rejects_invalid_page_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")

            with patch("pdf2svg.subprocess.run") as run:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, page=0, transparent=False)

        self.assertIsNone(result)
        self.assertIn("Page must be >= 1", stdout.getvalue())
        run.assert_not_called()

    def test_pdf_to_svg_reports_missing_converter_output_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")

            with patch("pdf2svg.subprocess.run"):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, transparent=True)

        self.assertIsNone(result)
        self.assertIn("did not create", stdout.getvalue())

    def test_pdf_to_svg_reports_converter_spawn_oserror_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")
            output.write_text("<svg>stale</svg>", encoding="utf-8")

            with patch("pdf2svg.shutil.which", return_value="/usr/local/bin/pdftocairo"), \
                 patch("pdf2svg.subprocess.run", side_effect=OSError("cannot execute")):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, transparent=False)

            self.assertIsNone(result)
            self.assertEqual("<svg>stale</svg>", output.read_text(encoding="utf-8"))
            self.assertIn("ERROR converting", stdout.getvalue())
            self.assertIn("cannot execute", stdout.getvalue())
            self.assertNotIn("Traceback", stdout.getvalue())

    def test_pdf_to_svg_checks_converter_before_trashing_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")
            output.write_text("<svg>stale</svg>", encoding="utf-8")

            with patch("pdf2svg.trash_path") as trash_path, \
                 patch("pdf2svg.shutil.which", return_value=None):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
                    pdf_to_svg(source, output, transparent=False)

            self.assertEqual(1, ctx.exception.code)
            self.assertIn("pdftocairo not found", stdout.getvalue())
            trash_path.assert_not_called()
            self.assertTrue(output.exists())

    def test_pdf_to_svg_rejects_empty_or_malformed_converter_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")

            def write_bad_svg(cmd, **_kwargs):
                Path(cmd[-1]).write_text("not svg", encoding="utf-8")

            with patch("pdf2svg.subprocess.run", side_effect=write_bad_svg):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, transparent=True)

        self.assertIsNone(result)
        self.assertIn("not a valid SVG", stdout.getvalue())

    def test_pdf_to_svg_trashes_malformed_temp_converter_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            output.write_text("<svg>stale</svg>", encoding="utf-8")
            source.write_bytes(b"%PDF")
            trash_calls_when_output_exists = []

            def write_bad_svg(cmd, **_kwargs):
                Path(cmd[-1]).write_text("not svg", encoding="utf-8")

            def record_trash(path):
                trash_calls_when_output_exists.append(Path(path).exists())
                self.assertNotEqual(output, Path(path))

            with patch("pdf2svg.trash_path") as trash_path, \
                 patch("pdf2svg.subprocess.run", side_effect=write_bad_svg):
                trash_path.side_effect = record_trash
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, transparent=True)

            self.assertIsNone(result)
            self.assertIn("not a valid SVG", stdout.getvalue())
            self.assertIn(True, trash_calls_when_output_exists)
            self.assertEqual("<svg>stale</svg>", output.read_text(encoding="utf-8"))

    def test_pdf_to_svg_reports_unreadable_converter_output_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")

            def write_unreadable_svg(cmd, **_kwargs):
                Path(cmd[-1]).write_bytes(b"\xff\xfe<svg>")

            with patch("pdf2svg.subprocess.run", side_effect=write_unreadable_svg):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, transparent=True)

        self.assertIsNone(result)
        self.assertIn("could not read", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_pdf_to_svg_reports_transparent_write_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "figure.pdf"
            output = Path(tmp) / "figure.svg"
            source.write_bytes(b"%PDF")

            def write_svg(cmd, **_kwargs):
                Path(cmd[-1]).write_bytes(b'<svg><rect fill="rgb(100%,100%,100%)" /></svg>')

            stdout = io.StringIO()
            with patch("pdf2svg.subprocess.run", side_effect=write_svg), \
                 patch.object(Path, "write_text", side_effect=OSError("write failed")):
                with contextlib.redirect_stdout(stdout):
                    result = pdf_to_svg(source, output, transparent=True)

        self.assertIsNone(result)
        self.assertIn("could not write", stdout.getvalue())
        self.assertIn("write failed", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_write_text_atomically_keeps_existing_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "figure.svg"
            path.write_text("<svg>old</svg>", encoding="utf-8")

            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    write_text_atomically(path, "<svg>new</svg>")

            self.assertEqual("<svg>old</svg>", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

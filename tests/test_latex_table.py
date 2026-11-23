#!/usr/bin/env python3
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from latex_table import Cell, DataRow, convert_cell_content, generate_html, main, parse_cells, parse_col_spec, parse_overlay_spec, write_output_file  # noqa: E402


class LatexTableTests(unittest.TestCase):
    def test_convert_cell_content_escapes_literal_html(self):
        self.assertEqual(
            "A &lt; B &amp; C",
            convert_cell_content("A < B & C", wrap_stars=False, wrap_se=False),
        )

    def test_convert_cell_content_escapes_text_inside_generated_tags(self):
        self.assertEqual(
            "<strong>A &lt; B</strong> &amp;",
            convert_cell_content(r"\textbf{A < B} \&", wrap_stars=False, wrap_se=False),
        )

    def test_convert_cell_content_escapes_literal_generated_like_tags(self):
        self.assertEqual(
            '&lt;span class="stars"&gt;***&lt;/span&gt;',
            convert_cell_content('<span class="stars">***</span>', wrap_stars=False, wrap_se=False),
        )

    def test_convert_cell_content_preserves_latex_math_commands(self):
        self.assertEqual(
            r"$\alpha + \beta$",
            convert_cell_content(r"$\alpha + \beta$", wrap_stars=False, wrap_se=False),
        )

    def test_convert_cell_content_does_not_rewrite_literal_placeholder_text(self):
        self.assertEqual(
            "__WS_SAFE_HTML_0__ <strong>Bold</strong>",
            convert_cell_content(r"__WS_SAFE_HTML_0__ \textbf{Bold}", wrap_stars=False, wrap_se=False),
        )

    def test_parse_overlay_spec_rejects_attribute_injection(self):
        with self.assertRaises(ValueError):
            parse_overlay_spec('1:1" onclick="alert(1)', "row")

    def test_parse_overlay_spec_rejects_zero_indexes(self):
        with self.assertRaises(ValueError):
            parse_overlay_spec("0:1", "row")

    def test_parse_col_spec_ignores_column_modifiers(self):
        self.assertEqual(
            ["left", "right"],
            parse_col_spec(r">{\raggedright\arraybackslash}p{4cm}r"),
        )

    def test_parse_cells_keeps_escaped_ampersands_inside_cell(self):
        self.assertEqual([r"R\&D", "1"], parse_cells(r"R\&D & 1"))

    def test_main_keeps_escaped_ampersand_inside_rendered_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular}{lc}
                Name & Value \\
                \midrule
                R\&D & 1 \\
                \end{tabular}
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex)]):
                with contextlib.redirect_stdout(stdout):
                    main()

        output = stdout.getvalue()
        self.assertIn("<td>R&amp;D</td>", output)
        self.assertIn('<td style="text-align: center">1</td>', output)

    def test_main_parses_tabular_star_width_before_column_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular*}{\textwidth}{lc}
                Name & Value \\
                \midrule
                A & 1 \\
                \end{tabular*}
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex)]):
                with contextlib.redirect_stdout(stdout):
                    main()

        output = stdout.getvalue()
        self.assertIn("<th>Name</th>", output)
        self.assertNotIn("{lc}", output)
        self.assertIn('<td style="text-align: center">1</td>', output)

    def test_generate_html_escapes_caption_text(self):
        table = generate_html(
            [],
            [DataRow([Cell("value")])],
            ["left"],
            {},
            caption="A < B & C",
        )

        self.assertIn("<caption>A &lt; B &amp; C</caption>", table)

    def test_generate_html_rejects_unsafe_font_size(self):
        with self.assertRaises(ValueError):
            generate_html(
                [],
                [DataRow([Cell("value")])],
                ["left"],
                {},
                font_size='16px" onclick="alert(1)',
            )

    def test_main_reports_invalid_overlay_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular}{lc}
                Name & Value \\
                \midrule
                A & 1 \\
                \end{tabular}
                """,
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex), "--highlight-row", "0:1"]):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("Row must be >= 1", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_missing_input_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.tex"
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(missing)]):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("not found", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_unreadable_input_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_bytes(b"\xff\xfe\\begin{tabular}{l}")
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex)]):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("could not read", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_malformed_column_spec_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular}{D{.}}
                Name & Value \\
                \end{tabular}
                """,
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex)]):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("Malformed column specification", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_unclosed_row_spacing_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular}{lc}
                Name & Value \\[
                A & 1 \\
                \end{tabular}
                """,
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex)]):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("Malformed row spacing", stderr.getvalue())
        self.assertNotIn("substring not found", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_expands_home_directory_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            tex = fake_home / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular}{lc}
                Name & Value \\
                A & 1 \\
                \end{tabular}
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.dict("os.environ", {"HOME": str(fake_home)}), \
                 patch.object(sys, "argv", ["latex_table.py", "~/table.tex"]):
                with contextlib.redirect_stdout(stdout):
                    main()

        self.assertIn("<table", stdout.getvalue())
        self.assertIn("Name", stdout.getvalue())

    def test_main_reports_input_directory_without_traceback_or_writing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex_dir = Path(tmp) / "table.tex"
            tex_dir.mkdir()
            output = Path(tmp) / "table.html"
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex_dir), "-o", str(output)]):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("is a directory", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(output.exists())

    def test_write_output_file_preserves_existing_output_until_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "table.html"
            output.parent.mkdir()
            output.write_text("stale", encoding="utf-8")

            with patch("latex_table.trash_path") as trash_path:
                write_output_file(output, "<table></table>")

            trash_path.assert_not_called()
            self.assertEqual("<table></table>", output.read_text(encoding="utf-8"))

    def test_write_output_file_keeps_existing_output_when_atomic_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "table.html"
            output.write_text("stale", encoding="utf-8")

            with patch("latex_table.write_text_atomically", side_effect=OSError("write failed")):
                with self.assertRaises(RuntimeError):
                    write_output_file(output, "<table></table>")

            self.assertEqual("stale", output.read_text(encoding="utf-8"))

    def test_write_output_file_expands_home_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            with patch.dict("os.environ", {"HOME": str(fake_home)}):
                write_output_file(Path("~/tables/table.html"), "<table></table>")
                output = fake_home / "tables" / "table.html"

                self.assertEqual("<table></table>", output.read_text(encoding="utf-8"))

    def test_write_output_file_rejects_existing_directory_without_trashing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "existing-output-dir"
            output_dir.mkdir()

            with patch("latex_table.trash_path") as trash_path:
                with self.assertRaises(ValueError):
                    write_output_file(output_dir, "<table></table>")

        trash_path.assert_not_called()

    def test_write_output_file_rejects_non_html_output_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "table.txt"

            with patch("latex_table.write_text_atomically") as write_text:
                with self.assertRaises(ValueError) as ctx:
                    write_output_file(output, "<table></table>")

        self.assertIn("Output path is not an HTML file", str(ctx.exception))
        write_text.assert_not_called()
        self.assertFalse(output.exists())

    def test_write_output_file_reports_parent_file_without_trashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent_file = Path(tmp) / "not-a-directory"
            parent_file.write_text("file", encoding="utf-8")
            output = parent_file / "table.html"

            with patch("latex_table.trash_path") as trash_path:
                with self.assertRaises(RuntimeError) as ctx:
                    write_output_file(output, "<table></table>")

        self.assertIn("could not prepare output directory", str(ctx.exception))
        trash_path.assert_not_called()

    def test_write_output_file_does_not_leave_partial_output_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "table.html"

            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(RuntimeError) as ctx:
                    write_output_file(output, "<table></table>")

            self.assertFalse(output.exists())
            self.assertIn("could not write", str(ctx.exception))
            self.assertIn("replace failed", str(ctx.exception))

    def test_main_reports_output_directory_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular}{lc}
                Name & Value \\
                A & 1 \\
                \end{tabular}
                """,
                encoding="utf-8",
            )
            output_dir = Path(tmp) / "existing-output-dir"
            output_dir.mkdir()
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex), "-o", str(output_dir)]):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("Output path is a directory", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_rejects_output_path_same_as_input_without_trashing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular}{lc}
                Name & Value \\
                A & 1 \\
                \end{tabular}
                """,
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex), "-o", str(tex)]), \
                 patch("latex_table.trash_path") as trash_path:
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("Output path must not be the input table", stderr.getvalue())
        trash_path.assert_not_called()

    def test_main_reports_output_write_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tex = Path(tmp) / "table.tex"
            tex.write_text(
                r"""
                \begin{tabular}{lc}
                Name & Value \\
                A & 1 \\
                \end{tabular}
                """,
                encoding="utf-8",
            )
            output = Path(tmp) / "table.html"
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["latex_table.py", str(tex), "-o", str(output)]), \
                 patch.object(Path, "write_text", side_effect=OSError("write failed")):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("could not write", stderr.getvalue())
        self.assertIn("write failed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

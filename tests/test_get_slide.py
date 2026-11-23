#!/usr/bin/env python3
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from get_slide import main, parse_slides, resolve_deck_path  # noqa: E402


class GetSlideTests(unittest.TestCase):
    def test_resolve_deck_path_accepts_directory(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            index = deck_dir / "index.html"
            index.write_text("", encoding="utf-8")

            self.assertEqual(index, resolve_deck_path(str(deck_dir)))

    def test_resolve_deck_path_falls_back_to_repo_relative_deck_from_other_cwd(self):
        from tempfile import TemporaryDirectory

        repo_root = Path(__file__).resolve().parents[1]
        expected = repo_root / "examples" / "kitchen-sink" / "index.html"
        with TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmp)
                self.assertEqual(expected, resolve_deck_path("examples/kitchen-sink"))
            finally:
                os.chdir(old_cwd)

    def test_main_reports_missing_deck_without_traceback(self):
        missing = "/tmp/definitely-missing-webslides-deck/index.html"
        with patch.object(sys, "argv", ["get_slide.py", missing, "--list"]):
            with self.assertRaises(SystemExit) as ctx:
                main()

        self.assertIn("ERROR:", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    def test_main_reports_unreadable_deck_without_traceback(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_bytes(b"\xff\xfe<html>")

            with patch.object(sys, "argv", ["get_slide.py", str(index), "--list"]):
                with self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertIn("ERROR: could not read deck", str(ctx.exception))

    def test_main_reports_index_html_directory_without_traceback(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            (deck_dir / "index.html").mkdir()

            with patch.object(sys, "argv", ["get_slide.py", str(deck_dir), "--list"]):
                with self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertIn("ERROR:", str(ctx.exception))
        self.assertIn("is a directory", str(ctx.exception))
        self.assertNotIn("could not read deck", str(ctx.exception))

    def test_main_reports_non_html_input_without_traceback(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            deck_file = Path(tmp) / "deck.css"
            deck_file.write_text("body {}", encoding="utf-8")

            with patch.object(sys, "argv", ["get_slide.py", str(deck_file), "--list"]):
                with self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertIn("ERROR:", str(ctx.exception))
        self.assertIn("is not an HTML file", str(ctx.exception))
        self.assertNotIn("No .ws-deck", str(ctx.exception))

    def test_title_only_outputs_only_the_title(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="intro" class="slide content"><h2>Intro Title</h2></section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["get_slide.py", str(index), "1", "--title-only"]):
                with contextlib.redirect_stdout(stdout):
                    main()

        self.assertEqual("Intro Title\n", stdout.getvalue())

    def test_encoded_id_selector_matches_decoded_fragment(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="appendix detail" class="slide appendix content"><h2>Detail Title</h2></section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["get_slide.py", str(index), "#appendix%20detail", "--title-only"]):
                with contextlib.redirect_stdout(stdout):
                    main()

        self.assertEqual("Detail Title\n", stdout.getvalue())

    def test_encoded_id_selector_matches_html_entity_decoded_id(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="methods &amp; data" class="slide content"><h2>Methods</h2></section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["get_slide.py", str(index), "#methods%20%26%20data", "--title-only"]):
                with contextlib.redirect_stdout(stdout):
                    main()

        self.assertEqual("Methods\n", stdout.getvalue())

    def test_encoded_id_selector_prefers_decoded_browser_target_over_literal_percent_id(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="appendix%20detail" class="slide appendix content"><h2>Literal Percent</h2></section>
                  <section id="appendix detail" class="slide appendix content"><h2>Decoded Target</h2></section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["get_slide.py", str(index), "#appendix%20detail", "--title-only"]):
                with contextlib.redirect_stdout(stdout):
                    main()

        self.assertEqual("Decoded Target\n", stdout.getvalue())

    def test_encoded_id_selector_falls_back_to_literal_percent_id(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="appendix%20literal" class="slide appendix content"><h2>Literal Percent</h2></section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["get_slide.py", str(index), "#appendix%20literal", "--title-only"]):
                with contextlib.redirect_stdout(stdout):
                    main()

        self.assertEqual("Literal Percent\n", stdout.getvalue())

    def test_id_selector_rejects_duplicate_slide_ids_instead_of_returning_first_match(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="duplicate" class="slide content"><h2>First</h2></section>
                  <section id="duplicate" class="slide content"><h2>Second</h2></section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )
            with patch.object(sys, "argv", ["get_slide.py", str(index), "#duplicate", "--title-only"]):
                with self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertIn('ERROR: Multiple slides with id #duplicate', str(ctx.exception))

    def test_title_only_multiple_slides_outputs_one_title_per_line_without_blank_separators(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="intro" class="slide content"><h2>Intro Title</h2></section>
                  <section id="methods" class="slide content"><h2>Methods Title</h2></section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["get_slide.py", str(index), "1", "2", "--title-only"]):
                with contextlib.redirect_stdout(stdout):
                    main()

        self.assertEqual("Intro Title\nMethods Title\n", stdout.getvalue())

    def test_parse_slides_isolates_last_main_slide_before_appendix(self):
        html = """
        <div class="ws-deck">
          <section class="slide content" id="intro"><h2>Intro</h2></section>
          <section class="slide content" id="end"><h2>End</h2></section>
          <!-- appendix marker should not be included in previous slide -->
          <section class="slide appendix content" id="appendix-detail">
            <h2>Detail</h2>
          </section>
        </div>
        <div id="ws-footer"></div>
        """

        slides = parse_slides(html)
        self.assertEqual(3, len(slides))
        self.assertEqual("end", slides[1]["id"])
        self.assertTrue(slides[1]["html"].strip().endswith("</section>"))
        self.assertNotIn("appendix marker", slides[1]["html"])
        self.assertNotIn("</section></section>", slides[1]["html"].replace("\n", ""))

    def test_parse_slides_supports_reordered_classes_and_single_quotes(self):
        html = """
        <section id='outside' class='slide content'><h2>Outside deck</h2></section>
        <div class='ws-deck'>
          <section id='main' class='content slide'><h2>Main</h2></section>
          <section id='with-div' class='content slide'>
            <h2>Nested div</h2>
            <div><div>Inner content</div></div>
          </section>
          <section class='appendix content slide' id='appendix-detail'><h2>Detail</h2></section>
        </div>
        """

        slides = parse_slides(html)
        self.assertEqual(["main", "with-div", "appendix-detail"], [s["id"] for s in slides])
        self.assertEqual(1, slides[0]["main_number"])
        self.assertEqual(2, slides[1]["main_number"])
        self.assertTrue(slides[2]["is_appendix"])
        self.assertIsNone(slides[2]["main_number"])

    def test_parse_slides_supports_non_div_ws_deck_container(self):
        html = """
        <main class="ws-deck">
          <section id="main" class="slide content"><h2>Main</h2></section>
        </main>
        """

        slides = parse_slides(html)

        self.assertEqual(["main"], [s["id"] for s in slides])

    def test_parse_slides_supports_unquoted_attributes(self):
        html = """
        <div class=ws-deck>
          <section id=main class="content slide"><h2>Main</h2></section>
          <section id=appendix-detail class="appendix content slide"><h2>Detail</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["main", "appendix-detail"], [s["id"] for s in slides])
        self.assertFalse(slides[0]["is_appendix"])
        self.assertTrue(slides[1]["is_appendix"])

    def test_parse_slides_preserves_nested_section_content(self):
        html = """
        <div class="ws-deck">
          <section class="slide content" id="nested">
            <h2>Nested</h2>
            <section class="callout"><p>Inner section</p></section>
            <p>After nested section</p>
          </section>
          <section class="slide content" id="next"><h2>Next</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["nested", "next"], [s["id"] for s in slides])
        self.assertIn("After nested section", slides[0]["html"])

    def test_parse_slides_reports_unclosed_top_level_slide(self):
        html = """
        <div class="ws-deck">
          <section class="slide content" id="broken"><h2>Broken</h2>
        </div>
        """

        with self.assertRaises(SystemExit) as ctx:
            parse_slides(html)

        self.assertIn('ERROR: slide id="broken" missing closing </section>', str(ctx.exception))

    def test_parse_slides_ignores_nested_slides_not_directly_in_deck(self):
        html = """
        <div class="ws-deck">
          <section class="wrapper">
            <section class="slide content" id="nested-fake"><h2>Nested fake</h2></section>
          </section>
          <section class="slide content" id="real"><h2>Real</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real"], [s["id"] for s in slides])

    def test_parse_slides_ignores_slides_nested_inside_non_section_wrappers(self):
        html = """
        <div class="ws-deck">
          <div class="wrapper">
            <section class="slide content" id="nested-in-div"><h2>Nested fake</h2></section>
          </div>
          <section class="slide content" id="real"><h2>Real</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real"], [s["id"] for s in slides])

    def test_parse_slides_decodes_title_entities(self):
        html = """
        <div class="ws-deck">
          <section class="slide content" id="figures">
            <h2>Figures &amp; Images</h2>
          </section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual("Figures & Images", slides[0]["title"])

    def test_parse_slides_ignores_commented_fake_deck_markup(self):
        html = """
        <!-- <div class="ws-deck"><section class="slide" id="commented"><h2>Comment</h2></section></div> -->
        <div class="ws-deck">
          <section class="slide content" id="real"><h2>Real</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real"], [s["id"] for s in slides])

    def test_parse_slides_ignores_template_fake_deck_markup(self):
        html = """
        <template>
          <div class="ws-deck">
            <section class="slide" id="template-fake"><h2>Template fake</h2></section>
          </div>
        </template>
        <div class="ws-deck">
          <section class="slide content" id="real"><h2>Real</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real"], [s["id"] for s in slides])

    def test_parse_slides_ignores_data_class_before_actual_deck(self):
        html = """
        <div data-class="ws-deck">
          <section class="slide" id="fake"><h2>Fake</h2></section>
        </div>
        <div class="ws-deck">
          <section class="slide content" id="real"><h2>Real</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real"], [s["id"] for s in slides])

    def test_parse_slides_rejects_multiple_deck_containers(self):
        html = """
        <div class="ws-deck">
          <section class="slide content" id="first"><h2>First</h2></section>
        </div>
        <div class="ws-deck">
          <section class="slide content" id="second"><h2>Second</h2></section>
        </div>
        """

        with self.assertRaises(SystemExit) as ctx:
            parse_slides(html)

        self.assertIn("ERROR: MULTIPLE DECKS", str(ctx.exception))

    def test_parse_slides_rejects_nested_deck_containers(self):
        html = """
        <div class="ws-deck">
          <section class="slide content" id="first">
            <h2>First</h2>
            <div class="ws-deck">
              <section class="slide content" id="nested"><h2>Nested</h2></section>
            </div>
          </section>
        </div>
        """

        with self.assertRaises(SystemExit) as ctx:
            parse_slides(html)

        self.assertIn("ERROR: MULTIPLE DECKS", str(ctx.exception))
        self.assertIn("NESTED DECK", str(ctx.exception))

    def test_parse_slides_ignores_commented_slides_inside_deck(self):
        html = """
        <div class="ws-deck">
          <!-- <section class="slide" id="commented"><h2>Comment</h2></section> -->
          <section class="slide content" id="real"><h2>Real</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real"], [s["id"] for s in slides])

    def test_parse_slides_ignores_div_markup_inside_script_raw_text(self):
        html = """
        <div class="ws-deck">
          <script>
            const oldMarkup = "</div><section class='slide' id='fake'><h2>Fake</h2></section>";
          </script>
          <section class="slide content" id="real"><h2>Real</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real"], [s["id"] for s in slides])

    def test_parse_slides_preserves_tags_with_gt_inside_quoted_attributes(self):
        html = """
        <div data-label="Deck > marker" class="ws-deck">
          <section data-title="A > B" id="target" class="slide content">
            <h2>Quoted marker</h2>
          </section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["target"], [s["id"] for s in slides])
        self.assertIn('data-title="A > B"', slides[0]["html"])

    def test_parse_slides_extracts_title_when_heading_attrs_contain_gt(self):
        html = """
        <div class="ws-deck">
          <section id="target" class="slide content">
            <h2 data-label="A > B">Quoted heading</h2>
          </section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual("Quoted heading", slides[0]["title"])

    def test_parse_slides_uses_first_visible_h1_or_h2_in_document_order(self):
        html = """
        <div class="ws-deck">
          <section id="target" class="slide content">
            <h2>Visible slide title</h2>
            <h1>Later decorative heading</h1>
          </section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual("Visible slide title", slides[0]["title"])

    def test_parse_slides_strips_title_child_tags_with_gt_in_attrs(self):
        html = """
        <div class="ws-deck">
          <section id="target" class="slide content">
            <h2><span data-label="A > B">Quoted child</span></h2>
          </section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual("Quoted child", slides[0]["title"])

    def test_parse_slides_ignores_comments_inside_titles(self):
        html = """
        <div class="ws-deck">
          <section id="target" class="slide content">
            <h2><!-- <span>Old title</span> -->Real title</h2>
          </section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual("Real title", slides[0]["title"])

    def test_parse_slides_ignores_heading_markup_inside_script_and_style(self):
        html = """
        <div class="ws-deck">
          <section id="target" class="slide content">
            <script>
              const staleTitle = "<h2>Fake script title</h2>";
            </script>
            <style>
              .example::after { content: "<h2>Fake style title</h2>"; }
            </style>
            <h2>Real title</h2>
          </section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual("Real title", slides[0]["title"])

    def test_parse_slides_ignores_heading_markup_inside_template(self):
        html = """
        <div class="ws-deck">
          <section id="target" class="slide content">
            <template><h2>Fake template title</h2></template>
            <h2>Real title</h2>
          </section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual("Real title", slides[0]["title"])

    def test_parse_slides_ignores_commented_divs_inside_deck(self):
        html = """
        <div class="ws-deck">
          <!-- <div><div>Comment-only nesting</div> -->
          <section class="slide content" id="real"><h2>Real</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real"], [s["id"] for s in slides])

    def test_parse_slides_ignores_commented_section_closing_tags(self):
        html = """
        <div class="ws-deck">
          <section class="slide content" id="real">
            <h2>Real</h2>
            <!-- old markup ended here </section> -->
            <p>After comment</p>
          </section>
          <section class="slide content" id="next"><h2>Next</h2></section>
        </div>
        """

        slides = parse_slides(html)

        self.assertEqual(["real", "next"], [s["id"] for s in slides])
        self.assertIn("After comment", slides[0]["html"])


if __name__ == "__main__":
    unittest.main()

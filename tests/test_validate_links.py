#!/usr/bin/env python3
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from validate_links import parse_args, resolve_deck_path, validate  # noqa: E402


class ValidateLinksTests(unittest.TestCase):
    def test_parse_args_rejects_extra_arguments(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            parse_args(["examples/kitchen-sink", "--bad-option"])

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("unrecognized arguments: --bad-option", stderr.getvalue())

    def test_resolve_deck_path_accepts_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            index = deck_dir / "index.html"
            index.write_text("", encoding="utf-8")

            self.assertEqual(index, resolve_deck_path(str(deck_dir)))

    def test_resolve_deck_path_falls_back_to_repo_relative_deck_from_other_cwd(self):
        repo_root = Path(__file__).resolve().parents[1]
        expected = repo_root / "examples" / "kitchen-sink" / "index.html"
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmp)
                self.assertEqual(expected, resolve_deck_path("examples/kitchen-sink"))
            finally:
                os.chdir(old_cwd)

    def test_invalid_utf8_deck_reports_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_bytes(b"\xff\xfe<html>")

            issues = validate(str(index))

        self.assertEqual(1, len(issues))
        self.assertTrue(issues[0].startswith("ERROR: could not read deck "))

    def test_index_html_directory_reports_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            (deck_dir / "index.html").mkdir()

            issues = validate(str(deck_dir))

        self.assertEqual(1, len(issues))
        self.assertIn("is a directory", issues[0])
        self.assertNotIn("could not read deck", issues[0])

    def test_non_html_input_reports_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "deck.css"
            deck.write_text("body {}", encoding="utf-8")

            issues = validate(str(deck))

        self.assertEqual(1, len(issues))
        self.assertIn("is not an HTML file", issues[0])
        self.assertNotIn("No .ws-deck", issues[0])

    def validate_html(self, html):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index.html"
            index.write_text(html, encoding="utf-8")
            return validate(str(index))

    def test_class_order_and_multiple_classes_are_supported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a class="small ws-appendix-link button" href="#appendix-detail">Details</a>
              </section>
              <section class="slide appendix content" id="appendix-detail">
                <h2>Detail</h2>
                <a href="#" class="quiet ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_non_div_ws_deck_container_is_validated_like_engine(self):
        issues = self.validate_html(
            """
            <html><body>
            <main class="ws-deck">
              <section id="main" class="slide content">
                <img src="figures/missing-from-main-deck.png">
              </section>
            </main>
            </body></html>
            """
        )

        self.assertNotIn("MISSING DECK: no .ws-deck container found", issues)
        self.assertIn("MISSING FILE: figures/missing-from-main-deck.png", issues)

    def test_appendix_with_reordered_classes_still_requires_back_link(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#appendix-detail" class="ws-appendix-link">Details</a>
              </section>
              <section id="appendix-detail" class="slide appendix content">
                <h2>Detail</h2>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn('MISSING BACK BUTTON: appendix slide id="appendix-detail"', issues)

    def test_appendix_slide_without_id_is_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
              <section class="slide appendix content">
                <h2>Unlinkable Appendix</h2>
                <a href="#" class="ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING ID: appendix slide has no id", issues)

    def test_unclosed_top_level_slide_is_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="broken" class="slide content">
                <h2>Broken</h2>
            </div>
            </body></html>
            """
        )

        self.assertIn('UNCLOSED SECTION: slide id="broken" is missing closing </section>', issues)

    def test_unclosed_ws_deck_container_is_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
            </body></html>
            """
        )

        self.assertIn("UNCLOSED DECK: .ws-deck is missing closing </div>", issues)

    def test_unclosed_non_div_ws_deck_reports_matching_tag(self):
        issues = self.validate_html(
            """
            <html><body>
            <main class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
            </body></html>
            """
        )

        self.assertIn("UNCLOSED DECK: .ws-deck is missing closing </main>", issues)

    def test_browser_recoverable_unclosed_child_does_not_report_unclosed_deck(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <p>Browser will close this paragraph
              </section>
            </div>
            </body></html>
            """
        )

        self.assertNotIn("UNCLOSED DECK: .ws-deck is missing closing </div>", issues)

    def test_missing_ws_deck_container_is_reported(self):
        issues = self.validate_html(
            """
            <html><body>
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
            </body></html>
            """
        )

        self.assertIn("MISSING DECK: no .ws-deck container found", issues)

    def test_ws_deck_without_direct_slides_is_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <div class="wrapper">
                <section id="nested" class="slide content">
                  <h2>Nested fake</h2>
                </section>
              </div>
            </div>
            </body></html>
            """
        )

        self.assertIn("NO SLIDES: .ws-deck contains no direct .slide sections", issues)

    def test_multiple_ws_deck_containers_are_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
            </div>
            <div class="ws-deck">
              <section id="extra" class="slide content">
                <h2>Extra</h2>
              </section>
            </div>
            </body></html>
            """
        )

        self.assertIn("MULTIPLE DECKS: found 2 .ws-deck containers; expected exactly 1", issues)

    def test_nested_ws_deck_container_is_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
                <div class="ws-deck">
                  <section id="nested" class="slide content">
                    <h2>Nested</h2>
                  </section>
                </div>
              </section>
            </div>
            </body></html>
            """
        )

        self.assertIn("NESTED DECK: .ws-deck containers must not be nested", issues)

    def test_non_slide_appendix_like_ids_are_not_orphan_appendices(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <figure id="appendix-figure-caption">Not an appendix slide</figure>
                <a href="#appendix-detail" class="ws-appendix-link">Details</a>
              </section>
              <section id="appendix-detail" class="slide appendix content">
                <h2>Detail</h2>
                <a href="#" class="ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_appendix_link_to_non_slide_id_is_broken(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <figure id="appendix-figure-caption">Not an appendix slide</figure>
                <a href="#appendix-figure-caption" class="ws-appendix-link">Bad details</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn(
            'BROKEN LINK: href="#appendix-figure-caption" — no appendix slide with this id',
            issues,
        )

    def test_appendix_link_to_non_appendix_hash_is_broken(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#detail" class="ws-appendix-link">Bad details</a>
              </section>
              <section id="detail" class="slide content">
                <h2>Not Appendix</h2>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn(
            'BROKEN LINK: href="#detail" — no appendix slide with this id',
            issues,
        )

    def test_appendix_link_to_nested_id_inside_appendix_slide_is_valid(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#appendix-detail-target" class="ws-appendix-link">Details</a>
              </section>
              <section id="appendix-detail" class="slide appendix content">
                <h2>Detail</h2>
                <p id="appendix-detail-target" data-step="2">Deep target</p>
                <a href="#" class="ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )

        self.assertEqual([], issues)

    def test_appendix_link_to_nested_id_inside_non_appendix_slide_is_broken(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#ordinary-detail-target" class="ws-appendix-link">Details</a>
              </section>
              <section id="ordinary-detail" class="slide content">
                <h2>Not Appendix</h2>
                <p id="ordinary-detail-target">Deep target</p>
              </section>
            </div>
            </body></html>
            """
        )

        self.assertIn(
            'BROKEN LINK: href="#ordinary-detail-target" — no appendix slide with this id',
            issues,
        )

    def test_appendix_link_to_nested_fake_appendix_slide_is_broken(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#nested-appendix" class="ws-appendix-link">Bad nested details</a>
              </section>
              <section class="wrapper">
                <section id="nested-appendix" class="slide appendix content">
                  <h2>Nested fake appendix</h2>
                  <a href="#" class="ws-back-link">Back</a>
                </section>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn(
            'BROKEN LINK: href="#nested-appendix" — no appendix slide with this id',
            issues,
        )

    def test_appendix_link_to_div_nested_fake_appendix_slide_is_broken(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#nested-appendix" class="ws-appendix-link">Bad nested details</a>
              </section>
              <div class="wrapper">
                <section id="nested-appendix" class="slide appendix content">
                  <h2>Nested fake appendix</h2>
                  <a href="#" class="ws-back-link">Back</a>
                </section>
              </div>
            </div>
            </body></html>
            """
        )
        self.assertIn(
            'BROKEN LINK: href="#nested-appendix" — no appendix slide with this id',
            issues,
        )

    def test_appendix_slide_id_does_not_need_appendix_prefix(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#robustness" class="ws-appendix-link">Details</a>
              </section>
              <section id="robustness" class="slide appendix content">
                <h2>Robustness</h2>
                <a href="#" class="ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_encoded_appendix_href_matches_decoded_slide_id(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#appendix%20encoded" class="ws-appendix-link">Details</a>
              </section>
              <section id="appendix encoded" class="slide appendix content">
                <h2>Encoded Appendix</h2>
                <a href="#" class="ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_appendix_href_can_match_literal_percent_slide_id(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#appendix%20literal" class="ws-appendix-link">Details</a>
              </section>
              <section id="appendix%20literal" class="slide appendix content">
                <h2>Literal Percent Appendix</h2>
                <a href="#" class="ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_duplicate_ids_inside_ws_deck_are_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="dup" class="slide content">
                <h2>Main</h2>
              </section>
              <section id="dup" class="slide content">
                <h2>Duplicate</h2>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn('DUPLICATE ID: id="dup" appears 2 times inside .ws-deck', issues)

    def test_duplicate_ids_outside_ws_deck_are_ignored(self):
        issues = self.validate_html(
            """
            <html><body>
            <template>
              <p id="template-dup">One</p>
              <p id="template-dup">Two</p>
            </template>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_duplicate_target_id_outside_ws_deck_is_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div hidden>
              <p id="appendix-detail">Outside duplicate</p>
            </div>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#appendix-detail" class="ws-appendix-link">Details</a>
              </section>
              <section id="appendix-detail" class="slide appendix content">
                <h2>Detail</h2>
                <a href="#" class="ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn(
            'DUPLICATE TARGET ID: id="appendix-detail" appears outside .ws-deck and can intercept navigation',
            issues,
        )

    def test_duplicate_target_id_inside_template_is_ignored(self):
        issues = self.validate_html(
            """
            <html><body>
            <template>
              <p id="appendix-detail">Template duplicate</p>
            </template>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#appendix-detail" class="ws-appendix-link">Details</a>
              </section>
              <section id="appendix-detail" class="slide appendix content">
                <h2>Detail</h2>
                <a href="#" class="ws-back-link">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_appendix_sections_outside_ws_deck_are_ignored(self):
        issues = self.validate_html(
            """
            <html><body>
            <template>
              <section id="appendix-template" class="slide appendix content">
                <h2>Template only</h2>
              </section>
            </template>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_images_outside_ws_deck_are_ignored(self):
        issues = self.validate_html(
            """
            <html><body>
            <template>
              <img src="figures/missing-template-image.png">
            </template>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_appendix_links_outside_ws_deck_are_ignored(self):
        issues = self.validate_html(
            """
            <html><body>
            <template>
              <a class="ws-appendix-link" href="#missing-template-appendix">Template details</a>
            </template>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <h2>Main</h2>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_broken_non_appendix_hash_links_inside_ws_deck_are_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#missing-methods">Methods</a>
                <a href="#main">Top</a>
                <a href="#">No target</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn('BROKEN HASH LINK: href="#missing-methods" — no element with this id', issues)
        self.assertNotIn('BROKEN HASH LINK: href="#main" — no element with this id', issues)
        self.assertNotIn('BROKEN HASH LINK: href="#" — no element with this id', issues)

    def test_hash_links_to_ids_outside_ws_deck_are_reported(self):
        issues = self.validate_html(
            """
            <html><body>
            <div hidden>
              <section id="template-target">Template only</section>
            </div>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#template-target">Template target</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn(
            'BROKEN HASH LINK: href="#template-target" — no element inside .ws-deck with this id',
            issues,
        )

    def test_self_closing_images_inside_ws_deck_are_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <img src="figures/missing-self-closing.png" />
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: figures/missing-self-closing.png", issues)

    def test_relative_local_images_inside_ws_deck_are_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <img src="./images/missing%20chart.png?version=1#view">
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: ./images/missing%20chart.png?version=1#view", issues)

    def test_local_resource_directories_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "figures").mkdir()
            index = root / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="main" class="slide content">
                    <img src="figures">
                  </section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("INVALID FILE: figures is a directory", issues)

    def test_external_and_data_images_inside_ws_deck_are_ignored(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <img src="https://example.com/chart.png">
                <img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E">
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_file_urls_inside_ws_deck_are_reported_as_invalid(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <img src="file:///Users/me/private/chart.png">
                <a href="file:///tmp/handout.pdf">Handout</a>
              </section>
            </div>
            </body></html>
            """
        )

        self.assertIn("INVALID FILE URL: file:///Users/me/private/chart.png", issues)
        self.assertIn("INVALID FILE URL: file:///tmp/handout.pdf", issues)

    def test_root_relative_local_images_are_checked_against_server_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").mkdir()
            (root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck_dir = root / "decks" / "talk"
            deck_dir.mkdir(parents=True)
            index = deck_dir / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="main" class="slide content">
                    <img src="/assets/missing.png">
                  </section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("MISSING FILE: /assets/missing.png", issues)

    def test_root_relative_parent_traversal_cannot_escape_server_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            server_root = workspace / "project"
            (server_root / "engine").mkdir(parents=True)
            (server_root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            outside_server_file = workspace / "outside.png"
            outside_server_file.write_bytes(b"exists but cannot be served by this deck URL")
            deck_dir = server_root / "decks" / "talk"
            deck_dir.mkdir(parents=True)
            index = deck_dir / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="main" class="slide content">
                    <img src="/../outside.png">
                  </section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("MISSING FILE: /../outside.png", issues)

    def test_parent_traversal_is_resolved_like_browser_url_against_server_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            server_root = workspace / "project"
            (server_root / "engine").mkdir(parents=True)
            (server_root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck_dir = server_root / "decks" / "talk"
            deck_dir.mkdir(parents=True)
            outside_server_file = workspace / "outside.png"
            outside_server_file.write_bytes(b"exists but cannot be served by this deck URL")
            index = deck_dir / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="main" class="slide content">
                    <img src="../../../outside.png">
                  </section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("MISSING FILE: ../../../outside.png", issues)

    def test_parent_traversal_can_resolve_to_existing_file_under_server_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            server_root = Path(tmp) / "project"
            (server_root / "engine").mkdir(parents=True)
            (server_root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            (server_root / "assets").mkdir()
            (server_root / "assets" / "chart.png").write_bytes(b"png")
            deck_dir = server_root / "decks" / "talk"
            deck_dir.mkdir(parents=True)
            index = deck_dir / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="main" class="slide content">
                    <img src="../../assets/chart.png">
                  </section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertEqual([], issues)

    def test_relative_stylesheet_and_script_resources_are_checked(self):
        issues = self.validate_html(
            """
            <html>
            <head>
              <link rel="stylesheet" href="styles/missing.css">
              <script src="./scripts/missing%20deck.js?cache=1"></script>
              <link rel="stylesheet" href="https://cdn.example.com/deck.css">
              <script src="data:text/javascript,console.log(1)"></script>
            </head>
            <body>
              <div class="ws-deck">
                <section id="main" class="slide content">
                  <h2>Main</h2>
                </section>
              </div>
            </body>
            </html>
            """
        )
        self.assertIn("MISSING FILE: styles/missing.css", issues)
        self.assertIn("MISSING FILE: ./scripts/missing%20deck.js?cache=1", issues)
        self.assertNotIn("MISSING FILE: https://cdn.example.com/deck.css", issues)
        self.assertNotIn("MISSING FILE: data:text/javascript,console.log(1)", issues)

    def test_root_relative_stylesheet_is_checked_against_server_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").mkdir()
            (root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck_dir = root / "decks" / "talk"
            deck_dir.mkdir(parents=True)
            index = deck_dir / "index.html"
            index.write_text(
                """
                <html>
                <head>
                  <link rel="stylesheet" href="/styles/missing.css">
                </head>
                <body>
                  <div class="ws-deck"><section class="slide"><h2>Main</h2></section></div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("MISSING FILE: /styles/missing.css", issues)

    def test_css_url_resources_are_checked(self):
        issues = self.validate_html(
            """
            <html>
            <head>
              <style>
                .hero { background-image: url("figures/missing-hero.png"); }
                .remote { background-image: url(https://example.com/hero.png); }
                .embedded { background-image: url(data:image/png;base64,abc); }
                .fragment { mask-image: url(#mask); }
              </style>
            </head>
            <body>
              <div class="ws-deck">
                <section id="main" class="slide content"
                         style="background-image: url('./figures/missing%20inline.jpg?cache=1#frag')">
                  <h2>Main</h2>
                </section>
              </div>
            </body>
            </html>
            """
        )
        self.assertIn("MISSING FILE: figures/missing-hero.png", issues)
        self.assertIn("MISSING FILE: ./figures/missing%20inline.jpg?cache=1#frag", issues)
        self.assertNotIn("MISSING FILE: https://example.com/hero.png", issues)
        self.assertNotIn("MISSING FILE: data:image/png;base64,abc", issues)
        self.assertNotIn("MISSING FILE: #mask", issues)

    def test_css_url_resources_in_local_stylesheets_are_checked_relative_to_css_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            styles_dir.mkdir()
            (styles_dir / "deck.css").write_text(
                """
                @import "../styles/missing-import.css";
                .hero { background-image: url("../figures/missing-from-css.png"); }
                .font { src: url("./fonts/missing.woff2"); }
                .remote { background: url("https://example.com/remote.png"); }
                .fragment { mask-image: url("#mask"); }
                """,
                encoding="utf-8",
            )
            index = root / "index.html"
            index.write_text(
                """
                <html>
                <head>
                  <link rel="alternate stylesheet" href="styles/deck.css">
                </head>
                <body>
                  <div class="ws-deck">
                    <section id="main" class="slide content">
                      <h2>Main</h2>
                    </section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("MISSING FILE: styles/deck.css -> ../figures/missing-from-css.png", issues)
        self.assertIn("MISSING FILE: styles/deck.css -> ./fonts/missing.woff2", issues)
        self.assertIn("MISSING FILE: styles/deck.css -> ../styles/missing-import.css", issues)
        self.assertNotIn("MISSING FILE: styles/deck.css -> https://example.com/remote.png", issues)
        self.assertNotIn("MISSING FILE: styles/deck.css -> #mask", issues)

    def test_css_file_urls_are_reported_as_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            styles_dir.mkdir()
            (styles_dir / "deck.css").write_text(
                '.hero { background-image: url("file:///Users/me/chart.png"); }\n',
                encoding="utf-8",
            )
            index = root / "index.html"
            index.write_text(
                """
                <html>
                <head><link rel="stylesheet" href="styles/deck.css"></head>
                <body>
                  <div class="ws-deck">
                    <section id="main" class="slide content"
                             style="background-image: url('file:///tmp/inline.png')">
                      <h2>Main</h2>
                    </section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("INVALID FILE URL: styles/deck.css -> file:///Users/me/chart.png", issues)
        self.assertIn("INVALID FILE URL: file:///tmp/inline.png", issues)

    def test_css_resource_directories_are_reported_relative_to_css_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            figures_dir = root / "figures"
            styles_dir.mkdir()
            figures_dir.mkdir()
            (styles_dir / "deck.css").write_text(
                '.hero { background-image: url("../figures"); }\n',
                encoding="utf-8",
            )
            index = root / "index.html"
            index.write_text(
                """
                <html>
                <head><link rel="stylesheet" href="styles/deck.css"></head>
                <body>
                  <div class="ws-deck">
                    <section id="main" class="slide content"><h2>Main</h2></section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("INVALID FILE: styles/deck.css -> ../figures is a directory", issues)

    def test_invalid_utf8_stylesheet_reports_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            styles_dir.mkdir()
            (styles_dir / "deck.css").write_bytes(b"\xff\xfe")
            index = root / "index.html"
            index.write_text(
                """
                <html>
                <head><link rel="stylesheet" href="styles/deck.css"></head>
                <body>
                  <div class="ws-deck">
                    <section id="main" class="slide content"><h2>Main</h2></section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertEqual(1, len(issues))
        self.assertTrue(issues[0].startswith("ERROR: could not read stylesheet styles/deck.css"))

    def test_stylesheet_directory_reports_single_invalid_file_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            styles_dir.mkdir()
            (styles_dir / "deck.css").mkdir()
            index = root / "index.html"
            index.write_text(
                """
                <html>
                <head><link rel="stylesheet" href="styles/deck.css"></head>
                <body>
                  <div class="ws-deck">
                    <section id="main" class="slide content"><h2>Main</h2></section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertEqual(["INVALID FILE: styles/deck.css is a directory"], issues)

    def test_css_url_resources_in_comments_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            styles_dir.mkdir()
            (styles_dir / "deck.css").write_text(
                """
                /*
                  .old-hero { background-image: url("../figures/commented-missing.png"); }
                  @import "../styles/commented-import.css";
                */
                .hero { background-image: none; }
                """,
                encoding="utf-8",
            )
            index = root / "index.html"
            index.write_text(
                """
                <html>
                <head>
                  <style>
                    /* .unused { background-image: url("figures/commented-inline.png"); } */
                  </style>
                  <link rel="stylesheet" href="styles/deck.css">
                </head>
                <body>
                  <div class="ws-deck">
                    <section id="main" class="slide content"
                             style="/* background-image: url('./figures/commented-attr.png') */">
                      <h2>Main</h2>
                    </section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertEqual([], issues)

    def test_css_imports_are_checked_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            styles_dir = root / "styles"
            styles_dir.mkdir()
            (styles_dir / "deck.css").write_text(
                '@import url("./theme.css");\n',
                encoding="utf-8",
            )
            (styles_dir / "theme.css").write_text(
                '.hero { background-image: url("../figures/missing-nested.png"); }\n',
                encoding="utf-8",
            )
            index = root / "index.html"
            index.write_text(
                """
                <html>
                <head>
                  <link rel="stylesheet" href="styles/deck.css">
                </head>
                <body>
                  <div class="ws-deck">
                    <section id="main" class="slide content"><h2>Main</h2></section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("MISSING FILE: styles/theme.css -> ../figures/missing-nested.png", issues)

    def test_relative_srcset_images_inside_ws_deck_are_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <picture>
                  <source srcset="figures/missing-small.png 1x, ./images/missing%20large.png 2x">
                  <img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E"
                       srcset="https://example.com/chart.png 1x, images/missing-fallback.png 2x">
                </picture>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: figures/missing-small.png", issues)
        self.assertIn("MISSING FILE: ./images/missing%20large.png", issues)
        self.assertIn("MISSING FILE: images/missing-fallback.png", issues)
        self.assertNotIn("MISSING FILE: https://example.com/chart.png", issues)

    def test_data_uri_srcset_inside_ws_deck_is_ignored(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <img srcset="data:image/svg+xml,%3Csvg%3E%3C/svg%3E 1x">
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)

    def test_relative_data_table_src_inside_ws_deck_is_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <div class="table-container" data-table-src="tables/missing%20table.html?version=1#frag"></div>
                <div class="table-container" data-table-src=""></div>
                <div class="table-container" data-table-src="https://example.com/table.html"></div>
                <div class="table-container" data-table-src="data:text/html,%3Ctable%3E%3C/table%3E"></div>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: tables/missing%20table.html?version=1#frag", issues)
        self.assertIn("INVALID TABLE SRC: data-table-src is empty", issues)
        self.assertNotIn("MISSING FILE: https://example.com/table.html", issues)
        self.assertNotIn("MISSING FILE: data:text/html,%3Ctable%3E%3C/table%3E", issues)

    def test_root_relative_data_table_src_is_checked_against_server_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").mkdir()
            (root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck_dir = root / "decks" / "talk"
            deck_dir.mkdir(parents=True)
            index = deck_dir / "index.html"
            index.write_text(
                """
                <html><body>
                <div class="ws-deck">
                  <section id="main" class="slide content">
                    <div class="table-container" data-table-src="/tables/missing.html"></div>
                  </section>
                </div>
                </body></html>
                """,
                encoding="utf-8",
            )

            issues = validate(str(index))

        self.assertIn("MISSING FILE: /tables/missing.html", issues)

    def test_relative_media_sources_inside_ws_deck_are_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <video src="media/missing-video.mp4"></video>
                <audio src="./media/missing%20audio.mp3?version=1"></audio>
                <video>
                  <source src="media/missing-source.webm">
                  <source src="https://example.com/video.webm">
                </video>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: media/missing-video.mp4", issues)
        self.assertIn("MISSING FILE: ./media/missing%20audio.mp3?version=1", issues)
        self.assertIn("MISSING FILE: media/missing-source.webm", issues)
        self.assertNotIn("MISSING FILE: https://example.com/video.webm", issues)

    def test_relative_rich_media_auxiliary_sources_are_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <video poster="media/missing-poster.jpg">
                  <track src="captions/missing.vtt" kind="captions">
                </video>
                <object data="figures/missing-diagram.svg"></object>
                <embed src="media/missing-embed.pdf">
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: media/missing-poster.jpg", issues)
        self.assertIn("MISSING FILE: captions/missing.vtt", issues)
        self.assertIn("MISSING FILE: figures/missing-diagram.svg", issues)
        self.assertIn("MISSING FILE: media/missing-embed.pdf", issues)

    def test_relative_svg_href_resources_inside_ws_deck_are_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <svg>
                  <use href="icons/missing-sprite.svg#arrow"></use>
                  <image href="figures/missing-svg-image.png"></image>
                  <use href="#local-symbol"></use>
                </svg>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: icons/missing-sprite.svg#arrow", issues)
        self.assertIn("MISSING FILE: figures/missing-svg-image.png", issues)
        self.assertNotIn("MISSING FILE: #local-symbol", issues)

    def test_relative_anchor_file_links_inside_ws_deck_are_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="appendix/missing-handout.pdf">Handout</a>
                <a href="./data/missing%20data.csv?download=1">Data</a>
                <a href="https://example.com/remote.pdf">Remote</a>
                <a href="mailto:test@example.com">Email</a>
                <a href="#main">Hash</a>
                <a href="#">Back</a>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: appendix/missing-handout.pdf", issues)
        self.assertIn("MISSING FILE: ./data/missing%20data.csv?download=1", issues)
        self.assertNotIn("MISSING FILE: https://example.com/remote.pdf", issues)
        self.assertNotIn("MISSING FILE: mailto:test@example.com", issues)
        self.assertNotIn("MISSING FILE: #main", issues)
        self.assertNotIn("MISSING FILE: #", issues)

    def test_relative_iframe_sources_inside_ws_deck_are_checked(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <iframe src="embeds/missing-demo.html"></iframe>
                <iframe srcdoc="<p>Inline</p>"></iframe>
                <iframe src="https://example.com/embed"></iframe>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertIn("MISSING FILE: embeds/missing-demo.html", issues)
        self.assertNotIn("MISSING FILE: https://example.com/embed", issues)

    def test_back_link_inside_nested_section_counts_for_appendix_slide(self):
        issues = self.validate_html(
            """
            <html><body>
            <div class="ws-deck">
              <section id="main" class="slide content">
                <a href="#appendix-detail" class="ws-appendix-link">Details</a>
              </section>
              <section id="appendix-detail" class="slide appendix content">
                <h2>Detail</h2>
                <section class="callout">
                  <a href="#" class="ws-back-link">Back</a>
                </section>
              </section>
            </div>
            </body></html>
            """
        )
        self.assertEqual([], issues)


if __name__ == "__main__":
    unittest.main()

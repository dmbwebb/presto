#!/usr/bin/env python3
import asyncio
import contextlib
import hashlib
import io
import http.server
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "tools-deck" / "index.html"
sys.path.insert(0, str(REPO / "tools"))
from export_pdf import (  # noqa: E402
    add_pdf_links,
    build_deck_url,
    find_webslides_root,
    normalized_pdf_links,
    parse_args,
    prepare_output_path,
    resolve_deck_path,
    start_server,
    validate_browser_deck_structure,
    validate_export_info,
    validate_export_links,
    validate_export_link_targets,
    validate_flat_slide_count,
    validate_pdf_output,
    write_stepped_pdf_atomically,
    write_browser_pdf_atomically,
    export_pdf,
    main,
)


def temp_deck(tmp, body, head="", engine=True):
    """A deck in tmp/deck/ with an engine symlink beside it (decks link ../engine/)."""
    root = Path(tmp)
    (root / "engine").symlink_to(REPO / "engine", target_is_directory=True)
    path = root / "deck" / "index.html"
    path.parent.mkdir()
    engine_tags = (
        '<link rel="stylesheet" href="../engine/webslides.css">'
        '<script defer src="../engine/webslides.js"></script>'
    ) if engine else ""
    path.write_text(
        f"<!DOCTYPE html><html><head>{engine_tags}{head}</head>"
        f'<body><div class="ws-deck">{body}</div></body></html>',
        encoding="utf-8",
    )
    return path


def squash(text):
    """Drop whitespace: text in scaled tables extracts with stray spaces between letters."""
    return "".join(text.split())


def pdf_texts(path):
    """Page texts of a PDF, whitespace removed (compare with squash(expected))."""
    from pypdf import PdfReader

    return [squash(page.extract_text()) for page in PdfReader(str(path)).pages]


class ExportPDFTests(unittest.TestCase):
    def test_export_server_handles_parallel_asset_fetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = start_server(Path(tmp), 0)
            try:
                self.assertIsInstance(server, http.server.ThreadingHTTPServer)
                self.assertTrue(server.daemon_threads)
            finally:
                server.shutdown()
                server.server_close()

    def test_build_deck_url_escapes_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deck = root / "Talk With Spaces" / "index.html"
            deck.parent.mkdir()
            deck.write_text("", encoding="utf-8")

            self.assertEqual(
                f"http://127.0.0.1:3333/Talk%20With%20Spaces/index.html",
                build_deck_url(deck, root, 3333),
            )

    def test_resolve_deck_path_accepts_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            index = deck_dir / "index.html"
            index.write_text("", encoding="utf-8")

            self.assertEqual(index.resolve(), resolve_deck_path(str(deck_dir)))

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

    def test_prepare_output_path_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "deck.pdf"

            prepared = prepare_output_path(output)

            self.assertEqual(output, prepared)
            self.assertTrue(output.parent.is_dir())

    def test_prepare_output_path_expands_home_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            with patch.dict("os.environ", {"HOME": str(fake_home)}):
                prepared = prepare_output_path("~/exports/deck.pdf")

            self.assertEqual(fake_home / "exports" / "deck.pdf", prepared)
            self.assertTrue(prepared.parent.is_dir())

    def test_prepare_output_path_rejects_non_pdf_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "deck.txt"

            with self.assertRaises(ValueError) as ctx:
                prepare_output_path(output)

        self.assertIn("Output path is not a PDF file", str(ctx.exception))
        self.assertFalse(output.parent.joinpath("deck.txt").exists())

    def test_prepare_output_path_preserves_existing_pdf_until_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "deck.pdf"
            output.write_bytes(b"stale pdf")

            with patch("export_pdf.trash_path") as trash_path:
                prepared = prepare_output_path(output)

            self.assertEqual(output, prepared)
            self.assertEqual(b"stale pdf", output.read_bytes())
            trash_path.assert_not_called()

    def test_export_keeps_existing_output_when_browser_export_fails(self):
        class FakeServer:
            server_address = ("127.0.0.1", 3333)

            def shutdown(self):
                pass

            def server_close(self):
                pass

        async def fail_export(_url, _output_path):
            raise RuntimeError("browser export failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").mkdir()
            (root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck = root / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            output = root / "slides.pdf"
            output.write_bytes(b"previous valid pdf")

            with patch("export_pdf.trash_path", side_effect=lambda path: Path(path).unlink()), \
                 patch("export_pdf.start_server", return_value=FakeServer()), \
                 patch("export_pdf.export_pdf_steps", side_effect=fail_export):
                with self.assertRaises(RuntimeError):
                    asyncio.run(export_pdf(str(deck), str(output)))

            self.assertEqual(b"previous valid pdf", output.read_bytes())

    def test_export_uses_bound_server_port_instead_of_preallocating_port(self):
        class FakeServer:
            server_address = ("127.0.0.1", 4567)

            def shutdown(self):
                pass

            def server_close(self):
                pass

        seen_urls = []

        async def capture_export(url, _output_path):
            seen_urls.append(url)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").mkdir()
            (root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck = root / "talk" / "index.html"
            deck.parent.mkdir()
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            output = root / "slides.pdf"

            with patch("export_pdf.start_server", return_value=FakeServer()) as start_server, \
                 patch("export_pdf.export_pdf_steps", side_effect=capture_export):
                asyncio.run(export_pdf(str(deck), str(output)))

        start_server.assert_called_once_with(root.resolve(), 0)
        self.assertEqual([f"http://127.0.0.1:4567/talk/index.html"], seen_urls)
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(encoding="utf-8")
        self.assertNotIn("find_free_port", source)
        self.assertNotIn("import socket", source)

    def test_prepare_output_path_rejects_existing_directory_without_trashing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "existing-output-dir"
            output_dir.mkdir()

            with patch("export_pdf.trash_path") as trash_path:
                with self.assertRaises(ValueError):
                    prepare_output_path(output_dir)

        trash_path.assert_not_called()

    def test_prepare_output_path_reports_parent_file_without_trashing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent_file = Path(tmp) / "not-a-directory"
            parent_file.write_text("file", encoding="utf-8")
            output = parent_file / "deck.pdf"

            with patch("export_pdf.trash_path") as trash_path:
                with self.assertRaises(ValueError) as ctx:
                    prepare_output_path(output)

        self.assertIn("could not prepare output directory", str(ctx.exception))
        trash_path.assert_not_called()

    def test_export_rejects_output_path_same_as_input_deck_without_trashing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            stderr = io.StringIO()

            with patch("export_pdf.trash_path") as trash_path:
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main([str(deck), "--output", str(deck)])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("Output path must not be the input deck", stderr.getvalue())
        trash_path.assert_not_called()

    def test_main_reports_missing_deck_without_starting_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing" / "index.html"
            stderr = io.StringIO()
            stdout = io.StringIO()
            with patch("export_pdf.start_server") as start_server:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        main([str(missing)])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("does not exist", stderr.getvalue())
        self.assertEqual("", stdout.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        start_server.assert_not_called()

    def test_find_webslides_root_falls_back_to_deck_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "standalone" / "index.html"
            deck.parent.mkdir()
            deck.write_text("", encoding="utf-8")

            self.assertEqual(deck.parent, find_webslides_root(deck))

    def test_find_webslides_root_walks_deep_project_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "webslides-project"
            (root / "engine").mkdir(parents=True)
            (root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck_dir = root
            for i in range(12):
                deck_dir = deck_dir / f"level-{i}"
            deck_dir.mkdir(parents=True)
            deck = deck_dir / "index.html"
            deck.write_text("", encoding="utf-8")

            self.assertEqual(root, find_webslides_root(deck))

    def test_flat_export_applies_reveal_state_as_the_engine_parses_it(self):
        """data-class names may contain ':'; an empty range means step 1; malformed ranges never apply."""
        with tempfile.TemporaryDirectory() as tmp:
            deck = temp_deck(tmp, """
<section class="slide">
  <p class="gate" data-class="is:open:2">COLON CLASS SHOWN</p>
  <p class="gate" data-class="shown:">EMPTY RANGE SHOWN</p>
  <p class="gate" data-class="shown:2-x">MALFORMED HIDDEN</p>
  <p data-step="1">STEP ONE SHOWN</p>
  <p data-dim="1">DIMMED SHOWN</p>
</section>""", head="""<style>
  .gate { display: none; }
  .gate.shown, .gate.is\\:open { display: block; }
</style>""")
            output = Path(tmp) / "flat.pdf"
            asyncio.run(export_pdf(str(deck), str(output), flat=True))
            text = pdf_texts(output)[0]
        for shown in ("COLON CLASS SHOWN", "EMPTY RANGE SHOWN", "STEP ONE SHOWN", "DIMMED SHOWN"):
            self.assertIn(squash(shown), text)
        self.assertNotIn(squash("MALFORMED HIDDEN"), text)

    def test_flat_export_of_fixture_hides_early_ranged_reveals_and_shows_every_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "flat.pdf"
            asyncio.run(export_pdf(str(FIXTURE), str(output), flat=True))
            texts = pdf_texts(output)
        self.assertEqual(16, len(texts))
        ranged, svg, table = texts[1], texts[2], texts[3]
        self.assertIn(squash("Late text from step three onward"), ranged)
        self.assertNotIn(squash("Early text"), ranged)  # data-step="1-2" ends before the last step (3)
        self.assertIn(squash("Step three bar"), svg)  # steps inside the inlined SVG
        self.assertIn(squash("Row at step two"), table)  # external table rows

    def test_stepped_export_of_fixture_has_one_page_per_engine_step(self):
        from pypdf import PdfReader

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "stepped.pdf"
            asyncio.run(export_pdf(str(FIXTURE), str(output)))
            pages = PdfReader(str(output)).pages
            images = [hashlib.sha1(page.images[0].data).hexdigest() for page in pages]
        reveal_each = 4 if len(images) == 28 else 0  # data-reveal="each" needs contract C2
        # 16 slides plus ranged 3, SVG 3 and table 2 extra steps (declared data-steps="5" is ignored)
        self.assertEqual(16 + 3 + 3 + 2 + reveal_each, len(images))
        svg_steps = images[5:9]
        self.assertEqual(4, len(set(svg_steps)), "every SVG reveal step should look different")
        table_steps = images[9:12]
        self.assertEqual(3, len(set(table_steps)), "every table row step should look different")

    def test_flat_export_styles_only_engine_slide_set(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def export_pdf_flat")
        block = source[start : source.index("\n\nasync def export_pdf(", start)]
        self.assertIn('document.querySelectorAll(".ws-deck > .slide")', block)
        self.assertNotIn('document.querySelectorAll(".slide")', block)

    def test_flat_export_ignores_nested_fake_slides_in_real_browser(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").symlink_to(repo / "engine", target_is_directory=True)
            deck = root / "index.html"
            deck.write_text(
                """
                <!doctype html>
                <html>
                <head>
                  <meta charset="utf-8">
                  <meta name="viewport" content="width=1280">
                  <link rel="stylesheet" href="engine/webslides.css">
                  <script defer src="engine/webslides.js"></script>
                </head>
                <body>
                  <div class="ws-deck">
                    <section class="slide"><h1>One</h1></section>
                    <div class="wrapper">
                      <section class="slide"><h1>Nested fake</h1></section>
                    </div>
                    <section class="slide"><h1>Two</h1></section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )
            output = root / "out.pdf"

            asyncio.run(export_pdf(str(deck), str(output), flat=True))

            validate_pdf_output(output, expected_pages=2)

    def test_flat_export_ignores_whitespace_data_class_tokens_in_real_browser(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").symlink_to(repo / "engine", target_is_directory=True)
            deck = root / "index.html"
            deck.write_text(
                """
                <!doctype html>
                <html>
                <head>
                  <meta charset="utf-8">
                  <meta name="viewport" content="width=1280">
                  <link rel="stylesheet" href="engine/webslides.css">
                  <script defer src="engine/webslides.js"></script>
                </head>
                <body>
                  <div class="ws-deck">
                    <section class="slide">
                      <h1>One</h1>
                      <p data-class="bad token:1">Malformed class token</p>
                    </section>
                  </div>
                </body>
                </html>
                """,
                encoding="utf-8",
            )
            output = root / "out.pdf"

            asyncio.run(export_pdf(str(deck), str(output), flat=True))

            validate_pdf_output(output, expected_pages=1)

    def test_add_pdf_links_noops_without_reading_pdf_when_no_links_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_pdf = Path(tmp) / "missing.pdf"

            add_pdf_links(missing_pdf, [[], None], {})

    def test_add_pdf_links_ignores_malformed_link_objects_without_reading_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_pdf = Path(tmp) / "missing.pdf"

            add_pdf_links(
                missing_pdf,
                [
                    [
                        None,
                        "bad",
                        {"targetSlideIndex": 1, "targetStep": 0},
                        {"targetSlideIndex": 1, "targetStep": 0, "x": 10, "y": 10, "w": 0, "h": 20},
                    ]
                ],
                {(1, 0): 1},
            )

    def test_add_pdf_links_ignores_links_from_unknown_source_pages_without_reading_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_pdf = Path(tmp) / "missing.pdf"

            add_pdf_links(
                missing_pdf,
                [
                    [],
                    [{"targetSlideIndex": 0, "targetStep": 0, "x": 10, "y": 20, "w": 30, "h": 40}],
                ],
                {(0, 0): 0},
            )

    def test_normalized_pdf_links_keeps_only_valid_cross_page_links(self):
        links = normalized_pdf_links(
            [
                [
                    {"targetSlideIndex": 1, "targetStep": 0, "x": 10, "y": 20, "w": 30, "h": 40},
                    {"targetSlideIndex": 0, "targetStep": 0, "x": 10, "y": 20, "w": 30, "h": 40},
                    {"targetSlideIndex": 1, "targetStep": 0, "x": 10, "y": 20, "w": -1, "h": 40},
                    {"targetSlideIndex": 2, "targetStep": 0, "x": 10, "y": 20, "w": 30, "h": 40},
                ]
            ],
            {(0, 0): 0, (1, 0): 2},
        )

        self.assertEqual([(0, 2, {"x": 10.0, "y": 20.0, "w": 30.0, "h": 40.0})], links)

    def test_normalized_pdf_links_rejects_bool_metadata(self):
        links = normalized_pdf_links(
            [
                [
                    {"targetSlideIndex": True, "targetStep": 0, "x": 10, "y": 20, "w": 30, "h": 40},
                    {"targetSlideIndex": 1, "targetStep": False, "x": 10, "y": 20, "w": 30, "h": 40},
                    {"targetSlideIndex": 1, "targetStep": 0, "x": True, "y": 20, "w": 30, "h": 40},
                ]
            ],
            {(0, 0): 0, (1, 0): 2},
        )

        self.assertEqual([], links)

    def test_validate_export_links_rejects_malformed_browser_link_results(self):
        malformed_cases = [
            None,
            "not links",
            [None],
            ["bad"],
            [{"targetSlideIndex": 1}, "bad"],
            [{"targetSlideIndex": 1, "targetStep": 0, "x": 1, "y": 2, "w": 3}],
            [{"targetSlideIndex": 1, "targetStep": 0, "x": 1, "y": 2, "w": 3, "h": float("inf")}],
            [{"targetSlideIndex": 1.5, "targetStep": 0, "x": 1, "y": 2, "w": 3, "h": 4}],
            [{"targetSlideIndex": True, "targetStep": 0, "x": 1, "y": 2, "w": 3, "h": 4}],
            [{"targetSlideIndex": 1, "targetStep": False, "x": 1, "y": 2, "w": 3, "h": 4}],
            [{"targetSlideIndex": -1, "targetStep": 0, "x": 1, "y": 2, "w": 3, "h": 4}],
            [{"targetSlideIndex": 1, "targetStep": -1, "x": 1, "y": 2, "w": 3, "h": 4}],
            [{"targetSlideIndex": 1, "targetStep": 0, "x": True, "y": 2, "w": 3, "h": 4}],
            [{"targetSlideIndex": 1, "targetStep": 0, "x": 1, "y": 2, "w": 0, "h": 4}],
            [{"targetSlideIndex": 1, "targetStep": 0, "x": 1, "y": 2, "w": 3, "h": -1}],
        ]
        for links in malformed_cases:
            with self.subTest(links=links):
                with self.assertRaises(ValueError):
                    validate_export_links(links)

        valid = [{"targetSlideIndex": 1, "targetStep": 0, "x": 1, "y": 2, "w": 3, "h": 4}]
        self.assertEqual(valid, validate_export_links(valid))

    def test_validate_export_link_targets_rejects_links_outside_page_plan(self):
        valid_link = {"targetSlideIndex": 1, "targetStep": 0, "x": 1, "y": 2, "w": 3, "h": 4}
        page_map = {(0, 0): 0, (1, 0): 1}

        self.assertEqual([[valid_link], []], validate_export_link_targets([[valid_link], []], page_map))

        malformed_cases = [
            [[{**valid_link, "targetSlideIndex": 2}]],
            [[{**valid_link, "targetStep": 1}]],
            [[valid_link]],
        ]
        for page_links in malformed_cases:
            with self.subTest(page_links=page_links):
                with self.assertRaises(ValueError):
                    validate_export_link_targets(page_links, page_map)

    def test_normalized_pdf_links_clips_rectangles_to_slide_bounds(self):
        links = normalized_pdf_links(
            [
                [
                    {"targetSlideIndex": 1, "targetStep": 0, "x": -10, "y": -20, "w": 40, "h": 60},
                    {"targetSlideIndex": 1, "targetStep": 0, "x": 1270, "y": 710, "w": 30, "h": 30},
                    {"targetSlideIndex": 1, "targetStep": 0, "x": -50, "y": 10, "w": 20, "h": 20},
                ]
            ],
            {(0, 0): 0, (1, 0): 1},
        )

        self.assertEqual(
            [
                (0, 1, {"x": 0.0, "y": 0.0, "w": 30.0, "h": 40.0}),
                (0, 1, {"x": 1270.0, "y": 710.0, "w": 10.0, "h": 10.0}),
            ],
            links,
        )

    def test_add_pdf_links_keeps_existing_pdf_when_annotation_write_fails(self):
        from pypdf import PdfReader, PdfWriter

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "deck.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=1280, height=720)
            writer.add_blank_page(width=1280, height=720)
            with pdf.open("wb") as handle:
                writer.write(handle)
            original = pdf.read_bytes()

            def fail_after_partial_write(self, handle):
                handle.write(b"partial")
                raise OSError("write failed")

            with patch("pypdf.PdfWriter.write", fail_after_partial_write):
                with self.assertRaises(OSError):
                    add_pdf_links(
                        pdf,
                        [[{"targetSlideIndex": 1, "targetStep": 0, "x": 10, "y": 20, "w": 30, "h": 40}]],
                        {(0, 0): 0, (1, 0): 1},
                    )

            self.assertEqual(original, pdf.read_bytes())
            self.assertEqual(2, len(PdfReader(str(pdf)).pages))

    def test_add_pdf_links_keeps_existing_pdf_when_annotation_write_is_corrupt(self):
        from pypdf import PdfReader, PdfWriter

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "deck.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=1280, height=720)
            writer.add_blank_page(width=1280, height=720)
            with pdf.open("wb") as handle:
                writer.write(handle)
            original = pdf.read_bytes()

            def write_corrupt_pdf(self, handle):
                handle.write(b"not a pdf")

            with patch("pypdf.PdfWriter.write", write_corrupt_pdf):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(Exception):
                        add_pdf_links(
                            pdf,
                            [[{"targetSlideIndex": 1, "targetStep": 0, "x": 10, "y": 20, "w": 30, "h": 40}]],
                            {(0, 0): 0, (1, 0): 1},
                        )

            self.assertEqual(original, pdf.read_bytes())
            self.assertEqual(2, len(PdfReader(str(pdf)).pages))

    def test_step_export_writes_initial_raster_pdf_atomically(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def export_pdf_steps")
        block = source[start : source.index("\ndef add_pdf_links", start)]
        helper_start = source.index("def write_stepped_pdf_atomically")
        helper_block = source[helper_start : source.index("\n\nasync def export_pdf_flat", helper_start)]

        self.assertIn("def write_raster_pdf_atomically", source)
        self.assertIn("write_stepped_pdf_atomically(output_path, screenshots, page_links, page_map)", block)
        self.assertIn("write_raster_pdf_atomically(tmp_path, screenshots)", helper_block)
        self.assertNotIn("first.save(\n            str(output_path)", block)

    def test_stepped_export_keeps_existing_output_when_link_validation_fails(self):
        from PIL import Image
        from pypdf import PdfReader, PdfWriter

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "deck.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=1280, height=720)
            with output.open("wb") as handle:
                writer.write(handle)
            original = output.read_bytes()

            image = Image.new("RGB", (16, 9), "white")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            bad_link = {"targetSlideIndex": 2, "targetStep": 0, "x": 1, "y": 2, "w": 3, "h": 4}

            with self.assertRaises(ValueError):
                write_stepped_pdf_atomically(
                    output,
                    [buffer.getvalue()],
                    [[bad_link]],
                    {(0, 0): 0},
                )

            self.assertEqual(original, output.read_bytes())
            self.assertEqual(1, len(PdfReader(str(output)).pages))

    def test_stepped_export_rejects_empty_capture_set_without_touching_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "deck.pdf"
            output.write_bytes(b"previous pdf")

            with self.assertRaises(RuntimeError) as ctx:
                write_stepped_pdf_atomically(output, [], [], {})

            self.assertIn("No PDF pages were captured", str(ctx.exception))
            self.assertEqual(b"previous pdf", output.read_bytes())

    def test_parse_args_rejects_unknown_export_options(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            parse_args(["examples/kitchen-sink", "--scale", "0"])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("unrecognized arguments: --scale 0", stderr.getvalue())

    def test_main_reports_output_directory_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            output_dir = Path(tmp) / "existing-output-dir"
            output_dir.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                main([str(deck), "--output", str(output_dir)])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("Output path is a directory", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_index_html_directory_without_starting_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            (deck_dir / "index.html").mkdir()
            output = Path(tmp) / "out.pdf"
            stderr = io.StringIO()
            with patch("export_pdf.start_server") as start_server:
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main([str(deck_dir), "--output", str(output)])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("is a directory", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        start_server.assert_not_called()

    def test_main_reports_non_html_input_without_starting_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "deck.css"
            deck.write_text("body {}", encoding="utf-8")
            output = Path(tmp) / "out.pdf"
            stderr = io.StringIO()
            with patch("export_pdf.start_server") as start_server:
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main([str(deck), "--output", str(output)])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("is not an HTML file", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        start_server.assert_not_called()

    def test_main_reports_server_start_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            output = Path(tmp) / "out.pdf"
            stderr = io.StringIO()
            with patch("export_pdf.start_server", side_effect=OSError("bind failed")):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main([str(deck), "--output", str(output)])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("bind failed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_playwright_timeout_without_traceback(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        async def fail_export(*_args, **_kwargs):
            raise PlaywrightTimeoutError("export timed out")

        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            output = Path(tmp) / "out.pdf"
            stderr = io.StringIO()
            with patch("export_pdf.export_pdf", side_effect=fail_export):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main([str(deck), "--output", str(output)])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("export timed out", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_missing_runtime_dependency_without_traceback(self):
        async def fail_export(*_args, **_kwargs):
            raise ImportError("No module named 'PIL'")

        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            output = Path(tmp) / "out.pdf"
            stderr = io.StringIO()
            with patch("export_pdf.export_pdf", side_effect=fail_export):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main([str(deck), "--output", str(output)])

        self.assertEqual(2, caught.exception.code)
        self.assertIn("No module named 'PIL'", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_validate_export_info_rejects_empty_or_malformed_page_plan(self):
        with self.assertRaises(ValueError):
            validate_export_info([])

        with self.assertRaises(ValueError):
            validate_export_info([{"index": 0}])

        with self.assertRaises(ValueError):
            validate_export_info([{"index": 0, "maxStep": -1}])

        with self.assertRaises(ValueError):
            validate_export_info([{"index": True, "maxStep": 0}])

        with self.assertRaises(ValueError):
            validate_export_info([{"index": 0, "maxStep": False}])

        self.assertEqual(
            [(0, 0), (0, 1), (0, 2)],
            validate_export_info([{"index": 0, "maxStep": 2}]),
        )

    def test_validate_export_info_rejects_duplicate_page_keys(self):
        with self.assertRaises(ValueError):
            validate_export_info([
                {"index": 2, "maxStep": 1},
                {"index": 2, "maxStep": 0},
            ])

    def test_validate_export_info_rejects_skipped_or_reordered_slide_indexes(self):
        malformed_cases = [
            [{"index": 1, "maxStep": 0}],
            [{"index": 0, "maxStep": 0}, {"index": 2, "maxStep": 0}],
            [{"index": 1, "maxStep": 0}, {"index": 0, "maxStep": 0}],
        ]
        for slide_info in malformed_cases:
            with self.subTest(slide_info=slide_info):
                with self.assertRaises(ValueError):
                    validate_export_info(slide_info)

    def test_validate_flat_slide_count_rejects_empty_or_invalid_counts(self):
        for count in (0, -1, None, "3", True):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    validate_flat_slide_count(count)

        self.assertEqual(3, validate_flat_slide_count(3))

    def test_validate_browser_deck_structure_rejects_missing_multiple_nested_or_empty_decks(self):
        invalid_cases = [
            ({}, "Invalid deck structure metadata"),
            ({"deckCount": 0, "nestedDeckCount": 0, "directSlideCount": 0}, "Expected exactly one .ws-deck container, found 0"),
            ({"deckCount": 2, "nestedDeckCount": 0, "directSlideCount": 2}, "Expected exactly one .ws-deck container, found 2"),
            ({"deckCount": 1, "nestedDeckCount": 1, "directSlideCount": 1}, ".ws-deck containers must not be nested"),
            ({"deckCount": 1, "nestedDeckCount": 0, "directSlideCount": 0}, "No exportable slides found"),
        ]

        for info, message in invalid_cases:
            with self.subTest(info=info):
                with self.assertRaises(ValueError) as ctx:
                    validate_browser_deck_structure(info)
                self.assertIn(message, str(ctx.exception))

        valid = {"deckCount": 1, "nestedDeckCount": 0, "directSlideCount": 3}
        self.assertEqual(valid, validate_browser_deck_structure(valid))

    def test_browser_exports_reject_bad_deck_structure_without_waiting_for_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            deck = temp_deck(tmp, '<section class="slide">One</section><div class="ws-deck"></div>')
            for flat in (False, True):
                with self.subTest(flat=flat), \
                     patch("export_pdf.wait_for_deck_ready") as wait, \
                     self.assertRaisesRegex(ValueError, r"\.ws-deck container"):
                    asyncio.run(export_pdf(str(deck), str(Path(tmp) / "out.pdf"), flat=flat))
                wait.assert_not_called()

    def test_validate_pdf_output_rejects_missing_or_empty_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.pdf"
            with self.assertRaises(RuntimeError):
                validate_pdf_output(missing, expected_pages=1)

            empty = Path(tmp) / "empty.pdf"
            empty.write_bytes(b"")
            with self.assertRaises(RuntimeError):
                validate_pdf_output(empty, expected_pages=1)

    def test_validate_pdf_output_expands_home_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            output = fake_home / "deck.pdf"
            output.write_bytes(b"%PDF-1.4\n")
            with patch.dict("os.environ", {"HOME": str(fake_home)}):
                validate_pdf_output("~/deck.pdf")

    def test_flat_export_validates_written_pdf_page_count(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def export_pdf_flat")
        block = source[start : source.index("\n\nasync def export_pdf(", start)]
        helper_start = source.index("async def write_browser_pdf_atomically")
        helper_block = source[helper_start : source.index("\ndef validate_export_info", helper_start)]
        self.assertIn("write_browser_pdf_atomically(", block)
        self.assertIn("expected_pages=total", block)
        self.assertIn("validate_pdf_output(tmp_path, expected_pages=expected_pages)", helper_block)

    def test_flat_export_writes_browser_pdf_atomically(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def export_pdf_flat")
        block = source[start : source.index("\n\nasync def export_pdf(", start)]

        self.assertIn("async def write_browser_pdf_atomically", source)
        self.assertIn("write_browser_pdf_atomically(", block)
        self.assertIn("page_errors=page_errors", block)
        self.assertNotIn("page.pdf(\n                path=str(output_path)", block)

    def test_flat_export_keeps_existing_output_when_page_error_arrives_during_pdf_write(self):
        class FakePage:
            async def pdf(self, path, **_kwargs):
                Path(path).write_bytes(b"%PDF-1.4\nnew")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "deck.pdf"
            output.write_bytes(b"stale pdf")

            with patch("export_pdf.validate_pdf_output"):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(
                        write_browser_pdf_atomically(
                            FakePage(),
                            output,
                            expected_pages=1,
                            page_errors=[RuntimeError("late deck error")],
                        )
                    )

            self.assertIn("late deck error", str(ctx.exception))
            self.assertEqual(b"stale pdf", output.read_bytes())

    def test_browser_exports_wait_for_ws_ready(self):
        """Content added just before WS.ready resolves must be exported."""
        # Stands in for an engine whose WS.ready resolves late; the observer
        # stops a real C1 engine from announcing readiness first.
        late = """<script type="module">
const root = document.documentElement;
const hold = () => { if (!window.__lateDone) delete root.dataset.wsReady; };
new MutationObserver(hold).observe(root, { attributes: true, attributeFilter: ['data-ws-ready'] });
hold();
WS.ready = new Promise(resolve => setTimeout(() => {
    document.querySelector('#late').insertAdjacentHTML('beforeend', '<p>ARRIVED BEFORE READY</p>');
    window.__lateDone = true;
    root.dataset.wsReady = '1';
    resolve(WS);
}, 700));
</script>"""
        with tempfile.TemporaryDirectory() as tmp:
            deck = temp_deck(tmp, '<section class="slide" id="late"><h2>Late</h2></section>', head=late)
            flat = Path(tmp) / "flat.pdf"
            asyncio.run(export_pdf(str(deck), str(flat), flat=True))
            self.assertIn(squash("ARRIVED BEFORE READY"), pdf_texts(flat)[0])

    def test_step_export_validates_final_pdf_page_count_after_link_annotation(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        start = source.index("def write_stepped_pdf_atomically")
        block = source[start : source.index("\n\nasync def export_pdf_flat", start)]
        self.assertIn("validate_export_link_targets(page_links, page_map)", block)
        self.assertIn("add_pdf_links(tmp_path, page_links, page_map)", block)
        self.assertIn("validate_pdf_output(tmp_path, expected_pages=len(screenshots))", block)
        self.assertLess(
            block.index("add_pdf_links(tmp_path, page_links, page_map)"),
            block.index("validate_pdf_output(tmp_path, expected_pages=len(screenshots))"),
        )

    def test_browser_exports_close_chromium_on_failures(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        for name in ("export_pdf_steps", "export_pdf_flat"):
            start = source.index(f"async def {name}")
            end = source.find("\n\nasync def ", start + 1)
            if end < 0:
                end = source.find("\n\ndef ", start + 1)
            block = source[start:end]
            self.assertIn("try:", block)
            self.assertIn("finally:", block)
            self.assertIn("await browser.close()", block)

    def test_browser_page_errors_fail_pdf_exports_without_waiting_out_the_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No engine, so readiness never arrives: only the page error can end the wait early.
            deck = temp_deck(tmp, '<section class="slide">One</section>', head="""<script>
setTimeout(() => { throw new Error('deck script exploded'); }, 200);
</script>""", engine=False)
            output = Path(tmp) / "out.pdf"
            for flat in (False, True):
                with self.subTest(flat=flat):
                    started = time.monotonic()
                    with self.assertRaisesRegex(RuntimeError, "Deck JavaScript error: .*deck script exploded"):
                        asyncio.run(export_pdf(str(deck), str(output), flat=flat))
                    self.assertLess(time.monotonic() - started, 15)
                    self.assertFalse(output.exists())

    def test_export_server_socket_is_closed_after_shutdown(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        start = source.index("\nasync def export_pdf(")
        block = source[start : source.index("\ndef parse_args", start)]
        self.assertIn("httpd.shutdown()", block)
        self.assertIn("httpd.server_close()", block)

    def test_step_export_verifies_navigation_before_capture(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('try { WS.goToSlide', source)
        self.assertIn("verifyExportNavigation", source)
        self.assertIn("Export navigation mismatch", source)

    def test_step_export_validates_link_metadata_before_appending(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "export_pdf.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def export_pdf_steps")
        block = source[start : source.index("\n\ndef add_pdf_links", start)]
        self.assertIn("validate_export_links", block)
        self.assertIn("page_links.append(validate_export_links(links))", block)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import asyncio
import contextlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from check_overflow import (  # noqa: E402
    build_deck_url,
    check_overflow,
    checked_overflow_results,
    find_webslides_root,
    main,
    pick_culprits,
    resolve_deck_path,
    validate_browser_deck_structure,
)

FIXTURE = REPO / "tests" / "fixtures" / "tools-deck" / "index.html"


def run_json(deck, *flags):
    """Run check_overflow --json in-process; return (payload, exit code)."""
    stdout = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(stdout):
        try:
            asyncio.run(check_overflow(str(deck), json_out=True, include_appendix="--all" in flags,
                                       strict="--strict" in flags))
        except SystemExit as exc:
            code = exc.code
    return json.loads(stdout.getvalue()), code


def temp_deck(tmp, body):
    root = Path(tmp)
    (root / "engine").symlink_to(REPO / "engine", target_is_directory=True)
    path = root / "deck" / "index.html"
    path.parent.mkdir()
    path.write_text(
        """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="../engine/webslides.css">
<script defer src="../engine/webslides.js"></script>
</head><body><div class="ws-deck">""" + body + "</div></body></html>",
        encoding="utf-8",
    )
    return path


class CheckOverflowTests(unittest.TestCase):
    def test_build_deck_url_escapes_spaces(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            deck = root / "Talk With Spaces" / "index.html"
            deck.parent.mkdir()
            deck.write_text("", encoding="utf-8")

            self.assertEqual(
                "http://127.0.0.1:3333/Talk%20With%20Spaces/index.html",
                build_deck_url(deck, root, 3333),
            )

    def test_resolve_deck_path_accepts_directory(self):
        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            index = deck_dir / "index.html"
            index.write_text("", encoding="utf-8")

            self.assertEqual(index.resolve(), resolve_deck_path(str(deck_dir)))

    def test_resolve_deck_path_falls_back_to_repo_relative_deck_from_other_cwd(self):
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

    def test_find_webslides_root_falls_back_to_deck_dir(self):
        with TemporaryDirectory() as tmp:
            deck = Path(tmp) / "standalone" / "index.html"
            deck.parent.mkdir()
            deck.write_text("", encoding="utf-8")

            self.assertEqual(deck.parent, find_webslides_root(deck))

    def test_find_webslides_root_walks_deep_project_paths(self):
        with TemporaryDirectory() as tmp:
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

    def test_step_counts_come_from_the_engine(self):
        payload, code = run_json(FIXTURE, "--all")
        self.assertEqual(0, code)
        steps = {s["index"]: s["maxStep"] for s in payload["slides"]}
        self.assertEqual(3, steps[1])  # ranged reveals; the stale data-steps="5" is ignored
        self.assertEqual(3, steps[2])  # steps inside the inlined SVG
        self.assertEqual(2, steps[3])  # rows of the external table

    def test_reveal_attribute_edge_cases_follow_the_engine(self):
        with TemporaryDirectory() as tmp:
            deck = temp_deck(tmp, """
<section class="slide"><p data-class="is:open:2">colon in the class name</p></section>
<section class="slide"><p data-step="">empty step means one</p><p data-class=":3">no class name</p></section>
<section class="slide"><p data-step="2-x">malformed range</p></section>""")
            payload, _code = run_json(deck)
        self.assertEqual([2, 1, 0], [s["maxStep"] for s in payload["slides"]])

    def test_overflow_at_a_later_step_is_reported_with_that_step(self):
        with TemporaryDirectory() as tmp:
            deck = temp_deck(tmp, """
<section class="slide"><h2>Grows</h2><div data-step="2" style="height: 900px">tall</div></section>""")
            payload, code = run_json(deck, "--strict")
        [slide] = payload["slides"]
        self.assertEqual(1, code)
        self.assertGreater(slide["vOverflow"], 0)
        self.assertEqual(2, slide["maxStep"])

    def test_failed_table_load_fails_instead_of_passing_silently(self):
        with TemporaryDirectory() as tmp:
            deck = temp_deck(tmp, """
<section class="slide"><div class="table-container" data-table-src="tables/missing.html"></div></section>""")
            payload, code = run_json(deck)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
                asyncio.run(check_overflow(str(deck)))
        self.assertEqual(1, code)
        self.assertEqual(["table"], [e["type"] for e in payload["loadErrors"]])
        self.assertEqual(1, caught.exception.code)
        self.assertIn("failed to load", stdout.getvalue())
        self.assertIn("missing.html", stdout.getvalue())

    def test_checked_overflow_results_rejects_empty_checked_set(self):
        with self.assertRaises(ValueError) as ctx:
            checked_overflow_results([{
                "index": 0,
                "maxStep": 0,
                "step": 0,
                "title": "Appendix",
                "isAppendix": True,
                "mainNumber": None,
                "vOverflow": 0,
                "hOverflow": 0,
                "maxBottom": 500,
                "maxRight": 1000,
                "items": [],
            }], include_appendix=False)

        self.assertIn("No slides were checked", str(ctx.exception))

    def test_checked_overflow_results_rejects_malformed_browser_results(self):
        malformed_cases = [
            None,
            "not a list",
            [None],
            [{"isAppendix": False}],
            [{
                "isAppendix": False,
                "vOverflow": 0,
                "hOverflow": 0,
                "items": [],
            }],
        ]

        for results in malformed_cases:
            with self.subTest(results=results):
                with self.assertRaises(ValueError):
                    checked_overflow_results(results, include_appendix=False)

    def test_checked_overflow_results_rejects_negative_browser_measurements(self):
        valid = {
            "index": 0,
            "maxStep": 0,
            "step": 0,
            "title": "Slide",
            "isAppendix": False,
            "mainNumber": "1",
            "vOverflow": 0,
            "hOverflow": 0,
            "maxBottom": 500,
            "maxRight": 1000,
            "items": [],
        }

        for field in ("index", "maxStep", "step", "vOverflow", "hOverflow", "maxBottom", "maxRight"):
            result = dict(valid)
            result[field] = -1
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    checked_overflow_results([result], include_appendix=False)

    def test_checked_overflow_results_rejects_step_above_max_step(self):
        result = {
            "index": 0,
            "maxStep": 1,
            "step": 2,
            "title": "Slide",
            "isAppendix": False,
            "mainNumber": "1",
            "vOverflow": 0,
            "hOverflow": 0,
            "maxBottom": 500,
            "maxRight": 1000,
            "items": [],
        }

        with self.assertRaises(ValueError) as ctx:
            checked_overflow_results([result], include_appendix=False)

        self.assertIn("step exceeds maxStep", str(ctx.exception))

    def test_checked_overflow_results_rejects_inconsistent_overflow_measurements(self):
        valid = {
            "index": 0,
            "maxStep": 0,
            "step": 0,
            "title": "Slide",
            "isAppendix": False,
            "mainNumber": "1",
            "vOverflow": 0,
            "hOverflow": 0,
            "maxBottom": 500,
            "maxRight": 1000,
            "items": [],
        }
        malformed_cases = [
            dict(valid, maxBottom=721, vOverflow=0),
            dict(valid, maxRight=1281, hOverflow=0),
            dict(valid, maxBottom=500, vOverflow=1),
            dict(valid, maxRight=1000, hOverflow=1),
        ]

        for result in malformed_cases:
            with self.subTest(result=result):
                with self.assertRaises(ValueError) as ctx:
                    checked_overflow_results([result], include_appendix=False)
                self.assertIn("inconsistent overflow", str(ctx.exception))

    def test_checked_overflow_results_rejects_skipped_or_reordered_indexes(self):
        def result(index):
            return {
                "index": index,
                "maxStep": 0,
                "step": 0,
                "title": f"Slide {index}",
                "isAppendix": False,
                "mainNumber": str(index + 1),
                "vOverflow": 0,
                "hOverflow": 0,
                "maxBottom": 500,
                "maxRight": 1000,
                "items": [],
            }

        malformed_cases = [
            [result(1)],
            [result(0), result(2)],
            [result(1), result(0)],
        ]

        for results in malformed_cases:
            with self.subTest(results=results):
                with self.assertRaises(ValueError):
                    checked_overflow_results(results, include_appendix=False)

    def test_checked_overflow_results_rejects_malformed_culprit_items(self):
        valid = {
            "index": 0,
            "maxStep": 0,
            "step": 0,
            "title": "Slide",
            "isAppendix": False,
            "mainNumber": "1",
            "vOverflow": 10,
            "hOverflow": 0,
            "maxBottom": 730,
            "maxRight": 1000,
            "items": [],
        }
        malformed_items = [
            None,
            {"tag": "p"},
            {"tag": "p", "cls": "", "id": "", "text": "", "bottom": 730, "right": 1000, "bOver": -1, "rOver": 0, "childCount": 0, "chain": "p"},
            {"tag": "p", "cls": "", "id": "", "text": "", "bottom": 730, "right": 1000, "bOver": 10, "rOver": 0, "childCount": "0", "chain": "p"},
        ]

        for item in malformed_items:
            result = dict(valid)
            result["items"] = [item]
            with self.subTest(item=item):
                with self.assertRaises(ValueError):
                    checked_overflow_results([result], include_appendix=False)

    def test_checked_overflow_results_keeps_appendix_when_requested(self):
        results = [{
            "index": 0,
            "maxStep": 0,
            "step": 0,
            "title": "Appendix",
            "isAppendix": True,
            "mainNumber": None,
            "vOverflow": 0,
            "hOverflow": 0,
            "maxBottom": 500,
            "maxRight": 1000,
            "items": [],
        }]

        self.assertEqual(results, checked_overflow_results(results, include_appendix=True))

    def test_pick_culprits_keeps_distinct_empty_text_elements(self):
        first = {
            "tag": "img",
            "cls": "plot",
            "id": "figure-a",
            "text": "",
            "bottom": 760,
            "right": 1200,
            "bOver": 40,
            "rOver": 0,
            "childCount": 0,
            "chain": "figure > img.plot",
        }
        second = {
            "tag": "img",
            "cls": "plot",
            "id": "figure-b",
            "text": "",
            "bottom": 750,
            "right": 1200,
            "bOver": 30,
            "rOver": 0,
            "childCount": 0,
            "chain": "figure > img.plot",
        }

        self.assertEqual([first, second], pick_culprits([first, second], max_items=3))

    def test_validate_browser_deck_structure_rejects_missing_multiple_nested_or_empty_decks(self):
        invalid_cases = [
            ({}, "Invalid deck structure metadata"),
            ({"deckCount": 0, "nestedDeckCount": 0, "directSlideCount": 0}, "Expected exactly one .ws-deck container, found 0"),
            ({"deckCount": 2, "nestedDeckCount": 0, "directSlideCount": 2}, "Expected exactly one .ws-deck container, found 2"),
            ({"deckCount": 1, "nestedDeckCount": 1, "directSlideCount": 1}, ".ws-deck containers must not be nested"),
            ({"deckCount": 1, "nestedDeckCount": 0, "directSlideCount": 0}, "No slides were checked"),
        ]

        for info, message in invalid_cases:
            with self.subTest(info=info):
                with self.assertRaises(ValueError) as ctx:
                    validate_browser_deck_structure(info)
                self.assertIn(message, str(ctx.exception))

        valid = {"deckCount": 1, "nestedDeckCount": 0, "directSlideCount": 3}
        self.assertEqual(valid, validate_browser_deck_structure(valid))

    def test_overflow_check_validates_deck_structure_before_waiting_for_runtime_ready(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "check_overflow.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def check_overflow")
        block = source[start : source.index("\ndef main", start)]

        structure_pos = block.index("validate_browser_deck_structure")
        wait_pos = block.index("await wait_for_deck_ready")
        self.assertLess(structure_pos, wait_pos)

    def test_nested_deck_fails_fast_with_a_structure_error(self):
        with TemporaryDirectory() as tmp:
            deck = temp_deck(tmp, '<section class="slide">One</section><div class="ws-deck"></div>')
            with self.assertRaisesRegex(ValueError, r"\.ws-deck container"):
                asyncio.run(check_overflow(str(deck)))

    def test_browser_is_closed_even_when_overflow_check_fails(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "check_overflow.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def check_overflow")
        block = source[start : source.index("\ndef main", start)]
        self.assertIn("finally:", block)
        self.assertIn("await browser.close()", block)

    def test_browser_page_errors_fail_overflow_check(self):
        with TemporaryDirectory() as tmp:
            deck = temp_deck(tmp, """<section class="slide">One</section>
<script>setTimeout(() => { throw new Error("deck script exploded"); }, 0);</script>""")
            with self.assertRaisesRegex(RuntimeError, "Deck JavaScript error: .*deck script exploded"):
                asyncio.run(check_overflow(str(deck)))

    def test_overflow_check_reports_page_errors_before_runtime_wait_timeout(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "check_overflow.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def check_overflow")
        block = source[start : source.index("\ndef main", start)]

        self.assertIn("raise_for_page_errors(page_errors)", block)
        first_error_check = block.index("raise_for_page_errors(page_errors)")
        wait_pos = block.index("await wait_for_deck_ready(page, page_errors)")
        self.assertLess(first_error_check, wait_pos)

    def test_server_socket_is_closed_after_overflow_check(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "check_overflow.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def check_overflow")
        block = source[start : source.index("\ndef main", start)]
        self.assertIn("httpd.shutdown()", block)
        self.assertIn("httpd.server_close()", block)

    def test_overflow_check_uses_bound_server_port_instead_of_preallocating_port(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "check_overflow.py").read_text(
            encoding="utf-8"
        )
        start = source.index("async def check_overflow")
        block = source[start : source.index("\ndef main", start)]

        self.assertIn("httpd = start_server(root, 0)", block)
        self.assertIn("port = httpd.server_address[1]", block)
        self.assertNotIn("port = find_free_port()", block)
        self.assertNotIn("find_free_port", source)
        self.assertNotIn("import socket", source)

    def test_main_reports_server_start_failure_without_traceback(self):
        with TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["check_overflow.py", str(deck)]), \
                 patch("check_overflow.start_server", side_effect=OSError("bind failed")):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main()

        self.assertEqual(2, caught.exception.code)
        self.assertIn("bind failed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_playwright_timeout_without_traceback(self):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        async def fail_check(*_args, **_kwargs):
            raise PlaywrightTimeoutError("waiting for WS timed out")

        with TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["check_overflow.py", str(deck)]), \
                 patch("check_overflow.check_overflow", side_effect=fail_check):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main()

        self.assertEqual(2, caught.exception.code)
        self.assertIn("waiting for WS timed out", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_missing_runtime_dependency_without_traceback(self):
        async def fail_check(*_args, **_kwargs):
            raise ImportError("No module named 'playwright'")

        with TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text("<html><body><div class='ws-deck'></div></body></html>", encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["check_overflow.py", str(deck)]), \
                 patch("check_overflow.check_overflow", side_effect=fail_check):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main()

        self.assertEqual(2, caught.exception.code)
        self.assertIn("No module named 'playwright'", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_missing_deck_without_starting_server(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing" / "index.html"
            stderr = io.StringIO()
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["check_overflow.py", str(missing)]), \
                 patch("check_overflow.start_server") as start_server:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as caught:
                        main()

        self.assertEqual(2, caught.exception.code)
        self.assertIn("not found", stderr.getvalue())
        self.assertEqual("", stdout.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        start_server.assert_not_called()

    def test_main_reports_index_html_directory_without_starting_server(self):
        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            (deck_dir / "index.html").mkdir()
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["check_overflow.py", str(deck_dir)]), \
                 patch("check_overflow.start_server") as start_server:
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main()

        self.assertEqual(2, caught.exception.code)
        self.assertIn("is a directory", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        start_server.assert_not_called()

    def test_main_reports_non_html_input_without_starting_server(self):
        with TemporaryDirectory() as tmp:
            deck = Path(tmp) / "deck.css"
            deck.write_text("body {}", encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(sys, "argv", ["check_overflow.py", str(deck)]), \
                 patch("check_overflow.start_server") as start_server:
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main()

        self.assertEqual(2, caught.exception.code)
        self.assertIn("is not an HTML file", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        start_server.assert_not_called()


if __name__ == "__main__":
    unittest.main()

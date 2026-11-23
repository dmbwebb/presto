#!/usr/bin/env python3
"""Tests for tools/_common.py: interpreter re-exec, readiness, slide selection."""
import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import _common  # noqa: E402
from _common import parse_slide_selection  # noqa: E402

SYSTEM_PYTHON = Path("/usr/bin/python3")
PLAYWRIGHT_TOOLS = ["check_overflow", "check_style", "export_pdf", "screenshot_slide", "export_beamer"]
PURE_TOOLS = ["latex_table", "get_slide", "validate_links", "serve", "pdf2svg"]


def system_python_lacks_playwright():
    if not SYSTEM_PYTHON.exists():
        return False
    probe = subprocess.run(
        [str(SYSTEM_PYTHON), "-c", "import importlib.util,sys; sys.exit(importlib.util.find_spec('playwright') is not None)"],
        capture_output=True,
    )
    return probe.returncode == 0


# Stands in for an engine whose WS.ready resolves late (700 ms), running %s
# just before it resolves. A module script runs after the deferred engine
# script; the observer stops a real C1 engine announcing readiness first.
LATE_READY_SCRIPT = """<script type="module">
const root = document.documentElement;
const hold = () => { if (!window.__lateDone) delete root.dataset.wsReady; };
new MutationObserver(hold).observe(root, { attributes: true, attributeFilter: ['data-ws-ready'] });
hold();
WS.ready = new Promise(resolve => setTimeout(() => {%s
    window.__lateDone = true;
    root.dataset.wsReady = '1';
    resolve(WS);
}, 700));
</script>"""


def deck(root, body, head=""):
    (root / "engine").symlink_to(REPO / "engine", target_is_directory=True)
    path = root / "deck" / "index.html"
    path.parent.mkdir()
    path.write_text(
        f"""<!DOCTYPE html><html><head>
<link rel="stylesheet" href="../engine/webslides.css">
<script defer src="../engine/webslides.js"></script>
{head}
</head><body><div class="ws-deck">{body}</div></body></html>""",
        encoding="utf-8",
    )
    return path


@unittest.skipUnless(system_python_lacks_playwright(), "needs a system python3 without Playwright")
class SystemPythonTests(unittest.TestCase):
    """`python3 tools/<tool>.py` must work under the 3.9 system interpreter."""

    def run_tool(self, tool, *args, env=None):
        return subprocess.run(
            [str(SYSTEM_PYTHON), str(REPO / "tools" / f"{tool}.py"), *args],
            capture_output=True, text=True, env=env, timeout=120,
        )

    @unittest.skipUnless(_common.EXPORT_PYTHON.exists(), "export venv missing")
    def test_playwright_tools_reexec_under_the_export_venv(self):
        for tool in PLAYWRIGHT_TOOLS:
            with self.subTest(tool=tool):
                result = self.run_tool(tool, "--help")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("usage:", result.stdout)

    @unittest.skipUnless(_common.EXPORT_PYTHON.exists(), "export venv missing")
    def test_reexecuted_tool_really_runs(self):
        result = self.run_tool("check_overflow", str(REPO / "tests" / "fixtures" / "tools-deck"), "--all", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(16, json.loads(result.stdout)["total"])

    def test_missing_venv_gives_a_clear_error(self):
        env = dict(os.environ, WEBSLIDES_EXPORT_PYTHON="/nonexistent/webslides-export/bin/python")
        result = self.run_tool("check_overflow", "--help", env=env)
        self.assertEqual(2, result.returncode)
        self.assertIn("playwright", result.stderr)
        self.assertIn("/nonexistent/webslides-export/bin/python (missing)", result.stderr)
        self.assertIn("Rebuild it with", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_pure_python_tools_run_under_python39(self):
        for tool in PURE_TOOLS:
            with self.subTest(tool=tool):
                env = dict(os.environ, WEBSLIDES_EXPORT_PYTHON="/nonexistent/python")
                result = self.run_tool(tool, "--help", env=env)
                self.assertEqual(0, result.returncode, result.stderr)


class EnsureToolPythonTests(unittest.TestCase):
    def test_noop_when_modules_are_present(self):
        with patch("os.execve") as execve:
            _common.ensure_tool_python(("json",))
        execve.assert_not_called()

    def test_reexec_passes_script_and_arguments_and_guards_against_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "venv" / "bin" / "python"
            fake.parent.mkdir(parents=True)
            fake.write_text("#!/bin/sh\n", encoding="utf-8")
            fake.chmod(0o755)
            with patch.object(_common, "EXPORT_PYTHON", fake), \
                 patch.object(sys, "argv", ["tools/check_style.py", "deck", "--json"]), \
                 patch.dict(os.environ, {}, clear=False), \
                 patch("os.execve", side_effect=RuntimeError("exec")) as execve, \
                 self.assertRaisesRegex(RuntimeError, "exec"):
                os.environ.pop(_common.REEXEC_ENV, None)
                _common.ensure_tool_python(("surely_not_a_module_xyz",))
            path, argv, env = execve.call_args.args
            self.assertEqual(str(fake), path)
            self.assertEqual([str(fake), os.path.abspath("tools/check_style.py"), "deck", "--json"], argv)
            self.assertEqual("1", env[_common.REEXEC_ENV])

    def test_no_second_reexec_after_a_failed_one(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {_common.REEXEC_ENV: "1"}), patch("os.execve") as execve, \
             contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            _common.ensure_tool_python(("surely_not_a_module_xyz",))
        execve.assert_not_called()
        self.assertEqual(2, caught.exception.code)
        self.assertIn("surely_not_a_module_xyz", stderr.getvalue())


class SlideSelectionTests(unittest.TestCase):
    SLIDES = [
        {"index": 0, "id": "", "innerIds": []},
        {"index": 1, "id": "intro", "innerIds": ["fig-a"]},
        {"index": 2, "id": "", "innerIds": []},
        {"index": 3, "id": "app-x", "innerIds": []},
    ]

    def test_numbers_ranges_ids_and_all(self):
        self.assertEqual([0], parse_slide_selection(["1"], self.SLIDES))
        self.assertEqual([1, 2, 3], parse_slide_selection(["2-4"], self.SLIDES))
        self.assertEqual([3, 1], parse_slide_selection(["#app-x", "intro"], self.SLIDES))
        self.assertEqual([1], parse_slide_selection(["fig-a"], self.SLIDES))
        self.assertEqual([0, 1, 2, 3], parse_slide_selection(["all"], self.SLIDES))
        self.assertEqual([0, 2], parse_slide_selection(["1,3", "1"], self.SLIDES))

    def test_hyphenated_ids_are_ids_not_ranges(self):
        self.assertEqual([3], parse_slide_selection(["app-x"], self.SLIDES))

    def test_out_of_range_and_unknown_specs_fail(self):
        for spec in ("0", "5", "3-9", "nope"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                parse_slide_selection([spec], self.SLIDES)


class ReadinessTests(unittest.TestCase):
    def test_tools_wait_for_ws_ready_when_the_engine_provides_it(self):
        """Content added just before WS.ready resolves must be measured."""
        from check_overflow import check_overflow

        late = LATE_READY_SCRIPT % """
    const block = document.createElement('div');
    block.style.height = '1500px';
    block.textContent = 'late content';
    document.querySelector('#late').appendChild(block);"""
        with tempfile.TemporaryDirectory() as tmp:
            path = deck(Path(tmp), '<section class="slide" id="late"><h2>Late</h2></section>', head=late)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                asyncio.run(check_overflow(str(path), json_out=True))
        [slide] = json.loads(stdout.getvalue())["slides"]
        self.assertGreater(slide["vOverflow"], 0)

    def test_load_errors_fall_back_to_the_dom_on_older_engines(self):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as tmp:
            path = deck(Path(tmp), """
<section class="slide"><div class="table-container" data-table-src="tables/missing.html"></div></section>
<section class="slide"><img data-inline src="figures/missing.svg"></section>""")
            with _common.DeckServer(path) as served, sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    page = browser.new_page()
                    page.goto(served.url)
                    _common.wait_for_deck_ready_sync(page)
                    errors = page.evaluate(_common.LOAD_ERRORS_JS)
                finally:
                    browser.close()
        by_type = {e["type"]: e for e in errors}
        self.assertEqual({"table", "svg"}, set(by_type))
        self.assertEqual(0, by_type["table"]["slideIndex"])
        self.assertEqual(1, by_type["svg"]["slideIndex"])
        self.assertIn("missing.svg", by_type["svg"]["src"])


if __name__ == "__main__":
    unittest.main()

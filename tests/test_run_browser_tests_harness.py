#!/usr/bin/env python3
import contextlib
import io
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


RUN_TESTS = Path(__file__).resolve().parents[1] / "tests" / "run-tests.sh"
sys.path.insert(0, str(RUN_TESTS.parent))
from run_browser_tests import main, run_browser_test, run_layout_harness  # noqa: E402


class RunBrowserTestsHarnessTests(unittest.TestCase):
    def test_run_tests_delegates_to_playwright_gate(self):
        script = RUN_TESTS.read_text(encoding="utf-8")
        self.assertIn('python3 "$SCRIPT_DIR/run_browser_tests.py"', script)

    def test_playwright_gate_checks_browser_fixture_summaries(self):
        runner = RUN_TESTS.with_name("run_browser_tests.py").read_text(encoding="utf-8")
        self.assertIn("sync_playwright", runner)
        self.assertIn("run_browser_test", runner)
        self.assertIn("wait_for_result", runner)
        self.assertIn("PRESTO_TEST_RESULTS", runner)
        self.assertIn("MODE_TEST_RESULTS", runner)
        self.assertIn("TABLE_STEP_TEST_RESULTS", runner)
        self.assertIn("result_name", runner)
        self.assertIn("225/225 tests passed", runner)
        self.assertIn("/tests/engine-layout-tests.html", runner)
        self.assertIn("/tests/engine-layout-tests.html?inline-only", runner)
        self.assertIn("LAYOUT_TEST_RESULTS", runner)
        self.assertIn("8/8 passed", runner)
        self.assertIn("7/7 passed", runner)
        self.assertIn("6/6 passed", runner)
        self.assertIn("TABLE_STEP_TESTS: passed", runner)

    def test_playwright_gate_supports_expected_page_error_fixtures(self):
        runner = RUN_TESTS.with_name("run_browser_tests.py").read_text(encoding="utf-8")
        fixture = RUN_TESTS.parent / "malformed-deck-tests.html"

        self.assertTrue(fixture.exists())
        self.assertIn("Malformed deck structure: multiple decks", runner)
        self.assertIn("Malformed deck structure: missing deck", runner)
        self.assertIn("Malformed deck structure: nested deck", runner)
        self.assertIn("Malformed deck structure: empty deck", runner)
        self.assertIn("Malformed deck structure: non-section slide", runner)
        self.assertIn("/tests/malformed-deck-tests.html", runner)
        self.assertIn("/tests/malformed-deck-missing.html", runner)
        self.assertIn("/tests/malformed-deck-nested.html", runner)
        self.assertIn("/tests/malformed-deck-empty.html", runner)
        self.assertIn("/tests/malformed-deck-nonsection.html", runner)
        self.assertIn("expected_page_error", runner)
        self.assertIn("Expected page error", runner)
        self.assertIn("expected exactly one .ws-deck container", runner)
        self.assertIn(".ws-deck containers must not be nested", runner)
        self.assertIn(".ws-deck contains no direct .slide sections", runner)

    def test_browser_gate_runs_layout_geometry_harness(self):
        runner = RUN_TESTS.with_name("run_browser_tests.py").read_text(encoding="utf-8")
        self.assertIn("run_layout_harness", runner)
        self.assertIn("chrome-edit-preview-layout.js", runner)
        self.assertIn("WEBSLIDES_LAYOUT_MODE", runner)
        self.assertIn("webslides-edit-layout.png", runner)
        self.assertIn("webslides-preview-layout.png", runner)

    def test_layout_geometry_harness_handles_chrome_spawn_errors(self):
        script = (RUN_TESTS.parent / "chrome-edit-preview-layout.js").read_text(encoding="utf-8")

        self.assertIn("chrome.on('error'", script)
        self.assertIn("Chrome failed to launch", script)

    def test_layout_geometry_harness_fails_on_uncaught_runtime_exceptions(self):
        script = (RUN_TESTS.parent / "chrome-edit-preview-layout.js").read_text(encoding="utf-8")

        self.assertIn("on(method, handler)", script)
        self.assertIn("Runtime.exceptionThrown", script)
        self.assertIn("runtimeErrors.push", script)
        self.assertIn("Uncaught runtime exception", script)

    def test_layout_geometry_harness_validates_viewport_env_values(self):
        script = (RUN_TESTS.parent / "chrome-edit-preview-layout.js").read_text(encoding="utf-8")

        self.assertIn("parsePositiveIntegerEnv", script)
        self.assertIn("must be a positive integer", script)
        self.assertIn("WEBSLIDES_TEST_WIDTH", script)
        self.assertIn("WEBSLIDES_TEST_HEIGHT", script)

    def test_layout_geometry_harness_reports_bad_viewport_env_without_traceback(self):
        env = os.environ.copy()
        env["WEBSLIDES_TEST_WIDTH"] = "bad"
        result = subprocess.run(
            ["node", str(RUN_TESTS.parent / "chrome-edit-preview-layout.js")],
            cwd=RUN_TESTS.parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(1, result.returncode)
        self.assertIn('FAIL WEBSLIDES_TEST_WIDTH must be a positive integer, got "bad"', result.stderr)
        self.assertNotIn("    at ", result.stderr)

    def test_layout_geometry_harness_cleans_partial_startup_resources(self):
        script = (RUN_TESTS.parent / "chrome-edit-preview-layout.js").read_text(encoding="utf-8")

        self.assertIn("let server;", script)
        self.assertIn("let userDataDir;", script)
        self.assertIn("let chrome;", script)
        self.assertLess(script.index("let server;"), script.index("server = await startServer()"))
        self.assertLess(script.index("let userDataDir;"), script.index("userDataDir = await fs.mkdtemp"))
        self.assertLess(script.index("let chrome;"), script.index("chrome = spawn("))
        self.assertIn("if (chrome) chrome.kill('SIGKILL');", script)
        self.assertIn("if (server) await new Promise((resolve) => server.close(resolve));", script)
        self.assertIn("if (userDataDir) spawnSync('trash', [userDataDir]", script)

    def test_browser_gate_closes_browser_in_finally(self):
        runner = RUN_TESTS.with_name("run_browser_tests.py").read_text(encoding="utf-8")
        start = runner.index("with sync_playwright() as playwright:")
        block = runner[start : runner.index("for layout_mode", start)]

        self.assertIn("browser = None", runner)
        self.assertIn("finally:", block)
        self.assertIn("if browser is not None:", block)
        self.assertIn("browser.close()", block)

    def test_browser_gate_fails_on_uncaught_page_errors(self):
        runner = RUN_TESTS.with_name("run_browser_tests.py").read_text(encoding="utf-8")
        start = runner.index("def run_browser_test")
        block = runner[start : runner.index("\ndef run_layout_harness", start)]

        self.assertIn("page_errors = []", block)
        self.assertIn("page.on(\"pageerror\"", block)
        self.assertIn("page_errors.append", block)
        self.assertIn("Uncaught page error", block)
        self.assertIn("raise AssertionError", block)

    def test_browser_gate_reports_page_error_before_wait_timeout(self):
        class FakePage:
            def __init__(self):
                self.handlers = {}

            def on(self, event, handler):
                self.handlers[event] = handler

            def goto(self, _url, wait_until=None):
                self.handlers["pageerror"](RuntimeError("fixture exploded"))

            def wait_for_function(self, *_args, **_kwargs):
                raise AssertionError("waited for result object")

        test = {
            "label": "Broken fixture",
            "path": "/tests/broken.html",
            "result_name": "BROKEN_RESULTS",
            "expected_total": 1,
            "expected_summary": "1/1 passed",
        }

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(AssertionError) as ctx:
            run_browser_test(FakePage(), "http://127.0.0.1:3333", test)

        self.assertIn("Uncaught page error", str(ctx.exception))
        self.assertIn("fixture exploded", str(ctx.exception))
        self.assertNotIn("waited for result object", str(ctx.exception))

    def test_browser_gate_waits_briefly_for_expected_page_errors(self):
        class FakePage:
            def __init__(self):
                self.handlers = {}
                self.wait_calls = 0

            def on(self, event, handler):
                self.handlers[event] = handler

            def goto(self, _url, wait_until=None):
                pass

            def wait_for_timeout(self, _ms):
                self.wait_calls += 1
                self.handlers["pageerror"](RuntimeError("WebSlides deck structure error: expected exactly one deck"))

        test = {
            "label": "Malformed deck",
            "path": "/tests/malformed-deck-tests.html",
            "expected_page_error": "WebSlides deck structure error",
        }

        stdout = io.StringIO()
        page = FakePage()
        with contextlib.redirect_stdout(stdout):
            run_browser_test(page, "http://127.0.0.1:3333", test)

        self.assertGreaterEqual(page.wait_calls, 1)
        self.assertIn("expected page error", stdout.getvalue())

    def test_layout_harness_reports_missing_node_cleanly(self):
        with patch("run_browser_tests.subprocess.run", side_effect=FileNotFoundError("node")):
            with self.assertRaises(AssertionError) as ctx:
                run_layout_harness("edit", "missing-node.png")

        self.assertIn("could not launch layout harness", str(ctx.exception))
        self.assertIn("node", str(ctx.exception))

    def test_layout_harness_rejects_stale_screenshot(self):
        with TemporaryDirectory() as tmp:
            stale_screenshot = Path(tmp) / "webslides-stale-layout-test.png"
            stale_screenshot.write_bytes(b"stale")
            completed = subprocess.CompletedProcess(
                args=["node"],
                returncode=0,
                stdout="PASS content:\nPASS section-header:\n",
                stderr="",
            )

            def run_without_new_screenshot(args, **_kwargs):
                if args[0] == "trash":
                    stale_screenshot.rename(stale_screenshot.with_suffix(".trashed"))
                    return subprocess.CompletedProcess(args=args, returncode=0)
                return completed

            with patch("run_browser_tests.subprocess.run", side_effect=run_without_new_screenshot):
                with self.assertRaises(AssertionError) as ctx:
                    run_layout_harness("edit", str(stale_screenshot))

        self.assertIn("did not create screenshot", str(ctx.exception))

    def test_layout_harness_rejects_corrupt_screenshot(self):
        with TemporaryDirectory() as tmp:
            corrupt_screenshot = Path(tmp) / "webslides-corrupt-layout-test.png"

            def write_corrupt_screenshot(*_args, **_kwargs):
                corrupt_screenshot.write_bytes(b"not a png but not empty")
                return subprocess.CompletedProcess(
                    args=["node"],
                    returncode=0,
                    stdout="PASS content:\nPASS section-header:\n",
                    stderr="",
                )

            with patch("run_browser_tests.subprocess.run", side_effect=write_corrupt_screenshot):
                with self.assertRaises(AssertionError) as ctx:
                    run_layout_harness("edit", str(corrupt_screenshot))

        self.assertIn("not a PNG", str(ctx.exception))

    def test_layout_harness_rejects_future_dated_stale_screenshot(self):
        with TemporaryDirectory() as tmp:
            stale_screenshot = Path(tmp) / "webslides-future-stale-layout-test.png"
            stale_screenshot.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\rIHDR"
                b"\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00"
            )
            future = time.time() + 3600
            os.utime(stale_screenshot, (future, future))
            completed = subprocess.CompletedProcess(
                args=["node"],
                returncode=0,
                stdout="PASS content:\nPASS section-header:\n",
                stderr="",
            )

            def run_without_new_screenshot(args, **_kwargs):
                if args[0] == "trash":
                    stale_screenshot.rename(stale_screenshot.with_suffix(".trashed"))
                    return subprocess.CompletedProcess(args=args, returncode=0)
                return completed

            with patch("run_browser_tests.subprocess.run", side_effect=run_without_new_screenshot):
                with self.assertRaises(AssertionError) as ctx:
                    run_layout_harness("edit", str(stale_screenshot))

        self.assertIn("did not create screenshot", str(ctx.exception))

    def test_main_reports_server_start_failure_without_traceback(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"CHROME": str(RUN_TESTS)}), \
             patch("run_browser_tests.start_server", side_effect=OSError("bind failed")):
            with contextlib.redirect_stderr(stderr):
                result = main()

        self.assertEqual(1, result)
        self.assertIn("FAILED: could not start browser test server", stderr.getvalue())
        self.assertIn("bind failed", stderr.getvalue())

    def test_main_reports_out_of_range_port_without_traceback(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"CHROME": str(RUN_TESTS), "PORT": "70000"}):
            with contextlib.redirect_stderr(stderr):
                result = main()

        self.assertEqual(1, result)
        self.assertIn("FAILED: could not start browser test server", stderr.getvalue())
        self.assertIn("port must be between 0 and 65535", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_reports_non_numeric_port_without_traceback(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"CHROME": str(RUN_TESTS), "PORT": "abc"}):
            with contextlib.redirect_stderr(stderr):
                result = main()

        self.assertEqual(1, result)
        self.assertIn("FAILED: could not start browser test server", stderr.getvalue())
        self.assertIn("PORT must be an integer", stderr.getvalue())
        self.assertNotIn("invalid literal for int", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_browser_gate_handles_missing_playwright_import_cleanly(self):
        runner = RUN_TESTS.with_name("run_browser_tests.py").read_text(encoding="utf-8")

        self.assertIn("PLAYWRIGHT_IMPORT_ERROR", runner)
        self.assertIn("FAILED: could not import Playwright", runner)

    def test_main_reports_playwright_startup_failure_without_traceback(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"CHROME": str(RUN_TESTS)}), \
             patch("run_browser_tests.sync_playwright", side_effect=ImportError("No module named 'playwright'")):
            with contextlib.redirect_stderr(stderr):
                result = main()

        self.assertEqual(1, result)
        self.assertIn("FAILED: could not start Playwright", stderr.getvalue())
        self.assertIn("No module named 'playwright'", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

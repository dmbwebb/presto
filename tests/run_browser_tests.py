#!/usr/bin/env python3
import os
import subprocess
import sys
import time
import threading
import struct
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_IMPORT_ERROR = None
except ImportError as exc:
    PlaywrightError = PlaywrightTimeoutError = RuntimeError
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

TESTS = [
    {
        "label": "Engine API",
        "path": "/tests/engine-api-tests.html",
        "result_name": "PRESTO_TEST_RESULTS",
        "expected_total": 225,
        "expected_summary": "225/225 tests passed",
    },
    {
        "label": "Engine table layout",
        "path": "/tests/engine-layout-tests.html",
        "result_name": "LAYOUT_TEST_RESULTS",
        "expected_total": 39,
        "expected_summary": "39/39 passed",
    },
    {
        "label": "Engine table layout: inline tables only",
        "path": "/tests/engine-layout-tests.html?inline-only",
        "result_name": "LAYOUT_TEST_RESULTS",
        "expected_total": 24,
        "expected_summary": "24/24 passed",
    },
    {
        "label": "Mode system: preview",
        "path": "/tests/mode-tests.html?mode=preview",
        "result_name": "MODE_TEST_RESULTS",
        "expected_total": 8,
        "expected_summary": "8/8 passed",
    },
    {
        "label": "Mode system: audience",
        "path": "/tests/mode-tests.html?mode=audience",
        "result_name": "MODE_TEST_RESULTS",
        "expected_total": 7,
        "expected_summary": "7/7 passed",
    },
    {
        "label": "URL parameter navigation",
        "path": "/tests/mode-tests.html?slide=2&step=1",
        "result_name": "MODE_TEST_RESULTS",
        "expected_total": 6,
        "expected_summary": "6/6 passed",
    },
    {
        "label": "Invalid URL normalization",
        "path": "/tests/mode-tests.html?slide=abc&step=bad&mode=garbage",
        "result_name": "MODE_TEST_RESULTS",
        "expected_total": 6,
        "expected_summary": "6/6 passed",
    },
    {
        "label": "External table reveal state",
        "path": "/tests/table-step-tests.html",
        "result_name": "TABLE_STEP_TEST_RESULTS",
        "expected_total": None,
        "expected_summary": "TABLE_STEP_TESTS: passed",
    },
    {
        "label": "Step model: relative steps, fades, focus, morphs",
        "path": "/tests/step-model-tests.html",
        "result_name": "STEP_MODEL_TEST_RESULTS",
        "expected_total": 65,
        "expected_summary": "65/65 passed",
    },
    {
        "label": "Presto WYSIWYG editing",
        "path": "/tests/wysiwyg-tests.html",
        "result_name": "WYSIWYG_TEST_RESULTS",
        "expected_total": 387,
        "expected_summary": "387/387 passed",
        "timeout_ms": 20000,
    },
    {
        "label": "Presto WYSIWYG transaction regressions",
        "path": "/tests/wysiwyg-transaction-tests.html",
        "result_name": "WYSIWYG_TRANSACTION_RESULTS",
        "expected_total": 5,
        "expected_summary": "5/5 passed",
    },
    {
        "label": "Presto WYSIWYG alignment guides",
        "path": "/tests/wysiwyg-guides-tests.html",
        "result_name": "GUIDES_TEST_RESULTS",
        "expected_total": 81,
        "expected_summary": "81/81 passed",
    },
    {
        "label": "Presto visual editor redesign",
        "path": "/tests/wysiwyg-redesign-tests.html",
        "result_name": "WYSIWYG_REDESIGN_RESULTS",
        "expected_total": 178,
        "expected_summary": "178/178 passed",
        "timeout_ms": 20000,
    },
    {
        "label": "Presto edit chrome",
        "path": "/tests/edit-chrome-tests.html",
        "result_name": "EDIT_CHROME_TEST_RESULTS",
        "expected_total": 113,
        "expected_summary": "113/113 passed",
        "timeout_ms": 20000,
    },
    {
        "label": "Presto Decks page",
        "path": "/tests/decks-tests.html",
        "result_name": "DECKS_TEST_RESULTS",
        "expected_total": 122,
        "expected_summary": "122/122 passed",
        "timeout_ms": 20000,
    },
    {
        "label": "Malformed deck structure: multiple decks",
        "path": "/tests/malformed-deck-tests.html",
        "expected_page_error": "expected exactly one .ws-deck container",
    },
    {
        "label": "Malformed deck structure: missing deck",
        "path": "/tests/malformed-deck-missing.html",
        "expected_page_error": "expected exactly one .ws-deck container, found 0",
    },
    {
        "label": "Malformed deck structure: nested deck",
        "path": "/tests/malformed-deck-nested.html",
        "expected_page_error": ".ws-deck containers must not be nested",
    },
    {
        "label": "Malformed deck structure: empty deck",
        "path": "/tests/malformed-deck-empty.html",
        "expected_page_error": ".ws-deck contains no direct .slide sections",
    },
    {
        "label": "Malformed deck structure: non-section slide",
        "path": "/tests/malformed-deck-nonsection.html",
        "expected_page_error": ".ws-deck contains no direct .slide sections",
    },
]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


def start_server():
    port_text = os.environ.get("PORT")
    try:
        port = int(port_text) if port_text else 0
    except ValueError as exc:
        raise ValueError(f"PORT must be an integer, got {port_text!r}") from exc
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    handler = partial(QuietHandler, directory=str(PROJECT_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def wait_for_result(page, result_name, expected_total, timeout_ms=10000):
    page.wait_for_function(
        """
        ([name, expectedTotal]) => {
            const result = window[name];
            if (!result) return false;
            if (expectedTotal === null) return result.passed === true;
            return result.passed === expectedTotal &&
                result.total === expectedTotal &&
                result.failed === 0;
        }
        """,
        arg=[result_name, expected_total],
        timeout=timeout_ms,
    )


def tail_text(text, max_lines=40):
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def png_dimensions(path):
    with Path(path).open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        raise AssertionError(f"layout harness screenshot is not a PNG: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise AssertionError(f"layout harness screenshot has invalid dimensions: {path}")
    return width, height


def run_browser_test(page, base_url, test):
    label = test["label"]
    path = test["path"]
    url = f"{base_url}{path}"
    print(f"--- {label} ---")
    page_errors = []
    page.on("pageerror", lambda exc: page_errors.append(exc))
    page.goto(url, wait_until="load")

    expected_page_error = test.get("expected_page_error")
    if expected_page_error is not None:
        deadline = time.time() + 2
        while not page_errors and time.time() < deadline:
            page.wait_for_timeout(50)
        if not page_errors:
            raise AssertionError(f"Expected page error in {url}: {expected_page_error}")
        first_error = str(page_errors[0])
        if expected_page_error not in first_error:
            raise AssertionError(
                f"Expected page error containing {expected_page_error!r} in {url}, got: {first_error}"
            )
        print(f"  PASS: expected page error {expected_page_error!r}")
        return

    if page_errors:
        raise AssertionError(f"Uncaught page error in {url}: {page_errors[0]}")
    wait_for_result(page, test["result_name"], test["expected_total"],
                    timeout_ms=test.get("timeout_ms", 10000))
    if page_errors:
        raise AssertionError(f"Uncaught page error in {url}: {page_errors[0]}")
    expected_summary = test["expected_summary"]
    result = page.evaluate("name => window[name]", test["result_name"])
    body_text = page.locator("body").inner_text(timeout=1000)
    if expected_summary not in body_text:
        raise AssertionError(f"Expected summary text not found in {url}: {expected_summary}")
    if "✗" in body_text or " failed" in body_text.lower():
        raise AssertionError(f"Failure marker found in {url}\n{tail_text(body_text)}")
    print(f"  PASS: {expected_summary} {result}")


def run_layout_harness(layout_mode, screenshot_name):
    script = PROJECT_DIR / "tests" / "chrome-edit-preview-layout.js"
    screenshot_path = Path("/tmp") / screenshot_name
    env = os.environ.copy()
    env["WEBSLIDES_LAYOUT_MODE"] = layout_mode
    env["WEBSLIDES_TEST_SCREENSHOT"] = str(screenshot_path)
    if screenshot_path.exists():
        try:
            subprocess.run(
                ["trash", str(screenshot_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise AssertionError(
                f"{layout_mode} layout harness could not clear existing screenshot: {screenshot_path}: {exc}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise AssertionError(
                f"{layout_mode} layout harness could not clear existing screenshot: {screenshot_path}"
            ) from exc
        if screenshot_path.exists():
            raise AssertionError(
                f"{layout_mode} layout harness could not clear existing screenshot: {screenshot_path}"
            )
    started_at_ns = time.time_ns()
    try:
        result = subprocess.run(
            ["node", str(script)],
            cwd=PROJECT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        raise AssertionError(f"{layout_mode} layout harness could not launch layout harness: {exc}") from exc
    if result.returncode != 0:
        raise AssertionError(
            f"{layout_mode} layout harness failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    if "PASS content:" not in result.stdout or "PASS section-header:" not in result.stdout:
        raise AssertionError(f"{layout_mode} layout harness did not report expected geometry checks\n{result.stdout}")
    try:
        screenshot_stat = screenshot_path.stat()
    except OSError:
        raise AssertionError(f"{layout_mode} layout harness did not create screenshot: {screenshot_path}")
    if screenshot_stat.st_size == 0:
        raise AssertionError(f"{layout_mode} layout harness did not create screenshot: {screenshot_path}")
    if screenshot_stat.st_mtime_ns < started_at_ns:
        raise AssertionError(f"{layout_mode} layout harness did not create fresh screenshot: {screenshot_path}")
    png_dimensions(screenshot_path)
    print(f"  PASS: {layout_mode} layout geometry ({screenshot_path})")


def main():
    chrome = os.environ.get("CHROME", DEFAULT_CHROME)
    if not Path(chrome).exists():
        print(f"FAILED: Chrome executable not found at {chrome}", file=sys.stderr)
        return 1

    if PLAYWRIGHT_IMPORT_ERROR is not None:
        print(f"FAILED: could not import Playwright: {PLAYWRIGHT_IMPORT_ERROR}", file=sys.stderr)
        return 1

    try:
        server, port = start_server()
    except (OSError, ValueError) as exc:
        print(f"FAILED: could not start browser test server: {exc}", file=sys.stderr)
        return 1
    base_url = f"http://127.0.0.1:{port}"
    print("=== WebSlides Engine Browser Tests ===")
    print("")
    print(f"Server running on {base_url}")
    print("")

    passed = 0
    failed = 0
    browser = None
    try:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=chrome, headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 720})
                    for test in TESTS:
                        try:
                            run_browser_test(page, base_url, test)
                            passed += 1
                        except (AssertionError, PlaywrightError, PlaywrightTimeoutError) as exc:
                            failed += 1
                            print(f"  FAIL: {exc}")
                            try:
                                print(tail_text(page.locator("body").inner_text(timeout=1000)))
                            except PlaywrightError:
                                pass
                finally:
                    if browser is not None:
                        browser.close()
                        browser = None
        except (ImportError, PlaywrightError, PlaywrightTimeoutError, OSError) as exc:
            print(f"FAILED: could not start Playwright: {exc}", file=sys.stderr)
            return 1

        for layout_mode, screenshot_name in (
            ("edit", "webslides-edit-layout.png"),
            ("preview", "webslides-preview-layout.png"),
        ):
            print(f"--- Layout geometry: {layout_mode} ---")
            try:
                run_layout_harness(layout_mode, screenshot_name)
                passed += 1
            except (AssertionError, subprocess.SubprocessError) as exc:
                failed += 1
                print(f"  FAIL: {exc}")
    finally:
        server.shutdown()
        server.server_close()
        time.sleep(0.1)

    print("")
    print("=== Results ===")
    print(f"Passed: {passed} / {passed + failed}")
    if failed:
        print("FAILED")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

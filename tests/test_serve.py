#!/usr/bin/env python3
import contextlib
import io
import json
import os
import unittest
import http.server
import sys
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import serve  # noqa: E402
from serve import build_deck_url, find_webslides_root, main, maybe_open_browser, parse_args, scan_files, start_server  # noqa: E402


class ServeToolTests(unittest.TestCase):
    def test_find_webslides_root_uses_nearest_engine_ancestor(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine").mkdir()
            (root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck_dir = root / "decks" / "talk"
            deck_dir.mkdir(parents=True)

            self.assertEqual(root, find_webslides_root(deck_dir))

    def test_find_webslides_root_walks_deep_project_paths(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "webslides-project"
            (root / "engine").mkdir(parents=True)
            (root / "engine" / "webslides.js").write_text("// ok", encoding="utf-8")
            deck_dir = root
            for i in range(14):
                deck_dir = deck_dir / f"level-{i}"
            deck_dir.mkdir(parents=True)

            self.assertEqual(root, find_webslides_root(deck_dir))

    def test_find_webslides_root_falls_back_to_requested_external_dir(self):
        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "external-talk"
            deck_dir.mkdir()

            self.assertEqual(deck_dir, find_webslides_root(deck_dir))

    def test_start_server_uses_threading_http_server(self):
        with TemporaryDirectory() as tmp:
            server = start_server(Path(tmp), 0)
            try:
                self.assertIsInstance(server, http.server.ThreadingHTTPServer)
                self.assertTrue(server.daemon_threads)
                self.assertEqual("127.0.0.1", server.server_address[0])
            finally:
                server.server_close()

    def test_first_reload_poll_is_not_reported_as_a_change(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<html></html>", encoding="utf-8")
            serve._file_state = {}
            serve._changed = False

            server = start_server(root, 0)
            port = server.server_address[1]
            try:
                import threading

                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                response = urllib.request.urlopen(f"http://127.0.0.1:{port}/__ws_poll").read()

                self.assertEqual({"changed": False}, json.loads(response))
            finally:
                server.shutdown()
                server.server_close()

    def test_background_watcher_does_not_consume_reload_change_before_poll(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<html></html>", encoding="utf-8")
            serve._file_state = {}
            serve._changed = False
            server = start_server(root, 0)
            try:
                (root / "deck.js").write_text("window.changed = true;", encoding="utf-8")

                serve.check_for_changes(root, consume=False)

                self.assertTrue(serve.check_for_changes(root))
                self.assertFalse(serve.check_for_changes(root))
            finally:
                server.server_close()

    def test_build_deck_url_for_served_root_directory(self):
        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()

            self.assertEqual("http://127.0.0.1:3333/", build_deck_url(deck_dir, deck_dir, 3333))

    def test_build_deck_url_for_file_under_served_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            deck = root / "decks" / "talk" / "index.html"
            deck.parent.mkdir(parents=True)
            deck.write_text("", encoding="utf-8")

            self.assertEqual(
                "http://127.0.0.1:3333/decks/talk/index.html",
                build_deck_url(deck, root, 3333),
            )

    def test_main_accepts_repo_relative_deck_from_other_cwd(self):
        class FakeServer:
            def __init__(self):
                self.closed = False

            def serve_forever(self):
                raise KeyboardInterrupt()

            def server_close(self):
                self.closed = True

        fake_server = FakeServer()
        repo_root = Path(__file__).resolve().parents[1]
        expected_root = repo_root

        with TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                import os

                os.chdir(tmp)
                with patch.object(sys, "argv", ["serve.py", "examples/kitchen-sink", "--no-open"]), \
                     patch("serve.start_server", return_value=fake_server) as start_server_mock, \
                     patch("serve.threading.Thread"):
                    main()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(expected_root, start_server_mock.call_args.args[0])
        self.assertTrue(fake_server.closed)

    def test_main_restores_cwd_after_server_stops(self):
        class FakeServer:
            def serve_forever(self):
                raise KeyboardInterrupt()

            def server_close(self):
                pass

        with TemporaryDirectory() as tmp, TemporaryDirectory() as start_dir:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            (deck_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(start_dir)
                with patch.object(sys, "argv", ["serve.py", str(deck_dir), "--no-open"]), \
                     patch("serve.start_server", return_value=FakeServer()), \
                     patch("serve.threading.Thread"):
                    main()

                self.assertEqual(Path(start_dir).resolve(), Path.cwd().resolve())
            finally:
                os.chdir(old_cwd)

    def test_scan_files_watches_projects_inside_hidden_parent_dirs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / ".worktrees" / "webslides"
            root.mkdir(parents=True)
            index = root / "index.html"
            index.write_text("<html></html>", encoding="utf-8")

            self.assertIn(str(index), scan_files(root))

    def test_scan_files_watches_uppercase_web_assets(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "INDEX.HTML"
            index.write_text("<html></html>", encoding="utf-8")
            styles = root / "THEME.CSS"
            styles.write_text("body{}", encoding="utf-8")
            script = root / "DECK.JS"
            script.write_text("window.ok = true", encoding="utf-8")

            watched = scan_files(root)

            self.assertIn(str(index), watched)
            self.assertIn(str(styles), watched)
            self.assertIn(str(script), watched)

    def test_scan_files_detects_same_mtime_size_changes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index.html"
            index.write_text("short", encoding="utf-8")
            fixed_time = 1_700_000_000
            os.utime(index, (fixed_time, fixed_time))

            before = scan_files(root)
            index.write_text("longer content", encoding="utf-8")
            os.utime(index, (fixed_time, fixed_time))
            after = scan_files(root)

            self.assertNotEqual(before[str(index)], after[str(index)])

    def test_parse_args_reports_invalid_port_without_traceback(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            parse_args(["--port", "notaport"])

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("invalid int value", stderr.getvalue())

    def test_parse_args_rejects_out_of_range_port_without_traceback(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            parse_args(["--port", "99999"])

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("port must be between 1 and 65535", stderr.getvalue())

    def test_parse_args_reports_missing_port_value_without_traceback(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            parse_args(["--port"])

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("expected one argument", stderr.getvalue())

    def test_parse_args_rejects_unknown_flags(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            parse_args(["examples/kitchen-sink", "--bad-option"])

        self.assertEqual(2, ctx.exception.code)
        self.assertIn("unrecognized arguments: --bad-option", stderr.getvalue())

    def test_parse_args_accepts_no_open_flag(self):
        args = parse_args(["examples/kitchen-sink", "--no-open"])

        self.assertTrue(args.no_open)

    def test_maybe_open_browser_skips_subprocess_when_disabled(self):
        with patch("serve.subprocess.Popen") as popen:
            maybe_open_browser("http://127.0.0.1:3333/", open_browser=False)

        popen.assert_not_called()

    def test_maybe_open_browser_uses_chrome_when_enabled(self):
        with patch("serve.subprocess.Popen") as popen:
            maybe_open_browser("http://127.0.0.1:3333/", open_browser=True)

        popen.assert_called_once()
        self.assertEqual(["open", "-a", "Google Chrome", "http://127.0.0.1:3333/"], popen.call_args.args[0])

    def test_main_does_not_claim_opening_when_no_open_is_set(self):
        class FakeServer:
            def serve_forever(self):
                raise KeyboardInterrupt()

            def server_close(self):
                pass

        stdout = io.StringIO()
        with patch.object(sys, "argv", ["serve.py", "examples/kitchen-sink", "--no-open"]), \
             patch("serve.start_server", return_value=FakeServer()), \
             patch("serve.threading.Thread"), \
             contextlib.redirect_stdout(stdout):
            main()

        output = stdout.getvalue()
        self.assertNotIn("Opening:", output)
        self.assertIn("Browser opening disabled.", output)

    def test_main_rejects_missing_deck_before_starting_server(self):
        missing = "/tmp/definitely_missing_webslides_deck.html"
        with patch.object(sys, "argv", ["serve.py", missing]):
            with patch("serve.start_server") as start_server_mock:
                with self.assertRaises(SystemExit) as ctx:
                    main()

        self.assertIn("not found", str(ctx.exception))
        start_server_mock.assert_not_called()

    def test_main_rejects_deck_directory_without_index_before_starting_server(self):
        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()

            with patch.object(sys, "argv", ["serve.py", str(deck_dir), "--no-open"]):
                with patch("serve.start_server") as start_server_mock:
                    with self.assertRaises(SystemExit) as ctx:
                        main()

        self.assertIn("index.html not found", str(ctx.exception))
        start_server_mock.assert_not_called()

    def test_main_rejects_deck_directory_with_index_directory_before_starting_server(self):
        with TemporaryDirectory() as tmp:
            deck_dir = Path(tmp) / "talk"
            deck_dir.mkdir()
            (deck_dir / "index.html").mkdir()

            with patch.object(sys, "argv", ["serve.py", str(deck_dir), "--no-open"]):
                with patch("serve.start_server") as start_server_mock:
                    with self.assertRaises(SystemExit) as ctx:
                        main()

        self.assertIn("is a directory, not an HTML file", str(ctx.exception))
        start_server_mock.assert_not_called()

    def test_main_rejects_explicit_non_html_deck_file_before_starting_server(self):
        with TemporaryDirectory() as tmp:
            deck_file = Path(tmp) / "deck.css"
            deck_file.write_text("body {}", encoding="utf-8")

            with patch.object(sys, "argv", ["serve.py", str(deck_file), "--no-open"]):
                with patch("serve.start_server") as start_server_mock:
                    with self.assertRaises(SystemExit) as ctx:
                        main()

        self.assertIn("is not an HTML file", str(ctx.exception))
        start_server_mock.assert_not_called()

    def test_main_closes_server_if_serve_forever_fails(self):
        class FakeServer:
            def __init__(self):
                self.closed = False

            def serve_forever(self):
                raise RuntimeError("server failed")

            def server_close(self):
                self.closed = True

        fake_server = FakeServer()

        with patch.object(sys, "argv", ["serve.py", "--no-open"]), \
             patch("serve.start_server", return_value=fake_server), \
             patch("serve.threading.Thread"):
            with self.assertRaises(RuntimeError):
                main()

        self.assertTrue(fake_server.closed)

    def test_main_reports_bind_failure_before_starting_watcher(self):
        with patch.object(sys, "argv", ["serve.py", "--no-open"]), \
             patch("serve.start_server", side_effect=OSError("bind failed")), \
             patch("serve.threading.Thread") as thread:
            with self.assertRaises(SystemExit) as ctx:
                main()

        self.assertIn("could not start server", str(ctx.exception))
        self.assertIn("bind failed", str(ctx.exception))
        thread.assert_not_called()

    def test_reload_script_injection_handles_uppercase_tags_or_missing_body(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            uppercase = root / "uppercase.html"
            uppercase.write_text("<HTML><BODY>Deck</BODY></HTML>", encoding="utf-8")
            minimal = root / "minimal.html"
            minimal.write_text("<html>Deck</html>", encoding="utf-8")

            server = start_server(root, 0)
            port = server.server_address[1]
            try:
                import threading

                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                upper_html = urllib.request.urlopen(f"http://127.0.0.1:{port}/uppercase.html").read()
                minimal_html = urllib.request.urlopen(f"http://127.0.0.1:{port}/minimal.html").read()

                self.assertIn(b"/__ws_poll", upper_html)
                self.assertLess(upper_html.index(b"/__ws_poll"), upper_html.lower().index(b"</body>"))
                self.assertIn(b"/__ws_poll", minimal_html)
                self.assertTrue(minimal_html.endswith(b"</script>\n"))
            finally:
                server.shutdown()
                server.server_close()

    def test_reload_script_injection_handles_uppercase_html_extension(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            uppercase_ext = root / "INDEX.HTML"
            uppercase_ext.write_text("<HTML><BODY>Deck</BODY></HTML>", encoding="utf-8")

            server = start_server(root, 0)
            port = server.server_address[1]
            try:
                import threading

                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                html = urllib.request.urlopen(f"http://127.0.0.1:{port}/INDEX.HTML").read()

                self.assertIn(b"/__ws_poll", html)
                self.assertLess(html.index(b"/__ws_poll"), html.lower().index(b"</body>"))
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()

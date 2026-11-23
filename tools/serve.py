#!/usr/bin/env python3
"""WebSlides development server with auto-reload.

Usage:
    python3 tools/serve.py [deck-path]
    python3 tools/serve.py decks/example/ --port 8080
    python3 tools/serve.py /path/to/project/presentations/webslides/deck/

Serves from the nearest ancestor containing engine/webslides.js so both repo
decks and project-owned decks with an engine symlink resolve correctly.
Injects a live-reload script into HTML files. Opens Chrome on start unless
--no-open is passed.

If deck-path is given, opens directly to that deck.
"""

import argparse
import http.server
import json
import os
import subprocess
import threading
import time
import urllib.parse
from functools import partial
from pathlib import Path

from _common import widen_root_for_stats

# Defaults
DEFAULT_PORT = 3333
DEFAULT_HOST = "127.0.0.1"
POLL_INTERVAL = 0.5

# Find repo root (parent of tools/)
WEBSLIDES_ROOT = Path(__file__).resolve().parent.parent

# Track file modification state
_file_state: dict[str, tuple[int, int]] = {}
_changed = False
_lock = threading.Lock()

RELOAD_SCRIPT = b"""
<script>
(function(){
    setInterval(async()=>{
        try{
            const r=await fetch('/__ws_poll');
            const d=await r.json();
            if(d.changed)location.reload();
        }catch(e){}
    },500);
})();
</script>
"""


WATCH_SUFFIXES = {".html", ".css", ".js"}
SKIPPED_DIRS = {"node_modules"}


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def scan_files(root: Path) -> dict[str, tuple[int, int]]:
    """Get change signatures of all watchable files under root."""
    state: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if path.suffix.lower() not in WATCH_SUFFIXES:
            continue
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") or part in SKIPPED_DIRS for part in rel_parts):
            continue
        try:
            if path.is_file():
                stat = path.stat()
                state[str(path)] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
    return state


def check_for_changes(root: Path, *, consume: bool = True) -> bool:
    """Check if watched files changed; optionally consume the reload signal."""
    global _file_state, _changed
    new_state = scan_files(root)
    with _lock:
        if new_state != _file_state:
            _file_state = new_state
            _changed = True
        changed = _changed
        if consume:
            _changed = False
    return changed


def find_webslides_root(start: Path) -> Path:
    """Find nearest ancestor that can serve a deck and engine together."""
    d = start
    while True:
        if (d / "engine" / "webslides.js").exists():
            return d
        parent = d.parent
        if parent == d:
            break
        d = parent
    return start


def resolve_deck_path(deck_arg: str) -> Path:
    """Resolve a deck argument, falling back to paths relative to this repo."""
    path = Path(deck_arg).expanduser().resolve()
    if path.exists():
        return path

    candidate = (WEBSLIDES_ROOT / deck_arg).expanduser().resolve()
    if candidate.exists():
        return candidate

    return path


def start_server(root: Path, port: int, watch_root: "Path | None" = None) -> http.server.ThreadingHTTPServer:
    """Create the live-reload HTTP server for a root directory."""
    global _file_state, _changed
    with _lock:
        _file_state = scan_files(watch_root or root)
        _changed = False
    handler = partial(ReloadHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer((DEFAULT_HOST, port), handler)
    server.daemon_threads = True
    return server


def build_deck_url(deck_abs: Path, serve_root: Path, port: int) -> str:
    """Build the browser URL for a requested deck path."""
    rel = deck_abs.relative_to(serve_root)
    rel_url = "" if str(rel) == "." else urllib.parse.quote(rel.as_posix())
    suffix = "/" if deck_abs.is_dir() and rel_url else ""
    return f"http://{DEFAULT_HOST}:{port}/{rel_url}{suffix}"


def validate_deck_target(deck_abs: Path) -> None:
    """Reject explicit deck targets that cannot serve a deck page."""
    if deck_abs.is_dir():
        index = deck_abs / "index.html"
        if not index.exists():
            raise SystemExit(f"ERROR: {index} not found.")
        if index.is_dir():
            raise SystemExit(f"ERROR: {index} is a directory, not an HTML file.")
        return

    if not deck_abs.is_file():
        raise SystemExit(f"ERROR: {deck_abs} is not a file or directory.")
    if deck_abs.suffix.lower() != ".html":
        raise SystemExit(f"ERROR: {deck_abs} is not an HTML file.")


def file_watcher(root: Path) -> None:
    """Background thread that polls for file changes."""
    global _file_state
    _file_state = scan_files(root)
    while True:
        time.sleep(POLL_INTERVAL)
        check_for_changes(root, consume=False)


def maybe_open_browser(url: str, open_browser: bool = True) -> None:
    """Open the served URL in Chrome unless disabled by the caller."""
    if not open_browser:
        print(f"Browser opening disabled. Navigate to: {url}")
        return
    try:
        subprocess.Popen(
            ["open", "-a", "Google Chrome", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        print(f"Could not open Chrome. Navigate to: {url}")


class ReloadHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler that injects live-reload and serves a change-poll endpoint."""

    def do_GET(self) -> None:
        if self.path == "/__ws_poll":
            changed = check_for_changes(Path(self.directory))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps({"changed": changed}).encode())
            return

        path = Path(self.translate_path(self.path))
        if path.is_dir():
            path = path / "index.html"

        if path.suffix.lower() == ".html" and path.is_file():
            self.serve_with_reload(path)
            return

        super().do_GET()

    def serve_with_reload(self, filepath: Path) -> None:
        """Serve HTML with injected live-reload polling script."""
        try:
            content = filepath.read_bytes()
        except OSError:
            self.send_error(404)
            return

        marker = b"</body>"
        marker_idx = content.lower().rfind(marker)
        if marker_idx >= 0:
            content = content[:marker_idx] + RELOAD_SCRIPT + content[marker_idx:]
        else:
            content += RELOAD_SCRIPT

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format_string, *args):
        """Quieter logging — skip poll requests."""
        if "/__ws_poll" in str(args):
            return
        super().log_message(format_string, *args)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a WebSlides development server with live reload.",
    )
    parser.add_argument("deck_path", nargs="?", help="Optional deck index.html or deck directory to open.")
    parser.add_argument("--port", type=port_number, default=DEFAULT_PORT, help=f"Port to bind. Defaults to {DEFAULT_PORT}.")
    parser.add_argument("--no-open", action="store_true", help="Do not open Chrome; print the URL only.")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    port = args.port
    deck_path = args.deck_path

    serve_root = WEBSLIDES_ROOT
    watch_root = serve_root
    deck_abs = None
    if deck_path:
        deck_abs = resolve_deck_path(deck_path)
        if not deck_abs.exists():
            raise SystemExit(f"ERROR: {deck_abs} not found.")
        validate_deck_target(deck_abs)
        search_start = deck_abs if deck_abs.is_dir() else deck_abs.parent
        watch_root = find_webslides_root(search_start)
        # A stats source outside the deck tree is served too, but not watched.
        serve_root = widen_root_for_stats(deck_abs / "index.html" if deck_abs.is_dir() else deck_abs, watch_root)

    previous_cwd = Path.cwd()
    os.chdir(serve_root)

    try:
        if deck_path:
            url = build_deck_url(deck_abs, serve_root, port)
        else:
            url = f"http://{DEFAULT_HOST}:{port}/"

        try:
            server = start_server(serve_root, port, watch_root=watch_root)
        except OSError as exc:
            raise SystemExit(f"ERROR: could not start server on {DEFAULT_HOST}:{port}: {exc}") from exc

        watcher = threading.Thread(target=file_watcher, args=(watch_root,), daemon=True)
        watcher.start()

        print(f"WebSlides dev server running at http://{DEFAULT_HOST}:{port}/")
        if deck_path and not args.no_open:
            print(f"Opening: {url}")

        maybe_open_browser(url, open_browser=not args.no_open)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
        finally:
            server.server_close()
    finally:
        os.chdir(previous_cwd)


if __name__ == "__main__":
    main()

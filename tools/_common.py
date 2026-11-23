"""Shared helpers for the WebSlides browser tools.

One place for the things every Playwright tool needs:

- ``ensure_tool_python``: re-run a tool under the WebSlides export venv when the
  current interpreter (often system ``python3`` 3.9) lacks Playwright.
- serving a deck over local HTTP from the nearest ancestor that holds
  ``engine/webslides.js`` (decks link ``../engine/...``).
- waiting until the deck is ready: ``WS.ready`` when the engine provides it,
  otherwise the older polling fallback (tables loaded, KaTeX present, fonts).
- step counts and reveal ranges from the engine itself (``WS.getExportInfo()``,
  ``WS.slideSteps``, ``WS.parseRevealRange``), so no tool keeps its own parser.

This module must stay importable by Python 3.9 (system ``python3``).
"""

from __future__ import annotations

import asyncio
import http.server
import importlib.util
import os
import sys
import threading
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

SLIDE_WIDTH = 1280
SLIDE_HEIGHT = 720
WEBSLIDES_ROOT = Path(__file__).resolve().parent.parent
EXPORT_PYTHON = Path(
    os.environ.get("WEBSLIDES_EXPORT_PYTHON")
    or Path.home() / ".venvs" / "webslides-export" / "bin" / "python"
)
REEXEC_ENV = "WEBSLIDES_TOOL_REEXEC"
READY_TIMEOUT_MS = 30000
VENV_REBUILD_HINT = (
    "python3 -m venv ~/.venvs/webslides-export && "
    "~/.venvs/webslides-export/bin/pip install playwright beautifulsoup4 lxml pypdf pillow python-pptx fonttools xlsxwriter && "
    "~/.venvs/webslides-export/bin/python -m playwright install chromium"
)


# ---------------------------------------------------------------- interpreter
def missing_modules(modules):
    return [name for name in modules if importlib.util.find_spec(name) is None]


def _running_in(python):
    try:
        return Path(sys.prefix).resolve() == Path(python).parent.parent.resolve()
    except OSError:
        return False


def ensure_tool_python(modules=("playwright",), script=None):
    """Re-exec the calling tool under the export venv when ``modules`` are missing.

    Call this from a tool's ``if __name__ == "__main__":`` guard before anything
    imports Playwright. Exits with status 2 and a clear message when no usable
    interpreter exists.
    """
    missing = missing_modules(modules)
    if not missing:
        return
    script = os.path.abspath(script or sys.argv[0])
    can_reexec = (
        os.environ.get(REEXEC_ENV) != "1"
        and EXPORT_PYTHON.exists()
        and os.access(str(EXPORT_PYTHON), os.X_OK)
        and not _running_in(EXPORT_PYTHON)
    )
    if can_reexec:
        env = dict(os.environ)
        env[REEXEC_ENV] = "1"
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(str(EXPORT_PYTHON), [str(EXPORT_PYTHON), script] + sys.argv[1:], env)
    sys.stderr.write(
        "Error: {tool} needs the Python package(s) {pkgs}, which {python} does not have.\n"
        "       Expected the WebSlides export venv at {venv}{state}.\n"
        "       Rebuild it with: {hint}\n".format(
            tool=Path(script).name,
            pkgs=", ".join(missing),
            python=sys.executable,
            venv=EXPORT_PYTHON,
            state="" if EXPORT_PYTHON.exists() else " (missing)",
            hint=VENV_REBUILD_HINT,
        )
    )
    raise SystemExit(2)


# ---------------------------------------------------------------- deck paths and serving
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files silently and ignores broken-pipe races with Chromium."""

    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def start_server(root_dir, port=0):
    """Start a threaded local HTTP server for ``root_dir`` in a daemon thread.

    Threading matters: decks with many external tables fetch in parallel, and a
    serial server slows their loading about tenfold.
    """
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port),
        lambda *args, **kwargs: QuietHandler(*args, directory=str(root_dir), **kwargs),
    )
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def stop_server(httpd):
    httpd.shutdown()
    httpd.server_close()


def find_webslides_root(html_path):
    """Directory to serve the deck from.

    The nearest ancestor of the deck containing engine/webslides.js (else the
    deck dir), widened to a common ancestor when the deck's stats source
    (``data-stats-dir`` / ``data-stats-src`` on ``.ws-deck``) lies outside it.
    """
    html_path = Path(html_path)
    d = html_path.parent
    while True:
        if (d / "engine" / "webslides.js").exists():
            root = d
            break
        parent = d.parent
        if parent == d:
            root = html_path.parent
            break
        d = parent
    return widen_root_for_stats(html_path, root)


class _DeckRootParser(HTMLParser):
    """Attributes of the first element whose class list holds ``ws-deck``."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attrs = None

    def handle_starttag(self, tag, attrs):
        if self.attrs is None:
            values = dict(attrs)
            if "ws-deck" in (values.get("class") or "").split():
                self.attrs = values


def deck_root_attributes(html_path):
    """The ``.ws-deck`` element's attributes ({} when the file has none)."""
    try:
        text = Path(html_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    parser = _DeckRootParser()
    parser.feed(text)
    return parser.attrs or {}


def stats_source_paths(html_path):
    """Local paths named by the deck's data-stats-dir / data-stats-src.

    Resolved lexically against the deck directory (as the browser resolves
    them against the deck URL); URLs with a scheme or host are skipped.
    """
    attrs = deck_root_attributes(html_path)
    deck_dir = os.path.abspath(str(Path(html_path).parent))
    paths = []
    for name in ("data-stats-dir", "data-stats-src"):
        value = (attrs.get(name) or "").strip()
        parsed = urllib.parse.urlparse(value)
        # Root-relative paths resolve against the server root: nothing to widen.
        if not value or parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
            continue
        paths.append(Path(os.path.normpath(os.path.join(deck_dir, urllib.parse.unquote(parsed.path)))))
    return paths


def widen_root_for_stats(html_path, root):
    """``root``, or the common ancestor of it and every local stats path."""
    widened = Path(os.path.abspath(str(root)))
    changed = False
    for path in stats_source_paths(html_path):
        try:
            path.relative_to(widened)
        except ValueError:
            widened = Path(os.path.commonpath([str(widened), str(path)]))
            changed = True
    return widened if changed else root


def build_deck_url(html_path, root, port):
    html_path, root = Path(html_path), Path(root)
    if html_path.is_absolute() != root.is_absolute():
        html_path, root = Path(os.path.abspath(str(html_path))), Path(os.path.abspath(str(root)))
    rel_url = urllib.parse.quote(html_path.relative_to(root).as_posix())
    return f"http://127.0.0.1:{port}/{rel_url}"


def resolve_deck_path(deck_arg):
    """Resolve an index.html path or deck directory (also relative to the repo root)."""
    path = Path(deck_arg).expanduser().resolve()
    if path.is_dir():
        path = path / "index.html"
    if path.exists():
        return path
    candidate = (WEBSLIDES_ROOT / deck_arg).expanduser().resolve()
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if candidate.exists():
        return candidate
    return path


def check_deck_file(html_path):
    """Raise ValueError unless ``html_path`` is an existing .html file."""
    html_path = Path(html_path)
    if not html_path.exists():
        raise ValueError(f"{html_path} not found")
    if html_path.is_dir():
        raise ValueError(f"{html_path} is a directory, not an HTML file")
    if not html_path.is_file():
        raise ValueError(f"{html_path} is not a file")
    if html_path.suffix.lower() != ".html":
        raise ValueError(f"{html_path} is not an HTML file")
    return html_path


class DeckServer:
    """Context manager: serve a deck and expose its URL.

    >>> with DeckServer(index_html) as served:
    ...     page.goto(served.url)
    """

    def __init__(self, html_path):
        self.html_path = Path(html_path)
        self.root = find_webslides_root(self.html_path)
        self.httpd = None
        self.url = None

    def __enter__(self):
        self.httpd = start_server(self.root, 0)
        self.url = build_deck_url(self.html_path, self.root, self.httpd.server_address[1])
        return self

    def __exit__(self, *exc):
        if self.httpd is not None:
            stop_server(self.httpd)
            self.httpd = None


# ---------------------------------------------------------------- browser-side helpers
DECK_STRUCTURE_JS = """() => {
    const decks = Array.from(document.querySelectorAll(".ws-deck"));
    return {
        deckCount: decks.length,
        nestedDeckCount: decks.filter(deck => deck.parentElement && deck.parentElement.closest(".ws-deck")).length,
        directSlideCount: decks.reduce((count, deck) => {
            return count + Array.from(deck.children).filter(
                child => child.tagName === "SECTION" && child.classList && child.classList.contains("slide")
            ).length;
        }, 0),
    };
}"""


def strict_int(value, *, min_value=None):
    if type(value) is not int:
        return None
    if min_value is not None and value < min_value:
        return None
    return value


def validate_browser_deck_structure(info, empty_message="No slides were checked"):
    """Validate browser-observed deck structure before waiting on runtime state."""
    if not isinstance(info, dict):
        raise ValueError("Invalid deck structure metadata")
    deck_count = strict_int(info.get("deckCount"), min_value=0)
    nested_deck_count = strict_int(info.get("nestedDeckCount"), min_value=0)
    direct_slide_count = strict_int(info.get("directSlideCount"), min_value=0)
    if deck_count is None or nested_deck_count is None or direct_slide_count is None:
        raise ValueError("Invalid deck structure metadata")
    if deck_count != 1:
        raise ValueError(f"Expected exactly one .ws-deck container, found {deck_count}")
    if nested_deck_count:
        raise ValueError(".ws-deck containers must not be nested")
    if direct_slide_count <= 0:
        raise ValueError(empty_message)
    return info


def raise_for_page_errors(page_errors):
    if page_errors:
        raise RuntimeError(f"Deck JavaScript error: {page_errors[0]}")


# JS prelude defining engine-backed helpers; tools embed it with engine_js().
#   wsRange(raw)    -> {start, end} (end null when open) or null, via WS.parseRevealRange
#   wsElRange(el, attr='step') -> the engine-resolved range of an element's attribute
#                      (WS.getRevealRange; needed for relative steps "+" / "=")
#   wsMaxStep(i)    -> the engine's max step for slide i
#   wsSlides()      -> the engine's slide list
# The fallbacks only serve engines older than the WS.ready / parseRevealRange API.
ENGINE_HELPERS_JS = r"""
    const wsSlides = () => (window.WS && Array.isArray(WS.slides) && WS.slides.length)
        ? WS.slides
        : Array.from(document.querySelectorAll('.ws-deck > section.slide'));
    const wsRange = (function () {
        if (window.WS && typeof WS.parseRevealRange === 'function') {
            return function (raw) {
                const r = WS.parseRevealRange(raw);
                if (!r || !Number.isFinite(r.start)) return null;
                const open = r.end === undefined || r.end === null || r.end === Infinity;
                return { start: r.start, end: open ? null : r.end };
            };
        }
        // Fallback mirroring the engine's private parseStepValue (pre-C1 engines).
        function int(raw) {
            raw = String(raw).trim();
            if (!/^\d+$/.test(raw)) return null;
            const n = Number(raw);
            return Number.isSafeInteger(n) ? n : null;
        }
        return function (raw) {
            if (raw === undefined || raw === null) return { start: 1, end: null };
            raw = String(raw).trim();
            if (raw === '') return { start: 1, end: null };
            if (raw.endsWith('+')) {
                const s = int(raw.slice(0, -1));
                return s === null ? null : { start: s, end: null };
            }
            if (raw.includes('-')) {
                const parts = raw.split('-');
                if (parts.length !== 2) return null;
                const s = int(parts[0]);
                const e = int(parts[1]);
                if (s === null || e === null || e < s) return null;
                return { start: s, end: e };
            }
            const n = int(raw);
            return n === null ? null : { start: n, end: null };
        };
    })();
    // The engine's resolved range for an element's reveal attribute ('step',
    // 'fade-after', 'highlight', 'dim'): relative steps ("+", "=") only have a
    // value in the context of their slide, so never parse those here.
    const wsElRange = function (el, attr) {
        attr = attr || 'step';
        if (window.WS && typeof WS.getRevealRange === 'function') {
            const r = WS.getRevealRange(el, attr);
            if (!r || !Number.isFinite(r.start)) return null;
            return { start: r.start, end: r.end === undefined || r.end === null || r.end === Infinity ? null : r.end };
        }
        return el.hasAttribute('data-' + attr) ? wsRange(el.getAttribute('data-' + attr)) : null;
    };
    const wsExportInfo = (function () {
        try {
            const info = window.WS && typeof WS.getExportInfo === 'function' ? WS.getExportInfo() : null;
            return Array.isArray(info) ? info : null;
        } catch (err) {
            return null;
        }
    })();
    function wsFallbackMaxStep(slide) {
        let max = 0;
        slide.querySelectorAll('[data-step], [data-highlight], [data-dim], [data-class]').forEach(el => {
            ['step', 'highlight', 'dim', 'class'].forEach(attr => {
                let raw = el.dataset[attr];
                if (raw === undefined) return;
                if (attr === 'class') {
                    const sep = raw.lastIndexOf(':');
                    if (sep <= 0) return;
                    raw = raw.slice(sep + 1);
                }
                const r = wsRange(raw);
                if (r) max = Math.max(max, r.start, r.end || 0);
            });
        });
        return max;
    }
    function wsMaxStep(index) {
        const entry = wsExportInfo && wsExportInfo[index];
        if (entry && Number.isSafeInteger(entry.maxStep) && entry.maxStep >= 0) return entry.maxStep;
        const slide = wsSlides()[index];
        if (!slide) return 0;
        const steps = window.WS && WS.slideSteps;
        if (steps) {
            const v = typeof steps.get === 'function' ? steps.get(slide) : steps[index];
            if (Number.isSafeInteger(v) && v >= 0) return v;
        }
        return wsFallbackMaxStep(slide);
    }
"""


def engine_js(body, params=""):
    """Wrap ``body`` in a page function with the engine helpers in scope."""
    return f"({params}) => {{\n{ENGINE_HELPERS_JS}\n{body}\n}}"


# Readiness: WS.ready (C1) when present, else the pre-C1 polling predicate.
READY_STATUS_JS = r"""() => {
    const WS = window.WS;
    if (!WS || typeof WS.goToSlide !== 'function' || !Array.isArray(WS.slides) || WS.slides.length === 0) {
        return { ready: false, api: false, waitingFor: 'engine initialisation' };
    }
    if (WS.ready && typeof WS.ready.then === 'function') {
        if (document.documentElement.dataset.wsReady === '1' || window.__wsToolsReady) {
            return { ready: true, api: true };
        }
        if (!window.__wsToolsReadyHooked) {
            window.__wsToolsReadyHooked = true;
            WS.ready.then(() => { window.__wsToolsReady = true; }, () => { window.__wsToolsReady = true; });
        }
        return { ready: false, api: true, waitingFor: 'WS.ready' };
    }
    for (const slide of WS.slides) {
        for (const el of slide.querySelectorAll('[data-table-src]')) {
            if (el.children.length === 0 && !el.classList.contains('ws-table-load-error')) {
                return { ready: false, api: false, waitingFor: 'external tables' };
            }
        }
    }
    const katexRequested = document.querySelector('link[href*="katex"], script[src*="katex"]');
    if (katexRequested && typeof window.katex === 'undefined') {
        return { ready: false, api: false, waitingFor: 'KaTeX' };
    }
    return { ready: true, api: false };
}"""

# After readiness: web fonts, then the deck's images (table HTML can bring in
# images after page load; capped so one stalled image cannot hang a tool), then
# two frames for the engine's requestAnimationFrame layout work.
SETTLE_JS = """() => document.fonts.ready.then(() => {
    const pending = Array.from(document.querySelectorAll('.ws-deck img'))
        .filter(img => !img.complete && img.getAttribute('loading') !== 'lazy')
        .map(img => new Promise(resolve => {
            img.addEventListener('load', resolve, { once: true });
            img.addEventListener('error', resolve, { once: true });
        }));
    return Promise.race([Promise.all(pending), new Promise(resolve => setTimeout(resolve, 10000))]);
}).then(() => new Promise(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
}))"""

LOAD_ERRORS_JS = r"""() => {
    const slides = (window.WS && Array.isArray(WS.slides)) ? WS.slides : [];
    const indexOf = el => {
        const slide = el.closest && el.closest('section.slide');
        const i = slides.indexOf(slide);
        return i >= 0 ? i : null;
    };
    if (window.WS && Array.isArray(WS.loadErrors)) {
        return WS.loadErrors.map(e => ({
            type: String((e && e.type) || ''),
            src: String((e && e.src) || ''),
            detail: String((e && e.detail) || ''),
            slideIndex: e && Number.isInteger(e.slideIndex) ? e.slideIndex : null,
        }));
    }
    // Engines without WS.loadErrors: failed tables show an error box and
    // failed SVG inlines leave the <img data-inline> in place.
    const out = [];
    slides.forEach(slide => {
        slide.querySelectorAll('.ws-table-load-error').forEach(el => out.push({
            type: 'table', src: el.getAttribute('data-table-src') || '',
            detail: (el.textContent || '').trim().slice(0, 200), slideIndex: indexOf(el),
        }));
        slide.querySelectorAll('img[data-inline]').forEach(img => out.push({
            type: 'svg', src: img.getAttribute('src') || '',
            detail: 'SVG was not inlined', slideIndex: indexOf(img),
        }));
    });
    return out;
}"""

SLIDE_INFO_JS = engine_js(r"""
    return wsSlides().map((slide, i) => {
        const heading = slide.querySelector('h1, h2');
        const main = slide.dataset.wsMainNumber;
        return {
            index: i,
            maxStep: wsMaxStep(i),
            id: slide.id || '',
            isAppendix: slide.classList.contains('appendix'),
            mainNumber: main && /^\d+$/.test(main) ? Number(main) : null,
            title: heading ? (heading.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 100) : '',
            classes: Array.from(slide.classList).filter(c => !c.startsWith('ws-')),
            declaredSteps: slide.getAttribute('data-steps'),
            innerIds: Array.from(slide.querySelectorAll('[id]')).map(el => el.id).filter(Boolean),
        };
    });
""")


class DeckNotReady(RuntimeError):
    pass


def _not_ready_message(status, timeout_ms):
    waiting = (status or {}).get("waitingFor") or "deck initialisation"
    return f"Deck did not become ready within {timeout_ms / 1000:g}s (still waiting for {waiting})"


async def wait_for_deck_ready(page, page_errors=None, timeout_ms=READY_TIMEOUT_MS):
    """Async: wait for WS.ready (or the fallback predicate), then fonts and layout.

    Raises DeckNotReady on timeout and RuntimeError as soon as the deck throws.
    Returns the final status dict ({ready, api}).
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        raise_for_page_errors(page_errors)
        status = await page.evaluate(READY_STATUS_JS)
        if status.get("ready"):
            break
        if time.monotonic() > deadline:
            raise DeckNotReady(_not_ready_message(status, timeout_ms))
        await asyncio.sleep(0.05)
    await page.evaluate(SETTLE_JS)
    raise_for_page_errors(page_errors)
    return status


def wait_for_deck_ready_sync(page, page_errors=None, timeout_ms=READY_TIMEOUT_MS):
    """Sync-API twin of wait_for_deck_ready."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        raise_for_page_errors(page_errors)
        status = page.evaluate(READY_STATUS_JS)
        if status.get("ready"):
            break
        if time.monotonic() > deadline:
            raise DeckNotReady(_not_ready_message(status, timeout_ms))
        time.sleep(0.05)
    page.evaluate(SETTLE_JS)
    raise_for_page_errors(page_errors)
    return status


def format_load_error(err):
    where = f"slide {err['slideIndex'] + 1}" if isinstance(err.get("slideIndex"), int) else "deck"
    detail = f": {err['detail']}" if err.get("detail") else ""
    return f"{err.get('type') or 'asset'} {err.get('src') or '(no source)'} failed to load ({where}){detail}"


def parse_slide_selection(specs, slides):
    """Map CLI slide specs to slide indexes.

    Each spec is a 1-based deck position (``?slide=N``), a range ``A-B``, an
    element id (``#`` optional; an id inside a slide selects that slide), or
    ``all``. Raises ValueError for unknown or out-of-range specs.
    """
    by_id = {}
    for s in slides:
        for inner in s.get("innerIds") or ():
            by_id.setdefault(inner, s["index"])
    by_id.update({s["id"]: s["index"] for s in slides if s.get("id")})
    total = len(slides)
    chosen = []
    for raw in specs:
        for spec in str(raw).split(","):
            spec = spec.strip()
            if not spec:
                continue
            if spec.lower() == "all":
                chosen.extend(range(total))
                continue
            if spec.isdigit():
                n = int(spec)
                if not 1 <= n <= total:
                    raise ValueError(f"slide {n} out of range (deck has {total} slides)")
                chosen.append(n - 1)
                continue
            parts = spec.split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                a, b = int(parts[0]), int(parts[1])
                if a > b or a < 1 or b > total:
                    raise ValueError(f"slide range {spec} out of range (deck has {total} slides)")
                chosen.extend(range(a - 1, b))
                continue
            key = spec[1:] if spec.startswith("#") else spec
            if key in by_id:
                chosen.append(by_id[key])
                continue
            raise ValueError(f"unknown slide {spec!r} (use a 1-based number, a range like 3-7, or a slide id)")
    return list(dict.fromkeys(chosen))  # de-duplicate, keeping first-seen order

#!/usr/bin/env python3
"""Export a deck to an editable PowerPoint file.

    python3 tools/export_pptx.py path/to/index.html --output talk.pptx
    python3 tools/export_pptx.py path/to/claude-slides/project --output talk.pptx

Each deck slide becomes one PowerPoint slide. Text, bullets, tables, shapes and
figures are native objects, and every reveal step is a native click animation.
Accepts a WebSlides deck, or the project folder of a Claude Slides deck (the
folder holding deck.json and slides/). The export is one-way.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

if __name__ == "__main__":
    from _common import ensure_tool_python

    ensure_tool_python(("playwright", "pptx", "lxml", "PIL", "fontTools", "xlsxwriter"))

from _common import DeckServer, SLIDE_INFO_JS, resolve_deck_path, wait_for_deck_ready_sync  # noqa: E402

TOOLS = Path(__file__).resolve().parent
EXTRACT_JS = (TOOLS / "pptx_export" / "extract.js").read_text(encoding="utf-8")
NO_MOTION = "*,*::before,*::after{transition:none!important;animation:none!important}"

INLINE_SVG_IMAGES_JS = r"""async () => {
    for (const img of Array.from(document.querySelectorAll('section img'))) {
        if (!/\.svg(\?|#|$)/i.test(img.getAttribute('src') || '')) continue;
        const r = img.getBoundingClientRect();
        try {
            const text = await (await fetch(img.currentSrc || img.src)).text();
            const holder = document.createElement('div');
            holder.innerHTML = text;
            const svg = holder.querySelector('svg');
            if (!svg) continue;
            for (const a of img.attributes) if (a.name.startsWith('data-') || a.name === 'class') svg.setAttribute(a.name, a.value);
            svg.setAttribute('style', (img.getAttribute('style') || '') + ';width:' + img.offsetWidth + 'px;height:' + img.offsetHeight + 'px');
            if (img.alt) svg.setAttribute('aria-label', img.alt);
            img.replaceWith(svg);
        } catch (err) { console.warn(err); }
    }
}"""


class WebSlidesSource:
    design_w, design_h = 1280, 720
    root = "WS.slides[{i}]"

    def __init__(self, index):
        self.index = Path(index)
        self.server = None

    def open(self, page):
        self.server = DeckServer(self.index)
        self.server.__enter__()
        page.goto(self.server.url, wait_until="load")
        wait_for_deck_ready_sync(page)
        page.evaluate("WS.prepareForExport()")
        page.add_style_tag(content=NO_MOTION)
        page.evaluate(INLINE_SVG_IMAGES_JS)
        self.info = page.evaluate(SLIDE_INFO_JS)
        self.title = page.title()
        self.lang = page.evaluate("document.documentElement.lang") or "en-GB"

    def close(self):
        if self.server:
            self.server.__exit__(None, None, None)

    def slide_meta(self):
        return [{"id": s["id"], "inner_ids": s["innerIds"], "max_step": s["maxStep"]} for s in self.info]

    def show(self, page, index, step):
        page.evaluate("([i, s]) => { WS.goToSlide(i, s); return WS.refitTables ? WS.refitTables() : null; }", [index, step])
        page.wait_for_timeout(30)

    def footer(self, page, index):
        return page.evaluate(r"""() => ['ws-footer-left', 'ws-footer-right'].map(id => {
            const el = document.getElementById(id);
            if (!el || !el.textContent.trim() || !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return null;
            for (let p = el; p; p = p.parentElement) if (parseFloat(getComputedStyle(p).opacity) < 0.05) return null;
            const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
            if (r.width < 1) return null;
            return {role: id.endsWith('right') ? 'sldNum' : 'ftr', text: el.textContent.trim(), x: r.left, y: r.top, w: r.width, h: r.height,
                    sz: parseFloat(cs.fontSize), color: cs.color, f: cs.fontFamily.split(',')[0].replace(/["']/g, '').trim()};
        }).filter(Boolean)""")

    def fetch(self, src):
        return fetch_url(src)


class ClaudeSlidesSource:
    design_w, design_h = 1920, 1080
    root = "document.querySelectorAll('section')[{i}]"

    def __init__(self, project):
        self.project = Path(project)
        self.deck = json.loads((self.project / "deck.json").read_text(encoding="utf-8"))
        blob_map = self.project.parent / "assets" / "blob_map.json"
        self.blobs = {}
        if blob_map.exists():
            self.blobs = {k: blob_map.parent / v for k, v in json.loads(blob_map.read_text(encoding="utf-8")).items()}

    def _html(self):
        faces = "&".join("family=" + urllib.parse.quote(f["family"]) + ":wght@400;600;700;800" for f in self.deck.get("faces", {}).values())
        sections = "\n".join((self.project / "slides" / f"{sid}.html").read_text(encoding="utf-8") for sid in self.deck["order"])
        return (f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{html.escape(self.deck.get("title", ""))}</title>'
                f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?{faces}&display=swap"><style>*{{margin:0;box-sizing:border-box}}'
                "body{width:1920px;height:1080px;overflow:hidden}section{position:absolute;left:0;top:0;width:1920px;height:1080px;overflow:hidden;visibility:hidden}"
                f"section.px-on{{visibility:visible}}aside{{display:none}}a{{text-decoration:none}}{NO_MOTION}</style></head><body>{sections}</body></html>")

    def open(self, page):
        def handle(route):
            url = route.request.url
            if url.rstrip("/") == "http://px.local":
                return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self._html())
            data = self.fetch(url)
            return route.fulfill(status=200, body=data) if data else route.fulfill(status=404, body="")
        page.route("http://px.local/**", handle)
        page.goto("http://px.local/", wait_until="load")
        page.evaluate("document.fonts.ready")
        self.title = self.deck.get("title", "")
        self.lang = "fr-FR"
        self.steps = page.evaluate(r"""() => Array.from(document.querySelectorAll('section')).map(s => {
            const orders = new Set();
            s.querySelectorAll('[data-build-in],[data-build-out]').forEach(el => {
                ['data-build-in', 'data-build-out'].forEach(a => {
                    const m = (el.getAttribute(a) || '').match(/(\d+)/);
                    if (m) orders.add(Number(m[1]));
                });
            });
            return Array.from(orders).sort((a, b) => a - b);
        })""")

    def close(self):
        pass

    def slide_meta(self):
        return [{"id": sid, "inner_ids": [], "max_step": len(self.steps[i])} for i, sid in enumerate(self.deck["order"])]

    def show(self, page, index, step):
        orders = self.steps[index]
        limit = orders[step - 1] if step else -1
        page.evaluate(r"""([index, limit]) => {
            const sections = document.querySelectorAll('section');
            sections.forEach((s, i) => s.classList.toggle('px-on', i === index));
            sections[index].querySelectorAll('[data-build-in],[data-build-out]').forEach(el => {
                const order = a => { const m = (el.getAttribute(a) || '').match(/(\d+)/); return m ? Number(m[1]) : null; };
                const a = order('data-build-in'), b = order('data-build-out');
                const shown = (a === null || a <= limit) && (b === null || b > limit);
                el.style.setProperty('visibility', shown ? 'visible' : 'hidden', 'important');
            });
        }""", [index, limit])

    def footer(self, page, index):
        return []

    def fetch(self, src):
        m = re.search(r"/_blob/([0-9a-f]+)", src)
        if m:
            path = self.blobs.get(m.group(1))
            return path.read_bytes() if path and path.exists() else None
        return fetch_url(src)


def fetch_url(src):
    try:
        if src.startswith("data:"):
            header, payload = src.split(",", 1)
            return base64.b64decode(payload) if "base64" in header else urllib.parse.unquote_to_bytes(payload)
        with urllib.request.urlopen(src, timeout=20) as response:
            return response.read()
    except Exception:  # a missing picture is reported per slide by the writer
        return None


def to_supported_image(data):
    from PIL import Image

    if data is None or data[:4] in (b"\x89PNG", b"GIF8") or data[:2] == b"\xff\xd8":
        return data
    buffer = BytesIO()
    try:
        Image.open(BytesIO(data)).convert("RGBA").save(buffer, "PNG")
    except OSError:  # not a raster image (an SVG background, say): the writer reports it per slide
        return None
    return buffer.getvalue()


def capture_deck(source, page):
    from pptx_export.charts import detect_charts
    from pptx_export.merge import merge_frames, swap_steps

    page.evaluate(EXTRACT_JS)
    lang = source.lang if "-" in source.lang else {"en": "en-GB", "fr": "fr-FR"}.get(source.lang, source.lang)
    slides, first_of = [], []
    for i, meta in enumerate(source.slide_meta()):
        frames, bg, notes = [], None, ""
        root = source.root.format(i=i)
        for step in range(meta["max_step"] + 1):
            source.show(page, i, step)
            data = page.evaluate(f"(w) => PX.capture({root}, w)", source.design_w)
            for obj in data["objects"]:
                if obj["kind"] == "snapshot":
                    obj["png"] = page.screenshot(clip=obj["clip"], type="png")
            frames.append(data["objects"])
            bg, notes = data["bg"], data["notes"]
        foot = source.footer(page, i)
        first_of.append(len(slides))
        cuts = [0] + swap_steps(merge_frames(frames), source.design_w * source.design_h) + [len(frames)]
        for part, (start, end) in enumerate(zip(cuts, cuts[1:])):
            objects = detect_charts(merge_frames(frames[start:end]), lang)
            mark_slide_number(objects, foot, i, source)
            slides.append({"objects": objects, "bg": bg, "notes": notes if part == 0 else "",
                           "id": meta["id"] if part == 0 else None, "inner_ids": meta["inner_ids"] if part == 0 else []})
    return {"slides": slides, "first_of": first_of, "design_w": source.design_w, "design_h": source.design_h, "title": source.title, "lang": lang}


def mark_slide_number(objects, footers, index, source):
    """A typed page number goes stale when slides move: make it a live slide-number field."""
    from pptx_export.charts import text_of

    for obj in objects:
        if obj["kind"] != "text" or len(obj["paras"]) != 1:
            continue
        rect = obj["rect"]
        if text_of(obj) == str(index + 1) and rect["y"] > source.design_h * 0.85 and rect["x"] > source.design_w * 0.75:
            obj["role"] = "sldNum"
    n = len(objects[0]["vis"]) if objects else 1
    for foot in footers:
        text = str(index + 1) if foot["role"] == "sldNum" else foot["text"]
        run = {"t": text, "f": foot["f"], "sz": foot["sz"], "b": False, "i": False, "c": _css_color(foot["color"])}
        align = "r" if foot["role"] == "sldNum" else "l"
        objects.append({"kind": "text", "key": foot["role"], "uid": foot["role"], "role": foot["role"], "lines": 1, "nowrap": True, "op": 1,
                        "rect": {k: foot[k] for k in "xywh"}, "paras": [{"pkey": "f", "runs": [run], "align": align, "vis": [True] * n, "rel": [1.0] * n}],
                        "vis": [True] * n, "rel": [1.0] * n, "groups": []})


def _css_color(value):
    numbers = re.findall(r"[\d.]+", value)[:3]
    return {"hex": "".join(f"{int(float(v)):02X}" for v in numbers), "a": 1}


def summarise(path):
    from pptx import Presentation

    prs = Presentation(path)
    native = {"slides": len(prs.slides), "animated_slides": 0, "text_shapes": 0, "tables": 0, "charts": 0, "groups": 0, "pictures": 0, "top_level_objects": 0}
    for slide in prs.slides:
        native["animated_slides"] += "<p:timing" in slide._element.xml
        native["top_level_objects"] += len(slide.shapes)
        for shape in slide.shapes:
            native["groups"] += shape.shape_type == 6
            native["pictures"] += shape.shape_type == 13
            native["tables"] += bool(getattr(shape, "has_table", False))
            native["charts"] += bool(getattr(shape, "has_chart", False))
            native["text_shapes"] += bool(shape.has_text_frame and shape.text_frame.text.strip())
    return native


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("deck", help="WebSlides index.html or folder, or a Claude Slides project folder")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    sys.path.insert(0, str(TOOLS))
    from pptx_export.writer import DeckWriter

    deck = Path(args.deck).expanduser()
    source = ClaudeSlidesSource(deck) if (deck / "deck.json").exists() else WebSlidesSource(resolve_deck_path(args.deck))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": source.design_w, "height": source.design_h})
        try:
            source.open(page)
            data = capture_deck(source, page)
            warnings = DeckWriter(data, lambda src: to_supported_image(source.fetch(src))).build(args.output)
        finally:
            source.close()
            browser.close()
    print(json.dumps(summarise(args.output)))
    for warning in warnings:
        print("WARNING", warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Stats bound to files (data-stat, \\stat{}) and $...$ math in SVG text.

Browser tests load tests/fixtures/content-deck in headless Chromium (and WebKit
for the geometry check). The fixture loads KaTeX from the CDN like real decks;
requests are answered from a local KaTeX dist when one is found
(WEBSLIDES_KATEX_DIST or a known node_modules copy), else they go to the
network, and the math tests skip when KaTeX does not arrive.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "content-deck"
sys.path.insert(0, str(REPO / "tools"))

import check_stats  # noqa: E402
from _common import DeckServer, find_webslides_root, stats_source_paths  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - system python without Playwright
    sync_playwright = None

KATEX_CANDIDATES = [
    os.environ.get("WEBSLIDES_KATEX_DIST", ""),
    str(Path(__file__).resolve().parents[1] / "engine/vendor/katex"),
]
KATEX_DIST = next((Path(p) for p in KATEX_CANDIDATES if p and (Path(p) / "katex.min.js").is_file()), None)
CONTENT_TYPES = {".css": "text/css", ".js": "application/javascript", ".woff2": "font/woff2",
                 ".woff": "font/woff", ".ttf": "font/ttf"}


def serve_katex(route):
    m = re.search(r"/dist/(.+?)(?:\?.*)?$", route.request.url)
    path = KATEX_DIST / m.group(1) if (KATEX_DIST and m) else None
    if path and path.is_file():
        route.fulfill(path=str(path), headers={"content-type": CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
                                               "access-control-allow-origin": "*"})
    else:
        route.continue_()


class NormalisationTests(unittest.TestCase):
    def test_stat_files_become_slide_text(self):
        cases = {
            "4,051%": "4,051",
            "17.5\\%%\n": "17.5%",
            "$<$ 0.001%": "< 0.001",
            "-0.061%": "−0.061",
            "5{,}000%": "5,000",
            "[-1.279, -0.000]%": "[−1.279, −0.000]",
            "+0.002%": "+0.002",
            "Skills Training%": "Skills Training",
            "2010--2015": "2010–2015",
            "7e-04%": "7e-04",
        }
        for raw, want in cases.items():
            self.assertEqual(want, check_stats.stat_tex_to_text(raw), raw)

    def test_rounding_keeps_grouping_and_signs(self):
        self.assertEqual("24", check_stats.round_stat_text("23.9", "0"))
        self.assertEqual("−0.06", check_stats.round_stat_text("−0.061", "2"))
        self.assertEqual("12,346", check_stats.round_stat_text("12,345.6", "0"))
        self.assertEqual("[−1.3, 0.0]", check_stats.round_stat_text("[−1.279, 0.000]", "1"))
        self.assertEqual("17.5%", check_stats.round_stat_text("17.5%", "x"))


class CheckStatsToolTests(unittest.TestCase):
    def run_tool(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_stats.main(list(argv))
        return code, out.getvalue()

    def test_lists_values_and_flags_missing(self):
        code, out = self.run_tool(str(FIXTURE / "index.html"))
        self.assertEqual(1, code)
        self.assertRegex(out, r"n_analyses = 4,051 +slides 1, 2")
        self.assertRegex(out, r"months = 25 \(round 0\) +slide 1 +placeholder \"24\"")
        self.assertRegex(out, r"MISSING no_such_stat +slide 1 +stats/no_such_stat.tex: not found")
        self.assertIn("label = Skills Training (JSON)", out, "the JSON source wins over the directory")
        self.assertIn("pct_fadeout = 17.5%", out, "\\stat{} in SVG math and HTML math are listed")
        self.assertTrue(out.rstrip().endswith("1 missing, 10 with a different placeholder"), out)

    def test_json_output_and_declared_source_override(self):
        code, out = self.run_tool(str(FIXTURE / "no-source.html"), "--json")
        self.assertEqual(1, code)
        data = json.loads(out)
        self.assertIsNone(data["source"])
        code, out = self.run_tool(str(FIXTURE / "no-source.html"), "--json", "--stats-dir", str(FIXTURE / "stats"))
        self.assertEqual(0, code)
        [row] = json.loads(out)["stats"]
        self.assertEqual(("n_analyses", "4,051", ["4,000"]), (row["name"], row["value"], row["stale"]))


class ServeRootTests(unittest.TestCase):
    def test_root_widens_to_a_stats_dir_outside_the_deck_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            deck = project / "presentations" / "webslides" / "talk" / "index.html"
            (project / "presentations" / "webslides" / "engine").mkdir(parents=True)
            (project / "presentations" / "webslides" / "engine" / "webslides.js").write_text("")
            (project / "outputs" / "stats").mkdir(parents=True)
            deck.parent.mkdir(parents=True)
            deck.write_text('<div class="ws-deck" data-stats-dir="../../../outputs/stats"></div>')
            self.assertEqual([project / "outputs" / "stats"], stats_source_paths(deck))
            self.assertEqual(project, find_webslides_root(deck))
            deck.write_text('<div class="ws-deck"></div>')
            self.assertEqual(project / "presentations" / "webslides", find_webslides_root(deck))


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class EngineContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def open(self, name="index.html", browser=None, viewport=(1280, 720)):
        server = DeckServer(FIXTURE / name)
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        page = (browser or self.browser).new_page(viewport={"width": viewport[0], "height": viewport[1]})
        self.addCleanup(page.close)
        page.route(re.compile(r"https://cdn\.jsdelivr\.net/npm/katex@.*"), serve_katex)
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(server.url)
        page.wait_for_function("document.documentElement.dataset.wsReady === '1'", timeout=30000)
        self.assertEqual([], errors)
        return page

    def open_math(self, **kw):
        page = self.open(**kw)
        if not page.evaluate("typeof window.katex !== 'undefined'"):
            self.skipTest("KaTeX did not load (offline and no local KaTeX dist)")
        return page

    def text_of(self, page, selector):
        return page.evaluate("s => document.querySelector(s).textContent", selector)

    # ------------------------------------------------------------ stats
    def test_html_stats_are_filled_and_authored_text_kept(self):
        page = self.open()
        want = {"#s-count": "4,051", "#s-pct": "17.5%", "#s-pval": "< 0.001", "#s-delta": "−0.061",
                "#s-big": "5,000", "#s-label": "Skills Training (JSON)", "#s-round": "25", "#s-round2": "0.46",
                "#s-json": "0.25", "#s-json-str": "12,345"}
        for selector, text in want.items():
            self.assertEqual(text, self.text_of(page, selector), selector)
        self.assertEqual("4,000", page.evaluate("document.querySelector('#s-count').getAttribute('data-ws-original-html')"))

    def test_missing_stat_is_marked_and_reported(self):
        page = self.open()
        self.assertEqual("[no_such_stat?]", self.text_of(page, "#s-missing"))
        errors = [e for e in page.evaluate("WS.loadErrors") if e["type"] == "stat"]
        self.assertEqual([{"type": "stat", "src": "stats/no_such_stat.tex", "slideIndex": 0,
                           "detail": 'stat "no_such_stat": 404 File not found'}], errors)

    def test_bad_svg_math_is_shown_and_reported(self):
        page = self.open_math()
        [err] = [e for e in page.evaluate("WS.loadErrors") if e["type"] == "math"]
        self.assertEqual(("\\frac{1", 1), (err["src"], err["slideIndex"]))
        self.assertIn("KaTeX parse error", err["detail"])
        fill = page.evaluate("""() => getComputedStyle(Array.from(document.querySelectorAll('[id$="m-error"] tspan'))
            .find(t => t.textContent.includes('frac'))).fill""")
        self.assertNotEqual("rgb(0, 0, 0)", fill, "KaTeX shows the bad source in its error colour")

    def test_engine_values_match_check_stats(self):
        page = self.open()
        engine = page.evaluate("WS.stats")
        _source, rows = check_stats.check(FIXTURE / "index.html", type("A", (), {"stats_dir": None, "stats_src": None})())
        tool = {r["name"]: r["value"] for r in rows if r["value"] is not None and not r["round"]}
        for name, value in tool.items():
            self.assertEqual(value, engine.get(name), name)
        rounded = {r["name"]: r["value"] for r in rows if r["round"]}
        self.assertEqual(rounded["months"], self.text_of(page, "#s-round"))
        self.assertEqual(rounded["share"], self.text_of(page, "#s-round2"))

    def test_table_stats_and_stat_macro(self):
        page = self.open_math()
        page.evaluate("WS.goToSlide(3, 0)")
        self.assertEqual("5,000", self.text_of(page, "#t-count"))
        self.assertIn("0.061", self.text_of(page, "#t-math .katex-html"))
        self.assertTrue(page.evaluate("document.querySelector('.table-container').hasAttribute('data-ws-original-html')"))

    def test_html_math_stat_keeps_authored_tex_for_editors(self):
        page = self.open_math()
        info = page.evaluate("""() => Array.from(document.querySelectorAll('#p-math .katex')).map(k => ({
            ann: k.querySelector('annotation').textContent, tex: k.getAttribute('data-ws-tex'),
            shown: k.querySelector('.katex-html').textContent }))""")
        self.assertEqual(["\\hat\\beta = \\stat{delta}", "\\tau = \\stat{pct_fadeout}"], [i["ann"] for i in info])
        self.assertEqual(["\\hat\\beta = {-0.061}", "\\tau = {17.5\\%}"], [i["tex"] for i in info])
        self.assertIn("0.061", info[0]["shown"])
        self.assertTrue(page.evaluate("!!document.querySelector('#p-math .katex-display')"), "$$ stays display math")

    def test_deck_without_a_stats_source_is_left_as_typed(self):
        page = self.open("no-source.html")
        self.assertEqual("4,000", self.text_of(page, "#n-placeholder"))
        self.assertEqual("costs $5 and $6, no KaTeX here", self.text_of(page, "#no-katex"))
        self.assertEqual(0, page.evaluate("document.querySelectorAll('[data-ws-original-html]').length"))
        self.assertEqual([], page.evaluate("WS.loadErrors"))
        self.assertEqual({}, page.evaluate("WS.stats"))

    # ------------------------------------------------------------ SVG math
    def svg_texts(self, page):
        return page.evaluate("""() => {
            const svg = document.querySelector('svg[data-ws-original-img]');
            const out = {};
            svg.querySelectorAll('text[id]').forEach(t => {
                const id = t.id.replace(/^ws-svg\\d+-/, '');
                out[id] = { text: t.textContent, fonts: Array.from(t.querySelectorAll('tspan[style*="KaTeX"]')).length,
                            wrapped: t.parentNode.classList.contains('ws-svg-math'), step: t.getAttribute('data-step'),
                            parentStep: t.parentNode.getAttribute('data-step') };
            });
            return out;
        }""")

    def test_svg_math_is_typeset_in_katex_fonts(self):
        page = self.open_math()
        texts = self.svg_texts(page)
        for name in ("m-start", "m-middle", "m-end", "m-baseline", "m-dy", "m-halo", "m-rotated", "m-frac",
                     "m-stat", "m-color", "m-tspan-lines"):
            self.assertNotIn("$", texts[name]["text"], name)
            self.assertGreater(texts[name]["fonts"], 0, name)
        self.assertEqual("cost $5 to $10, no math", texts["m-dollar"]["text"].strip())
        self.assertEqual(0, texts["m-dollar"]["fonts"])
        self.assertIn("17.5%", texts["m-stat"]["text"])
        self.assertIn("4,051", texts["m-stat"]["text"])
        self.assertEqual(["\\frac{1"], [e["src"] for e in page.evaluate("WS.loadErrors") if e["type"] == "math"])

    def test_plain_svg_text_is_untouched(self):
        page = self.open_math()
        html = page.evaluate("document.querySelector('[id$=\"m-plain\"]').outerHTML")
        self.assertEqual('<text id="ws-svg0-m-plain" x="560" y="530" font-size="24">plain label untouched</text>', html)

    def test_rules_and_radicals_wrap_the_text_and_carry_its_step(self):
        page = self.open_math()
        texts = self.svg_texts(page)
        self.assertTrue(texts["m-frac"]["wrapped"])
        self.assertTrue(texts["m-frac-step"]["wrapped"])
        self.assertIsNone(texts["m-frac-step"]["step"])
        self.assertEqual("2", texts["m-frac-step"]["parentStep"])
        self.assertFalse(texts["m-start"]["wrapped"])
        parts = page.evaluate("""() => { const g = document.querySelector('[id$="m-frac"]').parentNode;
            return { rects: g.querySelectorAll(':scope > rect').length, svgs: g.querySelectorAll(':scope > svg').length }; }""")
        self.assertGreaterEqual(parts["rects"], 1, "fraction bar")
        self.assertGreaterEqual(parts["svgs"], 1, "radical sign")
        self.assertEqual(2, page.evaluate("WS.slideSteps.get(WS.slides[1])"))
        visible = "s => document.querySelector(s).parentNode.classList.contains('ws-visible')"
        page.evaluate("WS.goToSlide(1, 1)")
        self.assertFalse(page.evaluate(visible, '[id$="m-frac-step"]'))
        page.evaluate("WS.goToSlide(1, 2)")
        self.assertTrue(page.evaluate(visible, '[id$="m-frac-step"]'))

    GEOMETRY_JS = """async () => {
        const doc = new DOMParser().parseFromString(await (await fetch('figures/math.svg')).text(), 'image/svg+xml');
        const svg = document.querySelector('svg[data-ws-original-img]');
        const out = {};
        for (const orig of doc.querySelectorAll('text[id^="m-"]')) {
            const live = svg.querySelector('[id$="' + orig.id + '"]');
            if (!live || orig.querySelector('tspan[x]') || orig.hasAttribute('transform')) continue;
            // Where the browser puts the alphabetic baseline for the original
            // attributes (dominant-baseline, dy), against where the engine did.
            const probe = (y, attrs) => {
                const p = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                attrs.forEach(a => p.setAttribute(a.name, a.value));
                p.removeAttribute('id'); p.setAttribute('x', '0'); p.setAttribute('y', y);
                p.style.textAnchor = 'start'; p.textContent = 'x';
                live.parentNode.appendChild(p);
                const b = p.getBBox(); p.remove(); return b;
            };
            const fromOriginal = probe(orig.getAttribute('y'), Array.from(orig.attributes));
            const fromEngine = probe(live.getAttribute('y'), Array.from(orig.attributes).filter(a => a.name !== 'dy' && a.name !== 'dominant-baseline'));
            const box = live.getBBox();
            out[orig.id] = { anchor: orig.getAttribute('text-anchor') || 'start', x: Number(orig.getAttribute('x')),
                left: box.x, right: box.x + box.width, baselineError: fromEngine.y - fromOriginal.y };
        }
        return out;
    }"""

    def check_geometry(self, page, label):
        geo = page.evaluate(self.GEOMETRY_JS)
        self.assertGreaterEqual(len(geo), 6, label)
        for name, g in geo.items():
            self.assertAlmostEqual(0, g["baselineError"], delta=0.6, msg=f"{label} {name} baseline")
            if g["anchor"] == "middle":
                self.assertAlmostEqual(g["x"], (g["left"] + g["right"]) / 2, delta=2.5, msg=f"{label} {name} centre")
            elif g["anchor"] == "end":
                self.assertAlmostEqual(g["x"], g["right"], delta=3, msg=f"{label} {name} end")
            else:
                self.assertAlmostEqual(g["x"], g["left"], delta=3, msg=f"{label} {name} start")

    def test_svg_math_keeps_anchor_and_baseline_in_chromium(self):
        self.check_geometry(self.open_math(), "chromium")

    def test_svg_math_keeps_anchor_and_baseline_in_webkit(self):
        try:
            webkit = self._pw.webkit.launch()
        except Exception as exc:  # noqa: BLE001 - WebKit build not installed
            self.skipTest(f"Playwright WebKit unavailable: {exc}")
        self.addCleanup(webkit.close)
        self.check_geometry(self.open_math(browser=webkit, viewport=(1000, 563)), "webkit")

    def test_inline_svg_math_keeps_authored_source(self):
        page = self.open_math()
        info = page.evaluate("""() => { const svg = document.querySelector('#inline-svg');
            const t = document.querySelector('#i-math');
            return { text: t.textContent, html: t.querySelector('span') !== null,
                     original: svg.getAttribute('data-ws-original-html') }; }""")
        self.assertNotIn("$", info["text"])
        self.assertFalse(info["html"], "no HTML left inside SVG text")
        self.assertIn("inline $\\mu_t \\le \\bar\\mu$ svg", info["original"])
        self.assertIn('<tspan data-stat="delta">D</tspan>', info["original"])
        self.assertEqual("stat −0.061 here", self.text_of(page, "#i-stat"))

    # ------------------------------------------------------------ editors
    SERIALIZE_JS = """async () => {
        await new Promise(res => { const s = document.createElement('script'); s.src = '/presto/wysiwyg.js';
            s.onload = res; document.head.appendChild(s); });
        const doc = new DOMParser().parseFromString(await (await fetch(location.href)).text(), 'text/html');
        const authored = Array.from(doc.querySelectorAll('.ws-deck > section.slide'));
        return WS.slides.map((s, i) => [authored[i].innerHTML, window.__prestoWysiwyg.serializeSection(s)]);
    }"""

    def test_presto_saves_the_authored_source(self):
        page = self.open_math()
        for i, (authored, saved) in enumerate(page.evaluate(self.SERIALIZE_JS)):
            if i == 4:  # the deck's own auto-rendered $x^2$ comes back wrapped in a <span> (Presto, not this feature)
                saved = saved.replace("<span>$x^2$</span>", "$x^2$")
            self.assertEqual(authored, saved, f"slide {i + 1}")

    def test_slides_swapped_in_by_an_editor_are_filled_again(self):
        page = self.open_math()
        page.evaluate("""async () => {
            const doc = new DOMParser().parseFromString(await (await fetch(location.href)).text(), 'text/html');
            const slides = doc.querySelectorAll('.ws-deck > section.slide');
            for (const i of [0, 2]) {
                window.replacePrestoEditSlideHTML(i, slides[i].outerHTML);
                // Presto re-runs the deck's KaTeX auto-render on the new slide
                renderMathInElement(WS.slides[i], { delimiters: [{left: '$$', right: '$$', display: true}, {left: '$', right: '$', display: false}] });
            }
        }""")
        page.wait_for_function("document.querySelector('#i-math') && !document.querySelector('#i-math').textContent.includes('$')"
                               " && document.querySelector('#s-count').textContent === '4,051'", timeout=10000)
        self.assertFalse(page.evaluate("!!document.querySelector('#i-math span')"))
        self.assertEqual("stat −0.061 here", self.text_of(page, "#i-stat"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""KaTeX inside zoomed slides, in the system WebKit that Presto uses.

The system WebKit misplaces KaTeX sub- and superscripts whenever a slide is
shrunk with CSS zoom below about 0.85 (Chromium and Playwright's WebKit do
not). presto/edit-layout.js makes KaTeX lay out at an effective zoom of 1.
This test compiles tests/engine-redesign-wkharness.swift (an offscreen,
never-activated WKWebView), loads tests/fixtures/engine-redesign-katex with
the vendored KaTeX, applies PrestoLayout at slide scales 0.5 to 1.6 and
checks every glyph box of inline maths, display maths, \\tau_\\alpha^2,
\\mathbb{E}[\\gamma_j], fractions, roots, a table cell and a slide patched in
after load against the same maths at scale 1.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "engine-redesign-katex" / "index.html"
HARNESS_SRC = REPO / "tests" / "engine-redesign-wkharness.swift"
LAYOUT_JS = REPO / "presto" / "edit-layout.js"
sys.path.insert(0, str(REPO / "tools"))

from _common import DeckServer  # noqa: E402

SCALES = [0.5, 0.62, 0.75, 0.9, 1.3, 1.6]
# Largest glyph shift on screen, CSS px. Pixel snapping alone gives up to
# about 2.6 px (WebKit's own layout at 1.6); the bug moved the worst glyph of
# every formula set 17 to 26 px and every single formula at least 4.5 px.
MAX_SHIFT_PX = 3.0
MAX_DROP_CHANGE = 0.08  # subscript drop / base height (the bug: 0.22 to 0.29)

PROBE_JS = r"""
await WS.ready;
await document.fonts.ready;
const scales = %(scales)s;
const ids = ['m-tau', 'm-expect', 'm-frac', 'm-root', 'm-hat', 'm-step', 'm-display', 'm-display-frac', 'm-table'];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const apply = s => PrestoLayout.apply({ mode: 'audience', viewportWidth: 1280 * s, viewportHeight: 720 * s,
    insetLeft: 0, insetRight: 0, insetTop: 0, insetBottom: 0, padding: 0 });
function leaves(k) {
  return Array.from(k.querySelectorAll('.katex-html *')).filter(el =>
    Array.from(el.childNodes).some(n => n.nodeType === 3 && n.data.trim()));
}
// Glyph boxes relative to each formula, in slide px.
function signature(host, scale) {
  return Array.from(host.querySelectorAll('.katex')).map(k => {
    const o = k.querySelector('.katex-html').getBoundingClientRect();
    return leaves(k).map(el => { const r = el.getBoundingClientRect();
      return [(r.left - o.left) / scale, (r.top - o.top) / scale, (r.bottom - o.top) / scale]; });
  });
}
function shift(sig, ref, scale) {
  let worst = 0;
  sig.forEach((boxes, i) => {
    if (!ref[i] || ref[i].length !== boxes.length) { worst = Infinity; return; }
    boxes.forEach((b, j) => { for (let c = 0; c < 3; c++) worst = Math.max(worst, Math.abs(b[c] - ref[i][j][c]) * scale); });
  });
  return worst;
}
function drop(host) {
  const k = host.querySelector('.katex');
  const base = k.querySelector('.base .mord.mathnormal');
  const parts = Array.from(k.querySelector('.msupsub').querySelectorAll('.vlist .sizing, .vlist .mord'))
    .filter(e => e.textContent.trim()).map(e => e.getBoundingClientRect());
  const b = base.getBoundingClientRect();
  const sub = parts.reduce((a, r) => (r.bottom > a.bottom ? r : a));
  return (sub.bottom - b.bottom) / b.height;
}
function measureAll(scale) {
  const out = { sig: {}, drop: drop(document.getElementById('m-tau')) };
  ids.forEach(id => { out.sig[id] = signature(document.getElementById(id), scale); });
  return out;
}
WS.goToSlide(0, 'end');
apply(1); await sleep(120);
const ref = measureAll(1);
const result = { ua: navigator.userAgent, refDrop: ref.drop, scales: {}, formulas: ids.map(id => ref.sig[id].length) };
for (const s of scales) {
  const g = apply(s); await sleep(120);
  const m = measureAll(g.scale);
  const per = {};
  ids.forEach(id => { per[id] = +shift(m.sig[id], ref.sig[id], g.scale).toFixed(2); });
  result.scales[s] = { scale: g.scale, drop: +m.drop.toFixed(3), shift: per,
    katexZoomVar: document.querySelector('.ws-deck').style.getPropertyValue('--ws-katex-zoom') };
}
// For the record (not asserted): the same maths at 0.75 without the compensation.
apply(0.75); await sleep(50);
const deck = document.querySelector('.ws-deck');
deck.style.removeProperty('--ws-katex-zoom'); deck.style.removeProperty('--ws-katex-unzoom');
await sleep(120);
const raw = measureAll(0.75);
result.uncompensated075 = { drop: +raw.drop.toFixed(3),
  worst: +Math.max.apply(null, ids.map(id => shift(raw.sig[id], ref.sig[id], 0.75))).toFixed(2) };
// A slide patched in after load (code-pane save, Refine): maths rendered into
// the replacement is compensated with no further call.
apply(1); await sleep(50);
WS.goToSlide(2, 0);
const patchRef = drop(document.getElementById('m-patch'));
apply(0.62); await sleep(50);
WS.replacePrestoEditSlideHTML(2, '<section class="slide content" id="patched"><h2>Patched later</h2><p id="m-patch">$\\sigma_\\kappa^2$ and $\\tau_\\alpha^2$</p></section>');
WS.goToSlide(2, 0); await sleep(120);
const patched = document.getElementById('m-patch');
apply(1); await sleep(120);
const patchedRef1 = drop(patched);
apply(0.62); await sleep(120);
result.patched = { rendered: patched.querySelectorAll('.katex').length, drop062: +drop(patched).toFixed(3),
  drop1: +patchedRef1.toFixed(3), originalDrop1: +patchRef.toFixed(3) };
return JSON.stringify(result);
"""


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "needs macOS and swiftc")
class SystemWebKitKatexZoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.harness = Path(cls.tmp.name) / "wkharness"
        subprocess.run(["swiftc", "-framework", "WebKit", "-framework", "Cocoa", str(HARNESS_SRC), "-o", str(cls.harness)],
                       check=True, capture_output=True, text=True, timeout=300)
        script = Path(cls.tmp.name) / "probe.js"
        script.write_text(PROBE_JS % {"scales": json.dumps(SCALES)}, encoding="utf-8")
        with DeckServer(FIXTURE) as server:
            proc = subprocess.run([str(cls.harness), "--url", server.url, "--script", str(script), "--inject", str(LAYOUT_JS),
                                   "--width", "2100", "--height", "1200", "--block-network", "--timeout", "90"],
                                  capture_output=True, text=True, timeout=120)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        if line is None:
            raise AssertionError(f"harness failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr[-2000:]}")
        cls.result = json.loads(line[len("RESULT "):])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_probe_rendered(self):
        self.assertTrue(all(n >= 1 for n in self.result["formulas"]), self.result["formulas"])
        self.assertGreater(self.result["refDrop"], 0.2, "subscript sits below the baseline at scale 1")

    def test_glyphs_keep_their_scale_one_positions(self):
        for s, r in self.result["scales"].items():
            for probe, px in r["shift"].items():
                self.assertLessEqual(px, MAX_SHIFT_PX, f"scale {s}: {probe} glyphs moved {px}px")

    def test_subscripts_keep_their_drop(self):
        for s, r in self.result["scales"].items():
            self.assertAlmostEqual(self.result["refDrop"], r["drop"], delta=MAX_DROP_CHANGE, msg=f"scale {s}")

    def test_compensation_only_below_one(self):
        for s, r in self.result["scales"].items():
            if float(s) < 1:
                self.assertAlmostEqual(float(s), float(r["katexZoomVar"]), places=6)
            else:
                self.assertEqual("", r["katexZoomVar"], f"scale {s}")

    def test_patched_slide_maths_is_compensated(self):
        p = self.result["patched"]
        self.assertEqual(2, p["rendered"], "the engine patcher renders $...$")
        self.assertAlmostEqual(p["drop1"], p["drop062"], delta=MAX_DROP_CHANGE)

    def test_report(self):
        # Printed for the record: the uncompensated layout in this WebKit.
        print(f"\n  system WebKit: scale-1 drop {self.result['refDrop']:.3f}; "
              f"uncompensated at 0.75: drop {self.result['uncompensated075']['drop']}, "
              f"worst glyph shift {self.result['uncompensated075']['worst']}px; "
              + "; ".join(f"{s}: drop {r['drop']} worst {max(r['shift'].values())}px" for s, r in self.result["scales"].items()))


if __name__ == "__main__":
    unittest.main()

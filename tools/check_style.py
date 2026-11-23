#!/usr/bin/env python3
"""Check a WebSlides deck against Duncan's slide-style rules.

Loads the real deck in headless Chromium (served over local HTTP, waiting on
``WS.ready`` with a fallback for older engines), walks every slide at every
reveal step through the engine, and reports per slide and step:

errors (exit 1)
  text-size       text rendered below 14px on a main slide (effective size,
                  after table scaling, CSS transforms and SVG viewBox scaling)
  clipped         text cut off inside an overflow:hidden / scrolling box
  footer-overlap  content drawn over the footer text or slide number
  button-overlap  content under the pinned appendix buttons (.header-links)
                  or appendix back link (.ws-back-link), bottom right
  load-error      a table or inline SVG failed to load (WS.loadErrors)
  katex-error     KaTeX could not render a formula (.katex-error)

warnings (exit 1 only with --strict)
  text-size       main-slide text at 14-20px outside the small-text allowlist
                  (.footnote .cite .source .table-note sup sub and appendix /
                  header links); appendix text below 14px
  words-slide     too many words visible at once on a main slide
  words-bullet    a bullet that is a sentence, not a headline
  acronym         an acronym used before it is spelled out in the deck
  top-heavy       a large empty band below the content of a content slide
  low-res-image   a raster image shown at fewer than 1.5 image pixels per
                  slide pixel (blurry on a 1920px projector)
  font-script     the deck shrinks fonts with its own script (hides overflow)

notes (never fail)
  data-steps      declared data-steps that are stale or unneeded

Usage:
    python3 tools/check_style.py path/to/deck/index.html
    python3 tools/check_style.py path/to/deck --slides 3,7-9,appendix-id
    python3 tools/check_style.py path/to/deck --json
    python3 tools/check_style.py path/to/deck --strict      # warnings fail too

A deck can declare acronyms its audience knows on the deck container:
    <div class="ws-deck" data-known-acronyms="CSAE, BREAD, MHM">
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __name__ == "__main__":
    from _common import ensure_tool_python

    ensure_tool_python(("playwright",))

from _common import (  # noqa: E402
    DECK_STRUCTURE_JS,
    LOAD_ERRORS_JS,
    SLIDE_HEIGHT,
    SLIDE_INFO_JS,
    SLIDE_WIDTH,
    DeckServer,
    check_deck_file,
    engine_js,
    format_load_error,
    parse_slide_selection,
    raise_for_page_errors,
    resolve_deck_path,
    validate_browser_deck_structure,
    wait_for_deck_ready_sync,
)

# ---------------------------------------------------------------- thresholds
# Contract C4: main-slide text >= 20px except the allowlist, which must still be
# >= 14px; appendix text >= 14px.
MAIN_FLOOR_PX = 20.0
HARD_FLOOR_PX = 14.0
APPENDIX_FLOOR_PX = 14.0
SIZE_TOLERANCE_PX = 0.25
SMALL_TEXT_ALLOWLIST = (
    ".footnote, .cite, .source, .table-note, sup, sub, "
    ".ws-appendix-link, .appendix-links, .header-links"
)

# Density. Words exclude the title, tables, SVG charts, maths and small-text
# notes. Calibrated on Duncan's decks (2026-10-27): in the decks he called well
# balanced the densest main slide shows 69 words (mada-bread-2026) and 66
# (long-run-tcd), the longest bullet 19 and 15 words; the dense decks reach 114
# words / 50-word bullets (mada-princeton-kt-2026) and 91 / 24
# (mada-novafrica-2026). The limits are the first round numbers above the
# well-balanced maxima, so those decks pass and the dense tails are flagged.
WORDS_PER_SLIDE_WARN = 70
WORDS_PER_BULLET_WARN = 20

# Top-heavy: empty band under the content, as a share of the space between
# the title and the usable bottom of the slide.
EMPTY_BAND_RATIO_WARN = 0.40
EMPTY_BAND_MIN_PX = 160

# Raster images: image pixels per slide pixel (1920px projector / 1280px slide).
MIN_IMAGE_PIXEL_RATIO = 1.5
MIN_IMAGE_CHECK_PX = 48

KNOWN_ACRONYMS = frozenset("""
    RCT RCTS GDP GNP US USA UK EU UN NGO NGOS OLS IV TV OK ID PDF HIV AIDS COVID
    WHO IMF OECD UNICEF CEO CEOS AI USD EUR GBP PM AM DC NYC FAQ PHD MBA BA BSC
    MSC MP MPS NHS IQ ICT IT SMS GPS DNA
""".split())
# Upper-case words that are emphasis or numerals, not acronyms.
NOT_ACRONYMS = frozenset("""
    I II III IV V VI VII VIII IX X XI XII XIII XIV XV XX
    A AN AND OR NOT NO YES THE BUT ALL ONLY NOW NEW BIG MORE LESS VERY MUST WHY
    HOW WHAT WHEN WHERE IS ARE WAS DO DOES DID CAN WILL ON OFF IN OUT OF TO FOR
    AT BY UP SO IF AS BE WE YOU OUR MY ME HE SHE IT ITS TOO BOTH EACH NONE
""".split())
ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6})s?(?![A-Za-z0-9])")
SKIP_ACRONYM_SLIDE_CLASSES = {"title", "thanks"}
NON_CONTENT_CLASSES = {"title", "section-header", "subsection-header", "signpost", "thanks", "photo", "split-image",
                       "outline"}

# Inline deck scripts that assign font sizes (the auto-shrink pattern).
FONT_SCRIPT_RE = re.compile(
    r"\.style\.fontSize\s*=|setProperty\(\s*['\"]font-size|\.style\.font\s*=",
)
INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# Records inline font-size changes made by deck scripts after parsing.
FONT_WATCH_INIT_JS = r"""(() => {
    const changes = [];
    window.__wsFontChanges = changes;
    const fontOf = text => {
        const m = /(?:^|;)\s*font-size\s*:\s*([^;]+)/i.exec(text || '');
        return m ? m[1].trim() : '';
    };
    const observer = new MutationObserver(mutations => {
        for (const m of mutations) {
            const el = m.target;
            if (!(el instanceof Element)) continue;
            const before = fontOf(m.oldValue);
            const after = fontOf(el.getAttribute('style'));
            if (after && after !== before) changes.push({ el, from: before, to: after });
        }
    });
    observer.observe(document, { subtree: true, attributes: true, attributeFilter: ['style'], attributeOldValue: true });
})();"""

FONT_CHANGES_JS = r"""() => {
    const slides = (window.WS && Array.isArray(WS.slides)) ? WS.slides : [];
    return (window.__wsFontChanges || []).map(c => {
        const slide = c.el.closest('section.slide');
        const tag = c.el.tagName.toLowerCase();
        const cls = (typeof c.el.className === 'string' ? c.el.className : '').trim().split(/\s+/).filter(Boolean)
            .filter(x => !x.startsWith('ws-')).slice(0, 2).join('.');
        return {
            slideIndex: slides.indexOf(slide),
            element: cls ? tag + '.' + cls : tag,
            from: c.from,
            to: c.to,
        };
    });
}"""

DECK_TEXT_JS = engine_js(r"""
    // Text per slide for the acronym pass (all slides, step-independent).
    // textContent keeps the authored case (CSS text-transform is ignored).
    const EXCLUDE = 'aside.notes, script, style, noscript, template, .katex, code, pre, '
        + '.cite, .source, .footnote, .table-note, .ws-table-load-error, .ws-svg-load-error';
    const deck = document.querySelector('.ws-deck');
    const known = deck ? (deck.getAttribute('data-known-acronyms') || '') : '';
    const slides = wsSlides().map(slide => {
        const parts = [];
        const walker = document.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const parent = node.parentElement;
            if (!parent || parent.closest(EXCLUDE)) continue;
            const t = node.textContent;
            if (/\S/.test(t)) parts.push(t);
        }
        return parts.join(' ').replace(/\s+/g, ' ').trim();
    });
    return { known, slides };
""")

MEASURE_JS = engine_js(r"""
    const ALLOW = cfg.allow;
    const SKIP = 'script, style, noscript, template, aside.notes, .katex-mathml, '
        + '.ws-table-load-error, .ws-svg-load-error, .ws-table-config-error, [class*="presto-"], [id^="presto-"]';
    const WORD_EXCLUDE = 'h1, h2, table, svg, .katex, button, ' + ALLOW;
    const slides = wsSlides();
    const ids = new WeakMap();
    let nextId = 1;
    const idOf = el => { if (!ids.has(el)) ids.set(el, nextId++); return ids.get(el); };
    const round1 = x => Math.round(x * 10) / 10;

    function describe(el, stop) {
        const segs = [];
        let p = el;
        while (p && p !== stop && segs.length < 3) {
            let seg = p.tagName.toLowerCase();
            const cls = (typeof p.className === 'string' ? p.className : (p.className && p.className.baseVal) || '')
                .trim().split(/\s+/).filter(c => c && !c.startsWith('ws-')).slice(0, 2);
            if (p.id && !p.id.startsWith('ws-svg')) seg += '#' + p.id;
            else if (cls.length) seg += '.' + cls.join('.');
            segs.unshift(seg);
            p = p.parentElement;
        }
        return segs.join(' > ');
    }

    function snippet(text) {
        text = (text || '').replace(/\s+/g, ' ').trim();
        return text.length > 60 ? text.slice(0, 57) + '...' : text;
    }

    function countWords(text) {
        const m = (text || '').match(/[A-Za-zÀ-ɏ0-9][A-Za-zÀ-ɏ0-9'’.\-]*/g);
        return m ? m.length : 0;
    }

    function htmlScale(el) {
        let p = el;
        while (p && !(p instanceof HTMLElement && p.offsetWidth > 0)) p = p.parentElement;
        if (!p) return 1;
        const r = p.getBoundingClientRect();
        const s = r.width / p.offsetWidth;
        return Number.isFinite(s) && s > 0 ? s : 1;
    }

    function hasPaint(el, cs) {
        if (el instanceof HTMLImageElement || el instanceof HTMLVideoElement || el instanceof HTMLCanvasElement) return true;
        if (el instanceof SVGSVGElement || el.tagName === 'TABLE') return true;
        const bg = cs.backgroundColor;
        if (bg && bg !== 'transparent' && !/rgba\([^)]*,\s*0\)$/.test(bg)) return true;
        if (cs.backgroundImage && cs.backgroundImage !== 'none') return true;
        return ['Top', 'Bottom', 'Left', 'Right'].some(side =>
            parseFloat(cs['border' + side + 'Width']) > 0 && cs['border' + side + 'Style'] !== 'none');
    }

    function isSvgImage(img) {
        const src = (img.currentSrc || img.getAttribute('src') || '').split(/[?#]/)[0].toLowerCase();
        return src.endsWith('.svg') || src.startsWith('data:image/svg');
    }

    function footerZones() {
        const zones = [];
        if (document.body.classList.contains('ws-bare-slide')) return zones;
        const visible = el => {
            if (!el) return false;
            const cs = getComputedStyle(el);
            return cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.05;
        };
        const footer = document.getElementById('ws-footer');
        if (visible(footer)) {
            [['ws-footer-left', 'footer text'], ['ws-footer-right', 'slide number']].forEach(([id, label]) => {
                const part = document.getElementById(id);
                if (!part || !(part.textContent || '').trim()) return;
                const r = part.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) zones.push({ label, l: r.left - 3, t: r.top - 3, r: r.right + 3, b: r.bottom + 3 });
            });
        }
        const nav = document.getElementById('ws-section-nav');
        if (visible(nav)) {
            nav.querySelectorAll('.ws-section-nav-item').forEach(item => {
                const r = item.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) zones.push({ label: 'section navigation', l: r.left - 2, t: r.top - 2, r: r.right + 2, b: r.bottom + 2 });
            });
        }
        return zones;
    }

    // The theme pins appendix buttons (.header-links) and the appendix back
    // link (.ws-back-link) to the bottom-right corner, outside the flow.
    const PINNED_BUTTONS = '.header-links, .ws-back-link';
    function buttonZones(slide, shown) {
        const zones = [];
        slide.querySelectorAll(PINNED_BUTTONS).forEach(owner => {
            if (owner.parentElement && owner.parentElement.closest(PINNED_BUTTONS)) return;
            const position = getComputedStyle(owner).position;
            if (position !== 'absolute' && position !== 'fixed') return;
            const isBack = owner.matches('.ws-back-link');
            const parts = isBack ? [owner] : Array.from(owner.children);
            parts.forEach(part => {
                if (!shown(part)) return;
                const r = part.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    zones.push({ label: isBack ? 'back button' : 'appendix buttons', l: r.left - 2, t: r.top - 2, r: r.right + 2, b: r.bottom + 2 });
                }
            });
        });
        return zones;
    }

    const intersects = (a, z) => a.left < z.r && a.right > z.l && a.top < z.b && a.bottom > z.t;

    function measureStep(slide, step, acc) {
        const sRect = slide.getBoundingClientRect();
        const sScale = sRect.width / 1280 || 1;
        const rel = y => (y - sRect.top) / sScale;
        const opacityCache = new Map();
        function opacity(el) {
            if (opacityCache.has(el)) return opacityCache.get(el);
            let value;
            if (!el || el === slide) value = 1;
            else {
                const cs = getComputedStyle(el);
                value = cs.display === 'none' ? 0 : parseFloat(cs.opacity) * opacity(el.parentElement);
            }
            opacityCache.set(el, value);
            return value;
        }
        const skipCache = new Map();
        const skipped = el => {
            if (!skipCache.has(el)) skipCache.set(el, !!el.closest(SKIP));
            return skipCache.get(el);
        };

        // ---- visible text runs
        const runs = [];
        const range = document.createRange();
        const walker = document.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const text = node.textContent;
            if (!/\S/.test(text)) continue;
            const el = node.parentElement;
            if (!el || skipped(el)) continue;
            const cs = getComputedStyle(el);
            if (cs.visibility !== 'visible' || opacity(el) < 0.05) continue;
            range.selectNodeContents(node);
            let rects = Array.from(range.getClientRects()).filter(r => r.width > 0.5 && r.height > 0.5);
            if (!rects.length && el instanceof SVGElement) {
                const r = el.getBoundingClientRect();
                if (r.width > 0.5 && r.height > 0.5) rects = [r];
            }
            if (!rects.length) continue;
            runs.push({ node, el, text, rects });
        }

        // ---- effective font size
        for (const run of runs) {
            const el = run.el;
            let size;
            let kind = 'html';
            const katexRoot = el.closest('.katex');
            if (katexRoot) {
                // Sub/superscripts inside a formula follow the formula's base size.
                size = parseFloat(getComputedStyle(katexRoot).fontSize) * htmlScale(katexRoot) / sScale;
                kind = 'math';
            } else if (el instanceof SVGElement) {
                const ctm = el.getScreenCTM && el.getScreenCTM();
                const scale = ctm ? Math.sqrt(Math.abs(ctm.a * ctm.d - ctm.b * ctm.c)) : 1;
                size = parseFloat(getComputedStyle(el).fontSize) * scale / sScale;
                kind = 'svg';
            } else {
                size = parseFloat(getComputedStyle(el).fontSize) * htmlScale(el) / sScale;
            }
            if (!Number.isFinite(size) || size >= cfg.reportBelow) continue;
            const target = katexRoot || el;
            const key = idOf(target) + ':' + round1(size);
            let f = acc.fonts.get(key);
            if (!f) {
                f = {
                    size: round1(size), kind,
                    allowlisted: !!target.closest(ALLOW),
                    element: describe(target, slide),
                    text: snippet(katexRoot ? katexRoot.textContent : run.text),
                    steps: [],
                };
                acc.fonts.set(key, f);
            }
            if (!f.steps.includes(step)) f.steps.push(step);
        }

        // ---- words visible at once, and per bullet
        let words = 0;
        const bulletWords = new Map();
        for (const run of runs) {
            if (run.el.closest(WORD_EXCLUDE)) continue;
            const n = countWords(run.text);
            words += n;
            const li = run.el.closest('li');
            if (li && slide.contains(li)) bulletWords.set(li, (bulletWords.get(li) || 0) + n);
        }
        acc.words[step] = words;
        bulletWords.forEach((n, li) => {
            const key = idOf(li);
            const prev = acc.bullets.get(key);
            if (!prev || n > prev.words) {
                const own = Array.from(li.childNodes)
                    .filter(c => !(c.nodeType === 1 && /^(UL|OL)$/.test(c.tagName)))
                    .map(c => c.textContent).join(' ');
                acc.bullets.set(key, { words: n, text: snippet(own), element: describe(li, slide), steps: prev ? prev.steps : [] });
            }
            const b = acc.bullets.get(key);
            if (!b.steps.includes(step)) b.steps.push(step);
        });

        // ---- clipped text inside overflow:hidden / scrolling boxes (not the slide itself)
        for (const box of slide.querySelectorAll('*')) {
            if (!(box instanceof HTMLElement) || skipped(box) || box.closest('.katex')) continue;
            const cs = getComputedStyle(box);
            const clipX = cs.overflowX !== 'visible';
            const clipY = cs.overflowY !== 'visible';
            if (!clipX && !clipY) continue;
            if (!(box.scrollHeight > box.clientHeight + 1 || box.scrollWidth > box.clientWidth + 1)) continue;
            if (opacity(box) < 0.05 || cs.visibility !== 'visible') continue;
            const r = box.getBoundingClientRect();
            const sx = box.offsetWidth ? r.width / box.offsetWidth : 1;
            const sy = box.offsetHeight ? r.height / box.offsetHeight : 1;
            const inner = {
                l: r.left + box.clientLeft * sx, t: r.top + box.clientTop * sy,
                r: r.left + (box.clientLeft + box.clientWidth) * sx, b: r.top + (box.clientTop + box.clientHeight) * sy,
            };
            let count = 0, worst = 0, sample = '';
            for (const run of runs) {
                if (!box.contains(run.el)) continue;
                let over = 0;
                for (const rc of run.rects) {
                    if (clipY) over = Math.max(over, rc.bottom - inner.b, inner.t - rc.top);
                    if (clipX) over = Math.max(over, rc.right - inner.r, inner.l - rc.left);
                }
                if (over > 2) {
                    count++;
                    if (over > worst) { worst = over; sample = snippet(run.text); }
                }
            }
            if (!count) continue;
            const key = idOf(box);
            const prev = acc.clipped.get(key);
            const entry = prev || { element: describe(box, slide), count: 0, overPx: 0, text: sample, steps: [] };
            entry.count = Math.max(entry.count, count);
            if (worst > entry.overPx) { entry.overPx = Math.round(worst); entry.text = sample; }
            if (!entry.steps.includes(step)) entry.steps.push(step);
            acc.clipped.set(key, entry);
        }

        // ---- overlap with the footer / slide number / section nav, or with
        // the pinned appendix and back buttons
        const shown = el => opacity(el) >= 0.05 && getComputedStyle(el).visibility === 'visible';
        const zones = footerZones();
        const buttons = buttonZones(slide, shown);
        if (zones.length || buttons.length) {
            const boxes = runs.map(run => ({ el: run.el, rects: run.rects, text: run.text }));
            slide.querySelectorAll('img, video, canvas').forEach(el => {
                if (skipped(el) || !shown(el)) return;
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) boxes.push({ el, rects: [r], text: '(' + el.tagName.toLowerCase() + ')' });
            });
            const record = (map, box, z) => {
                const key = idOf(box.el) + ':' + z.label;
                const entry = map.get(key) || { zone: z.label, element: describe(box.el, slide), text: snippet(box.text), steps: [] };
                if (!entry.steps.includes(step)) entry.steps.push(step);
                map.set(key, entry);
            };
            const hit = (box, list) => list.find(z => box.rects.some(rc => intersects(rc, z)));
            for (const box of boxes) {
                const footerZone = hit(box, zones);
                if (footerZone) record(acc.overlap, box, footerZone);
                const buttonZone = box.el.closest(PINNED_BUTTONS) ? null : hit(box, buttons);
                if (buttonZone) record(acc.buttonOverlap, box, buttonZone);
            }
        }

        // ---- lowest content (for the top-heavy check)
        const allowed = el => !!el.closest(ALLOW);
        for (const run of runs) {
            const bottom = Math.max(...run.rects.map(rc => rel(rc.bottom)));
            const top = Math.min(...run.rects.map(rc => rel(rc.top)));
            if (allowed(run.el)) acc.noteTop = Math.min(acc.noteTop, top);
            else acc.contentBottom = Math.max(acc.contentBottom, bottom);
        }
        for (const el of slide.querySelectorAll('*')) {
            if (skipped(el) || el.closest('svg') && !(el instanceof SVGSVGElement)) continue;
            if (opacity(el) < 0.05) continue;
            const cs = getComputedStyle(el);
            if (cs.visibility !== 'visible' || !hasPaint(el, cs)) continue;
            const r = el.getBoundingClientRect();
            if (r.width <= 1 || r.height <= 1) continue;
            if (allowed(el)) acc.noteTop = Math.min(acc.noteTop, rel(r.top));
            else acc.contentBottom = Math.max(acc.contentBottom, rel(r.bottom));
        }
        const footer = document.getElementById('ws-footer');
        if (footer && zones.some(z => z.label !== 'section navigation')) {
            acc.footerTop = Math.min(acc.footerTop, rel(footer.getBoundingClientRect().top));
        }

        // ---- raster image resolution
        slide.querySelectorAll('img').forEach(img => {
            if (skipped(img) || isSvgImage(img) || !img.naturalWidth || !img.naturalHeight) return;
            if (opacity(img) < 0.05 || getComputedStyle(img).visibility !== 'visible') return;
            const r = img.getBoundingClientRect();
            const w = r.width / sScale, h = r.height / sScale;
            if (w < cfg.minImagePx && h < cfg.minImagePx) return;
            const fit = getComputedStyle(img).objectFit;
            const rx = img.naturalWidth / w, ry = img.naturalHeight / h;
            const ratio = (fit === 'contain' || fit === 'scale-down') ? Math.max(rx, ry) : (fit === 'none' ? 1 : Math.min(rx, ry));
            const key = idOf(img);
            const entry = acc.images.get(key) || {
                src: img.getAttribute('src') || '', natural: [img.naturalWidth, img.naturalHeight],
                shown: [Math.round(w), Math.round(h)], fit, ratio: Infinity, steps: [],
            };
            entry.ratio = Math.min(entry.ratio, Math.round(ratio * 100) / 100);
            if (!entry.steps.includes(step)) entry.steps.push(step);
            acc.images.set(key, entry);
        });
    }

    const out = [];
    for (const index of cfg.indexes) {
        const slide = slides[index];
        const maxStep = cfg.maxSteps[index] || 0;
        const acc = {
            fonts: new Map(), words: [], bullets: new Map(), clipped: new Map(), overlap: new Map(),
            buttonOverlap: new Map(), images: new Map(),
            contentBottom: -Infinity, noteTop: Infinity, footerTop: Infinity,
        };
        for (let step = 0; step <= maxStep; step++) {
            WS.goToSlide(index, step);
            measureStep(slide, step, acc);
        }
        const cs = getComputedStyle(slide);
        const sRect = slide.getBoundingClientRect();
        const sScale = sRect.width / 1280 || 1;
        const heading = slide.querySelector(':scope > h2, :scope > h1');
        const titleBottom = heading ? (heading.getBoundingClientRect().bottom - sRect.top) / sScale : parseFloat(cs.paddingTop) || 0;
        let usableBottom = 720 - (parseFloat(cs.paddingBottom) || 0);
        if (Number.isFinite(acc.footerTop)) usableBottom = Math.min(usableBottom, acc.footerTop);
        if (Number.isFinite(acc.noteTop) && acc.noteTop > acc.contentBottom) usableBottom = Math.min(usableBottom, acc.noteTop);
        out.push({
            index,
            fonts: Array.from(acc.fonts.values()),
            words: acc.words,
            bullets: Array.from(acc.bullets.values()),
            clipped: Array.from(acc.clipped.values()),
            overlap: Array.from(acc.overlap.values()),
            buttonOverlap: Array.from(acc.buttonOverlap.values()),
            images: Array.from(acc.images.values()),
            band: {
                titleBottom: Math.round(titleBottom),
                contentBottom: Number.isFinite(acc.contentBottom) ? Math.round(acc.contentBottom) : null,
                usableBottom: Math.round(usableBottom),
            },
            katexErrors: Array.from(slide.querySelectorAll('.katex-error')).map(el => ({
                text: snippet(el.textContent), detail: snippet(el.getAttribute('title') || ''),
            })),
        });
    }
    return out;
""", params="cfg")


# ---------------------------------------------------------------- analysis
def finding(severity, rule, message, slide=None, **detail):
    steps = detail.pop("steps", None)
    return {
        "severity": severity,
        "rule": rule,
        "slide": slide,
        "steps": sorted(steps) if steps else [],
        "message": message,
        "detail": detail,
    }


def format_steps(steps, max_step):
    if not steps or max_step == 0:
        return ""
    steps = sorted(set(steps))
    if steps == list(range(steps[0], max_step + 1)):
        return f"step {steps[0]}" if steps[0] == max_step else f"steps {steps[0]}-{max_step}"
    if len(steps) == 1:
        return f"step {steps[0]}"
    return "steps " + ",".join(str(s) for s in steps)


def is_content_slide(info):
    return not info["isAppendix"] and not (set(info["classes"]) & NON_CONTENT_CLASSES)


def size_findings(info, fonts):
    """text-size: errors below 14px on main slides; warnings 14-20px outside the allowlist."""
    out = []
    groups = {}
    for f in fonts:
        size = f["size"]
        if info["isAppendix"]:
            if size < APPENDIX_FLOOR_PX - SIZE_TOLERANCE_PX:
                groups.setdefault(("warning", "appendix"), []).append(f)
        elif size < HARD_FLOOR_PX - SIZE_TOLERANCE_PX:
            groups.setdefault(("error", "hard"), []).append(f)
        elif size < MAIN_FLOOR_PX - SIZE_TOLERANCE_PX and not f["allowlisted"]:
            groups.setdefault(("warning", "main"), []).append(f)
    for (severity, kind), items in groups.items():
        items.sort(key=lambda f: f["size"])
        lo, hi = items[0]["size"], items[-1]["size"]
        span = f"{lo:g}px" if lo == hi else f"{lo:g}-{hi:g}px"
        if kind == "hard":
            what = f"below the {HARD_FLOOR_PX:g}px minimum"
        elif kind == "main":
            what = f"below {MAIN_FLOOR_PX:g}px on a main slide (only footnotes, citations, sources and table notes may be smaller)"
        else:
            what = f"below the {APPENDIX_FLOOR_PX:g}px appendix minimum"
        examples = "; ".join(f"\"{f['text']}\" {f['size']:g}px <{f['element']}>" for f in items[:3])
        steps = sorted({s for f in items for s in f["steps"]})
        out.append(finding(
            severity, "text-size",
            f"{len(items)} text element(s) at {span}, {what}: {examples}",
            slide=info["index"], steps=steps, elements=items,
        ))
    return out


def density_findings(info, data):
    out = []
    if info["isAppendix"] or "title" in info["classes"] or "thanks" in info["classes"]:
        return out
    words = data["words"]
    if words and max(words) > WORDS_PER_SLIDE_WARN:
        peak = max(words)
        steps = [s for s, n in enumerate(words) if n == peak]
        out.append(finding(
            "warning", "words-slide",
            f"{peak} words visible at once (aim for at most {WORDS_PER_SLIDE_WARN}; split the slide, "
            "cut words, or move detail to an appendix)",
            slide=info["index"], steps=steps, words=peak,
        ))
    long_bullets = [b for b in data["bullets"] if b["words"] > WORDS_PER_BULLET_WARN]
    if long_bullets:
        long_bullets.sort(key=lambda b: -b["words"])
        examples = "; ".join(f"{b['words']} words: \"{b['text']}\"" for b in long_bullets[:3])
        out.append(finding(
            "warning", "words-bullet",
            f"{len(long_bullets)} bullet(s) over {WORDS_PER_BULLET_WARN} words (bullets are headlines, "
            f"not sentences): {examples}",
            slide=info["index"], steps={s for b in long_bullets for s in b["steps"]}, bullets=long_bullets,
        ))
    return out


def layout_findings(info, data):
    out = []
    for c in data["clipped"]:
        out.append(finding(
            "error", "clipped",
            f"text cut off by an overflow:hidden box <{c['element']}> ({c['count']} text run(s), "
            f"up to {c['overPx']}px hidden): \"{c['text']}\"",
            slide=info["index"], **c,
        ))
    for o in data["overlap"]:
        out.append(finding(
            "error", "footer-overlap",
            f"content overlaps the {o['zone']}: <{o['element']}> \"{o['text']}\"",
            slide=info["index"], **o,
        ))
    for o in data["buttonOverlap"]:
        out.append(finding(
            "error", "button-overlap",
            f"content runs under the pinned {o['zone']}: <{o['element']}> \"{o['text']}\"",
            slide=info["index"], **o,
        ))
    band = data["band"]
    if is_content_slide(info) and band["contentBottom"] is not None:
        avail = band["usableBottom"] - band["titleBottom"]
        empty = band["usableBottom"] - band["contentBottom"]
        if (avail > 0 and band["contentBottom"] > band["titleBottom"] and empty >= EMPTY_BAND_MIN_PX
                and empty / avail > EMPTY_BAND_RATIO_WARN):
            out.append(finding(
                "warning", "top-heavy",
                f"the bottom {round(100 * empty / avail)}% of the space below the title is empty "
                f"(content ends at y={band['contentBottom']}px of {band['usableBottom']}px); "
                "centre it with the v-center layout, enlarge the figure, or merge with another slide",
                slide=info["index"], **band,
            ))
    for img in data["images"]:
        if img["ratio"] < MIN_IMAGE_PIXEL_RATIO:
            (nw, nh), (w, h) = img["natural"], img["shown"]
            stretched = img["fit"] == "fill" and h and nh and abs((w / h) / (nw / nh) - 1) > 0.05
            out.append(finding(
                "warning", "low-res-image",
                f"{img['src']} is {nw}×{nh}px shown at {w}×{h}px ({img['ratio']:g} image px per slide px; "
                f"use at least {MIN_IMAGE_PIXEL_RATIO:g}, or a vector SVG)"
                + (" and it is stretched out of its aspect ratio" if stretched else ""),
                slide=info["index"], **img,
            ))
    for err in data["katexErrors"]:
        out.append(finding(
            "error", "katex-error",
            f"KaTeX could not render \"{err['text']}\"" + (f" ({err['detail']})" if err["detail"] else ""),
            slide=info["index"], **err,
        ))
    return out


def acronym_definitions(text, acronym):
    esc = re.escape(acronym)
    patterns = (
        rf"[A-Za-z][\w'’\-]*(?:\s+[\w'’\-]+){{1,8}}\s*\(\s*{esc}s?\s*\)",  # words (ACR)
        rf"(?<![A-Za-z0-9]){esc}s?\s*\(\s*[A-Za-z][^()]*\s[^()]*\)",  # ACR (spelled out)
        rf"(?<![A-Za-z0-9]){esc}s?\s*(?:=|:|—|–)\s*[a-z]",  # ACR = spelled out
    )
    return any(re.search(p, text) for p in patterns)


def acronym_findings(slide_infos, texts, deck_known, selected):
    """First use of each acronym on a main slide before it is spelled out."""
    known = KNOWN_ACRONYMS | {a.upper() for a in deck_known}
    defined = set()
    first_use = {}
    uses = {}
    for info, text in zip(slide_infos, texts):
        found = [m.group(1) for m in ACRONYM_RE.finditer(text)]
        candidates = [a for a in dict.fromkeys(found) if a not in NOT_ACRONYMS and a not in known]
        for acronym in candidates:
            if acronym in defined:
                continue
            if acronym_definitions(text, acronym):
                defined.add(acronym)
                continue
            if info["isAppendix"] or set(info["classes"]) & SKIP_ACRONYM_SLIDE_CLASSES:
                continue
            uses.setdefault(acronym, []).append(info["index"])
            first_use.setdefault(acronym, info["index"])
    out = []
    for acronym, index in first_use.items():
        if index not in selected:
            continue
        later = [i + 1 for i in uses[acronym] if i != index]
        also = f"; also on slide(s) {', '.join(map(str, later[:6]))}" if later else ""
        out.append(finding(
            "warning", "acronym",
            f"\"{acronym}\" is used before it is spelled out (on first use write the words with "
            f"\"({acronym})\" after them, use plain words instead, or list it in the deck's "
            f"data-known-acronyms){also}",
            slide=index, acronym=acronym, slides=[i + 1 for i in uses[acronym]],
        ))
    return out


def font_script_findings(deck_html, changes):
    blocks = []
    for m in INLINE_SCRIPT_RE.finditer(deck_html):
        if FONT_SCRIPT_RE.search(m.group(1)):
            start = deck_html.count("\n", 0, m.start()) + 1
            end = deck_html.count("\n", 0, m.end()) + 1
            blocks.append((start, end))
    changed = [c for c in changes if isinstance(c.get("slideIndex"), int) and c["slideIndex"] >= 0]
    if not blocks and not changed:
        return []
    parts = []
    if blocks:
        parts.append("index.html lines " + ", ".join(f"{a}-{b}" for a, b in blocks))
    if changed:
        slides = sorted({c["slideIndex"] + 1 for c in changed})
        sizes = sorted({c["to"] for c in changed})
        parts.append(f"set font-size {', '.join(sizes[:4])} on slide(s) {', '.join(map(str, slides[:12]))}")
    return [finding(
        "warning", "font-script",
        "the deck shrinks fonts with its own script (" + "; ".join(parts) + "). It hides overflow "
        "instead of fixing it and makes text smaller than the theme intends; remove it and split or "
        "cut the slides it shrinks",
        script_lines=blocks, changes=changed,
    )]


def data_steps_findings(slide_infos):
    declared = [(info, info["declaredSteps"]) for info in slide_infos if info.get("declaredSteps") is not None]
    if not declared:
        return []
    stale = [(info, raw) for info, raw in declared if not (raw.isdigit() and int(raw) == info["maxStep"])]
    detail = ", ".join(f"slide {info['index'] + 1} declares {raw!r}, engine counts {info['maxStep']}"
                       for info, raw in stale[:8])
    message = (f"data-steps is declared on {len(declared)} slide(s); the engine and the tools ignore it, "
               "so it can be deleted")
    if stale:
        message += f". {len(stale)} of them are stale: {detail}"
    return [finding(
        "info", "data-steps", message,
        declared=[info["index"] + 1 for info, _ in declared],
        stale=[{"slide": info["index"] + 1, "declared": raw, "engine": info["maxStep"]} for info, raw in stale],
    )]


def load_error_findings(load_errors):
    return [finding("error", "load-error", format_load_error(err),
                    slide=err.get("slideIndex") if isinstance(err.get("slideIndex"), int) else None, **err)
            for err in load_errors]


def merge_repeated_overlaps(findings, min_slides=3):
    """One deck-wide finding for the same footer overlap on many slides (a shared-layout problem)."""
    groups = {}
    for f in findings:
        if f["rule"] == "footer-overlap":
            d = f["detail"]
            groups.setdefault((d["zone"], d["element"], d["text"]), []).append(f)
    merged = []
    for (zone, element, text), items in groups.items():
        if len(items) < min_slides:
            continue
        slides = [f["slide"] + 1 for f in items]
        listed = ", ".join(map(str, slides[:12])) + (", ..." if len(slides) > 12 else "")
        merged.append(finding(
            "error", "footer-overlap",
            f"content overlaps the {zone} on {len(slides)} slides ({listed}): <{element}> \"{text}\"; "
            "the same element on every slide points to the theme or a shared layout",
            zone=zone, element=element, text=text, slides=slides,
        ))
        for f in items:
            f["merged"] = True
    return [f for f in findings if not f.pop("merged", False)] + merged


def build_report(deck_path, slide_infos, selected, measurements, texts, deck_known, load_errors, font_changes,
                 deck_html):
    findings = []
    by_index = {m["index"]: m for m in measurements}
    for index in selected:
        info = slide_infos[index]
        data = by_index[index]
        findings += size_findings(info, data["fonts"])
        findings += density_findings(info, data)
        findings += layout_findings(info, data)
    findings = merge_repeated_overlaps(findings)
    findings += acronym_findings(slide_infos, texts, deck_known, set(selected))
    findings += [f for f in load_error_findings(load_errors) if f["slide"] is None or f["slide"] in selected]
    findings += font_script_findings(deck_html, font_changes)
    findings += data_steps_findings([slide_infos[i] for i in selected])
    findings.sort(key=lambda f: (f["slide"] if f["slide"] is not None else 10 ** 6,
                                 SEVERITY_ORDER[f["severity"]], f["rule"]))
    counts = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITY_ORDER}
    return {
        "deck": str(deck_path),
        "checked": {
            "slides": len(selected),
            "main": sum(1 for i in selected if not slide_infos[i]["isAppendix"]),
            "appendix": sum(1 for i in selected if slide_infos[i]["isAppendix"]),
            "steps": sum(slide_infos[i]["maxStep"] + 1 for i in selected),
        },
        "thresholds": {
            "mainFloorPx": MAIN_FLOOR_PX,
            "hardFloorPx": HARD_FLOOR_PX,
            "appendixFloorPx": APPENDIX_FLOOR_PX,
            "wordsPerSlide": WORDS_PER_SLIDE_WARN,
            "wordsPerBullet": WORDS_PER_BULLET_WARN,
            "emptyBandRatio": EMPTY_BAND_RATIO_WARN,
            "emptyBandMinPx": EMPTY_BAND_MIN_PX,
            "minImagePixelRatio": MIN_IMAGE_PIXEL_RATIO,
        },
        "slides": [
            {
                "index": i,
                "number": i + 1,
                "mainNumber": slide_infos[i]["mainNumber"],
                "id": slide_infos[i]["id"],
                "title": slide_infos[i]["title"],
                "isAppendix": slide_infos[i]["isAppendix"],
                "maxStep": slide_infos[i]["maxStep"],
                "wordsVisible": max(by_index[i]["words"]) if by_index[i]["words"] else 0,
            }
            for i in selected
        ],
        "findings": findings,
        "summary": {"errors": counts["error"], "warnings": counts["warning"], "info": counts["info"]},
    }


# ---------------------------------------------------------------- output
MARK = {"error": "✗ error  ", "warning": "⚠ warning", "info": "· note   "}


def slide_label(info):
    label = f"Slide {info['index'] + 1}"
    if info["isAppendix"]:
        label += " (appendix)"
    if info["id"]:
        label += f" #{info['id']}"
    if info["title"]:
        label += f" \"{info['title'][:70]}\""
    return label


def print_report(report, slide_infos, strict):
    print("\n=== WebSlides Style Check ===\n")
    c = report["checked"]
    print(f"Checked {c['slides']} slide(s) ({c['main']} main, {c['appendix']} appendix), "
          f"{c['steps']} reveal step(s), at {SLIDE_WIDTH}×{SLIDE_HEIGHT}.")
    current = object()
    for f in report["findings"]:
        if f["slide"] != current:
            current = f["slide"]
            print()
            print(slide_label(slide_infos[current]) if current is not None else "Deck-wide")
        max_step = slide_infos[f["slide"]]["maxStep"] if f["slide"] is not None else 0
        steps = format_steps(f["steps"], max_step)
        severity = "error" if strict and f["severity"] == "warning" else f["severity"]
        print(f"  {MARK[severity]} {f['rule']:<14} {steps + ': ' if steps else ''}{f['message']}")
    s = report["summary"]
    print()
    if not report["findings"]:
        print("  ✓ No style problems found.")
    print(f"Summary: {s['errors']} error(s), {s['warnings']} warning(s), {s['info']} note(s)"
          + (" (--strict: warnings count as errors)" if strict and s["warnings"] else "") + ".")
    print()


# ---------------------------------------------------------------- main
def check_style(html_path, slides=None, timeout_ms=None):
    from playwright.sync_api import sync_playwright

    html_path = check_deck_file(resolve_deck_path(html_path))
    deck_html = html_path.read_text(encoding="utf-8", errors="replace")
    with DeckServer(html_path) as served, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT}, device_scale_factor=1)
            page_errors = []
            page.on("pageerror", lambda e: page_errors.append(e))
            page.add_init_script(FONT_WATCH_INIT_JS)
            page.goto(served.url, wait_until="load")
            validate_browser_deck_structure(page.evaluate(DECK_STRUCTURE_JS))
            raise_for_page_errors(page_errors)
            kwargs = {"timeout_ms": timeout_ms} if timeout_ms else {}
            wait_for_deck_ready_sync(page, page_errors, **kwargs)
            page.evaluate("WS.prepareForExport()")
            slide_infos = page.evaluate(SLIDE_INFO_JS)
            selected = parse_slide_selection(slides, slide_infos) if slides else list(range(len(slide_infos)))
            load_errors = page.evaluate(LOAD_ERRORS_JS)
            font_changes = page.evaluate(FONT_CHANGES_JS)
            deck_text = page.evaluate(DECK_TEXT_JS)
            measurements = page.evaluate(MEASURE_JS, {
                "indexes": selected,
                "maxSteps": [s["maxStep"] for s in slide_infos],
                "allow": SMALL_TEXT_ALLOWLIST,
                "reportBelow": MAIN_FLOOR_PX - SIZE_TOLERANCE_PX,
                "minImagePx": MIN_IMAGE_CHECK_PX,
            })
            raise_for_page_errors(page_errors)
        finally:
            browser.close()
    deck_known = [a for a in re.split(r"[\s,;]+", deck_text["known"]) if a]
    report = build_report(html_path, slide_infos, selected, measurements, deck_text["slides"], deck_known,
                          load_errors, font_changes, deck_html)
    return report, slide_infos


def exit_code(report, strict):
    s = report["summary"]
    return 1 if s["errors"] or (strict and s["warnings"]) else 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Check a WebSlides deck against the slide-style rules (text size, density, "
                    "acronyms, clipping, footer and button overlap, balance, image resolution, load errors).",
    )
    ap.add_argument("html", help="Path to deck index.html or deck directory")
    ap.add_argument("--slides", action="append",
                    help="Only these slides: 1-based numbers, ranges (3-7) or ids, comma-separated; repeatable")
    ap.add_argument("--json", dest="json_out", action="store_true", help="Emit machine-readable JSON")
    ap.add_argument("--strict", action="store_true", help="Treat warnings as errors (exit 1)")
    ap.add_argument("--timeout", type=int, default=None, help="Deck readiness timeout in ms (default 30000)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        PlaywrightError = RuntimeError
    try:
        report, slide_infos = check_style(args.html, slides=args.slides, timeout_ms=args.timeout)
    except (OSError, RuntimeError, ValueError, ImportError, PlaywrightError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if args.json_out:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report, slide_infos, args.strict)
    raise SystemExit(exit_code(report, args.strict))


if __name__ == "__main__":
    main()

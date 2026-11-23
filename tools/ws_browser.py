"""Browser half of ``ws``: one headless Chromium and one local server per run.

``run(args)`` handles ``outline``, ``check``, ``diff`` and ``layout``. The deck
is loaded once; checks (style via check_style's in-page measurement, layout
via LAYOUT_JS below, links statically), step descriptions and captures all
reuse that page.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import os
import re
import time
from pathlib import Path

import ws_deck
import ws_steps
from _common import (
    DECK_STRUCTURE_JS,
    LOAD_ERRORS_JS,
    SLIDE_HEIGHT,
    SLIDE_INFO_JS,
    SLIDE_WIDTH,
    DeckServer,
    raise_for_page_errors,
    validate_browser_deck_structure,
    wait_for_deck_ready_sync,
)

CACHE_ROOT = Path(os.environ.get("WEBSLIDES_SHOT_CACHE") or Path.home() / ".cache" / "webslides" / "shots")
OUT_ROOT = Path(os.environ.get("WEBSLIDES_WS_OUT") or "/tmp/ws")
NO_TRANSITIONS_CSS = "*, *::before, *::after { transition: none !important; animation: none !important; }"
DIFF_THRESHOLD = 24     # per-channel difference that counts as a change
DIFF_MARGIN = 12        # px of context around a changed region
DIFF_CELL = 16          # grid cell used to split far-apart changes
LINE_WIDTH = 150

# Navigate, refit tables, finish any engine-driven animation (tools capture end
# states, never a mid-transition frame), then wait two frames.
NAVIGATE_JS = """async ([index, step]) => {
    WS.goToSlide(index, step);
    const state = WS.getState();
    if (!state || state.index !== index || state.step !== step) {
        throw new Error(`navigation mismatch: requested ${index}/${step}, got ${state ? state.index + '/' + state.step : 'none'}`);
    }
    if (typeof WS.refitTables === 'function') await WS.refitTables();
    const finish = () => {
        if (!document.getAnimations) return;
        for (const a of document.getAnimations()) {
            try { a.finish(); } catch (err) { /* infinite animations cannot finish */ }
        }
    };
    finish();
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    finish();
    return true;
}"""

# Shared in-page helpers: labels for elements, effective opacity.
LABEL_HELPERS_JS = r"""
    const SKIP_SEL = 'script, style, noscript, template, aside.notes, .katex-mathml, '
        + '.ws-table-load-error, .ws-svg-load-error, [class*="presto-"], [id^="presto-"]';
    function svgFile(el) {
        const root = el.closest && el.closest('svg[data-ws-original-img]');
        if (!root) return '';
        const m = /\bsrc\s*=\s*["']([^"']+)["']/.exec(root.getAttribute('data-ws-original-img') || '');
        return m ? m[1].split('/').pop() : 'svg';
    }
    function ownText(el) {
        let out = '';
        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode()) && out.length < 80) {
            const p = node.parentElement;
            if (p && p.closest('.katex-mathml, script, style, aside.notes')) continue;
            out += (p instanceof SVGElement && out ? ' ' : '') + node.textContent;
        }
        return out.replace(/[​-‍﻿]/g, '').replace(/\s+/g, ' ').trim();
    }
    function shortDesc(el) {
        let seg = el.tagName.toLowerCase();
        const cls = (typeof el.className === 'string' ? el.className : (el.className && el.className.baseVal) || '')
            .trim().split(/\s+/).filter(c => c && !c.startsWith('ws-')).slice(0, 2);
        if (el.id) seg += '#' + el.id.replace(/^ws-svg\d+-/, '');
        else if (cls.length) seg += '.' + cls.join('.');
        else if (el instanceof SVGElement && el.children.length) {
            // Anonymous SVG group: name it by its most common shape ("g: 12 circle").
            const counts = {};
            el.querySelectorAll('circle, ellipse, rect, line, path, polygon, polyline, text').forEach(c => {
                counts[c.tagName] = (counts[c.tagName] || 0) + 1;
            });
            const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
            if (top) seg += ': ' + top[1] + ' ' + top[0];
        }
        return seg;
    }
    function label(el, withTag) {
        const file = svgFile(el);
        const prefix = file ? file + ':' : '';
        if (file && el.hasAttribute('data-ws-original-img')) return prefix + '<svg>';
        let text = ownText(el);
        if (text.length > 30) text = text.slice(0, 29).trimEnd() + '…';
        if (text) return prefix + '"' + text + '"' + (withTag ? ' <' + shortDesc(el) + '>' : '');
        return prefix + '<' + shortDesc(el) + '>';
    }
    function makeOpacity(slide) {
        const cache = new Map();
        return function opacity(el) {
            if (cache.has(el)) return cache.get(el);
            let value;
            if (!el || el === slide) value = 1;
            else {
                const cs = getComputedStyle(el);
                value = cs.display === 'none' ? 0 : parseFloat(cs.opacity) * opacity(el.parentElement);
            }
            cache.set(el, value);
            return value;
        };
    }
"""

# Per-step reveal changes observed in the live DOM (fallback for engines
# without WS.describeSteps): the classes the engine toggles, step by step.
OBSERVE_STEPS_JS = r"""async ([index, maxStep]) => {
""" + LABEL_HELPERS_JS + r"""
    const slide = WS.slides[index];
    const REVEAL = '[data-step], [data-highlight], [data-dim], [data-class], [data-fade-after], [data-focus]';
    const classEntries = el => {
        const raw = el.getAttribute('data-class') || '';
        const out = [];
        const re = /([^\s:;,]+)\s*:\s*([^\s;,]+)/g;
        let m;
        while ((m = re.exec(raw))) out.push(m[1]);
        return out;
    };
    function snapshot() {
        const state = new Map();
        slide.querySelectorAll(REVEAL).forEach(el => {
            if (el.closest(SKIP_SEL)) return;
            const s = {};
            if (el.hasAttribute('data-step')) s.shown = el.classList.contains('ws-visible');
            if (el.hasAttribute('data-highlight')) s.highlight = el.classList.contains('ws-highlighted');
            if (el.hasAttribute('data-dim')) s.dim = el.classList.contains('ws-dimmed');
            classEntries(el).forEach(c => { s['class:' + c] = el.classList.contains(c); });
            state.set(el, s);
        });
        slide.querySelectorAll('.ws-faded, [data-fade-after], [data-focus] *').forEach(el => {
            if (el.closest(SKIP_SEL)) return;
            const s = state.get(el) || {};
            s.faded = el.classList.contains('ws-faded');
            state.set(el, s);
        });
        return state;
    }
    const go = async step => {
        WS.goToSlide(index, step);
        if (typeof WS.refitTables === 'function') await WS.refitTables();
    };
    const states = [];
    for (let step = 0; step <= maxStep; step++) {
        await go(step);
        states.push(snapshot());
    }
    const VERB = {
        shown: ['+', '-'], highlight: ['highlight ', 'unhighlight '], dim: ['dim ', 'undim '],
        faded: ['fade ', 'unfade '],
    };
    const out = [];
    for (let step = 1; step <= maxStep; step++) {
        const prev = states[step - 1], cur = states[step];
        const changes = [];   // {el, key, on}
        cur.forEach((s, el) => {
            const p = prev.get(el) || {};
            Object.keys(s).forEach(key => {
                const before = key === 'faded' ? !!p[key] : p[key];
                if (before !== undefined && before !== s[key]) changes.push({ el, key, on: s[key] });
                else if (before === undefined && key === 'faded' && s[key]) changes.push({ el, key, on: true });
            });
        });
        prev.forEach((s, el) => {
            if (s.faded && !(cur.get(el) || {}).faded && !changes.some(c => c.el === el && c.key === 'faded')) {
                changes.push({ el, key: 'faded', on: false });
            }
        });
        // Report the outermost element of each kind of change.
        const keep = changes.filter(c => !changes.some(o => o !== c && o.key === c.key && o.on === c.on
            && o.el !== c.el && o.el.contains(c.el)));
        // Shows and hides stay one item each; class, fade, dim and highlight
        // changes are grouped per kind ("fx-soft on a, b (+5 more)").
        const groups = new Map();
        keep.forEach(c => {
            const verb = c.key.startsWith('class:') ? c.key.slice(6) + (c.on ? ' on ' : ' off ') : VERB[c.key][c.on ? 0 : 1];
            const gkey = c.key + (c.on ? '1' : '0');
            if (!groups.has(gkey)) groups.set(gkey, { verb, labels: [] });
            groups.get(gkey).labels.push(label(c.el));
        });
        const items = [];
        groups.forEach((g, gkey) => {
            if (gkey === 'shown1' || gkey === 'shown0') {
                g.labels.forEach(l => items.push(g.verb + l));
                return;
            }
            const shown = g.labels.slice(0, 2).join(', ');
            items.push(g.verb + shown + (g.labels.length > 2 ? ` (+${g.labels.length - 2} more)` : ''));
        });
        out.push({ step, changes: items });
    }
    return out;
}"""

DESCRIBE_JS = """(index) => {
    if (!window.WS || typeof WS.describeSteps !== 'function') return null;
    try { return WS.describeSteps(index); } catch (err) { return { error: String(err && err.message || err) }; }
}"""

LAYOUT_JS = r"""async (cfg) => {
""" + LABEL_HELPERS_JS + r"""
    const slide = WS.slides[cfg.index];
    const isAppendix = slide.classList.contains('appendix');
    const round = x => Math.round(x);
    const results = [];
    const TEXT_MIN = 0.1;

    function htmlScale(el) {
        let p = el;
        while (p && !(p instanceof HTMLElement && p.offsetWidth > 0)) p = p.parentElement;
        if (!p) return 1;
        const r = p.getBoundingClientRect();
        const s = r.width / p.offsetWidth;
        return Number.isFinite(s) && s > 0 ? s : 1;
    }

    function measure(step) {
        const sRect = slide.getBoundingClientRect();
        const sc = sRect.width / 1280 || 1;
        const rel = r => ({ l: (r.left - sRect.left) / sc, t: (r.top - sRect.top) / sc,
                            r: (r.right - sRect.left) / sc, b: (r.bottom - sRect.top) / sc });
        const opacity = makeOpacity(slide);
        const problems = [];
        const push = p => { p.step = step; problems.push(p); };

        // ---- text boxes: HTML text lines, KaTeX formulas, SVG <text>
        const boxes = [];
        const byOwner = new Map();
        const walker = document.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
        const range = document.createRange();
        let node;
        while ((node = walker.nextNode())) {
            if (!/\S/.test(node.textContent)) continue;
            const parent = node.parentElement;
            if (!parent || parent.closest(SKIP_SEL)) continue;
            const owner = parent.closest('.katex') || (parent instanceof SVGElement ? (parent.closest('text') || parent) : parent);
            if (byOwner.has(owner)) {
                if (owner === parent && !(owner instanceof SVGElement) && !owner.classList.contains('katex')) {
                    range.selectNodeContents(node);
                    const extra = Array.from(range.getClientRects()).filter(r => r.width > 0.5 && r.height > 0.5).map(rel);
                    byOwner.get(owner).rects.push(...extra);
                }
                continue;
            }
            const cs = getComputedStyle(owner);
            if (cs.visibility !== 'visible' || opacity(owner) < TEXT_MIN) continue;
            let rects;
            if (owner instanceof SVGElement || owner.classList.contains('katex')) {
                const r = owner.getBoundingClientRect();
                rects = r.width > 0.5 && r.height > 0.5 ? [rel(r)] : [];
            } else {
                range.selectNodeContents(node);
                rects = Array.from(range.getClientRects()).filter(r => r.width > 0.5 && r.height > 0.5).map(rel);
            }
            if (!rects.length) continue;
            let size;
            if (owner.classList && owner.classList.contains('katex')) {
                size = parseFloat(getComputedStyle(owner).fontSize) * htmlScale(owner) / sc;
            } else if (owner instanceof SVGElement) {
                const ctm = owner.getScreenCTM && owner.getScreenCTM();
                const k = ctm ? Math.sqrt(Math.abs(ctm.a * ctm.d - ctm.b * ctm.c)) : 1;
                size = parseFloat(cs.fontSize) * k / sc;
            } else {
                size = parseFloat(cs.fontSize) * htmlScale(owner) / sc;
            }
            const box = {
                owner, rects, size, svg: owner instanceof SVGElement,
                halo: owner instanceof SVGElement && cs.stroke && cs.stroke !== 'none' && parseFloat(cs.strokeWidth) > 0,
                positioned: false,
            };
            byOwner.set(owner, box);
            boxes.push(box);
        }
        const union = rs => rs.reduce((u, r) => ({ l: Math.min(u.l, r.l), t: Math.min(u.t, r.t),
            r: Math.max(u.r, r.r), b: Math.max(u.b, r.b) }), { l: Infinity, t: Infinity, r: -Infinity, b: -Infinity });
        boxes.forEach(b => { b.u = union(b.rects); b.label = label(b.owner, true); });

        // ---- overlapping text
        const positioned = el => {
            for (let p = el; p && p !== slide; p = p.parentElement) {
                if (p instanceof HTMLElement) {
                    const pos = getComputedStyle(p).position;
                    if (pos === 'absolute' || pos === 'fixed') return true;
                }
            }
            return false;
        };
        const pairs = [];
        for (let i = 0; i < boxes.length; i++) {
            for (let j = i + 1; j < boxes.length; j++) {
                const A = boxes[i], B = boxes[j];
                if (A.u.r <= B.u.l || B.u.r <= A.u.l || A.u.b <= B.u.t || B.u.b <= A.u.t) continue;
                const nested = A.owner.contains(B.owner) || B.owner.contains(A.owner);
                if (nested && !positioned(A.owner) && !positioned(B.owner)) continue;
                let best = null;
                for (const ra of A.rects) for (const rb of B.rects) {
                    const iw = Math.min(ra.r, rb.r) - Math.max(ra.l, rb.l);
                    const ih = Math.min(ra.b, rb.b) - Math.max(ra.t, rb.t);
                    if (iw <= 2 || ih <= 2) continue;
                    const minH = Math.min(ra.b - ra.t, rb.b - rb.t);
                    if (ih < 0.3 * minH) continue;
                    if (!best || iw * ih > best.w * best.h) best = { w: iw, h: ih, x: Math.max(ra.l, rb.l), y: Math.max(ra.t, rb.t) };
                }
                if (best) pairs.push({ A, B, best });
            }
        }
        // A formula inside a note that overlaps something is the same problem
        // as the note overlapping it: keep the outermost pair.
        const within = (inner, outer) => inner !== outer && outer.owner.contains(inner.owner);
        for (const p of pairs) {
            const redundant = pairs.some(q => q !== p && (
                (q.A === p.A && within(p.B, q.B)) || (q.B === p.B && within(p.A, q.A)) ||
                (q.A === p.B && within(p.A, q.B)) || (q.B === p.A && within(p.B, q.A))));
            if (redundant) continue;
            const best = p.best;
            push({ kind: 'overlap', severity: 'error', a: p.A.label, b: p.B.label,
                detail: `${round(best.w)}x${round(best.h)}px at ${round(best.x)},${round(best.y)}` });
        }

        // ---- text outside the slide, or clipped by a box or an SVG edge
        const clipCache = new Map();
        function clipBoxes(el) {
            if (clipCache.has(el)) return clipCache.get(el);
            const out = [];
            for (let p = el.parentElement; p && p !== slide; p = p.parentElement) {
                if (p instanceof SVGSVGElement) {
                    const cs = getComputedStyle(p);
                    if (cs.overflow === 'visible' && p.parentElement && p.parentElement.closest('svg')) continue;
                    const r = p.getBoundingClientRect();
                    out.push({ el: p, r: rel(r), what: 'its SVG' + (svgFile(p) ? ' (' + svgFile(p) + ')' : '') });
                } else if (p instanceof HTMLElement) {
                    const cs = getComputedStyle(p);
                    if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
                    const r = p.getBoundingClientRect();
                    const sx = p.offsetWidth ? r.width / p.offsetWidth : 1;
                    const sy = p.offsetHeight ? r.height / p.offsetHeight : 1;
                    out.push({ el: p, what: '<' + shortDesc(p) + '>', r: rel({
                        left: r.left + p.clientLeft * sx, top: r.top + p.clientTop * sy,
                        right: r.left + (p.clientLeft + p.clientWidth) * sx, bottom: r.top + (p.clientTop + p.clientHeight) * sy,
                    }), x: cs.overflowX !== 'visible', y: cs.overflowY !== 'visible' });
                }
            }
            clipCache.set(el, out);
            return out;
        }
        for (const b of boxes) {
            const u = b.u;
            const over = Math.max(-u.l, -u.t, u.r - 1280, u.b - 720);
            if (over > 1) {
                const sides = [];
                if (u.l < -1) sides.push('left ' + round(-u.l) + 'px');
                if (u.t < -1) sides.push('top ' + round(-u.t) + 'px');
                if (u.r > 1281) sides.push('right ' + round(u.r - 1280) + 'px');
                if (u.b > 721) sides.push('bottom ' + round(u.b - 720) + 'px');
                push({ kind: 'outside', severity: 'error', a: b.label, detail: 'past the slide ' + sides.join(', ') });
                continue;
            }
            for (const c of clipBoxes(b.owner)) {
                const r = c.r;
                const cx = c.x !== false, cy = c.y !== false;
                let cut = 0;
                if (cx) cut = Math.max(cut, r.l - u.l, u.r - r.r);
                if (cy) cut = Math.max(cut, r.t - u.t, u.b - r.b);
                if (cut > 2) {
                    push({ kind: 'clipped', severity: 'error', a: b.label, detail: `${round(cut)}px cut off by ${c.what}` });
                    break;
                }
            }
        }

        // ---- rendered font size below the floor
        if (cfg.fonts) {
            for (const b of boxes) {
                const allowed = cfg.allow && b.owner.closest(cfg.allow);
                const floor = cfg.floor !== null ? cfg.floor : (isAppendix || allowed ? 14 : 20);
                if (b.size < floor - 0.25) {
                    push({ kind: 'small-text', severity: b.size < 14 - 0.25 ? 'error' : 'warning', a: b.label,
                        detail: `${Math.round(b.size * 10) / 10}px < ${floor}px` });
                }
            }
        }

        // ---- SVG text over small data marks (dots, diamonds, short bars)
        if (cfg.marks) {
            const marks = [];
            slide.querySelectorAll('svg circle, svg ellipse, svg rect, svg polygon, svg path, svg use').forEach(m => {
                if (m.closest(SKIP_SEL) || m.closest('defs, clipPath, mask, marker, pattern, symbol')) return;
                const cs = getComputedStyle(m);
                if (cs.visibility !== 'visible' || opacity(m) < TEXT_MIN) return;
                if ((cs.fill === 'none' || parseFloat(cs.fillOpacity) === 0) && (cs.stroke === 'none')) return;
                const r = rel(m.getBoundingClientRect());
                const w = r.r - r.l, h = r.b - r.t;
                if (w < 2 || h < 2 || w > 40 || h > 40) return;
                marks.push({ m, r });
            });
            for (const b of boxes) {
                if (b.halo) continue;
                for (const { m, r } of marks) {
                    if (b.owner.contains(m)) continue;
                    for (const t of b.rects) {
                        const iw = Math.min(t.r, r.r) - Math.max(t.l, r.l);
                        const ih = Math.min(t.b, r.b) - Math.max(t.t, r.t);
                        if (iw > 0 && ih > 0 && iw * ih > 0.25 * (r.r - r.l) * (r.b - r.t)) {
                            push({ kind: 'text-over-mark', severity: 'warning', a: b.label, b: label(m, true),
                                detail: `${round(iw)}x${round(ih)}px` });
                            break;
                        }
                    }
                }
            }
        }

        // ---- overflow (same rule as check_overflow.py: any rendered element past the slide box)
        if (cfg.overflow) {
            const items = [];
            let maxB = 0, maxR = 0;
            for (const el of slide.querySelectorAll('*')) {
                const tag = el.tagName;
                if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'BR') continue;
                if (el.classList && el.classList.contains('notes')) continue;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const raw = el.getBoundingClientRect();
                if (raw.width <= 0 || raw.height <= 0) continue;
                const r = rel(raw);
                maxB = Math.max(maxB, r.b);
                maxR = Math.max(maxR, r.r);
                if (r.b > 721 || r.r > 1281) {
                    items.push({ label: label(el, true), bOver: Math.max(0, round(r.b - 720)), rOver: Math.max(0, round(r.r - 1280)),
                                 childCount: el.children.length });
                }
            }
            if (maxB > 721 || maxR > 1281) {
                items.sort((x, y) => (y.bOver + y.rOver) - (x.bOver + x.rOver) || x.childCount - y.childCount);
                const leaf = items.find(x => x.childCount === 0) || items[0];
                const dims = [];
                if (maxB > 721) dims.push(`bottom +${round(maxB - 720)}px`);
                if (maxR > 1281) dims.push(`right +${round(maxR - 1280)}px`);
                push({ kind: 'overflow', severity: 'error', a: leaf ? leaf.label : '', detail: dims.join(', ')
                    + (items.length > 1 ? ` (${items.length} elements)` : '') });
            }
        }
        return problems;
    }

    for (const step of cfg.steps) {
        WS.goToSlide(cfg.index, step);
        if (typeof WS.refitTables === 'function') await WS.refitTables();
        if (document.getAnimations) for (const a of document.getAnimations()) { try { a.finish(); } catch (e) {} }
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        results.push(...measure(step));
    }
    return results;
}"""


# ---------------------------------------------------------------- helpers
def short(text, width=LINE_WIDTH):
    text = re.sub(r"[\r\n\t]+", " ", str(text).replace("​", "")).rstrip()
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def fmt_plan(plan):
    by_slide = {}
    for i, s in plan:
        by_slide.setdefault(i, []).append(s)
    return " ".join(f"{i + 1}:{fmt_steps(steps)}" for i, steps in by_slide.items())


def fmt_steps(steps):
    steps = sorted(set(steps))
    if not steps:
        return ""
    runs, start, prev = [], steps[0], steps[0]
    for s in steps[1:]:
        if s == prev + 1:
            prev = s
            continue
        runs.append((start, prev))
        start = prev = s
    runs.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def parse_steps_spec(spec, max_step):
    spec = (spec or "last").strip().lower()
    if spec == "all":
        return list(range(max_step + 1))
    if spec == "last":
        return [max_step]
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "last":
            out.append(max_step)
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            out.extend(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise ws_deck.DeckError(f"bad step {part!r} (use 3,4 or 2-4 or all or last)")
    return list(dict.fromkeys(out))


def merge_problems(problems):
    """One entry per distinct problem, with the steps it occurs at."""
    merged = {}
    for p in problems:
        key = (p["kind"], p.get("a", ""), p.get("b", ""), re.sub(r" at \d+,\d+$", "", p.get("detail", "")))
        entry = merged.get(key)
        if entry is None:
            entry = dict(p)
            entry["steps"] = []
            merged[key] = entry
        entry["steps"].append(p["step"])
    out = list(merged.values())
    # Overflow caused by text already reported as outside the slide adds nothing.
    outside = {p["a"] for p in out if p["kind"] == "outside"}
    out = [p for p in out if not (p["kind"] == "overflow" and p.get("a") in outside)]
    # One line per severity for small text.
    small = {}
    for p in out:
        if p["kind"] == "small-text":
            small.setdefault(p["severity"], []).append(p)
    out = [p for p in out if p["kind"] != "small-text"]
    for severity, items in small.items():
        sizes = sorted(float(re.match(r"[\d.]+", p["detail"]).group(0)) for p in items)
        floor = re.search(r"< ([\d.]+px)", items[0]["detail"]).group(1)
        span = f"{sizes[0]:g}px" if sizes[0] == sizes[-1] else f"{sizes[0]:g}-{sizes[-1]:g}px"
        names = [re.sub(r" <[^>]*>$", "", p["a"]) for p in items]
        listed = ", ".join(names[:3]) + (f" (+{len(names) - 3} more)" if len(names) > 3 else "")
        out.append({"kind": "small-text", "severity": severity, "a": listed,
                    "detail": f"{len(items)} text element(s) at {span}, floor {floor}",
                    "steps": sorted({s for p in items for s in p["steps"]}), "elements": items})
    return out


def problem_text(p):
    if p["kind"] == "overlap":
        return f"overlap {p['a']} x {p['b']} ({p['detail']})"
    if p["kind"] == "text-over-mark":
        return f"text over mark {p['a']} x {p['b']} ({p['detail']})"
    if p["kind"] == "outside":
        return f"text outside slide {p['a']} ({p['detail']})"
    if p["kind"] == "clipped":
        return f"clipped {p['a']} ({p['detail']})"
    if p["kind"] == "small-text":
        return f"small text {p['a']} ({p['detail']})"
    if p["kind"] == "overflow":
        return f"overflow {p['detail']}: {p['a']}"
    return f"{p['kind']} {p.get('a', '')} {p.get('detail', '')}"


SEV = {"error": "error", "warning": "warn ", "info": "note "}


def style_text(f):
    d = f.get("detail") or {}
    rule = f["rule"]
    if rule == "acronym":
        return f"acronym \"{d.get('acronym')}\" used before it is spelled out"
    if rule == "top-heavy":
        return f"top-heavy: content ends at y={d.get('contentBottom')}px of {d.get('usableBottom')}px"
    if rule == "words-slide":
        return f"words-slide: {d.get('words')} words visible at once (max 70)"
    if rule == "text-size":
        items = d.get("elements") or []
        ex = "; ".join(f"\"{e['text'][:24]}\" {e['size']:g}px" for e in items[:2])
        return f"text-size: {len(items)} element(s) at {min(e['size'] for e in items):g}px: {ex}" if items else f["message"]
    return f"{rule}: {f['message']}"


# ---------------------------------------------------------------- links
def link_issues_for(index, slides, selected):
    import validate_links

    issues = validate_links.validate(str(index))
    chosen_html = {i: slides[i]["html"] for i in selected}
    mine, elsewhere, deck_wide = {}, 0, []
    for issue in issues:
        if issue.split(":")[0] in ("MISSING DECK", "MULTIPLE DECKS", "NESTED DECK", "UNCLOSED DECK", "NO SLIDES"):
            deck_wide.append(issue)
            continue
        tokens = re.findall(r'"#?([^"]+)"', issue)
        m = re.match(r"(?:MISSING FILE|INVALID FILE|INVALID FILE URL): (?:.* -> )?(.+?)(?: is a directory)?$", issue)
        if m:
            tokens.append(m.group(1))
        hit = [i for i, html in chosen_html.items() if any(t and t in html for t in tokens)]
        if hit:
            for i in hit:
                mine.setdefault(i, []).append(issue)
        else:
            elsewhere += 1
    return mine, elsewhere, deck_wide


# ---------------------------------------------------------------- session
class Session:
    def __init__(self, index: Path, timeout_ms=30000, style=False):
        self.index = index
        self.timeout_ms = timeout_ms
        self.style = style
        self._stack = contextlib.ExitStack()

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        served = self._stack.enter_context(DeckServer(self.index))
        p = self._stack.enter_context(sync_playwright())
        self.browser = p.chromium.launch()
        self._stack.callback(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT},
                                          device_scale_factor=1)
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(e))
        if self.style:
            from check_style import FONT_WATCH_INIT_JS

            self.page.add_init_script(FONT_WATCH_INIT_JS)
        self.page.goto(served.url, wait_until="load")
        validate_browser_deck_structure(self.page.evaluate(DECK_STRUCTURE_JS))
        raise_for_page_errors(self.errors)
        wait_for_deck_ready_sync(self.page, self.errors, timeout_ms=self.timeout_ms)
        self.page.add_style_tag(content=NO_TRANSITIONS_CSS)
        self.infos = self.page.evaluate(SLIDE_INFO_JS)
        return self

    def __exit__(self, *exc):
        self._stack.close()

    def goto(self, index, step):
        self.page.evaluate(NAVIGATE_JS, [index, step])

    def shot(self, index, step):
        self.goto(index, step)
        return self.page.screenshot()


def check_counts(session, slides):
    if len(session.infos) != len(slides):
        raise ws_deck.DeckError(f"the browser sees {len(session.infos)} slides but the file has {len(slides)}; "
                                "check for slides added by scripts")


# ---------------------------------------------------------------- outline
def normalise_described(raw):
    """Turn WS.describeSteps output into [{'step', 'changes': [str]}], or None if unusable."""
    if raw is None or isinstance(raw, dict) and raw.get("error"):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("steps"), list):
        raw = raw["steps"]
    if not isinstance(raw, list):
        return None
    out = []
    for n, entry in enumerate(raw):
        step = n
        changes = []
        if isinstance(entry, str):
            changes = [entry]
        elif isinstance(entry, list):
            changes = [describe_item(x) for x in entry]
        elif isinstance(entry, dict) and "appear" in entry:
            step = entry.get("step", n)
            changes = engine_changes(entry)
        elif isinstance(entry, dict):
            step = entry.get("step", n)
            for key, value in entry.items():
                if key in ("step", "index", "slide"):
                    continue
                if isinstance(value, list) and value:
                    changes.append(f"{key}: " + ", ".join(describe_item(x) for x in value))
                elif isinstance(value, str) and value:
                    changes.append(f"{key}: {value}")
        if isinstance(step, int) and step >= 1:
            out.append({"step": step, "changes": changes})
    return out


def engine_changes(entry):
    """Compact tokens for one step of the engine's WS.describeSteps shape:
    +shown -hidden ~faded =unfaded, then focus, morph, class, highlight and dim changes."""
    def labels(key):
        return [describe_item(x) for x in entry.get(key) or []]

    changes = [f"+{x}" for x in labels("appear")] + [f"-{x}" for x in labels("disappear")]
    changes += [f"~{x}" for x in labels("fade")] + [f"={x}" for x in labels("unfade")]
    for f in entry.get("focus") or []:
        changes.append(f"focus {f.get('selector')} in {f.get('on')}"
                       + ("" if f.get("matched", 1) else " (matches nothing)"))
    for f in entry.get("unfocus") or []:
        changes.append(f"unfocus {f.get('on')}")
    for m in entry.get("morph") or []:
        changes.append(f"morph {m.get('key')}: {m.get('from')} to {m.get('to')}")
    changes += [str(c) for c in entry.get("classes") or []]
    changes += [f"highlight {x}" for x in labels("highlight")] + [f"dim {x}" for x in labels("dim")]
    return changes


def describe_item(x):
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        for key in ("label", "text", "description", "selector", "id"):
            if x.get(key):
                return str(x[key])
        return json.dumps(x, ensure_ascii=False)[:60]
    return str(x)


def outline(args):
    index, _text, slides = ws_deck.load_deck(args.deck)
    selected = ws_deck.select_slides(args.slides, slides) if args.slides else list(range(len(slides)))
    with Session(index, args.timeout) as s:
        check_counts(s, slides)
        rows = []
        source = None
        for i in selected:
            info = s.infos[i]
            row = {"number": i + 1, "id": slides[i]["key"], "title": slides[i]["title"],
                   "maxStep": info["maxStep"], "appendix": slides[i]["is_appendix"],
                   "lines": [slides[i]["line_start"], slides[i]["line_end"]]}
            if args.steps and info["maxStep"]:
                raw = s.page.evaluate(DESCRIBE_JS, i)
                described = normalise_described(raw)
                if described is not None:
                    source = source or "engine"
                    row["warnings"] = (raw.get("warnings") or []) if isinstance(raw, dict) else []
                else:
                    source = "observed" if source in (None, "observed") else source
                    described = s.page.evaluate(OBSERVE_STEPS_JS, [i, info["maxStep"]])
                row["steps"] = described
            rows.append(row)
        raise_for_page_errors(s.errors)
    if args.json:
        print(json.dumps({"deck": str(index), "slides": rows}, indent=1, ensure_ascii=False))
        return 0
    from ws import outline_line

    for row in rows:
        print(outline_line(slides[row["number"] - 1], row["maxStep"]))
        for st in row.get("steps") or []:
            print(f"      {st['step']}: " + ws_steps.summarise(st["changes"], 120))
        for w in row.get("warnings") or []:
            print(f"      warn: {w}")
    return 0


# ---------------------------------------------------------------- images
def sheet_layout(n):
    if n <= 4:
        return 2, 640
    if n <= 9:
        return 3, 426
    return 4, 320


def contact_sheet(shots, infos, title):
    from screenshot_slide import build_contact_sheet

    cols, width = sheet_layout(len(shots))
    data, _size = build_contact_sheet(shots, infos, title, columns=cols, thumb_width=width)
    return data


def changed_regions(old_png: bytes, new_png: bytes):
    """Return (union box, [component boxes]) of pixels that differ, or (None, []) when identical."""
    from PIL import Image, ImageChops

    a = Image.open(io.BytesIO(old_png)).convert("RGB")
    b = Image.open(io.BytesIO(new_png)).convert("RGB")
    if a.size != b.size:
        return (0, 0) + b.size, [(0, 0) + b.size]
    mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > DIFF_THRESHOLD else 0)
    union = mask.getbbox()
    if not union:
        return None, []
    w, h = mask.size
    gw, gh = math.ceil(w / DIFF_CELL), math.ceil(h / DIFF_CELL)
    small = mask.resize((gw, gh), Image.BOX).point(lambda v: 255 if v > 0 else 0)
    px = small.load()
    seen = set()
    comps = []
    for y in range(gh):
        for x in range(gw):
            if not px[x, y] or (x, y) in seen:
                continue
            stack = [(x, y)]
            seen.add((x, y))
            x0 = x1 = x
            y0 = y1 = y
            while stack:
                cx, cy = stack.pop()
                x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                for dx in (-2, -1, 0, 1, 2):
                    for dy in (-2, -1, 0, 1, 2):
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < gw and 0 <= ny < gh and px[nx, ny] and (nx, ny) not in seen:
                            seen.add((nx, ny))
                            stack.append((nx, ny))
            box = (x0 * DIFF_CELL, y0 * DIFF_CELL, min(w, (x1 + 1) * DIFF_CELL), min(h, (y1 + 1) * DIFF_CELL))
            sub = mask.crop(box).getbbox()
            if sub:
                comps.append((box[0] + sub[0], box[1] + sub[1], box[0] + sub[2], box[1] + sub[3]))
    return union, comps or [union]


def pad_box(box, size, margin=DIFF_MARGIN):
    w, h = size
    return (max(0, box[0] - margin), max(0, box[1] - margin), min(w, box[2] + margin), min(h, box[3] + margin))


def diff_crop(old_png: bytes, new_png: bytes, before=False):
    """Return (png bytes of the changed region(s), description) or (None, None) when nothing changed."""
    from PIL import Image, ImageDraw

    union, comps = changed_regions(old_png, new_png)
    if union is None:
        return None, None
    new = Image.open(io.BytesIO(new_png)).convert("RGB")
    old = Image.open(io.BytesIO(old_png)).convert("RGB")
    size = new.size
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])  # noqa: E731
    boxes = [pad_box(union, size)]
    if 1 < len(comps) <= 4 and area(union) > 2 * sum(area(pad_box(c, size)) for c in comps):
        boxes = [pad_box(c, size) for c in sorted(comps, key=lambda c: (c[1], c[0]))]
    pieces = []  # (caption, image)
    for box in boxes:
        where = f"{box[0]},{box[1]}"
        if before and old.size == size:
            pieces.append((f"before {where}", old.crop(box)))
            pieces.append((f"after {where}", new.crop(box)))
        else:
            pieces.append((where, new.crop(box)))
    label_h = 16 if len(pieces) > 1 else 0
    gap = 6 if len(pieces) > 1 else 0
    width = max(img.width for _c, img in pieces)
    height = sum(img.height + label_h for _c, img in pieces) + gap * (len(pieces) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    y = 0
    for caption, img in pieces:
        if label_h:
            draw.text((2, y + 2), caption, fill="#B03A2E")
            y += label_h
        canvas.paste(img, (0, y))
        y += img.height + gap
    out = io.BytesIO()
    canvas.save(out, "PNG")
    desc = ", ".join(f"{b[2] - b[0]}x{b[3] - b[1]} at {b[0]},{b[1]}" for b in boxes)
    return out.getvalue(), desc


# ---------------------------------------------------------------- check / diff / layout
def plan_steps(selected, infos, spec):
    plan = []
    notes = []
    for i in selected:
        m = infos[i]["maxStep"]
        steps = parse_steps_spec(spec, m)
        bad = [s for s in steps if s > m]
        if bad:
            if len(selected) == 1:
                raise ws_deck.DeckError(f"step {bad[0]} out of range for slide {i + 1} (it has steps 0-{m})")
            notes.append(f"{i + 1}: no step {fmt_steps(bad)} (steps 0-{m})")
        plan.extend((i, s) for s in steps if s <= m)
    return plan, notes


def layout_cfg(index, steps, *, fonts, overflow, marks=False, floor=None):
    from check_style import SMALL_TEXT_ALLOWLIST

    return {"index": index, "steps": steps, "fonts": fonts, "overflow": overflow, "marks": marks,
            "floor": floor, "allow": SMALL_TEXT_ALLOWLIST}


def layout(args):
    index, _text, slides = ws_deck.load_deck(args.deck)
    selected = ws_deck.select_slides(args.slides, slides)
    out = []
    with Session(index, args.timeout) as s:
        check_counts(s, slides)
        for i in selected:
            m = s.infos[i]["maxStep"]
            steps = parse_steps_spec(args.step, m)
            if any(x > m for x in steps):
                raise ws_deck.DeckError(f"slide {i + 1} has steps 0-{m}")
            probs = s.page.evaluate(LAYOUT_JS, layout_cfg(i, steps, fonts=True, overflow=True,
                                                          marks=args.marks, floor=args.floor))
            out.append((i, m, steps, merge_problems(probs)))
        raise_for_page_errors(s.errors)
    if args.json:
        print(json.dumps([{"slide": i + 1, "id": slides[i]["key"], "steps": st, "maxStep": m, "problems": p}
                          for i, m, st, p in out], indent=1, ensure_ascii=False))
        return 0
    bad = 0
    for i, m, steps, probs in out:
        for p in sorted(probs, key=lambda p: (p["steps"][0], p["kind"])):
            bad += 1
            where = f"{i + 1}:{fmt_steps(p['steps'])}" if m else f"{i + 1}"
            print(short(f"{where:<6} {SEV[p['severity']]} {problem_text(p)}"))
    if not bad:
        print("OK" if len(out) == 1 else f"OK ({len(out)} slides)")
    return 1 if any(p["severity"] == "error" for *_x, probs in out for p in probs) else 0


def check(args, diff_only=False):
    started = getattr(args, "started", time.monotonic())
    index, text, slides = ws_deck.load_deck(args.deck)
    specs = list(args.slides_pos) + list(args.slides)
    if not specs:
        raise ws_deck.DeckError("give slides, e.g. --slides 10,12-13 (or `all`)")
    selected = ws_deck.select_slides(specs, slides)
    key = ws_deck.deck_key(index)
    out_dir = Path(args.out).expanduser() if args.out else OUT_ROOT / key
    cache_dir = CACHE_ROOT / key
    want_diff = diff_only or getattr(args, "diff", False)
    run_checks = not diff_only
    lines = []          # problem lines
    step_status = {}    # (i, s) -> text
    report = {"deck": str(index), "slides": [], "images": [], "problems": []}
    with Session(index, args.timeout, style=run_checks) as s:
        check_counts(s, slides)
        infos = s.infos
        plan, notes = plan_steps(selected, infos, args.steps)
        captures = []
        if not args.no_shots:
            for i, st in plan:
                captures.append((i, st, s.shot(i, st)))
        problems = []
        if run_checks:
            from check_style import (
                DECK_TEXT_JS, FONT_CHANGES_JS, MAIN_FLOOR_PX, MEASURE_JS, MIN_IMAGE_CHECK_PX,
                SIZE_TOLERANCE_PX, SMALL_TEXT_ALLOWLIST, build_report,
            )

            s.page.evaluate("WS.prepareForExport()")
            load_errors = s.page.evaluate(LOAD_ERRORS_JS)
            font_changes = s.page.evaluate(FONT_CHANGES_JS)
            deck_text = s.page.evaluate(DECK_TEXT_JS)
            measurements = s.page.evaluate(MEASURE_JS, {
                "indexes": selected, "maxSteps": [x["maxStep"] for x in infos], "allow": SMALL_TEXT_ALLOWLIST,
                "reportBelow": MAIN_FLOOR_PX - SIZE_TOLERANCE_PX, "minImagePx": MIN_IMAGE_CHECK_PX,
            })
            known = [a for a in re.split(r"[\s,;]+", deck_text["known"]) if a]
            style = build_report(index, infos, selected, measurements, deck_text["slides"], known,
                                 load_errors, font_changes, text)
            for f in style["findings"]:
                if f["severity"] == "info" or (f["slide"] is None and f["severity"] != "error"):
                    continue
                problems.append({"slide": f["slide"], "steps": f["steps"], "severity": f["severity"],
                                 "text": style_text(f), "source": "style"})
            for i in selected:
                m = infos[i]["maxStep"]
                probs = s.page.evaluate(LAYOUT_JS, layout_cfg(i, list(range(m + 1)), fonts=False, overflow=True))
                for p in merge_problems(probs):
                    problems.append({"slide": i, "steps": p["steps"], "severity": p["severity"],
                                     "text": problem_text(p), "source": "layout"})
        raise_for_page_errors(s.errors)

    if run_checks:
        mine, elsewhere, deck_wide = link_issues_for(index, slides, selected)
        for i, issues in mine.items():
            for issue in issues:
                problems.append({"slide": i, "steps": [], "severity": "error", "text": "link " + issue,
                                 "source": "links"})
        for issue in deck_wide:
            problems.append({"slide": None, "steps": [], "severity": "error", "text": "link " + issue,
                             "source": "links"})
        if elsewhere:
            notes.append(f"{elsewhere} link issue(s) on other slides (validate_links.py lists them)")

    # ---- images
    out_dir.mkdir(parents=True, exist_ok=True)
    image_lines = []
    if captures:
        sheet_infos = [dict(info, id="") for info in infos]
        to_sheet = []
        for i, st, data in captures:
            cached = cache_dir / slides[i]["key"] / f"step-{st}.png"
            tag = f"{i + 1}:{st}"
            if want_diff:
                if cached.exists():
                    crop, desc = diff_crop(cached.read_bytes(), data, before=getattr(args, "before", False))
                    if crop is None:
                        step_status[(i, st)] = "no visual change"
                    else:
                        path = out_dir / f"s{i + 1}-{st}-diff.png"
                        path.write_bytes(crop)
                        step_status[(i, st)] = f"changed {desc}: {path}"
                        report["images"].append(str(path))
                else:
                    path = out_dir / f"s{i + 1}-{st}.png"
                    path.write_bytes(data)
                    step_status[(i, st)] = f"no baseline yet, full image: {path}"
                    report["images"].append(str(path))
            elif args.each or len(captures) == 1:
                path = out_dir / f"s{i + 1}-{st}.png"
                path.write_bytes(data)
                report["images"].append(str(path))
                image_lines.append(f"{tag} {path}")
            else:
                to_sheet.append((i, st, data))
                # Refresh the single-step file too, so an s<slide>-<step>.png left by an earlier
                # single-step check never shows a stale render next to a fresh sheet.
                (out_dir / f"s{i + 1}-{st}.png").write_bytes(data)
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cached)
        if to_sheet:
            nums = sorted({i + 1 for i, _s, _d in to_sheet})
            name = "sheet-" + (str(nums[0]) if len(nums) == 1 else f"{nums[0]}-{nums[-1]}")
            for n in range(0, len(to_sheet), 24):
                chunk = to_sheet[n:n + 24]
                path = out_dir / (f"{name}.png" if n == 0 else f"{name}-{n // 24 + 1}.png")
                path.write_bytes(contact_sheet(chunk, sheet_infos, f"{index.parent.name}  " +
                                               fmt_plan([(i, st) for i, st, _d in chunk])))
                report["images"].append(str(path))
                image_lines.append(f"sheet {path} ({len(chunk)} captures)")

    # ---- PDF
    pdf_line = None
    if run_checks and args.pdf is not None:
        import export_pdf

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(export_pdf.export_pdf(str(index), args.pdf or None))
        done = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("Exported")]
        pdf_line = done[-1].strip() if done else "PDF exported"

    # ---- report
    elapsed = time.monotonic() - started
    n_err = sum(1 for p in problems if p["severity"] == "error")
    if args.json:
        report["slides"] = [{"slide": i + 1, "id": slides[i]["key"], "maxStep": infos[i]["maxStep"]} for i in selected]
        report["captures"] = [{"slide": i + 1, "step": st,
                               "status": step_status.get((i, st), "captured" if captures else "not captured")}
                              for i, st in plan]
        report["problems"] = [dict(p, slide=None if p["slide"] is None else p["slide"] + 1) for p in problems]
        report["notes"] = notes
        report["seconds"] = round(elapsed, 2)
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        order = {"error": 0, "warning": 1}
        for p in sorted(problems, key=lambda p: (p["slide"] if p["slide"] is not None else -1,
                                                 order.get(p["severity"], 2), p["steps"][:1])):
            if p["slide"] is None:
                where = "deck"
            else:
                m = infos[p["slide"]]["maxStep"]
                where = f"{p['slide'] + 1}" + (f":{fmt_steps(p['steps'])}" if p["steps"] and m else "")
            lines.append(short(f"{where:<6} {SEV[p['severity']]} {p['text']}"))
        for line in lines:
            print(line)
        if want_diff:
            for i, st in plan:
                print(short(f"{i + 1}:{st}  {step_status.get((i, st), 'no capture')}", 220))
        for line in image_lines:
            print(line)
        for note in notes:
            print(f"note: {note}")
        if pdf_line:
            print(pdf_line)
        n_warn = sum(1 for p in problems if p["severity"] == "warning")
        captured = fmt_plan(plan) if plan and captures else "nothing"
        if run_checks:
            steps_checked = sum(infos[i]["maxStep"] + 1 for i in selected)
            verdict = "ok" if not problems else f"{n_err} error(s), {n_warn} warning(s)"
            print(short(f"{verdict}: links, overflow, style, layout on {len(selected)} slide(s) / "
                        f"{steps_checked} step(s); captured {captured} ({elapsed:.1f}s)", 220))
        else:
            states = list(step_status.values())
            changed = sum(1 for x in states if x.startswith("changed"))
            fresh = sum(1 for x in states if x.startswith("no baseline"))
            print(f"diff {captured}: {changed} changed, {len(states) - changed - fresh} unchanged"
                  + (f", {fresh} without baseline" if fresh else "") + f" ({elapsed:.1f}s)")
    if n_err or (getattr(args, "strict", False) and problems):
        return 1
    return 0


def run(args):
    if args.command == "outline":
        return outline(args)
    if args.command == "layout":
        return layout(args)
    if args.command == "diff":
        return check(args, diff_only=True)
    return check(args)

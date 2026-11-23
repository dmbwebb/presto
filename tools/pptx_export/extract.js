// Browser-side extraction for the editable PowerPoint export.
// Installs window.PX.capture(root, designWidth): the drawable objects visible in
// the slide's current reveal state. The Python side calls it once per step and
// merges the results by each object's stable key.
(() => {
    const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT', 'LINK', 'META', 'ASIDE', 'TITLE']);
    const SKIP_CLASSES = ['ws-table-config-error', 'ws-table-load-error', 'katex-mathml'];
    const SVG_SKIP = new Set(['defs', 'clipPath', 'mask', 'symbol', 'marker', 'title', 'desc', 'style', 'metadata', 'linearGradient', 'radialGradient', 'pattern', 'filter']);
    const REVEAL_ATTRS = ['data-step', 'data-build-in', 'data-build-out'];
    const SVG_NODE_LIMIT = 1500;

    const ids = new WeakMap();
    let nextId = 1;
    const idOf = el => { if (!ids.has(el)) ids.set(el, nextId++); return ids.get(el); };

    let origin, scale, out;
    const round = v => Math.round(v * 100) / 100;
    const norm = r => ({x: round((r.left - origin.left) / scale), y: round((r.top - origin.top) / scale), w: round(r.width / scale), h: round(r.height / scale)});
    const px = v => { const n = parseFloat(v); return Number.isFinite(n) ? n : 0; };
    const isReveal = el => REVEAL_ATTRS.some(a => el.hasAttribute(a));
    const inlineLevel = cs => cs.display.startsWith('inline') || cs.display === 'contents' || cs.display === 'ruby';
    const isHidden = cs => cs.display === 'none' || cs.visibility === 'hidden';
    const isKatex = el => el.classList.contains('katex') || el.classList.contains('katex-display');
    const unquote = s => s.replace(/^["']|["']$/g, '');

    function color(value, alpha) {
        if (!value || value === 'none' || value === 'transparent') return null;
        const m = value.match(/rgba?\(([^)]+)\)/);
        if (!m) return null;
        const p = m[1].split(/[\s,/]+/).filter(Boolean).map(parseFloat);
        const a = (p.length > 3 ? p[3] : 1) * (alpha === undefined ? 1 : alpha);
        if (a <= 0.01) return null;
        const hex = p.slice(0, 3).map(v => Math.round(v).toString(16).padStart(2, '0')).join('').toUpperCase();
        return {hex, a: round(a)};
    }

    function rangeRect(node) {
        const range = document.createRange();
        range.selectNodeContents(node);
        let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
        const tops = [];
        for (const rc of range.getClientRects()) {
            if (rc.width < 0.5 || rc.height < 0.5) continue;
            l = Math.min(l, rc.left); t = Math.min(t, rc.top); r = Math.max(r, rc.right); b = Math.max(b, rc.bottom);
            tops.push(rc.top + rc.height / 2);
        }
        if (l === Infinity) return null;
        tops.sort((a, c) => a - c);
        let lines = 1;
        const gap = Math.max(6, (b - t) / Math.max(tops.length, 1) * 0.45);
        for (let i = 1; i < tops.length; i++) if (tops[i] - tops[i - 1] > gap) lines++;
        return {left: l, top: t, width: r - l, height: b - t, lines};
    }

    // Ratio of the font's natural line height to its size: PowerPoint's 100% spacing.
    const naturalRatio = new Map();
    function naturalLine(family, weight) {
        const key = family + '|' + weight;
        if (!naturalRatio.has(key)) {
            const s = document.createElement('span');
            s.textContent = 'Hg';
            s.style.cssText = 'position:absolute;visibility:hidden;line-height:normal;font-size:100px;font-weight:' + weight + ';font-family:' + family;
            document.body.appendChild(s);
            naturalRatio.set(key, s.getBoundingClientRect().height / 100 || 1.2);
            s.remove();
        }
        return naturalRatio.get(key);
    }

    function firstFamily(cs) { return unquote(cs.fontFamily.split(',')[0].trim()); }

    function transformCase(s, cs) {
        if (cs.textTransform === 'uppercase') return s.toLocaleUpperCase();
        if (cs.textTransform === 'lowercase') return s.toLocaleLowerCase();
        if (cs.textTransform === 'capitalize') return s.replace(/(^|\s)(\S)/g, (m, a, b) => a + b.toLocaleUpperCase());
        return s;
    }

    function baselineOf(el, cs) {
        if (cs.verticalAlign === 'super' || el.closest('sup')) return 30000;
        if (cs.verticalAlign === 'sub' || el.closest('sub')) return -25000;
        return 0;
    }

    function runStyle(el, cs, st) {
        const deco = cs.textDecorationLine || '';
        const base = baselineOf(el, cs);
        return {
            f: firstFamily(cs), sz: round(px(cs.fontSize) * st.k * (base ? 1.45 : 1)),
            b: (parseInt(cs.fontWeight, 10) || 400) >= 600, i: cs.fontStyle === 'italic',
            c: color(cs.color, st.op), u: deco.includes('underline'), s: deco.includes('line-through'),
            base, spc: cs.letterSpacing === 'normal' ? 0 : round(px(cs.letterSpacing) * st.k),
        };
    }

    function isChip(cs) {
        if (!inlineLevel(cs) || cs.display === 'contents') return false;
        const painted = color(cs.backgroundColor) || px(cs.borderTopWidth) > 0 || px(cs.borderLeftWidth) > 0;
        return !!painted && (cs.display !== 'inline' || px(cs.borderTopLeftRadius) > 0 || px(cs.paddingLeft) > 2);
    }

    // MathML (from KaTeX) as editable runs: italics, sub/superscripts, a/b fractions.
    function mathRuns(node, style, base, runs) {
        const add = (text, extra) => { if (text) runs.push(Object.assign({}, style, {t: text, base: base || 0}, extra || {})); };
        const tag = node.localName;
        const kids = Array.from(node.children);
        if (tag === 'annotation') return;
        if (tag === 'mi') add(node.textContent, {i: node.textContent.length === 1});
        else if (tag === 'mn' || tag === 'mo' || tag === 'mtext') add(node.textContent.replace(/⁡|⁢/g, ''));
        else if (tag === 'mfrac') { add('('); mathRuns(kids[0], style, base, runs); add(')/('); mathRuns(kids[1], style, base, runs); add(')'); }
        else if (tag === 'msqrt') { add('√('); kids.forEach(k => mathRuns(k, style, base, runs)); add(')'); }
        else if (tag === 'msub' || tag === 'munder') { mathRuns(kids[0], style, base, runs); mathRuns(kids[1], style, -25000, runs); }
        else if (tag === 'msup' || tag === 'mover') { mathRuns(kids[0], style, base, runs); mathRuns(kids[1], style, 30000, runs); }
        else if (tag === 'msubsup' || tag === 'munderover') { mathRuns(kids[0], style, base, runs); mathRuns(kids[1], style, -25000, runs); mathRuns(kids[2], style, 30000, runs); }
        else kids.forEach(k => mathRuns(k, style, base, runs));
    }

    function newPara(el, cs, st) {
        const family = firstFamily(cs), fs = px(cs.fontSize);
        const lh = cs.lineHeight === 'normal' ? null : px(cs.lineHeight);
        const align = {center: 'ctr', right: 'r', end: 'r', justify: 'l'}[cs.textAlign] || 'l';
        return {pkey: String(idOf(el)), runs: [], align, ln: lh ? round(lh / (fs * naturalLine(family, cs.fontWeight))) : null, op: st.op};
    }

    // Paragraphs of a block: inline descendants become runs, text-only blocks become
    // further paragraphs, and anything else is returned to be walked on its own.
    function parasOf(block, st) {
        const paras = [], deferred = [];
        let cur = null;
        const blockCs = getComputedStyle(block);
        const flush = () => { if (cur) { paras.push(cur); cur = null; } };
        const open = (el, cs) => { if (!cur) cur = newPara(el, cs, st); return cur; };

        function visit(parent, state) {
            const pcs = getComputedStyle(parent);
            for (const node of parent.childNodes) {
                if (node.nodeType === Node.TEXT_NODE) {
                    let s = node.nodeValue;
                    if (!/^(pre|pre-wrap|break-spaces)$/.test(pcs.whiteSpace)) s = s.replace(/\s+/g, ' ');
                    if (!s || (s === ' ' && !cur)) continue;
                    const run = runStyle(parent, pcs, state);
                    run.t = transformCase(s, pcs);
                    if (parent !== block && inlineLevel(pcs)) { const hl = color(pcs.backgroundColor); if (hl) run.hl = hl; }
                    const a = parent.closest('a');
                    if (a && block.contains(a) && a !== block) run.link = a.getAttribute('href');
                    open(parent === block ? block : parent, pcs).runs.push(run);
                    extendPara(cur, node);
                    continue;
                }
                if (node.nodeType !== Node.ELEMENT_NODE || SKIP_TAGS.has(node.tagName)) continue;
                const el = node, cs = getComputedStyle(el);
                if (SKIP_CLASSES.some(c => el.classList.contains(c))) continue;
                if (isHidden(cs)) continue;
                const op = state.op * px(cs.opacity);
                if (op < 0.02) continue;
                if (el.tagName === 'BR') { open(block, blockCs).runs.push({br: 1}); continue; }
                if (el.tagName === 'UL' || el.tagName === 'OL') { flush(); deferred.push(el); continue; }
                if (isKatex(el)) {
                    const math = el.querySelector('math');
                    if (math) { mathRuns(math, runStyle(el, cs, state), 0, open(el, cs).runs); extendPara(cur, el); }
                    continue;
                }
                if (el.localName === 'svg' || el.tagName === 'IMG' || el.tagName === 'TABLE') { deferred.push(el); continue; }
                if (inlineLevel(cs)) {
                    if (isChip(cs) || isReveal(el)) deferred.push(el);
                    else visit(el, Object.assign({}, state, {op}));
                    continue;
                }
                if (textOnly(el)) {
                    flush();
                    cur = newPara(el, cs, state);
                    visit(el, Object.assign({}, state, {op}));
                    flush();
                } else { flush(); deferred.push(el); }
            }
        }
        visit(block, st);
        flush();
        for (const p of paras) finishPara(p);
        return {paras: paras.filter(p => p.runs.length), deferred};
    }

    // A paragraph's vertical extent comes from its own text, not from a block that may hold sub-lists.
    function extendPara(p, node) {
        const rr = rangeRect(node);
        if (!rr) return;
        const top = round((rr.top - origin.top) / scale), bottom = round((rr.top + rr.height - origin.top) / scale);
        p.top = p.top === undefined ? top : Math.min(p.top, top);
        p.bottom = p.bottom === undefined ? bottom : Math.max(p.bottom, bottom);
    }

    function finishPara(p) {
        while (p.runs.length && p.runs[0].t !== undefined && !p.runs[0].t.trim()) p.runs.shift();
        while (p.runs.length && p.runs[p.runs.length - 1].t !== undefined && !p.runs[p.runs.length - 1].t.trim()) p.runs.pop();
        while (p.runs.length && p.runs[p.runs.length - 1].br) p.runs.pop();
        if (p.runs.length && p.runs[0].t !== undefined) p.runs[0].t = p.runs[0].t.replace(/^\s+/, '');
        const last = p.runs[p.runs.length - 1];
        if (last && last.t !== undefined) last.t = last.t.replace(/\s+$/, '');
        const merged = [];
        for (const r of p.runs) {
            const prev = merged[merged.length - 1];
            if (prev && prev.t !== undefined && r.t !== undefined) {
                const {t: a, ...x} = prev, {t: b, ...y} = r;
                if (JSON.stringify(x) === JSON.stringify(y)) { prev.t += r.t; continue; }
            }
            merged.push(r);
        }
        p.runs = merged;
        if (p.top === undefined) { p.top = null; p.bottom = null; }
    }

    function textOnly(el) {
        if (el.querySelector('img,svg,table,video,iframe,canvas,ul,ol')) return false;
        for (const d of [el].concat(Array.from(el.querySelectorAll('*')))) {
            const cs = getComputedStyle(d);
            if (cs.position === 'absolute') return false;
            if (color(cs.backgroundColor) || px(cs.borderTopWidth) > 0 || px(cs.borderBottomWidth) > 0 || px(cs.borderLeftWidth) > 0) return false;
        }
        return true;
    }

    function hasInlineContent(el) {
        for (const node of el.childNodes) {
            if (node.nodeType === Node.TEXT_NODE && node.nodeValue.trim()) return true;
            if (node.nodeType !== Node.ELEMENT_NODE || SKIP_TAGS.has(node.tagName)) continue;
            const cs = getComputedStyle(node);
            if (cs.display === 'none') continue;
            if (isKatex(node)) return true;
            if (node.tagName === 'BR') continue;
            if (inlineLevel(cs) && !isChip(cs) && !isReveal(node) && (node.textContent || '').trim()) return true;
        }
        return false;
    }

    function emit(obj, ctx) {
        obj.groups = ctx.groups;
        if (obj.z === undefined) obj.z = ctx.z || 0;
        out.push(obj);
        return obj;
    }

    function snippet(el) { return (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 32); }

    function textObject(el, cs, ctx, paras) {
        const rr = rangeRect(el) || el.getBoundingClientRect();
        const r = el.getBoundingClientRect();
        const block = !inlineLevel(cs);
        const left = block ? Math.min(r.left + px(cs.paddingLeft) + px(cs.borderLeftWidth), rr.left) : rr.left;
        const right = block ? Math.max(r.right - px(cs.paddingRight) - px(cs.borderRightWidth), rr.left + rr.width) : rr.left + rr.width;
        return {
            kind: 'text', key: idOf(el) + ':t', rect: norm({left, top: rr.top, width: right - left, height: rr.height}),
            tw: round(rr.width / scale), paras, lines: rr.lines || 1,
            nowrap: cs.whiteSpace === 'nowrap' || cs.whiteSpace === 'pre', op: ctx.op,
        };
    }

    function emitTextNode(node, parent, cs, ctx, n) {
        const rr = rangeRect(node);
        if (!rr) return;
        const s = node.nodeValue.replace(/\s+/g, ' ').trim();
        const run = runStyle(parent, cs, {op: ctx.op, k: ctx.k});
        run.t = transformCase(s, cs);
        const para = {pkey: idOf(parent) + '.n' + n, runs: [run], align: 'l', ln: null, top: null, bottom: null, op: ctx.op};
        emit({kind: 'text', key: idOf(parent) + ':n' + n, rect: norm(rr), tw: round(rr.width / scale), paras: [para], lines: rr.lines || 1, nowrap: false, op: ctx.op}, ctx);
    }

    function spacing(paras) {
        for (let i = 1; i < paras.length; i++) {
            const gap = paras[i].top !== null && paras[i - 1].bottom !== null ? paras[i].top - paras[i - 1].bottom : 0;
            paras[i].before = gap > 0.5 ? round(gap) : 0;
        }
    }

    function markerOf(li, cs) {
        const pseudo = getComputedStyle(li, '::before');
        const content = pseudo.content;
        if (content && content !== 'none' && content !== 'normal' && pseudo.display !== 'none') {
            const ch = unquote(content);
            if (ch.trim()) return {ch: ch.trim().slice(0, 2), c: color(pseudo.color)};
            return {ch: '•', c: color(pseudo.backgroundColor) || color(pseudo.color)};
        }
        const type = cs.listStyleType;
        if (type === 'none') return null;
        if (type === 'decimal' || type === 'decimal-leading-zero') return {auto: 'arabicPeriod', c: color(cs.color)};
        if (type === 'lower-alpha' || type === 'lower-latin') return {auto: 'alphaLcPeriod', c: color(cs.color)};
        if (type === 'lower-roman') return {auto: 'romanLcPeriod', c: color(cs.color)};
        return {ch: {circle: '○', square: '▪'}[type] || '•', c: color(cs.color)};
    }

    function simpleList(list) {
        if (list.querySelector('img,svg,table,video,iframe,canvas')) return false;
        return Array.from(list.querySelectorAll('li')).every(li => {
            const cs = getComputedStyle(li);
            return !/flex|grid/.test(cs.display) && !color(cs.backgroundColor) && px(cs.borderTopWidth) === 0 && px(cs.borderLeftWidth) === 0 && px(cs.borderBottomWidth) === 0;
        });
    }

    function listParas(list, level, ctx, listLeft, paras) {
        for (const li of list.children) {
            if (li.tagName !== 'LI') continue;
            const cs = getComputedStyle(li);
            if (isHidden(cs)) continue;
            const op = ctx.op * px(cs.opacity);
            if (op < 0.02) continue;
            const {paras: own, deferred} = parasOf(li, {op, k: ctx.k});
            const rr = rangeRect(li);
            const textLeft = rr ? (rr.left - origin.left) / scale : 0;
            own.forEach((p, i) => {
                p.pkey = idOf(li) + '.' + i;
                p.level = level;
                p.marL = round(Math.max(0, textLeft - listLeft));
                if (i === 0) {
                    p.bullet = markerOf(li, cs);
                    p.indent = p.bullet ? -round(Math.min(p.marL, px(cs.fontSize) * 1.15)) : 0;
                } else { p.bullet = null; p.indent = 0; }
                paras.push(p);
            });
            deferred.filter(d => d.tagName === 'UL' || d.tagName === 'OL').forEach(sub => {
                const subCtx = Object.assign({}, ctx, {op});
                if (getComputedStyle(sub).display !== 'none') listParas(sub, level + 1, subCtx, listLeft, paras);
            });
        }
    }

    function emitList(list, ctx) {
        const r = list.getBoundingClientRect();
        const listLeft = (r.left - origin.left) / scale;
        const paras = [];
        listParas(list, 0, ctx, listLeft, paras);
        if (!paras.length) return;
        spacing(paras);
        const rr = rangeRect(list);
        emit({
            kind: 'text', key: idOf(list) + ':l', isList: true,
            rect: norm({left: r.left, top: rr ? rr.top : r.top, width: r.width, height: rr ? rr.height : r.height}),
            paras, lines: Math.max(paras.length, 2), nowrap: false, op: ctx.op,
        }, ctx);
    }

    function boxOf(el, cs, ctx) {
        const fill = color(cs.backgroundColor, ctx.op);
        const img = cs.backgroundImage;
        const url = img && img.startsWith('url(') ? unquote(img.slice(4, -1)) : null;
        const grad = gradient(img, ctx.op);
        const sides = ['Top', 'Right', 'Bottom', 'Left'].map(s => ({w: cs['border' + s + 'Style'] === 'none' ? 0 : px(cs['border' + s + 'Width']), c: color(cs['border' + s + 'Color'], ctx.op)}));
        const live = sides.filter(s => s.w > 0 && s.c);
        const uniform = live.length === 4 && live.every(s => s.w === live[0].w && s.c.hex === live[0].c.hex);
        const r = el.getBoundingClientRect();
        const rad = px(cs.borderTopLeftRadius) / (cs.borderTopLeftRadius.includes('%') ? 100 / Math.min(r.width, r.height) : 1) * ctx.k;
        return {fill, grad, url, line: uniform ? {w: live[0].w, c: live[0].c} : null, sides: uniform ? [] : sides, radius: round(rad / scale), r};
    }

    function emitSideLines(el, box, ctx) {
        const r = norm(box.r), names = ['t', 'r', 'b', 'l'];
        box.sides.forEach((s, i) => {
            if (!(s.w > 0 && s.c)) return;
            const half = s.w / 2;
            const seg = [[r.x, r.y + half, r.x + r.w, r.y + half], [r.x + r.w - half, r.y, r.x + r.w - half, r.y + r.h],
                [r.x, r.y + r.h - half, r.x + r.w, r.y + r.h - half], [r.x + half, r.y, r.x + half, r.y + r.h]][i];
            emit({kind: 'line', key: idOf(el) + ':b' + names[i], pts: seg, stroke: {c: s.c, w: s.w}, op: ctx.op}, ctx);
        });
    }

    function splitTop(text) {
        const parts = []; let depth = 0, cur = '';
        for (const ch of text) {
            if (ch === '(') depth++; if (ch === ')') depth--;
            if (ch === ',' && depth === 0) { parts.push(cur.trim()); cur = ''; } else cur += ch;
        }
        if (cur.trim()) parts.push(cur.trim());
        return parts;
    }

    function gradient(value, op) {
        const m = (value || '').match(/(?:repeating-)?linear-gradient\((.*)\)/);
        if (!m) return null;
        const parts = splitTop(m[1]);
        let angle = 180;
        if (!/rgb/.test(parts[0])) {
            const head = parts.shift();
            const deg = head.match(/(-?[\d.]+)deg/);
            if (deg) angle = parseFloat(deg[1]);
            else angle = {'to top': 0, 'to right': 90, 'to bottom': 180, 'to left': 270}[head] ?? 180;
        }
        const stops = parts.map((part, i) => {
            const c = color((part.match(/rgba?\([^)]+\)/) || [])[0], op);
            const pos = part.match(/([\d.]+)%\s*$/);
            return {c: c || {hex: 'FFFFFF', a: 0}, pos: pos ? parseFloat(pos[1]) : (parts.length > 1 ? i / (parts.length - 1) * 100 : 0)};
        });
        return stops.length ? {angle, stops} : null;
    }

    function zOf(cs, fallback) {
        const z = parseInt(cs.zIndex, 10);
        return cs.position !== 'static' && Number.isFinite(z) ? z : fallback;
    }

    function pseudoBox(el, which, ctx) {
        if (el.tagName === 'LI') return;
        const p = getComputedStyle(el, which);
        if (p.content === 'none' || p.content === 'normal' || p.display === 'none') return;
        const fill = color(p.backgroundColor, ctx.op), grad = gradient(p.backgroundImage, ctx.op);
        const w = px(p.width), h = px(p.height);
        if ((!fill && !grad) || !w || !h) return;
        const r = el.getBoundingClientRect();
        const abs = p.position === 'absolute' || p.position === 'fixed';
        const left = r.left + (abs && p.left !== 'auto' ? px(p.left) : 0);
        const top = abs && p.top !== 'auto' ? r.top + px(p.top) : (which === '::after' ? r.bottom - h + px(p.marginTop) : r.top);
        emit({kind: 'box', key: idOf(el) + which, rect: norm({left, top, width: w, height: h}), fill: grad ? null : fill, grad, radius: px(p.borderTopLeftRadius), op: ctx.op, z: zOf(p, ctx.z)}, ctx);
    }

    function emitImage(el, cs, ctx) {
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return;
        let crop = null, rect = r;
        const nw = el.naturalWidth, nh = el.naturalHeight;
        if (nw && nh && cs.objectFit === 'cover') {
            const k = Math.max(r.width / nw, r.height / nh);
            const cx = 1 - r.width / (nw * k), cy = 1 - r.height / (nh * k);
            crop = {l: round(cx / 2), r: round(cx / 2), t: round(cy / 2), b: round(cy / 2)};
        } else if (nw && nh && (cs.objectFit === 'contain' || cs.objectFit === 'scale-down')) {
            const k = Math.min(r.width / nw, r.height / nh), w = nw * k, h = nh * k;
            rect = {left: r.left + (r.width - w) / 2, top: r.top + (r.height - h) / 2, width: w, height: h};
        }
        emit({kind: 'image', key: idOf(el) + ':i', rect: norm(rect), src: el.currentSrc || el.src, alt: el.alt || '', crop, op: ctx.op}, ctx);
    }

    function emitSnapshot(el, ctx, why) {
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return;
        emit({kind: 'snapshot', key: idOf(el) + ':s', rect: norm(r), why, op: ctx.op, clip: {x: r.left, y: r.top, width: r.width, height: r.height}}, ctx);
    }

    // ------------------------------------------------------------------ tables
    function emitTable(tbl, ctx) {
        const k = tbl.getBoundingClientRect().width / (tbl.offsetWidth || 1);
        const rows = Array.from(tbl.rows), taken = new Set(), cells = [];
        let ncol = 0;
        rows.forEach((tr, ri) => {
            let c = 0;
            for (const td of tr.cells) {
                while (taken.has(ri + ':' + c)) c++;
                const cspan = td.colSpan || 1, rspan = td.rowSpan || 1;
                for (let a = 0; a < rspan; a++) for (let b = 0; b < cspan; b++) taken.add((ri + a) + ':' + (c + b));
                cells.push({td, r: ri, c, cspan, rspan});
                c += cspan; ncol = Math.max(ncol, c);
            }
        });
        const widths = new Array(ncol).fill(null);
        cells.filter(x => x.cspan === 1).forEach(x => {
            const w = x.td.getBoundingClientRect().width / scale;
            widths[x.c] = widths[x.c] === null ? w : Math.max(widths[x.c], w);
        });
        const total = tbl.getBoundingClientRect().width / scale;
        const known = widths.reduce((a, w) => a + (w || 0), 0), blanks = widths.filter(w => w === null).length;
        const cols = widths.map(w => round(w === null ? Math.max(total - known, 0) / Math.max(blanks, 1) : w));
        const outCells = cells.map(x => {
            const tcs = getComputedStyle(x.td);
            let op = ctx.op * px(tcs.opacity);
            for (let p = x.td.parentElement; p && p !== tbl; p = p.parentElement) op *= px(getComputedStyle(p).opacity);
            const hidden = op < 0.02 || tcs.visibility === 'hidden';
            const {paras} = parasOf(x.td, {op: hidden ? 1 : op, k});
            spacing(paras);
            const side = s => ({w: tcs['border' + s + 'Style'] === 'none' ? 0 : round(px(tcs['border' + s + 'Width']) * k), c: color(tcs['border' + s + 'Color'])});
            return {
                r: x.r, c: x.c, cspan: x.cspan, rspan: x.rspan, paras, hidden,
                fill: color(tcs.backgroundColor) || color(getComputedStyle(x.td.parentElement).backgroundColor),
                borders: {l: side('Left'), r: side('Right'), t: side('Top'), b: side('Bottom')},
                pad: [tcs.paddingLeft, tcs.paddingRight, tcs.paddingTop, tcs.paddingBottom].map(v => round(px(v) * k)),
                valign: {top: 't', bottom: 'b'}[tcs.verticalAlign] || 'ctr',
            };
        });
        emit({
            kind: 'table', key: idOf(tbl) + ':tb', rect: norm(tbl.getBoundingClientRect()), cols,
            rows: rows.map(tr => round(tr.getBoundingClientRect().height / scale)), cells: outCells, op: ctx.op,
        }, ctx);
    }

    // ------------------------------------------------------------------ SVG
    function svgPoint(m, x, y) {
        return [round((m.a * x + m.c * y + m.e - origin.left) / scale), round((m.b * x + m.d * y + m.f - origin.top) / scale)];
    }

    const svgScale = m => Math.sqrt(Math.abs(m.a * m.d - m.b * m.c)) / scale;
    const svgAngle = m => Math.atan2(m.b, m.a) * 180 / Math.PI;

    function pathCommands(el, m) {
        const d = el.getAttribute('d') || '';
        if (/[aA]/.test(d)) return sampledPath(el, m);
        const tokens = d.match(/[a-zA-Z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?/g) || [];
        const cmds = [];
        let i = 0, cmd = '', x = 0, y = 0, sx = 0, sy = 0, cx = 0, cy = 0;
        const num = () => parseFloat(tokens[i++]);
        const isCmd = t => /^[a-zA-Z]$/.test(t);
        while (i < tokens.length) {
            if (isCmd(tokens[i])) cmd = tokens[i++];
            const rel = cmd === cmd.toLowerCase(), C = cmd.toUpperCase();
            const ox = rel ? x : 0, oy = rel ? y : 0;
            if (C === 'M') { x = num() + ox; y = num() + oy; sx = x; sy = y; cmds.push(['M', ...svgPoint(m, x, y)]); cmd = rel ? 'l' : 'L'; }
            else if (C === 'L') { x = num() + ox; y = num() + oy; cmds.push(['L', ...svgPoint(m, x, y)]); }
            else if (C === 'H') { x = num() + ox; cmds.push(['L', ...svgPoint(m, x, y)]); }
            else if (C === 'V') { y = num() + oy; cmds.push(['L', ...svgPoint(m, x, y)]); }
            else if (C === 'C' || C === 'S') {
                let x1, y1;
                if (C === 'C') { x1 = num() + ox; y1 = num() + oy; } else { x1 = 2 * x - cx; y1 = 2 * y - cy; }
                const x2 = num() + ox, y2 = num() + oy, nx = num() + ox, ny = num() + oy;
                cmds.push(['C', ...svgPoint(m, x1, y1), ...svgPoint(m, x2, y2), ...svgPoint(m, nx, ny)]);
                cx = x2; cy = y2; x = nx; y = ny;
            } else if (C === 'Q' || C === 'T') {
                let qx, qy;
                if (C === 'Q') { qx = num() + ox; qy = num() + oy; } else { qx = 2 * x - cx; qy = 2 * y - cy; }
                const nx = num() + ox, ny = num() + oy;
                cmds.push(['C', ...svgPoint(m, x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y)), ...svgPoint(m, nx + 2 / 3 * (qx - nx), ny + 2 / 3 * (qy - ny)), ...svgPoint(m, nx, ny)]);
                cx = qx; cy = qy; x = nx; y = ny;
            } else if (C === 'Z') { cmds.push(['Z']); x = sx; y = sy; }
            else break;
        }
        return cmds;
    }

    function sampledPath(el, m) {
        const len = el.getTotalLength();
        const n = Math.min(240, Math.max(12, Math.ceil(len / 2)));
        const cmds = [];
        for (let i = 0; i <= n; i++) {
            const p = el.getPointAtLength(len * i / n);
            cmds.push([i ? 'L' : 'M', ...svgPoint(m, p.x, p.y)]);
        }
        return cmds;
    }

    function svgPaint(cs, op, m) {
        const k = svgScale(m);
        const fillOp = op * px(cs.fillOpacity === '' ? 1 : cs.fillOpacity);
        const strokeOp = op * px(cs.strokeOpacity === '' ? 1 : cs.strokeOpacity);
        const fill = svgColor(cs.fill, fillOp);
        const sw = px(cs.strokeWidth) * k;
        const sc = svgColor(cs.stroke, strokeOp);
        const dash = cs.strokeDasharray && cs.strokeDasharray !== 'none' ? cs.strokeDasharray.split(/[\s,]+/).map(v => px(v) * k) : null;
        return {fill, stroke: sc && sw > 0 ? {c: sc, w: round(sw), dash, cap: cs.strokeLinecap} : null};
    }

    function firstStop(value, op) {
        const m = value.match(/url\(["']?#([^"')]+)/);
        const paintServer = m && document.getElementById(m[1]);
        const stop = paintServer && paintServer.querySelector('stop');
        return stop ? color(getComputedStyle(stop).stopColor, op) : null;
    }

    function svgColor(value, op) {
        return value.startsWith('url(') ? firstStop(value, op) : color(value, op);
    }

    function svgText(el, cs, op, ctx, m) {
        const bb = el.getBoundingClientRect();
        if (bb.width < 0.5) return;
        const k = svgScale(m);
        const st = {op, k};
        const lines = [];
        const pushRun = (node, s, ncs) => {
            if (!lines.length || (node.localName === 'tspan' && node.hasAttribute('y'))) lines.push([]);
            const run = runStyle(node, ncs, st);
            run.c = svgColor(ncs.fill, op) || run.c;
            run.t = s; lines[lines.length - 1].push(run);
        };
        const visit = node => {
            for (const c of node.childNodes) {
                if (c.nodeType === Node.TEXT_NODE) { const s = c.nodeValue.replace(/\s+/g, ' '); if (s.trim()) pushRun(node, s, getComputedStyle(node)); }
                else if (c.localName === 'tspan') visit(c);
            }
        };
        visit(el);
        const align = {middle: 'ctr', end: 'r'}[cs.textAnchor] || 'l';
        const angle = svgAngle(m);
        let rect = norm(bb), rot = 0;
        if (Math.abs(angle) > 1) {
            const box = el.getBBox(), w = box.width * k, h = box.height * k;
            rect = {x: round(rect.x + rect.w / 2 - w / 2), y: round(rect.y + rect.h / 2 - h / 2), w: round(w), h: round(h)};
            rot = round(angle);
        }
        const paras = lines.map((runs, i) => {
            const p = {pkey: idOf(el) + '.' + i, runs, align, ln: null, top: null, bottom: null, op};
            finishParaRuns(p);
            return p;
        }).filter(p => p.runs.length);
        if (paras.length) emit({kind: 'text', key: idOf(el) + ':t', rect, rot, paras, lines: paras.length, nowrap: true, op}, ctx);
    }

    function finishParaRuns(p) {
        if (p.runs.length) {
            p.runs[0].t = p.runs[0].t.replace(/^\s+/, '');
            const last = p.runs[p.runs.length - 1];
            last.t = last.t.replace(/\s+$/, '');
        }
        p.runs = p.runs.filter(r => r.t);
    }

    function walkSvgNode(el, ctx) {
        const tag = el.localName;
        if (SVG_SKIP.has(tag)) return;
        const cs = getComputedStyle(el);
        if (isHidden(cs)) return;
        const op = ctx.op * px(cs.opacity);
        if (op < 0.02) return;
        const m = el.getScreenCTM ? el.getScreenCTM() : null;
        const key = idOf(el);
        if (tag === 'g' || tag === 'a' || tag === 'svg') {
            const c = Object.assign({}, ctx, {op});
            if (isReveal(el)) c.groups = ctx.groups.concat([{id: key, name: snippet(el) || 'Chart step'}]);
            for (const kid of el.children) walkSvgNode(kid, c);
            return;
        }
        if (!m) return;
        const paint = svgPaint(cs, op, m);
        if (tag === 'text') return svgText(el, cs, op, ctx, m);
        if (tag === 'line') {
            const [a, b] = [svgPoint(m, px(el.getAttribute('x1')), px(el.getAttribute('y1'))), svgPoint(m, px(el.getAttribute('x2')), px(el.getAttribute('y2')))];
            if (paint.stroke) emit({kind: 'line', key: key + ':g', pts: [a[0], a[1], b[0], b[1]], stroke: paint.stroke, op}, ctx);
            return;
        }
        if (tag === 'rect' || tag === 'circle' || tag === 'ellipse') {
            if (!paint.fill && !paint.stroke) return;
            if (Math.abs(svgAngle(m)) < 0.5) {
                const r = norm(el.getBoundingClientRect());
                if (r.w <= 0 && r.h <= 0) return;
                const rx = tag === 'rect' ? px(cs.rx === 'auto' ? 0 : cs.rx) * Math.sqrt(Math.abs(m.a * m.d)) / scale : 0;
                emit({kind: 'box', key: key + ':g', rect: r, fill: paint.fill, line: paint.stroke, radius: round(rx), ellipse: tag !== 'rect', op}, ctx);
                return;
            }
        }
        let cmds = null;
        if (tag === 'path') cmds = pathCommands(el, m);
        else if (tag === 'polygon' || tag === 'polyline') {
            const pts = Array.from(el.points).map(p => svgPoint(m, p.x, p.y));
            cmds = pts.map((p, i) => [i ? 'L' : 'M', p[0], p[1]]);
            if (tag === 'polygon') cmds.push(['Z']);
        } else if (el.getTotalLength) cmds = sampledPath(el, m);
        if (cmds && cmds.length > 1 && (paint.fill || paint.stroke)) {
            emit({kind: 'freeform', key: key + ':g', cmds, fill: tag === 'polyline' && cs.fill === 'none' ? null : paint.fill, stroke: paint.stroke, op}, ctx);
        }
    }

    function emitSvg(svg, ctx) {
        const nodes = svg.querySelectorAll('*').length;
        if (nodes > SVG_NODE_LIMIT || svg.querySelector('use,image,foreignObject')) return emitSnapshot(svg, ctx, 'complex figure');
        const t = svg.querySelector('title');
        const name = svg.getAttribute('aria-label') || (t && t.textContent) || 'Chart';
        const c = Object.assign({}, ctx, {groups: ctx.groups.concat([{id: idOf(svg), name: name.trim().slice(0, 48)}])});
        for (const kid of svg.children) walkSvgNode(kid, c);
    }

    // ------------------------------------------------------------------ HTML
    function walk(el, parentCtx) {
        if (SKIP_TAGS.has(el.tagName) || SKIP_CLASSES.some(c => el.classList.contains(c))) return;
        const cs = getComputedStyle(el);
        if (isHidden(cs)) return;
        const op = parentCtx.op * px(cs.opacity);
        if (op < 0.02) return;
        const r = el.getBoundingClientRect();
        const k = parentCtx.k;
        const ctx = Object.assign({}, parentCtx, {op});
        const tag = el.localName;

        if (tag === 'svg') return emitSvg(el, ctx);
        if (tag === 'img') return emitImage(el, cs, ctx);
        if (tag === 'table') return emitTable(el, ctx);
        if (['iframe', 'video', 'canvas', 'object', 'embed'].includes(tag)) return emitSnapshot(el, ctx, tag);
        if (isKatex(el)) {
            const math = el.querySelector('math'), runs = [];
            if (math) mathRuns(math, runStyle(el, cs, {op, k}), 0, runs);
            if (runs.length) emit(textObject(el, cs, ctx, [{pkey: String(idOf(el)), runs, align: 'ctr', ln: null, top: null, bottom: null, op}]), ctx);
            return;
        }

        const box = boxOf(el, cs, ctx);
        const painted = !!(box.fill || box.grad || box.line || box.url);
        if (isReveal(el) || painted) ctx.groups = ctx.groups.concat([{id: idOf(el), name: snippet(el) || (painted ? 'Box' : 'Step')}]);

        ctx.z = zOf(cs, ctx.z);
        const start = out.length;
        let boxObj = null;
        if (painted && r.width > 0.5 && r.height > 0.5) {
            const circle = box.radius * 2 >= Math.min(r.width, r.height) / scale - 0.5 && Math.abs(r.width - r.height) < 1;
            boxObj = emit({kind: 'box', key: idOf(el) + ':b', rect: norm(r), fill: box.fill, grad: box.grad, line: box.line, radius: box.radius, ellipse: circle, url: box.url, op}, ctx);
        }
        pseudoBox(el, '::before', ctx);
        if ((tag === 'ul' || tag === 'ol') && simpleList(el)) {
            emitList(el, ctx);
        } else if (/flex|grid/.test(cs.display)) {
            let n = 0;
            for (const node of el.childNodes) {
                if (node.nodeType === Node.ELEMENT_NODE) walk(node, ctx);
                else if (node.nodeType === Node.TEXT_NODE && node.nodeValue.trim()) emitTextNode(node, el, cs, ctx, n++);
            }
        } else if (hasInlineContent(el)) {
            const {paras, deferred} = parasOf(el, {op, k});
            if (paras.length) { spacing(paras); emit(textObject(el, cs, ctx, paras), ctx); }
            deferred.forEach(d => walk(d, ctx));
        } else {
            for (const child of el.children) walk(child, ctx);
        }

        // A painted element holding one text block becomes one shape with its text inside.
        const inner = boxObj && out.length === start + 2 && out[start + 1].kind === 'text' ? out[start + 1] : null;
        const fits = inner && inner.rect.y >= boxObj.rect.y - 2 && inner.rect.y + inner.rect.h <= boxObj.rect.y + boxObj.rect.h + 2
            && inner.rect.x >= boxObj.rect.x - 2 && inner.rect.x + inner.rect.w <= boxObj.rect.x + boxObj.rect.w + 2;
        if (fits && !boxObj.url && !boxObj.grad) {
            const t = out.pop();
            const b = boxObj.rect, q = t.rect;
            boxObj.kind = 'textbox';
            boxObj.key = t.key;
            boxObj.paras = t.paras; boxObj.lines = t.lines; boxObj.nowrap = t.nowrap; boxObj.isList = t.isList;
            const top = q.y - b.y, bottom = b.y + b.h - q.y - q.h;
            boxObj.anchor = Math.abs(top - bottom) < 3 ? 'ctr' : 't';
            boxObj.ins = {l: round(Math.max(q.x - b.x, 0)), r: round(Math.max(b.x + b.w - q.x - q.w, 0)), t: round(Math.max(top, 0)), b: round(Math.max(bottom, 0))};
        }
        if (box.sides.length) emitSideLines(el, box, ctx);
        pseudoBox(el, '::after', ctx);
        const link = tag === 'a' ? el : el.closest('a');
        if (link) {
            for (let i = start; i < out.length; i++) {
                out[i].link = link.getAttribute('href');
                (out[i].paras || []).forEach(p => p.runs.forEach(run => { delete run.link; }));
            }
        }
    }

    function capture(root, designWidth) {
        const rr = root.getBoundingClientRect();
        origin = rr; scale = rr.width / designWidth; out = [];
        const cs = getComputedStyle(root);
        const ctx = {op: 1, k: 1, groups: [], z: 0};
        const rootBox = boxOf(root, cs, ctx);
        if (rootBox.url) emit({kind: 'image', key: idOf(root) + ':bg', rect: norm(rr), src: rootBox.url, alt: '', op: 1}, ctx);
        if (rootBox.grad) emit({kind: 'box', key: idOf(root) + ':grad', rect: norm(rr), grad: rootBox.grad, radius: 0, op: 1}, ctx);
        pseudoBox(root, '::before', ctx);
        for (const child of root.children) walk(child, ctx);
        pseudoBox(root, '::after', ctx);
        out.sort((a, b) => a.z - b.z);
        const heading = root.querySelector('h1, h2');
        if (heading) {
            const key = idOf(heading) + ':t';
            const t = out.find(o => o.key === key);
            if (t) t.role = 'title';
        }
        const notes = root.querySelector('aside.notes, aside');
        return {objects: out, bg: color(cs.backgroundColor), notes: notes ? notes.textContent.trim() : ''};
    }

    window.PX = {capture};
})();

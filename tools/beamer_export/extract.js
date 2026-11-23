// Browser-side semantic extraction for WebSlides -> Beamer.
// Evaluated with page.evaluate(EXTRACT_JS, {slideIndex}) after WS.goToSlide(i, maxStep).
// Returns a JSON-serialisable tree for one slide, with computed styles and rects
// normalised to the 1280x720 slide coordinate system.
(args) => {
    const slideIndex = args.slideIndex;
    const slide = WS.slides[slideIndex];
    if (!slide) throw new Error('No slide ' + slideIndex);

    const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT', 'LINK', 'META']);
    const SKIP_CLASSES = ['column-overlay', 'ws-table-config-error', 'ws-table-load-error'];
    const INLINE_DISPLAYS = new Set(['inline', 'inline-block', 'inline-flex', 'inline-grid', 'inline-table', 'contents', 'ruby']);

    const sr = slide.getBoundingClientRect();
    const scale = sr.width / 1280 || 1;

    function px(v) { const n = parseFloat(v); return isNaN(n) ? 0 : n; }
    function nrect(r) {
        return {
            x: Math.round((r.left - sr.left) / scale * 10) / 10,
            y: Math.round((r.top - sr.top) / scale * 10) / 10,
            w: Math.round(r.width / scale * 10) / 10,
            h: Math.round(r.height / scale * 10) / 10,
        };
    }
    function rectOf(el) { return nrect(el.getBoundingClientRect()); }
    function textRect(node) {
        const range = document.createRange();
        range.selectNodeContents(node);
        const rects = range.getClientRects();
        if (!rects.length) return null;
        let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
        for (const rc of rects) {
            if (rc.width === 0 && rc.height === 0) continue;
            l = Math.min(l, rc.left); t = Math.min(t, rc.top);
            r = Math.max(r, rc.right); b = Math.max(b, rc.bottom);
        }
        if (l === Infinity) return null;
        return nrect({left: l, top: t, width: r - l, height: b - t});
    }

    function styleOf(el) {
        const cs = getComputedStyle(el);
        const fs = px(cs.fontSize);
        return {
            display: cs.display,
            position: cs.position,
            fs: fs,
            fw: parseInt(cs.fontWeight, 10) || 400,
            fst: cs.fontStyle,
            ff: cs.fontFamily,
            lh: cs.lineHeight === 'normal' ? Math.round(fs * 1.2 * 10) / 10 : px(cs.lineHeight),
            color: cs.color,
            bg: cs.backgroundColor,
            ta: cs.textAlign,
            tt: cs.textTransform,
            ls: cs.letterSpacing,
            td: cs.textDecorationLine,
            op: px(cs.opacity),
            vis: cs.visibility,
            bl: px(cs.borderLeftWidth), br: px(cs.borderRightWidth),
            bt: px(cs.borderTopWidth), bb: px(cs.borderBottomWidth),
            blc: cs.borderLeftColor, brc: cs.borderRightColor,
            btc: cs.borderTopColor, bbc: cs.borderBottomColor,
            radius: px(cs.borderTopLeftRadius),
            pl: px(cs.paddingLeft), pr: px(cs.paddingRight),
            pt: px(cs.paddingTop), pb: px(cs.paddingBottom),
            ml: cs.marginLeft, mr: cs.marginRight, mt: cs.marginTop, mb: cs.marginBottom,
            gtc: cs.gridTemplateColumns, gtr: cs.gridTemplateRows,
            fd: cs.flexDirection, jc: cs.justifyContent, ai: cs.alignItems,
            gap: cs.gap, ws: cs.whiteSpace,
            objectFit: cs.objectFit, objectPosition: cs.objectPosition,
            lst: cs.listStyleType,
            va: cs.verticalAlign,
            transform: cs.transform,
            shadow: cs.boxShadow !== 'none',
        };
    }

    // Does any matching CSS rule (or inline style) give this element an auto top margin
    // or a flex-grow (i.e. is it a flexible spacer in a flex column)?
    const ruleCache = [];
    for (const ss of document.styleSheets) {
        let rules;
        try { rules = ss.cssRules; } catch (e) { continue; }
        if (!rules) continue;
        for (const rule of rules) {
            if (rule.style && rule.selectorText) ruleCache.push(rule);
        }
    }
    function matchedDecl(el, prop, pseudoSel) {
        let val = null;
        if (!pseudoSel && el.style && el.style.getPropertyValue(prop)) val = el.style.getPropertyValue(prop);
        for (const rule of ruleCache) {
            let sel = rule.selectorText;
            if (pseudoSel) {
                if (!sel.includes(pseudoSel)) continue;
                sel = sel.split(',').map(s => s.trim()).filter(s => s.endsWith(pseudoSel)).map(s => s.slice(0, -pseudoSel.length)).join(',');
                if (!sel) continue;
            } else if (sel.includes('::')) continue;
            let ok = false;
            try { ok = el.matches(sel); } catch (e) { ok = false; }
            if (!ok) continue;
            const v = rule.style.getPropertyValue(prop);
            if (v) val = v;   // later rules win (rough cascade; good enough for auto detection)
        }
        return val;
    }
    function flexInfo(el) {
        const mt = matchedDecl(el, 'margin-top') || '';
        const m = matchedDecl(el, 'margin') || '';
        const fg = parseFloat(getComputedStyle(el).flexGrow) || 0;
        return {
            autoTop: mt.trim() === 'auto' || /^auto(\s|$)/.test(m.trim()) || /\sauto\s+\S+\s+\S+$/.test(m.trim()),
            grow: fg,
        };
    }

    function pseudo(el, which) {
        const cs = getComputedStyle(el, which);
        const content = cs.content;
        if (!content || content === 'none' || content === 'normal') return null;
        const mt = matchedDecl(el, 'margin-top', which) || '';
        return {
            content: content,
            color: cs.color,
            bg: cs.backgroundColor,
            w: px(cs.width), h: px(cs.height),
            display: cs.display,
            position: cs.position,
            mt: px(cs.marginTop),
            autoTop: mt.trim() === 'auto',
        };
    }

    // Relative steps ("+", "=", "+:2") only resolve in the context of the
    // slide; hand the Python side the engine's absolute range instead.
    const RESOLVED_ATTRS = {'data-step': 'step', 'data-fade-after': 'fade-after'};
    function resolvedValue(el, name, value) {
        if (!RESOLVED_ATTRS[name] || !/^\s*[+=]/.test(value) || typeof WS.getRevealRange !== 'function') return value;
        const r = WS.getRevealRange(el, RESOLVED_ATTRS[name]);
        if (!r) return 'unresolved';  // never parses: as hidden as the engine shows it
        return r.end === Infinity ? String(r.start) : r.start + '-' + r.end;
    }

    function dataAttrs(el) {
        const out = {};
        for (const a of el.attributes) {
            if (a.name.startsWith('data-') || ['href', 'alt', 'src', 'style', 'poster', 'title', 'colspan', 'rowspan'].includes(a.name)) {
                out[a.name] = resolvedValue(el, a.name, a.value);
            }
        }
        return out;
    }

    // outerHTML with relative data-step values replaced by resolved ones
    function resolvedOuterHTML(el) {
        const clone = el.cloneNode(true);
        const src = [el].concat(Array.from(el.querySelectorAll('*')));
        const dst = [clone].concat(Array.from(clone.querySelectorAll('*')));
        src.forEach((node, i) => {
            for (const name of Object.keys(RESOLVED_ATTRS)) {
                if (node.hasAttribute(name)) dst[i].setAttribute(name, resolvedValue(node, name, node.getAttribute(name)));
            }
        });
        return clone.outerHTML;
    }

    function hasSkipClass(el) {
        return SKIP_CLASSES.some(c => el.classList.contains(c));
    }

    function katexTex(el) {
        // Math with \stat{NAME}: the annotation keeps the authored TeX (what
        // editors save); data-ws-tex holds it with the values filled in.
        if (el.hasAttribute('data-ws-tex')) return el.getAttribute('data-ws-tex');
        const ann = el.querySelector('annotation[encoding="application/x-tex"]');
        return ann ? ann.textContent : null;
    }

    function walk(node) {
        if (node.nodeType === Node.TEXT_NODE) {
            const s = node.nodeValue;
            if (!s) return null;
            if (/^\s*$/.test(s)) return {t: 'ws', s: ' '};
            return {t: 'text', s: s, rect: textRect(node)};
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return null;
        const el = node;
        const tag = el.tagName.toLowerCase();
        if (SKIP_TAGS.has(el.tagName)) return null;
        if (hasSkipClass(el)) return null;

        const st = styleOf(el);
        if (st.display === 'none' || st.vis === 'hidden') return null;

        const base = {
            t: 'el', tag: tag, id: el.id || null,
            cls: Array.from(el.classList),
            attrs: dataAttrs(el),
            style: st,
            rect: rectOf(el),
        };
        base.inline = INLINE_DISPLAYS.has(st.display);
        base.flex = flexInfo(el);

        if (tag === 'aside' && el.classList.contains('notes')) {
            return {t: 'notes', text: el.innerText, rect: base.rect};
        }
        if (tag === 'br') { base.t = 'br'; return base; }
        if (tag === 'img') {
            base.t = 'img';
            base.src = el.currentSrc || el.src;
            base.nw = el.naturalWidth; base.nh = el.naturalHeight;
            base.alt = el.alt || '';
            return base;
        }
        if (tag === 'svg') {
            base.t = 'svg';
            base.html = resolvedOuterHTML(el);
            base.viewBox = el.getAttribute('viewBox');
            base.steps = Array.from(el.querySelectorAll('[data-step]')).map(g => resolvedValue(g, 'data-step', g.getAttribute('data-step')));
            return base;
        }
        if (tag === 'video' || tag === 'iframe' || tag === 'object' || tag === 'embed' || tag === 'canvas') {
            base.t = tag;
            base.src = el.getAttribute('src') || (el.querySelector('source') ? el.querySelector('source').getAttribute('src') : null);
            base.poster = el.getAttribute('poster');
            return base;
        }
        if (el.classList.contains('katex-display')) {
            const k = el.querySelector('.katex');
            return {t: 'math', tex: k ? katexTex(k) : el.textContent, display: true, rect: base.rect, style: st};
        }
        if (el.classList.contains('katex')) {
            return {t: 'math', tex: katexTex(el) || el.textContent, display: false, rect: base.rect, style: st};
        }
        if (el.hasAttribute('data-table-src') || tag === 'table') {
            base.t = 'table';
            const tbl = tag === 'table' ? el : el.querySelector('table');
            base.src = el.getAttribute('data-table-src');
            base.overlays = el.getAttribute('data-overlays');
            // innerHTML minus overlay chrome
            const clone = el.cloneNode(true);
            clone.querySelectorAll('.column-overlay, .ws-table-config-error').forEach(n => n.remove());
            base.html = tag === 'table' ? clone.outerHTML : clone.innerHTML;
            if (tbl) {
                const tcs = getComputedStyle(tbl);
                base.tableFs = px(tcs.fontSize);
                base.tableRect = rectOf(tbl);
                base.tableFf = tcs.fontFamily;
                const td = tbl.querySelector('tbody td');
                if (td) {
                    const tdcs = getComputedStyle(td);
                    base.tdPadX = px(tdcs.paddingLeft); base.tdPadY = px(tdcs.paddingTop);
                }
                // Table scale from transform matrix
                const m = tcs.transform;
                if (m && m.startsWith('matrix(')) {
                    base.tableScale = parseFloat(m.slice(7).split(',')[0]);
                } else base.tableScale = 1;
                const heads = Array.from(tbl.querySelectorAll('thead tr'));
                base.headerCells = heads.length ? Math.max(...heads.map(r => r.querySelectorAll('th,td').length)) : 0;
                const firstBody = tbl.querySelector('tbody tr');
                base.bodyCells = firstBody ? firstBody.querySelectorAll('td,th').length : 0;
                base.bodyRows = tbl.querySelectorAll('tbody tr').length;
            }
            return base;
        }

        if (tag === 'li') base.before = pseudo(el, '::before');
        if (tag === 'h1' || tag === 'h2') base.after = pseudo(el, '::after');
        if (tag === 'a') base.href = el.getAttribute('href');

        const children = [];
        for (const c of el.childNodes) {
            const r = walk(c);
            if (r) children.push(r);
        }
        base.children = children;
        base.textLen = (el.innerText || '').trim().length;
        return base;
    }

    const slideStyle = styleOf(slide);
    const out = {
        index: slideIndex,
        id: slide.id || null,
        cls: Array.from(slide.classList),
        attrs: dataAttrs(slide),
        mainNumber: slide.dataset.wsMainNumber ? parseInt(slide.dataset.wsMainNumber, 10) : null,
        isAppendix: slide.classList.contains('appendix'),
        maxStep: WS.getExportInfo()[slideIndex].maxStep,
        style: slideStyle,
        rect: rectOf(slide),
        children: [],
        innerText: (slide.innerText || '').trim(),
        after: pseudo(slide, '::after'),
    };
    for (const c of slide.childNodes) {
        const r = walk(c);
        if (r) out.children.push(r);
    }
    return out;
}

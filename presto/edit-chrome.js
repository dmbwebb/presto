/* Presto edit chrome: the slide rail, the step strip, the zoom label, the link
 * card and the Back chip around the edit-mode slide preview.
 *
 * Injected by Presto.swift into the edit preview after presto/edit-chrome.css
 * and before presto/wysiwyg.js (and by the browser test harnesses). Everything
 * here is app chrome outside the slides: it never writes deck content itself.
 * Link-target edits go through window.__prestoWysiwyg so they are undoable and
 * saved like any other visual edit.
 *
 * API (window.PrestoEditChrome):
 *   install(opts)            opts = { railVisible: true, ghostFuture: true }
 *   rebuild()                rebuild the rail (slide count or sections changed)
 *   refreshThumbnail(index)  re-render one rail thumbnail (after an edit)
 *   setRailVisible(bool)     show or hide the rail (re-applies PrestoLayout)
 *   goToSlide(index, step, {recordBack})  navigate; recordBack adds a Back chip
 *   back()                   return to the slide and step of the last link jump
 *   setGhostFuture(bool)     "Show later steps faintly" (shell's menu item)
 *   resolveLink(a) / followLink(a)  used by wysiwyg.js for links and ⌘-click
 *   closePopover()           close the link card's target list (Escape order)
 *
 * Messages to Swift (webkit.messageHandlers.presto):
 *   { type: 'editChromeReady' }
 *   { type: 'sidebarNavigate', index, step }   rail clicks and link jumps
 *   { type: 'ghostFutureChanged', on }         the strip's toggle flipped
 *
 * Geometry comes from the `presto:layout` event (presto/edit-layout.js); when
 * PrestoLayout is absent the active slide is measured instead.
 */

(function () {
    'use strict';

    if (window.PrestoEditChrome) return;

    var RAIL_W = 232;
    var STRIP_BAND = 72;
    var LAYOUT_PADDING = 28;
    var SLIDE_W = 1280;
    var THUMB_W = 180;
    var CARD_THUMB_W = 112;
    var MAX_RAIL_DOTS = 7;
    var DENSE_STEP_COUNT = 16;
    var MUTATION_IDLE_MS = 300;
    var RENDER_PER_FRAME = 3;
    var BACK_STACK_MAX = 10;
    var REVEAL_SELECTOR = '[data-step], [data-highlight], [data-dim], [data-class]';

    var state = {
        installed: false,
        railVisible: true,
        ghost: true,
        geometry: null,
        layoutOpts: { mode: 'edit', insetLeft: RAIL_W, insetRight: 0, insetTop: 0, insetBottom: STRIP_BAND, padding: LAYOUT_PADDING },
        groups: [],
        folded: {},
        items: [],
        renderQueue: [],
        renderRaf: null,
        mutationTimer: null,
        mutationPending: {},
        observer: null,
        mutations: null,
        stripMax: -1,
        stripIndex: -1,
        backStack: [],
        pendingJump: null,
        moves: 0,
        lastIndex: -1,
        link: null,
        linkTarget: null,
        cardKey: null,
        cardMode: 'card',
        pickRows: [],
        pickActive: 0,
        measureRaf: null
    };

    var dom = {
        rail: null,
        scroll: null,
        strip: null,
        steps: null,
        ghostToggle: null,
        zoom: null,
        back: null,
        card: null
    };

    var testMessageSink = null;

    /* ===== Utilities ===== */

    function post(msg) {
        if (testMessageSink) {
            try { testMessageSink(msg); } catch (e) { /* test hook must not break the chrome */ }
        }
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.presto) {
            window.webkit.messageHandlers.presto.postMessage(msg);
        }
    }

    function inPresto() {
        return !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.presto);
    }

    function wsReady() {
        return typeof window.WS !== 'undefined' && WS.slides && WS.slides.length > 0;
    }

    function wysiwyg() {
        return window.__prestoWysiwyg || null;
    }

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function svg(paths, viewBox, cls) {
        return '<svg class="' + (cls || '') + '" viewBox="' + (viewBox || '0 0 16 16') + '" aria-hidden="true">' + paths + '</svg>';
    }

    var ICON = {
        chevron: svg('<path d="M2.5 4.5L6 8l3.5-3.5"/>', '0 0 12 12', 'pec-sec-chev'),
        back: svg('<path d="M10 4L6 8l4 4"/>'),
        link: svg('<path d="M6.5 9.5l3-3M7 4.5l1-1a2.8 2.8 0 014 4l-1 1M9 11.5l-1 1a2.8 2.8 0 01-4-4l1-1"/>', '0 0 16 16', 'pec-ico'),
        go: svg('<path d="M4 8h8M8.5 4.5L12 8l-3.5 3.5"/>'),
        check: svg('<path d="M3.5 8.5l3 3 6-7"/>', '0 0 16 16', 'pec-ico')
    };

    function isAppendix(slide) {
        return !!(slide && slide.classList && slide.classList.contains('appendix'));
    }

    function slideAt(index) {
        return wsReady() ? WS.slides[index] || null : null;
    }

    // "18" for main slides, "A4" for appendix slides.
    function slideLabel(index) {
        var slide = slideAt(index);
        if (!slide) return String(index + 1);
        if (isAppendix(slide)) {
            var pos = WS.appendixSlides ? WS.appendixSlides.indexOf(slide) : -1;
            if (pos < 0) {
                pos = 0;
                for (var i = 0; i < index; i++) if (isAppendix(WS.slides[i])) pos++;
            }
            return 'A' + (pos + 1);
        }
        return slide.dataset.wsMainNumber || String(index + 1);
    }

    // Orders renders against mutations exactly (WebKit coarsens timestamps
    // to the millisecond, so times can tie).
    var sequence = 0;
    function nextSeq() {
        sequence += 1;
        return sequence;
    }

    function cleanText(text) {
        return String(text || '').replace(/\s+/g, ' ').trim();
    }

    // An element's words as the audience reads them: line breaks become
    // spaces and KaTeX's hidden MathML copy is dropped.
    function readableText(node) {
        var copy = node.cloneNode(true);
        copy.querySelectorAll('.katex-mathml').forEach(function (n) { n.remove(); });
        copy.querySelectorAll('br').forEach(function (n) { n.replaceWith(' '); });
        return cleanText(copy.textContent);
    }

    function slideTitle(slide) {
        if (!slide) return '';
        var heads = slide.querySelectorAll('h1, h2, h3');
        for (var i = 0; i < heads.length; i++) {
            var h = heads[i];
            if (h.closest('.ws-section-eyebrow, .section-toc, aside.notes, .presto-wysiwyg-chrome')) continue;
            var t = readableText(h);
            if (t) return t;
        }
        var block = slide.querySelector('p, li, figcaption');
        var fallback = block ? readableText(block) : '';
        if (fallback.length > 60) fallback = fallback.slice(0, 57).replace(/\s+\S*$/, '') + '…';
        return fallback || 'Untitled slide';
    }

    function maxStepOf(slide) {
        if (!slide || !wsReady()) return 0;
        var max = WS.slideSteps && WS.slideSteps.get ? WS.slideSteps.get(slide) : 0;
        return Number.isSafeInteger(max) && max > 0 ? max : 0;
    }

    function revealRange(node, attr) {
        if (window.WS && typeof WS.getRevealRange === 'function') {
            try { return WS.getRevealRange(node, attr); } catch (e) { return null; }
        }
        var raw = node.getAttribute('data-' + attr);
        if (raw === null) return null;
        if (attr === 'class') {
            var sep = raw.lastIndexOf(':');
            if (sep <= 0) return null;
            var r = parseRange(raw.slice(sep + 1));
            if (r) r.className = raw.slice(0, sep).trim();
            return r;
        }
        return parseRange(raw);
    }

    function parseRange(raw) {
        if (window.WS && typeof WS.parseRevealRange === 'function') return WS.parseRevealRange(raw);
        var m = /^\s*(\d+)\s*(?:(\+)|-\s*(\d+))?\s*$/.exec(String(raw === undefined ? '' : raw));
        if (!m) return String(raw).trim() === '' ? { start: 1, end: Infinity } : null;
        var start = parseInt(m[1], 10);
        var end = m[3] !== undefined ? parseInt(m[3], 10) : Infinity;
        return end < start ? null : { start: start, end: end };
    }

    function rangeActive(range, step) {
        return !!range && step >= range.start && step <= range.end;
    }

    function slideIndexOf(node) {
        if (!wsReady() || !node) return -1;
        var slide = node.classList && node.classList.contains('slide') ? node : node.closest && node.closest('.slide');
        return slide ? WS.slides.indexOf(slide) : -1;
    }

    /* ===== Thumbnails ===== */

    // Freeze a clone at the slide's final reveal step (what the slide finally
    // looks like), using the engine's resolved ranges on the original nodes.
    function freezeFinalState(original, clone) {
        var max = maxStepOf(original);
        var from = original.querySelectorAll(REVEAL_SELECTOR);
        var to = clone.querySelectorAll(REVEAL_SELECTOR);
        if (from.length !== to.length) {
            for (var k = 0; k < to.length; k++) to[k].classList.add('ws-visible');
            return;
        }
        for (var i = 0; i < from.length; i++) {
            var o = from[i];
            var c = to[i];
            if (o.hasAttribute('data-step')) c.classList.toggle('ws-visible', rangeActive(revealRange(o, 'step'), max));
            if (o.hasAttribute('data-highlight')) c.classList.toggle('ws-highlighted', rangeActive(revealRange(o, 'highlight'), max));
            if (o.hasAttribute('data-dim')) c.classList.toggle('ws-dimmed', rangeActive(revealRange(o, 'dim'), max));
            if (o.hasAttribute('data-class')) {
                var cr = revealRange(o, 'class');
                if (cr && cr.className) c.classList.toggle(cr.className, rangeActive(cr, max));
            }
        }
        clone.querySelectorAll('.ws-faded').forEach(function (n) { n.classList.remove('ws-faded'); });
    }

    function cleanClone(clone) {
        clone.classList.remove('ws-active');
        clone.querySelectorAll('.presto-wysiwyg-chrome, aside.notes, script, iframe').forEach(function (n) { n.remove(); });
        clone.querySelectorAll('[contenteditable]').forEach(function (n) {
            n.removeAttribute('contenteditable');
            n.removeAttribute('spellcheck');
        });
        clone.querySelectorAll('[data-pw-atom-lock]').forEach(function (n) { n.removeAttribute('data-pw-atom-lock'); });
        [clone].concat(Array.prototype.slice.call(clone.querySelectorAll('[class*="presto-wysiwyg"]'))).forEach(function (n) {
            n.classList.remove('presto-wysiwyg-selected', 'presto-wysiwyg-hover', 'presto-wysiwyg-editing');
        });
        clone.querySelectorAll('video').forEach(function (v) {
            v.removeAttribute('autoplay');
            v.setAttribute('preload', 'none');
            try { v.pause(); } catch (e) { /* not playing */ }
        });
        // Duplicate ids outside SVG would make document-wide id queries ambiguous;
        // SVG keeps them so its url(#...) references still resolve.
        if (clone.id) clone.removeAttribute('id');
        clone.querySelectorAll('[id]').forEach(function (n) {
            if (!n.closest('svg')) n.removeAttribute('id');
        });
        clone.querySelectorAll('.katex').forEach(function (k) {
            k.style.removeProperty('zoom');
        });
        clone.style.removeProperty('zoom');
        clone.style.removeProperty('transform');
        clone.style.removeProperty('--ws-katex-zoom');
        clone.removeAttribute('data-ws-index');
        clone.setAttribute('aria-hidden', 'true');
        clone.setAttribute('inert', '');
    }

    function makeThumbCanvas(slide, width) {
        var canvas = el('div', 'pec-canvas');
        canvas.style.transform = 'scale(' + (width / SLIDE_W) + ')';
        var clone = slide.cloneNode(true);
        freezeFinalState(slide, clone);
        cleanClone(clone);
        canvas.appendChild(clone);
        return canvas;
    }

    function renderItem(index) {
        var item = state.items[index];
        var slide = slideAt(index);
        if (!item || !slide) return;
        var canvas;
        try {
            canvas = makeThumbCanvas(slide, THUMB_W);
        } catch (e) {
            canvas = el('div', 'pec-placeholder');
        }
        var old = item.pic.querySelector('.pec-canvas, .pec-placeholder');
        if (old) item.pic.replaceChild(canvas, old);
        else item.pic.insertBefore(canvas, item.pic.firstChild);
        item.rendered = true;
        item.dirty = false;
        item.renderedSeq = nextSeq();
        renderDots(item, slide);
    }

    function renderDots(item, slide) {
        var dots = item.pic.querySelector('.pec-dots');
        var n = Math.min(MAX_RAIL_DOTS, maxStepOf(slide));
        if (dots.childElementCount === n) return;
        dots.innerHTML = '';
        for (var i = 0; i < n; i++) dots.appendChild(document.createElement('i'));
    }

    function queueRender(index) {
        if (state.renderQueue.indexOf(index) < 0) state.renderQueue.push(index);
        if (state.renderRaf !== null) return;
        state.renderRaf = (window.requestAnimationFrame || setTimeout)(drainRenderQueue);
    }

    function drainRenderQueue() {
        state.renderRaf = null;
        var current = wsReady() ? WS.currentIndex : -1;
        // The current slide's thumbnail first: it is the one being edited.
        state.renderQueue.sort(function (a, b) { return (a === current ? -1 : 0) - (b === current ? -1 : 0); });
        var batch = state.renderQueue.splice(0, RENDER_PER_FRAME);
        batch.forEach(renderItem);
        if (state.renderQueue.length) {
            state.renderRaf = (window.requestAnimationFrame || setTimeout)(drainRenderQueue);
        }
    }

    function refreshThumbnail(index) {
        var i = typeof index === 'number' ? index : parseInt(index, 10);
        if (!isFinite(i)) return false;
        if (!wsReady() || !dom.rail) return false;
        if (state.items.length !== WS.slides.length) { rebuild(); return true; }
        var item = state.items[i];
        if (!item) return false;
        item.dirty = true;
        // A replaced slide element still renders from WS.slides[i].
        if (item.visible || !state.observer) queueRender(i);
        renderDots(item, slideAt(i));
        if (i === WS.currentIndex) updateStrip(true);
        if (state.link) renderCardSoon();
        return true;
    }

    function refreshAllThumbnails() {
        state.items.forEach(function (item, i) { if (item) refreshThumbnail(i); });
    }

    // Any DOM change inside a slide (typing, drag, undo, an outside patch, a
    // table finishing loading) re-renders that slide's thumbnail once things
    // go quiet. Reveal-state and editing-chrome toggles are ignored.
    var IGNORED_CLASS = /^(ws-|presto-wysiwyg-)/;

    function classChangeMatters(record) {
        var before = (record.oldValue || '').split(/\s+/).filter(Boolean);
        var after = (record.target.getAttribute('class') || '').split(/\s+/).filter(Boolean);
        var diff = before.filter(function (c) { return after.indexOf(c) < 0; })
            .concat(after.filter(function (c) { return before.indexOf(c) < 0; }));
        if (record.target.dataset && record.target.dataset.class) {
            var token = record.target.dataset.class.slice(0, record.target.dataset.class.lastIndexOf(':')).trim();
            diff = diff.filter(function (c) { return c !== token; });
        }
        return diff.some(function (c) { return !IGNORED_CLASS.test(c); });
    }

    function onMutations(records) {
        if (!wsReady()) return;
        var touched = false;
        records.forEach(function (record) {
            var target = record.target.nodeType === 1 ? record.target : record.target.parentElement;
            if (!target) return;
            if (target.closest && target.closest('.presto-wysiwyg-chrome')) return;
            if (record.type === 'attributes') {
                var name = record.attributeName;
                if (name === 'contenteditable' || name === 'spellcheck' || name === 'data-pw-atom-lock' ||
                    name === 'aria-hidden' || name === 'data-ws-index') return;
                // Layout writes zoom/left/top on the slides themselves.
                if (name === 'style' && target.classList && target.classList.contains('slide')) return;
                if (name === 'class' && !classChangeMatters(record)) return;
            }
            var index = slideIndexOf(target);
            if (index < 0) {
                // A whole slide replaced: the deck's child list changed.
                if (record.type === 'childList' && target.classList && target.classList.contains('ws-deck')) {
                    record.addedNodes.forEach(function (n) {
                        var i = slideIndexOf(n);
                        if (i >= 0) { state.mutationPending[i] = nextSeq(); touched = true; }
                    });
                }
                return;
            }
            state.mutationPending[index] = nextSeq();
            touched = true;
        });
        if (!touched) return;
        clearTimeout(state.mutationTimer);
        state.mutationTimer = setTimeout(flushMutations, MUTATION_IDLE_MS);
    }

    // A thumbnail already rendered after the slide's last change (wysiwyg's
    // own refresh after a commit) is not rendered again.
    function flushMutations() {
        state.mutationTimer = null;
        var pending = state.mutationPending;
        state.mutationPending = {};
        Object.keys(pending).forEach(function (key) {
            var i = parseInt(key, 10);
            var item = state.items[i];
            if (item && item.rendered && !item.dirty && item.renderedSeq >= pending[key]) return;
            refreshThumbnail(i);
        });
    }

    function watchDeck() {
        if (state.mutations || typeof MutationObserver !== 'function') return;
        var deck = document.querySelector('.ws-deck');
        if (!deck) return;
        state.mutations = new MutationObserver(onMutations);
        state.mutations.observe(deck, {
            subtree: true,
            childList: true,
            characterData: true,
            attributes: true,
            attributeOldValue: true
        });
    }

    /* ===== Rail ===== */

    // Main slides grouped under the deck's sections (WS.sections, in order),
    // appendix slides last. Slides before the first section with no virtual
    // intro form an unnamed group shown without a header.
    function computeGroups() {
        var groups = [];
        var sections = (WS.sections || []).slice().sort(function (a, b) { return a.slideIndex - b.slideIndex; });
        var current = null;
        WS.slides.forEach(function (slide, i) {
            if (isAppendix(slide)) return;
            var sec = null;
            for (var s = sections.length - 1; s >= 0; s--) {
                if (sections[s].slideIndex <= i) { sec = sections[s]; break; }
            }
            var key = sec ? 'sec-' + sec.slideIndex : 'sec-none';
            if (!current || current.key !== key) {
                current = { key: key, name: sec ? sec.name : null, slides: [] };
                groups.push(current);
            }
            current.slides.push(i);
        });
        var appendix = [];
        WS.slides.forEach(function (slide, i) { if (isAppendix(slide)) appendix.push(i); });
        if (appendix.length) groups.push({ key: 'appendix', name: 'Appendix', slides: appendix, appendix: true });
        return groups;
    }

    function rangeLabel(group) {
        var first = slideLabel(group.slides[0]);
        var last = slideLabel(group.slides[group.slides.length - 1]);
        return first === last ? first : first + '–' + last;
    }

    function buildRail() {
        var rail = el('nav', 'presto-edit-chrome');
        rail.id = 'presto-edit-rail';
        rail.setAttribute('aria-label', 'Slides');
        var scroll = el('div', 'pec-rail-scroll');
        rail.appendChild(scroll);
        rail.addEventListener('click', onRailClick);
        rail.addEventListener('keydown', onRailKeyDown);
        document.body.appendChild(rail);
        dom.rail = rail;
        dom.scroll = scroll;
    }

    function rebuild() {
        if (!dom.rail || !wsReady()) return;
        if (state.observer) state.observer.disconnect();
        dom.scroll.innerHTML = '';
        state.items = [];
        state.renderQueue = [];
        state.groups = computeGroups();
        var current = WS.currentIndex;
        state.groups.forEach(function (group) {
            var sec = el('div', 'pec-sec' + (group.appendix ? ' pec-sec-appendix' : ''));
            sec.dataset.key = group.key;
            if (group.name) {
                var head = el('button', 'pec-sec-h');
                head.type = 'button';
                head.tabIndex = -1;
                head.innerHTML = ICON.chevron;
                head.appendChild(el('span', 'pec-sec-name', group.name));
                head.appendChild(el('em', '', rangeLabel(group)));
                head.title = group.name;
                sec.appendChild(head);
            }
            var body = el('div', 'pec-sec-body');
            group.slides.forEach(function (i) {
                var slide = WS.slides[i];
                var th = el('div', 'pec-th');
                th.setAttribute('role', 'button');
                th.tabIndex = -1;
                th.dataset.index = String(i);
                var label = slideLabel(i);
                th.setAttribute('aria-label', 'Slide ' + label + ': ' + slideTitle(slide));
                th.appendChild(el('div', 'pec-n', label));
                var pic = el('div', 'pec-pic');
                pic.appendChild(el('div', 'pec-placeholder'));
                pic.appendChild(el('div', 'pec-dots'));
                th.appendChild(pic);
                body.appendChild(th);
                state.items[i] = { el: th, pic: pic, rendered: false, dirty: true, visible: false, group: group.key };
                renderDots(state.items[i], slide);
            });
            sec.appendChild(body);
            dom.scroll.appendChild(sec);
            applyFold(group.key);
        });
        observeItems();
        markCurrent(current, true);
    }

    function observeItems() {
        if (typeof IntersectionObserver !== 'function') {
            state.observer = null;
            state.items.forEach(function (item, i) { if (item) queueRender(i); });
            return;
        }
        state.observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                var i = parseInt(entry.target.dataset.index, 10);
                var item = state.items[i];
                if (!item) return;
                item.visible = entry.isIntersecting;
                if (item.visible && item.dirty) queueRender(i);
            });
        }, { root: dom.scroll, rootMargin: '240px 0px' });
        state.items.forEach(function (item) { if (item) state.observer.observe(item.el); });
    }

    function groupEl(key) {
        return dom.scroll ? dom.scroll.querySelector('.pec-sec[data-key="' + key + '"]') : null;
    }

    function applyFold(key) {
        var sec = groupEl(key);
        if (!sec) return;
        var folded = !!state.folded[key];
        sec.classList.toggle('folded', folded);
        var head = sec.querySelector('.pec-sec-h');
        if (head) head.setAttribute('aria-expanded', folded ? 'false' : 'true');
    }

    function setFolded(key, folded) {
        state.folded[key] = !!folded;
        applyFold(key);
    }

    function markCurrent(index, scroll) {
        state.items.forEach(function (item, i) {
            if (!item) return;
            var on = i === index;
            item.el.classList.toggle('on', on);
            item.el.tabIndex = on ? 0 : -1;
            if (on) item.el.setAttribute('aria-current', 'true');
            else item.el.removeAttribute('aria-current');
        });
        var item = state.items[index];
        if (!item) return;
        if (state.folded[item.group]) setFolded(item.group, false);
        if (scroll && state.railVisible && dom.rail && dom.rail.getClientRects().length && item.el.scrollIntoView) {
            var r = item.el.getBoundingClientRect();
            var box = dom.scroll.getBoundingClientRect();
            if (r.top < box.top || r.bottom > box.bottom) item.el.scrollIntoView({ block: 'center' });
        }
    }

    function onRailClick(e) {
        var head = e.target.closest('.pec-sec-h');
        if (head) {
            var key = head.parentElement.dataset.key;
            setFolded(key, !state.folded[key]);
            return;
        }
        var th = e.target.closest('.pec-th');
        if (!th) return;
        var index = parseInt(th.dataset.index, 10);
        if (!isFinite(index)) return;
        th.focus({ preventScroll: true });
        if (wsReady() && index === WS.currentIndex) return;
        navigate(index, 0);
    }

    function visibleItems() {
        var list = [];
        state.groups.forEach(function (group) {
            if (state.folded[group.key]) return;
            group.slides.forEach(function (i) { list.push(i); });
        });
        return list;
    }

    // Up/Down move between visible thumbnails and open that slide; Left folds
    // the section, Right unfolds it; Home/End jump to the ends.
    function onRailKeyDown(e) {
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        var th = e.target.closest && e.target.closest('.pec-th');
        var head = e.target.closest && e.target.closest('.pec-sec-h');
        var key = e.key;
        var handled = true;
        if (head && (key === 'Enter' || key === ' ')) {
            var hk = head.parentElement.dataset.key;
            setFolded(hk, !state.folded[hk]);
        } else if (key === 'ArrowDown' || key === 'ArrowUp' || key === 'Home' || key === 'End') {
            var list = visibleItems();
            if (!list.length) return;
            var at = th ? list.indexOf(parseInt(th.dataset.index, 10)) : list.indexOf(WS.currentIndex);
            var next;
            if (key === 'Home') next = list[0];
            else if (key === 'End') next = list[list.length - 1];
            else next = list[Math.max(0, Math.min(list.length - 1, at + (key === 'ArrowDown' ? 1 : -1)))];
            if (next !== undefined && state.items[next]) {
                state.items[next].el.focus({ preventScroll: false });
                if (next !== WS.currentIndex) navigate(next, 0);
            }
        } else if ((key === 'ArrowLeft' || key === 'ArrowRight') && (th || head)) {
            var gk = th ? state.items[parseInt(th.dataset.index, 10)].group : head.parentElement.dataset.key;
            var sec = groupEl(gk);
            var header = sec && sec.querySelector('.pec-sec-h');
            if (key === 'ArrowLeft' && header) {
                setFolded(gk, true);
                header.focus();
            } else if (key === 'ArrowRight') {
                setFolded(gk, false);
            }
        } else if (key === 'Enter' && th) {
            navigate(parseInt(th.dataset.index, 10), 0);
        } else {
            handled = false;
        }
        if (handled) {
            e.preventDefault();
            e.stopPropagation();
        }
    }

    /* ===== Navigation and the Back chip ===== */

    function navigate(index, step) {
        if (!wsReady() || index < 0 || index >= WS.slides.length) return false;
        step = Math.max(0, step || 0);
        if (inPresto()) {
            // Swift flushes the source pane and any live text session first.
            post({ type: 'sidebarNavigate', index: index, step: step });
        } else {
            var W = wysiwyg();
            if (W && W.commitActiveSession) W.commitActiveSession();
            WS.goToSlide(index, step);
        }
        return true;
    }

    function goToSlide(index, step, opts) {
        var i = typeof index === 'number' ? index : parseInt(index, 10);
        if (!wsReady() || !isFinite(i) || i < 0 || i >= WS.slides.length) return false;
        if (opts && opts.recordBack && i !== WS.currentIndex) {
            state.backStack.push({ index: WS.currentIndex, step: WS.currentStep || 0 });
            if (state.backStack.length > BACK_STACK_MAX) state.backStack.shift();
            state.moves = 0;
            state.pendingJump = i;
            updateBackChip();
        }
        return navigate(i, step);
    }

    function back() {
        var top = state.backStack.pop();
        if (!top) { updateBackChip(); return false; }
        state.moves = 0;
        state.pendingJump = top.index;
        updateBackChip();
        return navigate(top.index, top.step);
    }

    function clearBack() {
        state.backStack = [];
        state.pendingJump = null;
        state.moves = 0;
        updateBackChip();
    }

    // The chip stays until you go back or move on twice.
    function trackMoves(index) {
        if (index === state.lastIndex) return;
        state.lastIndex = index;
        if (!state.backStack.length) return;
        if (state.pendingJump !== null && index === state.pendingJump) {
            state.pendingJump = null;
            return;
        }
        state.pendingJump = null;
        var top = state.backStack[state.backStack.length - 1];
        if (index === top.index) {
            state.backStack.pop();
            state.moves = 0;
            updateBackChip();
            return;
        }
        state.moves += 1;
        if (state.moves >= 2) clearBack();
    }

    function buildBackChip() {
        var chip = el('button', 'presto-edit-chrome');
        chip.id = 'presto-back-chip';
        chip.type = 'button';
        chip.addEventListener('mousedown', function (e) { e.preventDefault(); });
        chip.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            back();
        });
        document.body.appendChild(chip);
        dom.back = chip;
    }

    function updateBackChip() {
        if (!dom.back) return;
        var top = state.backStack[state.backStack.length - 1];
        if (!top || !slideAt(top.index)) {
            dom.back.classList.remove('visible');
            return;
        }
        var text = 'Back to ' + slideLabel(top.index) + ' · ' + slideTitle(slideAt(top.index));
        dom.back.innerHTML = ICON.back;
        dom.back.appendChild(el('span', '', text));
        dom.back.title = text + (top.step ? ' (step ' + top.step + ')' : '');
        dom.back.setAttribute('aria-label', text);
        dom.back.classList.add('visible');
        positionBackChip();
    }

    function positionBackChip() {
        if (!dom.back || !dom.back.classList.contains('visible')) return;
        var g = geometry();
        if (!g) return;
        var h = dom.back.offsetHeight || 30;
        var top = g.slideTop >= h + 16 ? g.slideTop - h - 8 : 8;
        dom.back.style.top = Math.round(top) + 'px';
        dom.back.style.left = Math.round(g.slideLeft + g.slideW / 2) + 'px';
    }

    /* ===== Step strip ===== */

    function buildStrip() {
        var strip = el('div', 'presto-edit-chrome');
        strip.id = 'presto-step-strip';
        strip.setAttribute('role', 'toolbar');
        strip.setAttribute('aria-label', 'Reveal steps');
        strip.appendChild(el('span', 'pec-strip-label', 'Reveal'));
        var steps = el('div', 'pec-steps');
        strip.appendChild(steps);
        strip.appendChild(el('span', 'pec-keys', '←/→'));
        strip.appendChild(el('span', 'pec-sep'));
        var ghost = el('label', 'pec-ghost');
        ghost.appendChild(el('span', '', 'Show later steps faintly'));
        var toggle = el('button', 'pec-switch');
        toggle.type = 'button';
        toggle.id = 'presto-ghost-toggle';
        toggle.setAttribute('role', 'switch');
        toggle.setAttribute('aria-label', 'Show later steps faintly');
        toggle.title = 'Show later steps faintly (⇧⌘O)';
        ghost.appendChild(toggle);
        strip.appendChild(ghost);
        strip.addEventListener('mousedown', function (e) {
            // Keep keyboard focus (and a text session's caret) where it was.
            if (e.target.closest('button')) e.preventDefault();
        });
        steps.addEventListener('click', function (e) {
            var b = e.target.closest('.pec-step');
            if (!b || !wsReady()) return;
            var k = parseInt(b.dataset.pecStep, 10);
            if (isFinite(k) && k !== WS.currentStep) WS.goToSlide(WS.currentIndex, k);
        });
        ghost.addEventListener('click', function (e) {
            e.preventDefault();
            if (toggle.disabled) return;
            setGhost(!state.ghost, true);
        });
        document.body.appendChild(strip);
        dom.strip = strip;
        dom.steps = steps;
        dom.ghostToggle = toggle;
        updateGhostToggle();
    }

    function updateStrip(force) {
        if (!dom.strip || !wsReady()) return;
        var st = WS.getState ? WS.getState() : { index: WS.currentIndex, step: WS.currentStep, maxSteps: maxStepOf(slideAt(WS.currentIndex)) };
        var max = st.maxSteps || 0;
        if (force || max !== state.stripMax || st.index !== state.stripIndex) {
            state.stripMax = max;
            state.stripIndex = st.index;
            dom.steps.innerHTML = '';
            if (max === 0) {
                dom.steps.appendChild(el('span', 'pec-none', 'No reveal steps'));
            } else {
                for (var k = 0; k <= max; k++) {
                    if (k > 0) dom.steps.appendChild(el('i', 'pec-step-link'));
                    var b = el('button', 'pec-step', String(k));
                    b.type = 'button';
                    // Not data-step: the engine's reveal CSS would hide it.
                    b.dataset.pecStep = String(k);
                    b.title = k === 0 ? 'Before any reveal' : 'Step ' + k;
                    dom.steps.appendChild(b);
                }
            }
            dom.strip.classList.toggle('no-steps', max === 0);
            dom.strip.classList.toggle('dense', max >= DENSE_STEP_COUNT);
        }
        dom.steps.querySelectorAll('.pec-step').forEach(function (b) {
            var k = parseInt(b.dataset.pecStep, 10);
            b.classList.toggle('done', k < st.step);
            b.classList.toggle('on', k === st.step);
            b.setAttribute('aria-pressed', k === st.step ? 'true' : 'false');
        });
        dom.strip.querySelector('.pec-keys').style.display = max === 0 ? 'none' : '';
        dom.ghostToggle.disabled = max === 0;
    }

    function updateGhostToggle() {
        if (dom.ghostToggle) dom.ghostToggle.setAttribute('aria-checked', state.ghost ? 'true' : 'false');
    }

    // fromUser: the strip's own toggle (posts ghostFutureChanged); the shell's
    // menu and wysiwyg's own state changes arrive through setGhostFuture and
    // the presto:ghostfuture event and are not echoed back.
    function setGhost(on, fromUser) {
        on = !!on;
        var changed = on !== state.ghost;
        state.ghost = on;
        updateGhostToggle();
        var W = wysiwyg();
        if (W && typeof W.setGhostFuture === 'function') W.setGhostFuture(on, { silent: true });
        if (fromUser && changed) post({ type: 'ghostFutureChanged', on: on });
    }

    /* ===== Zoom label ===== */

    function buildZoom() {
        var zoom = el('div', 'presto-edit-chrome');
        zoom.id = 'presto-zoom-label';
        zoom.setAttribute('aria-hidden', 'true');
        document.body.appendChild(zoom);
        dom.zoom = zoom;
    }

    function updateZoom() {
        if (!dom.zoom) return;
        var g = geometry();
        var pct = g ? Math.round(g.scale * 100) : null;
        dom.zoom.innerHTML = '';
        dom.zoom.appendChild(el('b', '', 'Fit'));
        if (pct) dom.zoom.appendChild(el('span', '', pct + '%'));
        dom.zoom.title = pct ? 'Slide fitted to the window at ' + pct + '%' : '';
    }

    /* ===== Geometry ===== */

    function measuredGeometry() {
        var slide = wsReady() ? WS.slides[WS.currentIndex] : null;
        if (!slide) return null;
        var r = slide.getBoundingClientRect();
        if (!r.width) return null;
        return {
            scale: r.width / SLIDE_W,
            slideLeft: r.left,
            slideTop: r.top,
            slideW: r.width,
            slideH: r.height,
            viewportW: window.innerWidth,
            viewportH: window.innerHeight
        };
    }

    function geometry() {
        if (state.geometry) return state.geometry;
        return measuredGeometry();
    }

    function layout() {
        var g = geometry();
        if (dom.strip && g) {
            dom.strip.style.left = Math.round(g.slideLeft + g.slideW / 2) + 'px';
            var viewportH = g.viewportH || window.innerHeight;
            var bandTop = Math.max(g.slideTop + g.slideH + 8, viewportH - STRIP_BAND);
            var h = dom.strip.offsetHeight || 38;
            var top = Math.min(viewportH - h - 10, bandTop + (STRIP_BAND - h) / 2);
            dom.strip.style.top = Math.round(Math.max(bandTop, top)) + 'px';
            dom.strip.style.bottom = 'auto';
        }
        updateZoom();
        positionBackChip();
        positionCard();
    }

    function onLayout(e) {
        var d = e && e.detail;
        if (d && isFinite(d.slideLeft) && isFinite(d.slideW) && d.slideW > 0) state.geometry = d;
        layout();
    }

    function onWindowResize() {
        if (window.PrestoLayout) return;   // edit-layout.js fires presto:layout
        state.geometry = null;
        if (state.measureRaf !== null) return;
        state.measureRaf = (window.requestAnimationFrame || setTimeout)(function () {
            state.measureRaf = null;
            layout();
        });
    }

    function railWidth() {
        return state.railVisible && dom.rail ? RAIL_W : 0;
    }

    function setRailVisible(visible) {
        state.railVisible = visible !== false;
        document.body.classList.toggle('presto-rail-hidden', !state.railVisible);
        document.body.style.setProperty('--presto-rail-w', railWidth() + 'px');
        if (window.PrestoLayout && typeof PrestoLayout.apply === 'function') {
            var opts = Object.assign({}, state.layoutOpts, { insetLeft: railWidth() });
            state.layoutOpts = opts;
            try {
                var g = PrestoLayout.apply(opts);
                if (g && isFinite(g.slideW)) state.geometry = g;
            } catch (e) { /* layout failures must not break editing */ }
        } else {
            state.geometry = null;
        }
        layout();
        if (state.railVisible) state.items.forEach(function (item, i) { if (item && item.dirty && item.visible) queueRender(i); });
        return state.railVisible;
    }

    /* ===== Link card ===== */

    function deckElementById(id) {
        if (!id || !wsReady()) return null;
        var esc = window.CSS && CSS.escape ? CSS.escape(id) : id.replace(/["\\]/g, '\\$&');
        for (var i = 0; i < WS.slides.length; i++) {
            var slide = WS.slides[i];
            if (slide.id === id) return slide;
            var hit = slide.querySelector('[id="' + esc + '"]');
            if (hit) return hit;
        }
        return null;
    }

    function firstVisibleStep(target) {
        var slide = target.classList.contains('slide') ? target : target.closest('.slide');
        var step = 0;
        var node = target;
        while (node && node !== slide) {
            if (node.hasAttribute && node.hasAttribute('data-step')) {
                var r = revealRange(node, 'step');
                if (r) step = Math.max(step, r.start);
            }
            node = node.parentElement;
        }
        return Math.min(step, maxStepOf(slide));
    }

    // What a link points at: an internal slide (index + the step that shows
    // the target), the deck's Back button, an external URL, or nothing.
    function resolveLink(a) {
        if (!a || !a.getAttribute) return { kind: 'none' };
        var href = (a.getAttribute('href') || '').trim();
        if (a.classList.contains('ws-back-link') || href === '#') return { kind: 'back', href: href };
        if (href.charAt(0) === '#') {
            var raw = href.slice(1);
            var id = raw;
            try { id = decodeURIComponent(raw); } catch (e) { /* keep raw */ }
            var target = deckElementById(id) || (id !== raw ? deckElementById(raw) : null);
            if (!target) return { kind: 'missing', id: id, href: href };
            var index = slideIndexOf(target);
            if (index < 0) return { kind: 'missing', id: id, href: href };
            return { kind: 'internal', id: id, href: href, index: index, step: firstVisibleStep(target) };
        }
        if (!href) return { kind: 'none', href: href };
        return { kind: 'external', href: href };
    }

    // ⌘-click: go straight to the link's target (or back, for a Back button).
    function followLink(a) {
        var t = resolveLink(a);
        if (t.kind === 'internal') {
            var W = wysiwyg();
            if (W && W.deselect) W.deselect();
            return goToSlide(t.index, t.step, { recordBack: true });
        }
        if (t.kind === 'back') return back();
        return false;
    }

    function linkLabel(a) {
        return cleanText(a.textContent) || 'Untitled link';
    }

    function buildCard() {
        var card = el('div', 'presto-edit-chrome presto-wysiwyg-chrome');
        card.id = 'presto-link-card';
        card.setAttribute('role', 'dialog');
        card.setAttribute('aria-label', 'Link');
        card.addEventListener('mousedown', function (e) {
            if (!e.target.closest('input')) e.preventDefault();
            e.stopPropagation();
        });
        card.addEventListener('keydown', onCardKeyDown);
        document.body.appendChild(card);
        dom.card = card;
    }

    // Called on every selection refresh (drags included): re-render only when
    // the link or its target changed, otherwise just follow the link.
    function cardKeyFor(a) {
        return (a.getAttribute('href') || '') + '\u0000' + (a.textContent || '');
    }

    function showCard(a) {
        if (!dom.card) return;
        var same = state.link === a && dom.card.classList.contains('visible');
        if (!same) state.cardMode = 'card';
        state.link = a;
        if (!same || (cardKeyFor(a) !== state.cardKey && state.cardMode === 'card')) renderCard();
        dom.card.classList.add('visible');
        positionCard();
    }

    function hideCard() {
        if (!dom.card) return;
        var hadFocus = dom.card.contains(document.activeElement);
        state.link = null;
        state.linkTarget = null;
        state.cardKey = null;
        state.cardMode = 'card';
        dom.card.classList.remove('visible');
        dom.card.innerHTML = '';
        if (hadFocus && document.activeElement && document.activeElement.blur) document.activeElement.blur();
    }

    var cardRenderPending = false;
    function renderCardSoon() {
        if (cardRenderPending) return;
        cardRenderPending = true;
        setTimeout(function () {
            cardRenderPending = false;
            if (state.link && state.cardMode === 'card' && state.link.isConnected) {
                renderCard();
                positionCard();
            }
        }, 0);
    }

    function button(label, cls, onClick, html) {
        var b = el('button', 'pec-btn' + (cls ? ' ' + cls : ''));
        b.type = 'button';
        if (html) b.innerHTML = html;
        b.appendChild(document.createTextNode(label));
        b.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            onClick();
        });
        return b;
    }

    function renderCard() {
        var a = state.link;
        var card = dom.card;
        if (!a || !card) return;
        card.innerHTML = '';
        state.cardKey = cardKeyFor(a);
        var target = resolveLink(a);
        state.linkTarget = target;

        var head = el('div', 'pec-lc-h');
        head.innerHTML = ICON.link;
        head.appendChild(el('span', '', 'Link'));
        head.appendChild(el('b', '', linkLabel(a)));
        card.appendChild(head);

        if (state.cardMode === 'pick') {
            renderPicker(card, target);
            return;
        }

        var row = el('div', 'pec-lc-t');
        var title = el('b');
        var sub = el('small');
        if (target.kind === 'internal') {
            var slide = slideAt(target.index);
            var pic = el('div', 'pec-pic');
            try { pic.appendChild(makeThumbCanvas(slide, CARD_THUMB_W)); } catch (e) { /* blank card */ }
            row.appendChild(pic);
            title.textContent = slideLabel(target.index) + ' · ' + slideTitle(slide);
            sub.textContent = (isAppendix(slide) ? 'Appendix slide ' + (target.index + 1) : 'Slide ' + slideLabel(target.index)) +
                (target.step > 0 ? ' · step ' + target.step : '');
        } else if (target.kind === 'back') {
            title.textContent = 'Back button';
            sub.textContent = 'Returns to the slide you came from';
        } else if (target.kind === 'external') {
            title.textContent = 'Web address';
            sub.textContent = target.href;
        } else if (target.kind === 'missing') {
            row.classList.add('missing');
            title.textContent = 'No slide has this target';
            sub.textContent = '#' + target.id;
        } else {
            row.classList.add('missing');
            title.textContent = 'No target';
            sub.textContent = 'Choose a slide for this link';
        }
        var meta = el('div');
        meta.appendChild(title);
        meta.appendChild(sub);
        row.appendChild(meta);
        card.appendChild(row);

        var actions = el('div', 'pec-lc-a');
        if (target.kind === 'internal' || target.kind === 'back') {
            var go = button(target.kind === 'back' ? 'Go back' : 'Go to slide', 'primary pec-go', function () {
                var link = state.link;
                if (link) followLink(link);
            }, ICON.go);
            go.disabled = target.kind === 'back' && !state.backStack.length;
            actions.appendChild(go);
        }
        actions.appendChild(button('Edit label', 'pec-edit-label', function () {
            var W = wysiwyg();
            var link = state.link;
            if (W && link && W.editLinkLabel) W.editLinkLabel(link);
        }));
        if (target.kind !== 'back') {
            actions.appendChild(button('Change target…', 'pec-change-target', function () { openPicker(); }));
        }
        card.appendChild(actions);
        if (target.kind === 'internal') card.appendChild(el('div', 'pec-lc-f', '⌘-click a link to go straight there'));
    }

    function openPicker() {
        if (!state.link) return;
        state.cardMode = 'pick';
        renderCard();
        positionCard();
        var input = dom.card.querySelector('.pec-pick-search');
        if (input) input.focus();
    }

    function closePicker() {
        if (state.cardMode !== 'pick') return false;
        state.cardMode = 'card';
        if (state.link && state.link.isConnected) {
            renderCard();
            positionCard();
        } else {
            hideCard();
        }
        return true;
    }

    function pickCandidates() {
        var appendix = [];
        var main = [];
        WS.slides.forEach(function (slide, i) {
            var row = { index: i, label: slideLabel(i), title: slideTitle(slide) };
            (isAppendix(slide) ? appendix : main).push(row);
        });
        return { appendix: appendix, main: main };
    }

    function renderPicker(card, target) {
        var input = el('input', 'pec-pick-search');
        input.type = 'search';
        input.placeholder = 'Find a slide';
        input.setAttribute('aria-label', 'Find a slide');
        input.autocomplete = 'off';
        input.spellcheck = false;
        card.appendChild(input);
        var list = el('div', 'pec-pick-list');
        list.setAttribute('role', 'listbox');
        card.appendChild(list);
        var foot = el('div', 'pec-pick-foot');
        foot.appendChild(button('Cancel', '', closePicker));
        card.appendChild(foot);
        var current = target && target.kind === 'internal' ? target.index : -1;
        function fill() {
            var q = cleanText(input.value).toLowerCase();
            var all = pickCandidates();
            list.innerHTML = '';
            state.pickRows = [];
            function match(row) {
                if (!q) return true;
                var hay = (row.label + ' ' + row.title).toLowerCase();
                return q.split(' ').every(function (tok) { return hay.indexOf(tok) >= 0; });
            }
            [['Appendix', all.appendix], ['Slides', all.main]].forEach(function (pair) {
                var rows = pair[1].filter(match);
                if (!rows.length) return;
                list.appendChild(el('div', 'pec-pick-group', pair[0]));
                rows.forEach(function (row) {
                    var b = el('button', 'pec-pick-row');
                    b.type = 'button';
                    b.setAttribute('role', 'option');
                    b.dataset.index = String(row.index);
                    b.appendChild(el('span', 'pec-pick-n', row.label));
                    b.appendChild(el('span', 'pec-pick-title', row.title));
                    var check = el('span', 'pec-pick-check');
                    if (row.index === current) check.innerHTML = ICON.check;
                    b.appendChild(check);
                    b.addEventListener('mousedown', function (e) { e.preventDefault(); });
                    b.addEventListener('click', function (e) {
                        e.preventDefault();
                        e.stopPropagation();
                        choose(row.index);
                    });
                    list.appendChild(b);
                    state.pickRows.push(b);
                });
            });
            if (!state.pickRows.length) list.appendChild(el('div', 'pec-pick-empty', 'No slide matches'));
            setPickActive(0);
        }
        input.addEventListener('input', fill);
        fill();
    }

    function setPickActive(i) {
        state.pickActive = Math.max(0, Math.min(state.pickRows.length - 1, i));
        state.pickRows.forEach(function (b, k) {
            b.classList.toggle('active', k === state.pickActive);
            b.setAttribute('aria-selected', k === state.pickActive ? 'true' : 'false');
        });
        var active = state.pickRows[state.pickActive];
        if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
    }

    function choose(index) {
        var W = wysiwyg();
        var link = state.link;
        if (!W || !link || typeof W.setLinkTarget !== 'function') return false;
        var ok = W.setLinkTarget(link, index);
        state.cardMode = 'card';
        if (state.link && state.link.isConnected) {
            renderCard();
            positionCard();
        }
        return ok;
    }

    function onCardKeyDown(e) {
        // The card owns its keys: they must not reach the editor or the engine.
        e.stopPropagation();
        var inPick = state.cardMode === 'pick';
        if (e.key === 'Escape') {
            e.preventDefault();
            if (!closePicker()) {
                var W = wysiwyg();
                if (W && W.deselect) W.deselect();
            }
            return;
        }
        if (!inPick) return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            setPickActive(state.pickActive + (e.key === 'ArrowDown' ? 1 : -1));
        } else if (e.key === 'Enter') {
            e.preventDefault();
            var row = state.pickRows[state.pickActive];
            if (row) choose(parseInt(row.dataset.index, 10));
        }
    }

    // Anchored to the link: above it when there is room, else below, never
    // over the link itself, and never under the rail.
    function positionCard() {
        var card = dom.card;
        var a = state.link;
        if (!card || !a || !card.classList.contains('visible')) return;
        if (!a.isConnected) { hideCard(); return; }
        var r = a.getBoundingClientRect();
        var w = card.offsetWidth || 352;
        var h = card.offsetHeight || 160;
        var minLeft = railWidth() + 8;
        var maxLeft = window.innerWidth - w - 8;
        var left = Math.max(minLeft, Math.min(maxLeft, r.left + r.width / 2 - w / 2));
        var top;
        if (r.top - h - 12 >= 8) top = r.top - h - 12;
        else if (r.bottom + 12 + h <= window.innerHeight - 8) top = r.bottom + 12;
        else {
            top = Math.max(8, Math.min(window.innerHeight - h - 8, r.top + r.height / 2 - h / 2));
            left = r.right + 12 + w <= window.innerWidth - 8 ? r.right + 12 : Math.max(minLeft, r.left - w - 12);
        }
        card.style.left = Math.round(left) + 'px';
        card.style.top = Math.round(top) + 'px';
    }

    function onSelection(e) {
        var d = (e && e.detail) || {};
        if (d.kind === 'link' && d.element && !d.editing) {
            showCard(d.element);
        } else if (state.link) {
            hideCard();
        }
    }

    function closePopover() {
        return closePicker();
    }

    /* ===== Engine events ===== */

    function onNavigate(st) {
        var index = st && typeof st.index === 'number' ? st.index : (wsReady() ? WS.currentIndex : -1);
        trackMoves(index);
        markCurrent(index, true);
        updateStrip(false);
        if (!window.PrestoLayout) state.geometry = null;
        layout();
        if (state.link && (!state.link.isConnected || slideIndexOf(state.link) !== index)) hideCard();
        else if (state.link) renderCardSoon();
    }

    function onGhostEvent(e) {
        var on = !!(e && e.detail && e.detail.on);
        if (on === state.ghost) return;
        state.ghost = on;
        updateGhostToggle();
    }

    /* ===== Install ===== */

    // The rail, the strip and their placement, after slides or sections changed.
    function rebuildAll() {
        rebuild();
        updateStrip(true);
        layout();
    }

    function install(opts) {
        opts = opts || {};
        if (typeof opts.ghostFuture === 'boolean') state.ghost = opts.ghostFuture;
        else {
            var W0 = wysiwyg();
            if (W0 && W0.state) state.ghost = W0.state().hiddenStepOpacity > 0;
        }
        if (state.installed) {
            if (typeof opts.railVisible === 'boolean') setRailVisible(opts.railVisible);
            setGhost(state.ghost, false);
            return true;
        }
        if (!wsReady()) {
            (window.requestAnimationFrame || setTimeout)(function () { install(opts); });
            return false;
        }
        state.installed = true;
        document.body.classList.add('presto-edit-chrome-on');
        buildRail();
        buildStrip();
        buildZoom();
        buildBackChip();
        buildCard();
        state.lastIndex = WS.currentIndex;
        if (window.PrestoLayout && typeof PrestoLayout.current === 'function') {
            try {
                var g = PrestoLayout.current();
                if (g && isFinite(g.slideW) && g.slideW > 0) state.geometry = g;
            } catch (e) { /* measure instead */ }
        }
        state.railVisible = opts.railVisible !== false;
        document.body.classList.toggle('presto-rail-hidden', !state.railVisible);
        document.body.style.setProperty('--presto-rail-w', railWidth() + 'px');
        rebuild();
        updateStrip(true);
        setGhost(state.ghost, false);
        layout();
        watchDeck();
        if (typeof WS.on === 'function') WS.on('navigate', onNavigate);
        if (WS.ready && typeof WS.ready.then === 'function') {
            // Sections, tables and inlined SVGs settle at WS.ready.
            WS.ready.then(rebuildAll);
        }
        document.addEventListener('presto:layout', onLayout);
        document.addEventListener('presto:selection', onSelection);
        document.addEventListener('presto:ghostfuture', onGhostEvent);
        document.addEventListener('presto:steps', function () { updateStrip(true); refreshThumbnail(WS.currentIndex); });
        window.addEventListener('resize', onWindowResize);
        post({ type: 'editChromeReady' });
        return true;
    }

    // Compatibility for slide patchers that still call the engine's old rail
    // hooks (Presto.swift's replacePrestoEditSlideHTML before integration).
    if (typeof window.updatePrestoEditSidebarThumbnail !== 'function') {
        window.updatePrestoEditSidebarThumbnail = function (index) { return refreshThumbnail(index); };
    }

    window.PrestoEditChrome = {
        version: 1,
        install: install,
        rebuild: rebuildAll,
        refreshThumbnail: refreshThumbnail,
        refreshAllThumbnails: refreshAllThumbnails,
        setRailVisible: setRailVisible,
        goToSlide: goToSlide,
        back: back,
        setGhostFuture: function (on) { setGhost(on, false); return state.ghost; },
        resolveLink: resolveLink,
        followLink: followLink,
        closePopover: closePopover,
        slideLabel: slideLabel,
        slideTitle: function (index) { return slideTitle(slideAt(index)); },

        // Introspection for tests.
        state: function () {
            return {
                installed: state.installed,
                railVisible: state.railVisible,
                ghostFuture: state.ghost,
                groups: state.groups.map(function (g) {
                    return { key: g.key, name: g.name, slides: g.slides.slice(), folded: !!state.folded[g.key] };
                }),
                backStack: state.backStack.map(function (b) { return { index: b.index, step: b.step }; }),
                backVisible: !!(dom.back && dom.back.classList.contains('visible')),
                cardVisible: !!(dom.card && dom.card.classList.contains('visible')),
                cardMode: state.cardMode,
                geometry: geometry()
            };
        },
        setFolded: setFolded,
        flushThumbnails: function () {
            if (state.mutationTimer) { clearTimeout(state.mutationTimer); flushMutations(); }
            while (state.renderQueue.length) drainRenderQueue();
        },
        set onMessage(fn) { testMessageSink = fn; },
        get onMessage() { return testMessageSink; }
    };
})();

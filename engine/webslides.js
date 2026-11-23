/* ============================================
   WEBSLIDES — Core Engine
   ============================================
   Navigation, progressive reveal, appendix,
   slide numbering, overview mode, scaling.
   Zero dependencies.
   ============================================ */

(function () {
    'use strict';

    const WS = {
        slides: [],
        mainSlides: [],
        appendixSlides: [],
        currentIndex: 0,
        currentStep: 0,
        returnStack: [],
        totalMainSlides: 0,
        overviewActive: false,
        slideSteps: new Map(), // slide element -> max step count
        requestedInitialSlide: null,
        requestedInitialStep: null,
        loadErrors: [], // { type: 'table' | 'svg', src, detail, slideIndex }
    };

    // Expose for external tools (PDF export, dev server, etc.)
    window.WS = WS;

    // Where this script was loaded from: the vendored KaTeX sits beside it.
    const ENGINE_SCRIPT_SRC = (document.currentScript && document.currentScript.src) || null;

    /* ===== Readiness ===== */

    // Resolves (to WS, never rejects) once SVGs are inlined, tables are loaded,
    // math-rendered, fitted and overlaid, fonts have settled and step counts are
    // final. Also sets <html data-ws-ready="1"> for pollers.
    let resolveReady;
    WS.ready = new Promise(function (resolve) { resolveReady = resolve; });

    function markReady() {
        document.documentElement.dataset.wsReady = '1';
        resolveReady(WS);
    }

    // Upper bound for each wait on outside resources (KaTeX, fonts, images).
    const READY_WAIT_MS = 5000;

    function boundedWait(promise, ms) {
        return Promise.race([
            Promise.resolve(promise).catch(function () {}),
            new Promise(function (resolve) { setTimeout(resolve, ms); })
        ]);
    }

    /* ===== Load Errors ===== */

    const loadErrorTargets = new Map(); // entry -> element, until slide indexes exist

    function recordLoadError(type, src, detail, el) {
        const entry = { type: type, src: src, detail: detail, slideIndex: null };
        WS.loadErrors.push(entry);
        loadErrorTargets.set(entry, el);
        resolveLoadErrorSlides();
    }

    function resolveLoadErrorSlides() {
        if (WS.slides.length === 0) return;
        loadErrorTargets.forEach(function (el, entry) {
            entry.slideIndex = slideIndexFromElement(el);
        });
        loadErrorTargets.clear();
    }

    /* ===== Reveal Ranges ===== */

    // Public form of the engine's own reveal-range parser: "3" and "3+" are
    // open-ended (end: Infinity), "2-4" is bounded, empty means step 1, and
    // anything malformed returns null.
    WS.parseRevealRange = function (raw) {
        let range;
        try {
            range = parseStepValue(raw);
        } catch (err) {
            return null;
        }
        return range ? { start: range.start, end: range.end === undefined ? Infinity : range.end } : null;
    };

    /* ===== Event System ===== */

    WS._listeners = Object.create(null);
    WS.on = function (event, cb) {
        if (typeof event !== 'string' || typeof cb !== 'function') return;
        if (!Object.prototype.hasOwnProperty.call(WS._listeners, event)) {
            WS._listeners[event] = [];
        }
        WS._listeners[event].push(cb);
    };
    WS._emit = function (event, data) {
        (WS._listeners[event] || []).forEach(function (fn) {
            try {
                fn(data);
            } catch (err) {
                console.warn('WebSlides listener failed for ' + event, err);
            }
        });
    };

    /* ===== Speaker Notes ===== */

    WS.getNotes = function (slideIndex) {
        const index = parseStrictInt(slideIndex);
        if (!Number.isInteger(index) || index < 0 || index >= WS.slides.length) return '';
        var aside = WS.slides[index].querySelector('aside.notes');
        return aside ? aside.innerHTML : '';
    };

    /* ===== State API ===== */

    WS.getState = function () {
        var slide = WS.slides[WS.currentIndex];
        return {
            index: WS.currentIndex,
            step: WS.currentStep,
            maxSteps: safeMaxStepForSlide(slide),
            totalSlides: WS.slides.length,
            mainSlides: WS.totalMainSlides,
            isAppendix: slide ? slide.classList.contains('appendix') : false,
            slideNumber: slide ? (slide.dataset.wsMainNumber || null) : null,
            returnStackDepth: WS.returnStack.length
        };
    };

    /* ===== Export Helpers ===== */

    WS.getExportInfo = function () {
        return WS.slides.map(function (slide, i) {
            return { index: i, maxStep: safeMaxStepForSlide(slide) };
        });
    };

    WS.getExportLinks = function () {
        var slide = WS.slides[WS.currentIndex];
        if (!slide) return [];

        function isVisible(el) {
            var node = el;
            while (node && node !== document.body) {
                var style = getComputedStyle(node);
                if (style.opacity === '0' || style.display === 'none' || style.visibility === 'hidden') return false;
                node = node.parentElement;
            }
            return true;
        }

        var links = [];

        // Appendix links on the active slide
        slide.querySelectorAll('a.ws-appendix-link').forEach(function (a) {
            if (!isVisible(a)) return;
            var href = a.getAttribute('href');
            if (!href || !href.startsWith('#')) return;
            var target = deckElementById(href.slice(1));
            var targetIndex = slideIndexFromElement(target);
            if (targetIndex === null) return;
            var rect = a.getBoundingClientRect();
            links.push({
                targetSlideIndex: targetIndex,
                targetStep: firstVisibleStepForTarget(target),
                x: rect.x, y: rect.y, w: rect.width, h: rect.height
            });
        });

        // Back links on appendix slides — find the main slide that references this appendix
        if (slide.classList.contains('appendix')) {
            slide.querySelectorAll('a.ws-back-link').forEach(function (a) {
                if (!isVisible(a)) return;
                var thisIndex = WS.currentIndex;
                var sourceIndex = -1;
                WS.slides.forEach(function (sourceCandidate) {
                    sourceCandidate.querySelectorAll('a.ws-appendix-link').forEach(function (link) {
                        var href = link.getAttribute('href');
                        if (!href || !href.startsWith('#')) return;
                        var target = deckElementById(href.slice(1));
                        if (slideIndexFromElement(target) === thisIndex) {
                            var sourceSlide = link.closest('.slide');
                            var parsedSourceIndex = slideIndexFromElement(sourceSlide);
                            if (parsedSourceIndex === null) return;
                            sourceIndex = parsedSourceIndex;
                        }
                    });
                });
                if (sourceIndex >= 0) {
                    var sourceSlide = WS.slides[sourceIndex];
                    var rect = a.getBoundingClientRect();
                    links.push({
                        targetSlideIndex: sourceIndex,
                        targetStep: safeMaxStepForSlide(sourceSlide),
                        x: rect.x, y: rect.y, w: rect.width, h: rect.height
                    });
                }
            });
        }

        // Section nav items
        var nav = document.getElementById('ws-section-nav');
        if (nav && nav.style.opacity !== '0') {
            nav.querySelectorAll('.ws-section-nav-item').forEach(function (item) {
                var idx = parseStrictInt(item.dataset.sectionIndex);
                if (idx !== null && WS.uniqueSections && WS.uniqueSections[idx]) {
                    var rect = item.getBoundingClientRect();
                    links.push({
                        targetSlideIndex: WS.uniqueSections[idx].slideIndex,
                        targetStep: 0,
                        x: rect.x, y: rect.y, w: rect.width, h: rect.height
                    });
                }
            });
        }

        return links;
    };

    WS.prepareForExport = function () {
        var style = document.createElement('style');
        style.textContent = '*, *::before, *::after { transition: none !important; animation: none !important; }';
        document.head.appendChild(style);
        disableMorphs(); // Web Animations ignore the CSS above
        var progress = document.getElementById('ws-progress');
        if (progress) progress.style.display = 'none';
    };

    /* ===== Initialization ===== */

    function discoverDeckSlides() {
        var decks = Array.from(document.querySelectorAll('.ws-deck'));
        if (decks.some(function (deck) {
            return deck.parentElement && deck.parentElement.closest('.ws-deck');
        })) {
            throw new Error('WebSlides deck structure error: .ws-deck containers must not be nested');
        }
        if (decks.length !== 1) {
            throw new Error(
                'WebSlides deck structure error: expected exactly one .ws-deck container, found ' +
                decks.length
            );
        }
        var deck = decks[0];
        var slides = Array.from(deck.children).filter(function (child) {
            return child.tagName === 'SECTION' && child.classList && child.classList.contains('slide');
        });
        if (slides.length === 0) {
            throw new Error('WebSlides deck structure error: .ws-deck contains no direct .slide sections');
        }
        return slides;
    }

    function init() {
        // Parse URL parameters for mode and initial state
        var params = new URLSearchParams(window.location.search);
        WS.mode = parseMode(params.get('mode'));

        if (WS.mode === 'preview') {
            document.body.classList.add('ws-mode-preview');
        } else if (WS.mode === 'audience') {
            document.body.classList.add('ws-mode-audience');
        }

        // Discover slides
        WS.slides = discoverDeckSlides();
        WS.slides.forEach((slide, i) => {
            slide.dataset.wsIndex = i;
            if (slide.classList.contains('appendix')) {
                WS.appendixSlides.push(slide);
            } else {
                WS.mainSlides.push(slide);
                slide.dataset.wsMainNumber = WS.mainSlides.length;
            }
            // Expand data-reveal="each" lists, then parse max step for this slide
            applyRevealShortcuts(slide);
            WS.slideSteps.set(slide, parseMaxStep(slide));
        });
        WS.totalMainSlides = WS.mainSlides.length;
        resolveLoadErrorSlides();

        // Build section/subsection structure (used by both nav bar + auto-injected TOCs)
        buildSectionStructure();

        // Auto-inject section TOCs into section-header / subsection-header slides
        injectSectionTocs();

        // Create progress bar
        createProgressBar();

        // Create section navigation bar (if enabled)
        buildSectionNav();

        // Create overview container
        createOverview();

        // Bind events (mode-dependent)
        if (WS.mode === 'normal') {
            bindKeyboard();
            bindSlideClicks();
        }
        bindOverviewClicks();
        bindHashChange();

        // Scale slides to viewport
        scaleSlides();
        window.addEventListener('resize', scaleSlides);

        // Expose navigation functions for external tools (Presto, PDF export, etc.)
        WS.goToSlide = goToSlide;
        WS.nextStep = nextStep;
        WS.prevStep = prevStep;
        WS.nextSlide = nextSlide;
        WS.prevSlide = prevSlide;
        WS.goToAppendix = goToAppendix;
        WS.returnFromAppendix = returnFromAppendix;
        WS.goBack = returnFromAppendix;
        WS.toggleOverview = toggleOverview;
        WS.refitTables = async function() { await fitAllTables(); createColumnOverlays(); };

        // Navigate to URL params, hash, or first slide
        var paramSlide = params.get('slide');
        var paramStep = params.get('step');
        if (paramSlide !== null) {
            var slideNumber = parseStrictInt(paramSlide);
            var slideIdx = slideNumber !== null ? slideNumber - 1 : NaN; // 1-indexed for users
            var parsedStep = paramStep !== null ? parseStrictInt(paramStep) : 0;
            var stepVal = parsedStep !== null ? parsedStep : 0;
            if (slideNumber !== null && slideIdx >= 0) {
                WS.requestedInitialSlide = slideIdx;
                WS.requestedInitialStep = stepVal;
            }
            if (!goToSlide(slideIdx, stepVal) && !navigateToHash()) {
                goToSlide(0, 0);
            }
        } else if (!navigateToHash()) {
            goToSlide(0, 0);
        }
    }

    /* ===== Step Parsing ===== */

    function parseMode(raw) {
        return raw === 'preview' || raw === 'audience' ? raw : 'normal';
    }

    function parseStrictInt(raw) {
        if (raw === undefined || raw === null || raw === '') return null;
        try {
            raw = String(raw).trim();
        } catch (err) {
            return null;
        }
        if (!/^\d+$/.test(raw)) return null;
        const n = Number(raw);
        return Number.isSafeInteger(n) ? n : null;
    }

    function slideIndexFromValue(raw) {
        const idx = parseStrictInt(raw);
        return idx !== null && idx >= 0 && idx < WS.slides.length ? idx : null;
    }

    function slideIndexFromElement(el) {
        if (!el) return null;
        const slide = el.classList && el.classList.contains('slide') ? el : el.closest('.slide');
        return slide && slide.dataset ? slideIndexFromValue(slide.dataset.wsIndex) : null;
    }

    function safeMaxStepForSlide(slide) {
        if (!slide) return 0;
        const maxStep = WS.slideSteps.get(slide);
        return Number.isSafeInteger(maxStep) && maxStep >= 0 ? maxStep : 0;
    }

    function firstVisibleStepForTarget(target) {
        if (!target || !target.closest) return 0;
        const slide = target.classList && target.classList.contains('slide')
            ? target
            : target.closest('.slide');
        if (!slide) return 0;
        ensureStepModel(slide);

        let step = 0;
        let node = target;
        while (node && node !== slide.parentElement) {
            if (node.dataset && node.dataset.step !== undefined) {
                const parsed = getCachedStep(node, 'step');
                if (parsed) step = Math.max(step, parsed.start);
            }
            if (node === slide) break;
            node = node.parentElement;
        }
        return Math.min(step, safeMaxStepForSlide(slide));
    }

    function deckElementById(id) {
        var rawId;
        try {
            rawId = String(id);
        } catch (err) {
            return null;
        }
        var decodedId = decodeFragment(rawId);
        var candidates = decodedId === rawId ? [rawId] : [decodedId, rawId];
        for (const candidate of candidates) {
            if (!candidate) continue;
            for (const slide of WS.slides) {
                if (slide.id === candidate) return slide;
                const matches = slide.querySelectorAll('[id]');
                for (const el of matches) {
                    if (el.id === candidate) return el;
                }
            }
        }
        return null;
    }

    function parseStepValue(val) {
        if (val === undefined || val === null) return { start: 1, end: undefined };
        val = String(val).trim();
        if (val === '') return { start: 1, end: undefined };

        function parseStepInt(raw) {
            return parseStrictInt(raw);
        }

        // "3+" -> appears at 3, stays forever
        if (val.endsWith('+')) {
            const start = parseStepInt(val.slice(0, -1));
            return start === null ? null : { start: start, end: undefined };
        }
        // "2-4" -> visible during steps 2 to 4
        if (val.includes('-')) {
            const parts = val.split('-');
            if (parts.length !== 2) return null;
            const start = parseStepInt(parts[0]);
            const end = parseStepInt(parts[1]);
            if (start === null || end === null || end < start) return null;
            return { start: start, end: end };
        }
        // "3" -> appears at step 3, stays forever (default behavior)
        const n = parseStepInt(val);
        return n === null ? null : { start: n, end: undefined };
    }

    const REVEAL_SELECTOR = '[data-step], [data-highlight], [data-dim], [data-class]';

    function maxStepOfElement(el) {
        let max = 0;
        ['step', 'highlight', 'dim', 'class'].forEach(attr => {
            const val = el.dataset[attr];
            if (val === undefined) return;
            // data-class is "classname:range" — extract the range portion
            let rangeStr = val;
            if (attr === 'class') {
                const sep = val.lastIndexOf(':');
                if (sep <= 0) return;
                rangeStr = val.slice(sep + 1);
            }
            // Relative steps ("+", "=") only have a value once the slide is resolved
            const parsed = attr === 'step' && isRelativeStep(val) ? (el._ws_step || null) : parseStepValue(rangeStr);
            if (!parsed) return;
            max = Math.max(max, parsed.start, parsed.end || 0);
        });
        return max;
    }

    function parseMaxStep(slide) {
        resolveSlideSteps(slide);
        let max = 0;
        slide.querySelectorAll(REVEAL_SELECTOR).forEach(el => {
            max = Math.max(max, maxStepOfElement(el));
        });
        engineCountedSlides.add(slide);
        return Math.max(max, stepModelMaxStep(slide));
    }

    /* ===== Reveal Shortcut ===== */

    // <ul data-reveal="each"> / <ol data-reveal="each">: every <li> (nested
    // sub-bullets included, in document order) gets its own step, starting at
    // data-reveal-start or one past the highest step used earlier on the slide.
    // Items with an explicit data-step keep it and do not use up a step.
    // Generated steps are marked data-ws-auto-step so editors can strip them.
    function applyRevealShortcuts(slide) {
        slide.querySelectorAll('ul[data-reveal="each"], ol[data-reveal="each"]').forEach(list => {
            const rawStart = list.getAttribute('data-reveal-start');
            let step = parseStrictInt(rawStart);
            if (step === null) {
                if (rawStart !== null) console.warn('Ignoring invalid data-reveal-start:', rawStart);
                resolveSlideSteps(slide); // relative steps before the list count too
                step = highestStepBefore(slide, list) + 1;
            }
            list.querySelectorAll('li').forEach(li => {
                if (li.hasAttribute('data-step')) return;
                li.setAttribute('data-step', String(step++));
                li.setAttribute('data-ws-auto-step', '');
            });
        });
    }

    // Highest reveal step on elements that precede `node` on its slide
    // (ancestors excluded).
    function highestStepBefore(slide, node) {
        let max = 0;
        slide.querySelectorAll(REVEAL_SELECTOR).forEach(el => {
            if (el.contains(node)) return;
            if (!(node.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING)) return;
            max = Math.max(max, maxStepOfElement(el));
        });
        return max;
    }

    WS.applyRevealShortcuts = applyRevealShortcuts;

    /* ===== Reveal System ===== */

    function isActive(range, step) {
        if (!range) return false;
        return step >= range.start && (range.end === undefined || step <= range.end);
    }

    // Cache parsed step values on elements to avoid re-parsing on every applyStep
    function getCachedStep(el, attr) {
        var key = '_ws_' + attr;
        if (el[key] === undefined) {
            if (attr === 'class') {
                var raw = el.dataset[attr];
                var sep = raw.lastIndexOf(':');
                if (sep <= 0) {
                    el[key] = null;
                } else {
                    var cls = raw.slice(0, sep).trim();
                    if (!isValidClassToken(cls)) {
                        el[key] = null;
                    } else {
                        var range = parseStepValue(raw.slice(sep + 1));
                        if (range) {
                            el[key] = { cls: cls, range: range };
                        } else {
                            el[key] = null;
                        }
                    }
                }
            } else {
                el[key] = parseStepValue(el.dataset[attr]);
            }
        }
        return el[key];
    }

    function isValidClassToken(cls) {
        return typeof cls === 'string' && cls !== '' && !/[\t\n\f\r ]/.test(cls);
    }

    function applyStep(slide, step) {
        ensureStepModel(slide);
        cancelMorphs(slide); // any step change not animated by nextStep/prevStep lands at once
        slide.querySelectorAll('[data-step]').forEach(el => {
            el.classList.toggle('ws-visible', isActive(getCachedStep(el, 'step'), step));
        });
        slide.querySelectorAll('[data-highlight]').forEach(el => {
            el.classList.toggle('ws-highlighted', isActive(getCachedStep(el, 'highlight'), step));
        });
        slide.querySelectorAll('[data-dim]').forEach(el => {
            el.classList.toggle('ws-dimmed', isActive(getCachedStep(el, 'dim'), step));
        });
        slide.querySelectorAll('[data-class]').forEach(el => {
            var cached = getCachedStep(el, 'class');
            if (cached) {
                el.classList.toggle(cached.cls, isActive(cached.range, step));
            }
        });
        applyFadeAndFocus(slide, step);
    }

    function applyAllRevealState(container) {
        container.querySelectorAll('[data-step]').forEach(function (el) {
            el.classList.add('ws-visible');
            el.style.opacity = '1';
        });
        container.querySelectorAll('[data-highlight]').forEach(function (el) {
            el.classList.add('ws-highlighted');
        });
        container.querySelectorAll('[data-dim]').forEach(function (el) {
            el.classList.remove('ws-dimmed');
        });
        container.querySelectorAll('[data-class]').forEach(function (el) {
            var raw = el.dataset.class || '';
            var sep = raw.lastIndexOf(':');
            if (sep <= 0) return;
            var cls = raw.slice(0, sep).trim();
            if (!isValidClassToken(cls) || !parseStepValue(raw.slice(sep + 1))) return;
            el.classList.add(cls);
        });
        container.querySelectorAll('.ws-faded').forEach(function (el) {
            el.classList.remove('ws-faded');
        });
    }

    /* ===== Step Model: relative steps, fades, focus, morphs ===== */
    // data-step="+" / "=" (optionally ":N" steps long), data-fade-after,
    // data-focus and data-morph. Nothing here rewrites authored attributes or
    // adds markup: resolved ranges live in the element's reveal cache
    // (el._ws_step) and in stepModels, fades are the ws-faded class (stripped
    // by Presto on save, like ws-visible), and morphs are Web Animations, which
    // never touch the DOM. Elements inside inlined SVGs take part in document
    // order like any other slide element.

    const RELATIVE_STEP_RE = /^([+=])(?::(\d+))?$/;
    // "R:SELECTOR"; R is a reveal range or a relative step ("+", "=:2")
    const FOCUS_PART_RE = /^\s*([+=](?::\d+)?|\d+(?:-\d+|\+)?)\s*:\s*([\s\S]*\S)\s*$/;
    const STEP_MODEL_SELECTOR = '[data-step], [data-fade-after], [data-focus]';
    // New-feature attributes an <img data-inline> passes on to its inlined <svg>
    const INLINE_IMG_STEP_ATTRS = ['data-fade-after', 'data-focus', 'data-morph'];
    // Never dimmed by data-focus: not rendered, or not slide content
    const FOCUS_SKIP_TAGS = new Set(['style', 'script', 'template', 'noscript', 'defs', 'title', 'desc',
        'metadata', 'lineargradient', 'radialgradient', 'clippath', 'mask', 'marker', 'pattern', 'filter', 'symbol']);
    const DEFAULT_MORPH_MS = 700;

    const stepModels = new WeakMap(); // element -> { fade: range|null, focus: [{raw, range, selector}]|null }
    const engineCountedSlides = new WeakSet(); // slides whose WS.slideSteps entry came from parseMaxStep
    const warnedFocusSelectors = new Set();

    function parseRelativeStep(raw) {
        if (raw === undefined || raw === null) return null;
        const m = RELATIVE_STEP_RE.exec(String(raw).trim());
        if (!m) return null;
        return { op: m[1], len: m[2] === undefined ? null : parseStrictInt(m[2]) };
    }

    function isRelativeStep(raw) {
        return parseRelativeStep(raw) !== null;
    }

    // "+": one past the largest data-step start so far; "=": the previous
    // data-step's start (or "+" when there is none); ":N" makes it N steps long.
    function resolveRelativeRange(rel, counters) {
        if (rel.len === 0) return null;
        const start = rel.op === '=' && counters.last !== null ? counters.last : counters.maxStart + 1;
        return { start: start, end: rel.len === null ? undefined : start + rel.len - 1 };
    }

    function resolveFadeRange(raw, counters) {
        if (raw === null || String(raw).trim() === '') return null;
        const rel = parseRelativeStep(raw);
        return rel ? resolveRelativeRange(rel, counters) : parseStepValue(raw);
    }

    // data-focus parts resolve after the container's own subtree, so "+" on a
    // figure means "after everything inside it". A relative focus takes a new
    // step that later "+" steps count; fades never do.
    function resolveFocusParts(container, counters) {
        const model = stepModels.get(container) || { fade: null, focus: null };
        model.focus = String(container.getAttribute('data-focus') || '').split(';')
            .map(part => part.trim())
            .filter(Boolean)
            .map(part => {
                const m = FOCUS_PART_RE.exec(part);
                if (!m) return { raw: part, range: null, selector: null };
                const rel = parseRelativeStep(m[1]);
                let range;
                if (rel) {
                    range = resolveRelativeRange(rel, counters);
                    if (range && rel.op === '+') counters.maxStart = Math.max(counters.maxStart, range.start);
                } else {
                    range = parseStepValue(m[1]);
                }
                return { raw: part, range: range, selector: m[2] };
            });
        stepModels.set(container, model);
    }

    function adoptInlineImgStepAttributes(slide) {
        slide.querySelectorAll('svg[data-ws-original-img]').forEach(svg => {
            if (svg._wsStepAttrsAdopted) return;
            svg._wsStepAttrsAdopted = true;
            const template = document.createElement('template');
            template.innerHTML = svg.getAttribute('data-ws-original-img') || '';
            const img = template.content.firstElementChild;
            if (!img) return;
            INLINE_IMG_STEP_ATTRS.forEach(name => {
                if (img.hasAttribute(name) && !svg.hasAttribute(name)) svg.setAttribute(name, img.getAttribute(name));
            });
        });
    }

    // Resolve every relative step, fade and focus range on a slide in document
    // order (inlined SVG content included). Idempotent; reruns pick up content
    // added later (external tables, reveal lists, Presto slide replacements).
    function resolveSlideSteps(slide) {
        adoptInlineImgStepAttributes(slide);
        const counters = { maxStart: 0, last: null };
        const pendingFocus = [];
        function closeFocusContainersBefore(node) {
            while (pendingFocus.length) {
                const top = pendingFocus[pendingFocus.length - 1];
                if (node && top.contains(node)) return;
                resolveFocusParts(pendingFocus.pop(), counters);
            }
        }
        if (slide.hasAttribute('data-focus')) pendingFocus.push(slide);
        slide.querySelectorAll(STEP_MODEL_SELECTOR).forEach(el => {
            closeFocusContainersBefore(el);
            if (el.hasAttribute('data-step')) {
                const raw = el.getAttribute('data-step');
                const rel = parseRelativeStep(raw);
                const range = rel ? resolveRelativeRange(rel, counters) : parseStepValue(raw);
                el._ws_step = range; // the reveal cache getCachedStep reads
                if (range) {
                    counters.last = range.start;
                    counters.maxStart = Math.max(counters.maxStart, range.start);
                }
            }
            stepModels.set(el, {
                fade: el.hasAttribute('data-fade-after') ? resolveFadeRange(el.getAttribute('data-fade-after'), counters) : null,
                focus: null
            });
            if (el.hasAttribute('data-focus')) pendingFocus.push(el);
        });
        closeFocusContainersBefore(null);
    }

    function stepModelMaxStep(slide) {
        let max = 0;
        const consider = range => {
            if (range) max = Math.max(max, range.start, range.end === undefined ? 0 : range.end);
        };
        [slide].concat(Array.from(slide.querySelectorAll('[data-fade-after], [data-focus]'))).forEach(el => {
            const model = stepModels.get(el);
            if (!model) return;
            consider(model.fade);
            (model.focus || []).forEach(part => consider(part.range));
        });
        return max;
    }

    // Slides replaced or rebuilt by other code (Presto's own slide patcher)
    // get resolved and counted the first time the engine shows them.
    function ensureStepModel(slide) {
        if (!slide || engineCountedSlides.has(slide) || WS.slides.indexOf(slide) === -1) return;
        applyRevealShortcuts(slide);
        WS.slideSteps.set(slide, parseMaxStep(slide));
    }

    const REVEAL_CACHE_KEYS = ['_ws_step', '_ws_highlight', '_ws_dim', '_ws_class'];

    // A slide whose DOM an editor changed in place (undo/redo, a rewritten
    // data-step): drop every cached and generated step, rebuild the reveal
    // shortcuts and the step model from the attributes as they are now, and
    // recount. Idempotent. When it is the current slide the current step is
    // re-applied (clamped if the slide lost steps) and 'navigate' fires if the
    // step or the step count changed; other slides take their state at the
    // next goToSlide. Returns the new max step, or null for an unknown slide.
    function refreshSlideSteps(slideOrIndex) {
        const slide = slideOrIndex && slideOrIndex.nodeType === 1
            ? slideOrIndex
            : WS.slides[parseStrictInt(slideOrIndex)];
        if (!slide || WS.slides.indexOf(slide) === -1) return null;
        const before = WS.slideSteps.get(slide);
        // Generated reveal-list steps are rebuilt, so edits that add, drop or
        // reorder items (or remove data-reveal) renumber them correctly.
        slide.querySelectorAll('[data-ws-auto-step]').forEach(el => {
            el.removeAttribute('data-step');
            el.removeAttribute('data-ws-auto-step');
        });
        [slide].concat(Array.from(slide.querySelectorAll('*'))).forEach(el => {
            REVEAL_CACHE_KEYS.forEach(key => { if (el[key] !== undefined) delete el[key]; });
        });
        stepModels.delete(slide); // the slide's own data-focus may be gone
        // Classes left by attributes that no longer exist would stick
        slide.querySelectorAll('.ws-highlighted:not([data-highlight])').forEach(el => el.classList.remove('ws-highlighted'));
        slide.querySelectorAll('.ws-dimmed:not([data-dim])').forEach(el => el.classList.remove('ws-dimmed'));
        engineCountedSlides.delete(slide);
        applyRevealShortcuts(slide);
        const maxStep = parseMaxStep(slide);
        WS.slideSteps.set(slide, maxStep);
        if (WS.slides[WS.currentIndex] === slide) {
            const step = Math.min(WS.currentStep || 0, maxStep);
            const changed = step !== WS.currentStep || before !== maxStep;
            WS.currentStep = step;
            applyStep(slide, step);
            if (changed) emitNavigate();
        }
        return maxStep;
    }

    WS.refreshSlideSteps = refreshSlideSteps;

    function stepModelSlide(el) {
        if (!el || !el.closest) return null;
        const slide = el.classList && el.classList.contains('slide') ? el : el.closest('.slide');
        return slide && WS.slides.indexOf(slide) !== -1 ? slide : null;
    }

    function publicRange(range) {
        return range ? { start: range.start, end: range.end === undefined ? Infinity : range.end } : null;
    }

    // Resolved reveal range of one element, as the engine uses it, so tools
    // never re-parse authored values ("+", "=" need the whole slide).
    // attr: 'step' (default), 'fade-after', 'highlight', 'dim', 'class'
    // (adds className) or 'focus' (an array of {start, end, selector}).
    // End is Infinity when open; null when absent or malformed.
    WS.getRevealRange = function (el, attr) {
        if (!el || el.nodeType !== 1) return null;
        const name = attr === undefined || attr === null ? 'step' : String(attr).replace(/^data-/, '');
        ensureStepModel(stepModelSlide(el));
        if (name === 'step' || name === 'highlight' || name === 'dim') {
            return el.hasAttribute('data-' + name) ? publicRange(getCachedStep(el, name)) : null;
        }
        if (name === 'class') {
            if (!el.hasAttribute('data-class')) return null;
            const cached = getCachedStep(el, 'class');
            if (!cached) return null;
            const range = publicRange(cached.range);
            range.className = cached.cls;
            return range;
        }
        const model = stepModels.get(el);
        if (name === 'fade-after') return model ? publicRange(model.fade) : null;
        if (name === 'focus') {
            if (!model || !model.focus) return null;
            return model.focus.map(part => {
                const range = publicRange(part.range) || { start: null, end: null };
                range.selector = part.selector;
                return range;
            });
        }
        return null;
    };

    /* --- Fades and focus --- */

    function inlinedSvgFor(el) {
        return el && el.closest ? el.closest('svg[data-ws-original-img]') : null;
    }

    // Inlined SVG ids carry a ws-svgN- prefix; let "#name" in a focus selector
    // match the authored id as well as its prefixed form.
    function focusSelectorFor(container, selector) {
        if (selector.indexOf('#') === -1) return selector;
        const ids = Array.from(container.querySelectorAll('[id]')).map(el => el.id);
        return selector.replace(/#(-?[_a-zA-Z][\w-]*)/g, (match, name) => {
            const prefixed = ids.filter(id => id.length > name.length && id.endsWith('-' + name) &&
                /^ws-svg\d+-$/.test(id.slice(0, id.length - name.length)));
            if (!prefixed.length) return match;
            return ':is(' + [match].concat(prefixed.map(id => '#' + CSS.escape(id))).join(', ') + ')';
        });
    }

    function focusMatches(container, part) {
        if (!part.selector) return null;
        if (part.compiled === undefined) part.compiled = focusSelectorFor(container, part.selector);
        try {
            return container.querySelectorAll(part.compiled);
        } catch (err) {
            if (!warnedFocusSelectors.has(part.selector)) {
                warnedFocusSelectors.add(part.selector);
                console.warn('Ignoring invalid data-focus selector:', part.selector);
            }
            return null;
        }
    }

    // Dim the largest subtrees of the container that neither match the focus
    // selector nor contain a match (so nothing is dimmed twice).
    function collectFocusFades(container, matches, faded) {
        if (!matches || matches.length === 0) return;
        const matched = new Set(matches);
        const holders = new Set();
        matched.forEach(m => {
            for (let n = m.parentNode; n && n !== container; n = n.parentNode) holders.add(n);
        });
        (function walk(parent) {
            Array.from(parent.children).forEach(child => {
                if (matched.has(child)) return;
                if (holders.has(child)) {
                    walk(child);
                    return;
                }
                if (FOCUS_SKIP_TAGS.has(child.localName.toLowerCase())) return;
                if (child.matches('aside.notes')) return;
                faded.add(child);
            });
        })(container);
    }

    // One focus per container at a time: of the parts active at this step,
    // the one that started last ("4:.a; 6:.b" focuses .a at 4-5, .b from 6).
    function activeFocusPart(container, step) {
        const model = stepModels.get(container);
        let best = null;
        (model && model.focus || []).forEach(part => {
            if (part.selector && isActive(part.range, step) && (!best || part.range.start >= best.range.start)) best = part;
        });
        return best;
    }

    function applyFadeAndFocus(slide, step) {
        const faded = new Set();
        const containers = slide.hasAttribute('data-focus') ? [slide] : [];
        slide.querySelectorAll('[data-fade-after], [data-focus]').forEach(el => {
            const model = stepModels.get(el);
            if (model && model.fade && isActive(model.fade, step)) faded.add(el);
            if (el.hasAttribute('data-focus')) containers.push(el);
        });
        containers.forEach(container => {
            const part = activeFocusPart(container, step);
            if (part) collectFocusFades(container, focusMatches(container, part), faded);
        });
        slide.querySelectorAll('.ws-faded').forEach(el => {
            if (!faded.has(el)) el.classList.remove('ws-faded');
        });
        faded.forEach(el => el.classList.add('ws-faded'));
    }

    /* --- Morphs --- */
    // Elements sharing data-morph="KEY" on a slide are one object. When the
    // shown one changes on nextStep/prevStep, the newly shown element is
    // animated from the old one's box to its own. Geometry is measured with
    // getScreenCTM/getBoundingClientRect, which agree under CSS zoom (Presto)
    // and transform: scale (the engine); SVG offsets are written as percentages
    // of the view box because WebKit multiplies px lengths in CSS transforms on
    // SVG elements by an ancestor's CSS zoom. goToSlide never animates, so
    // tools and jumps land in the final state.

    const morphAnimations = []; // { slide, animation }
    let morphsDisabled = false;

    function disableMorphs() {
        morphsDisabled = true;
        cancelMorphs(null);
    }

    function cancelMorphs(slide) {
        for (let i = morphAnimations.length - 1; i >= 0; i--) {
            if (slide && morphAnimations[i].slide !== slide) continue;
            const entry = morphAnimations.splice(i, 1)[0];
            try { entry.animation.cancel(); } catch (err) { /* already gone */ }
        }
    }

    function trackMorphAnimation(slide, animation) {
        const entry = { slide: slide, animation: animation };
        morphAnimations.push(entry);
        const forget = () => {
            const i = morphAnimations.indexOf(entry);
            if (i !== -1) morphAnimations.splice(i, 1);
        };
        animation.addEventListener('finish', forget);
        animation.addEventListener('cancel', forget);
    }

    function prefersReducedMotion() {
        return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    }

    function morphKeyOf(el) {
        const key = (el.getAttribute('data-morph') || '').trim();
        return key || null;
    }

    function shownAtStep(el, slide, step) {
        for (let node = el; node && node !== slide; node = node.parentNode) {
            if (node.nodeType === 1 && node.hasAttribute('data-step') && !isActive(getCachedStep(node, 'step'), step)) return false;
        }
        return true;
    }

    // key -> the first element with that key shown at the step
    function morphTargetsAt(slide, step) {
        const out = new Map();
        slide.querySelectorAll('[data-morph]').forEach(el => {
            const key = morphKeyOf(el);
            if (key && !out.has(key) && shownAtStep(el, slide, step)) out.set(key, el);
        });
        return out;
    }

    function isSvgGraphic(el) {
        // An outermost <svg> is a CSS box like any HTML element
        return typeof SVGGraphicsElement !== 'undefined' && el instanceof SVGGraphicsElement && !!el.ownerSVGElement;
    }

    function boxCorners(x, y, w, h) {
        return [new DOMPoint(x, y), new DOMPoint(x + w, y), new DOMPoint(x, y + h), new DOMPoint(x + w, y + h)];
    }

    function cornersBox(points) {
        const xs = points.map(p => p.x);
        const ys = points.map(p => p.y);
        const x = Math.min.apply(null, xs);
        const y = Math.min.apply(null, ys);
        return { x: x, y: y, w: Math.max.apply(null, xs) - x, h: Math.max.apply(null, ys) - y };
    }

    // The element's box corners in screen coordinates, as rendered right now
    // (an in-flight morph included).
    function screenCorners(el) {
        if (isSvgGraphic(el)) {
            const m = screenMatrix(el);
            if (!m) return null;
            const b = el.getBBox();
            return boxCorners(b.x, b.y, b.width, b.height).map(p => p.matrixTransform(m));
        }
        const r = el.getBoundingClientRect();
        return boxCorners(r.left, r.top, r.width, r.height);
    }

    function axisScale(target, own) {
        return target > 1e-6 && own > 1e-6 ? target / own : 1;
    }

    function cssNumber(n) {
        return Math.abs(n) < 1e-9 ? '0' : String(Math.round(n * 1e6) / 1e6);
    }

    // getScreenCTM gives an SVGMatrix, which only multiplies other SVGMatrix objects
    function screenMatrix(el) {
        const m = el && el.getScreenCTM ? el.getScreenCTM() : null;
        return m ? new DOMMatrix([m.a, m.b, m.c, m.d, m.e, m.f]) : null;
    }

    function rectCentre(el) {
        const r = el.getBoundingClientRect();
        return new DOMPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
    }

    function svgMorphFrame(m, unit) {
        return {
            transform: 'translate(' + cssNumber(m.e / unit.x) + unit.name + ', ' + cssNumber(m.f / unit.y) + unit.name +
                ') matrix(' + [m.a, m.b, m.c, m.d].map(cssNumber).join(', ') + ', 0, 0)',
            transformOrigin: '0px 0px',
            transformBox: 'view-box'
        };
    }

    // Parent user units moved per 1% (else per 1px) of a CSS translate on this
    // element, measured with a probe. Percentages are of the view box in most
    // cases, of the element's own box for WebKit <text>; px would be scaled by
    // CSS zoom in WebKit. Either way the result is zoom- and scale-invariant.
    function svgTranslateUnit(el, base, parent) {
        const parentInverse = new DOMMatrix([parent.a, parent.b, parent.c, parent.d, 0, 0]).inverse();
        const at = (v, name) => {
            const frame = svgMorphFrame(new DOMMatrix([base.a, base.b, base.c, base.d, v, v]), { x: 1, y: 1, name: name });
            const probe = el.animate([frame, frame], { duration: 1e6 });
            const centre = rectCentre(el);
            probe.cancel();
            return centre;
        };
        for (const name of ['%', 'px']) {
            const p0 = at(0, name);
            const p1 = at(10, name);
            const d = new DOMPoint(p1.x - p0.x, p1.y - p0.y).matrixTransform(parentInverse);
            if (Math.abs(d.x) > 1e-3 && Math.abs(d.y) > 1e-3) return { x: d.x / 10, y: d.y / 10, name: name };
        }
        return null;
    }

    function svgMorphFrames(el, fromCorners) {
        const own = screenMatrix(el);
        const parent = screenMatrix(el.parentNode);
        if (!own || !parent) return null;
        const a = cornersBox(fromCorners.map(p => p.matrixTransform(own.inverse())));
        const b = el.getBBox();
        const sx = axisScale(a.w, b.width);
        const sy = axisScale(a.h, b.height);
        // Map own box onto the old one, centre on centre, in own user space
        const g = new DOMMatrix([sx, 0, 0, sy,
            a.x + a.w / 2 - sx * (b.x + b.width / 2),
            a.y + a.h / 2 - sy * (b.y + b.height / 2)]);
        const base = parent.inverse().multiply(own); // the element's transform in its parent's space
        const unit = svgTranslateUnit(el, base, parent);
        if (!unit) return null;
        return [svgMorphFrame(base.multiply(g), unit), svgMorphFrame(base, unit)];
    }

    // Screen px per local px along x and y, measured by a probe translation
    // (exact under zoom, scale and scaled ancestors such as fitted tables).
    function htmlPxScale(el, base) {
        const shift = 'translate(100px, 100px)' + (base ? ' ' + base : '');
        const r0 = el.getBoundingClientRect();
        const probe = el.animate([{ transform: shift }, { transform: shift }], { duration: 1e6 });
        const r1 = el.getBoundingClientRect();
        probe.cancel();
        const kx = (r1.left - r0.left) / 100;
        const ky = (r1.top - r0.top) / 100;
        return Math.abs(kx) > 1e-6 && Math.abs(ky) > 1e-6 ? { x: kx, y: ky } : null;
    }

    function htmlMorphFrames(el, fromCorners) {
        const a = cornersBox(fromCorners);
        const style = getComputedStyle(el);
        const base = style.transform && style.transform !== 'none' ? style.transform : '';
        const k = htmlPxScale(el, base);
        if (!k) return null; // transforms do not apply (display: inline)
        const r = el.getBoundingClientRect();
        if (!base) {
            const sx = axisScale(a.w, r.width);
            const sy = axisScale(a.h, r.height);
            const dx = (a.x + a.w / 2 - r.left - sx * r.width / 2) / k.x;
            const dy = (a.y + a.h / 2 - r.top - sy * r.height / 2) / k.y;
            return [
                { transform: 'translate(' + cssNumber(dx) + 'px, ' + cssNumber(dy) + 'px) scale(' + cssNumber(sx) + ', ' + cssNumber(sy) + ')', transformOrigin: '0px 0px' },
                { transform: 'translate(0px, 0px) scale(1, 1)', transformOrigin: '0px 0px' }
            ];
        }
        // Own CSS transform: move centre to centre and keep it (no resize)
        const dx = (a.x + a.w / 2 - r.left - r.width / 2) / k.x;
        const dy = (a.y + a.h / 2 - r.top - r.height / 2) / k.y;
        return [
            { transform: 'translate(' + cssNumber(dx) + 'px, ' + cssNumber(dy) + 'px) ' + base },
            { transform: 'translate(0px, 0px) ' + base }
        ];
    }

    function cssTimeMs(raw, fallback) {
        const m = /^\s*(\d*\.?\d+)\s*(ms|s)\s*$/.exec(raw || '');
        if (!m) return fallback;
        return Number(m[1]) * (m[2] === 's' ? 1000 : 1);
    }

    function finishOpacityTransitions(el) {
        if (typeof el.getAnimations !== 'function') return;
        el.getAnimations().forEach(anim => {
            if (anim.transitionProperty === 'opacity') anim.finish();
        });
    }

    // Before the step changes: what to morph, and where each old element is now.
    function planMorphs(slide, fromStep, toStep) {
        if (morphsDisabled || prefersReducedMotion() || typeof Element.prototype.animate !== 'function') return null;
        if (!slide.querySelector('[data-morph]')) return null;
        const before = morphTargetsAt(slide, fromStep);
        const after = morphTargetsAt(slide, toStep);
        const plan = [];
        after.forEach((toEl, key) => {
            const fromEl = before.get(key);
            if (!fromEl || fromEl === toEl) return;
            let corners = null;
            try {
                corners = screenCorners(fromEl);
            } catch (err) {
                corners = null; // not rendered (getBBox throws)
            }
            if (corners) plan.push({ key: key, fromEl: fromEl, toEl: toEl, corners: corners });
        });
        return plan.length ? plan : null;
    }

    function runMorphs(slide, plan) {
        if (!plan) return;
        plan.forEach(item => {
            try {
                const frames = isSvgGraphic(item.toEl)
                    ? svgMorphFrames(item.toEl, item.corners)
                    : htmlMorphFrames(item.toEl, item.corners);
                if (!frames) return;
                const style = getComputedStyle(item.toEl);
                const duration = cssTimeMs(style.getPropertyValue('--ws-morph-duration'), DEFAULT_MORPH_MS);
                const easing = style.getPropertyValue('--ws-morph-easing').trim() || 'ease-in-out';
                // The moving element shows at once and the old one hides at once
                // (no cross-fade ghost); their reveal fades are skipped.
                finishOpacityTransitions(item.toEl);
                finishOpacityTransitions(item.fromEl);
                let animation;
                try {
                    animation = item.toEl.animate(frames, { duration: duration, easing: easing });
                } catch (err) {
                    animation = item.toEl.animate(frames, { duration: duration, easing: 'ease-in-out' });
                }
                trackMorphAnimation(slide, animation);
                trackMorphAnimation(slide, item.fromEl.animate([{ opacity: 0 }, { opacity: 0 }], { duration: duration }));
            } catch (err) {
                console.warn('WebSlides morph failed for data-morph="' + item.key + '"', err);
            }
        });
    }

    function applyStepWithMorphs(slide, fromStep, toStep) {
        let plan = null;
        try {
            plan = planMorphs(slide, fromStep, toStep);
        } catch (err) {
            console.warn('WebSlides morph planning failed', err);
        }
        applyStep(slide, toStep);
        runMorphs(slide, plan);
    }

    /* --- Step description (for outlines and agents) --- */

    function inlinedSvgName(svg) {
        if (svg._wsSvgName === undefined) {
            const template = document.createElement('template');
            template.innerHTML = svg.getAttribute('data-ws-original-img') || '';
            const img = template.content.firstElementChild;
            const src = img ? (img.getAttribute('src') || '') : '';
            svg._wsSvgName = src.indexOf('data:') === 0 ? 'svg' : (src.split(/[?#]/)[0].split('/').pop() || 'svg');
        }
        return svg._wsSvgName;
    }

    function describeLabel(el, slide) {
        if (el === slide) return 'slide';
        const svg = inlinedSvgFor(el);
        if (svg === el) return 'svg(' + inlinedSvgName(svg) + ')';
        let label;
        const id = (el.id || '').replace(/^ws-svg\d+-/, '');
        if (id) {
            label = '#' + id;
        } else if (el.getAttribute('data-label')) {
            label = el.getAttribute('data-label');
        } else if (el.getAttribute('aria-label')) {
            label = el.getAttribute('aria-label');
        } else {
            const cls = Array.from(el.classList || []).filter(c => c.indexOf('ws-') !== 0)[0];
            const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
            label = el.localName + (cls ? '.' + cls : '') +
                (text ? ' "' + (text.length > 32 ? text.slice(0, 30) + '…' : text) + '"' : '');
        }
        return svg ? inlinedSvgName(svg) + ' ' + label : label;
    }

    function collapseLabels(labels) {
        const counts = new Map();
        labels.forEach(l => counts.set(l, (counts.get(l) || 0) + 1));
        return Array.from(counts, ([l, n]) => n > 1 ? l + ' ×' + n : l);
    }

    // What changes at each step of a slide. Returns null for an unknown slide,
    // else { index, maxStep, steps: [...], warnings: [string] } where each step
    // is { step, appear, disappear, fade, unfade, focus, unfocus, morph,
    // classes, highlight, dim } (every key always present; lists may be empty).
    WS.describeSteps = function (slideIndex) {
        const index = slideIndex === undefined || slideIndex === null ? WS.currentIndex : parseStrictInt(slideIndex);
        const slide = index !== null ? WS.slides[index] : null;
        if (!slide) return null;
        ensureStepModel(slide);
        const maxStep = safeMaxStepForSlide(slide);
        const warnings = [];
        const label = el => describeLabel(el, slide);

        const stepped = Array.from(slide.querySelectorAll('[data-step]'));
        stepped.forEach(el => {
            if (!getCachedStep(el, 'step')) warnings.push(label(el) + ': data-step="' + el.getAttribute('data-step') + '" is malformed (never shown)');
        });
        const fading = [];
        const focusContainers = [];
        const focusMatched = new Map(); // focus part -> number of matches
        [slide].concat(Array.from(slide.querySelectorAll('[data-fade-after], [data-focus]'))).forEach(el => {
            const model = stepModels.get(el);
            if (el.hasAttribute('data-fade-after')) {
                if (model && model.fade) fading.push({ el: el, range: model.fade });
                else warnings.push(label(el) + ': data-fade-after="' + el.getAttribute('data-fade-after') + '" is malformed');
            }
            if (!el.hasAttribute('data-focus')) return;
            focusContainers.push(el);
            (model && model.focus || []).forEach(part => {
                if (!part.range || !part.selector) {
                    warnings.push(label(el) + ': data-focus part "' + part.raw + '" is malformed (want R:SELECTOR)');
                    return;
                }
                const matches = focusMatches(el, part);
                const matched = matches ? matches.length : 0;
                if (!matches) warnings.push(label(el) + ': data-focus selector "' + part.selector + '" is invalid');
                else if (!matched) warnings.push(label(el) + ': data-focus selector "' + part.selector + '" matches nothing');
                focusMatched.set(part, matched);
            });
        });
        const classed = [];
        slide.querySelectorAll('[data-class]').forEach(el => {
            const cached = getCachedStep(el, 'class');
            if (cached) classed.push({ el: el, cls: cached.cls, range: cached.range });
        });
        const marked = attr => Array.from(slide.querySelectorAll('[data-' + attr + ']'))
            .map(el => ({ el: el, range: getCachedStep(el, attr) }))
            .filter(x => x.range);
        const highlighted = marked('highlight');
        const dimmed = marked('dim');

        const morphCounts = new Map();
        slide.querySelectorAll('[data-morph]').forEach(el => {
            const key = morphKeyOf(el);
            if (!key) return;
            morphCounts.set(key, (morphCounts.get(key) || 0) + 1);
            if (!isSvgGraphic(el) && getComputedStyle(el).display === 'inline') {
                warnings.push(label(el) + ': data-morph="' + key + '" on an inline element does not move (use inline-block)');
            }
        });
        morphCounts.forEach((n, key) => {
            if (n < 2) warnings.push('data-morph="' + key + '" has only one element');
        });
        const crowded = new Set();

        const steps = [];
        let prevTargets = null;
        for (let k = 0; k <= maxStep; k++) {
            const now = range => isActive(range, k);
            const was = range => k > 0 && isActive(range, k - 1);
            const turnsOn = range => now(range) && !was(range);
            const turnsOff = range => !now(range) && was(range);
            const entry = { step: k, appear: [], disappear: [], fade: [], unfade: [], focus: [], unfocus: [],
                morph: [], classes: [], highlight: [], dim: [] };

            const targets = morphTargetsAt(slide, k);
            const morphing = new Set();
            if (prevTargets) {
                targets.forEach((toEl, key) => {
                    const fromEl = prevTargets.get(key);
                    if (!fromEl || fromEl === toEl) return;
                    entry.morph.push({ key: key, from: label(fromEl), to: label(toEl) });
                    morphing.add(fromEl);
                    morphing.add(toEl);
                });
            }
            morphCounts.forEach((n, key) => {
                if (n < 2 || crowded.has(key)) return;
                const shown = Array.from(slide.querySelectorAll('[data-morph]'))
                    .filter(el => morphKeyOf(el) === key && shownAtStep(el, slide, k));
                if (shown.length > 1) {
                    crowded.add(key);
                    warnings.push('data-morph="' + key + '": ' + shown.length + ' elements shown at step ' + k + ' (the first one morphs)');
                }
            });
            prevTargets = targets;

            // Effective visibility (stepped ancestors included); an element
            // that toggles with its nearest stepped ancestor is covered by it.
            const toggled = new Map();
            const toggle = el => {
                if (!toggled.has(el)) {
                    const on = shownAtStep(el, slide, k);
                    toggled.set(el, k > 0 && on !== shownAtStep(el, slide, k - 1) ? (on ? 'on' : 'off') : (k === 0 && on ? 'on' : null));
                }
                return toggled.get(el);
            };
            stepped.forEach(el => {
                if (morphing.has(el)) return;
                const change = toggle(el);
                if (!change) return;
                const holder = el.parentNode && el.parentNode.closest ? el.parentNode.closest('[data-step]') : null;
                if (holder && slide.contains(holder) && holder !== slide && toggle(holder) === change) return;
                entry[change === 'on' ? 'appear' : 'disappear'].push(label(el));
            });
            fading.forEach(x => {
                if (turnsOn(x.range)) entry.fade.push(label(x.el));
                else if (turnsOff(x.range)) entry.unfade.push(label(x.el));
            });
            focusContainers.forEach(container => {
                const part = activeFocusPart(container, k);
                const before = k > 0 ? activeFocusPart(container, k - 1) : null;
                if (part === before) return;
                if (part) entry.focus.push({ on: label(container), selector: part.selector, matched: focusMatched.get(part) || 0 });
                else entry.unfocus.push({ on: label(container), selector: before.selector });
            });
            classed.forEach(x => {
                if (turnsOn(x.range)) entry.classes.push(label(x.el) + ' +' + x.cls);
                else if (turnsOff(x.range)) entry.classes.push(label(x.el) + ' -' + x.cls);
            });
            highlighted.forEach(x => { if (turnsOn(x.range)) entry.highlight.push(label(x.el)); });
            dimmed.forEach(x => { if (turnsOn(x.range)) entry.dim.push(label(x.el)); });
            ['appear', 'disappear', 'fade', 'unfade', 'classes', 'highlight', 'dim'].forEach(name => {
                entry[name] = collapseLabels(entry[name]);
            });
            steps.push(entry);
        }
        return { index: index, maxStep: maxStep, steps: steps, warnings: warnings };
    };

    /* ===== Navigation ===== */

    function emitNavigate() {
        WS._emit('navigate', WS.getState());
    }

    function goToSlide(index, step) {
        const numericIndex = parseStrictInt(index);
        if (numericIndex === null || numericIndex < 0 || numericIndex >= WS.slides.length) return false;
        index = numericIndex;

        // Pause any playing videos on the current slide
        const current = WS.slides[WS.currentIndex];
        if (current) {
            current.querySelectorAll('video').forEach(v => v.pause());
            current.classList.remove('ws-active');
        }

        // Activate new slide
        WS.currentIndex = index;
        const slide = WS.slides[index];
        slide.classList.add('ws-active');

        // Auto-play videos on the new slide (audience/normal modes, not preview)
        if (WS.mode !== 'preview') {
            slide.querySelectorAll('video').forEach(v => {
                v.play().catch(() => {});
            });
        }

        // Set step
        ensureStepModel(slide);
        const maxStep = safeMaxStepForSlide(slide);
        if (step === undefined || step === null) {
            // Default: show nothing (step 0)
            WS.currentStep = 0;
        } else if (step === 'end') {
            WS.currentStep = maxStep;
        } else {
            const numericStep = parseStrictInt(step);
            WS.currentStep = numericStep !== null ? Math.min(Math.max(0, numericStep), maxStep) : 0;
        }

        applyStep(slide, WS.currentStep);
        updateFooter(slide);
        updateSectionNav(slide);
        updateProgress();
        updateHash();
        emitNavigate();
        return true;
    }

    function nextStep() {
        if (WS.overviewActive) return;
        const slide = WS.slides[WS.currentIndex];
        const maxStep = safeMaxStepForSlide(slide);

        if (WS.currentStep < maxStep) {
            WS.currentStep++;
            applyStepWithMorphs(slide, WS.currentStep - 1, WS.currentStep);
            emitNavigate();
        } else {
            // Next slide
            nextSlide();
        }
    }

    function prevStep() {
        if (WS.overviewActive) return;

        if (WS.currentStep > 0) {
            WS.currentStep--;
            applyStepWithMorphs(WS.slides[WS.currentIndex], WS.currentStep + 1, WS.currentStep);
            emitNavigate();
        } else {
            // Previous slide, show at its last step
            prevSlide();
        }
    }

    function nextSlide() {
        // If in appendix and this is the last appendix slide, don't wrap
        if (WS.currentIndex < WS.slides.length - 1) {
            goToSlide(WS.currentIndex + 1, 0);
        }
    }

    function prevSlide() {
        if (WS.currentIndex > 0) {
            goToSlide(WS.currentIndex - 1, 'end');
        }
    }

    /* ===== Appendix Navigation ===== */

    function goToAppendix(targetId) {
        const target = deckElementById(targetId);
        const targetIndex = slideIndexFromElement(target);
        if (targetIndex === null) return;

        // Save current position (slide index + step) only after the target is valid.
        WS.returnStack.push({ index: WS.currentIndex, step: WS.currentStep });
        goToSlide(targetIndex, firstVisibleStepForTarget(target));
    }

    function returnFromAppendix() {
        if (WS.returnStack.length > 0) {
            const pos = WS.returnStack.pop();
            goToSlide(pos.index, pos.step);
        }
    }

    // Link and hash routes into an appendix slide keep the position to return
    // to, exactly like goToAppendix.
    function goToSlideByRoute(index, step) {
        if (index !== WS.currentIndex && WS.slides[index].classList.contains('appendix')) {
            WS.returnStack.push({ index: WS.currentIndex, step: WS.currentStep });
        }
        goToSlide(index, step);
    }

    /* ===== Footer & Progress ===== */

    /* ===== Section / Subsection Structure ===== */

    // Flat list of every section-header slide occurrence (used for "which section is the
    // current slide in" lookups). Sections may repeat (legacy decks duplicated section
    // headers per subsection); the dedup'd list is in WS.uniqueSections.
    WS.sections = []; // { name, slideIndex }
    WS.uniqueSections = []; // deduplicated by name: { name, slideIndex (first occurrence) }

    // Tree structure used for auto-injected TOCs.
    // [{ name, slideIndex, subsections: [{ name, slideIndex }] }]
    // Sections are deduplicated by name; subsections from later occurrences
    // of the same section are merged into the first occurrence.
    WS.tocStructure = [];

    function buildSectionStructure() {
        WS.sections = [];
        WS.uniqueSections = [];
        WS.tocStructure = [];

        const deck = document.querySelector('.ws-deck');
        let introName = null;
        if (deck && deck.hasAttribute('data-section-nav')) {
            // Decks with section navigation get a virtual intro section by default,
            // so opening content before the first section-header is represented in
            // the nav and in auto-injected section TOCs. Override the name with
            // data-intro-section="Name".
            introName = deck.getAttribute('data-intro-section') || 'Intro';
        }
        const seenNames = new Map(); // name -> tocStructure entry
        if (introName) {
            const entry = { name: introName, slideIndex: 0, subsections: [] };
            WS.sections.push({ name: introName, slideIndex: 0 });
            WS.uniqueSections.push({ name: introName, slideIndex: 0 });
            WS.tocStructure.push(entry);
            seenNames.set(introName, entry);
        }
        let currentTocSection = null;

        WS.mainSlides.forEach(slide => {
            const slideIdx = slideIndexFromElement(slide);
            if (slideIdx === null) return;

            if (slide.classList.contains('subsection-header')) {
                if (!currentTocSection) return; // orphan subsection — ignore
                const h1 = slide.querySelector('h1');
                if (!h1) return;
                currentTocSection.subsections.push({
                    name: h1.textContent.trim(),
                    slideIndex: slideIdx,
                });
                return;
            }

            if (slide.classList.contains('section-header')) {
                const h1 = slide.querySelector('h1');
                if (!h1) return;
                const name = h1.textContent.trim();

                WS.sections.push({ name, slideIndex: slideIdx });

                if (seenNames.has(name)) {
                    // Repeated section header — point currentTocSection at the original
                    // so any following subsections still attach to the right place.
                    currentTocSection = seenNames.get(name);
                } else {
                    const entry = { name, slideIndex: slideIdx, subsections: [] };
                    WS.tocStructure.push(entry);
                    WS.uniqueSections.push({ name, slideIndex: slideIdx });
                    seenNames.set(name, entry);
                    currentTocSection = entry;
                }
            }
        });
    }

    /* ===== Auto-injected Section TOCs ===== */

    function injectSectionTocs() {
        if (WS.tocStructure.length === 0) return;
        // Auto-inject only when the deck has opted into section navigation.
        // This keeps backward compat for decks that intentionally use bare
        // section-header slides without a TOC.
        const deck = document.querySelector('.ws-deck');
        if (!deck || !deck.hasAttribute('data-section-nav')) return;

        WS.mainSlides.forEach(slide => {
            const isSection = slide.classList.contains('section-header');
            const isSubsection = slide.classList.contains('subsection-header');
            if (!isSection && !isSubsection) return;
            // Skip if author wrote a manual TOC (backward compat)
            if (slide.querySelector('.section-toc')) return;

            const slideIdx = slideIndexFromElement(slide);
            if (slideIdx === null) return;

            // Determine active section + subsection for this slide
            let activeSection = null;
            let activeSubsection = null;

            if (isSubsection) {
                // Walk back through tocStructure to find the parent section
                for (const sec of WS.tocStructure) {
                    const sub = sec.subsections.find(s => s.slideIndex === slideIdx);
                    if (sub) {
                        activeSection = sec;
                        activeSubsection = sub;
                        break;
                    }
                }
            } else {
                // Section-header: find by name (handles repeated section headers — they
                // all point to the same tocStructure entry)
                const h1 = slide.querySelector('h1');
                const name = h1 ? h1.textContent.trim() : null;
                if (name) activeSection = WS.tocStructure.find(s => s.name === name);
            }

            if (!activeSection) return;

            // For subsection-header slides, inject a "section eyebrow" above the h1
            // (so the audience sees parent section + current subsection + TOC).
            if (isSubsection && !slide.querySelector('.ws-section-eyebrow')) {
                const h1 = slide.querySelector('h1');
                if (h1) {
                    const eyebrow = document.createElement('div');
                    eyebrow.className = 'ws-section-eyebrow';
                    eyebrow.textContent = activeSection.name;
                    h1.parentNode.insertBefore(eyebrow, h1);
                }
            }

            slide.appendChild(buildSectionTocElement(activeSection, activeSubsection));
        });
    }

    const SUBSECTION_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

    function buildSectionTocElement(activeSection, activeSubsection) {
        const ul = document.createElement('ul');
        ul.className = 'section-toc';

        WS.tocStructure.forEach((sec, i) => {
            const li = document.createElement('li');
            const isActiveSec = sec === activeSection;

            // Section name as a clickable span (so child clicks don't trigger parent nav)
            const label = document.createElement('span');
            label.className = 'ws-toc-label';
            label.textContent = (i + 1) + '. ' + sec.name;
            label.addEventListener('click', e => {
                e.stopPropagation();
                goToSlide(sec.slideIndex, 0);
            });
            li.appendChild(label);

            if (isActiveSec) {
                li.classList.add('active');

                // Expand subsections under the active section
                if (sec.subsections.length > 0) {
                    const subUl = document.createElement('ul');
                    subUl.className = 'ws-subsection-list';
                    sec.subsections.forEach((sub, j) => {
                        const subLi = document.createElement('li');
                        const subLabel = document.createElement('span');
                        subLabel.className = 'ws-toc-label';
                        // data-subsection-numbers="decimal" on .ws-deck numbers subsections 3.1, 3.2
                        // under section 3; the default letters them A, B.
                        const decimal = document.querySelector('.ws-deck[data-subsection-numbers="decimal"]');
                        subLabel.textContent = decimal
                            ? (i + 1) + '.' + (j + 1) + ' ' + sub.name
                            : (SUBSECTION_LETTERS[j] || (j + 1)) + '. ' + sub.name;
                        subLabel.addEventListener('click', e => {
                            e.stopPropagation();
                            goToSlide(sub.slideIndex, 0);
                        });
                        subLi.appendChild(subLabel);
                        if (sub === activeSubsection) subLi.classList.add('active');
                        subUl.appendChild(subLi);
                    });
                    li.appendChild(subUl);
                }
            }

            ul.appendChild(li);
        });

        return ul;
    }

    function buildSectionNav() {
        const deck = document.querySelector('.ws-deck');
        if (!deck || !deck.hasAttribute('data-section-nav')) return;
        if (WS.uniqueSections.length === 0) return;

        // Create the nav bar element using deduplicated sections (subsections excluded)
        const nav = document.createElement('div');
        nav.id = 'ws-section-nav';
        WS.uniqueSections.forEach((sec, i) => {
            const span = document.createElement('span');
            span.className = 'ws-section-nav-item';
            span.textContent = sec.name;
            span.dataset.sectionIndex = i;
            span.addEventListener('click', (e) => {
                e.stopPropagation();
                goToSlide(sec.slideIndex, 0);
            });
            nav.appendChild(span);
            if (i < WS.uniqueSections.length - 1) {
                const sep = document.createElement('span');
                sep.className = 'ws-section-nav-sep';
                sep.textContent = '·';
                nav.appendChild(sep);
            }
        });
        document.body.appendChild(nav);
    }

    function updateSectionNav(slide) {
        const nav = document.getElementById('ws-section-nav');
        if (!nav) return;

        const slideIdx = slideIndexFromElement(slide);
        if (slideIdx === null) return;

        // Hide on title slide only (section nav stays visible in appendix)
        if (slide.classList.contains('title')) {
            nav.style.opacity = '0';
            nav.style.pointerEvents = 'none';
            return;
        }
        nav.style.opacity = '1';
        nav.style.pointerEvents = 'auto';

        // Find which section we're in (last section-header before current
        // slide). Appendix slides belong to no section: nothing is highlighted.
        let activeSectionName = null;
        for (let i = slide.classList.contains('appendix') ? -1 : WS.sections.length - 1; i >= 0; i--) {
            if (WS.sections[i].slideIndex <= slideIdx) {
                activeSectionName = WS.sections[i].name;
                break;
            }
        }

        // Highlight the deduplicated nav item matching the active section name
        nav.querySelectorAll('.ws-section-nav-item').forEach((item, i) => {
            item.classList.toggle('active',
                activeSectionName !== null && WS.uniqueSections[i] && WS.uniqueSections[i].name === activeSectionName);
        });
    }

    function updateFooter(slide) {
        // Full-bleed photo slides (or data-no-chrome) hide footer, progress bar and section nav
        const bare = slide.classList.contains('photo') || slide.hasAttribute('data-no-chrome');
        document.body.classList.toggle('ws-bare-slide', bare);

        const right = document.getElementById('ws-footer-right');
        if (!right) return;

        if (slide.classList.contains('appendix')) {
            right.textContent = 'Appendix';
        } else if (slide.classList.contains('title')) {
            right.textContent = '';
        } else {
            const num = slide.dataset.wsMainNumber;
            right.textContent = num + ' / ' + WS.totalMainSlides;
        }

        updateSectionNav(slide);
    }

    function createProgressBar() {
        const bar = document.createElement('div');
        bar.id = 'ws-progress';
        document.body.appendChild(bar);
    }

    function updateProgress() {
        const bar = document.getElementById('ws-progress');
        if (!bar) return;
        const slide = WS.slides[WS.currentIndex];

        if (slide.classList.contains('appendix')) {
            bar.style.width = '100%';
            bar.style.opacity = '0.3';
        } else {
            const num = parseStrictInt(slide.dataset.wsMainNumber) || 0;
            const pct = WS.totalMainSlides > 0 ? (num / WS.totalMainSlides) * 100 : 0;
            bar.style.width = pct + '%';
            bar.style.opacity = '1';
        }
    }

    /* ===== URL Hash ===== */

    function decodeFragment(fragment) {
        try {
            return decodeURIComponent(String(fragment));
        } catch (err) {
            try {
                return String(fragment);
            } catch (stringErr) {
                return '';
            }
        }
    }

    function updateHash() {
        const slide = WS.slides[WS.currentIndex];
        if (slide.id) {
            history.replaceState(null, '', '#' + encodeURIComponent(slide.id));
        } else {
            history.replaceState(null, '', '#slide-' + (WS.currentIndex + 1));
        }
    }

    // `asRoute`: a hash change after load, which pushes the appendix return
    // position; the initial deep link does not.
    function navigateToHash(asRoute) {
        const hash = window.location.hash.slice(1);
        if (!hash) return false;
        const go = asRoute ? goToSlideByRoute : goToSlide;

        // Try by ID first
        const byId = deckElementById(hash);
        const byIdIndex = slideIndexFromElement(byId);
        if (byIdIndex !== null) {
            go(byIdIndex, firstVisibleStepForTarget(byId));
            return true;
        }

        // Try slide-N format
        const match = decodeFragment(hash).match(/^slide-(\d+)$/);
        if (match) {
            const parsed = parseStrictInt(match[1]);
            const num = parsed === null ? -1 : parsed - 1;
            if (num >= 0 && num < WS.slides.length) {
                go(num, 0);
                return true;
            }
        }

        return false;
    }

    function bindHashChange() {
        window.addEventListener('hashchange', () => {
            navigateToHash(true);
        });
    }

    /* ===== Scaling ===== */

    function scaleSlides() {
        // Inside Presto the slide frame is PrestoLayout's (presto/edit-layout.js)
        if (window.PrestoLayout && typeof window.PrestoLayout.current === 'function' && window.PrestoLayout.current()) {
            window.PrestoLayout.apply();
            return;
        }
        const sw = 1280;
        const sh = 720;
        const scaleX = window.innerWidth / sw;
        const scaleY = window.innerHeight / sh;
        const scale = Math.min(scaleX, scaleY);
        if (!Number.isFinite(scale) || scale <= 0) return;

        WS.slides.forEach(s => {
            s.style.transform = 'translate(-50%, -50%) scale(' + scale + ')';
        });

        // Scale footer to match
        const footer = document.getElementById('ws-footer');
        if (footer) {
            footer.style.transform = 'scale(' + scale + ')';
            footer.style.width = (100 / scale) + '%';
            footer.style.transformOrigin = 'bottom left';
            footer.style.left = '0';
        }

        // Scale section nav to match
        const sectionNav = document.getElementById('ws-section-nav');
        if (sectionNav) {
            sectionNav.style.transform = 'scale(' + scale + ')';
            sectionNav.style.width = (100 / scale) + '%';
            sectionNav.style.transformOrigin = 'top left';
            sectionNav.style.left = '0';
        }
    }

    /* ===== Keyboard ===== */

    function bindKeyboard() {
        document.addEventListener('keydown', (e) => {
            // Don't capture if user is in an input field
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            // In Presto's edit preview Enter and Backspace belong to the editor,
            // even with nothing selected; they never step the slide.
            if ((e.code === 'Enter' || e.code === 'Backspace') && isPrestoEditPreview()) return;

            switch (e.code) {
                case 'ArrowRight':
                case 'Space':
                case 'PageDown':
                case 'Enter':
                    e.preventDefault();
                    if (WS.overviewActive) return;
                    nextStep();
                    break;

                case 'ArrowLeft':
                case 'PageUp':
                case 'Backspace':
                    e.preventDefault();
                    if (WS.overviewActive) return;
                    prevStep();
                    break;

                case 'Home':
                    e.preventDefault();
                    goToSlide(0, 0);
                    break;

                case 'End':
                    e.preventDefault();
                    // Go to last main slide
                    if (WS.mainSlides.length > 0) {
                        const lastMain = WS.mainSlides[WS.mainSlides.length - 1];
                        const idx = slideIndexFromElement(lastMain);
                        if (idx !== null) goToSlide(idx, 'end');
                    }
                    break;

                case 'Escape':
                    e.preventDefault();
                    if (WS.overviewActive) {
                        toggleOverview();
                    } else {
                        returnFromAppendix();
                    }
                    break;

                case 'KeyO':
                    if (!e.metaKey && !e.ctrlKey) {
                        e.preventDefault();
                        toggleOverview();
                    }
                    break;

                case 'ArrowDown':
                    e.preventDefault();
                    if (WS.overviewActive) return;
                    nextStep();
                    break;

                case 'ArrowUp':
                    e.preventDefault();
                    if (WS.overviewActive) return;
                    prevStep();
                    break;
            }
        });
    }

    /* ===== Click Navigation ===== */

    function bindOverviewClicks() {
        document.addEventListener('click', (e) => {
            // Appendix links
            const link = e.target.closest('.ws-appendix-link');
            if (link) {
                e.preventDefault();
                const href = link.getAttribute('href');
                if (href && href.startsWith('#')) {
                    goToAppendix(href.slice(1));
                }
                return;
            }

            const back = e.target.closest('.ws-back-link');
            if (back) {
                e.preventDefault();
                returnFromAppendix();
                return;
            }

            // Plain hash links into an appendix slide keep the return position too;
            // links to main slides keep the browser's default hash navigation.
            const hashLink = e.target.closest('a[href^="#"]');
            if (hashLink) {
                const target = deckElementById(hashLink.getAttribute('href').slice(1));
                const targetIndex = slideIndexFromElement(target);
                if (targetIndex !== null && WS.slides[targetIndex].classList.contains('appendix')) {
                    e.preventDefault();
                    goToSlideByRoute(targetIndex, firstVisibleStepForTarget(target));
                    return;
                }
            }

            // Overview click
            const overviewItem = e.target.closest('.ws-overview-item');
            if (overviewItem) {
                const idx = slideIndexFromValue(overviewItem.dataset.wsIndex);
                if (idx === null) return;
                toggleOverview();
                goToSlide(idx, 0);
                return;
            }
        });
    }

    // Clicks inside these never advance: interactive elements and link widgets,
    // including their labels and gaps (Presto's audience guard lets them through).
    const NO_ADVANCE_CLICK_SELECTOR = 'a, button, video, audio, input, select, textarea, label, summary, ' +
        '[role="button"], [role="link"], .ws-overview-item, .appendix-links, .header-links, [data-ws-link]';

    function bindSlideClicks() {
        document.addEventListener('click', (e) => {
            // Presto sets WS.mode after load, so check it per click, not only at bind time
            if (WS.mode !== 'normal' || WS.overviewActive) return;
            const target = e.target;
            if (!(target instanceof Element) || target.isContentEditable || target.closest(NO_ADVANCE_CLICK_SELECTOR)) return;

            if (!target.closest('.slide')) return;

            // Any click on the slide advances; going back is keyboard-only.
            nextStep();
        });
    }

    /* ===== Overview Mode ===== */

    function createOverview() {
        const container = document.createElement('div');
        container.id = 'ws-overview';
        document.body.appendChild(container);
    }

    function toggleOverview() {
        WS.overviewActive = !WS.overviewActive;
        document.body.classList.toggle('ws-overview-active', WS.overviewActive);

        const container = document.getElementById('ws-overview');

        if (WS.overviewActive) {
            // Build overview thumbnails
            container.innerHTML = '';
            WS.slides.forEach((slide, i) => {
                const item = document.createElement('div');
                item.className = 'ws-overview-item';
                if (i === WS.currentIndex) item.classList.add('ws-overview-current');
                item.dataset.wsIndex = i;

                // Clone slide for thumbnail
                const clone = slide.cloneNode(true);
                clone.classList.remove('ws-active');
                clone.classList.add('ws-overview-slide');
                clone.style.opacity = '1';
                clone.style.pointerEvents = 'none';
                clone.style.position = 'absolute';
                // Presto lays slides out with zoom and offsets; thumbnails scale by transform
                clone.style.zoom = '';
                clone.style.left = '0';
                clone.style.top = '0';

                // Show all reveal steps in overview
                applyAllRevealState(clone);

                item.appendChild(clone);

                // Label
                const label = document.createElement('div');
                label.className = 'ws-overview-label';
                if (slide.classList.contains('appendix')) {
                    const heading = slide.querySelector('h1, h2');
                    label.textContent = 'App: ' + (heading ? heading.textContent : (i + 1));
                } else {
                    const heading = slide.querySelector('h1, h2');
                    label.textContent = slide.dataset.wsMainNumber + '. ' + (heading ? heading.textContent : '');
                }
                item.appendChild(label);

                container.appendChild(item);

                // Scale the cloned slide to fit the thumbnail
                requestAnimationFrame(() => {
                    const itemW = item.offsetWidth;
                    const scaleThumb = itemW / 1280;
                    clone.style.transform = 'scale(' + scaleThumb + ')';
                    clone.style.width = '1280px';
                    clone.style.height = '720px';
                    clone.style.transformOrigin = 'top left';
                });
            });

            // Scroll current into view
            const current = container.querySelector('.ws-overview-current');
            if (current) {
                current.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }
    }

    /* ===== Presto edit preview ===== */
    // The slide rail is Presto's own UI now (presto/edit-chrome.js) and the
    // slide-frame layout lives in presto/edit-layout.js (window.PrestoLayout),
    // which Presto injects into its pages. What stays here is what pages and
    // tools still call: the deprecated rail entry point, the in-place slide
    // patcher and the viewport entry point, which delegate to those files.

    // Deprecated: installs Presto's edit chrome when it is on the page, else
    // does nothing (the engine no longer draws a rail).
    window.installPrestoEditSidebar = function () {
        if (window.PrestoEditChrome && typeof window.PrestoEditChrome.install === 'function') {
            window.PrestoEditChrome.install();
        }
    };

    function refreshPrestoThumbnail(index) {
        const chrome = window.PrestoEditChrome;
        if (!chrome) return;
        try {
            if (typeof chrome.refreshThumbnail === 'function') chrome.refreshThumbnail(index);
            else if (typeof chrome.rebuild === 'function') chrome.rebuild();
        } catch (err) {
            console.warn('WebSlides: Presto thumbnail refresh failed:', err);
        }
    }

    // Presto's edit preview (PrestoLayout in edit mode): keys there edit,
    // they do not present.
    function isPrestoEditPreview() {
        return document.documentElement.classList.contains('presto-layout-edit');
    }

    function replaceSlideReference(slides, current, replacement) {
        if (!slides) return;
        const referenceIndex = slides.indexOf(current);
        if (referenceIndex >= 0) slides[referenceIndex] = replacement;
    }

    function replacePrestoEditSlideHTML(index, sectionHTML) {
        const numericIndex = parseStrictInt(index);
        if (numericIndex === null || numericIndex < 0 || numericIndex >= WS.slides.length) return false;
        if (typeof sectionHTML !== 'string') return false;

        const current = WS.slides[numericIndex];
        if (!current || !current.parentNode) return false;

        const template = document.createElement('template');
        template.innerHTML = sectionHTML.trim();
        const replacement = template.content.firstElementChild;
        if (!replacement || replacement.tagName !== 'SECTION' || !replacement.classList.contains('slide')) return false;
        if (replacement.querySelector('[data-table-src], img[data-inline]')) return false;
        if (current.classList.contains('appendix') !== replacement.classList.contains('appendix')) return false;

        const wasActive = current.classList.contains('ws-active');
        replacement.dataset.wsIndex = String(numericIndex);
        if (current.dataset.wsMainNumber) replacement.dataset.wsMainNumber = current.dataset.wsMainNumber;
        replacement.classList.toggle('ws-active', wasActive);
        replacement.style.cssText = current.style.cssText;

        current.replaceWith(replacement);
        WS.slides[numericIndex] = replacement;
        replaceSlideReference(WS.mainSlides, current, replacement);
        replaceSlideReference(WS.appendixSlides, current, replacement);

        applyRevealShortcuts(replacement);
        const maxStep = parseMaxStep(replacement);
        WS.slideSteps.delete(current);
        WS.slideSteps.set(replacement, maxStep);
        // Authored source: $...$ maths is rendered like the deck's own
        // auto-render did at load (stats and SVG maths follow through
        // watchReplacedSlides).
        if (typeof window.renderMathInElement === 'function') {
            try {
                window.renderMathInElement(replacement, window.__wsKatexOptions || DEFAULT_AUTO_RENDER_OPTIONS);
            } catch (err) {
                console.warn('WebSlides: maths in a replaced slide did not render:', err);
            }
        }
        if (wasActive) {
            goToSlide(numericIndex, Math.min(WS.currentStep || 0, maxStep));
        } else {
            applyStep(replacement, 0);
            if (typeof window.applyPrestoEditViewport === 'function') window.applyPrestoEditViewport();
        }
        refreshPrestoThumbnail(numericIndex);
        if (replacement.querySelector('.table-container table')) {
            WS.refitTables().catch(err => console.warn('Table refit after slide replacement failed:', err));
        }
        return true;
    }

    WS.replacePrestoEditSlideHTML = replacePrestoEditSlideHTML;
    window.replacePrestoEditSlideHTML = replacePrestoEditSlideHTML;

    // One source of truth: when Presto's layout (presto/edit-layout.js) is on
    // the page this only translates the old option names and returns its
    // geometry, plus the old railW and availableW. railWidth becomes
    // insetLeft; no options re-applies the last layout.
    window.applyPrestoEditViewport = function (options) {
        const layout = window.PrestoLayout;
        if (layout && typeof layout.apply === 'function') {
            let geometry;
            if (!options) {
                geometry = layout.apply();
            } else {
                const forward = {};
                if (options.railWidth != null) forward.insetLeft = options.railWidth;
                ['mode', 'insetLeft', 'insetRight', 'insetTop', 'insetBottom', 'padding', 'viewportWidth', 'viewportHeight']
                    .forEach(key => { if (options[key] != null) forward[key] = options[key]; });
                geometry = layout.apply(forward);
            }
            if (!geometry) return null;
            geometry.railW = geometry.insetLeft;
            geometry.availableW = geometry.viewportW - geometry.insetLeft - geometry.insetRight;
            return geometry;
        }
        return legacyPrestoViewport(options);
    };

    // Pages without presto/edit-layout.js (older browser harnesses): the
    // pre-redesign layout, kept unchanged until those harnesses inject the
    // Presto file. Presto itself always injects it.
    function legacyPrestoViewport(options) {
        const opts = options || {};
        const sw = 1280;
        const sh = 720;
        const rawRailW = opts.railWidth != null ? Number(opts.railWidth) : 0;
        const railW = Number.isFinite(rawRailW) && rawRailW > 0 ? rawRailW : 0;
        const viewportW = opts.viewportWidth != null ? Number(opts.viewportWidth) : window.innerWidth;
        const viewportH = opts.viewportHeight != null ? Number(opts.viewportHeight) : window.innerHeight;
        if (!Number.isFinite(viewportW) || !Number.isFinite(viewportH) || viewportW <= 0 || viewportH <= 0) {
            return null;
        }
        const availableW = Math.max(320, viewportW - railW);
        const scale = Math.min(availableW / sw, viewportH / sh);
        if (!Number.isFinite(scale) || scale <= 0) return null;
        const slideW = sw * scale;
        const slideH = sh * scale;
        const slideLeft = Math.max(0, (availableW - slideW) / 2);
        const slideTop = Math.max(0, (viewportH - slideH) / 2);
        const deck = document.querySelector('.ws-deck');

        if (deck) {
            deck.style.position = railW > 0 ? 'fixed' : 'relative';
            deck.style.left = railW > 0 ? railW + 'px' : '0';
            deck.style.top = '0';
            deck.style.width = availableW + 'px';
            deck.style.height = viewportH + 'px';
        }

        WS.slides.forEach(function (s) {
            s.style.zoom = scale;
            s.style.transform = 'none';
            s.style.transformOrigin = 'top left';
            s.style.left = (slideLeft / scale) + 'px';
            s.style.top = (slideTop / scale) + 'px';
        });

        applyPrestoViewportElement(
            document.getElementById('ws-footer'),
            scale,
            railW,
            availableW,
            'bottom left',
            '0'
        );
        applyPrestoViewportElement(
            document.getElementById('ws-section-nav'),
            scale,
            railW,
            availableW,
            'top left'
        );

        return {
            scale: scale,
            railW: railW,
            availableW: availableW,
            slideLeft: slideLeft,
            slideTop: slideTop,
            slideW: slideW,
            slideH: slideH,
            viewportH: viewportH
        };
    }

    function applyPrestoViewportElement(element, scale, railW, availableW, transformOrigin, bottom) {
        if (!element) return;
        element.style.zoom = scale;
        element.style.transform = 'none';
        element.style.transformOrigin = transformOrigin;
        element.style.left = (railW / scale) + 'px';
        element.style.right = 'auto';
        element.style.width = (availableW / scale) + 'px';
        if (bottom !== undefined) element.style.bottom = bottom;
    }

    /* ===== Inline SVGs ===== */
    // Replace <img src="*.svg" data-inline> with actual SVG DOM content,
    // so data-step attributes inside the SVG are reachable by the engine.
    async function inlineSVGs() {
        const imgs = document.querySelectorAll('img[data-inline]');
        let svgCounter = 0;
        await Promise.all(Array.from(imgs).map(async (img) => {
            const src = img.getAttribute('src') || '';
            try {
                if (!src.trim()) {
                    showSvgLoadError(img, src, 'Missing SVG source');
                    return;
                }
                const resp = await fetch(img.src);
                if (!resp.ok) {
                    showSvgLoadError(img, src, resp.status + ' ' + resp.statusText);
                    return;
                }
                let text = await resp.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(text, 'image/svg+xml');
                const svg = doc.documentElement;
                if (!svg || svg.tagName.toLowerCase() !== 'svg' || doc.querySelector('parsererror')) {
                    console.warn('Failed to inline SVG: invalid SVG document', img.src);
                    showSvgLoadError(img, src, 'invalid SVG document');
                    return;
                }
                prefixSvgIds(svg, 'ws-svg' + (svgCounter++) + '-');
                replaceInlineImg(img, svg);
            } catch (e) {
                console.warn('Failed to inline SVG:', img.src, e);
                showSvgLoadError(img, src, (e && e.message) || 'network error');
            }
        }));
    }

    function replaceInlineImg(img, svg) {
        // Copy over style/class from the img
        if (img.style.cssText) svg.style.cssText = img.style.cssText;
        if (img.className) svg.setAttribute('class', img.className);
        // Keep the authored img so editing tools can round-trip to source
        svg.setAttribute('data-ws-original-img', img.outerHTML);
        img.replaceWith(svg);
    }

    function setAttributes(el, pairs) {
        pairs.forEach(function (pair) {
            el.setAttribute(pair[0], String(pair[1]));
        });
    }

    // Visible stand-in for an SVG that could not be inlined. It is an <svg>
    // carrying data-ws-original-img, so editors restore the authored <img>.
    function showSvgLoadError(img, src, detail) {
        const ns = 'http://www.w3.org/2000/svg';
        const shownSrc = src.length > 80 ? src.slice(0, 77) + '...' : src;
        const message = 'Could not load SVG: ' + shownSrc + ' (' + detail + ')';
        const width = Math.min(1200, Math.max(360, message.length * 10 + 40));
        const svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 ' + width + ' 64');
        svg.setAttribute('width', img.getAttribute('width') || String(width));
        svg.setAttribute('height', img.getAttribute('height') || '64');
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', message);
        const box = document.createElementNS(ns, 'rect');
        setAttributes(box, [['x', 1], ['y', 1], ['width', width - 2], ['height', 62], ['rx', 6], ['fill', '#FDECEA'],
            ['stroke', '#B03A2E'], ['stroke-width', 2], ['stroke-dasharray', '8 5']]);
        const text = document.createElementNS(ns, 'text');
        setAttributes(text, [['x', width / 2], ['y', 39], ['text-anchor', 'middle'], ['font-family', 'system-ui, sans-serif'],
            ['font-size', 20], ['fill', '#8A1F11']]);
        text.textContent = message;
        svg.appendChild(box);
        svg.appendChild(text);
        replaceInlineImg(img, svg);
        svg.classList.add('ws-svg-load-error');
        recordLoadError('svg', src, detail, svg);
    }

    function prefixSvgIds(svg, prefix) {
        const idMap = new Map();
        const elements = [svg].concat(Array.from(svg.querySelectorAll('*')));
        elements.forEach(function (el) {
            const oldId = el.getAttribute('id');
            if (!oldId) return;
            const newId = prefix + oldId;
            idMap.set(oldId, newId);
            el.setAttribute('id', newId);
        });
        if (idMap.size === 0) return;

        function escapeRegExp(value) {
            return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        }

        function rewriteReference(value) {
            let rewritten = value;
            idMap.forEach(function (newId, oldId) {
                if (rewritten === '#' + oldId) rewritten = '#' + newId;
                rewritten = rewritten.replace(
                    new RegExp('url\\(([\'"]?)#' + escapeRegExp(oldId) + '\\1\\)', 'g'),
                    function (_match, quote) {
                        return 'url(' + quote + '#' + newId + quote + ')';
                    }
                );
            });
            return rewritten;
        }

        function rewriteStyleReference(value) {
            let rewritten = rewriteReference(value);
            idMap.forEach(function (newId, oldId) {
                rewritten = rewritten.replace(
                    new RegExp('(^|[\\s,{>+~])#' + escapeRegExp(oldId) + '(?![-_a-zA-Z0-9])', 'g'),
                    function (match, prefix, offset, fullText) {
                        var hashOffset = offset + prefix.length;
                        var previous = fullText.slice(0, hashOffset).trimEnd().slice(-1);
                        if (previous === ':') return match;
                        return prefix + '#' + newId;
                    }
                );
            });
            return rewritten;
        }

        elements.forEach(function (el) {
            Array.from(el.attributes).forEach(function (attr) {
                const rewritten = rewriteReference(attr.value);
                if (rewritten !== attr.value) el.setAttribute(attr.name, rewritten);
            });
        });
        svg.querySelectorAll('style').forEach(function (styleEl) {
            styleEl.textContent = rewriteStyleReference(styleEl.textContent || '');
        });
    }

    /* ===== Deck Content: Stats and SVG Math ===== */
    // Filled in after SVGs are inlined and before init() (and again for any
    // slide an editor swaps in later):
    //  - data-stat="NAME" (HTML elements, SVG <text>/<tspan>) shows the value
    //    from the stats source declared on .ws-deck: data-stats-dir (one
    //    NAME.tex file per value) and/or data-stats-src (a JSON object).
    //    \stat{NAME} works inside math. Decks that declare no source are
    //    left exactly as authored.
    //  - $...$ in SVG <text> is typeset by KaTeX in an off-screen HTML line,
    //    then written back as native SVG: positioned <tspan>s in KaTeX's fonts,
    //    plus <rect>/<svg> for fraction rules and radicals. No foreignObject, so
    //    it survives CSS zoom (Presto), print, screenshots and SVG-to-PDF.
    // Editors must save authored content, never filled values: inlined SVGs
    // restore from data-ws-original-img; every other element changed here
    // (stat spans, inline <svg>) keeps its authored innerHTML in
    // data-ws-original-html, which Presto restores on save.

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const STAT_NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.:+-]*$/;
    const STAT_MACRO_RE = /\\stat\s*\{\s*([^{}]*?)\s*\}/g;
    const STAT_MISSING_COLOR = '#B03A2E';
    const statRaw = new Map();       // name -> raw file text, or null when missing
    const statProblems = new Map();  // name -> { src, detail }
    const statLoads = new Map();     // name -> Promise
    const reportedStats = new Set();
    const processedStatEls = new WeakSet();
    const preparedSvgRoots = new WeakSet();
    const processedMathTexts = new WeakSet();
    let statsJsonLoad = null;

    WS.stats = {}; // name -> value as shown (before data-stat-round)

    function statsSource() {
        const deck = document.querySelector('.ws-deck');
        if (!deck) return null;
        const dir = (deck.getAttribute('data-stats-dir') || '').trim();
        const src = (deck.getAttribute('data-stats-src') || '').trim();
        return dir || src ? { dir: dir, src: src } : null;
    }

    function statNameOf(el) {
        return (el.getAttribute('data-stat') || '').trim();
    }

    function statNamesIn(root) {
        const names = new Set();
        root.querySelectorAll('[data-stat]').forEach(el => names.add(statNameOf(el)));
        const text = root.textContent || '';
        if (text.indexOf('\\stat') !== -1) {
            for (const m of text.matchAll(STAT_MACRO_RE)) names.add(m[1]);
        }
        return Array.from(names);
    }

    function loadStatsJson(src) {
        if (!statsJsonLoad) {
            statsJsonLoad = (async () => {
                try {
                    const resp = await fetch(src);
                    if (!resp.ok) return { error: resp.status + ' ' + resp.statusText };
                    const data = JSON.parse(await resp.text());
                    if (!data || typeof data !== 'object' || Array.isArray(data)) return { error: 'not a JSON object' };
                    return { data: data };
                } catch (e) {
                    return { error: (e && e.message) || 'network error' };
                }
            })();
        }
        return statsJsonLoad;
    }

    async function fetchStat(name, source) {
        if (!STAT_NAME_RE.test(name)) return { src: name || '(empty name)', detail: 'invalid stat name' };
        if (source.src) {
            const json = await loadStatsJson(source.src);
            if (json.data && Object.prototype.hasOwnProperty.call(json.data, name)) {
                const value = json.data[name];
                if (typeof value !== 'string' && typeof value !== 'number') {
                    return { src: source.src + '#' + name, detail: 'value is not a string or number' };
                }
                return { raw: String(value), src: source.src + '#' + name };
            }
            if (!source.dir) {
                return { src: source.src + '#' + name, detail: json.error ? 'stats file failed to load: ' + json.error : 'not in stats file' };
            }
        }
        const url = source.dir.replace(/\/+$/, '') + '/' + encodeURIComponent(name) + '.tex';
        try {
            const resp = await fetch(url);
            if (!resp.ok) return { src: url, detail: resp.status + ' ' + resp.statusText };
            return { raw: await resp.text(), src: url };
        } catch (e) {
            return { src: url, detail: (e && e.message) || 'network error' };
        }
    }

    function loadStat(name, source) {
        if (!statLoads.has(name)) {
            statLoads.set(name, fetchStat(name, source).then(result => {
                if (result.raw !== undefined && statTexToText(result.raw) === '') {
                    result = { src: result.src, detail: 'empty value' };
                }
                if (result.raw !== undefined) {
                    statRaw.set(name, result.raw);
                    WS.stats[name] = statTexToText(result.raw);
                } else {
                    statRaw.set(name, null);
                    statProblems.set(name, result);
                }
            }));
        }
        return statLoads.get(name);
    }

    function loadStatValues(names) {
        const source = statsSource();
        if (!source || names.length === 0) return Promise.resolve();
        return boundedWait(Promise.all(names.map(name => loadStat(name, source))), READY_WAIT_MS);
    }

    // Drop TeX comments: everything from an unescaped % to the end of a line
    // (stats files often end in % to swallow the newline).
    function stripTexComment(raw) {
        return String(raw).split(/\r?\n/).map(line => line.replace(/(^|[^\\])%.*$/, '$1')).join(' ');
    }

    // A stats file's TeX as slide text: "17.5\%%" -> "17.5%", "5{,}000" ->
    // "5,000", "$<$ 0.001" -> "< 0.001", "-0.06" -> "−0.06".
    function statTexToText(raw) {
        return stripTexComment(raw)
            .replace(/\\textless\b\s*/g, '<').replace(/\\textgreater\b\s*/g, '>')
            .replace(/\\(?:times)\b\s*/g, '×').replace(/\\pm\b\s*/g, '±')
            .replace(/\\(?:leq?)\b\s*/g, '≤').replace(/\\(?:geq?)\b\s*/g, '≥')
            .replace(/\\(?:,|thinspace\b\s*)/g, ' ').replace(/(^|[^\\])~/g, '$1 ')
            .replace(/(^|[^\\])\$/g, '$1').replace(/(^|[^\\])\$/g, '$1')
            .replace(/\\([%$&#_{}])/g, '$1')
            .replace(/\{,\}/g, ',').replace(/\{\.\}/g, '.').replace(/[{}]/g, '')
            .replace(/---/g, '—').replace(/--/g, '–')
            .replace(/(^|[\s(\[,;:=<>/])-(?=\.?\d)/g, '$1−')
            .replace(/\s+/g, ' ').trim();
    }

    // data-stat-round="N": every number in the value to N decimals, keeping
    // thousands separators when the source had them.
    function roundStatText(text, rawDigits) {
        const digits = parseStrictInt(rawDigits);
        if (digits === null || digits < 0 || digits > 20) return text;
        return text.replace(/(\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE]([+\-−]?\d+))?/g, (token, num, exp) => {
            const value = Number(num.replace(/,/g, '') + (exp ? 'e' + exp.replace('−', '-') : ''));
            if (!Number.isFinite(value)) return token;
            const parts = value.toFixed(digits).split('.');
            if (num.indexOf(',') !== -1) parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
            return parts.join('.');
        });
    }

    function statText(name, el) {
        const raw = statRaw.get(name);
        if (raw === null || raw === undefined) return null;
        const text = statTexToText(raw);
        const digits = el && el.getAttribute('data-stat-round');
        return digits ? roundStatText(text, digits) : text;
    }

    function reportMissingStat(name, el) {
        if (reportedStats.has(name)) return;
        reportedStats.add(name);
        const problem = statProblems.get(name) || { src: name, detail: 'timed out' };
        recordLoadError('stat', problem.src, 'stat "' + name + '": ' + problem.detail, el);
    }

    function escapeTexText(s) {
        return String(s).replace(/[\\{}$&#%_^~]/g, ch => '\\' + ch);
    }

    // A stat as TeX for \stat{NAME} inside math.
    function statMathTex(name, el) {
        const raw = statRaw.get(name);
        if (raw === null || raw === undefined) {
            reportMissingStat(name, el);
            return '\\textcolor{' + STAT_MISSING_COLOR + '}{\\textbf{[' + escapeTexText(name) + '?]}}';
        }
        const tex = stripTexComment(raw).replace(/(^|[^\\])\$/g, '$1').replace(/(^|[^\\])\$/g, '$1').trim();
        if (/[A-Za-z]/.test(tex.replace(/\\[A-Za-z]+/g, ''))) return '\\text{' + escapeTexText(statTexToText(raw)) + '}';
        return '{' + tex + '}';
    }

    function substituteStatMacros(tex, el) {
        if (!statsSource() || tex.indexOf('\\stat') === -1) return tex;
        return tex.replace(STAT_MACRO_RE, (_m, name) => statMathTex(name, el));
    }

    function outermostSvg(el) {
        let svg = el.closest('svg');
        while (svg && svg.parentElement && svg.parentElement.closest('svg')) svg = svg.parentElement.closest('svg');
        return svg;
    }

    // Before the engine changes an SVG: inlined SVGs already restore from
    // data-ws-original-img; an inline <svg> written in the HTML keeps its
    // authored innerHTML (after undoing the deck's KaTeX auto-render, which
    // turns $...$ in SVG text into HTML that SVG cannot show).
    function prepareSvgRoot(svg) {
        if (!svg || preparedSvgRoots.has(svg)) return;
        preparedSvgRoots.add(svg);
        if (svg.hasAttribute('data-ws-original-img')) return;
        restoreAutoRenderedSvgMath(svg);
        if (!svg.closest('[data-ws-original-html]')) svg.setAttribute('data-ws-original-html', svg.innerHTML);
    }

    function katexSourceOf(node) {
        const display = node.matches('.katex-display') || !!node.querySelector('.katex-display');
        const k = node.matches('.katex') ? node : node.querySelector('.katex');
        const annotation = k && k.querySelector('annotation[encoding="application/x-tex"]');
        const delim = display ? '$$' : '$';
        if (annotation) return delim + annotation.textContent + delim;
        const err = node.matches('.katex-error') ? node : node.querySelector('.katex-error');
        return err ? '$' + err.textContent + '$' : null;
    }

    function restoreAutoRenderedSvgMath(svg) {
        svg.querySelectorAll('text').forEach(text => {
            let changed = false;
            text.querySelectorAll('*').forEach(node => {
                if (node.namespaceURI === SVG_NS || !node.parentNode || node.parentNode.namespaceURI !== SVG_NS) return;
                const tex = katexSourceOf(node);
                if (tex === null) return;
                node.replaceWith(document.createTextNode(tex));
                changed = true;
            });
            if (changed) text.normalize();
        });
    }

    function fillStatElement(el) {
        if (processedStatEls.has(el)) return;
        processedStatEls.add(el);
        const name = statNameOf(el);
        const svg = el instanceof SVGElement ? outermostSvg(el) : null;
        if (svg) prepareSvgRoot(svg);
        else if (!el.closest('[data-ws-original-html]')) el.setAttribute('data-ws-original-html', el.innerHTML);
        const text = statText(name, el);
        if (text !== null) {
            el.textContent = text;
            return;
        }
        reportMissingStat(name, el);
        const label = '[' + (name || 'stat') + '?]';
        if (svg) {
            el.textContent = label;
            el.style.fill = STAT_MISSING_COLOR;
            el.style.fontWeight = '700';
            return;
        }
        const mark = document.createElement('span');
        mark.className = 'ws-stat-missing';
        mark.title = 'Missing stat: ' + name;
        mark.style.cssText = 'color:' + STAT_MISSING_COLOR + ';font-weight:700;outline:2px dashed ' + STAT_MISSING_COLOR + ';outline-offset:1px;';
        mark.textContent = label;
        el.replaceChildren(mark);
    }

    function fillStatsIn(root) {
        if (!statsSource()) return;
        root.querySelectorAll('[data-stat]').forEach(fillStatElement);
    }

    // \stat{NAME} inside $...$ of loaded table HTML, before its math renders
    // (the table container restores its authored HTML on save).
    function substituteTableStatMacros(container) {
        if (!statsSource() || (container.textContent || '').indexOf('\\stat') === -1) return;
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        const nodes = [];
        while (walker.nextNode()) if (walker.currentNode.data.indexOf('\\stat') !== -1) nodes.push(walker.currentNode);
        nodes.forEach(node => {
            const parts = splitMath(node.data);
            if (!parts) return;
            node.data = parts.map(part => {
                if (part.tex === undefined) return part.raw;
                const delim = part.display ? '$$' : '$';
                return delim + substituteStatMacros(part.tex, container) + delim;
            }).join('');
        });
    }

    // Split text at $...$ / $$...$$ (auto-render's delimiters). Text parts
    // carry the unescaped text (\$ -> $) and their raw source. Null when
    // there is neither math nor an escaped dollar.
    function splitMath(s) {
        if (s.indexOf('$') === -1) return null;
        const parts = [];
        let text = '';
        let raw = '';
        let found = false;
        let i = 0;
        const flush = () => {
            if (raw) parts.push({ text: text, raw: raw });
            text = '';
            raw = '';
        };
        while (i < s.length) {
            const c = s[i];
            if (c === '\\' && s[i + 1] === '$') {
                text += '$';
                raw += '\\$';
                i += 2;
                found = true;
                continue;
            }
            if (c === '$') {
                const display = s[i + 1] === '$';
                const open = display ? 2 : 1;
                const close = findMathEnd(s, i + open, display);
                if (close !== -1 && close > i + open) {
                    flush();
                    parts.push({ tex: s.slice(i + open, close), display: display, raw: s.slice(i, close + open) });
                    i = close + open;
                    found = true;
                    continue;
                }
            }
            text += c;
            raw += c;
            i++;
        }
        flush();
        return found ? parts : null;
    }

    function findMathEnd(s, from, display) {
        for (let j = from; j < s.length; j++) {
            if (s[j] === '\\') { j++; continue; }
            if (s[j] === '$' && (!display || s[j + 1] === '$')) return j;
        }
        return -1;
    }

    /* ===== Offline KaTeX ===== */
    // Decks link KaTeX from the CDN. When that fails (no network, a captive
    // portal, a CDN outage) the engine loads its vendored copy
    // (engine/vendor/katex/, the same 0.16.21 files) and renders with the
    // deck's own auto-render options. Deferred deck scripts have all run or
    // failed by DOMContentLoaded, when start() calls this, so a missing
    // window.katex means the CDN script failed. A failed stylesheet is
    // repaired in place, keeping its cascade position (the theme's .katex
    // font-size must still come after KaTeX's own).

    const KATEX_JS_RE = /(^|\/)katex(\.min)?\.js([?#]|$)/i;
    const KATEX_CSS_RE = /(^|\/)katex(\.min)?\.css([?#]|$)/i;
    const AUTO_RENDER_RE = /auto-render(\.min)?\.js([?#]|$)/i;
    const DEFAULT_AUTO_RENDER_OPTIONS = {
        delimiters: [{ left: '$$', right: '$$', display: true }, { left: '$', right: '$', display: false }],
        throwOnError: false
    };

    function engineScriptURL() {
        if (ENGINE_SCRIPT_SRC) return ENGINE_SCRIPT_SRC;
        const own = Array.from(document.querySelectorAll('script[src]')).find(s => /webslides(\.min)?\.js([?#]|$)/.test(s.src));
        return own ? own.src : null;
    }

    function vendoredKatexURL(file) {
        const base = engineScriptURL();
        try {
            return base ? new URL('vendor/katex/' + file, base).href : null;
        } catch (e) {
            return null;
        }
    }

    function katexReady() {
        return !!(window.katex && typeof window.katex.render === 'function');
    }

    function katexCssApplied() {
        const probe = document.createElement('span');
        probe.className = 'katex';
        probe.style.cssText = 'position:absolute;left:-10000px;top:0;visibility:hidden;';
        document.body.appendChild(probe);
        const applied = /KaTeX/.test(getComputedStyle(probe).fontFamily);
        probe.remove();
        return applied;
    }

    function loadFallbackScript(url) {
        return new Promise(resolve => {
            const s = document.createElement('script');
            s.src = url;
            s.async = false;
            s.setAttribute('data-ws-katex-fallback', '');
            s.onload = () => resolve(true);
            s.onerror = () => resolve(false);
            document.head.appendChild(s);
        });
    }

    function repairKatexStylesheet(link) {
        const url = vendoredKatexURL('katex.min.css');
        if (!url) return Promise.resolve(false);
        return boundedWait(new Promise(resolve => {
            link.addEventListener('load', () => resolve(true), { once: true });
            link.addEventListener('error', () => resolve(false), { once: true });
            link.setAttribute('data-ws-katex-fallback', link.getAttribute('href') || '');
            link.href = url;
        }), READY_WAIT_MS);
    }

    async function ensureKatex() {
        const scripts = Array.from(document.querySelectorAll('script[src]'));
        const katexScript = scripts.find(s => KATEX_JS_RE.test(s.getAttribute('src')));
        const autoRender = scripts.find(s => AUTO_RENDER_RE.test(s.getAttribute('src')));
        const cssLink = Array.from(document.querySelectorAll('link[rel~="stylesheet"][href]'))
            .find(l => KATEX_CSS_RE.test(l.getAttribute('href')));
        if (!katexScript && !autoRender && !cssLink) return; // the deck does not use KaTeX
        // async scripts may still be on their way
        if (!katexReady() && [katexScript, autoRender].some(s => s && s.async)) {
            await boundedWait(new Promise(resolve => {
                (function poll() { if (katexReady()) resolve(); else setTimeout(poll, 50); })();
            }), 1500);
        }
        const tasks = [];
        if (cssLink && !katexCssApplied()) {
            console.warn('WebSlides: KaTeX stylesheet did not load; using the engine copy');
            tasks.push(repairKatexStylesheet(cssLink));
        }
        // An auto-render that loaded without KaTeX captured an undefined
        // katex, so it is reloaded along with KaTeX.
        const needKatex = !!(katexScript || autoRender) && !katexReady();
        const needAutoRender = !!autoRender && (needKatex || typeof window.renderMathInElement !== 'function');
        if (needKatex || needAutoRender) {
            console.warn('WebSlides: KaTeX did not load; using the engine copy');
            tasks.push((async () => {
                const files = (needKatex ? ['katex.min.js'] : []).concat(needAutoRender ? ['contrib/auto-render.min.js'] : []);
                for (const file of files) {
                    const url = vendoredKatexURL(file);
                    if (!url || !(await boundedWait(loadFallbackScript(url), READY_WAIT_MS))) {
                        console.warn('WebSlides: the engine copy of KaTeX did not load either (' + file + ')');
                        return;
                    }
                }
                if (autoRender) renderDeckMathWithFallback(autoRender);
            })());
        }
        await Promise.all(tasks);
    }

    // The deck's auto-render call lives in its script's onload attribute;
    // run that handler (its options are the deck's), else render the body
    // with window.__wsKatexOptions or the usual $$ / $ delimiters.
    function renderDeckMathWithFallback(autoRender) {
        try {
            if (autoRender && typeof autoRender.onload === 'function') {
                autoRender.onload.call(autoRender, new Event('load'));
            } else if (typeof window.renderMathInElement === 'function') {
                window.renderMathInElement(document.body, window.__wsKatexOptions || DEFAULT_AUTO_RENDER_OPTIONS);
            }
        } catch (err) {
            console.warn('WebSlides: rendering maths with the engine copy of KaTeX failed:', err);
        }
    }

    function waitForKatex() {
        return new Promise(resolve => {
            const started = Date.now();
            (function poll() {
                if (window.katex && typeof window.katex.render === 'function') {
                    resolve(window.katex);
                } else if (!document.querySelector('script[src*="katex"]') || Date.now() - started >= READY_WAIT_MS) {
                    resolve(null);
                } else {
                    setTimeout(poll, 50);
                }
            })();
        });
    }

    function katexOptions(extra) {
        const deckOptions = window.__wsKatexOptions || {};
        return Object.assign({ throwOnError: false, strict: 'ignore', macros: deckOptions.macros || {} }, extra);
    }

    // \stat{NAME} in the deck's own HTML math. The deck's auto-render ran
    // before the engine and could not expand \stat: it left the formula as
    // text (default options) or a .katex-error span (throwOnError: false).
    // The re-rendered formula keeps the authored TeX in its annotation (what
    // Presto saves) and the substituted TeX in data-ws-tex (for exporters).
    function renderStatMathInHtml(root, katex) {
        if ((root.textContent || '').indexOf('\\stat') === -1) return;
        const render = (authored, display, near) => {
            const holder = document.createElement('span');
            const filled = substituteStatMacros(authored, near);
            katex.render(filled, holder, katexOptions({ displayMode: display, output: 'htmlAndMathml' }));
            const k = holder.querySelector('.katex');
            const annotation = k && k.querySelector('annotation[encoding="application/x-tex"]');
            if (annotation) annotation.textContent = authored;
            if (k) k.setAttribute('data-ws-tex', filled);
            return holder;
        };
        root.querySelectorAll('.katex-error').forEach(err => {
            if (err.closest('svg')) return;
            const tex = err.textContent || '';
            if (!/\\stat\s*\{/.test(tex)) return;
            err.replaceWith(...render(tex, false, err).childNodes);
        });
        const skip = 'svg, script, style, textarea, pre, code, .katex, [data-no-math]';
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        const nodes = [];
        while (walker.nextNode()) {
            const node = walker.currentNode;
            if (node.data.indexOf('\\stat') === -1 || !node.parentElement || node.parentElement.closest(skip)) continue;
            nodes.push(node);
        }
        nodes.forEach(node => {
            const parts = splitMath(node.data);
            if (!parts || !parts.some(p => p.tex !== undefined && /\\stat\s*\{/.test(p.tex))) return;
            const frag = document.createDocumentFragment();
            parts.forEach(part => {
                if (part.tex !== undefined && /\\stat\s*\{/.test(part.tex)) frag.append(...render(part.tex, part.display, node.parentElement).childNodes);
                else frag.appendChild(document.createTextNode(part.raw));
            });
            node.replaceWith(frag);
        });
    }

    /* --- $...$ in SVG text --- */

    // Attributes that must stay on whatever wraps a <text> whose math needs
    // non-text parts (rules, radicals), so reveals and transforms cover both.
    const MATH_WRAP_ATTRS = ['transform', 'opacity', 'data-step', 'data-class', 'data-fade-after',
        'data-morph', 'data-focus', 'data-highlight', 'data-dim'];
    const MATH_WRAP_STATE_CLASSES = ['ws-visible', 'ws-highlighted', 'ws-dimmed', 'ws-faded'];
    const POSITION_ATTRS = ['dx', 'dy', 'rotate', 'textLength', 'lengthAdjust'];

    function svgTextMightHaveMath(text) {
        return (text.textContent || '').indexOf('$') !== -1 || !!text.querySelector('.katex, .katex-error');
    }

    function isPositionedTspan(el) {
        return el.localName === 'tspan' && (el.hasAttribute('x') || el.hasAttribute('y'));
    }

    // Nearest <text> or <tspan x|y> above node: one independently placed line.
    function mathUnitFor(node, text) {
        for (let el = node.parentNode; el && el !== text; el = el.parentNode) {
            if (isPositionedTspan(el)) return el;
        }
        return text;
    }

    // Text nodes and elements of a unit, leaving out nested positioned tspans
    // (they start lines of their own).
    function mathUnitContent(unit, text) {
        const nodes = [];
        const elements = [unit];
        const walker = document.createTreeWalker(unit, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
            acceptNode: n => (n !== unit && n.nodeType === 1 && isPositionedTspan(n)) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
        });
        while (walker.nextNode()) {
            const n = walker.currentNode;
            if (n.nodeType === 3) {
                if (mathUnitFor(n, text) === unit) nodes.push(n);
            } else if (n.namespaceURI === SVG_NS) {
                elements.push(n);
            }
        }
        return { nodes: nodes, elements: elements };
    }

    function collectMathUnits(text, units) {
        const walker = document.createTreeWalker(text, NodeFilter.SHOW_TEXT);
        const unitEls = [];
        while (walker.nextNode()) {
            const node = walker.currentNode;
            const parts = splitMath(node.data);
            if (!parts) continue;
            if (parts.every(p => p.tex === undefined)) {
                node.data = parts.map(p => p.text).join(''); // only escaped dollars
                continue;
            }
            const unit = mathUnitFor(node, text);
            if (unitEls.indexOf(unit) === -1) unitEls.push(unit);
        }
        unitEls.forEach(unit => {
            const content = mathUnitContent(unit, text);
            units.push({
                text: text,
                unit: unit,
                elements: content.elements,
                entries: content.nodes.map(node => ({ node: node, parent: node.parentElement, parts: splitMath(node.data) || [{ text: node.data, raw: node.data }] }))
            });
        });
    }

    function mathMarker() {
        const m = document.createElement('span');
        m.className = 'ws-math-marker';
        m.style.cssText = 'display:inline-block!important;width:0!important;height:0!important;margin:0!important;padding:0!important;border:0!important;vertical-align:baseline!important;position:static!important;';
        return m;
    }

    function cssColorOfFill(fill) {
        return /^(rgb|hsl|#)/i.test(fill || '') ? fill : '';
    }

    // One off-screen HTML line per unit, in the unit's fonts, with KaTeX
    // rendering each $...$.
    function buildMathMirror(u, host, katex) {
        const line = document.createElement('div');
        line.style.cssText = 'white-space:nowrap;line-height:normal;padding:0;margin:0 0 40px 0;';
        u.start = mathMarker();
        line.appendChild(u.start);
        const last = u.entries.length - 1;
        u.entries.forEach((entry, idx) => {
            const cs = getComputedStyle(entry.parent);
            const span = document.createElement('span');
            const color = cssColorOfFill(cs.fill);
            span.style.cssText = 'font-family:' + cs.fontFamily + ';font-size:' + cs.fontSize + ';font-style:' + cs.fontStyle
                + ';font-weight:' + cs.fontWeight + ';letter-spacing:' + cs.letterSpacing + ';word-spacing:' + cs.wordSpacing
                + (color ? ';color:' + color : '');
            entry.parts.forEach((part, j) => {
                if (part.tex === undefined) {
                    let t = part.text;
                    if (idx === 0 && j === 0) t = t.replace(/^\s+/, '');
                    if (idx === last && j === entry.parts.length - 1) t = t.replace(/\s+$/, '');
                    if (t) span.appendChild(document.createTextNode(t));
                    return;
                }
                const holder = document.createElement('span');
                const tex = substituteStatMacros(part.tex, u.text);
                try {
                    katex.render(part.display ? '\\displaystyle ' + tex : tex, holder, katexOptions({ displayMode: false, output: 'html' }));
                } catch (e) {
                    holder.textContent = part.raw;
                    recordLoadError('math', part.tex, (e && e.message) || 'KaTeX error', u.text);
                }
                const err = holder.querySelector('.katex-error');
                if (err) recordLoadError('math', part.tex, err.getAttribute('title') || 'KaTeX error', u.text);
                span.appendChild(holder);
            });
            entry.mirror = span;
            line.appendChild(span);
        });
        u.end = mathMarker();
        line.appendChild(u.end);
        host.appendChild(line);
    }

    // WebKit can run deferred scripts before katex.min.css has applied; the
    // layout (and KaTeX's @font-face rules) are only right once it has.
    function waitForKatexCss(host) {
        if (!document.querySelector('link[href*="katex"]')) return Promise.resolve();
        const probe = document.createElement('span');
        probe.className = 'katex';
        host.appendChild(probe);
        return new Promise(resolve => {
            const started = Date.now();
            (function poll() {
                if (/KaTeX/.test(getComputedStyle(probe).fontFamily) || Date.now() - started >= READY_WAIT_MS) {
                    probe.remove();
                    resolve();
                } else {
                    setTimeout(poll, 30);
                }
            })();
        });
    }

    function waitForMirrorFonts(host) {
        const fonts = new Map();
        const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const cs = getComputedStyle(walker.currentNode.parentElement);
            const font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
            fonts.set(font, (fonts.get(font) || '') + walker.currentNode.data);
        }
        if (!document.fonts || typeof document.fonts.load !== 'function') return Promise.resolve();
        return boundedWait(Promise.all(Array.from(fonts).map(([font, text]) => {
            return document.fonts.load(font, text).catch(() => null);
        })), READY_WAIT_MS);
    }

    // SVG side: where the unit's line starts (forcing text-anchor:start) and
    // where its alphabetic baseline sits under its dominant-baseline. The
    // start position is read with dominant-baseline forced to alphabetic:
    // the system WebKit (and Playwright WebKit 26.0) already shifts
    // getStartPositionOfChar by the dominant baseline, Chromium and WebKit
    // 26.4 do not, so the shift comes only from the two bounding-box probes.
    function measureSvgUnits(units) {
        units.forEach(u => {
            const cs = getComputedStyle(u.unit);
            u.anchor = cs.textAnchor;
            u.dominantBaseline = cs.dominantBaseline;
            u.probeCss = 'font-family:' + cs.fontFamily + ';font-size:' + cs.fontSize + ';font-style:' + cs.fontStyle
                + ';font-weight:' + cs.fontWeight + ';text-anchor:start;';
        });
        units.forEach(u => {
            u.savedAnchors = u.elements.map(el => el.style.getPropertyValue('text-anchor'));
            u.elements.forEach(el => el.style.setProperty('text-anchor', 'start'));
            if (u.dominantBaseline && u.dominantBaseline !== 'auto' && u.dominantBaseline !== 'alphabetic') {
                u.probes = [u.dominantBaseline, 'alphabetic'].map(baseline => {
                    const probe = document.createElementNS(SVG_NS, 'text');
                    probe.setAttribute('x', '0');
                    probe.setAttribute('y', '0');
                    probe.setAttribute('style', u.probeCss + 'dominant-baseline:' + baseline);
                    probe.textContent = 'x';
                    u.text.parentNode.insertBefore(probe, u.text);
                    return probe;
                });
            }
        });
        units.forEach(u => {
            const savedBaselines = u.probes ? u.elements.map(el => el.style.getPropertyValue('dominant-baseline')) : null;
            if (u.probes) u.elements.forEach(el => el.style.setProperty('dominant-baseline', 'alphabetic'));
            try {
                const p = u.unit.getStartPositionOfChar(0);
                u.originX = p.x;
                u.originY = p.y;
                if (u.probes) u.originY += u.probes[0].getBBox().y - u.probes[1].getBBox().y;
            } catch (e) {
                u.failed = true; // not rendered (defs, display:none): leave as typed
            }
            if (u.probes) {
                u.elements.forEach((el, i) => {
                    if (savedBaselines[i]) el.style.setProperty('dominant-baseline', savedBaselines[i]);
                    else el.style.removeProperty('dominant-baseline');
                });
                u.probes.forEach(p => p.remove());
            }
            if (u.failed) {
                u.elements.forEach((el, i) => {
                    if (u.savedAnchors[i]) el.style.setProperty('text-anchor', u.savedAnchors[i]);
                    else el.style.removeProperty('text-anchor');
                });
            }
        });
    }

    function markMirror(u) {
        u.entries.forEach(entry => {
            entry.runs = [];
            const walker = document.createTreeWalker(entry.mirror, NodeFilter.SHOW_TEXT);
            const textNodes = [];
            while (walker.nextNode()) textNodes.push(walker.currentNode);
            textNodes.forEach(node => {
                const lead = node.data.search(/[^\s​]/);
                if (lead === -1) return; // spaces and KaTeX's zero-width struts
                const marker = mathMarker();
                node.parentNode.insertBefore(marker, node);
                entry.runs.push({ node: node, lead: lead, marker: marker });
            });
        });
    }

    function isTransparent(color) {
        return !color || color === 'transparent' || /rgba\([^)]*,\s*0\)$/.test(color);
    }

    // HTML layout -> SVG user units, relative to the line's start and baseline.
    function readMirror(u, k) {
        const s = u.start.getBoundingClientRect();
        const ux = x => (x - s.left) / k;
        const uy = y => (y - s.bottom) / k;
        u.width = (u.end.getBoundingClientRect().left - s.left) / k;
        u.parts = [];
        u.runs = [];
        u.lastBaseline = 0;
        const partEls = [];
        const addPart = (part, el) => {
            u.parts.push(part);
            partEls.push(el);
        };
        u.entries.forEach(entry => {
            const baseColor = getComputedStyle(entry.mirror).color;
            entry.runs.forEach(run => {
                const range = document.createRange();
                range.setStart(run.node, run.lead);
                range.setEnd(run.node, run.node.length);
                const r = range.getBoundingClientRect();
                run.x = ux(r.left);
                run.advance = r.width / k;
                run.y = uy(run.marker.getBoundingClientRect().bottom);
                run.text = run.node.data.slice(run.lead).replace(/\s+/g, ' ');
                const parent = run.node.parentElement;
                if (parent !== entry.mirror) {
                    const cs = getComputedStyle(parent);
                    run.style = 'font-family:' + cs.fontFamily + ';font-size:' + cs.fontSize + ';font-style:' + cs.fontStyle
                        + ';font-weight:' + cs.fontWeight + (cs.color !== baseColor ? ';fill:' + cs.color : '');
                }
                u.lastBaseline = run.y;
                u.runs.push(run);
            });
            entry.mirror.querySelectorAll('*').forEach(el => {
                if (el.classList.contains('ws-math-marker')) return;
                if (el.namespaceURI === SVG_NS) {
                    if (el.localName === 'svg' && el.parentNode.namespaceURI !== SVG_NS) addPart(svgMathPart(el, entry.mirror, ux, uy, k), el);
                    return;
                }
                const cs = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                const box = { x: ux(r.left), y: uy(r.top), w: r.width / k, h: r.height / k };
                if (!isTransparent(cs.backgroundColor) && box.w > 0 && box.h > 0) {
                    addPart({ rect: box, fill: cs.backgroundColor }, el);
                }
                ['Top', 'Right', 'Bottom', 'Left'].forEach(side => {
                    const width = parseFloat(cs['border' + side + 'Width']) || 0;
                    const style = cs['border' + side + 'Style'];
                    const color = cs['border' + side + 'Color'];
                    if (width <= 0 || style === 'none' || style === 'hidden' || isTransparent(color)) return;
                    const rect = side === 'Top' ? { x: box.x, y: box.y, w: box.w, h: width }
                        : side === 'Bottom' ? { x: box.x, y: box.y + box.h - width, w: box.w, h: width }
                        : side === 'Left' ? { x: box.x, y: box.y, w: width, h: box.h }
                        : { x: box.x + box.w - width, y: box.y, w: width, h: box.h };
                    addPart({ rect: rect, fill: color }, el);
                });
            });
        });
        // Anchor each rule or radical to the glyph run just before it in
        // KaTeX's DOM (the denominator for a fraction bar, the radicand for a
        // root), else the first run after it.
        u.parts.forEach((part, i) => {
            const el = partEls[i];
            const before = u.runs.filter(run => el.compareDocumentPosition(run.marker) & Node.DOCUMENT_POSITION_PRECEDING);
            part.run = before.length ? before[before.length - 1] : u.runs[0] || null;
        });
    }

    // KaTeX draws radicals, wide accents and stretchy arrows as inline SVG,
    // clipped by an overflow:hidden parent; keep the visible window.
    function svgMathPart(svg, mirror, ux, uy, k) {
        const r = svg.getBoundingClientRect();
        let clip = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
        for (let el = svg.parentElement; el && el !== mirror; el = el.parentElement) {
            const cs = getComputedStyle(el);
            if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
                const c = el.getBoundingClientRect();
                clip = { left: Math.max(clip.left, c.left), top: Math.max(clip.top, c.top),
                    right: Math.min(clip.right, c.right), bottom: Math.min(clip.bottom, c.bottom) };
                break;
            }
        }
        const outer = document.createElementNS(SVG_NS, 'svg');
        const w = Math.max(0, (clip.right - clip.left) / k);
        const h = Math.max(0, (clip.bottom - clip.top) / k);
        setAttributes(outer, [['x', roundUnits(ux(clip.left))], ['y', roundUnits(uy(clip.top))], ['width', roundUnits(w)],
            ['height', roundUnits(h)], ['viewBox', '0 0 ' + roundUnits(w) + ' ' + roundUnits(h)], ['overflow', 'hidden']]);
        const inner = svg.cloneNode(true);
        inner.removeAttribute('style');
        setAttributes(inner, [['x', roundUnits((r.left - clip.left) / k)], ['y', roundUnits((r.top - clip.top) / k)],
            ['width', roundUnits(r.width / k)], ['height', roundUnits(r.height / k)]]);
        const originals = [svg].concat(Array.from(svg.querySelectorAll('*')));
        const copies = [inner].concat(Array.from(inner.querySelectorAll('*')));
        originals.forEach((el, i) => {
            const cs = getComputedStyle(el);
            copies[i].setAttribute('style', 'fill:' + cs.fill + ';stroke:' + cs.stroke + ';stroke-width:' + cs.strokeWidth);
        });
        outer.appendChild(inner);
        return { svg: outer };
    }

    function roundUnits(v) {
        return String(Math.round(v * 100) / 100);
    }

    function wrapMathText(text) {
        const parent = text.parentNode;
        if (parent && parent.namespaceURI === SVG_NS && parent.classList.contains('ws-svg-math')) return parent;
        const g = document.createElementNS(SVG_NS, 'g');
        g.setAttribute('class', 'ws-svg-math');
        MATH_WRAP_ATTRS.forEach(name => {
            if (!text.hasAttribute(name)) return;
            g.setAttribute(name, text.getAttribute(name));
            text.removeAttribute(name);
        });
        const dataClass = g.getAttribute('data-class') || '';
        const sep = dataClass.lastIndexOf(':');
        const token = sep > 0 ? dataClass.slice(0, sep).trim() : '';
        MATH_WRAP_STATE_CLASSES.concat(token ? [token] : []).forEach(cls => {
            if (!text.classList.contains(cls)) return;
            g.classList.add(cls);
            text.classList.remove(cls);
        });
        parent.insertBefore(g, text);
        g.appendChild(text);
        return g;
    }

    function anchorShift(anchor, width) {
        return anchor === 'middle' ? width / 2 : anchor === 'end' ? width : 0;
    }

    // The first run is placed absolutely; later runs by dx from where the
    // previous one ends, so they follow the text as the browser actually sets
    // it (WebKit's SVG text runs slightly wider than the same HTML text).
    function writeMathRuns(u) {
        const x0 = u.originX - anchorShift(u.anchor, u.width);
        const y0 = u.originY;
        u.x0 = x0;
        u.elements.forEach(el => POSITION_ATTRS.forEach(name => el.removeAttribute(name)));
        u.unit.setAttribute('x', roundUnits(x0));
        u.unit.setAttribute('y', roundUnits(y0));
        u.unit.style.setProperty('text-anchor', 'start');
        u.unit.style.setProperty('dominant-baseline', 'alphabetic');
        let end = null;
        u.entries.forEach(entry => {
            const frag = document.createDocumentFragment();
            entry.runs.forEach(run => {
                const tspan = document.createElementNS(SVG_NS, 'tspan');
                if (end === null) tspan.setAttribute('x', roundUnits(x0 + run.x));
                else if (Math.abs(run.x - end) > 0.005) tspan.setAttribute('dx', roundUnits(run.x - end));
                end = run.x + run.advance;
                tspan.setAttribute('y', roundUnits(y0 + run.y));
                if (run.style) tspan.setAttribute('style', run.style);
                tspan.textContent = run.text;
                run.tspan = tspan;
                frag.appendChild(tspan);
            });
            entry.node.replaceWith(frag);
        });
        // Text after this line (outside the unit) continues from the baseline.
        if (Math.abs(u.lastBaseline) > 0.01) {
            const reset = document.createElementNS(SVG_NS, 'tspan');
            reset.setAttribute('y', roundUnits(y0));
            reset.textContent = '​';
            u.unit.appendChild(reset);
        }
    }

    function runExtent(run) {
        const n = run.tspan.getNumberOfChars();
        return n > 0 ? { start: run.tspan.getStartPositionOfChar(0).x, end: run.tspan.getEndPositionOfChar(n - 1).x } : null;
    }

    // Re-centre middle/end anchored lines on the width the browser really set.
    function fitMathUnit(u) {
        if (u.anchor !== 'middle' && u.anchor !== 'end') return;
        let actual = -Infinity;
        let expected = -Infinity;
        u.runs.forEach(run => {
            const extent = runExtent(run);
            if (extent) actual = Math.max(actual, extent.end);
            expected = Math.max(expected, u.x0 + run.x + run.advance);
        });
        const delta = anchorShift(u.anchor, actual - expected);
        if (!Number.isFinite(delta) || Math.abs(delta) < 0.01 || !u.runs.length) return;
        u.x0 -= delta;
        u.runs[0].tspan.setAttribute('x', roundUnits(u.x0 + u.runs[0].x));
    }

    function placeMathParts(u) {
        if (u.parts.length === 0) return;
        const g = wrapMathText(u.text);
        const y0 = u.originY;
        const offsets = new Map();
        u.parts.forEach(part => {
            const run = part.run;
            if (run && !offsets.has(run)) {
                const extent = runExtent(run);
                offsets.set(run, extent ? extent.start - (u.x0 + run.x) : 0);
            }
            const x0 = u.x0 + (run ? offsets.get(run) : 0);
            if (part.svg) {
                part.svg.setAttribute('x', roundUnits(x0 + Number(part.svg.getAttribute('x'))));
                part.svg.setAttribute('y', roundUnits(y0 + Number(part.svg.getAttribute('y'))));
                g.appendChild(part.svg);
                return;
            }
            const rect = document.createElementNS(SVG_NS, 'rect');
            setAttributes(rect, [['x', roundUnits(x0 + part.rect.x)], ['y', roundUnits(y0 + part.rect.y)],
                ['width', roundUnits(part.rect.w)], ['height', roundUnits(part.rect.h)], ['fill', part.fill]]);
            g.appendChild(rect);
        });
    }

    // Under CSS zoom (Presto's scaling) WebKit reports SVG text font sizes,
    // and positions given in em, in zoomed units. Slides swapped in after
    // Presto zoomed them are measured with that inline zoom lifted for the
    // duration of a synchronous read (no paint happens in between).
    function withoutInlineZoom(nodes, fn) {
        const lifted = [];
        nodes.forEach(node => {
            for (let el = node; el && el.style; el = el.parentElement) {
                if (!el.style.zoom || el.style.zoom === '1' || lifted.some(l => l.el === el)) continue;
                lifted.push({ el: el, zoom: el.style.zoom });
                el.style.zoom = '1';
            }
        });
        try {
            return fn();
        } finally {
            lifted.forEach(l => { l.el.style.zoom = l.zoom; });
        }
    }

    async function renderSvgMath(root) {
        const texts = Array.from(root.querySelectorAll('section.slide svg text')).filter(text => {
            return !processedMathTexts.has(text) && !text.closest('[data-no-math]') && svgTextMightHaveMath(text);
        });
        if (texts.length === 0) return;
        const katex = await waitForKatex();
        if (!katex) {
            console.warn('WebSlides: $...$ in SVG text needs KaTeX, which this deck does not load');
            return;
        }
        const units = [];
        texts.forEach(text => {
            processedMathTexts.add(text);
            prepareSvgRoot(outermostSvg(text));
            collectMathUnits(text, units);
        });
        if (units.length === 0) return;
        // Lay out in the page so deck and theme rules on .katex (size) apply,
        // but keep the theme's .accent class off KaTeX's \hat/\bar accents.
        const host = document.createElement('div');
        host.className = 'ws-svg-math-host';
        host.setAttribute('aria-hidden', 'true');
        host.style.cssText = 'position:absolute;left:-100000px;top:0;width:1000px;visibility:hidden;pointer-events:none;';
        const hostStyle = document.createElement('style');
        hostStyle.textContent = '.ws-svg-math-host .katex .accent { color: inherit; font-weight: inherit; }';
        host.appendChild(hostStyle);
        document.body.appendChild(host);
        const mathTexts = units.map(u => u.text);
        try {
            withoutInlineZoom(mathTexts, () => units.forEach(u => buildMathMirror(u, host, katex)));
            await waitForKatexCss(host);
            await waitForMirrorFonts(host);
            const k = host.getBoundingClientRect().width / 1000 || 1;
            withoutInlineZoom(mathTexts, () => measureSvgUnits(units));
            const live = units.filter(u => !u.failed);
            live.forEach(markMirror);
            live.forEach(u => readMirror(u, k));
            live.forEach(writeMathRuns);
            withoutInlineZoom(mathTexts, () => live.forEach(fitMathUnit));
            withoutInlineZoom(mathTexts, () => live.forEach(placeMathParts));
        } finally {
            host.remove();
        }
    }

    // Stats, \stat math and SVG math for the deck, or for one slide an editor
    // swapped in.
    async function loadDeckContent(root) {
        if (statsSource()) {
            await loadStatValues(statNamesIn(root));
            fillStatsIn(root);
            if ((root.textContent || '').indexOf('\\stat') !== -1) {
                const katex = await waitForKatex();
                if (katex) renderStatMathInHtml(root, katex);
            }
        }
        await renderSvgMath(root);
    }

    // Presto's code pane and edit flushes replace whole slides with authored
    // source; fill those too, then let the reveal system rescan the slide.
    function watchReplacedSlides() {
        const deck = document.querySelector('.ws-deck');
        if (!deck || typeof MutationObserver !== 'function') return;
        new MutationObserver(records => {
            records.forEach(record => record.addedNodes.forEach(node => {
                if (node.nodeType !== 1 || !node.matches('section.slide')) return;
                loadDeckContent(node).then(() => {
                    if (WS.slides.indexOf(node) === -1) return;
                    applyRevealShortcuts(node);
                    WS.slideSteps.set(node, parseMaxStep(node));
                    if (WS.slides[WS.currentIndex] === node) applyStep(node, WS.currentStep);
                }).catch(err => console.warn('WebSlides: filling a replaced slide failed:', err));
            }));
        }).observe(deck, { childList: true });
    }

    /* ===== External Table Loading ===== */

    function engineSlideForElement(el) {
        const slide = el && el.closest ? el.closest('section.slide') : null;
        return slide && WS.slides && WS.slides.indexOf(slide) !== -1 ? slide : null;
    }

    function engineScopedElements(selector) {
        return Array.from(document.querySelectorAll(selector)).filter(el => engineSlideForElement(el));
    }

    const loadedTableContainers = [];
    const mathRenderedTables = new WeakSet();
    let deferredTableRefits = null;

    // Load external table HTML via data-table-src and re-scan steps, then fit
    // every table (inline ones too) and create overlays.
    async function loadExternalTables() {
        const containers = engineScopedElements('[data-table-src]');

        function showTableLoadError(el, shown, detail) {
            el.classList.add('ws-table-load-error');
            el.textContent = 'Failed to load table: ' + shown;
            recordLoadError('table', (el.dataset.tableSrc || '').trim(), detail || shown, el);
        }

        // Fetch and inject all table HTML
        await Promise.all(Array.from(containers).map(async el => {
            // Keep the authored content so editing tools can round-trip to source
            if (!el.hasAttribute('data-ws-original-html')) {
                el.setAttribute('data-ws-original-html', el.innerHTML);
            }
            try {
                const tableSrc = (el.dataset.tableSrc || '').trim();
                if (!tableSrc) {
                    showTableLoadError(el, 'Missing table source');
                    return;
                }
                const resp = await fetch(tableSrc);
                if (!resp.ok) {
                    showTableLoadError(el, resp.status + ' ' + resp.statusText);
                    return;
                }
                el.innerHTML = await resp.text();
                if (statsSource()) {
                    await loadStatValues(statNamesIn(el));
                    fillStatsIn(el);
                    substituteTableStatMacros(el);
                }
                loadedTableContainers.push(el);
                // Re-scan the parent slide's max step count
                const slide = engineSlideForElement(el);
                if (slide) {
                    applyRevealShortcuts(slide);
                    WS.slideSteps.set(slide, parseMaxStep(slide));
                }
            } catch (e) {
                console.warn('Failed to load table:', el.dataset.tableSrc, e);
                showTableLoadError(el, el.dataset.tableSrc || 'unknown source', (e && e.message) || 'network error');
            }
        }));

        // KaTeX auto-render ran before these tables existed
        if (typeof window.renderMathInElement === 'function') renderTableMath(window.renderMathInElement);

        if (engineScopedElements('.table-container').length === 0) return;
        // Scale tables to fit, then create column overlays
        await fitAllTables();
        createColumnOverlays();
        deferredTableRefits = schedulePostLayoutTableRefit();
        applyRequestedInitialStepAfterAsyncContent();
    }

    function renderTableMath(render) {
        const options = window.__wsKatexOptions || {
            delimiters: [
                { left: '$$', right: '$$', display: true },
                { left: '$', right: '$', display: false }
            ],
            throwOnError: false
        };
        loadedTableContainers.forEach(el => {
            if (mathRenderedTables.has(el) || !el.isConnected) return;
            mathRenderedTables.add(el);
            try {
                render(el, options);
            } catch (e) {
                console.warn('Failed to render math in table:', el.dataset.tableSrc, e);
            }
        });
    }

    // The deck's KaTeX auto-render, or null when the deck does not load KaTeX
    // (or it does not arrive within READY_WAIT_MS).
    function waitForMathRenderer() {
        return new Promise(resolve => {
            const started = Date.now();
            (function poll() {
                if (typeof window.renderMathInElement === 'function') {
                    resolve(window.renderMathInElement);
                } else if (!document.querySelector('script[src*="katex"]') || Date.now() - started >= READY_WAIT_MS) {
                    resolve(null);
                } else {
                    setTimeout(poll, 50);
                }
            })();
        });
    }

    function waitForTableSlideImages() {
        const pending = new Set();
        engineScopedElements('.table-container').forEach(container => {
            engineSlideForElement(container).querySelectorAll('img').forEach(img => {
                if (!img.complete && img.loading !== 'lazy') pending.add(img);
            });
        });
        return Promise.all(Array.from(pending).map(img => new Promise(resolve => {
            img.addEventListener('load', resolve, { once: true });
            img.addEventListener('error', resolve, { once: true });
        })));
    }

    // Everything WS.ready waits for after the first table fit: late KaTeX (whose
    // own onload renders the body), fonts, images beside tables, the deferred
    // refits, then one final fit.
    async function settleAsyncContent() {
        const hasTables = engineScopedElements('.table-container').length > 0;
        await Promise.all([
            waitForMathRenderer().then(render => { if (render) renderTableMath(render); }),
            boundedWait(document.fonts && document.fonts.ready, READY_WAIT_MS),
            hasTables ? boundedWait(waitForTableSlideImages(), READY_WAIT_MS) : null,
            deferredTableRefits
        ]);
        if (hasTables) await WS.refitTables();
        applyRequestedInitialStepAfterAsyncContent();
    }

    function applyRequestedInitialStepAfterAsyncContent() {
        if (WS.requestedInitialSlide === null || WS.currentIndex !== WS.requestedInitialSlide) return;
        if (Number(WS.requestedInitialStep) === WS.currentStep) return;
        goToSlide(WS.requestedInitialSlide, WS.requestedInitialStep);
    }

    function schedulePostLayoutTableRefit() {
        var delays = [0, 100, 300];
        return Promise.all(delays.map(function (delay) {
            return new Promise(function (resolve) {
                window.setTimeout(function () {
                    WS.refitTables().catch(function (err) {
                        console.warn('Deferred table refit failed:', err);
                    }).then(resolve);
                }, delay);
            });
        }));
    }

    // Tables grow into free space by at most this factor (opt out per container
    // with data-no-grow); shrinking is bounded only by MIN_FITTED_TABLE_HEIGHT.
    const TABLE_GROW_CAP = 1.3;
    const MIN_FITTED_TABLE_HEIGHT = 150;

    // Two frames for layout to settle; the timer covers views where
    // requestAnimationFrame is paused (hidden or background pages).
    function afterLayout() {
        return new Promise(resolve => {
            let done = false;
            const finish = () => { if (!done) { done = true; resolve(); } };
            requestAnimationFrame(() => requestAnimationFrame(finish));
            setTimeout(finish, 250);
        });
    }

    // Scale each table to the space its slide leaves it: shrink so nothing
    // overflows, or grow into free space up to TABLE_GROW_CAP. Growth only applies
    // when the table is the slide's only fitted table.
    function fitAllTables() {
        return afterLayout().then(() => {
            const bySlide = new Map();
            engineScopedElements('.table-container').forEach(container => {
                if (!container.querySelector('table')) return;
                const slide = engineSlideForElement(container);
                if (!bySlide.has(slide)) bySlide.set(slide, []);
                bySlide.get(slide).push(container);
            });
            bySlide.forEach((containers, slide) => {
                // Not laid out (e.g. overview open): keep the previous fit
                if (!(slide.getBoundingClientRect().height > 0)) return;
                containers.forEach(resetTableFit);
                containers.forEach(container => {
                    fitTable(container, slide, containers.length === 1 && !container.hasAttribute('data-no-grow'));
                });
            });
        });
    }

    function resetTableFit(container) {
        const table = container.querySelector('table');
        table.style.transform = '';
        table.style.transformOrigin = '';
        container.style.height = '';
        container.style.overflow = '';
    }

    function isOutOfFlow(style) {
        return style.position === 'absolute' || style.position === 'fixed';
    }

    function fitTable(container, slide, allowGrow) {
        const table = container.querySelector('table');
        const tW = table.scrollWidth;
        const tH = table.scrollHeight;
        if (tW <= 0 || tH <= 0) return;

        // Measure the layout the fitted container will have: overflow:hidden makes it
        // a block formatting context, so the table's own margins sit inside it (they
        // can collapse outside an unfitted container). flex-shrink:0 stops a flex
        // wrapper squeezing it during the measurement.
        const savedFlexShrink = container.style.flexShrink;
        container.style.overflow = 'hidden';
        container.style.flexShrink = '0';
        const slideStyle = getComputedStyle(slide);
        const isColumnFlex = slideStyle.display.includes('flex') && slideStyle.flexDirection.startsWith('column');
        let holder = container;
        while (holder.parentElement !== slide) holder = holder.parentElement;
        const marked = isColumnFlex ? markTableMeasurement(slide, holder) : [];
        let scale;
        let fixedH;
        try {
            const slideRect = slide.getBoundingClientRect();
            const r = slideRect.height / 720;
            const containerStyle = getComputedStyle(container);
            const containerRect = container.getBoundingClientRect();
            const padL = parseFloat(containerStyle.paddingLeft) || 0;
            const contentW = container.clientWidth - padL - (parseFloat(containerStyle.paddingRight) || 0);
            const tableOffset = Math.max(0, (table.getBoundingClientRect().left - containerRect.left) / r -
                (parseFloat(containerStyle.borderLeftWidth) || 0) - padL);
            const availW = contentW > 0
                ? contentW - tableOffset
                : 1280 - (parseFloat(slideStyle.paddingLeft) || 0) - (parseFloat(slideStyle.paddingRight) || 0);
            const freeH = freeSlideHeight(slide, holder, slideStyle, slideRect, containerRect, r);
            // freeH is about 0 when a shrinkable fill item (.fig-plot) took the rest:
            // the table fits as it is. Otherwise keep a 1px margin.
            const allowedH = freeH > -0.5
                ? tH + Math.max(0, freeH - 1)
                : Math.max(tH + freeH - 1, Math.min(MIN_FITTED_TABLE_HEIGHT, tH));
            scale = Math.min(availW / tW, allowedH / tH, allowGrow ? TABLE_GROW_CAP : 1);
            fixedH = container.offsetHeight - tH; // table margins, container padding, other content
        } finally {
            if (isColumnFlex) slide.classList.remove('ws-measuring-tables');
            marked.forEach(el => el.removeAttribute('data-ws-measure'));
            container.style.flexShrink = savedFlexShrink;
        }

        if (!(scale > 0) || (scale >= 0.999 && scale <= 1.01)) {
            container.style.overflow = '';
            return;
        }
        table.style.transform = 'scale(' + scale + ')';
        table.style.transformOrigin = 'top left';
        container.style.height = Math.ceil(fixedH + tH * scale) + 'px';
    }

    // Lay a column-flex slide out for measuring (rules under .ws-measuring-tables in
    // webslides.css): the child holding the table takes its content height; fill
    // items with a zero basis (flex: 1 1 0, e.g. .fig-plot) keep their own height
    // instead of collapsing, so growth never takes their share; leftover space goes
    // to a spacer, not to auto margins (a footnote's margin-top: auto, .v-center).
    // Returns the children it marked.
    function markTableMeasurement(slide, holder) {
        const marked = [];
        for (const child of slide.children) {
            let role = null;
            if (child === holder) {
                role = 'holder';
            } else {
                const style = getComputedStyle(child);
                if (parseFloat(style.flexGrow) > 0 && parseFloat(style.flexBasis) === 0) role = 'fill';
            }
            if (role) {
                child.setAttribute('data-ws-measure', role);
                marked.push(child);
            }
        }
        slide.classList.add('ws-measuring-tables');
        return marked;
    }

    // Vertical space (slide px) left in the slide's content box, from the extent of
    // its in-flow children; negative when they overflow. `holder` is the slide
    // child that contains the table.
    function freeSlideHeight(slide, holder, slideStyle, slideRect, containerRect, r) {
        const top = (parseFloat(slideStyle.paddingTop) || 0) + (parseFloat(slideStyle.borderTopWidth) || 0);
        const bottom = 720 - (parseFloat(slideStyle.paddingBottom) || 0) - (parseFloat(slideStyle.borderBottomWidth) || 0);
        const toSlide = y => (y - slideRect.top) / r;
        if (isOutOfFlow(getComputedStyle(holder))) return bottom - toSlide(containerRect.bottom);

        // Include the container itself in case a collapsed wrapper clips it
        let start = toSlide(containerRect.top);
        let end = toSlide(containerRect.bottom);
        for (const child of slide.children) {
            if (child.tagName === 'ASIDE') continue;
            const style = getComputedStyle(child);
            if (style.display === 'none' || isOutOfFlow(style)) continue;
            const rect = child.getBoundingClientRect();
            start = Math.min(start, toSlide(rect.top) - (parseFloat(style.marginTop) || 0));
            end = Math.max(end, toSlide(rect.bottom) + (parseFloat(style.marginBottom) || 0));
        }
        return (bottom - top) - (end - start);
    }

    // Create column/row highlight overlays from data-overlays attribute
    // Config format: [{"cols":[1],"step":"0"}, {"rows":[1],"step":"0","class":"blue"}]
    function createColumnOverlays() {
        // Remove any existing overlays (safe to re-call after refitTables)
        engineScopedElements('.table-container').forEach(container => {
            container.querySelectorAll('.column-overlay').forEach(el => el.remove());
            container.querySelectorAll('.ws-table-config-error').forEach(el => el.remove());
        });

        function showTableConfigError(container, detail) {
            console.warn('Invalid table overlay config:', detail);
            const error = document.createElement('div');
            error.className = 'ws-table-config-error';
            error.textContent = 'Invalid table overlay config: ' + detail;
            container.appendChild(error);
        }

        function parseOverlayIndexes(values, limit, label, container) {
            if (!Array.isArray(values) || values.length === 0) {
                showTableConfigError(container, label + ' must be a non-empty array');
                return null;
            }
            for (var i = 0; i < values.length; i++) {
                var value = values[i];
                if (!Number.isSafeInteger(value) || value < 0 || value >= limit) {
                    showTableConfigError(container, label + ' index out of range: ' + value);
                    return null;
                }
                if (i > 0 && value < values[i - 1]) {
                    showTableConfigError(container, label + ' indexes must be in ascending order');
                    return null;
                }
            }
            return values;
        }

        engineScopedElements('.table-container[data-overlays]').forEach(container => {
            const table = container.querySelector('table');
            if (!table) return;
            var overlayConfig;
            try { overlayConfig = JSON.parse(container.dataset.overlays); }
            catch (e) {
                showTableConfigError(container, e.message || 'could not parse JSON');
                return;
            }
            if (!Array.isArray(overlayConfig)) {
                showTableConfigError(container, 'expected an array');
                return;
            }

            container.style.position = 'relative';
            var containerRect = container.getBoundingClientRect();
            var containerScaleX = container.offsetWidth ? containerRect.width / container.offsetWidth : 1;
            var containerScaleY = container.offsetHeight ? containerRect.height / container.offsetHeight : 1;
            if (!Number.isFinite(containerScaleX) || containerScaleX <= 0) containerScaleX = 1;
            if (!Number.isFinite(containerScaleY) || containerScaleY <= 0) containerScaleY = 1;

            function cssX(x) {
                return (x - containerRect.left) / containerScaleX;
            }

            function cssY(y) {
                return (y - containerRect.top) / containerScaleY;
            }

            function cssW(w) {
                return w / containerScaleX;
            }

            function cssH(h) {
                return h / containerScaleY;
            }

            // Use the header row with the most cells (avoids colspan rows)
            var headerRows = table.querySelectorAll('thead tr');
            if (!headerRows.length) return;
            var headerRow = headerRows[0];
            for (var ri = 1; ri < headerRows.length; ri++) {
                if (headerRows[ri].querySelectorAll('th').length > headerRow.querySelectorAll('th').length) {
                    headerRow = headerRows[ri];
                }
            }
            var headerCells = headerRow.querySelectorAll('th');
            var tableRect = table.getBoundingClientRect();

            overlayConfig.forEach(function (cfg) {
                if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) {
                    showTableConfigError(container, 'overlay entry must be an object');
                    return;
                }
                if (cfg.cols !== undefined && !Array.isArray(cfg.cols)) {
                    showTableConfigError(container, 'cols must be an array');
                    return;
                }
                if (cfg.rows !== undefined && !Array.isArray(cfg.rows)) {
                    showTableConfigError(container, 'rows must be an array');
                    return;
                }
                if (cfg.cols === undefined && cfg.rows === undefined) {
                    showTableConfigError(container, 'overlay entry needs cols or rows');
                    return;
                }
                var overlay = document.createElement('div');
                overlay.className = 'column-overlay' + (cfg.class ? ' ' + cfg.class : '');
                if (cfg.step !== undefined) overlay.dataset.step = cfg.step;

                if (cfg.cols) {
                    var cols = parseOverlayIndexes(cfg.cols, headerCells.length, 'cols', container);
                    if (!cols) return;
                    // Column overlay
                    var firstCol = headerCells[cols[0]];
                    var lastCol = headerCells[cols[cols.length - 1]];
                    var firstColRect = firstCol.getBoundingClientRect();
                    var lastColRect = lastCol.getBoundingClientRect();

                    overlay.style.left = cssX(firstColRect.left) + 'px';
                    overlay.style.top = cssY(tableRect.top) + 'px';
                    overlay.style.width = cssW(lastColRect.right - firstColRect.left) + 'px';
                    overlay.style.height = cssH(tableRect.height) + 'px';
                } else if (cfg.rows) {
                    // Row overlay — rows are 0-indexed within tbody
                    var tbody = table.querySelector('tbody');
                    if (!tbody) return;
                    var rows = tbody.querySelectorAll('tr');
                    var rowIndexes = parseOverlayIndexes(cfg.rows, rows.length, 'rows', container);
                    if (!rowIndexes) return;
                    var firstRow = rows[rowIndexes[0]];
                    var lastRow = rows[rowIndexes[rowIndexes.length - 1]];
                    var firstRowRect = firstRow.getBoundingClientRect();
                    var lastRowRect = lastRow.getBoundingClientRect();

                    overlay.style.left = cssX(tableRect.left) + 'px';
                    overlay.style.top = cssY(firstRowRect.top) + 'px';
                    overlay.style.width = cssW(tableRect.width) + 'px';
                    overlay.style.height = cssH(lastRowRect.bottom - firstRowRect.top) + 'px';
                }

                container.appendChild(overlay);
            });

            // Re-scan max steps for the parent slide
            var slide = engineSlideForElement(container);
            if (slide) WS.slideSteps.set(slide, parseMaxStep(slide));
        });

        // Re-apply step for current slide (overlays created after initial applyStep)
        if (WS.slides[WS.currentIndex]) {
            applyStep(WS.slides[WS.currentIndex], WS.currentStep);
        }
    }

    /* ===== Start ===== */

    async function start() {
        try {
            await ensureKatex().catch(err => console.warn('WebSlides: KaTeX fallback failed:', err));
            await inlineSVGs();
            await loadDeckContent(document.querySelector('.ws-deck') || document.body);
            init();
            watchReplacedSlides();
            await loadExternalTables();
            await settleAsyncContent();
        } finally {
            // WS.ready never rejects; a deck error still surfaces as a page error.
            markReady();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            start().catch(err => setTimeout(() => { throw err; }, 0));
        });
    } else {
        start().catch(err => setTimeout(() => { throw err; }, 0));
    }

})();

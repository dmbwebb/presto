/* Presto slide-frame layout (injected by Presto into the edit preview and the
   audience window; the engine's applyPrestoEditViewport delegates here).

   window.PrestoLayout.apply(opts) -> geometry | null
   window.PrestoLayout.current()   -> geometry of the last apply | null

   opts (CSS px): { mode: 'edit' | 'audience' | 'preview', insetLeft,
   insetRight, insetTop, insetBottom, padding, viewportWidth?, viewportHeight? }.
   Missing fields come from the previous apply when the mode is unchanged,
   else from the mode's defaults (edit: rail 232, step strip 72, padding 28;
   audience and preview: all zero). viewportWidth/Height override the window
   size for that one call (tests). apply() with no argument re-applies the
   last options (window resizes do this automatically).

   geometry: { scale, slideLeft, slideTop, slideW, slideH, viewportW,
   viewportH, mode, insetLeft, insetRight, insetTop, insetBottom, padding },
   slide position in page CSS px.

   Every slide, #ws-section-nav, #ws-footer and #ws-progress are placed in the
   slide frame: one CSS zoom factor for all, the nav on the slide's top edge,
   the footer on its bottom edge, both exactly the slide's width, the progress
   bar along the slide's top edge as a fraction of the slide's width. Edit
   centres the slide in its canvas; audience and preview centre it across and
   pin it to the top. CSS zoom (not transform) keeps text sharp on external
   displays.

   KaTeX: below zoom 1 the system WebKit misplaces sub- and superscripts
   (KaTeX's 1px .vlist-s strut cell collapses once its zoomed size falls under
   about 0.85px). While the scale is below 1, the stylesheet below gives each
   slide's .katex-html the inverse zoom and a compensating font size, so KaTeX
   lays out at an effective zoom of 1. It is driven by --ws-katex-zoom and
   --ws-katex-unzoom on the .ws-deck element only, so it reaches maths added
   later (slide patches, equation previews, table maths) with no extra call,
   and no inline style is ever written on an equation.

   Nothing here touches slide content: inline styles go on .ws-deck, the
   slides' own boxes, the nav, footer and progress bar, plus classes and a
   custom property on <html>. Presto saves slide bodies only (the section tag
   comes from the deck file), so none of it can reach the deck source.

   After each apply: document 'presto:layout' CustomEvent, detail = geometry. */
(function () {
    'use strict';
    if (window.PrestoLayout && window.PrestoLayout.version === 1) return;

    var SLIDE_W = 1280;
    var SLIDE_H = 720;
    var MODES = ['edit', 'audience', 'preview'];
    var INSET_KEYS = ['insetLeft', 'insetRight', 'insetTop', 'insetBottom', 'padding'];
    var DEFAULTS = {
        edit: { insetLeft: 232, insetRight: 0, insetTop: 0, insetBottom: 72, padding: 28 },
        audience: { insetLeft: 0, insetRight: 0, insetTop: 0, insetBottom: 0, padding: 0 },
        preview: { insetLeft: 0, insetRight: 0, insetTop: 0, insetBottom: 0, padding: 0 }
    };
    var STYLE_ID = 'presto-layout-style';
    var FRAME_ID = 'presto-slide-frame';

    // (Overflow stays the engine's: hidden, and auto while the overview scrolls.)
    var CSS = [
        'html.presto-layout-edit, html.presto-layout-edit body { background: var(--p-canvas, #E7EAEE) !important; }',
        'html.presto-layout-audience, html.presto-layout-audience body,',
        'html.presto-layout-preview, html.presto-layout-preview body { background: var(--presto-letterbox, #FFFFFF) !important; }',
        '#' + FRAME_ID + ' { position: fixed; pointer-events: none; z-index: 0; margin: 0; padding: 0; border: 0; }',
        'html.presto-layout-edit #' + FRAME_ID + ' { box-shadow: var(--p-shadow-slide, 0 1px 2px rgba(16, 24, 40, .06), 0 10px 30px rgba(16, 24, 40, .10)); }',
        'body.ws-overview-active #' + FRAME_ID + ' { display: none; }',
        'html.presto-layout body.ws-overview-active { background: var(--p-canvas, #E7EAEE) !important; }',
        'html.presto-layout .ws-overview-item { border-color: transparent; box-shadow: var(--p-shadow-card, 0 0 0 .5px rgba(0, 0, 0, .08), 0 1px 3px rgba(0, 0, 0, .05)); }',
        'html.presto-layout .ws-overview-item:hover, html.presto-layout .ws-overview-item.ws-overview-current { border-color: var(--p-accent, #2F6BD8); }',
        'html.presto-layout .ws-overview-item.ws-overview-current { box-shadow: 0 0 0 2px var(--p-accent-ring, rgba(47, 107, 216, .28)); }',
        'html.presto-layout .ws-overview-item .ws-overview-label { background: rgba(255, 255, 255, .94); color: var(--p-ink, #1C2532);' +
            ' border-top: 1px solid var(--p-hair, #DDE0E5); font-family: var(--p-font-ui, -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif); }',
        'html.presto-layout .ws-deck .slide .katex-html { zoom: var(--ws-katex-unzoom, 1); font-size: calc(var(--ws-katex-zoom, 1) * 100%); }'
    ].join('\n');

    var lastOptions = null;
    var geometry = null;
    var progressFraction = null;
    var pageBackground = null;
    var navigateHooked = false;

    function finiteNonNegative(value, fallback) {
        var n = Number(value);
        return Number.isFinite(n) && n >= 0 ? n : fallback;
    }

    function resolveOptions(opts) {
        opts = opts || {};
        var mode = MODES.indexOf(opts.mode) !== -1 ? opts.mode
            : (lastOptions ? lastOptions.mode : (window.WS && WS.mode === 'preview' ? 'preview' : 'audience'));
        var base = lastOptions && lastOptions.mode === mode ? lastOptions : DEFAULTS[mode];
        var resolved = { mode: mode };
        INSET_KEYS.forEach(function (key) {
            resolved[key] = finiteNonNegative(opts[key], base[key]);
        });
        return resolved;
    }

    function ensureStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = CSS;
        (document.head || document.documentElement).appendChild(style);
    }

    function ensureFrame(deck) {
        var frame = document.getElementById(FRAME_ID);
        if (frame) return frame;
        frame = document.createElement('div');
        frame.id = FRAME_ID;
        frame.setAttribute('aria-hidden', 'true');
        // Before the deck, so the deck (also position: fixed) paints over it.
        if (deck && deck.parentNode) deck.parentNode.insertBefore(frame, deck);
        else document.body.appendChild(frame);
        return frame;
    }

    function isTransparent(color) {
        return !color || color === 'transparent' || /^rgba\([^)]*,\s*0(\.0+)?\)$/.test(color);
    }

    // What the audience sees around the slide: the slide's own background
    // (the theme's white), else the page background the deck set.
    function letterboxColor() {
        var slide = window.WS && WS.slides ? WS.slides[WS.currentIndex] : null;
        var color = slide ? getComputedStyle(slide).backgroundColor : null;
        if (!isTransparent(color)) return color;
        return isTransparent(pageBackground) ? '#FFFFFF' : pageBackground;
    }

    function updateLetterbox() {
        var color = letterboxColor();
        document.documentElement.style.setProperty('--presto-letterbox', color);
        var frame = document.getElementById(FRAME_ID);
        if (frame) frame.style.background = color;
    }

    function setStyles(el, styles) {
        if (!el) return;
        Object.keys(styles).forEach(function (key) { el.style[key] = styles[key]; });
    }

    // The engine writes the progress width as a percentage of the window on
    // every navigation; keep the fraction and draw it across the slide.
    function placeProgress() {
        var bar = document.getElementById('ws-progress');
        if (!bar || !geometry) return;
        var raw = bar.style.width || '';
        if (/%$/.test(raw)) progressFraction = Math.max(0, Math.min(1, parseFloat(raw) / 100 || 0));
        var fraction = progressFraction === null ? 0 : progressFraction;
        var scale = geometry.scale;
        setStyles(bar, {
            zoom: String(scale),
            left: (geometry.slideLeft / scale) + 'px',
            top: (geometry.slideTop / scale) + 'px',
            right: 'auto',
            width: (fraction * SLIDE_W) + 'px',
            transform: 'none'
        });
    }

    function onNavigate() {
        if (!geometry) return;
        updateLetterbox();
        placeProgress();
    }

    function hookNavigate() {
        if (navigateHooked || !window.WS || typeof WS.on !== 'function') return;
        navigateHooked = true;
        WS.on('navigate', onNavigate);
    }

    function apply(opts) {
        var o = resolveOptions(opts);
        var viewportW = opts && opts.viewportWidth != null ? Number(opts.viewportWidth) : window.innerWidth;
        var viewportH = opts && opts.viewportHeight != null ? Number(opts.viewportHeight) : window.innerHeight;
        if (!Number.isFinite(viewportW) || !Number.isFinite(viewportH) || viewportW <= 0 || viewportH <= 0) return null;
        if (!window.WS || !Array.isArray(WS.slides) || WS.slides.length === 0) return null;
        var availW = viewportW - o.insetLeft - o.insetRight - 2 * o.padding;
        var availH = viewportH - o.insetTop - o.insetBottom - 2 * o.padding;
        var scale = Math.min(availW / SLIDE_W, availH / SLIDE_H);
        if (!Number.isFinite(scale) || scale <= 0) return null;

        var dpr = window.devicePixelRatio || 1;
        var snap = function (v) { return Math.round(v * dpr) / dpr; };
        var slideW = SLIDE_W * scale;
        var slideH = SLIDE_H * scale;
        var slideLeft = snap(o.insetLeft + o.padding + (availW - slideW) / 2);
        // Edit centres the slide on its canvas; the audience and the console's
        // preview pin it to the top, so a screen taller than 16:9 leaves its
        // spare height below the slide instead of a band above the section bar.
        var spareTop = o.mode === 'edit' ? (availH - slideH) / 2 : 0;
        var slideTop = snap(o.insetTop + o.padding + spareTop);
        var deckLeft = o.insetLeft;
        var deckTop = o.insetTop;

        if (pageBackground === null && document.body) pageBackground = getComputedStyle(document.body).backgroundColor;
        ensureStyle();
        var root = document.documentElement;
        root.classList.add('presto-layout');
        MODES.forEach(function (m) { root.classList.toggle('presto-layout-' + m, m === o.mode); });

        var deck = document.querySelector('.ws-deck');
        if (deck) {
            setStyles(deck, {
                position: 'fixed',
                left: deckLeft + 'px',
                top: deckTop + 'px',
                right: 'auto',
                bottom: 'auto',
                width: Math.max(0, viewportW - o.insetLeft - o.insetRight) + 'px',
                height: Math.max(0, viewportH - o.insetTop - o.insetBottom) + 'px',
                margin: '0'
            });
            if (scale < 1) {
                deck.style.setProperty('--ws-katex-zoom', String(scale));
                deck.style.setProperty('--ws-katex-unzoom', String(1 / scale));
            } else {
                deck.style.removeProperty('--ws-katex-zoom');
                deck.style.removeProperty('--ws-katex-unzoom');
            }
        }

        WS.slides.forEach(function (slide) {
            setStyles(slide, {
                zoom: String(scale),
                transform: 'none',
                transformOrigin: 'top left',
                left: ((slideLeft - deckLeft) / scale) + 'px',
                top: ((slideTop - deckTop) / scale) + 'px'
            });
        });

        setStyles(ensureFrame(deck), {
            left: slideLeft + 'px',
            top: slideTop + 'px',
            width: slideW + 'px',
            height: slideH + 'px'
        });

        setStyles(document.getElementById('ws-section-nav'), {
            zoom: String(scale),
            transform: 'none',
            transformOrigin: 'top left',
            left: (slideLeft / scale) + 'px',
            top: (slideTop / scale) + 'px',
            right: 'auto',
            bottom: 'auto',
            width: SLIDE_W + 'px'
        });

        // Top at the slide's bottom edge, lifted by the footer's own height.
        setStyles(document.getElementById('ws-footer'), {
            zoom: String(scale),
            transform: 'translateY(-100%)',
            transformOrigin: 'top left',
            left: (slideLeft / scale) + 'px',
            top: ((slideTop + slideH) / scale) + 'px',
            right: 'auto',
            bottom: 'auto',
            width: SLIDE_W + 'px'
        });

        lastOptions = o;
        geometry = {
            scale: scale,
            slideLeft: slideLeft,
            slideTop: slideTop,
            slideW: slideW,
            slideH: slideH,
            viewportW: viewportW,
            viewportH: viewportH,
            mode: o.mode,
            insetLeft: o.insetLeft,
            insetRight: o.insetRight,
            insetTop: o.insetTop,
            insetBottom: o.insetBottom,
            padding: o.padding
        };
        placeProgress();
        updateLetterbox();
        hookNavigate();

        var detail = current();
        try {
            document.dispatchEvent(new CustomEvent('presto:layout', { detail: detail }));
        } catch (err) {
            console.warn('PrestoLayout: a presto:layout listener failed', err);
        }
        return detail;
    }

    function current() {
        return geometry ? Object.assign({}, geometry) : null;
    }

    window.PrestoLayout = { version: 1, apply: apply, current: current };

    window.addEventListener('resize', function () {
        if (lastOptions) apply(null);
    });
})();

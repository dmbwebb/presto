/* Presto — WYSIWYG alignment guides, snapping, and live position/size readout.
 *
 * Standalone companion to presto/wysiwyg.js. That file owns the drag/resize
 * gestures; this module owns the *feedback* around them: it snaps the moving
 * element to the slide edges/centers and to its siblings' edges/centers, draws
 * the guide lines, and shows a live x,y / w×h badge. It never touches the slide
 * DOM — every visual it creates lives in fixed-position overlay elements on
 * <body>/<head> carrying class `presto-wysiwyg-chrome`, so serialization strips
 * them the same way it strips wysiwyg.js chrome.
 *
 * All snapping math is done in AUTHORING coordinates (a fixed 1280×720 slide);
 * only rendering converts slide-px → client-px using the live slide rect, so
 * everything is zoom/scale-aware. Snap targets are precomputed once per gesture
 * (at dragStart / resizeStart) — the per-move path never scans the DOM.
 *
 * ===========================================================================
 * INTEGRATION CONTRACT — driven by wysiwyg.js via `window.__prestoWysiwygGuides`
 * ===========================================================================
 * All coordinates are slide-px (authoring units). Every method is a no-op-safe
 * call: if this module is absent, wysiwyg.js uses its raw values unchanged.
 *
 *   init(opts?)                     Build the overlay (idempotent). Lazy: the
 *                                   first drag/resizeStart calls it for you.
 *                                   opts.threshold overrides the 5-px snap band.
 *   dispose()                       Remove overlay + the window-resize listener.
 *
 *   dragStart(el, slide)            Snapshot el's box + translate, precompute
 *                                   sibling/slide snap targets. Call once, when
 *                                   the drag actually begins (past threshold).
 *   dragMove(el, slide, x, y, opts) x,y = the PROPOSED new translate (slide-px).
 *       → { x, y }                  Returns the SNAPPED translate. Draws/hides
 *                                   guide lines and the x,y readout. opts.alt
 *                                   (Alt held) disables snapping but keeps the
 *                                   readout live.
 *   dragEnd()                       Hide all guides + readout, end the gesture.
 *
 *   resizeStart(el, slide)          As dragStart, for a resize gesture.
 *   resizeMove(el, slide, w, h, o)  w = proposed width (slide-px); h = proposed
 *       → { width, height? }        height (slide-px) or undefined for width-
 *                                   only (aspect-locked) resizes. Snaps the
 *                                   moving right/bottom edges; returns snapped
 *                                   size and draws the w×h readout. opts.alt
 *                                   disables snapping.
 *   resizeEnd()                     Hide all guides + readout, end the gesture.
 *
 * The x,y readout shows the element's on-slide POSITION (its top-left corner in
 * authoring px), NOT the raw translate — that is the number a user aligns by.
 *
 * ---------------------------------------------------------------------------
 * RECOMMENDED wysiwyg.js WIRING (≤10 lines; the orchestrator applies these):
 *
 *   // onDragMove(), inside the `if (!drag.active) { … }` block, after
 *   // `drag.active = true;` :
 *       if (window.__prestoWysiwygGuides) __prestoWysiwygGuides.dragStart(drag.el, activeSlide());
 *
 *   // onDragMove(), replace the single setTranslate(...) call with:
 *       var nx = drag.base.x + dx / scale, ny = drag.base.y + dy / scale;
 *       if (window.__prestoWysiwygGuides) {
 *           var snapped = __prestoWysiwygGuides.dragMove(drag.el, activeSlide(), nx, ny, { alt: e.altKey });
 *           nx = snapped.x; ny = snapped.y;
 *       }
 *       setTranslate(drag.el, nx, ny);
 *
 *   // onDragEnd(), first line:
 *       if (window.__prestoWysiwygGuides) __prestoWysiwygGuides.dragEnd();
 *
 *   // startResize(), after `state.resize = { … }`:
 *       if (window.__prestoWysiwygGuides) __prestoWysiwygGuides.resizeStart(el, section);
 *
 *   // onResizeMove(), replace the `var width = Math.max(24, …)` assignment tail:
 *       var width = Math.max(24, resize.startWidth + dx / scale);
 *       if (window.__prestoWysiwygGuides) width = __prestoWysiwygGuides.resizeMove(resize.el, slide, width, undefined, { alt: e.altKey }).width;
 *
 *   // onResizeEnd(), first line:
 *       if (window.__prestoWysiwygGuides) __prestoWysiwygGuides.resizeEnd();
 * ---------------------------------------------------------------------------
 */

(function () {
    'use strict';

    if (window.__prestoWysiwygGuides) return;

    var SLIDE_WIDTH = 1280;
    var SLIDE_HEIGHT = 720;

    var config = {
        threshold: 5,      // snap band, slide-px
        minSize: 24,       // floor for a snapped dimension, slide-px
        minTargetSize: 8   // ignore siblings smaller than this, slide-px
    };

    // Precomputed per-gesture state; null between gestures.
    //   { mode, el, slide, box0:{left,top,width,height}, base:{x,y},
    //     targetsX:[{v,kind}], targetsY:[{v,kind}], aspectRatio }
    var session = null;

    var dom = { style: null, vLine: null, hLine: null, readout: null, built: false };
    var resizeHandler = null;
    var lastRender = null;   // snapshot of the last render() call, for tests

    /* ===== Geometry ===== */

    function slideMetrics(slide) {
        var rect = slide.getBoundingClientRect();
        var scale = rect.width > 0 ? rect.width / SLIDE_WIDTH : 1;
        return { rect: rect, scale: scale };
    }

    // Element bounding box expressed in authoring (slide) coordinates.
    function boxInSlide(el, slideRect, scale) {
        var r = el.getBoundingClientRect();
        return {
            left: (r.left - slideRect.left) / scale,
            top: (r.top - slideRect.top) / scale,
            width: r.width / scale,
            height: r.height / scale
        };
    }

    function parseTranslate(el) {
        var raw = (el.style && el.style.translate) || '';
        if (!raw || raw === 'none') return { x: 0, y: 0 };
        var parts = raw.split(/\s+/).map(parseFloat);
        return { x: isFinite(parts[0]) ? parts[0] : 0, y: isFinite(parts[1]) ? parts[1] : 0 };
    }

    /* ===== Snap targets ===== */

    function kindPriority(k) { return k === 'center' ? 3 : (k === 'edge' ? 2 : 1); }

    function collectTargets(slide, el, slideRect, scale) {
        var xs = [
            { v: 0, kind: 'edge' },
            { v: SLIDE_WIDTH, kind: 'edge' },
            { v: SLIDE_WIDTH / 2, kind: 'center' }
        ];
        var ys = [
            { v: 0, kind: 'edge' },
            { v: SLIDE_HEIGHT, kind: 'edge' },
            { v: SLIDE_HEIGHT / 2, kind: 'center' }
        ];
        var children = slide.children || [];
        for (var i = 0; i < children.length; i++) {
            var c = children[i];
            if (c === el) continue;
            if (c.classList && c.classList.contains('presto-wysiwyg-chrome')) continue;
            if (c.closest && c.closest('.presto-wysiwyg-chrome')) continue;
            if (c.contains && (c.contains(el) || el.contains(c))) continue;
            var b = boxInSlide(c, slideRect, scale);
            if (b.width <= 0 || b.height <= 0) continue;
            if (b.width < config.minTargetSize && b.height < config.minTargetSize) continue;
            xs.push({ v: b.left, kind: 'sibling' });
            xs.push({ v: b.left + b.width / 2, kind: 'sibling' });
            xs.push({ v: b.left + b.width, kind: 'sibling' });
            ys.push({ v: b.top, kind: 'sibling' });
            ys.push({ v: b.top + b.height / 2, kind: 'sibling' });
            ys.push({ v: b.top + b.height, kind: 'sibling' });
        }
        return { x: xs, y: ys };
    }

    // Nearest target within the threshold. `elLines` are the moving element's
    // candidate lines on this axis; nearest offset wins, ties break toward the
    // higher-priority target (slide center > slide edge > sibling).
    function snapAxis(elLines, targets, threshold) {
        var best = null;
        for (var i = 0; i < targets.length; i++) {
            var t = targets[i];
            for (var j = 0; j < elLines.length; j++) {
                var off = t.v - elLines[j];
                var a = Math.abs(off);
                if (a > threshold) continue;
                if (!best || a < best.abs - 1e-6 ||
                    (Math.abs(a - best.abs) <= 1e-6 && kindPriority(t.kind) > kindPriority(best.kind))) {
                    best = { abs: a, offset: off, value: t.v, kind: t.kind, elLine: elLines[j] };
                }
            }
        }
        return best;
    }

    function computeDragSnap(sess, x, y, alt) {
        var left = sess.box0.left + (x - sess.base.x);
        var top = sess.box0.top + (y - sess.base.y);
        var w = sess.box0.width, h = sess.box0.height;
        var snapX = alt ? null : snapAxis([left, left + w / 2, left + w], sess.targetsX, config.threshold);
        var snapY = alt ? null : snapAxis([top, top + h / 2, top + h], sess.targetsY, config.threshold);
        var offX = snapX ? snapX.offset : 0;
        var offY = snapY ? snapY.offset : 0;
        return {
            x: x + offX, y: y + offY,
            left: left + offX, top: top + offY, width: w, height: h,
            snapX: snapX, snapY: snapY
        };
    }

    function computeResizeSnap(sess, width, height, alt) {
        var left = sess.box0.left, top = sess.box0.top;
        var snapX = alt ? null : snapAxis([left + width], sess.targetsX, config.threshold);
        var newWidth = Math.max(config.minSize, (snapX ? snapX.value : left + width) - left);
        var out = { left: left, top: top, width: newWidth, snapX: snapX, snapY: null };
        if (typeof height === 'number') {
            var snapY = alt ? null : snapAxis([top + height], sess.targetsY, config.threshold);
            out.height = Math.max(config.minSize, (snapY ? snapY.value : top + height) - top);
            out.snapY = snapY;
        }
        return out;
    }

    /* ===== Overlay DOM ===== */

    var GUIDES_CSS = [
        '.presto-wysiwyg-guide {',
        '    position: fixed;',
        '    z-index: 2147482990;',
        '    margin: 0;',
        '    padding: 0;',
        '    background: var(--p-accent, #2F6BD8);',
        '    pointer-events: none;',
        '    display: none;',
        '}',
        '.presto-wysiwyg-guide.visible { display: block; }',
        '.presto-wysiwyg-guide-v { width: 1px; }',
        '.presto-wysiwyg-guide-h { height: 1px; }',
        '.presto-wysiwyg-guide-v.pw-guide-center {',
        '    background: repeating-linear-gradient(to bottom, var(--p-accent, #2F6BD8) 0 6px, transparent 6px 11px);',
        '}',
        '.presto-wysiwyg-guide-h.pw-guide-center {',
        '    background: repeating-linear-gradient(to right, var(--p-accent, #2F6BD8) 0 6px, transparent 6px 11px);',
        '}',
        '.presto-wysiwyg-guide-readout {',
        '    position: fixed;',
        '    z-index: 2147483002;',
        '    display: none;',
        '    padding: 3px 7px;',
        '    background: var(--p-surface, #FFFFFF);',
        '    border-radius: 6px;',
        '    box-shadow: var(--p-shadow-pop, 0 1px 2px rgba(16, 24, 40, .08), 0 8px 24px rgba(16, 24, 40, .14), 0 0 0 .5px rgba(0, 0, 0, .08));',
        '    font-family: var(--p-font-mono, "SF Mono", ui-monospace, Menlo, monospace);',
        '    font-size: 11px;',
        '    line-height: 1.4;',
        '    color: var(--p-ink, #1C2532);',
        '    font-variant-numeric: tabular-nums;',
        '    white-space: nowrap;',
        '    pointer-events: none;',
        '}',
        '.presto-wysiwyg-guide-readout.visible { display: block; }'
    ].join('\n');

    function makeLine(cls) {
        var d = document.createElement('div');
        d.className = 'presto-wysiwyg-guide ' + cls + ' presto-wysiwyg-chrome';
        document.body.appendChild(d);
        return d;
    }

    function init(opts) {
        if (opts && typeof opts.threshold === 'number') config.threshold = opts.threshold;
        if (dom.built) return;

        var style = document.createElement('style');
        style.id = 'presto-wysiwyg-guides-style';
        style.className = 'presto-wysiwyg-chrome';
        style.textContent = GUIDES_CSS;
        document.head.appendChild(style);
        dom.style = style;

        dom.vLine = makeLine('presto-wysiwyg-guide-v');
        dom.hLine = makeLine('presto-wysiwyg-guide-h');

        var readout = document.createElement('div');
        readout.className = 'presto-wysiwyg-guide-readout presto-wysiwyg-chrome';
        document.body.appendChild(readout);
        dom.readout = readout;

        resizeHandler = function () { hideAll(); };
        window.addEventListener('resize', resizeHandler);
        dom.built = true;
    }

    function dispose() {
        if (!dom.built) return;
        [dom.style, dom.vLine, dom.hLine, dom.readout].forEach(function (el) {
            if (el && el.parentNode) el.parentNode.removeChild(el);
        });
        if (resizeHandler) window.removeEventListener('resize', resizeHandler);
        resizeHandler = null;
        dom.style = dom.vLine = dom.hLine = dom.readout = null;
        dom.built = false;
        session = null;
    }

    function showEl(el) { if (el) el.classList.add('visible'); }
    function hideEl(el) { if (el) el.classList.remove('visible'); }

    function hideAll() {
        if (!dom.built) return;
        hideEl(dom.vLine);
        hideEl(dom.hLine);
        hideEl(dom.readout);
    }

    /* ===== Rendering (slide-px → client-px) ===== */

    function positionVLine(rect, scale, xVal, center) {
        var clientX = rect.left + xVal * scale;
        var thick = center ? 2 : 1;
        dom.vLine.style.width = thick + 'px';
        dom.vLine.style.left = (clientX - thick / 2) + 'px';
        dom.vLine.style.top = rect.top + 'px';
        dom.vLine.style.height = rect.height + 'px';
        dom.vLine.classList.toggle('pw-guide-center', center);
        showEl(dom.vLine);
    }

    function positionHLine(rect, scale, yVal, center) {
        var clientY = rect.top + yVal * scale;
        var thick = center ? 2 : 1;
        dom.hLine.style.height = thick + 'px';
        dom.hLine.style.top = (clientY - thick / 2) + 'px';
        dom.hLine.style.left = rect.left + 'px';
        dom.hLine.style.width = rect.width + 'px';
        dom.hLine.classList.toggle('pw-guide-center', center);
        showEl(dom.hLine);
    }

    function positionReadout(rect, scale, o) {
        dom.readout.textContent = o.readout;
        showEl(dom.readout);                     // show before measuring so size is real
        var badge = dom.readout.getBoundingClientRect();
        var cl = rect.left + o.left * scale;
        var ct = rect.top + o.top * scale;
        var cr = rect.left + (o.left + o.width) * scale;
        var cb = rect.top + (o.top + o.height) * scale;
        var x, y;
        if (o.mode === 'resize') { x = cr + 8; y = cb + 8; }
        else { x = cl; y = ct - badge.height - 6; }
        x = Math.max(4, Math.min(x, window.innerWidth - badge.width - 4));
        y = Math.max(4, Math.min(y, window.innerHeight - badge.height - 4));
        dom.readout.style.left = Math.round(x) + 'px';
        dom.readout.style.top = Math.round(y) + 'px';
    }

    function render(metrics, o) {
        var rect = metrics.rect, scale = metrics.scale;
        if (o.snapX) positionVLine(rect, scale, o.snapX.value, o.snapX.kind === 'center');
        else hideEl(dom.vLine);
        if (o.snapY) positionHLine(rect, scale, o.snapY.value, o.snapY.kind === 'center');
        else hideEl(dom.hLine);
        positionReadout(rect, scale, o);
        lastRender = o;
    }

    /* ===== Gesture API ===== */

    function dragStart(el, slide) {
        if (!el || !slide) { session = null; return; }
        init();
        var m = slideMetrics(slide);
        var targets = collectTargets(slide, el, m.rect, m.scale);
        session = {
            mode: 'drag', el: el, slide: slide,
            box0: boxInSlide(el, m.rect, m.scale),
            base: parseTranslate(el),
            targetsX: targets.x, targetsY: targets.y, aspectRatio: 0
        };
        hideAll();
    }

    function dragMove(el, slide, x, y, opts) {
        if (!session || session.mode !== 'drag') return { x: x, y: y };
        var m = slideMetrics(slide);
        var r = computeDragSnap(session, x, y, !!(opts && opts.alt));
        render(m, {
            left: r.left, top: r.top, width: r.width, height: r.height,
            snapX: r.snapX, snapY: r.snapY, mode: 'drag',
            readout: Math.round(r.left) + ', ' + Math.round(r.top)
        });
        return { x: r.x, y: r.y };
    }

    function dragEnd() {
        session = null;
        hideAll();
    }

    function resizeStart(el, slide) {
        if (!el || !slide) { session = null; return; }
        init();
        var m = slideMetrics(slide);
        var box0 = boxInSlide(el, m.rect, m.scale);
        var targets = collectTargets(slide, el, m.rect, m.scale);
        var aspect = (el.matches && el.matches('img, svg, video') && box0.width > 0)
            ? box0.height / box0.width : 0;
        session = {
            mode: 'resize', el: el, slide: slide, box0: box0,
            base: { x: 0, y: 0 }, targetsX: targets.x, targetsY: targets.y, aspectRatio: aspect
        };
        hideAll();
    }

    function resizeMove(el, slide, width, height, opts) {
        if (!session || session.mode !== 'resize') return { width: width, height: height };
        var m = slideMetrics(slide);
        var r = computeResizeSnap(session, width, height, !!(opts && opts.alt));
        var displayH;
        if (typeof r.height === 'number') displayH = Math.round(r.height);
        else if (session.aspectRatio) displayH = Math.round(r.width * session.aspectRatio);
        else displayH = Math.round(boxInSlide(el, m.rect, m.scale).height);
        render(m, {
            left: r.left, top: r.top, width: r.width,
            height: typeof r.height === 'number' ? r.height : displayH,
            snapX: r.snapX, snapY: r.snapY, mode: 'resize',
            readout: Math.round(r.width) + ' × ' + displayH
        });
        var out = { width: r.width };
        if (typeof r.height === 'number') out.height = r.height;
        return out;
    }

    function resizeEnd() {
        session = null;
        hideAll();
    }

    /* ===== Public API ===== */

    window.__prestoWysiwygGuides = {
        version: 1,
        init: init,
        dispose: dispose,
        dragStart: dragStart,
        dragMove: dragMove,
        dragEnd: dragEnd,
        resizeStart: resizeStart,
        resizeMove: resizeMove,
        resizeEnd: resizeEnd,

        // Introspection + pure helpers for tests.
        __test: {
            config: config,
            SLIDE_WIDTH: SLIDE_WIDTH,
            SLIDE_HEIGHT: SLIDE_HEIGHT,
            snapAxis: snapAxis,
            kindPriority: kindPriority,
            collectTargets: collectTargets,
            computeDragSnap: computeDragSnap,
            computeResizeSnap: computeResizeSnap,
            boxInSlide: boxInSlide,
            slideMetrics: slideMetrics,
            dom: dom,
            session: function () { return session; },
            lastRender: function () { return lastRender; }
        }
    };
})();

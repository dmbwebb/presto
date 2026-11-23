/* Shared slide-frame geometry probe for presto/edit-layout.js.
 *
 * Evaluate this file in a deck page that already has presto/edit-layout.js,
 * then call:
 *   await window.__prestoLayoutProbe({ mode, insetLeft, insetRight, insetTop,
 *       insetBottom, padding, slides: [index, ...] })
 * It applies PrestoLayout at the page's real window size for each slide and
 * returns { results: [{ index, kind, geometry, rects, failures }], failures }.
 * Used by tests/chrome-edit-preview-layout.js (Chrome), and by
 * tests/test_engine_redesign_layout.py (Playwright Chromium and WebKit, and
 * the system WebKit through tests/engine-redesign-wkharness.swift).
 */
(function () {
    'use strict';
    var TOL = 1.5;

    function rectOf(el) {
        if (!el) return null;
        var cs = getComputedStyle(el);
        if (cs.display === 'none') return null;
        var r = el.getBoundingClientRect();
        return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
    }

    // Geometry is read with forced layout; a timer (not requestAnimationFrame,
    // which an offscreen WKWebView never runs) lets resize handlers settle.
    function settle() {
        return new Promise(function (resolve) {
            setTimeout(function () { document.body.getBoundingClientRect(); resolve(); }, 30);
        });
    }

    function kindOf(slide) {
        if (slide.classList.contains('appendix')) return 'appendix';
        if (slide.classList.contains('section-header')) return 'section-header';
        if (slide.classList.contains('title')) return 'title';
        return 'content';
    }

    window.__prestoLayoutProbe = async function (cfg) {
        await WS.ready;
        var noTransitions = document.getElementById('presto-layout-probe-style');
        if (!noTransitions) {
            noTransitions = document.createElement('style');
            noTransitions.id = 'presto-layout-probe-style';
            noTransitions.textContent = '*, *::before, *::after { transition: none !important; animation: none !important; }';
            document.head.appendChild(noTransitions);
        }
        var opts = {
            mode: cfg.mode,
            insetLeft: cfg.insetLeft || 0,
            insetRight: cfg.insetRight || 0,
            insetTop: cfg.insetTop || 0,
            insetBottom: cfg.insetBottom || 0,
            padding: cfg.padding || 0
        };
        var out = { results: [], failures: [], viewport: { width: window.innerWidth, height: window.innerHeight } };
        for (var n = 0; n < cfg.slides.length; n++) {
            var index = cfg.slides[n];
            WS.goToSlide(index, 0);
            var events = [];
            var listener = function (e) { events.push(e.detail); };
            document.addEventListener('presto:layout', listener);
            var g = PrestoLayout.apply(opts);
            document.removeEventListener('presto:layout', listener);
            await settle();
            var failures = [];
            var slide = WS.slides[index];
            var label = 'slide ' + (index + 1) + ' (' + kindOf(slide) + ')';
            function check(name, ok, detail) { if (!ok) failures.push(label + ': ' + name + (detail ? ' (' + detail + ')' : '')); }
            function close(name, got, want) {
                check(name, got !== null && want !== null && Math.abs(got - want) <= TOL,
                    'got ' + (got === null ? 'none' : got.toFixed(2)) + ', want ' + (want === null ? 'none' : want.toFixed(2)));
            }
            if (!g) {
                failures.push(label + ': PrestoLayout.apply returned null');
                out.results.push({ index: index, kind: kindOf(slide), geometry: null, rects: null, failures: failures });
                out.failures = out.failures.concat(failures);
                continue;
            }
            var vw = window.innerWidth, vh = window.innerHeight;
            var canvas = {
                left: opts.insetLeft + opts.padding,
                top: opts.insetTop + opts.padding,
                right: vw - opts.insetRight - opts.padding,
                bottom: vh - opts.insetBottom - opts.padding
            };
            var s = rectOf(slide);
            var nav = rectOf(document.getElementById('ws-section-nav'));
            var footer = rectOf(document.getElementById('ws-footer'));
            var progressEl = document.getElementById('ws-progress');
            var progress = rectOf(progressEl);
            var frame = rectOf(document.getElementById('presto-slide-frame'));
            var heading = rectOf(slide.querySelector('h1, h2'));
            var deck = document.querySelector('.ws-deck');

            check('presto:layout fired once with the geometry', events.length === 1 && events[0] && events[0].scale === g.scale);
            check('current() matches apply()', JSON.stringify(PrestoLayout.current()) === JSON.stringify(g));
            close('slide left', s.left, g.slideLeft);
            close('slide top', s.top, g.slideTop);
            close('slide width', s.width, g.slideW);
            close('slide height', s.height, g.slideH);
            close('slide aspect', s.width / s.height * 720, 1280);
            check('slide inside the canvas', s.left >= canvas.left - TOL && s.right <= canvas.right + TOL &&
                s.top >= canvas.top - TOL && s.bottom <= canvas.bottom + TOL,
                JSON.stringify(s) + ' in ' + JSON.stringify(canvas));
            check('slide fills the canvas in one direction',
                Math.abs(s.width - (canvas.right - canvas.left)) <= TOL || Math.abs(s.height - (canvas.bottom - canvas.top)) <= TOL);
            close('slide centred horizontally', s.left - canvas.left, canvas.right - s.right);
            if (opts.mode === 'edit') close('slide centred vertically', s.top - canvas.top, canvas.bottom - s.bottom);
            else close('slide pinned to the top', s.top, canvas.top);
            check('heading inside the slide', !heading || (heading.top >= s.top - TOL && heading.bottom <= s.bottom + TOL &&
                heading.left >= s.left - TOL && heading.right <= s.right + TOL));
            if (nav) {
                close('section nav top = slide top', nav.top, s.top);
                close('section nav left = slide left', nav.left, s.left);
                close('section nav right = slide right', nav.right, s.right);
                var active = document.querySelectorAll('#ws-section-nav .ws-section-nav-item.active').length;
                if (slide.classList.contains('appendix')) check('appendix highlights no section', active === 0, active + ' active');
                else if (!slide.classList.contains('title')) check('one section highlighted', active === 1, active + ' active');
            }
            if (footer) {
                close('footer bottom = slide bottom', footer.bottom, s.bottom);
                close('footer left = slide left', footer.left, s.left);
                close('footer right = slide right', footer.right, s.right);
            }
            if (progress && progressEl.style.opacity !== '0') {
                close('progress top = slide top', progress.top, s.top);
                close('progress left = slide left', progress.left, s.left);
                check('progress within the slide width', progress.right <= s.right + TOL);
                var state = WS.getState();
                var fraction = state.isAppendix ? 1 : (state.mainSlides ? Number(state.slideNumber) / state.mainSlides : 0);
                close('progress length is the slide fraction', progress.width, fraction * s.width);
            }
            if (frame) {
                close('frame left', frame.left, s.left);
                close('frame top', frame.top, s.top);
                close('frame width', frame.width, s.width);
                close('frame height', frame.height, s.height);
            } else {
                check('slide frame element exists', false);
            }
            var leaking = WS.slides.filter(function (other, i) {
                if (i === index) return false;
                var cs = getComputedStyle(other);
                return cs.visibility !== 'hidden' && parseFloat(cs.opacity || '0') > 0.01;
            }).length;
            check('only the current slide is visible', leaking === 0, leaking + ' others visible');
            var root = document.documentElement;
            check('mode class on <html>', root.classList.contains('presto-layout') && root.classList.contains('presto-layout-' + opts.mode));
            var bodyBg = getComputedStyle(document.body).backgroundColor;
            var slideBg = getComputedStyle(slide).backgroundColor;
            if (opts.mode === 'edit') {
                check('edit canvas is light', bodyBg === 'rgb(231, 234, 238)', bodyBg);
                check('slide frame has a shadow', getComputedStyle(document.getElementById('presto-slide-frame')).boxShadow !== 'none');
            } else {
                check('letterbox is the slide background', bodyBg === slideBg, bodyBg + ' vs ' + slideBg);
                check('letterbox is never black', bodyBg !== 'rgb(0, 0, 0)' || slideBg === 'rgb(0, 0, 0)', bodyBg);
            }
            var kz = deck.style.getPropertyValue('--ws-katex-zoom');
            if (g.scale < 1) close('KaTeX compensation var', parseFloat(kz), g.scale);
            else check('no KaTeX compensation at zoom >= 1', kz === '', kz);
            check('no inline style on any equation', !slide.querySelector('.katex[style*="zoom"], .katex-html[style]'));
            out.results.push({
                index: index, kind: kindOf(slide), geometry: g, failures: failures,
                rects: { slide: s, nav: nav, footer: footer, progress: progress, frame: frame }
            });
            out.failures = out.failures.concat(failures);
        }
        return out;
    };
})();

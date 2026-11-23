/* Presto one-screen show controls, injected into the audience deck page.
 *
 * window.PrestoHUD = { install(opts), hint(text, ms), setState(state) }
 *   install({oneScreen})  oneScreen true: a light control bar (‹ n / N ›, time,
 *                         slide grid, edit this slide, End) that appears when
 *                         the pointer moves and hides 2.5 s later (never while
 *                         hovered), plus a corner timer the shell toggles. On an
 *                         external-display audience window (oneScreen false)
 *                         neither exists; only hint() works.
 *   hint(text, ms)        a light toast at the bottom centre, just above the
 *                         control bar (the Escape guard).
 *   setState({slideNumber, total, isAppendix, elapsedMs, timerVisible, timerRunning})
 *
 * It runs inside Duncan's real decks, so it binds no keys, never calls
 * preventDefault outside its own elements, adds nothing to the slide layout
 * (one fixed, zero-size root under <html>), and namespaces every id and class
 * with presto-hud-. Messages go to webkit.messageHandlers.presto:
 * hudNavigate {direction}, hudOverview {}, hudEditHere {}, hudEnd {}.
 */
(function () {
    'use strict';

    if (window.PrestoHUD && window.PrestoHUD.__prestoHUD) return;

    var HIDE_AFTER_MS = 2500;
    var HINT_DEFAULT_MS = 2000;
    var TICK_MS = 500;
    var MOVE_THRESHOLD_PX = 3;
    var SVG_NS = 'http://www.w3.org/2000/svg';
    var HINT_FALLBACK_CSS = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);z-index:2147483647;' +
        'padding:6px 12px;border-radius:8px;background:rgba(255,255,255,.94);color:#485263;' +
        'font:12.5px -apple-system,BlinkMacSystemFont,sans-serif;' +
        'box-shadow:0 0 0 .5px rgba(0,0,0,.1),0 4px 14px rgba(16,24,40,.12);pointer-events:none;';

    var root = null;
    var bar = null;
    var countNum = null;
    var countTotal = null;
    var barTime = null;
    var cornerTimer = null;
    var hintEl = null;
    var oneScreen = false;
    var barVisible = false;
    var hovered = false;
    var hideTimer = null;
    var hintTimer = null;
    var tickTimer = null;
    var lastPointer = null;
    var listening = false;
    var state = {
        slideNumber: null,
        total: null,
        isAppendix: false,
        elapsedMs: 0,
        receivedAt: Date.now(),
        timerVisible: false,
        timerRunning: true
    };

    function post(type, payload) {
        var handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.presto;
        if (!handler) return;
        var message = { type: type };
        if (payload) {
            for (var key in payload) {
                if (Object.prototype.hasOwnProperty.call(payload, key)) message[key] = payload[key];
            }
        }
        try {
            handler.postMessage(message);
        } catch (e) {
            if (window.console) console.warn('Presto HUD: failed to post ' + type, e);
        }
    }

    function pad2(n) { return (n < 10 ? '0' : '') + n; }

    function formatDuration(ms) {
        var total = Math.floor(Math.max(0, typeof ms === 'number' && isFinite(ms) ? ms : 0) / 1000);
        var h = Math.floor(total / 3600);
        var m = Math.floor((total % 3600) / 60);
        var s = total % 60;
        return h > 0 ? h + ':' + pad2(m) + ':' + pad2(s) : pad2(m) + ':' + pad2(s);
    }

    function currentElapsed() {
        var elapsed = state.elapsedMs;
        if (state.timerRunning) elapsed += Math.max(0, Date.now() - state.receivedAt);
        return elapsed;
    }

    function setText(el, text) {
        if (el && el.textContent !== text) el.textContent = text;
    }

    function cssLoaded() {
        if (!root || !window.getComputedStyle) return false;
        return getComputedStyle(root).getPropertyValue('--presto-hud-css').trim() === '1';
    }

    /* ===== DOM ===== */

    function icon(paths) {
        var svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', '0 0 18 18');
        svg.setAttribute('aria-hidden', 'true');
        paths.forEach(function (spec) {
            var node = document.createElementNS(SVG_NS, spec[0]);
            for (var attr in spec[1]) node.setAttribute(attr, spec[1][attr]);
            svg.appendChild(node);
        });
        return svg;
    }

    function button(action, label, content) {
        var b = document.createElement('button');
        b.type = 'button';
        b.tabIndex = -1;
        b.className = 'presto-hud-btn presto-hud-' + action;
        b.setAttribute('data-presto-hud', action);
        b.setAttribute('aria-label', label);
        b.title = label;
        if (content) b.appendChild(content);
        return b;
    }

    function divider() {
        var d = document.createElement('span');
        d.className = 'presto-hud-div';
        return d;
    }

    function buildBar() {
        bar = document.createElement('div');
        bar.id = 'presto-hud-bar';
        bar.setAttribute('role', 'toolbar');
        bar.setAttribute('aria-label', 'Presentation controls');

        bar.appendChild(button('prev', 'Previous', icon([['path', { d: 'M11 4L6 9l5 5' }]])));
        var count = document.createElement('span');
        count.className = 'presto-hud-count';
        countNum = document.createElement('b');
        countTotal = document.createElement('span');
        countTotal.className = 'presto-hud-total';
        count.appendChild(countNum);
        count.appendChild(countTotal);
        bar.appendChild(count);
        bar.appendChild(button('next', 'Next', icon([['path', { d: 'M7 4l5 5-5 5' }]])));
        bar.appendChild(divider());
        barTime = document.createElement('span');
        barTime.className = 'presto-hud-time';
        bar.appendChild(barTime);
        bar.appendChild(divider());
        bar.appendChild(button('grid', 'Slide grid', icon([
            ['rect', { x: '3', y: '3', width: '5', height: '5', rx: '1' }],
            ['rect', { x: '10', y: '3', width: '5', height: '5', rx: '1' }],
            ['rect', { x: '3', y: '10', width: '5', height: '5', rx: '1' }],
            ['rect', { x: '10', y: '10', width: '5', height: '5', rx: '1' }]
        ])));
        bar.appendChild(button('edit', 'Edit this slide', icon([['path', { d: 'M12 3l3 3-8.5 8.5H3.5V11.5z' }]])));
        var end = button('end', 'End presentation', null);
        end.textContent = 'End';
        bar.appendChild(end);

        bar.addEventListener('mouseenter', function () {
            hovered = true;
            cancelHide();
        });
        bar.addEventListener('mouseleave', function () {
            hovered = false;
            scheduleHide();
        });
        bar.addEventListener('click', onBarClick);
        root.appendChild(bar);
    }

    // Nothing that happens on the HUD may reach the deck: no slide advance, no
    // deck script, no focus change away from the page.
    function swallow(event) {
        event.stopPropagation();
        if (event.type === 'mousedown' || event.type === 'pointerdown') event.preventDefault();
    }

    function onBarClick(event) {
        var target = event.target && event.target.closest ? event.target.closest('[data-presto-hud]') : null;
        event.preventDefault();
        if (!target) return;
        var action = target.getAttribute('data-presto-hud');
        if (action === 'prev' || action === 'next') post('hudNavigate', { direction: action });
        else if (action === 'grid') post('hudOverview', {});
        else if (action === 'edit') post('hudEditHere', {});
        else if (action === 'end') post('hudEnd', {});
        if (!hovered) scheduleHide();
    }

    function ensureRoot() {
        if (root && root.isConnected) return;
        root = document.getElementById('presto-hud-root');
        if (!root) {
            root = document.createElement('div');
            root.id = 'presto-hud-root';
            root.setAttribute('data-presto-ui', 'hud');
            (document.documentElement || document.body).appendChild(root);
        }
        ['click', 'dblclick', 'mousedown', 'mouseup', 'pointerdown', 'pointerup', 'contextmenu', 'wheel']
            .forEach(function (type) { root.addEventListener(type, swallow, false); });
        hintEl = document.createElement('div');
        hintEl.id = 'presto-hud-hint';
        hintEl.setAttribute('role', 'status');
        hintEl.setAttribute('aria-live', 'polite');
        root.appendChild(hintEl);
    }

    function removeBar() {
        if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
        if (cornerTimer && cornerTimer.parentNode) cornerTimer.parentNode.removeChild(cornerTimer);
        bar = countNum = countTotal = barTime = cornerTimer = null;
        barVisible = false;
        hovered = false;
        cancelHide();
    }

    /* ===== Show / hide ===== */

    function setBarVisible(visible) {
        barVisible = visible;
        if (bar) bar.classList.toggle('presto-hud-visible', visible);
        render();
    }

    function cancelHide() {
        if (hideTimer !== null) clearTimeout(hideTimer);
        hideTimer = null;
    }

    function scheduleHide() {
        cancelHide();
        hideTimer = setTimeout(function () {
            hideTimer = null;
            if (!hovered) setBarVisible(false);
        }, HIDE_AFTER_MS);
    }

    function onPointerMove(event) {
        if (!oneScreen || !bar) return;
        // WebKit sends mousemove when the content under a still pointer
        // changes (a slide transition); only real movement shows the bar.
        var x = event.screenX;
        var y = event.screenY;
        if (lastPointer && Math.abs(x - lastPointer.x) + Math.abs(y - lastPointer.y) < MOVE_THRESHOLD_PX) return;
        lastPointer = { x: x, y: y };
        if (!barVisible) setBarVisible(true);
        if (!hovered) scheduleHide();
    }

    function onPointerLeave(event) {
        if (event.relatedTarget) return;
        hovered = false;
        if (barVisible) scheduleHide();
    }

    function listen() {
        if (listening) return;
        listening = true;
        window.addEventListener('mousemove', onPointerMove, { capture: true, passive: true });
        document.addEventListener('mouseout', onPointerLeave, { capture: true, passive: true });
    }

    /* ===== Rendering ===== */

    function render() {
        var cssOK = cssLoaded();
        var elapsedText = formatDuration(currentElapsed());
        if (bar) {
            var number = state.slideNumber;
            var numText = number === null || number === undefined || number === ''
                ? (state.isAppendix ? 'Appendix' : '—')
                : String(number);
            setText(countNum, numText);
            setText(countTotal, !state.isAppendix && state.total ? '/ ' + state.total : '');
            setText(barTime, elapsedText);
            bar.style.display = cssOK ? '' : 'none';
        }
        if (cornerTimer) {
            cornerTimer.classList.toggle('presto-hud-visible', oneScreen && state.timerVisible && cssOK);
            setText(cornerTimer, elapsedText);
            cornerTimer.style.display = cssOK ? '' : 'none';
        }
        scheduleTick();
    }

    function needsTick() {
        return state.timerRunning && oneScreen && (barVisible || state.timerVisible);
    }

    function scheduleTick() {
        if (tickTimer !== null || !needsTick()) return;
        tickTimer = setTimeout(function () {
            tickTimer = null;
            render();
        }, TICK_MS);
    }

    /* ===== Public API ===== */

    function install(opts) {
        opts = opts && typeof opts === 'object' ? opts : {};
        ensureRoot();
        oneScreen = opts.oneScreen === true;
        if (oneScreen) {
            if (!bar) buildBar();
            if (!cornerTimer) {
                cornerTimer = document.createElement('div');
                cornerTimer.id = 'presto-hud-timer';
                cornerTimer.setAttribute('aria-hidden', 'true');
                root.appendChild(cornerTimer);
            }
            listen();
        } else {
            removeBar();
        }
        if (!cssLoaded() && window.console) {
            console.warn('Presto HUD: present-hud.css is missing; the control bar stays hidden');
        }
        render();
        return window.PrestoHUD;
    }

    function setState(next) {
        if (!next || typeof next !== 'object') return;
        if ('slideNumber' in next) {
            var n = next.slideNumber;
            state.slideNumber = typeof n === 'number' && isFinite(n) ? n : (typeof n === 'string' ? n.trim() : null);
        }
        if ('total' in next) {
            state.total = typeof next.total === 'number' && next.total > 0 && isFinite(next.total) ? next.total : null;
        }
        if ('isAppendix' in next) state.isAppendix = next.isAppendix === true;
        if ('elapsedMs' in next && typeof next.elapsedMs === 'number' && isFinite(next.elapsedMs)) {
            state.elapsedMs = Math.max(0, next.elapsedMs);
            state.receivedAt = Date.now();
        }
        if ('timerRunning' in next && typeof next.timerRunning === 'boolean') {
            if (!next.timerRunning && state.timerRunning && !('elapsedMs' in next)) {
                state.elapsedMs = currentElapsed();
            }
            state.timerRunning = next.timerRunning;
            state.receivedAt = Date.now();
        }
        if ('timerVisible' in next && typeof next.timerVisible === 'boolean') state.timerVisible = next.timerVisible;
        render();
    }

    function hint(text, ms) {
        ensureRoot();
        var duration = typeof ms === 'number' && isFinite(ms) && ms > 0 ? ms : HINT_DEFAULT_MS;
        setText(hintEl, String(text === undefined || text === null ? '' : text));
        // Without the stylesheet, inline styles still show the Escape guard legibly.
        hintEl.style.cssText = cssLoaded() ? '' : HINT_FALLBACK_CSS;
        hintEl.classList.add('presto-hud-visible');
        if (hintTimer !== null) clearTimeout(hintTimer);
        hintTimer = setTimeout(function () {
            hintTimer = null;
            hintEl.classList.remove('presto-hud-visible');
            if (hintEl.style.cssText) hintEl.style.cssText = 'display:none';
        }, duration);
    }

    window.PrestoHUD = {
        __prestoHUD: true,
        install: install,
        hint: hint,
        setState: setState,
        _debug: function () {
            return {
                oneScreen: oneScreen,
                barVisible: barVisible,
                hovered: hovered,
                cssLoaded: cssLoaded(),
                state: JSON.parse(JSON.stringify(state))
            };
        }
    };
})();

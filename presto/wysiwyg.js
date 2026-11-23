/* Presto — WYSIWYG direct-manipulation editing layer for the edit-mode slide preview.
 *
 * Injected by Presto.swift into the editor-preview WKWebView after the deck loads
 * (and by the browser test harnesses). Everything here is app chrome: nothing in
 * this file may leak into the deck source. Serialization restores engine-applied
 * transforms (inlined SVGs, external tables, KaTeX, reveal classes) back to their
 * authored form before any HTML is sent to Swift for saving.
 *
 * Message protocol (window.webkit.messageHandlers.presto):
 *   { type: 'wysiwygChanged', index, html, saveId }  — save serialized slide body
 *       Undo/redo of an outside (source or Refine) change adds
 *       sectionHTML (the full section to install) and expectSectionHTML (what
 *       Swift's copy must currently be); Swift refuses a mismatch with
 *       saveFailed(saveId, …) and the page puts the slide back.
 *   { type: 'wysiwygEditingState', textEditing, hasSelection, selectionKind }
 *       — gate native key handling. textEditing is also true while an in-page
 *       field (font size, equation source, link card search) has focus.
 *   { type: 'wysiwygWillEdit' } — ask Swift to flush any dirty code-pane edit first
 *   { type: 'undoState', canUndo, canRedo, undoLabel, redoLabel } — the one history
 *   { type: 'ghostFutureChanged', on } — ⇧⌘O typed into the page
 *   { type: 'toggleSourceSidebar' } / { type: 'exportPDFRequested' } — API only
 *
 * Swift calls back:
 *   __prestoWysiwyg.saveConfirmed(saveId)
 *   __prestoWysiwyg.saveFailed(saveId, message)
 *   PrestoEdit.recordExternalChange({index, beforeHTML, afterHTML, origin})
 *   PrestoEdit.undo() / redo() / addTextBox() / editEquation() / handleEscape()
 *
 * DOM events for presto/edit-chrome.js (same page):
 *   presto:selection {kind, element, editing, index}, presto:steps {index, max},
 *   presto:ghostfuture {on}
 */

(function () {
    'use strict';

    if (window.__prestoWysiwyg) {
        // Re-injection into the same page (navigation reloads create a fresh
        // window, so this only happens on duplicate injection).
        return;
    }

    var SLIDE_WIDTH = 1280;
    var SLIDE_HEIGHT = 720;
    var DRAG_THRESHOLD_PX = 4;
    var NUDGE_STEP = 2;
    var NUDGE_STEP_LARGE = 20;
    var MAX_UNDO = 100;
    var STATUS_FADE_MS = 1600;
    var FONT_SIZE_MIN = 6;
    var FONT_SIZE_MAX = 300;
    // Coalescing: a rapid burst of nudges or font-size steps is one undo entry +
    // one save, flushed after this much key/pointer silence (or on any commit,
    // deselect, navigation, or arrow keyup).
    var COALESCE_IDLE_MS = 400;
    // Press-and-hold on the −/+ steppers: first repeat after HOLD_MS, then every
    // REPEAT_MS.
    var FONT_HOLD_MS = 350;
    var FONT_REPEAT_MS = 60;
    // The toolbar only re-anchors while the pointer is over/near it if the
    // element has drifted more than this far from the current placement.
    var TOOLBAR_ORPHAN_GAP = 80;
    // Treat the pointer as "over/near" the toolbar within this margin (px).
    var TOOLBAR_PROTECT_MARGIN = 28;
    var HIDDEN_STEP_OPACITY_STORAGE_KEY = 'presto-hidden-step-opacity';
    var GHOST_OPACITY = 0.33;
    // A new text box holds this until the user types; a box that ends its
    // first session untouched is removed without a save (BACKLOG 9).
    var TEXT_BOX_PLACEHOLDER = 'Text';
    var OPAQUE_MOVE_MESSAGE = 'Can’t move this: its position uses a custom transform. Edit it in the source.';
    var MAX_TEXT_SWATCHES = 12;

    // Elements the user can select by clicking. Deepest match wins.
    var LEAF_SELECTOR = 'h1, h2, h3, h4, h5, h6, p, li, figure, blockquote, pre, img, svg, video, a[href], .box, .text-box';
    // Containers whose background the Box row recolours.
    var BOX_SELECTOR = '.box, .text-box, .eqbox, .callout';
    var KIND_LABELS = { image: 'Image', table: 'Table', equation: 'Equation', box: 'Box', link: 'Link' };
    // Elements treated as one indivisible object: movable/deletable, never text-edited inside.
    var ATOM_SELECTOR = '[data-table-src], svg, img, video, .katex-display, .katex';
    // Editing chrome and engine chrome that must never be selected.
    var IGNORE_SELECTOR = 'aside.notes, .column-overlay, .ws-table-config-error, .presto-wysiwyg-chrome';
    // Inline-style properties this layer writes onto a selected element. Used to
    // carry user edits (move/resize/format) back onto an authored <img> without
    // dragging along an inlined SVG file-root's own class/style (svglite, etc.).
    var WYSIWYG_STYLE_PROPS = [
        'translate', 'width', 'height', 'left', 'top',
        'font-size', 'color', 'text-align', 'font-weight', 'font-style', 'text-decoration-line',
        'background-color'
    ];

    var state = {
        enabled: false,
        installed: false,
        selected: null,
        selectedIndex: -1,        // slide index that owned the selection when made
        textEditing: false,
        textEditSnapshot: null,   // snapshot taken when a text session starts
        textEditSection: null,    // section element that owns the running session
        textEditIndex: -1,        // slide index that owns the running session
        hover: null,
        drag: null,
        resize: null,
        saveCounter: 0,
        history: {},              // slide index -> { undo: [], redo: [] }
        lastEditingStateMsg: '',
        statusTimer: null,
        watchdogTimer: null,
        pendingSerializeWarning: null,  // katex span-soup stripped during serialize
        thumb: { pending: {}, rafId: null },
        coalesce: null,           // active nudge / font-step burst, see beginOrExtendBurst
        pointerX: -1,             // last seen pointer client x/y (for toolbar stickiness)
        pointerY: -1,
        stackPick: null,          // click-to-cycle state for overlapping objects
        overlapCount: 0,
        hiddenStepOpacity: GHOST_OPACITY,
        sourceSidebarVisible: false,
        pdfExportAvailable: true,
        newBox: null,             // text box created by this session, not yet committed
        newBoxTyped: false,
        popover: null,            // { name, el, anchor, onClose } — one at a time
        equation: null,           // live equation-editor session
        chromeTextFocus: false,   // an in-page field (font size, TeX, search) has focus
        suppressNavigate: 0,      // step refreshes that re-fire 'navigate' mid-session
        pendingHistory: {},       // saveId -> outside-change undo/redo awaiting Swift
        externalCounter: 0,
        lastUndoStateMsg: '',
        swallowClick: false,
        theme: null               // cached swatches from the deck's CSS variables
    };

    var chrome = {
        style: null,
        toolbar: null,
        status: null,
        handles: null,
        fontInput: null,
        groups: null,
        kindLabel: null,
        colorDot: null,
        boxSwatches: null,
        boxCustom: null,
        appearsChip: null,
        editTex: null
    };

    var testMessageSink = null;

    /* ===== Utilities ===== */

    function post(msg) {
        if (testMessageSink) {
            try { testMessageSink(msg); } catch (e) { /* test hook must not break editing */ }
        }
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.presto) {
            window.webkit.messageHandlers.presto.postMessage(msg);
        }
    }

    function wsReady() {
        return typeof window.WS !== 'undefined' && WS.slides && WS.slides.length > 0;
    }

    function activeSlide() {
        if (!wsReady()) return null;
        return WS.slides[WS.currentIndex] || null;
    }

    function slideScale(slide) {
        var rect = slide.getBoundingClientRect();
        return rect.width > 0 ? rect.width / SLIDE_WIDTH : 1;
    }

    function slidePointFromClient(slide, clientX, clientY) {
        var rect = slide.getBoundingClientRect();
        var scale = rect.width > 0 ? rect.width / SLIDE_WIDTH : 1;
        return {
            x: (clientX - rect.left) / scale,
            y: (clientY - rect.top) / scale
        };
    }

    function pxNum(token) {
        // A plain px length or a bare (unitless, == px) number, else null.
        return /^[-+]?\d*\.?\d+(?:px)?$/.test(token) ? parseFloat(token) : null;
    }

    function analyzeTranslate(el) {
        // Uses the standalone CSS `translate` property so we never clobber an
        // authored `transform`. Classifies the authored value so non-px units
        // (%, em, calc(), 3-value/z) are never silently rewritten as px.
        var raw = (el.style.translate || '').trim();
        if (!raw || raw === 'none') return { kind: 'px', x: 0, y: 0 };
        if (raw.indexOf('(') >= 0) return { kind: 'opaque', raw: raw };   // calc() etc.
        var parts = raw.split(/\s+/);
        if (parts.length > 2) return { kind: 'opaque', raw: raw };        // 3-value / z-axis
        var nx = pxNum(parts[0]);
        var ny = parts.length > 1 ? pxNum(parts[1]) : 0;
        if (nx !== null && ny !== null) return { kind: 'px', x: nx, y: ny };
        return { kind: 'unit', x: parts[0], y: parts.length > 1 ? parts[1] : '0' };
    }

    function setTranslate(el, x, y) {
        x = Math.round(x);
        y = Math.round(y);
        if (x === 0 && y === 0) {
            el.style.removeProperty('translate');
        } else {
            el.style.translate = x + 'px ' + y + 'px';
        }
        cleanEmptyStyleAttr(el);
    }

    function calcAddLength(base, delta) {
        // Add `delta` px to an authored length that may carry a non-px unit,
        // without resolving it (preserves %, em, calc semantics).
        var v = (base || '').trim();
        if (delta === 0) return v;
        if (v === '' || v === '0') return delta + 'px';
        var n = pxNum(v);
        if (n !== null) return (n + delta) + 'px';
        return 'calc(' + v + ' + ' + delta + 'px)';
    }

    // Move an element by (dx, dy) slide px. Returns false (with a status note)
    // for authored translate values this layer can't safely rewrite.
    function translateBy(el, dx, dy) {
        var a = analyzeTranslate(el);
        if (a.kind === 'px') { setTranslate(el, a.x + dx, a.y + dy); return true; }
        if (a.kind === 'unit') {
            el.style.translate = calcAddLength(a.x, dx) + ' ' + calcAddLength(a.y, dy);
            cleanEmptyStyleAttr(el);
            return true;
        }
        setStatus('error', OPAQUE_MOVE_MESSAGE);
        return false;
    }

    function cleanEmptyStyleAttr(el) {
        if (el.getAttribute && el.getAttribute('style') === '') el.removeAttribute('style');
    }

    function cleanEmptyClassAttr(el) {
        if (el.getAttribute && el.getAttribute('class') === '') el.removeAttribute('class');
    }

    function mergeInlineStyle(base, extra) {
        // Overlay the declarations in `extra` onto `base` (extra wins per-property).
        var d = document.createElement('div');
        d.setAttribute('style', base || '');
        var e = document.createElement('div');
        e.setAttribute('style', extra || '');
        for (var i = 0; i < e.style.length; i++) {
            var prop = e.style[i];
            d.style.setProperty(prop, e.style.getPropertyValue(prop), e.style.getPropertyPriority(prop));
        }
        return d.getAttribute('style') || '';
    }

    function extractManagedStyle(el) {
        // Only the inline-style properties this layer authors, as a style string.
        var src = el.style;
        var out = document.createElement('div');
        WYSIWYG_STYLE_PROPS.forEach(function (prop) {
            var v = src.getPropertyValue(prop);
            if (v) out.style.setProperty(prop, v, src.getPropertyPriority(prop));
        });
        return out.getAttribute('style') || '';
    }

    function parseStepValue(raw) {
        if (raw === undefined || raw === null) return { start: 1, end: undefined };
        raw = String(raw).trim();
        if (raw === '') return { start: 1, end: undefined };
        if (raw.endsWith('+')) {
            var plusStart = parseInt(raw.slice(0, -1), 10);
            return isFinite(plusStart) ? { start: plusStart, end: undefined } : null;
        }
        if (raw.indexOf('-') >= 0) {
            var parts = raw.split('-');
            if (parts.length !== 2) return null;
            var s = parseInt(parts[0], 10);
            var e = parseInt(parts[1], 10);
            if (!isFinite(s) || !isFinite(e) || e < s) return null;
            return { start: s, end: e };
        }
        var single = parseInt(raw, 10);
        return isFinite(single) ? { start: single, end: undefined } : null;
    }

    // The engine's strict parser when it is exposed (so "3.5" or "+4" never
    // count as steps: BACKLOG 3); the local copy is only a test fallback.
    function parseRevealValue(raw) {
        if (window.WS && typeof WS.parseRevealRange === 'function') {
            var r = WS.parseRevealRange(raw);
            return r ? { start: r.start, end: r.end === Infinity ? undefined : r.end } : null;
        }
        return parseStepValue(raw);
    }

    function computeMaxStep(slide) {
        var max = 0;
        slide.querySelectorAll('[data-step], [data-highlight], [data-dim], [data-class]').forEach(function (el) {
            ['step', 'highlight', 'dim', 'class'].forEach(function (attr) {
                var raw = el.dataset[attr];
                if (raw === undefined) return;
                var range = raw;
                if (attr === 'class') {
                    var sep = raw.lastIndexOf(':');
                    if (sep <= 0) return;
                    range = raw.slice(sep + 1);
                }
                var parsed = attr === 'step' && el._ws_step && /^\s*[+=]/.test(raw)
                    ? el._ws_step   // a relative step the engine already resolved
                    : parseRevealValue(range);
                if (!parsed) return;
                max = Math.max(max, parsed.start, parsed.end || 0);
            });
        });
        return max;
    }

    // Re-count a slide's reveal steps after its DOM changed in place (undo,
    // the Appears chip, a new bullet in a reveal list). Uses the engine's
    // WS.refreshSlideSteps when present (it also renumbers data-reveal="each"
    // lists); a 'navigate' it fires never ends a live text session.
    function refreshSteps(section) {
        if (!wsReady() || !section) return 0;
        var index = WS.slides.indexOf(section);
        if (index < 0) return 0;
        var max = null;
        state.suppressNavigate += 1;
        try {
            if (typeof WS.refreshSlideSteps === 'function') {
                try { max = WS.refreshSlideSteps(section); } catch (e) { max = null; }
            }
            if (typeof max !== 'number') {
                section.querySelectorAll('ul[data-reveal="each"] [data-ws-auto-step], ol[data-reveal="each"] [data-ws-auto-step]').forEach(function (li) {
                    li.removeAttribute('data-step');
                    li.removeAttribute('data-ws-auto-step');
                });
                if (typeof WS.applyRevealShortcuts === 'function') {
                    try { WS.applyRevealShortcuts(section); } catch (e) { /* keep the authored steps */ }
                }
                section.querySelectorAll('[data-step]').forEach(function (el) {
                    if (!/^\s*[+=]/.test(el.getAttribute('data-step'))) el._ws_step = undefined;
                });
                section.querySelectorAll('[data-highlight], [data-dim], [data-class]').forEach(function (el) {
                    el._ws_highlight = undefined;
                    el._ws_dim = undefined;
                    el._ws_class = undefined;
                });
                max = computeMaxStep(section);
                if (WS.slideSteps && typeof WS.slideSteps.set === 'function') WS.slideSteps.set(section, max);
                if (index === WS.currentIndex && typeof WS.goToSlide === 'function') {
                    WS.goToSlide(index, Math.min(WS.currentStep || 0, max));
                }
            }
        } finally {
            state.suppressNavigate -= 1;
        }
        try {
            document.dispatchEvent(new CustomEvent('presto:steps', { detail: { index: index, max: max } }));
        } catch (e) { /* old engines without CustomEvent */ }
        return max;
    }

    /* ===== Serialization: live DOM -> authored source ===== */

    function stripWysiwygArtifacts(root) {
        root.querySelectorAll('.presto-wysiwyg-chrome').forEach(function (el) { el.remove(); });
        root.querySelectorAll('[contenteditable]').forEach(function (el) {
            el.removeAttribute('contenteditable');
            el.removeAttribute('spellcheck');
        });
        root.querySelectorAll('[data-pw-atom-lock]').forEach(function (el) {
            el.removeAttribute('data-pw-atom-lock');
        });
        root.querySelectorAll('.presto-wysiwyg-selected, .presto-wysiwyg-hover, .presto-wysiwyg-editing').forEach(function (el) {
            el.classList.remove('presto-wysiwyg-selected', 'presto-wysiwyg-hover', 'presto-wysiwyg-editing');
            cleanEmptyClassAttr(el);
        });
    }

    function dataClassToken(el) {
        var raw = el.dataset.class || '';
        var sep = raw.lastIndexOf(':');
        if (sep <= 0) return null;
        var cls = raw.slice(0, sep).trim();
        if (!cls || /[\t\n\f\r ]/.test(cls)) return null;
        return cls;
    }

    function stripEngineArtifacts(root) {
        // Engine chrome injected into slides at runtime.
        root.querySelectorAll('.column-overlay, .ws-table-config-error').forEach(function (el) { el.remove(); });

        // Section-nav chrome the engine injects into section/subsection headers.
        // The eyebrow is always engine-generated; a `.section-toc` is only stripped
        // when it carries the engine's own `.ws-toc-label` markup (so a manually
        // authored TOC that opted out of injection is preserved).
        root.querySelectorAll('.ws-section-eyebrow').forEach(function (el) { el.remove(); });
        root.querySelectorAll('.section-toc').forEach(function (el) {
            if (el.querySelector('.ws-toc-label')) el.remove();
        });

        // Reveal-state classes.
        root.querySelectorAll('.ws-visible, .ws-highlighted, .ws-dimmed, .ws-faded, .ws-table-load-error').forEach(function (el) {
            el.classList.remove('ws-visible', 'ws-highlighted', 'ws-dimmed', 'ws-faded', 'ws-table-load-error');
        });
        root.querySelectorAll('[data-class]').forEach(function (el) {
            var cls = dataClassToken(el);
            if (cls) el.classList.remove(cls);
        });

        // Steps the engine generated from `data-reveal="each"` lists. The
        // authored source carries only the list attribute, so drop both the
        // generated step and its marker; explicit author steps are untouched.
        root.querySelectorAll('[data-ws-auto-step]').forEach(function (el) {
            el.removeAttribute('data-step');
            el.removeAttribute('data-ws-auto-step');
        });

        // Table auto-fit styles the engine writes when scaling a table down
        // (fitAllTables + createColumnOverlays). The engine's own refit reset
        // omits overflow/transform-origin/position, so strip all of them here to
        // honor the "nothing engine-written leaks into the deck source" contract.
        root.querySelectorAll('.table-container').forEach(function (container) {
            container.style.removeProperty('height');
            container.style.removeProperty('overflow');
            container.style.removeProperty('position');
            cleanEmptyStyleAttr(container);
            var table = container.querySelector('table');
            if (table) {
                table.style.removeProperty('transform');
                table.style.removeProperty('transform-origin');
                cleanEmptyStyleAttr(table);
            }
        });

        root.querySelectorAll('[class=""]').forEach(function (el) { el.removeAttribute('class'); });
    }

    function restoreInlinedSVGs(root) {
        root.querySelectorAll('svg[data-ws-original-img]').forEach(function (svg) {
            var template = document.createElement('template');
            template.innerHTML = svg.getAttribute('data-ws-original-img');
            var img = template.content.firstElementChild;
            if (!img || img.tagName !== 'IMG') return;
            // The stashed <img> already carries its authored class/style. Carry
            // back ONLY the inline-style properties this layer edits — never the
            // inlined svg's own root class/style (e.g. svglite's class="svglite",
            // which the engine left in place because the img had none).
            var managed = extractManagedStyle(svg);
            var merged = mergeInlineStyle(img.getAttribute('style') || '', managed);
            if (merged) img.setAttribute('style', merged); else img.removeAttribute('style');
            img.classList.remove('presto-wysiwyg-selected', 'presto-wysiwyg-hover', 'presto-wysiwyg-editing');
            cleanEmptyClassAttr(img);
            svg.replaceWith(img);
        });
    }

    function restoreExternalTables(root) {
        root.querySelectorAll('[data-ws-original-html]').forEach(function (el) {
            el.innerHTML = el.getAttribute('data-ws-original-html');
            el.removeAttribute('data-ws-original-html');
            el.classList.remove('ws-table-load-error');
            cleanEmptyClassAttr(el);
        });
    }

    function katexUserStyle(el) {
        // The inline style / non-structural classes a user applied to a rendered
        // equation (move/format), with katex's own structure + our chrome removed.
        var style = el.getAttribute('style') || '';
        var classes = [];
        el.classList.forEach(function (c) {
            if (c === 'katex' || c === 'katex-display') return;
            if (c.indexOf('presto-wysiwyg') === 0) return;
            classes.push(c);
        });
        return { style: style, classes: classes };
    }

    function isEquationWrapper(parent, katexEl, tag) {
        // A bare span/div whose only content is this equation — either one we
        // emitted on a previous serialize (re-rendered by KaTeX) or an authored
        // wrapper around the equation. Either way, merge into it rather than nest.
        return !!parent && parent.tagName === tag &&
            parent.children.length === 1 && parent.children[0] === katexEl &&
            parent.textContent.trim() === katexEl.textContent.trim();
    }

    function restoreOneKaTeX(el, delimiter, wrapperTag) {
        var annotation = el.querySelector('annotation[encoding="application/x-tex"]');
        if (!annotation) return false;   // caller strips orphans
        var tex = document.createTextNode(delimiter + annotation.textContent + delimiter);
        var carry = katexUserStyle(el);
        var parent = el.parentElement;
        if (isEquationWrapper(parent, el, wrapperTag)) {
            // Overlay any freshly-applied style/classes onto the existing wrapper,
            // then drop back to the authored `$tex$` form. Keeps serialization
            // idempotent across edit → serialize → re-render → edit → serialize.
            if (carry.style) {
                var merged = mergeInlineStyle(parent.getAttribute('style') || '', carry.style);
                if (merged) parent.setAttribute('style', merged); else parent.removeAttribute('style');
            }
            carry.classes.forEach(function (c) { parent.classList.add(c); });
            cleanEmptyClassAttr(parent);
            el.replaceWith(tex);
        } else if (carry.style || carry.classes.length) {
            // Preserve the user's move/format by wrapping the authored TeX so the
            // engine re-renders inside the wrapper (position/size round-trip).
            var wrapper = document.createElement(wrapperTag.toLowerCase());
            if (carry.style) wrapper.setAttribute('style', carry.style);
            carry.classes.forEach(function (c) { wrapper.classList.add(c); });
            wrapper.appendChild(tex);
            el.replaceWith(wrapper);
        } else {
            el.replaceWith(tex);
        }
        return true;
    }

    function restoreKaTeX(root) {
        // KaTeX auto-render replaced `$...$` / `$$...$$` text with rendered spans.
        // The authored TeX survives inside <annotation encoding="application/x-tex">.
        root.querySelectorAll('.katex-display').forEach(function (el) {
            restoreOneKaTeX(el, '$$', 'DIV');
        });
        root.querySelectorAll('.katex').forEach(function (el) {
            if (el.closest('.katex-display')) return;
            restoreOneKaTeX(el, '$', 'SPAN');
        });
        // Defense in depth: any katex left without an annotation is unrecoverable
        // span soup — never let it reach the deck source. Drop it and flag a warning.
        var orphans = root.querySelectorAll('.katex-display, .katex');
        if (orphans.length) {
            orphans.forEach(function (el) { el.remove(); });
            state.pendingSerializeWarning = 'Removed unrecoverable equation markup. Re-enter the equation with Edit TeX or in the source.';
        }
    }

    function serializeSection(section) {
        state.pendingSerializeWarning = null;
        var clone = section.cloneNode(true);
        stripWysiwygArtifacts(clone);
        restoreInlinedSVGs(clone);
        restoreExternalTables(clone);
        restoreKaTeX(clone);
        stripEngineArtifacts(clone);
        return clone.innerHTML;
    }

    // Re-run the source-restore passes on already-wysiwyg-stripped live HTML.
    // Lets a snapshot compute its `source` lazily from `live` (one clone total).
    function sourceFromLive(live) {
        state.pendingSerializeWarning = null;
        var box = document.createElement('div');
        box.innerHTML = live;
        restoreInlinedSVGs(box);
        restoreExternalTables(box);
        restoreKaTeX(box);
        stripEngineArtifacts(box);
        return box.innerHTML;
    }

    function liveHTML(section) {
        // Snapshot of the live DOM (engine artifacts intact) used for undo/redo,
        // so slides with async content never need re-transformation.
        var clone = section.cloneNode(true);
        stripWysiwygArtifacts(clone);
        return clone.innerHTML;
    }

    /* ===== Undo / redo ===== */

    function historyFor(index) {
        if (!state.history[index]) state.history[index] = { undo: [], redo: [] };
        return state.history[index];
    }

    function captureSnapshot(section) {
        // One clone: read the live HTML, then derive `source` lazily (only undo/
        // redo ever needs it, so most commit snapshots never serialize twice).
        var live = liveHTML(section);
        var source = null;
        var hasSource = false;
        return {
            live: live,
            get source() {
                if (!hasSource) { source = sourceFromLive(live); hasSource = true; }
                return source;
            }
        };
    }

    // The one undo history, per slide (100 entries each). Two entry kinds:
    //   visual   { kind, snap, label, origin: 'visual', restore } — the slide
    //            body before a visual edit (live DOM, so async content never
    //            re-renders); restore is the authored section it equals when
    //            the entry sits directly on an outside change
    //   external { kind, before, after, origin: 'source'|'refine', label, id,
    //            prompt, at } — full authored sections around a committed
    //            source edit or a Refine result (recordExternalChange)
    function pushEntry(index, entry) {
        var h = historyFor(index);
        if (!entry.at) entry.at = Date.now();
        h.undo.push(entry);
        if (h.undo.length > MAX_UNDO) h.undo.shift();
        h.redo = [];
        updateToolbarState();
        postUndoState();
    }

    function pushUndo(index, snapshot, label) {
        // Directly on top of an outside change the slide is that change's
        // authored section. Undoing back to it saves those exact bytes: a
        // re-serialised body differs, and Swift would then refuse to undo
        // the outside change ("This slide changed since").
        var h = historyFor(index);
        var top = h.undo[h.undo.length - 1];
        pushEntry(index, { kind: 'visual', snap: snapshot, label: label || 'Edit', origin: 'visual',
                           restore: top && top.kind === 'external' ? top.after : null });
    }

    function applySnapshot(index, snapshot, restoreSection) {
        var section = WS.slides[index];
        if (!section) return;
        deselect();
        section.innerHTML = snapshot.live;
        var maxStep = refreshSteps(section);
        if (typeof WS.goToSlide === 'function' && index === WS.currentIndex) {
            WS.goToSlide(index, Math.min(WS.currentStep || 0, maxStep || 0));
        }
        scheduleThumbnail(index);
        if (restoreSection) saveSection(index, sectionBody(restoreSection), { sectionHTML: restoreSection });
        else saveSection(index, snapshot.source);
    }

    function sectionBody(sectionHTML) {
        var template = document.createElement('template');
        template.innerHTML = String(sectionHTML || '').trim();
        var section = template.content.firstElementChild;
        return section ? section.innerHTML : String(sectionHTML || '');
    }

    function renderMathIfNeeded(section) {
        if (!section || typeof window.renderMathInElement !== 'function') return;
        if ((section.textContent || '').indexOf('$') < 0) return;
        try {
            window.renderMathInElement(section, window.__wsKatexOptions || {
                delimiters: [
                    { left: '$$', right: '$$', display: true },
                    { left: '$', right: '$', display: false }
                ],
                throwOnError: false
            });
        } catch (e) { /* a maths error must not break the slide */ }
    }

    // Put a full authored section back into the live deck. Shell's patcher
    // with origin 'history' neither clears nor records; when it refuses (slides
    // with external tables or inlined SVGs) the section is patched in place.
    function installSection(index, sectionHTML) {
        if (!wsReady() || !WS.slides[index]) return false;
        var ok = false;
        if (typeof window.replacePrestoEditSlideHTML === 'function') {
            try { ok = window.replacePrestoEditSlideHTML(index, sectionHTML, { origin: 'history' }) === true; } catch (e) { ok = false; }
        }
        if (!ok) ok = installSectionInPlace(index, sectionHTML);
        if (!ok) return false;
        var section = WS.slides[index];
        renderMathIfNeeded(section);
        refreshSteps(section);
        scheduleThumbnail(index);
        return true;
    }

    var KEEP_SECTION_ATTRS = { style: true, 'data-ws-index': true, 'data-ws-main-number': true };

    function installSectionInPlace(index, sectionHTML) {
        var template = document.createElement('template');
        template.innerHTML = String(sectionHTML || '').trim();
        var replacement = template.content.firstElementChild;
        var section = WS.slides[index];
        if (!replacement || replacement.tagName !== 'SECTION' || !section) return false;
        var active = section.classList.contains('ws-active');
        Array.prototype.slice.call(section.attributes).forEach(function (attr) {
            if (!KEEP_SECTION_ATTRS[attr.name] && !replacement.hasAttribute(attr.name)) section.removeAttribute(attr.name);
        });
        Array.prototype.slice.call(replacement.attributes).forEach(function (attr) {
            if (!KEEP_SECTION_ATTRS[attr.name]) section.setAttribute(attr.name, attr.value);
        });
        section.classList.toggle('ws-active', active);
        section.innerHTML = replacement.innerHTML;
        return true;
    }

    function applyEntry(index, entry, direction) {
        var h = historyFor(index);
        var section = WS.slides[index];
        if (!section) return false;
        var counterStack = direction === 'undo' ? h.redo : h.undo;
        if (entry.kind !== 'external') {
            // The counter entry keeps `restore` for its own way back down.
            counterStack.push({ kind: 'visual', snap: captureSnapshot(section), label: entry.label, origin: 'visual',
                                at: entry.at, restore: entry.restore || null });
            applySnapshot(index, entry.snap, direction === 'undo' ? entry.restore : null);
            return true;
        }
        var install = direction === 'undo' ? entry.before : entry.after;
        var expect = direction === 'undo' ? entry.after : entry.before;
        deselect();
        if (!installSection(index, install)) {
            setStatus('error', 'Could not restore this slide');
            return false;
        }
        counterStack.push(entry);
        var saveId = saveSection(index, sectionBody(install), {
            sectionHTML: install,
            expectSectionHTML: expect
        });
        state.pendingHistory[saveId] = { index: index, entry: entry, direction: direction };
        return true;
    }

    // Swift refused an outside-change undo/redo (its copy of the slide moved
    // on): put the slide and both stacks back exactly as they were.
    function rollbackHistorySave(saveId) {
        var pending = state.pendingHistory[saveId];
        if (!pending) return false;
        delete state.pendingHistory[saveId];
        var h = historyFor(pending.index);
        var landed = pending.direction === 'undo' ? h.redo : h.undo;
        var origin = pending.direction === 'undo' ? h.undo : h.redo;
        var at = landed.lastIndexOf(pending.entry);
        if (at >= 0) landed.splice(at, 1);
        origin.push(pending.entry);
        installSection(pending.index, pending.direction === 'undo' ? pending.entry.after : pending.entry.before);
        updateToolbarState();
        postUndoState();
        return true;
    }

    function stepHistory(direction) {
        if (!wsReady()) return false;
        flushBurst();
        closePopover();
        // Finishing a text session can create the first undo entry; new typing
        // clears redo, so check the stacks only after that commit.
        endTextEdit(true);
        var index = WS.currentIndex;
        var h = historyFor(index);
        var stack = direction === 'undo' ? h.undo : h.redo;
        if (stack.length === 0) return false;
        if (!WS.slides[index]) return false;
        var entry = stack.pop();
        var ok = applyEntry(index, entry, direction);
        if (!ok) stack.push(entry);
        updateToolbarState();
        postUndoState();
        return ok;
    }

    function undo() { return stepHistory('undo'); }
    function redo() { return stepHistory('redo'); }

    function historyState(index) {
        var i = typeof index === 'number' ? index : (wsReady() ? WS.currentIndex : -1);
        var h = i >= 0 && state.history[i] ? state.history[i] : { undo: [], redo: [] };
        var u = h.undo[h.undo.length - 1];
        var r = h.redo[h.redo.length - 1];
        return {
            canUndo: !!u,
            canRedo: !!r,
            undoLabel: u ? 'Undo ' + u.label : 'Undo',
            redoLabel: r ? 'Redo ' + r.label : 'Redo'
        };
    }

    function postUndoState() {
        if (!state.enabled && !state.installed) return;
        var s = historyState();
        // The Refine count is part of the key: two Refines in a row leave
        // the labels unchanged, but the inspector's "n earlier changes"
        // (refreshed by Presto on each undoState) must move.
        var key = JSON.stringify(s) + '|' + refineHistory().length;
        if (key === state.lastUndoStateMsg) return;
        state.lastUndoStateMsg = key;
        post({ type: 'undoState', canUndo: s.canUndo, canRedo: s.canRedo, undoLabel: s.undoLabel, redoLabel: s.redoLabel });
    }

    function refineHistory(index) {
        var i = typeof index === 'number' ? index : (wsReady() ? WS.currentIndex : -1);
        var h = state.history[i];
        if (!h) return [];
        return h.undo.filter(function (entry) { return entry.origin === 'refine'; })
            .map(function (entry) { return { id: entry.id, prompt: entry.prompt || '', at: entry.at }; });
    }

    // Slide i's DOM is replaced from outside: end, without committing, the
    // coalesced burst, equation editor and text session that belong to it.
    function dropLiveEditsOn(i) {
        if (state.coalesce && state.coalesce.index === i) {
            if (state.coalesce.timer) clearTimeout(state.coalesce.timer);
            state.coalesce = null;
        }
        if (state.equation && state.equation.index === i) closePopover();
        if (state.textEditing && state.textEditIndex === i) abandonTextEdit();
    }

    // Shell calls this when it patches a slide from a committed source edit or
    // a Refine result (instead of notifyExternalReplace): earlier history stays,
    // and ⌘Z can now undo the outside change.
    function recordExternalChange(change) {
        change = change || {};
        if (!wsReady()) return false;
        var i = typeof change.index === 'number' ? change.index : parseInt(change.index, 10);
        if (!isFinite(i) || i < 0 || i >= WS.slides.length) return false;
        if (typeof change.beforeHTML !== 'string' || typeof change.afterHTML !== 'string') return false;
        // The slide's DOM was already replaced: drop (never commit) whatever
        // was live on it, or a stale save would overwrite the outside change.
        dropLiveEditsOn(i);
        if (state.selected && (!state.selected.isConnected || state.selectedIndex === i)) deselect();
        scheduleThumbnail(i);
        if (change.beforeHTML === change.afterHTML) return false;
        var origin = change.origin === 'refine' ? 'refine' : 'source';
        state.externalCounter += 1;
        pushEntry(i, {
            kind: 'external',
            before: change.beforeHTML,
            after: change.afterHTML,
            origin: origin,
            label: change.label || (origin === 'refine' ? 'Refine' : 'Source Edit'),
            id: change.id || (origin + '-' + state.externalCounter),
            prompt: change.prompt || null,
            at: Date.now()
        });
        return true;
    }

    /* ===== Saving ===== */

    // Returns the save id. `extra` carries the outside-change undo fields.
    function saveSection(index, sourceHTML, extra) {
        state.saveCounter += 1;
        if (state.pendingSerializeWarning) {
            setStatus('error', state.pendingSerializeWarning);
            state.pendingSerializeWarning = null;
        } else {
            setStatus('saving', 'Saving…');
        }
        var msg = {
            type: 'wysiwygChanged',
            index: index,
            html: sourceHTML,
            saveId: state.saveCounter
        };
        if (extra) {
            Object.keys(extra).forEach(function (key) { msg[key] = extra[key]; });
        }
        post(msg);
        return state.saveCounter;
    }

    // Serialize + save a specific section against a captured index. Used by every
    // commit path so navigation between gesture-start and gesture-end can never
    // write typed content to the wrong slide.
    function saveCapturedSection(index, section) {
        saveSection(index, serializeSection(section));
        scheduleThumbnail(index);
    }

    // Commit a captured gesture/session against its owning slide; discard (with a
    // status warning) if that slide was replaced or detached in the meantime.
    function commitOwnedSection(index, section, snapshot, label) {
        if (!section || !snapshot) return false;
        if (!section.isConnected || (wsReady() && WS.slides[index] !== section)) {
            setStatus('error', 'Edit discarded: the slide changed underneath');
            return false;
        }
        pushUndo(index, snapshot, label);
        saveCapturedSection(index, section);
        return true;
    }

    function saveCurrentSlide() {
        var section = activeSlide();
        if (!section) return;
        saveCapturedSection(WS.currentIndex, section);
    }

    // `mutate` runs a DOM change on the active slide as one undoable, saved
    // step; `label` names it in the Edit menu ("Undo <label>").
    function commit(mutate, label) {
        return commitOn(wsReady() ? WS.currentIndex : -1, mutate, label);
    }

    function commitOn(index, mutate, label) {
        // Any discrete commit closes an in-flight coalesced burst first so its
        // pre-burst snapshot lands as its own undo step, in order.
        flushBurst();
        var section = wsReady() ? WS.slides[index] : null;
        if (!section) return false;
        var snapshot = captureSnapshot(section);
        var changed = mutate(section);
        if (changed === false) return false;
        pushUndo(index, snapshot, label);
        saveCapturedSection(index, section);
        return true;
    }

    var BURST_LABELS = { nudge: 'Move', fontstep: 'Font Size', color: 'Colour', boxcolor: 'Box Colour' };

    /* ===== Coalescing: nudge + font-step bursts ===== */

    // Begin (or extend) a burst of the given `kind` on the active slide. The
    // FIRST call captures one pre-burst snapshot; later calls of the same kind on
    // the same slide reuse it. A different kind/slide flushes the old burst first.
    // Returns the section to mutate, or null if there is no active slide.
    function beginOrExtendBurst(kind) {
        var section = activeSlide();
        if (!section) return null;
        var index = WS.currentIndex;
        var c = state.coalesce;
        if (c && (c.kind !== kind || c.section !== section || c.index !== index)) {
            flushBurst();
        }
        if (!state.coalesce) {
            state.coalesce = {
                kind: kind,
                index: index,
                section: section,
                snapshot: captureSnapshot(section),
                timer: null
            };
        }
        var burst = state.coalesce;
        if (burst.timer) clearTimeout(burst.timer);
        burst.timer = setTimeout(flushBurst, COALESCE_IDLE_MS);
        return section;
    }

    // Commit an in-flight burst as ONE undo entry + ONE save against its owning
    // slide. A no-op burst (nothing actually moved) is dropped silently.
    function flushBurst() {
        var burst = state.coalesce;
        if (!burst) return;
        state.coalesce = null;
        if (burst.timer) { clearTimeout(burst.timer); burst.timer = null; }
        if (!burst.section || !burst.section.isConnected) return;
        if (liveHTML(burst.section) === burst.snapshot.live) return;
        commitOwnedSection(burst.index, burst.section, burst.snapshot, BURST_LABELS[burst.kind]);
    }

    // The edit chrome's rail first (BACKLOG 13: the window.* hooks this used
    // were never defined, so thumbnails went stale), then any legacy hook,
    // window.* or the engine's WS.* copy.
    function updateSidebarThumbnail(index, section) {
        if (window.PrestoEditChrome && typeof PrestoEditChrome.refreshThumbnail === 'function') {
            PrestoEditChrome.refreshThumbnail(index);
            return;
        }
        var ws = window.WS || {};
        if (typeof window.updatePrestoEditSidebarThumbnail === 'function') {
            window.updatePrestoEditSidebarThumbnail(index, section);
        } else if (typeof ws.updatePrestoEditSidebarThumbnail === 'function') {
            ws.updatePrestoEditSidebarThumbnail(index, section);
        } else if (typeof ws.rebuildPrestoEditSidebar === 'function') {
            ws.rebuildPrestoEditSidebar();
        } else if (typeof window.rebuildPrestoEditSidebar === 'function') {
            window.rebuildPrestoEditSidebar();
        }
    }

    // Coalesce thumbnail rebuilds to gesture-idle: a full slide clone + forced
    // layout on every keypress/toolbar click is wasted work for a 90px preview
    // the user isn't watching mid-gesture. Flushed eagerly on navigation.
    function scheduleThumbnail(index) {
        state.thumb.pending[index] = true;
        if (state.thumb.rafId != null) return;
        if (typeof requestAnimationFrame !== 'function') { flushThumbnails(); return; }
        state.thumb.rafId = requestAnimationFrame(flushThumbnails);
    }

    function flushThumbnails() {
        if (state.thumb.rafId != null && typeof cancelAnimationFrame === 'function') {
            cancelAnimationFrame(state.thumb.rafId);
        }
        state.thumb.rafId = null;
        var pending = state.thumb.pending;
        state.thumb.pending = {};
        Object.keys(pending).forEach(function (key) {
            var index = parseInt(key, 10);
            var section = wsReady() ? WS.slides[index] : null;
            updateSidebarThumbnail(index, section);
        });
    }

    /* ===== Selection model ===== */

    function isChrome(el) {
        return !!(el.closest && el.closest('.presto-wysiwyg-chrome'));
    }

    function isIgnored(el) {
        return !!(el.closest && el.closest(IGNORE_SELECTOR));
    }

    function isAtomic(el) {
        return !!(el.matches && el.matches(ATOM_SELECTOR));
    }

    function pickSelectable(target, slide) {
        if (!target || !slide.contains(target) || target === slide) return null;
        if (isIgnored(target)) return null;

        var el = target;
        // Climb out of foreign subtrees to their container.
        var foreign = el.closest('[data-ws-original-html], svg[data-ws-original-img], .katex-display');
        if (foreign && slide.contains(foreign)) el = foreign;
        else {
            var katex = el.closest('.katex');
            if (katex && slide.contains(katex)) el = katex;
        }

        // Deepest atom or leaf at/above the (possibly climbed) element.
        var leaf = el.closest(ATOM_SELECTOR + ', ' + LEAF_SELECTOR);
        if (leaf && slide.contains(leaf) && leaf !== slide) return leaf;

        // Fall back to the outermost block: the ancestor that is a direct child
        // of the slide (covers bare divs / layout wrappers).
        var node = el;
        while (node && node.parentElement && node.parentElement !== slide) {
            node = node.parentElement;
        }
        if (node && node.parentElement === slide) return node;
        return null;
    }

    function isHiddenAtCurrentStep(el) {
        return !!(el.closest && el.closest('[data-step]:not(.ws-visible)'));
    }

    function selectableStackAtPoint(target, clientX, clientY, slide) {
        var hits = [target];
        if (document.elementsFromPoint) {
            hits = hits.concat(document.elementsFromPoint(clientX, clientY));
        }
        var stack = [];
        hits.forEach(function (hit) {
            var item = pickSelectable(hit, slide);
            if (!item || stack.indexOf(item) >= 0) return;
            var sameObject = stack.some(function (existing) {
                return existing.contains(item) || item.contains(existing);
            });
            if (!sameObject) stack.push(item);
        });
        // Objects visible at the current step outrank future-step ghosts, so
        // overlapping reveals edit what the audience currently sees; fully
        // hidden ghosts (0% opacity) are not selectable at all.
        var visible = [];
        var ghosted = [];
        stack.forEach(function (item) {
            if (!isHiddenAtCurrentStep(item)) visible.push(item);
            else if (state.hiddenStepOpacity > 0) ghosted.push(item);
        });
        return visible.concat(ghosted);
    }

    function canTextEdit(el) {
        if (isAtomic(el)) return false;
        if (el.closest('[data-ws-original-html]')) return false;
        return true;
    }

    function isEquation(el) {
        return !!(el && el.matches && el.matches('.katex, .katex-display'));
    }

    function isLink(el) {
        return !!(el && el.matches && el.matches('a[href]'));
    }

    function isBoxLike(el) {
        if (!el || !el.matches || el.classList.contains('slide')) return false;
        if (el.matches(BOX_SELECTOR)) return true;
        // Deck-specific boxes: any class ending in "box" (eqbox, keybox…).
        return Array.prototype.some.call(el.classList, function (c) { return /box$/i.test(c); });
    }

    // The container whose background the Box row recolours: the selection
    // itself, or the box an equation sits in.
    function boxTarget(el) {
        if (!el) return null;
        if (isBoxLike(el)) return el;
        if (!isEquation(el)) return null;
        var slide = el.closest('.slide');
        var node = el.parentElement;
        while (node && node !== slide) {
            if (isBoxLike(node)) return node;
            node = node.parentElement;
        }
        return null;
    }

    // What the format bar offers: text, list, box, image, table, equation, link.
    function selectionKind(el) {
        if (!el || !el.matches) return null;
        if (isLink(el)) return 'link';
        if (isEquation(el)) return 'equation';
        if (el.matches('[data-table-src], .table-container, table')) return 'table';
        if (el.matches('img, svg, video')) return 'image';
        if (el.matches('li')) return 'list';
        if (isBoxLike(el)) return 'box';
        return 'text';
    }

    function announceSelection() {
        var el = state.selected && state.selected.isConnected ? state.selected : null;
        try {
            document.dispatchEvent(new CustomEvent('presto:selection', {
                detail: {
                    kind: el ? selectionKind(el) : null,
                    element: el,
                    editing: !!(el && state.textEditing),
                    index: el ? state.selectedIndex : -1
                }
            }));
        } catch (e) { /* CustomEvent unavailable */ }
    }

    function select(el) {
        if (state.selected === el) { refreshChrome(); return; }
        deselect();
        state.selected = el;
        state.selectedIndex = wsReady() ? WS.currentIndex : -1;
        el.classList.add('presto-wysiwyg-selected');
        refreshChrome(true);   // a new selection always re-anchors the toolbar
        postEditingState();
    }

    function selectFromStack(el, count) {
        select(el);
        state.overlapCount = count;
        refreshChrome();
    }

    function deselect() {
        flushBurst();
        closePopover();
        if (state.textEditing) endTextEdit(true);
        if (state.selected) {
            state.selected.classList.remove('presto-wysiwyg-selected');
            cleanEmptyClassAttr(state.selected);
            state.selected = null;
        }
        state.selectedIndex = -1;
        state.overlapCount = 0;
        setHover(null);
        refreshChrome();
        postEditingState();
    }

    function setHover(el) {
        if (state.hover === el) return;
        if (state.hover) {
            state.hover.classList.remove('presto-wysiwyg-hover');
            cleanEmptyClassAttr(state.hover);
        }
        state.hover = el;
        if (el) el.classList.add('presto-wysiwyg-hover');
    }

    // textEditing is also true while an in-page field has focus, so Swift
    // forwards ⌘↩, Escape and arrows to the page instead of acting on them.
    function postEditingState() {
        var kind = state.selected ? selectionKind(state.selected) : null;
        var textEditing = state.textEditing || state.chromeTextFocus;
        var msg = JSON.stringify({ t: textEditing, s: !!state.selected, k: kind });
        if (msg === state.lastEditingStateMsg) return;
        state.lastEditingStateMsg = msg;
        post({
            type: 'wysiwygEditingState',
            textEditing: textEditing,
            hasSelection: !!state.selected,
            selectionKind: kind
        });
    }

    function isTextField(node) {
        return !!(node && node.matches && (node.matches('input, textarea') || node.isContentEditable));
    }

    function onFocusChange() {
        var active = document.activeElement;
        var focused = !!(active && isTextField(active) && active.closest &&
            active.closest('.presto-wysiwyg-chrome, .presto-edit-chrome'));
        if (focused === state.chromeTextFocus) return;
        state.chromeTextFocus = focused;
        postEditingState();
    }

    function onFocusIn() { onFocusChange(); }
    function onFocusOut() { setTimeout(onFocusChange, 0); }

    /* ===== Text editing ===== */

    function lockNestedAtoms(el) {
        // Make rendered atoms (KaTeX/SVG/table internals) indivisible for the
        // session: WebKit skips the caret over them and a cross-boundary delete
        // removes the whole atom, so partial-delete can't leave span soup and
        // typing can't sink into unrecoverable math internals.
        el.querySelectorAll(ATOM_SELECTOR + ', [data-ws-original-html]').forEach(function (a) {
            if (a === el || a.hasAttribute('contenteditable')) return;
            a.setAttribute('contenteditable', 'false');
            a.setAttribute('data-pw-atom-lock', '');
        });
    }

    function unlockNestedAtoms(root) {
        if (!root || !root.querySelectorAll) return;
        root.querySelectorAll('[data-pw-atom-lock]').forEach(function (a) {
            a.removeAttribute('contenteditable');
            a.removeAttribute('data-pw-atom-lock');
        });
    }

    function clearSessionState() {
        state.textEditing = false;
        state.textEditSnapshot = null;
        state.textEditSection = null;
        state.textEditIndex = -1;
        state.newBox = null;
        state.newBoxTyped = false;
    }

    // A new text box whose first session ends without real typed content is
    // an accident (a stray double-click): it leaves no trace in the deck.
    function newBoxIsEmpty(box) {
        var text = (box.textContent || '').replace(/​/g, '').trim();
        return !text || (!state.newBoxTyped && text === TEXT_BOX_PLACEHOLDER);
    }

    // Word characters for double-click expansion: letters (incl. Latin-1/extended
    // accents), digits, underscore, and intra-word apostrophes/hyphens.
    var WORD_CHAR = /[A-Za-z0-9_À-ɏ’'’-]/;

    // Place the caret from a click point, then expand it to the word under the
    // cursor (Keynote/Word double-click behavior). Falls back to a plain caret on
    // whitespace/punctuation or where boundaries can't be computed.
    function selectWordAt(el, clientX, clientY) {
        if (!document.caretRangeFromPoint) return false;
        var range = document.caretRangeFromPoint(clientX, clientY);
        if (!range || !el.contains(range.startContainer)) return false;
        var sel = window.getSelection();
        var node = range.startContainer;
        if (node.nodeType === 3) {
            var text = node.textContent;
            var start = range.startOffset;
            var end = start;
            while (start > 0 && WORD_CHAR.test(text.charAt(start - 1))) start--;
            while (end < text.length && WORD_CHAR.test(text.charAt(end))) end++;
            if (end > start) {
                var word = document.createRange();
                word.setStart(node, start);
                word.setEnd(node, end);
                sel.removeAllRanges();
                sel.addRange(word);
                return true;
            }
        }
        sel.removeAllRanges();
        sel.addRange(range);
        return true;
    }

    // Select the whole block's contents (triple-click behavior).
    function selectBlockContents(el) {
        var sel = window.getSelection();
        sel.removeAllRanges();
        var range = document.createRange();
        range.selectNodeContents(el);
        sel.addRange(range);
    }

    function beginTextEdit(el, clientX, clientY) {
        if (!el || !canTextEdit(el)) return false;
        if (state.textEditing && state.selected === el) return true;
        flushBurst();
        endTextEdit(true);
        select(el);
        var section = activeSlide();
        // Capture the owning section + index NOW. endTextEdit commits against
        // these, never activeSlide()/WS.currentIndex, so navigation mid-session
        // can't misattribute the typed text to a different slide.
        state.textEditSection = section;
        state.textEditIndex = wsReady() ? WS.currentIndex : -1;
        state.textEditSnapshot = section ? captureSnapshot(section) : null;
        state.textEditing = true;
        el.classList.add('presto-wysiwyg-editing');
        el.setAttribute('contenteditable', 'true');
        lockNestedAtoms(el);
        el.focus();
        if (clientX !== undefined) selectWordAt(el, clientX, clientY);
        refreshChrome();
        postEditingState();
        return true;
    }

    function endTextEdit(keepSelection) {
        if (!state.textEditing) return;
        var el = state.selected;
        var section = state.textEditSection;
        var index = state.textEditIndex;
        var snapshot = state.textEditSnapshot;
        var newBox = state.newBox && state.newBox === el ? el : null;
        var discardBox = !!(newBox && newBoxIsEmpty(newBox));
        clearSessionState();
        if (el) {
            unlockNestedAtoms(el);
            el.removeAttribute('contenteditable');
            el.removeAttribute('spellcheck');
            el.classList.remove('presto-wysiwyg-editing');
            cleanEmptyClassAttr(el);
            el.blur();
        }
        if (discardBox) {
            // Back to the state before the box existed: no undo step, no save.
            newBox.remove();
            state.selected = null;
            state.selectedIndex = -1;
            refreshChrome();
            postEditingState();
            return;
        }
        if (section && snapshot && liveHTML(section) !== snapshot.live) {
            // Commit against the OWNING slide (captured at beginTextEdit), never
            // activeSlide()/WS.currentIndex — navigation mid-session must not
            // misattribute the typed text. Discards if the slide was replaced.
            commitOwnedSection(index, section, snapshot, newBox ? 'Add Text Box' : 'Typing');
        }
        if (!keepSelection) deselect();
        refreshChrome();
        postEditingState();
    }

    // End a session WITHOUT committing (external replacement is about to overwrite
    // this slide's content, so a stale save would clobber it).
    function abandonTextEdit() {
        if (!state.textEditing) return;
        var el = state.selected;
        clearSessionState();
        if (el) {
            unlockNestedAtoms(el);
            el.removeAttribute('contenteditable');
            el.removeAttribute('spellcheck');
            el.classList.remove('presto-wysiwyg-editing');
            cleanEmptyClassAttr(el);
            if (el.blur) el.blur();
        }
        refreshChrome();
        postEditingState();
    }

    /* ===== Structural operations ===== */

    // Adds a text box and starts typing into it. Nothing is committed or saved
    // until the session ends with real content (BACKLOG 9): the session's undo
    // snapshot is the slide WITHOUT the box, so the box and its text are one
    // "Add Text Box" step, and an untouched box simply disappears.
    // opts.center: centre the box on (slideX, slideY) instead of its corner.
    function addTextBoxAt(slideX, slideY, opts) {
        var section = activeSlide();
        if (!section) return null;
        flushBurst();
        closePopover();
        if (state.textEditing) endTextEdit(true);
        post({ type: 'wysiwygWillEdit' });
        var snapshot = captureSnapshot(section);
        var created = document.createElement('div');
        created.className = 'text-box';
        created.setAttribute('style',
            'position: absolute; left: ' + Math.round(slideX) + 'px; top: ' + Math.round(slideY) +
            'px; max-width: 600px; min-width: 40px;');
        created.textContent = TEXT_BOX_PLACEHOLDER;
        section.appendChild(created);
        if (opts && opts.center) {
            var scale = slideScale(section);
            var r = created.getBoundingClientRect();
            var w = r.width / scale;
            var h = r.height / scale;
            var left = Math.max(0, Math.min(SLIDE_WIDTH - w, slideX - w / 2));
            var top = Math.max(0, Math.min(SLIDE_HEIGHT - h, slideY - h / 2));
            created.style.left = Math.round(left) + 'px';
            created.style.top = Math.round(top) + 'px';
        }
        select(created);
        beginTextEdit(created);
        state.textEditSnapshot = snapshot;
        state.newBox = created;
        state.newBoxTyped = false;
        // Select the placeholder so typing replaces it.
        var sel = window.getSelection();
        sel.removeAllRanges();
        var range = document.createRange();
        range.selectNodeContents(created);
        sel.addRange(range);
        return created;
    }

    // The visible Text box action (native toolbar button and ⌥⌘T).
    function addTextBoxCentered() {
        if (!state.enabled || overviewActive()) return null;
        return addTextBoxAt(SLIDE_WIDTH / 2, SLIDE_HEIGHT / 2, { center: true });
    }

    function deleteSelected() {
        var el = state.selected;
        if (!el) return false;
        endTextEdit(true);
        if (!el.isConnected) { deselect(); return false; }
        var section = activeSlide();
        var ok = commit(function () {
            deselect();
            el.remove();
        }, 'Delete');
        if (ok) refreshSteps(section);
        return ok;
    }

    function duplicateSelected() {
        var el = state.selected;
        if (!el) return false;
        endTextEdit(true);
        var copy = null;
        var ok = commit(function () {
            copy = el.cloneNode(true);
            copy.classList.remove('presto-wysiwyg-selected', 'presto-wysiwyg-hover', 'presto-wysiwyg-editing');
            copy.removeAttribute('contenteditable');
            copy.removeAttribute('data-pw-atom-lock');
            cleanEmptyClassAttr(copy);
            // Offset by +24 slide px, composing with any authored non-px unit
            // (calc/%/em) instead of parseFloat-ing it into a wrong px value.
            var a = analyzeTranslate(copy);
            if (copy.style.position === 'absolute') {
                copy.style.left = calcAddLength(copy.style.left, 24);
                copy.style.top = calcAddLength(copy.style.top, 24);
            } else if (a.kind === 'px') {
                setTranslate(copy, a.x + 24, a.y + 24);
            } else if (a.kind === 'unit') {
                copy.style.translate = calcAddLength(a.x, 24) + ' ' + calcAddLength(a.y, 24);
                cleanEmptyStyleAttr(copy);
            }
            if (copy.hasAttribute('data-ws-auto-step')) {
                copy.removeAttribute('data-step');
                copy.removeAttribute('data-ws-auto-step');
            }
            el.insertAdjacentElement('afterend', copy);
        }, 'Duplicate');
        if (ok && copy) {
            refreshSteps(activeSlide());
            select(copy);
        }
        return ok;
    }

    function makeBulletedList() {
        var el = state.selected;
        if (!el || el.matches('li') || !canTextEdit(el)) return false;
        endTextEdit(true);
        var item = null;
        var ok = commit(function () {
            if (!el.parentNode) return false;
            var list = document.createElement('ul');
            item = document.createElement('li');
            Array.prototype.slice.call(el.attributes).forEach(function (attr) {
                item.setAttribute(attr.name, attr.value);
            });
            item.classList.remove('presto-wysiwyg-selected', 'presto-wysiwyg-hover', 'presto-wysiwyg-editing');
            item.removeAttribute('contenteditable');
            item.removeAttribute('spellcheck');
            cleanEmptyClassAttr(item);
            item.innerHTML = el.innerHTML;
            el.replaceWith(list);
            list.appendChild(item);
        }, 'Bulleted List');
        if (ok && item) select(item);
        return ok;
    }

    function currentListItem() {
        var slide = activeSlide();
        if (!slide) return null;
        var item = null;
        if (state.textEditing) {
            var sel = window.getSelection();
            var anchor = sel && sel.anchorNode;
            var anchorEl = anchor && (anchor.nodeType === 1 ? anchor : anchor.parentElement);
            item = anchorEl && anchorEl.closest ? anchorEl.closest('li') : null;
            if (item && state.selected && state.selected.contains(anchor) && slide.contains(item)) return item;
        }
        if (state.selected && state.selected.closest) {
            item = state.selected.closest('li');
            if (item && slide.contains(item)) return item;
        }
        return null;
    }

    function directNestedList(item, tagName) {
        for (var i = item.children.length - 1; i >= 0; i--) {
            var child = item.children[i];
            if (child.tagName === tagName) return child;
        }
        return null;
    }

    function addListItem(splitAtCaret) {
        var item = currentListItem();
        if (!item || !item.parentElement.matches('ul, ol')) return false;
        var range = null;
        if (splitAtCaret) {
            var sel = window.getSelection();
            if (!sel || !sel.rangeCount) return false;
            range = sel.getRangeAt(0).cloneRange();
            if (!item.contains(range.startContainer) || !item.contains(range.endContainer)) return false;
        }
        endTextEdit(true);
        var next = document.createElement('li');
        // Carry presentation formatting, never identity or data bindings. An
        // authored step is shared with the new sibling; a step the engine
        // generated for a data-reveal="each" list is not: the list is
        // renumbered below, so the new item gets its own step (BACKLOG 16).
        var generated = item.hasAttribute('data-ws-auto-step');
        ['class', 'style', 'data-step'].forEach(function (name) {
            if (generated && name === 'data-step') return;
            if (item.hasAttribute(name)) next.setAttribute(name, item.getAttribute(name));
        });
        next.classList.remove('presto-wysiwyg-selected', 'presto-wysiwyg-hover', 'presto-wysiwyg-editing', 'ws-visible');
        cleanEmptyClassAttr(next);
        var revealList = item.closest('ul[data-reveal="each"], ol[data-reveal="each"]');
        var owner = activeSlide();
        var ok = commit(function () {
            if (range) {
                range.deleteContents();
                range.setEnd(item, item.childNodes.length);
                next.appendChild(range.extractContents());
                // Range extraction can clone ancestor wrappers at the split.
                next.querySelectorAll('[id]').forEach(function (el) {
                    if (document.getElementById(el.id)) el.removeAttribute('id');
                });
            }
            if (!next.hasChildNodes()) next.appendChild(document.createElement('br'));
            if (!item.hasChildNodes()) item.appendChild(document.createElement('br'));
            item.insertAdjacentElement('afterend', next);
        }, 'Add Bullet');
        if (ok) {
            if (revealList && owner && owner.contains(revealList)) {
                refreshSteps(owner);
                revealOwnStep(next);
            }
            beginTextEdit(next);
            var caret = document.createRange();
            caret.selectNodeContents(next);
            caret.collapse(true);
            var selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(caret);
        }
        return ok;
    }

    // Show the step an element appears at, so a new bullet in a reveal list
    // is not typed into while hidden.
    function revealOwnStep(el) {
        if (!wsReady() || !el || !el.hasAttribute('data-step') || el.classList.contains('ws-visible')) return;
        var range = typeof WS.getRevealRange === 'function' ? WS.getRevealRange(el, 'step') : parseRevealValue(el.getAttribute('data-step'));
        var index = WS.slides.indexOf(el.closest('.slide'));
        if (!range || index !== WS.currentIndex || range.start <= (WS.currentStep || 0)) return;
        state.suppressNavigate += 1;
        try { WS.goToSlide(index, range.start); } finally { state.suppressNavigate -= 1; }
    }

    function canIndentListItem(item) {
        return !!(item && item.parentElement && item.parentElement.matches('ul, ol') &&
            item.previousElementSibling && item.previousElementSibling.tagName === 'LI');
    }

    function canOutdentListItem(item) {
        var list = item && item.parentElement;
        return !!(list && list.matches('ul, ol') && list.parentElement && list.parentElement.tagName === 'LI');
    }

    function changeListLevel(mutate) {
        var item = currentListItem();
        if (!item) return false;
        var changed = false;
        if (state.textEditing && state.textEditSection && state.textEditSection.contains(item)) {
            changed = mutate(item) !== false;
        } else {
            changed = commit(function () { return mutate(item); }, 'List Level');
        }
        if (changed) refreshChrome();
        return changed;
    }

    function indentListItem() {
        return changeListLevel(function (item) {
            if (!canIndentListItem(item)) return false;
            var list = item.parentElement;
            var previous = item.previousElementSibling;
            var nested = directNestedList(previous, list.tagName);
            if (!nested) {
                nested = document.createElement(list.tagName.toLowerCase());
                previous.appendChild(nested);
            }
            nested.appendChild(item);
        });
    }

    function outdentListItem() {
        return changeListLevel(function (item) {
            if (!canOutdentListItem(item)) return false;
            var list = item.parentElement;
            var parentItem = list.parentElement;
            parentItem.insertAdjacentElement('afterend', item);
            if (!list.children.length) list.remove();
        });
    }

    function nudgeSelected(dx, dy) {
        var el = state.selected;
        if (!el) return false;
        return commit(function () {
            return translateBy(el, dx, dy);
        }, 'Move');
    }

    // Arrow-key nudge: applies the move immediately (cheap, composited) and folds
    // it into a coalesced burst so a rapid run of nudges is one undo step + one
    // save instead of one per 2px keypress. Flushed on keyup / idle / any commit.
    function coalescedNudge(dx, dy) {
        var el = state.selected;
        if (!el) return false;
        if (analyzeTranslate(el).kind === 'opaque') {
            setStatus('error', OPAQUE_MOVE_MESSAGE);
            return false;
        }
        var section = beginOrExtendBurst('nudge');
        if (!section) return false;
        translateBy(el, dx, dy);
        refreshChrome();
        return true;
    }

    /* ===== Formatting ===== */

    function formatTarget() {
        return state.selected;
    }

    function hasRangedTextSelection() {
        if (!state.textEditing) return false;
        var sel = window.getSelection();
        return !!(sel && sel.rangeCount > 0 && !sel.isCollapsed &&
                  state.selected && state.selected.contains(sel.anchorNode));
    }

    // True when a caret (collapsed or ranged) sits inside the running text
    // session. Inline B/I/U then route through execCommand — with a collapsed
    // caret WebKit sets the *typing* style so the next characters carry it,
    // instead of styling the whole block (Keynote/Pages behavior).
    function editingCaretInSelected() {
        if (!state.textEditing || !state.selected) return false;
        var sel = window.getSelection();
        return !!(sel && sel.rangeCount > 0 && state.selected.contains(sel.anchorNode));
    }

    function currentFontSizePx() {
        var el = formatTarget();
        if (!el) return null;
        var size = parseFloat(window.getComputedStyle(el).fontSize);
        return isFinite(size) ? Math.round(size * 10) / 10 : null;
    }

    function setFontSize(px) {
        var el = formatTarget();
        if (!el) return false;
        px = Math.max(FONT_SIZE_MIN, Math.min(FONT_SIZE_MAX, px));
        var ok = commit(function () {
            el.style.fontSize = px + 'px';
        }, 'Font Size');
        refreshChrome();
        return ok;
    }

    function adjustFontSize(delta) {
        var size = currentFontSizePx();
        if (size === null) return false;
        return setFontSize(Math.round(size + delta));
    }

    // Coalesced font-size step used by the −/+ press-and-hold steppers and the
    // font field's Up/Down arrows: applies the new size immediately (and mirrors
    // it into the field) but folds the run into one undo entry + one save.
    function stepFontSize(delta) {
        var el = formatTarget();
        if (!el) return false;
        var size = currentFontSizePx();
        if (size === null) return false;
        var next = Math.max(FONT_SIZE_MIN, Math.min(FONT_SIZE_MAX, Math.round(size + delta)));
        var section = beginOrExtendBurst('fontstep');
        if (!section) return false;
        el.style.fontSize = next + 'px';
        if (chrome.fontInput) chrome.fontInput.value = String(next);
        refreshChrome();
        return true;
    }

    // Apply whatever is typed in the font field: a valid, changed value commits
    // (clamped); an invalid or unchanged value reverts the field to the element's
    // real size. Called on blur and when the user clicks back onto the slide.
    function commitFontInputValue() {
        var input = chrome.fontInput;
        if (!input) return;
        var current = currentFontSizePx();
        var parsed = parseFloat(input.value);
        if (isFinite(parsed) && current !== null && Math.round(parsed) !== Math.round(current)) {
            setFontSize(parsed);
        }
        var now = currentFontSizePx();
        input.value = now === null ? '' : String(now);
    }

    function execWithCSS(command, value, styleWithCSS) {
        document.execCommand('styleWithCSS', false, !!styleWithCSS);
        document.execCommand(command, false, value === undefined ? null : value);
    }

    var TOGGLE_LABELS = { bold: 'Bold', italic: 'Italic', underline: 'Underline' };

    function toggleInlineOrBlock(execCommand, readBlock, applyBlock) {
        var el = formatTarget();
        if (!el) return false;
        if (editingCaretInSelected()) {
            // Inside a text session execCommand participates in the native undo
            // stack; the whole session commits as one step when it ends. With a
            // collapsed caret this sets the typing style rather than the block.
            execWithCSS(execCommand, undefined, false);
            return true;
        }
        var ok = commit(function () {
            applyBlock(el, readBlock(el));
        }, TOGGLE_LABELS[execCommand]);
        refreshChrome();
        return ok;
    }

    function toggleBold() {
        return toggleInlineOrBlock('bold',
            function (el) { return parseInt(window.getComputedStyle(el).fontWeight, 10) >= 600; },
            function (el, isBold) {
                el.style.fontWeight = isBold ? '400' : '700';
            });
    }

    function toggleItalic() {
        return toggleInlineOrBlock('italic',
            function (el) { return window.getComputedStyle(el).fontStyle === 'italic'; },
            function (el, isItalic) {
                el.style.fontStyle = isItalic ? 'normal' : 'italic';
            });
    }

    function toggleUnderline() {
        return toggleInlineOrBlock('underline',
            function (el) { return window.getComputedStyle(el).textDecorationLine.indexOf('underline') >= 0; },
            function (el, isUnderlined) {
                el.style.textDecorationLine = isUnderlined ? 'none' : 'underline';
            });
    }

    // The literal colour a CSS colour (or var(--x)) resolves to here.
    function resolveColor(cssColor, context) {
        var probe = document.createElement('span');
        probe.style.color = cssColor;
        if (!probe.style.color) return null;
        probe.style.display = 'none';
        (context || document.body).appendChild(probe);
        var out = getComputedStyle(probe).color;
        probe.remove();
        return out;
    }

    // cssColor: a colour, var(--theme-colour) (kept as written, so the deck's
    // theme still drives it) or null to remove the inline colour.
    function setColor(cssColor) {
        var el = formatTarget();
        if (!el) return false;
        if (hasRangedTextSelection() && cssColor) {
            // execCommand only takes literal colours.
            execWithCSS('foreColor', resolveColor(cssColor, el) || cssColor, true);
            return true;
        }
        var ok = commit(function () {
            if (cssColor) el.style.color = cssColor;
            else el.style.removeProperty('color');
            cleanEmptyStyleAttr(el);
        }, 'Colour');
        refreshChrome();
        return ok;
    }

    function setAlign(value) {
        var el = formatTarget();
        if (!el) return false;
        var ok = commit(function () {
            el.style.textAlign = value;
        }, 'Alignment');
        refreshChrome();
        return ok;
    }

    // Background of the selected box (or of the box an equation sits in).
    function setBoxColor(cssColor) {
        var target = boxTarget(state.selected);
        if (!target) return false;
        var ok = commit(function () {
            if (cssColor) target.style.backgroundColor = cssColor;
            else target.style.removeProperty('background-color');
            cleanEmptyStyleAttr(target);
        }, 'Box Colour');
        refreshChrome();
        return ok;
    }

    // Live preview while the native colour panel is dragged: one undo step
    // per drag (a coalesced burst), committed on the input's change event.
    function previewColor(kind, cssColor) {
        var target = kind === 'boxcolor' ? boxTarget(state.selected) : formatTarget();
        if (!target) return false;
        if (!beginOrExtendBurst(kind)) return false;
        if (kind === 'boxcolor') target.style.backgroundColor = cssColor;
        else target.style.color = cssColor;
        return true;
    }

    /* ===== Theme colours (BACKLOG 6) ===== */

    var THEME_TEXT_VARS = [
        ['--color-text', 'Text'],
        ['--color-heading', 'Heading'],
        ['--color-accent', 'Accent'],
        ['--color-accent-2', 'Accent 2'],
        ['--color-muted', 'Muted']
    ];
    var THEME_BOX_VARS = [
        ['--color-box-bg', 'Box'],
        ['--color-result-bg', 'Result'],
        ['--color-card', 'Card'],
        ['--color-highlight-bg', 'Highlight'],
        ['--color-bg', 'White']
    ];
    // Custom properties that are not text colours.
    var NOT_TEXT_COLOR = /(^--(p|ws|presto)-)|bg|background|border|card|box|shadow|surface|soft|faint|font|size|radius|gap|width|height|space|padding|margin|overlay|section/i;

    function readVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function isColorValue(value) {
        if (!value) return false;
        if (window.CSS && CSS.supports) return CSS.supports('color', value);
        var probe = document.createElement('span');
        probe.style.color = value;
        return !!probe.style.color;
    }

    function prettyName(name) {
        var bare = name.replace(/^--(color-)?/, '').replace(/-/g, ' ');
        return bare.charAt(0).toUpperCase() + bare.slice(1);
    }

    // Custom properties declared on :root / html in the deck's own sheets
    // (cross-origin sheets such as KaTeX's CDN copy are unreadable; skipped).
    function rootCustomProperties() {
        var names = [];
        Array.prototype.forEach.call(document.styleSheets, function (sheet) {
            var rules;
            try { rules = sheet.cssRules; } catch (e) { return; }
            if (!rules) return;
            Array.prototype.forEach.call(rules, function (rule) {
                if (!rule.selectorText || !rule.style) return;
                if (!/(^|,)\s*(:root|html)\s*(,|$)/.test(rule.selectorText)) return;
                for (var i = 0; i < rule.style.length; i++) {
                    var prop = rule.style[i];
                    if (prop.indexOf('--') === 0 && names.indexOf(prop) < 0) names.push(prop);
                }
            });
        });
        return names;
    }

    function themeColors() {
        if (state.theme) return state.theme;
        var text = [];
        var seen = {};
        function add(list, name, label) {
            var value = readVar(name);
            if (!isColorValue(value)) return;
            var resolved = resolveColor(value) || value;
            var key = (list === text ? 't' : 'b') + resolved;
            if (seen[key]) return;
            seen[key] = true;
            list.push({ name: label || prettyName(name), value: 'var(' + name + ')', color: resolved, variable: name });
        }
        THEME_TEXT_VARS.forEach(function (pair) { add(text, pair[0], pair[1]); });
        rootCustomProperties().forEach(function (name) {
            if (text.length >= MAX_TEXT_SWATCHES) return;
            if (NOT_TEXT_COLOR.test(name)) return;
            add(text, name);
        });
        var box = [];
        THEME_BOX_VARS.forEach(function (pair) { if (box.length < 3) add(box, pair[0], pair[1]); });
        if (!box.length) box.push({ name: 'White', value: '#FFFFFF', color: 'rgb(255, 255, 255)' });
        state.theme = { text: text.slice(0, MAX_TEXT_SWATCHES), box: box };
        return state.theme;
    }

    function rgbToHex(rgb) {
        var m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(rgb || '');
        if (!m) return '#000000';
        return '#' + [m[1], m[2], m[3]].map(function (n) {
            var h = parseInt(n, 10).toString(16);
            return h.length < 2 ? '0' + h : h;
        }).join('');
    }

    /* ===== Appears at step ===== */

    function ownStepRange(el) {
        if (!el || !el.hasAttribute('data-step')) return null;
        if (window.WS && typeof WS.getRevealRange === 'function') {
            var r = WS.getRevealRange(el, 'step');
            if (r) return r;
        }
        var p = parseRevealValue(el.getAttribute('data-step'));
        return p ? { start: p.start, end: p.end === undefined ? Infinity : p.end } : null;
    }

    // { text, title, start, end, own } describing when the element appears.
    function appearsInfo(el) {
        if (!el) return { text: 'always', title: '' };
        if (el.hasAttribute('data-step')) {
            var r = ownStepRange(el);
            var raw = el.getAttribute('data-step');
            if (!r) return { text: 'never', title: 'data-step="' + raw + '" is not a valid step, so this never appears', own: true, invalid: true };
            if (r.start <= 0 && r.end === Infinity) return { text: 'always', title: 'Visible from the start', start: 0, end: Infinity, own: true };
            if (r.end === Infinity) return { text: 'step ' + r.start, title: 'Appears at step ' + r.start, start: r.start, end: r.end, own: true };
            if (r.end === r.start) return { text: 'step ' + r.start + ' only', title: 'Visible only at step ' + r.start, start: r.start, end: r.end, own: true };
            return { text: 'steps ' + r.start + '–' + r.end, title: 'Visible from step ' + r.start + ' to ' + r.end, start: r.start, end: r.end, own: true };
        }
        var slide = el.closest('.slide');
        var parent = el.parentElement ? el.parentElement.closest('[data-step]') : null;
        if (parent && slide && slide.contains(parent)) {
            var pr = ownStepRange(parent);
            if (pr && pr.start > 0) return { text: 'with step ' + pr.start, title: 'Appears with its container at step ' + pr.start, start: pr.start, end: pr.end, own: false };
        }
        return { text: 'always', title: 'Visible from the start', start: 0, end: Infinity, own: false };
    }

    // step: null/0 = always, n = appears at step n and stays. Written through
    // the engine's strict parser; undoable; renumbers the slide's steps.
    function setAppearsAt(step) {
        var el = state.selected;
        var section = activeSlide();
        if (!el || !section || !section.contains(el)) return false;
        endTextEdit(true);
        var value = step === null || step === undefined || step === 0 ? null : String(step);
        if (value !== null && !parseRevealValue(value)) return false;
        var info = appearsInfo(el);
        if (value === null ? (info.own ? info.text === 'always' : info.start === 0) : (info.own && info.start === step && info.end === Infinity)) {
            return false;
        }
        var ok = commit(function () {
            var generated = el.hasAttribute('data-ws-auto-step');
            el.removeAttribute('data-ws-auto-step');
            if (value === null) {
                // An item of a data-reveal="each" list (or of a stepped
                // container) needs an explicit 0 to show from the start.
                var insideReveal = generated || !!el.closest('ul[data-reveal="each"], ol[data-reveal="each"]');
                if (insideReveal) el.setAttribute('data-step', '0');
                else el.removeAttribute('data-step');
            } else {
                el.setAttribute('data-step', value);
            }
            el._ws_step = undefined;
        }, 'Reveal Step');
        if (ok) {
            refreshSteps(section);
            refreshChrome();
        }
        return ok;
    }

    /* ===== Links (decision 7) ===== */

    function slugify(text) {
        var base = String(text || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '');
        base = base.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40).replace(/-+$/, '');
        return base || 'slide';
    }

    function idInUse(id) {
        if (document.getElementById(id)) return true;
        return wsReady() && WS.slides.some(function (s) { return s.id === id; });
    }

    // An id that reaches slide `index` without touching its <section> tag
    // (saves carry only the slide body): the section's own id, an id on its
    // first heading, or a new id written onto that heading (saved as an edit
    // on that slide).
    function ensureSlideAnchor(index) {
        var slide = wsReady() ? WS.slides[index] : null;
        if (!slide) return null;
        if (slide.id) return slide.id;
        var heading = null;
        var heads = slide.querySelectorAll('h1, h2, h3');
        for (var i = 0; i < heads.length; i++) {
            if (!heads[i].closest('.ws-section-eyebrow, .section-toc, aside.notes')) { heading = heads[i]; break; }
        }
        var anchor = heading || slide.firstElementChild;
        if (!anchor) return null;
        if (anchor.id) return anchor.id;
        var prefix = slide.classList.contains('appendix') ? 'appendix-' : 'slide-';
        var base = prefix + slugify(anchor.textContent).replace(/^appendix-/, '');
        var id = base;
        for (var n = 2; idInUse(id); n++) id = base + '-' + n;
        commitOn(index, function () { anchor.id = id; }, 'Link Target');
        return id;
    }

    function setLinkTarget(link, index) {
        if (!link || !isLink(link) || !wsReady()) return false;
        var owner = WS.slides.indexOf(link.closest('.slide'));
        if (owner < 0 || index < 0 || index >= WS.slides.length) return false;
        post({ type: 'wysiwygWillEdit' });
        var id = ensureSlideAnchor(index);
        if (!id) return false;
        var href = '#' + id;
        if (link.getAttribute('href') === href) return false;
        var ok = commitOn(owner, function () {
            link.setAttribute('href', href);
            // A link into the appendix uses the engine's appendix-link button
            // behaviour (return stack); keep the authored class otherwise.
            if (WS.slides[index].classList.contains('appendix') && !link.classList.contains('ws-back-link') &&
                !link.classList.contains('ws-appendix-link') && link.closest('.header-links, .appendix-links')) {
                link.classList.add('ws-appendix-link');
            }
        }, 'Link Target');
        refreshChrome();
        return ok;
    }

    function editLinkLabel(link) {
        link = link || state.selected;
        if (!isLink(link)) return false;
        if (!beginTextEdit(link)) return false;
        selectBlockContents(link);
        refreshChrome();
        return true;
    }

    function followLinkAt(target, slide) {
        var link = target && target.closest ? target.closest('a[href]') : null;
        if (!link || !slide.contains(link)) return false;
        if (!window.PrestoEditChrome || typeof PrestoEditChrome.followLink !== 'function') return false;
        return PrestoEditChrome.followLink(link) !== false;
    }

    /* ===== Chrome: styles, toolbar, handles, status ===== */

    var CHROME_CSS = [
        'body.presto-wysiwyg-on .slide.ws-active [data-step] {',
        '    transition: none !important;',
        '}',
        'body.presto-wysiwyg-on .slide.ws-active [data-step]:not(.ws-visible) {',
        '    opacity: var(--presto-hidden-step-opacity, 0.33) !important;',
        '    visibility: visible !important;',
        '    pointer-events: auto !important;',
        '}',
        /* At 0% the ghosts are invisible — they must not swallow clicks aimed
           at objects that are visible at the current step. */
        'body.presto-wysiwyg-on.presto-future-steps-hidden .slide.ws-active [data-step]:not(.ws-visible) {',
        '    pointer-events: none !important;',
        '}',
        'body.presto-wysiwyg-on .slide.ws-active .presto-wysiwyg-hover {',
        '    outline: 1px solid rgba(47, 107, 216, 0.45) !important;',
        '    outline-offset: 1px;',
        '}',
        'body.presto-wysiwyg-on .slide.ws-active .presto-wysiwyg-selected {',
        '    outline: 1.5px solid var(--p-accent, #2F6BD8) !important;',
        '    outline-offset: 1px;',
        '}',
        'body.presto-wysiwyg-on .slide.ws-active .presto-wysiwyg-editing {',
        '    outline: 2px solid var(--p-accent, #2F6BD8) !important;',
        '    outline-offset: 1px;',
        '    cursor: text;',
        '}',
        'body.presto-wysiwyg-on .slide.ws-active a[href].presto-wysiwyg-hover,',
        'body.presto-wysiwyg-on .slide.ws-active a[href].presto-wysiwyg-selected { cursor: default; }',
        'body.presto-wysiwyg-on .slide.ws-active [contenteditable="true"] {',
        '    -webkit-user-select: text;',
        '    user-select: text;',
        '}',
        /* Format bar: white, soft shadow, contextual groups (kind-specific). */
        '#presto-wysiwyg-toolbar {',
        '    position: fixed;',
        '    z-index: 2147483000;',
        '    display: none;',
        '    align-items: center;',
        '    gap: 2px;',
        '    padding: 4px;',
        '    box-sizing: border-box;',
        '    background: var(--p-surface, #FFFFFF);',
        '    border-radius: 10px;',
        '    box-shadow: var(--p-shadow-pop, 0 1px 2px rgba(16, 24, 40, .08), 0 8px 24px rgba(16, 24, 40, .14), 0 0 0 .5px rgba(0, 0, 0, .08));',
        '    font-family: var(--p-font-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif);',
        '    font-size: var(--p-text-ui, 12.5px);',
        '    line-height: 1;',
        '    color: var(--p-ink, #1C2532);',
        '    white-space: nowrap;',
        '    -webkit-user-select: none;',
        '    user-select: none;',
        '    -webkit-font-smoothing: antialiased;',
        '}',
        '#presto-wysiwyg-toolbar.visible { display: flex; }',
        '#presto-wysiwyg-toolbar .pw-group { display: inline-flex; align-items: center; gap: 2px; }',
        '#presto-wysiwyg-toolbar [hidden] { display: none !important; }',
        '.presto-wysiwyg-chrome button.pw-tb, #presto-wysiwyg-toolbar button {',
        '    display: inline-flex;',
        '    align-items: center;',
        '    justify-content: center;',
        '    gap: 5px;',
        '    min-width: 28px;',
        '    height: 28px;',
        '    margin: 0;',
        '    padding: 0 7px;',
        '    box-sizing: border-box;',
        '    background: transparent;',
        '    color: inherit;',
        '    border: 0;',
        '    border-radius: 6px;',
        '    font: inherit;',
        '    line-height: 1;',
        '    cursor: default;',
        '}',
        '#presto-wysiwyg-toolbar button:hover { background: rgba(0, 0, 0, .05); }',
        '#presto-wysiwyg-toolbar button:active { background: rgba(0, 0, 0, .08); }',
        '#presto-wysiwyg-toolbar button.on, #presto-wysiwyg-toolbar button.pw-primary {',
        '    background: var(--p-accent-tint, rgba(47, 107, 216, 0.11));',
        '    color: var(--p-accent, #2F6BD8);',
        '}',
        '#presto-wysiwyg-toolbar button:disabled { color: var(--p-faint, #A9AFB8); background: transparent; }',
        '#presto-wysiwyg-toolbar button:focus-visible, .pw-pop button:focus-visible {',
        '    outline: 2px solid var(--p-accent-ring, rgba(47, 107, 216, 0.28));',
        '    outline-offset: 1px;',
        '}',
        '#presto-wysiwyg-toolbar svg { width: 14px; height: 14px; flex: none; }',
        '#presto-wysiwyg-toolbar .pw-divider {',
        '    flex: none;',
        '    width: 1px;',
        '    height: 18px;',
        '    margin: 0 3px;',
        '    background: var(--p-hair, #DDE0E5);',
        '}',
        '#presto-wysiwyg-toolbar .pw-kind-label {',
        '    padding: 0 8px 0 6px;',
        '    font-size: var(--p-text-small, 11.5px);',
        '    font-weight: 600;',
        '    color: var(--p-dim, #7B8390);',
        '}',
        '#presto-wysiwyg-font-size {',
        '    width: 38px;',
        '    height: 24px;',
        '    box-sizing: border-box;',
        '    margin: 0 1px;',
        '    padding: 0;',
        '    background: rgba(0, 0, 0, .055);',
        '    color: var(--p-ink, #1C2532);',
        '    border: 0;',
        '    border-radius: 6px;',
        '    font: 12px/24px var(--p-font-ui, -apple-system, BlinkMacSystemFont, sans-serif);',
        '    font-variant-numeric: tabular-nums;',
        '    text-align: center;',
        '    outline: none;',
        '    -webkit-user-select: text;',
        '    user-select: text;',
        '}',
        '#presto-wysiwyg-font-size:focus { box-shadow: 0 0 0 2px var(--p-accent-ring, rgba(47, 107, 216, 0.28)); }',
        '#presto-wysiwyg-toolbar .pw-b { font-weight: 700; }',
        '#presto-wysiwyg-toolbar .pw-i { font-style: italic; font-family: var(--p-font-serif, "New York", Georgia, serif); }',
        '#presto-wysiwyg-toolbar .pw-u { text-decoration: underline; }',
        '.pw-dot {',
        '    display: inline-block;',
        '    flex: none;',
        '    width: 14px;',
        '    height: 14px;',
        '    border-radius: 50%;',
        '    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .12);',
        '}',
        '.pw-dot.pw-auto {',
        '    background: linear-gradient(135deg, transparent 45%, var(--p-live, #D4462C) 45% 55%, transparent 55%), #FFFFFF;',
        '}',
        '#presto-wysiwyg-toolbar .pw-boxrow { gap: 0; padding-left: 7px; }',
        '#presto-wysiwyg-toolbar .pw-boxrow .pw-boxlabel { padding-right: 4px; }',
        '#presto-wysiwyg-toolbar .pw-boxrow button.pw-swatch,',
        '.pw-pop button.pw-swatch {',
        '    display: inline-grid;',
        '    place-items: center;',
        '    min-width: 22px;',
        '    width: 22px;',
        '    height: 22px;',
        '    margin: 0;',
        '    padding: 0;',
        '    border: 0;',
        '    border-radius: 50%;',
        '    background: transparent;',
        '    cursor: default;',
        '}',
        '.pw-pop button.pw-swatch { width: 26px; height: 26px; }',
        '.pw-pop button.pw-swatch:hover { background: rgba(0, 0, 0, .05); }',
        '#presto-wysiwyg-toolbar button.pw-swatch.on .pw-dot, .pw-pop button.pw-swatch.on .pw-dot {',
        '    box-shadow: inset 0 0 0 1px rgba(0, 0, 0, .12), 0 0 0 2px var(--p-surface, #FFFFFF), 0 0 0 3.5px var(--p-accent, #2F6BD8);',
        '}',
        '.pw-colorwell {',
        '    position: relative;',
        '    display: inline-grid;',
        '    place-items: center;',
        '    width: 22px;',
        '    height: 22px;',
        '    border-radius: 50%;',
        '}',
        '.pw-colorwell .pw-dot { background: conic-gradient(#E5484D, #F5A623, #F8E71C, #46A758, #3E7BFA, #8E4EC6, #E5484D); }',
        '.pw-colorwell input[type="color"] {',
        '    position: absolute;',
        '    inset: 0;',
        '    width: 100%;',
        '    height: 100%;',
        '    margin: 0;',
        '    padding: 0;',
        '    border: 0;',
        '    opacity: 0;',
        '    cursor: default;',
        '}',
        '#presto-wysiwyg-toolbar .pw-chip {',
        '    padding: 2px 6px;',
        '    border-radius: 5px;',
        '    background: var(--p-accent-tint, rgba(47, 107, 216, 0.11));',
        '    color: var(--p-accent, #2F6BD8);',
        '    font-size: 11px;',
        '    font-weight: 600;',
        '    line-height: 1.25;',
        '}',
        '#presto-wysiwyg-toolbar .pw-chip.pw-invalid { background: rgba(212, 70, 44, .1); color: var(--p-live, #D4462C); }',
        '#presto-wysiwyg-toolbar .pw-chev { width: 9px; height: 9px; opacity: .6; }',
        '#presto-wysiwyg-toolbar .pw-hint {',
        '    padding: 0 6px;',
        '    color: var(--p-dim, #7B8390);',
        '    font-size: var(--p-text-small, 11.5px);',
        '}',
        /* Popovers: colours, steps, the ⋯ menu and the equation editor. */
        '.pw-pop {',
        '    position: fixed;',
        '    z-index: 2147483050;',
        '    box-sizing: border-box;',
        '    min-width: 180px;',
        '    padding: 6px;',
        '    background: var(--p-surface, #FFFFFF);',
        '    border-radius: 10px;',
        '    box-shadow: var(--p-shadow-pop, 0 1px 2px rgba(16, 24, 40, .08), 0 8px 24px rgba(16, 24, 40, .14), 0 0 0 .5px rgba(0, 0, 0, .08));',
        '    font: var(--p-text-ui, 12.5px)/1.3 var(--p-font-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif);',
        '    color: var(--p-ink, #1C2532);',
        '    -webkit-user-select: none;',
        '    user-select: none;',
        '    -webkit-font-smoothing: antialiased;',
        '}',
        '.pw-pop .pw-pop-title {',
        '    padding: 6px 8px 4px;',
        '    font-size: 11px;',
        '    font-weight: 600;',
        '    color: var(--p-dim, #7B8390);',
        '}',
        '.pw-pop .pw-item {',
        '    display: flex;',
        '    align-items: center;',
        '    gap: 8px;',
        '    width: 100%;',
        '    height: 28px;',
        '    margin: 0;',
        '    padding: 0 10px 0 8px;',
        '    box-sizing: border-box;',
        '    border: 0;',
        '    border-radius: 6px;',
        '    background: transparent;',
        '    font: inherit;',
        '    color: inherit;',
        '    text-align: left;',
        '    cursor: default;',
        '}',
        '.pw-pop .pw-item:hover, .pw-pop .pw-item.active { background: var(--p-accent-tint, rgba(47, 107, 216, 0.11)); }',
        '.pw-pop .pw-item:disabled { color: var(--p-faint, #A9AFB8); background: transparent; }',
        '.pw-pop .pw-item .pw-check { flex: none; width: 14px; color: var(--p-accent, #2F6BD8); }',
        '.pw-pop .pw-item .pw-check svg { width: 14px; height: 14px; }',
        '.pw-pop .pw-item .pw-kbd { margin-left: auto; padding-left: 16px; color: var(--p-faint, #A9AFB8); font-size: 11.5px; }',
        '.pw-pop .pw-item.pw-danger { color: var(--p-live, #D4462C); }',
        '.pw-pop .pw-sep { height: 1px; margin: 5px 4px; background: var(--p-hair, #DDE0E5); }',
        '.pw-pop .pw-swatches { display: flex; flex-wrap: wrap; gap: 2px; max-width: 216px; padding: 0 4px 4px; }',
        '.pw-pop .pw-custom { display: flex; align-items: center; gap: 8px; padding: 4px 8px 6px; color: var(--p-ink-2, #485263); }',
        '.pw-btn {',
        '    height: 28px;',
        '    margin: 0;',
        '    padding: 0 11px;',
        '    display: inline-flex;',
        '    align-items: center;',
        '    gap: 6px;',
        '    border: 0;',
        '    border-radius: var(--p-radius-control, 7px);',
        '    background: var(--p-surface, #FFFFFF);',
        '    box-shadow: 0 0 0 .5px rgba(0, 0, 0, .14), 0 1px 1.5px rgba(0, 0, 0, .07);',
        '    font: 500 var(--p-text-ui, 12.5px)/1 var(--p-font-ui, -apple-system, BlinkMacSystemFont, sans-serif);',
        '    color: var(--p-ink, #1C2532);',
        '    cursor: default;',
        '}',
        '.pw-btn:hover { background: #F7F8FA; }',
        '.pw-btn.primary { background: var(--p-accent, #2F6BD8); color: #FFFFFF; box-shadow: 0 1px 1.5px rgba(0, 0, 0, .12); }',
        '.pw-btn.primary:hover { background: #2A61C6; }',
        '.pw-btn:disabled, .pw-btn.primary:disabled { background: var(--p-hair, #DDE0E5); color: var(--p-dim, #7B8390); box-shadow: none; }',
        '#presto-wysiwyg-equation { width: 460px; padding: 12px; }',
        '#presto-wysiwyg-equation .pw-eq-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; color: var(--p-dim, #7B8390); }',
        '#presto-wysiwyg-equation .pw-eq-head b { color: var(--p-ink, #1C2532); font-weight: 600; }',
        '#presto-wysiwyg-equation .pw-eq-src {',
        '    display: block;',
        '    width: 100%;',
        '    min-height: 60px;',
        '    max-height: 180px;',
        '    box-sizing: border-box;',
        '    margin: 0;',
        '    padding: 8px 10px;',
        '    resize: vertical;',
        '    border: 1px solid var(--p-hair-strong, #C8CCD3);',
        '    border-radius: 8px;',
        '    background: var(--p-code-bg, #FCFCFD);',
        '    color: var(--p-ink, #1C2532);',
        '    font: 12.5px/1.5 var(--p-font-mono, "SF Mono", ui-monospace, Menlo, monospace);',
        '    outline: none;',
        '    -webkit-user-select: text;',
        '    user-select: text;',
        '}',
        '#presto-wysiwyg-equation .pw-eq-src:focus { border-color: var(--p-accent, #2F6BD8); box-shadow: 0 0 0 3px var(--p-accent-ring, rgba(47, 107, 216, 0.28)); }',
        '#presto-wysiwyg-equation .pw-eq-preview {',
        '    display: flex;',
        '    align-items: center;',
        '    justify-content: center;',
        '    min-height: 52px;',
        '    max-height: 200px;',
        '    overflow: auto;',
        '    margin-top: 10px;',
        '    padding: 10px 12px;',
        '    box-sizing: border-box;',
        '    border-radius: 8px;',
        '    background: var(--p-panel, #F3F4F6);',
        '    color: #1C2532;',
        '    font-size: 22px;',
        '    line-height: 1.2;',
        '}',
        '#presto-wysiwyg-equation .pw-eq-preview .katex-display { margin: 0; }',
        '#presto-wysiwyg-equation .pw-eq-preview.pw-eq-empty { color: var(--p-faint, #A9AFB8); font-size: 12.5px; }',
        '#presto-wysiwyg-equation .pw-eq-error { margin-top: 6px; color: var(--p-live, #D4462C); font-size: var(--p-text-small, 11.5px); line-height: 1.4; overflow-wrap: anywhere; }',
        '#presto-wysiwyg-equation .pw-eq-error:empty { display: none; }',
        '#presto-wysiwyg-equation .pw-eq-foot { display: flex; align-items: center; gap: 6px; margin-top: 10px; }',
        '#presto-wysiwyg-equation .pw-eq-foot .pw-eq-hint { margin-right: auto; color: var(--p-dim, #7B8390); font-size: var(--p-text-small, 11.5px); }',
        '#presto-wysiwyg-status {',
        '    position: fixed;',
        '    left: calc(var(--presto-rail-w, 0px) + 16px);',
        '    bottom: 22px;',
        '    z-index: 2147483000;',
        '    max-width: 46vw;',
        '    padding: 5px 10px;',
        '    border-radius: 7px;',
        '    background: var(--p-surface, #FFFFFF);',
        '    box-shadow: var(--p-shadow-card, 0 0 0 .5px rgba(0, 0, 0, .08), 0 1px 3px rgba(0, 0, 0, .05));',
        '    font: 500 var(--p-text-small, 11.5px)/1.35 var(--p-font-ui, -apple-system, BlinkMacSystemFont, sans-serif);',
        '    color: var(--p-ink-2, #485263);',
        '    opacity: 0;',
        '    transition: opacity 0.25s ease;',
        '    pointer-events: none;',
        '    -webkit-font-smoothing: antialiased;',
        '}',
        '#presto-wysiwyg-status.visible { opacity: 1; }',
        '#presto-wysiwyg-status.saving { color: var(--p-dim, #7B8390); }',
        '#presto-wysiwyg-status.saved { color: var(--p-ok, #2E8B57); }',
        '#presto-wysiwyg-status.error { color: var(--p-live, #D4462C); }',
        '.presto-wysiwyg-handle {',
        '    position: fixed;',
        '    z-index: 2147483001;',
        '    width: 9px;',
        '    height: 9px;',
        '    box-sizing: border-box;',
        '    background: #FFFFFF;',
        '    border: 1.5px solid var(--p-accent, #2F6BD8);',
        '    border-radius: 2px;',
        '    box-shadow: 0 1px 2px rgba(16, 24, 40, .12);',
        '    display: none;',
        '}',
        '.presto-wysiwyg-handle.visible { display: block; }',
        '.presto-wysiwyg-handle[data-handle="se"] { cursor: nwse-resize; }',
        '.presto-wysiwyg-handle[data-handle="e"] { cursor: ew-resize; }',
        '.presto-wysiwyg-handle[data-handle="s"] { cursor: ns-resize; }',
        '.presto-wysiwyg-handle[data-handle="w"] { cursor: ew-resize; }',
        '@media (prefers-reduced-motion: reduce) {',
        '    #presto-wysiwyg-status { transition: none; }',
        '}'
    ].join('\n');

    function svgIcon(pathData, viewBox) {
        return '<svg width="14" height="14" viewBox="' + (viewBox || '0 0 16 16') +
            '" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
            pathData + '</svg>';
    }

    var ICONS = {
        alignLeft: svgIcon('<path d="M2 3.5h12M2 6.5h8M2 9.5h12M2 12.5h8"/>'),
        alignCenter: svgIcon('<path d="M2 3.5h12M4 6.5h8M2 9.5h12M4 12.5h8"/>'),
        alignRight: svgIcon('<path d="M2 3.5h12M6 6.5h8M2 9.5h12M6 12.5h8"/>'),
        bullets: svgIcon('<circle cx="3" cy="4" r="1" fill="currentColor" stroke="none"/><circle cx="3" cy="8" r="1" fill="currentColor" stroke="none"/><circle cx="3" cy="12" r="1" fill="currentColor" stroke="none"/><path d="M6 4h8M6 8h8M6 12h8"/>'),
        addBullet: svgIcon('<circle cx="3" cy="4" r="1" fill="currentColor" stroke="none"/><circle cx="3" cy="11" r="1" fill="currentColor" stroke="none"/><path d="M6 4h8M6 11h3M12 8v6M9 11h6"/>'),
        indent: svgIcon('<path d="M2 3.5h12M7 7.5h7M7 11.5h7M2 6l3 2.5L2 11"/>'),
        outdent: svgIcon('<path d="M2 3.5h12M7 7.5h7M7 11.5h7M5 6 2 8.5 5 11"/>'),
        addText: svgIcon('<path d="M2.5 3.5h8M6.5 3.5v9"/><path d="M11 9.5h4M13 7.5v4"/>'),
        duplicate: svgIcon('<rect x="5" y="5" width="8.5" height="8.5" rx="1"/><path d="M3 10.5V3.5A1 1 0 0 1 4 2.5h7"/>'),
        trash: svgIcon('<path d="M2.5 4h11M5.5 4V2.8a.8.8 0 0 1 .8-.8h3.4a.8.8 0 0 1 .8.8V4M4.5 4l.6 9a1 1 0 0 0 1 .9h3.8a1 1 0 0 0 1-.9l.6-9"/>'),
        tex: svgIcon('<path d="M3 4h4M5 4v8M9 8l4 4M13 8l-4 4"/>'),
        more: svgIcon('<circle cx="3.5" cy="8" r=".9" fill="currentColor"/><circle cx="8" cy="8" r=".9" fill="currentColor"/><circle cx="12.5" cy="8" r=".9" fill="currentColor"/>'),
        chevron: svgIcon('<path d="M2.5 4.5L6 8l3.5-3.5"/>', '0 0 12 12').replace('<svg ', '<svg class="pw-chev" '),
        check: svgIcon('<path d="M3.5 8.5l3 3 6-7"/>')
    };

    function toolbarButton(title, html, className, onClick) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.title = title;
        btn.setAttribute('aria-label', title);
        if (className) btn.className = className;
        btn.innerHTML = html;
        btn.addEventListener('mousedown', function (e) {
            // Keep focus (and any text selection) in the slide.
            e.preventDefault();
            e.stopPropagation();
        });
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            onClick();
        });
        return btn;
    }

    function divider() {
        var d = document.createElement('span');
        d.className = 'pw-divider';
        return d;
    }

    // A toolbar button that repeats its action while held (press-and-hold), like
    // a native stepper. Each repeat runs `step`; the whole hold is one coalesced
    // burst that commits on release. Keyboard activation (Enter/Space) fires once.
    function makeStepperButton(title, label, className, step) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.title = title;
        btn.setAttribute('aria-label', title);
        btn.className = className;
        btn.textContent = label;
        var holdTimer = null;
        var repeatTimer = null;
        var suppressClick = false;
        function stopRepeat() {
            if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
            if (repeatTimer) { clearInterval(repeatTimer); repeatTimer = null; }
        }
        function endPress() {
            stopRepeat();
            document.removeEventListener('mouseup', endPress, true);
            flushBurst();   // commit the whole hold as one undo step + one save
        }
        btn.addEventListener('mousedown', function (e) {
            e.preventDefault();
            e.stopPropagation();
            suppressClick = true;
            step();
            holdTimer = setTimeout(function () {
                repeatTimer = setInterval(step, FONT_REPEAT_MS);
            }, FONT_HOLD_MS);
            document.addEventListener('mouseup', endPress, true);
        });
        btn.addEventListener('mouseleave', stopRepeat);
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (suppressClick) { suppressClick = false; return; }
            step();
            flushBurst();
        });
        return btn;
    }

    function applyHiddenStepOpacity() {
        document.body.style.setProperty('--presto-hidden-step-opacity', String(state.hiddenStepOpacity));
        document.body.classList.toggle('presto-future-steps-hidden', state.hiddenStepOpacity === 0);
    }

    // "Show later steps faintly": the step strip's toggle, ⇧⌘O and the
    // shell's menu item all land here. opts.fromUser (⇧⌘O typed into the
    // page) tells Swift; opts.silent (the edit chrome, which posts for its
    // own toggle) does not. The edit chrome hears presto:ghostfuture.
    function setGhostFuture(on, opts) {
        var next = on ? GHOST_OPACITY : 0;
        var changed = next !== state.hiddenStepOpacity;
        state.hiddenStepOpacity = next;
        writeHiddenStepOpacity();
        applyHiddenStepOpacity();
        if (!changed) return state.hiddenStepOpacity > 0;
        if (opts && opts.fromUser) post({ type: 'ghostFutureChanged', on: !!on });
        try {
            document.dispatchEvent(new CustomEvent('presto:ghostfuture', { detail: { on: !!on } }));
        } catch (e) { /* CustomEvent unavailable */ }
        // A ghost that just vanished may hold the selection.
        if (!on && state.selected && isHiddenAtCurrentStep(state.selected)) refreshChrome();
        return state.hiddenStepOpacity > 0;
    }

    function toggleHiddenStepOpacity() {
        return setGhostFuture(!(state.hiddenStepOpacity > 0), { fromUser: true });
    }

    // The PDF and HTML-source buttons moved to the native toolbar; these stay
    // as API so older shells and scripts keep working.
    function requestSourceSidebarToggle() {
        post({ type: 'toggleSourceSidebar' });
    }

    function requestPDFExport() {
        if (!state.pdfExportAvailable) return false;
        post({ type: 'exportPDFRequested' });
        return true;
    }

    function setPDFExportAvailable(available) {
        state.pdfExportAvailable = available === true;
    }

    function setSourceSidebarVisible(visible) {
        state.sourceSidebarVisible = visible === true;
    }

    function readHiddenStepOpacity() {
        try {
            return localStorage.getItem(HIDDEN_STEP_OPACITY_STORAGE_KEY) === '0' ? 0 : GHOST_OPACITY;
        } catch (e) {
            return GHOST_OPACITY;
        }
    }

    function writeHiddenStepOpacity() {
        try {
            localStorage.setItem(HIDDEN_STEP_OPACITY_STORAGE_KEY, state.hiddenStepOpacity === 0 ? '0' : String(GHOST_OPACITY));
        } catch (e) { /* unavailable storage must not block editing */ }
    }

    function buildChrome() {
        chrome.style = document.createElement('style');
        chrome.style.id = 'presto-wysiwyg-style';
        chrome.style.textContent = CHROME_CSS;
        document.head.appendChild(chrome.style);

        var toolbar = document.createElement('div');
        toolbar.id = 'presto-wysiwyg-toolbar';
        toolbar.className = 'presto-wysiwyg-chrome';
        toolbar.setAttribute('role', 'toolbar');
        toolbar.setAttribute('aria-label', 'Slide formatting');
        chrome.groups = [];

        // Each group lists the selection kinds it applies to; refreshChrome
        // shows only those (text controls never appear for atoms).
        function group(cls, kinds) {
            var g = document.createElement('span');
            g.className = 'pw-group ' + cls;
            g.dataset.kinds = kinds;
            chrome.groups.push(g);
            toolbar.appendChild(g);
            return g;
        }
        function sep() { toolbar.appendChild(divider()); }

        var kindGroup = group('pw-g-kind', 'image table equation box');
        var kindLabel = document.createElement('span');
        kindLabel.className = 'pw-kind-label';
        kindGroup.appendChild(kindLabel);
        chrome.kindLabel = kindLabel;

        var fontGroup = group('pw-g-font', 'text list box');
        // Font size stepper (press-and-hold repeat)
        fontGroup.appendChild(makeStepperButton('Decrease font size', '−', 'pw-font-minus', function () { stepFontSize(-2); }));
        var fontInput = document.createElement('input');
        fontInput.id = 'presto-wysiwyg-font-size';
        fontInput.type = 'text';
        fontInput.inputMode = 'numeric';
        fontInput.title = 'Font size (px)';
        fontInput.setAttribute('aria-label', 'Font size in pixels');
        fontInput.addEventListener('mousedown', function (e) { e.stopPropagation(); });
        fontInput.addEventListener('keydown', function (e) {
            e.stopPropagation();
            if (e.key === 'Enter') {
                e.preventDefault();
                commitFontInputValue();
                fontInput.blur();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                fontInput.value = String(currentFontSizePx() || '');
                fontInput.blur();
            } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
                // Live ±1 (Shift = ±10) stepping, coalesced so a held arrow is one
                // undo step. Keeps focus so the field's own auto-repeat flows.
                e.preventDefault();
                var mag = e.shiftKey ? 10 : 1;
                stepFontSize(e.key === 'ArrowUp' ? mag : -mag);
            }
        });
        // Blur applies the typed value (not discard); invalid input reverts.
        fontInput.addEventListener('blur', function () { commitFontInputValue(); });
        chrome.fontInput = fontInput;
        fontGroup.appendChild(fontInput);
        fontGroup.appendChild(makeStepperButton('Increase font size', '+', 'pw-font-plus', function () { stepFontSize(2); }));
        sep();

        var styleGroup = group('pw-g-style', 'text list box');
        styleGroup.appendChild(toolbarButton('Bold (⌘B)', 'B', 'pw-b', toggleBold));
        styleGroup.appendChild(toolbarButton('Italic (⌘I)', 'I', 'pw-i', toggleItalic));
        styleGroup.appendChild(toolbarButton('Underline (⌘U)', 'U', 'pw-u', toggleUnderline));
        var colorBtn = toolbarButton('Text colour', '<span class="pw-dot pw-color-dot"></span>' + ICONS.chevron, 'pw-color', function () {
            togglePopover('colors', colorBtn, buildColorMenu);
        });
        colorBtn.setAttribute('aria-haspopup', 'true');
        styleGroup.appendChild(colorBtn);
        chrome.colorDot = colorBtn.querySelector('.pw-color-dot');
        sep();

        var alignGroup = group('pw-g-align', 'text list box');
        alignGroup.appendChild(toolbarButton('Align left', ICONS.alignLeft, 'pw-align-left', function () { setAlign('left'); }));
        alignGroup.appendChild(toolbarButton('Align center', ICONS.alignCenter, 'pw-align-center', function () { setAlign('center'); }));
        alignGroup.appendChild(toolbarButton('Align right', ICONS.alignRight, 'pw-align-right', function () { setAlign('right'); }));
        sep();

        var listGroup = group('pw-g-list', 'text list box');
        var bullets = toolbarButton('Bulleted list', ICONS.bullets, 'pw-bullets', makeBulletedList);
        bullets.dataset.kinds = 'text box';
        listGroup.appendChild(bullets);
        var addBullet = toolbarButton('Add bullet', ICONS.addBullet + '<span>Add bullet</span>', 'pw-add-bullet', function () { addListItem(false); });
        var outdent = toolbarButton('Decrease list level (Shift-Tab)', ICONS.outdent, 'pw-outdent', outdentListItem);
        var indent = toolbarButton('Increase list level (Tab)', ICONS.indent, 'pw-indent', indentListItem);
        [addBullet, outdent, indent].forEach(function (b) {
            b.dataset.kinds = 'list';
            listGroup.appendChild(b);
        });
        sep();

        var texGroup = group('pw-g-tex', 'equation');
        var editTex = toolbarButton('Edit TeX (double-click)', ICONS.tex + '<span>Edit TeX</span>', 'pw-edit-tex pw-primary', function () {
            openEquationEditor(state.selected);
        });
        texGroup.appendChild(editTex);
        chrome.editTex = editTex;
        sep();

        var boxGroup = group('pw-g-box pw-boxrow', 'box equation');
        var boxLabel = document.createElement('span');
        boxLabel.className = 'pw-boxlabel';
        boxLabel.textContent = 'Box';
        boxGroup.appendChild(boxLabel);
        var boxSwatches = document.createElement('span');
        boxSwatches.className = 'pw-group';
        boxGroup.appendChild(boxSwatches);
        chrome.boxSwatches = boxSwatches;
        chrome.boxCustom = colorWell('Custom box colour…', 'boxcolor', function (value) { setBoxColor(value); });
        boxGroup.appendChild(chrome.boxCustom);
        sep();

        var appearsGroup = group('pw-g-appears', 'text list box image table equation');
        var appears = toolbarButton('When this appears in the slide’s reveal', '<span>Appears</span><span class="pw-chip">always</span>', 'pw-appears', function () {
            togglePopover('steps', appears, buildStepMenu);
        });
        appears.setAttribute('aria-haspopup', 'true');
        appearsGroup.appendChild(appears);
        chrome.appearsChip = appears.querySelector('.pw-chip');
        sep();

        var moreGroup = group('pw-g-more', 'text list box image table equation');
        var more = toolbarButton('More', ICONS.more, 'pw-more', function () {
            togglePopover('more', more, buildMoreMenu);
        });
        more.setAttribute('aria-haspopup', 'true');
        moreGroup.appendChild(more);

        var hint = document.createElement('span');
        hint.className = 'pw-hint';
        hint.id = 'presto-wysiwyg-hint';
        toolbar.appendChild(hint);

        document.body.appendChild(toolbar);
        chrome.toolbar = toolbar;

        var status = document.createElement('div');
        status.id = 'presto-wysiwyg-status';
        status.className = 'presto-wysiwyg-chrome';
        status.setAttribute('role', 'status');
        document.body.appendChild(status);
        chrome.status = status;

        applyHiddenStepOpacity();

        chrome.handles = {};
        ['se', 'e', 's', 'w'].forEach(function (kind) {
            var handle = document.createElement('div');
            handle.className = 'presto-wysiwyg-handle presto-wysiwyg-chrome';
            handle.dataset.handle = kind;
            handle.addEventListener('mousedown', function (e) {
                e.preventDefault();
                e.stopPropagation();
                startResize(e, kind);
            });
            document.body.appendChild(handle);
            chrome.handles[kind] = handle;
        });
    }

    // A swatch-like native colour picker (WKWebView opens the system colour
    // panel). Dragging in the panel previews live as one coalesced undo step;
    // the change event commits it.
    function colorWell(title, burstKind, commitValue) {
        var well = document.createElement('label');
        well.className = 'pw-colorwell';
        well.title = title;
        var dot = document.createElement('span');
        dot.className = 'pw-dot';
        well.appendChild(dot);
        var input = document.createElement('input');
        input.type = 'color';
        input.setAttribute('aria-label', title);
        input.addEventListener('mousedown', function (e) { e.stopPropagation(); });
        input.addEventListener('click', function (e) { e.stopPropagation(); });
        input.addEventListener('input', function () { previewColor(burstKind, input.value); });
        input.addEventListener('change', function () {
            var burst = state.coalesce;
            if (burst && burst.kind === burstKind) flushBurst();
            else commitValue(input.value);
            refreshChrome();
        });
        well.appendChild(input);
        return well;
    }

    function swatchButton(entry, onPick, current) {
        var b = toolbarButton(entry.name, '<span class="pw-dot"></span>', 'pw-swatch', function () { onPick(entry.value); });
        var dot = b.querySelector('.pw-dot');
        if (entry.value === null) dot.classList.add('pw-auto');
        else dot.style.background = entry.color || entry.value;
        b.dataset.value = entry.value === null ? '' : entry.value;
        if (current !== undefined && current === entry.value) b.classList.add('on');
        return b;
    }

    function inlineValue(el, prop) {
        return el ? (el.style.getPropertyValue(prop) || '').trim() : '';
    }

    // Built once; later calls only move the "on" ring (rebuilding under the
    // pointer between mousedown and click would swallow the click).
    function renderBoxSwatches() {
        if (!chrome.boxSwatches) return;
        var target = boxTarget(state.selected);
        var current = inlineValue(target, 'background-color');
        if (!chrome.boxSwatches.childElementCount) {
            themeColors().box.forEach(function (entry) {
                chrome.boxSwatches.appendChild(swatchButton(entry, setBoxColor));
            });
        }
        Array.prototype.forEach.call(chrome.boxSwatches.children, function (b) {
            b.classList.toggle('on', !!current && b.dataset.value === current);
        });
        var input = chrome.boxCustom && chrome.boxCustom.querySelector('input');
        if (input && target && !(state.coalesce && state.coalesce.kind === 'boxcolor')) {
            input.value = rgbToHex(getComputedStyle(target).backgroundColor);
        }
    }

    function buildColorMenu(pop) {
        var el = formatTarget();
        var current = inlineValue(el, 'color');
        var title = document.createElement('div');
        title.className = 'pw-pop-title';
        title.textContent = 'Text colour';
        pop.appendChild(title);
        var row = document.createElement('div');
        row.className = 'pw-swatches';
        var pick = function (value) { setColor(value); closePopover(); };
        row.appendChild(swatchButton({ name: 'Theme default', value: null }, pick, current ? undefined : null));
        themeColors().text.forEach(function (entry) { row.appendChild(swatchButton(entry, pick, current)); });
        pop.appendChild(row);
        var custom = document.createElement('div');
        custom.className = 'pw-custom';
        var well = colorWell('Custom colour…', 'color', function (value) { setColor(value); });
        if (el) well.querySelector('input').value = rgbToHex(getComputedStyle(el).color);
        custom.appendChild(well);
        var label = document.createElement('span');
        label.textContent = 'Custom colour…';
        custom.appendChild(label);
        pop.appendChild(custom);
    }

    function checkIcon(on) {
        var c = document.createElement('span');
        c.className = 'pw-check';
        if (on) c.innerHTML = ICONS.check;
        return c;
    }

    function menuItem(label, onPick, opts) {
        opts = opts || {};
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'pw-item' + (opts.cls ? ' ' + opts.cls : '');
        b.appendChild(checkIcon(!!opts.checked));
        var text = document.createElement('span');
        text.textContent = label;
        b.appendChild(text);
        if (opts.kbd) {
            var k = document.createElement('span');
            k.className = 'pw-kbd';
            k.textContent = opts.kbd;
            b.appendChild(k);
        }
        if (opts.title) b.title = opts.title;
        b.disabled = !!opts.disabled;
        b.addEventListener('mousedown', function (e) { e.preventDefault(); e.stopPropagation(); });
        b.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (b.disabled) return;
            closePopover();
            onPick();
        });
        return b;
    }

    // Always, then every existing step, then a new one after the last.
    function buildStepMenu(pop) {
        var el = state.selected;
        var section = activeSlide();
        var info = appearsInfo(el);
        var max = section ? (safeSlideMax(section)) : 0;
        var title = document.createElement('div');
        title.className = 'pw-pop-title';
        title.textContent = 'Appears';
        pop.appendChild(title);
        if (info.own && (info.invalid || (info.end !== Infinity && info.end !== undefined))) {
            pop.appendChild(menuItem(info.text.charAt(0).toUpperCase() + info.text.slice(1), function () {}, { checked: true, disabled: true, title: info.title }));
        }
        var always = info.own ? info.text === 'always' : info.start === 0;
        pop.appendChild(menuItem('Always', function () { setAppearsAt(null); }, { checked: always }));
        for (var k = 1; k <= max; k++) {
            (function (step) {
                var checked = info.own && info.start === step && info.end === Infinity;
                pop.appendChild(menuItem('At step ' + step, function () { setAppearsAt(step); }, { checked: checked }));
            })(k);
        }
        pop.appendChild(menuItem('At a new step (' + (max + 1) + ')', function () { setAppearsAt(max + 1); }));
    }

    function safeSlideMax(section) {
        var max = WS.slideSteps && WS.slideSteps.get ? WS.slideSteps.get(section) : null;
        return Number.isSafeInteger(max) && max >= 0 ? max : computeMaxStep(section);
    }

    function buildMoreMenu(pop) {
        var kind = selectionKind(state.selected);
        if (kind === 'text' || kind === 'list' || kind === 'box') {
            pop.appendChild(menuItem('Edit text', function () {
                if (state.selected) beginTextEdit(state.selected);
            }, { kbd: '↩' }));
        }
        if (kind === 'equation') {
            pop.appendChild(menuItem('Edit TeX…', function () { openEquationEditor(state.selected); }, { kbd: '↩' }));
        }
        pop.appendChild(menuItem('Duplicate', duplicateSelected, { cls: 'pw-duplicate', kbd: '⌘D' }));
        var sepEl = document.createElement('div');
        sepEl.className = 'pw-sep';
        pop.appendChild(sepEl);
        pop.appendChild(menuItem('Delete', deleteSelected, { cls: 'pw-delete pw-danger', kbd: '⌫' }));
    }

    /* ===== Popovers ===== */

    function togglePopover(name, anchor, build) {
        if (state.popover && state.popover.name === name) { closePopover(); return; }
        openPopover(name, anchor, build);
    }

    function openPopover(name, anchor, build, opts) {
        closePopover();
        var pop = document.createElement('div');
        pop.className = 'presto-wysiwyg-chrome pw-pop';
        pop.id = 'presto-wysiwyg-' + name;
        pop.setAttribute('role', name === 'equation' ? 'dialog' : 'menu');
        pop.addEventListener('mousedown', function (e) {
            if (!e.target.closest('input, textarea, label')) e.preventDefault();
        });
        pop.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                closePopover();
            }
        });
        build(pop);
        document.body.appendChild(pop);
        state.popover = { name: name, el: pop, anchor: anchor, onClose: opts && opts.onClose };
        positionPopover(pop, anchor, opts && opts.avoid);
        if (chrome.toolbar && anchor && chrome.toolbar.contains(anchor)) anchor.classList.add('on');
        return pop;
    }

    function closePopover() {
        var p = state.popover;
        if (!p) return false;
        state.popover = null;
        if (p.anchor && p.anchor.classList && chrome.toolbar && chrome.toolbar.contains(p.anchor) &&
            !p.anchor.classList.contains('pw-primary')) {
            p.anchor.classList.remove('on');
        }
        var hadFocus = p.el.contains(document.activeElement);
        if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
        if (p.onClose) { try { p.onClose(); } catch (e) { /* keep closing */ } }
        if (hadFocus) onFocusChange();
        return true;
    }

    // Below the anchor (a toolbar button or an element rect), else above;
    // `avoid` is a rect the popover must not cover (the equation itself).
    function positionPopover(pop, anchor, avoid) {
        var r = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : anchor;
        if (!r) return;
        var w = pop.offsetWidth;
        var h = pop.offsetHeight;
        var minLeft = railRight() + 8;
        var left = Math.max(minLeft, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
        var below = r.bottom + 8;
        var above = r.top - h - 8;
        var top;
        if (avoid) {
            var belowAvoid = avoid.bottom + 10;
            var aboveAvoid = avoid.top - h - 10;
            if (belowAvoid + h <= window.innerHeight - 8) top = belowAvoid;
            else if (aboveAvoid >= 8) top = aboveAvoid;
            else top = Math.max(8, window.innerHeight - h - 8);
        } else if (below + h <= window.innerHeight - 8) top = below;
        else if (above >= 8) top = above;
        else top = Math.max(8, window.innerHeight - h - 8);
        pop.style.left = Math.round(left) + 'px';
        pop.style.top = Math.round(top) + 'px';
    }

    function railRight() {
        var w = parseFloat(getComputedStyle(document.body).getPropertyValue('--presto-rail-w'));
        if (isFinite(w)) return w;
        var rail = document.getElementById('presto-edit-rail');
        if (rail && rail.offsetParent !== null) return rail.getBoundingClientRect().right;
        return 0;
    }

    /* ===== Equation editing in place (BACKLOG 5) ===== */

    function katexOptions(display, throwOnError) {
        var base = window.__wsKatexOptions || {};
        var opts = { displayMode: !!display, throwOnError: !!throwOnError };
        ['macros', 'trust', 'strict', 'minRuleThickness', 'maxSize', 'maxExpand', 'fleqn', 'leqno'].forEach(function (k) {
            if (base[k] !== undefined) opts[k] = base[k];
        });
        return opts;
    }

    function equationTex(el) {
        var ann = el && el.querySelector('annotation[encoding="application/x-tex"]');
        return ann ? ann.textContent : '';
    }

    // Parse with errors thrown (for the inline message), then render the
    // preview leniently so a half-typed equation still shows what parses.
    function renderEquationPreview() {
        var eq = state.equation;
        if (!eq) return;
        var src = eq.textarea.value;
        eq.error.textContent = '';
        eq.preview.classList.remove('pw-eq-empty');
        eq.valid = true;
        if (!src.trim()) {
            eq.preview.textContent = 'Type TeX above';
            eq.preview.classList.add('pw-eq-empty');
            eq.valid = false;
        } else if (!window.katex || typeof katex.render !== 'function') {
            eq.preview.textContent = 'Preview unavailable: KaTeX is not loaded';
            eq.preview.classList.add('pw-eq-empty');
        } else {
            try {
                katex.render(src, eq.preview, katexOptions(eq.display, true));
            } catch (err) {
                eq.valid = false;
                eq.error.textContent = String(err && err.message || err).replace(/^KaTeX parse error:\s*/, '');
                try { katex.render(src, eq.preview, katexOptions(eq.display, false)); } catch (e2) { eq.preview.textContent = src; }
            }
        }
        eq.apply.disabled = !eq.valid;
    }

    function openEquationEditor(el) {
        el = el || state.selected;
        if (!el || !isEquation(el) || !state.enabled) return false;
        var target = el.closest('.katex-display') || el;
        var section = activeSlide();
        if (!section || !section.contains(target)) return false;
        if (state.selected !== target) select(target);
        post({ type: 'wysiwygWillEdit' });
        var display = target.matches('.katex-display');
        var tex = equationTex(target);
        var eq = {
            el: target,
            display: display,
            original: tex,
            index: WS.currentIndex,
            section: section,
            valid: true
        };
        openPopover('equation', target, function (pop) {
            var head = document.createElement('div');
            head.className = 'pw-eq-head';
            var b = document.createElement('b');
            b.textContent = 'Equation';
            head.appendChild(b);
            var mode = document.createElement('span');
            mode.textContent = display ? 'Display' : 'Inline';
            head.appendChild(mode);
            pop.appendChild(head);
            var textarea = document.createElement('textarea');
            textarea.className = 'pw-eq-src';
            textarea.spellcheck = false;
            textarea.setAttribute('autocapitalize', 'off');
            textarea.setAttribute('autocorrect', 'off');
            textarea.setAttribute('aria-label', 'TeX source');
            textarea.rows = Math.min(6, Math.max(2, tex.split('\n').length + (display ? 1 : 0)));
            textarea.value = tex;
            pop.appendChild(textarea);
            var preview = document.createElement('div');
            preview.className = 'pw-eq-preview';
            preview.setAttribute('aria-live', 'polite');
            pop.appendChild(preview);
            var error = document.createElement('div');
            error.className = 'pw-eq-error';
            pop.appendChild(error);
            var foot = document.createElement('div');
            foot.className = 'pw-eq-foot';
            var hintEl = document.createElement('span');
            hintEl.className = 'pw-eq-hint';
            hintEl.textContent = (display ? '⌘↩' : '↩') + ' to apply · Esc to cancel';
            foot.appendChild(hintEl);
            var cancel = document.createElement('button');
            cancel.type = 'button';
            cancel.className = 'pw-btn pw-eq-cancel';
            cancel.textContent = 'Cancel';
            cancel.addEventListener('click', function (e) { e.preventDefault(); closePopover(); });
            foot.appendChild(cancel);
            var apply = document.createElement('button');
            apply.type = 'button';
            apply.className = 'pw-btn primary pw-eq-apply';
            apply.textContent = 'Apply';
            apply.addEventListener('click', function (e) { e.preventDefault(); applyEquationEdit(); });
            foot.appendChild(apply);
            pop.appendChild(foot);
            eq.textarea = textarea;
            eq.preview = preview;
            eq.error = error;
            eq.apply = apply;
            textarea.addEventListener('input', renderEquationPreview);
            textarea.addEventListener('keydown', function (e) {
                e.stopPropagation();
                if (e.key === 'Escape') {
                    e.preventDefault();
                    closePopover();
                } else if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && ((e.metaKey || e.ctrlKey) || !display)) {
                    e.preventDefault();
                    applyEquationEdit();
                }
            });
        }, {
            avoid: target.getBoundingClientRect(),
            onClose: function () {
                if (state.equation === eq) state.equation = null;
                refreshChrome();
            }
        });
        state.equation = eq;
        renderEquationPreview();
        // Re-place now that the preview has its real height.
        if (state.popover) positionPopover(state.popover.el, target, target.getBoundingClientRect());
        eq.textarea.focus();
        eq.textarea.setSelectionRange(eq.textarea.value.length, eq.textarea.value.length);
        onFocusChange();
        refreshChrome();
        return true;
    }

    // Swap the rendered equation for a fresh render of the new TeX (keeping the
    // user's move/format styles). The serializer turns it back into $…$/$$…$$.
    function replaceEquation(target, tex, display) {
        var carry = katexUserStyle(target);
        var fresh = null;
        if (window.katex && typeof katex.render === 'function') {
            var holder = document.createElement('span');
            katex.render(tex, holder, katexOptions(display, false));
            fresh = holder.firstElementChild;
        }
        if (!fresh) {
            var text = document.createTextNode(display ? '$$' + tex + '$$' : '$' + tex + '$');
            target.replaceWith(text);
            return null;
        }
        if (carry.style) fresh.setAttribute('style', carry.style);
        carry.classes.forEach(function (c) { fresh.classList.add(c); });
        target.replaceWith(fresh);
        return fresh;
    }

    function applyEquationEdit() {
        var eq = state.equation;
        if (!eq) return false;
        renderEquationPreview();
        if (!eq.valid) {
            if (eq.textarea) eq.textarea.focus();
            return false;
        }
        var tex = eq.textarea.value;
        var target = eq.el;
        if (tex === eq.original || !target.isConnected) {
            closePopover();
            return false;
        }
        var fresh = null;
        var ok = commitOn(eq.index, function (section) {
            if (!section.contains(target)) return false;
            if (state.selected === target) deselectQuietly();
            fresh = replaceEquation(target, tex, eq.display);
        }, 'Edit Equation');
        state.equation = null;
        closePopover();
        if (ok && fresh && fresh.isConnected) select(fresh);
        else if (ok) {
            var section = WS.slides[eq.index];
            renderMathIfNeeded(section);
        }
        return ok;
    }

    // Drop the selection marker without the side effects of deselect()
    // (which would also close the open equation popover mid-commit).
    function deselectQuietly() {
        if (!state.selected) return;
        state.selected.classList.remove('presto-wysiwyg-selected');
        cleanEmptyClassAttr(state.selected);
        state.selected = null;
        state.selectedIndex = -1;
    }

    function setStatus(cls, text) {
        clearTimeout(state.statusTimer);
        if (chrome.status) {
            chrome.status.className = 'presto-wysiwyg-chrome visible ' + cls;
            chrome.status.textContent = text;
        }
        if (cls === 'saved') {
            state.statusTimer = setTimeout(function () {
                if (chrome.status) chrome.status.classList.remove('visible');
            }, STATUS_FADE_MS);
        }
    }

    function isResizable(el) {
        if (!el) return false;
        if (el.matches('img, svg, video, [data-table-src], .table-container, .text-box')) return true;
        return window.getComputedStyle(el).position === 'absolute';
    }

    function refreshChrome(force) {
        var el = state.selected;
        if (!chrome.toolbar) return;
        if (!el || !el.isConnected) {
            chrome.toolbar.classList.remove('visible');
            hideHandles();
            announceSelection();
            return;
        }
        var kind = selectionKind(el);
        // A link shows its card (presto/edit-chrome.js) instead of the bar;
        // while its label is typed neither is shown.
        var showBar = kind !== 'link';
        if (showBar) {
            updateToolbarState();
            chrome.toolbar.classList.add('visible');
            positionToolbar(el, force);
        } else {
            chrome.toolbar.classList.remove('visible');
        }
        if (isResizable(el) && !state.textEditing && kind !== 'link') {
            positionHandles(el);
        } else {
            hideHandles();
        }
        if (state.popover && state.popover.name !== 'equation' && state.popover.anchor && chrome.toolbar.contains(state.popover.anchor)) {
            if (showBar) positionPopover(state.popover.el, state.popover.anchor);
            else closePopover();
        }
        announceSelection();
    }

    // True while the pointer sits over/near the toolbar or a font-step press is
    // running — the moment the toolbar must NOT move under the user's cursor.
    function toolbarProtected() {
        if (state.coalesce && state.coalesce.kind === 'fontstep') return true;
        if (state.pointerX < 0) return false;
        var r = chrome.toolbar.getBoundingClientRect();
        var m = TOOLBAR_PROTECT_MARGIN;
        return state.pointerX >= r.left - m && state.pointerX <= r.right + m &&
               state.pointerY >= r.top - m && state.pointerY <= r.bottom + m;
    }

    // The content element (if any) directly above the selected element — used to
    // avoid parking the toolbar on top of the line just above it.
    function elementDirectlyAbove(el, rect) {
        var x = rect.left + Math.min(rect.width / 2, 40);
        var y = rect.top - 3;
        if (y < 0) return null;
        var hit = document.elementFromPoint(x, y);
        if (!hit || isChrome(hit) || hit === el || el.contains(hit) || hit.contains(el)) return null;
        return hit;
    }

    function hasOwnText(node) {
        for (var c = node.firstChild; c; c = c.nextSibling) {
            if (c.nodeType === 3 && /\S/.test(c.nodeValue)) return true;
        }
        return false;
    }

    // What a toolbar at this rect would hide (sampled at a few points; the
    // selection itself and its ancestors do not count): 0 nothing, 1 only
    // faint later-step ghosts, 2 content visible now.
    function coversSlideContent(el, left, top, w, h) {
        var slide = activeSlide();
        if (!slide || !document.elementsFromPoint) return 0;
        var worst = 0;
        var xs = [left + 8, left + w / 2, left + w - 8];
        var ys = [top + 4, top + h - 4];
        for (var i = 0; i < xs.length; i++) {
            for (var j = 0; j < ys.length; j++) {
                var hits = document.elementsFromPoint(xs[i], ys[j]);
                for (var k = 0; k < hits.length; k++) {
                    var hit = hits[k];
                    if (isChrome(hit) || !slide.contains(hit) || hit === slide) continue;
                    if (hit === el || el.contains(hit) || hit.contains(el)) continue;
                    var atom = hit.closest(ATOM_SELECTOR);
                    if (atom && (atom === el || atom.contains(el) || el.contains(atom))) continue;
                    // Only real content counts: an atom, or an element with
                    // its own text (a box title next to the selected equation
                    // does; the gap between two bullets does not).
                    if (!atom && !hasOwnText(hit)) continue;
                    if (isHiddenAtCurrentStep(hit)) {
                        if (state.hiddenStepOpacity > 0) worst = Math.max(worst, 1);
                        continue;
                    }
                    return 2;
                }
            }
        }
        return worst;
    }

    // Choose a placement: prefer above with clearance; flip below when there's no
    // room above or when above would cover other content and below would not.
    // Always kept inside the viewport.
    // Placement never covers the selection and never sits under the rail.
    function computeToolbarPlacement(el) {
        var rect = el.getBoundingClientRect();
        var tb = chrome.toolbar.getBoundingClientRect();
        var h = tb.height || 36;
        var w = tb.width || 200;
        var clear = 10;
        var minLeft = railRight() + 6;
        var maxLeft = Math.max(minLeft, window.innerWidth - w - 6);
        var aboveTop = rect.top - h - clear;
        var belowTop = rect.bottom + clear;
        var fitsAbove = aboveTop >= 6;
        var fitsBelow = belowTop + h <= window.innerHeight - 6;
        var left = Math.max(minLeft, Math.min(rect.left + rect.width / 2 - w / 2, maxLeft));
        var coversAbove = 0;
        var above = elementDirectlyAbove(el, rect);
        if (above && above.getBoundingClientRect().bottom > aboveTop) coversAbove = 2;
        // Hit-testing is skipped mid-gesture (it would run on every pointer move).
        var sample = !(state.drag && state.drag.active) && !state.resize;
        if (sample && !coversAbove && fitsAbove) coversAbove = coversSlideContent(el, left, aboveTop, w, h);
        var coversBelow = sample && fitsBelow ? coversSlideContent(el, left, belowTop, w, h) : 0;
        var top;
        if (fitsAbove && !coversAbove) top = aboveTop;
        else if (fitsBelow && !coversBelow) top = belowTop;
        // Both sides hide something (never the selection): hide the lesser.
        else if (fitsAbove && fitsBelow) top = coversBelow < coversAbove ? belowTop : aboveTop;
        else if (fitsAbove) top = aboveTop;
        else if (fitsBelow) top = belowTop;
        else {
            // A very tall selection: beside it if there is room, else pinned
            // inside its top edge (the only place left).
            top = Math.max(6, Math.min(rect.top, window.innerHeight - h - 6));
            if (rect.right + clear + w <= window.innerWidth - 6) left = rect.right + clear;
            else if (rect.left - clear - w >= minLeft) left = rect.left - clear - w;
            else top = Math.max(6, rect.top + 6);
        }
        return { top: top, left: left };
    }

    // `force` (selection change, gesture end, window resize) always re-anchors.
    // Otherwise the toolbar stays put while the pointer is over/near it, unless
    // the element drifted far enough that the toolbar would be orphaned.
    function positionToolbar(el, force) {
        var toolbar = chrome.toolbar;
        var place = computeToolbarPlacement(el);
        if (!force && hasToolbarPos() && toolbarProtected()) {
            var curTop = parseFloat(toolbar.style.top);
            var curLeft = parseFloat(toolbar.style.left);
            var tbRect = toolbar.getBoundingClientRect();
            var rect = el.getBoundingClientRect();
            var onScreen = curTop >= 4 && curLeft >= 4 &&
                curTop + tbRect.height <= window.innerHeight - 4 &&
                curLeft + tbRect.width <= window.innerWidth - 4;
            var gap = Math.max(rect.top - (curTop + tbRect.height), curTop - rect.bottom, 0);
            if (onScreen && gap <= TOOLBAR_ORPHAN_GAP) return;   // sticky: leave it
        }
        toolbar.style.top = Math.round(place.top) + 'px';
        toolbar.style.left = Math.round(place.left) + 'px';
    }

    function hasToolbarPos() {
        return chrome.toolbar && chrome.toolbar.style.top !== '' && chrome.toolbar.style.left !== '';
    }

    function positionHandles(el) {
        var rect = el.getBoundingClientRect();
        // 9px handles centred on the selection outline (1px outside the edge).
        var rightX = Math.round(rect.right + 1 - 4.5);
        var leftX = Math.round(rect.left - 1 - 4.5);
        var midX = Math.round(rect.left + rect.width / 2 - 4.5);
        var midY = Math.round(rect.top + rect.height / 2 - 4.5);
        var bottomY = Math.round(rect.bottom + 1 - 4.5);
        placeHandle('se', rightX, bottomY);
        placeHandle('e', rightX, midY);
        placeHandle('s', midX, bottomY);
        placeHandle('w', leftX, midY);
    }

    function placeHandle(kind, left, top) {
        var h = chrome.handles[kind];
        if (!h) return;
        h.style.left = left + 'px';
        h.style.top = top + 'px';
        h.classList.add('visible');
    }

    function hideHandles() {
        if (!chrome.handles) return;
        Object.keys(chrome.handles).forEach(function (k) {
            chrome.handles[k].classList.remove('visible');
        });
    }

    // Show only the groups that apply to the selected kind, then drop the
    // dividers that no longer separate two visible groups.
    function applyKindVisibility(kind) {
        var box = boxTarget(state.selected);
        (chrome.groups || []).forEach(function (g) {
            var kinds = g.dataset.kinds.split(' ');
            var show = kinds.indexOf(kind) >= 0;
            if (g.classList.contains('pw-g-box')) show = show && !!box;
            if (g.classList.contains('pw-g-kind')) show = show && !!kindLabelFor(kind);
            g.hidden = !show;
            g.querySelectorAll('[data-kinds]').forEach(function (b) {
                b.hidden = b.dataset.kinds.split(' ').indexOf(kind) < 0;
            });
        });
        var children = Array.prototype.slice.call(chrome.toolbar.children);
        var seenGroup = false;
        var pending = null;
        children.forEach(function (child) {
            if (child.classList.contains('pw-divider')) {
                child.hidden = true;
                if (seenGroup) pending = child;
                return;
            }
            if (!child.classList.contains('pw-group') || child.hidden) return;
            if (pending) { pending.hidden = false; pending = null; }
            seenGroup = true;
        });
    }

    function kindLabelFor(kind) {
        if (kind === 'box' && state.selected && state.selected.matches('.text-box')) return '';
        return KIND_LABELS[kind] || '';
    }

    function updateToolbarState() {
        if (!chrome.toolbar || !chrome.fontInput) return;
        var el = state.selected;
        var kind = el ? selectionKind(el) : null;
        if (kind) applyKindVisibility(kind);
        if (chrome.kindLabel) chrome.kindLabel.textContent = kindLabelFor(kind);
        if (el && document.activeElement !== chrome.fontInput) {
            var size = currentFontSizePx();
            chrome.fontInput.value = size === null ? '' : String(size);
        }
        var hint = document.getElementById('presto-wysiwyg-hint');
        if (hint) {
            var label = '';
            if (el) {
                if (el.matches('[data-table-src]')) label = 'External table: edit its source file';
                else if (state.overlapCount > 1) label = 'Overlapping objects: click again to select behind';
            }
            hint.textContent = label;
            hint.style.display = label ? '' : 'none';
        }
        if (el) {
            var style = window.getComputedStyle(el);
            setToolbarToggle('pw-b', parseInt(style.fontWeight, 10) >= 600);
            setToolbarToggle('pw-i', style.fontStyle === 'italic');
            setToolbarToggle('pw-u', style.textDecorationLine.indexOf('underline') >= 0);
            if (chrome.colorDot) chrome.colorDot.style.background = style.color;
            if (chrome.appearsChip) {
                var info = appearsInfo(el);
                chrome.appearsChip.textContent = info.text;
                chrome.appearsChip.classList.toggle('pw-invalid', !!info.invalid);
                chrome.appearsChip.parentElement.title = info.title || 'When this appears';
            }
            if (kind === 'box' || kind === 'equation') renderBoxSwatches();
        }
        if (chrome.editTex) chrome.editTex.disabled = !(el && isEquation(el));
        var item = currentListItem();
        setToolbarToggle('pw-bullets', !!item);
        setToolbarDisabled('pw-bullets', !el || !!item || !canTextEdit(el));
        setToolbarDisabled('pw-add-bullet', !item);
        setToolbarDisabled('pw-indent', !canIndentListItem(item));
        setToolbarDisabled('pw-outdent', !canOutdentListItem(item));
    }

    function setToolbarToggle(cls, on) {
        var btn = chrome.toolbar.querySelector('.' + cls);
        if (btn) btn.classList.toggle('on', !!on);
    }

    function setToolbarDisabled(cls, disabled) {
        var btn = chrome.toolbar.querySelector('.' + cls);
        if (btn) btn.disabled = !!disabled;
    }

    /* ===== Drag to move ===== */

    function startPotentialDrag(e, el) {
        flushBurst();
        state.drag = {
            el: el,
            startClientX: e.clientX,
            startClientY: e.clientY,
            base: analyzeTranslate(el),
            section: null,
            index: -1,
            snapshot: null,
            active: false
        };
    }

    function onDragMove(e) {
        var drag = state.drag;
        if (!drag) return;
        var dx = e.clientX - drag.startClientX;
        var dy = e.clientY - drag.startClientY;
        if (!drag.active) {
            if (Math.abs(dx) < DRAG_THRESHOLD_PX && Math.abs(dy) < DRAG_THRESHOLD_PX) return;
            if (state.stackPick) state.stackPick.dragged = true;
            if (drag.base.kind === 'opaque') {
                // Authored translate uses calc()/z we can't safely rewrite.
                setStatus('error', OPAQUE_MOVE_MESSAGE);
                state.drag = null;
                return;
            }
            var section = activeSlide();
            drag.section = section;
            drag.index = wsReady() ? WS.currentIndex : -1;
            drag.snapshot = section ? captureSnapshot(section) : null;
            drag.active = true;
            document.body.style.cursor = 'move';
            if (window.__prestoWysiwygGuides && drag.base.kind === 'px' && section) {
                __prestoWysiwygGuides.dragStart(drag.el, section);
            }
        }
        if (e.shiftKey) {
            if (Math.abs(dx) > Math.abs(dy)) dy = 0; else dx = 0;
        }
        var scale = slideScale(activeSlide() || drag.el);
        if (drag.base.kind === 'unit') {
            // Compose from the authored non-px base each move (no calc nesting).
            drag.el.style.translate = calcAddLength(drag.base.x, Math.round(dx / scale)) + ' ' +
                calcAddLength(drag.base.y, Math.round(dy / scale));
            cleanEmptyStyleAttr(drag.el);
        } else {
            var nx = drag.base.x + dx / scale;
            var ny = drag.base.y + dy / scale;
            if (window.__prestoWysiwygGuides) {
                var snapped = __prestoWysiwygGuides.dragMove(
                    drag.el, activeSlide(), nx, ny, { alt: e.altKey });
                nx = snapped.x;
                ny = snapped.y;
            }
            setTranslate(drag.el, nx, ny);
        }
        refreshChrome();
        e.preventDefault();
    }

    function onDragEnd() {
        if (window.__prestoWysiwygGuides) __prestoWysiwygGuides.dragEnd();
        var drag = state.drag;
        state.drag = null;
        document.body.style.cursor = '';
        if (!drag || !drag.active) return;
        commitOwnedSection(drag.index, drag.section, drag.snapshot, 'Move');
        refreshChrome(true);
    }

    /* ===== Resize =====
     *
     * Handles (positionHandles): se (corner, 2D), e (right edge, width), s
     * (bottom edge, height), w (left edge, width — RIGHT edge anchored). Corner
     * and edge handles that move the right/bottom edges are top-left anchored, so
     * their proposed width/height route through the guides module (which snaps
     * those moving edges). The w handle anchors the RIGHT edge — a case the guides
     * module can't express — so it never routes through and is applied raw.
     *
     * img/svg/video stay aspect-locked on width-changing drags (height:auto) so a
     * corner/edge drag scales the image proportionally; Shift on the corner frees
     * the ratio (explicit height). Boxes/.text-box/table containers get explicit
     * width and/or height per the handle grabbed.
     */

    var MIN_RESIZE_PX = 24;

    function isMediaEl(el) {
        return !!(el.matches && el.matches('img, svg, video'));
    }

    function startResize(e, kind) {
        var el = state.selected;
        if (!el) return;
        var section = activeSlide();
        var scale = section ? slideScale(section) : 1;
        var rect = el.getBoundingClientRect();
        state.resize = {
            el: el,
            kind: kind,
            startClientX: e.clientX,
            startClientY: e.clientY,
            startWidth: rect.width / scale,
            startHeight: rect.height / scale,
            isMedia: isMediaEl(el),
            isAbsolute: window.getComputedStyle(el).position === 'absolute',
            baseLeft: el.style.left || '',
            baseTranslate: analyzeTranslate(el),
            section: section,
            index: wsReady() ? WS.currentIndex : -1,
            snapshot: section ? captureSnapshot(section) : null,
            active: false
        };
        if (window.__prestoWysiwygGuides && section) {
            __prestoWysiwygGuides.resizeStart(el, section);
        }
    }

    // Keep the element's RIGHT edge fixed while its width changed: shift the left
    // edge by `shift` slide-px (negative = grew leftward). Absolute elements move
    // via `left`; translate-positioned elements via the standalone translate.
    function applyHorizontalShift(resize, shift) {
        var el = resize.el;
        shift = Math.round(shift);
        if (resize.isAbsolute) {
            var left = calcAddLength(resize.baseLeft, shift);
            if (left === '' || left === '0') el.style.removeProperty('left');
            else el.style.left = left;
        } else {
            var b = resize.baseTranslate;
            if (b.kind === 'px') {
                setTranslate(el, b.x + shift, b.y);
            } else if (b.kind === 'unit') {
                el.style.translate = calcAddLength(b.x, shift) + ' ' + b.y;
            }
            // opaque translate (calc/z): position can't be safely rewritten; the
            // width change still applies, growing rightward as a fallback.
        }
        cleanEmptyStyleAttr(el);
    }

    function onResizeMove(e) {
        var resize = state.resize;
        if (!resize) return;
        var dx = e.clientX - resize.startClientX;
        var dy = e.clientY - resize.startClientY;
        // ±2px activation on whichever axis this handle actually drives.
        if (!resize.active && Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
        resize.active = true;

        var slide = activeSlide();
        var scale = slide ? slideScale(slide) : 1;
        var kind = resize.kind;
        var dxS = dx / scale;
        var dyS = dy / scale;

        var anchorRight = kind === 'w';
        var changesWidth = kind === 'e' || kind === 'w' || kind === 'se';
        // A corner drag on media stays aspect-locked (no explicit height) unless
        // Shift frees the ratio; every other height-changing handle is explicit.
        var setsHeight = kind === 's' || (kind === 'se' && !(resize.isMedia && !e.shiftKey));

        var proposedWidth = resize.startWidth;
        if (kind === 'e' || kind === 'se') proposedWidth = resize.startWidth + dxS;
        else if (kind === 'w') proposedWidth = resize.startWidth - dxS;

        var proposedHeight;
        if (kind === 's' || kind === 'se') proposedHeight = resize.startHeight + dyS;

        // Guides snapping — only for right/bottom-edge handles (the module snaps
        // those moving edges). w anchors the right edge, so it never routes.
        if (window.__prestoWysiwygGuides && slide && !anchorRight) {
            var gW = changesWidth ? proposedWidth : resize.startWidth;
            var gH = setsHeight ? proposedHeight : undefined;
            var snapped = __prestoWysiwygGuides.resizeMove(
                resize.el, slide, gW, gH, { alt: e.altKey });
            if (changesWidth && typeof snapped.width === 'number') proposedWidth = snapped.width;
            if (setsHeight && typeof snapped.height === 'number') proposedHeight = snapped.height;
        }

        // Media whose width changes but whose height is not explicit stays
        // aspect-locked via height:auto.
        var mediaAuto = resize.isMedia && changesWidth && !setsHeight;

        if (changesWidth) {
            proposedWidth = Math.max(MIN_RESIZE_PX, proposedWidth);
            resize.el.style.width = Math.round(proposedWidth) + 'px';
            if (anchorRight) applyHorizontalShift(resize, resize.startWidth - proposedWidth);
        }
        if (mediaAuto) {
            resize.el.style.height = 'auto';
        } else if (setsHeight && typeof proposedHeight === 'number') {
            resize.el.style.height = Math.max(MIN_RESIZE_PX, Math.round(proposedHeight)) + 'px';
        }

        refreshChrome();
        e.preventDefault();
    }

    function onResizeEnd() {
        if (window.__prestoWysiwygGuides) __prestoWysiwygGuides.resizeEnd();
        var resize = state.resize;
        state.resize = null;
        if (!resize || !resize.active) return;
        var committed = commitOwnedSection(resize.index, resize.section, resize.snapshot, 'Resize');
        // A resized table container keeps a stale scale transform + fixed height
        // computed for its OLD width until the engine re-fits it. Saving already
        // happened above with fit styles stripped, so this only re-fits the LIVE
        // table to the new width; it never dirties the saved source.
        if (committed && resize.el && resize.el.isConnected &&
            resize.el.matches('[data-table-src], .table-container') &&
            window.WS && typeof WS.refitTables === 'function') {
            var refit = WS.refitTables();
            if (refit && typeof refit.catch === 'function') refit.catch(function () {});
        }
        refreshChrome();
    }

    /* ===== Event handlers (capture phase — run before the engine) ===== */

    function overviewActive() {
        return !!(window.WS && WS.overviewActive);
    }

    function onMouseDown(e) {
        if (!state.enabled || overviewActive()) return;
        if (e.button !== 0) return;
        state.stackPick = null;
        state.swallowClick = false;
        state.pointerX = e.clientX;
        state.pointerY = e.clientY;
        var target = e.target;
        // A click anywhere but the open popover (or the button that opened it)
        // closes it; an equation popover's Cancel is implicit.
        if (state.popover && !state.popover.el.contains(target) &&
            !(state.popover.anchor && state.popover.anchor.contains && state.popover.anchor.contains(target))) {
            closePopover();
        }
        if (isChrome(target)) {
            // A gesture starting on chrome (resize handle, toolbar button) ends
            // any open nudge/font burst first so its undo step lands before the
            // new gesture's. Runs in capture, before the handle's own mousedown.
            flushBurst();
            return;
        }

        var slide = activeSlide();
        if (!slide) return;

        // ⌘-click on a link goes straight to its target (decision 7).
        if ((e.metaKey || e.ctrlKey) && slide.contains(target) && !state.textEditing && followLinkAt(target, slide)) {
            e.stopImmediatePropagation();
            e.preventDefault();
            state.swallowClick = true;
            return;
        }

        // A pending edit typed into the font field commits when the user clicks
        // back onto the slide (blur would otherwise be cancelled by preventDefault).
        commitFontInputValue();
        // Selection prevents the browser's default focus change. Release chrome
        // focus explicitly so subsequent keys reach the selected slide object.
        if (document.activeElement && isChrome(document.activeElement)) {
            document.activeElement.blur();
        }

        var inSlide = slide.contains(target);
        if (!inSlide) {
            // Clicking chrome outside the slide (sidebar, deck background):
            // commit any text session, drop selection, let the engine handle it.
            if (state.textEditing) endTextEdit(false);
            else if (state.selected) deselect();
            return;
        }

        e.stopImmediatePropagation();

        if (state.textEditing) {
            if (state.selected && state.selected.contains(target)) {
                // Caret placement inside the active text session — native behavior.
                return;
            }
            endTextEdit(false);
        }

        post({ type: 'wysiwygWillEdit' });

        var stack = selectableStackAtPoint(target, e.clientX, e.clientY, slide);
        var wasSelected = !!(state.selected && stack.indexOf(state.selected) >= 0);
        var el = wasSelected ? state.selected : stack[0];
        if (el) {
            selectFromStack(el, stack.length);
            state.stackPick = {
                candidates: stack,
                start: el,
                cycle: wasSelected,
                dragged: false
            };
            startPotentialDrag(e, el);
            // Prevent native text-selection drag outside a text session.
            e.preventDefault();
        } else {
            deselect();
        }
    }

    function onMouseMove(e) {
        if (!state.enabled || overviewActive()) return;
        state.pointerX = e.clientX;
        state.pointerY = e.clientY;
        if (state.drag) { onDragMove(e); return; }
        if (state.resize) { onResizeMove(e); return; }
        var slide = activeSlide();
        if (!slide) return;
        if (state.textEditing) { setHover(null); return; }
        var target = e.target;
        if (isChrome(target) || !slide.contains(target)) { setHover(null); return; }
        // Same visible-first priority as selection, so hover previews what a
        // click would pick when reveals overlap.
        var el = selectableStackAtPoint(target, e.clientX, e.clientY, slide)[0] || null;
        setHover(el && el !== state.selected ? el : null);
    }

    function onMouseUp() {
        if (!state.enabled) return;
        if (state.drag) onDragEnd();
        if (state.resize) onResizeEnd();
    }

    function onClick(e) {
        if (!state.enabled) return;
        if (state.swallowClick) {
            // The click that ends a ⌘-click on a link: already followed.
            state.swallowClick = false;
            e.stopImmediatePropagation();
            e.preventDefault();
            return;
        }
        if (overviewActive()) return;
        var stackPick = state.stackPick;
        state.stackPick = null;
        var target = e.target;
        if (isChrome(target)) return;
        var slide = activeSlide();
        if (!slide || !slide.contains(target)) return;
        // Triple-click inside a text session selects the whole block (the word
        // was selected by the double-click that opened the session).
        if (state.textEditing && e.detail >= 3 && state.selected && state.selected.contains(target)) {
            selectBlockContents(state.selected);
        }
        if (e.detail < 2 && stackPick && stackPick.cycle && !stackPick.dragged && stackPick.candidates.length > 1) {
            var index = stackPick.candidates.indexOf(stackPick.start);
            var next = stackPick.candidates[(index + 1) % stackPick.candidates.length];
            if (next && next.isConnected) selectFromStack(next, stackPick.candidates.length);
        }
        // Block engine click-navigation and link-follow inside the edited slide.
        e.stopImmediatePropagation();
        e.preventDefault();
    }

    function onDblClick(e) {
        if (!state.enabled || overviewActive()) return;
        var target = e.target;
        if (isChrome(target)) return;
        var slide = activeSlide();
        if (!slide || !slide.contains(target)) return;
        e.stopImmediatePropagation();
        e.preventDefault();

        var stack = selectableStackAtPoint(target, e.clientX, e.clientY, slide);
        var el = stack[0];
        if (el && canTextEdit(el)) {
            if (beginTextEdit(el, e.clientX, e.clientY)) {
                state.overlapCount = stack.length;
                refreshChrome();
            }
        } else if (el && isEquation(el)) {
            openEquationEditor(el);
        } else if (!el) {
            var point = slidePointFromClient(slide, e.clientX, e.clientY);
            if (point.x >= 0 && point.x <= SLIDE_WIDTH && point.y >= 0 && point.y <= SLIDE_HEIGHT) {
                addTextBoxAt(point.x, point.y);
            }
        }
    }

    // Escape in Edit, one layer at a time: close a popover, then end a text
    // session, then deselect, then leave the overview (BACKLOG 1: the engine's
    // class is ws-overview-active). Never leaves the editor. Returns what it did.
    function handleEscape() {
        if (closePopover()) return 'popover';
        if (window.PrestoEditChrome && typeof PrestoEditChrome.closePopover === 'function' &&
            PrestoEditChrome.closePopover()) return 'popover';
        if (state.textEditing) { endTextEdit(true); return 'text'; }
        if (state.selected) { deselect(); return 'deselect'; }
        if (overviewActive() || document.body.classList.contains('ws-overview-active')) {
            if (window.WS && typeof WS.toggleOverview === 'function') WS.toggleOverview();
            return 'overview';
        }
        return 'none';
    }

    function onKeyDown(e) {
        if (!state.enabled) return;
        // Keys typed into editing chrome (e.g. the font-size input) belong to
        // that control's own handlers, not the selection/text-session model.
        if (isChrome(e.target)) return;
        if (e.key === 'Escape' && !e.isComposing) {
            if (handleEscape() !== 'none') {
                e.preventDefault();
                e.stopImmediatePropagation();
            }
            return;
        }
        if (overviewActive()) return;
        var meta = e.metaKey || e.ctrlKey;

        // ⇧⌘O toggles future-step opacity from anywhere in edit mode.
        if (meta && e.shiftKey && (e.key === 'o' || e.key === 'O')) {
            e.preventDefault();
            e.stopImmediatePropagation();
            toggleHiddenStepOpacity();
            return;
        }

        if (state.textEditing) {
            // The engine and Swift must never see keys typed into a text session.
            e.stopImmediatePropagation();
            if (e.key === 'Enter' && !e.isComposing && state.selected && isLink(state.selected)) {
                // A link label is one line: Return finishes it.
                e.preventDefault();
                endTextEdit(true);
                return;
            }
            if (e.key === 'Enter' && !meta && !e.isComposing) {
                e.preventDefault();
                if (!e.shiftKey && currentListItem()) {
                    addListItem(true);
                } else {
                    document.execCommand('insertLineBreak');
                }
                return;
            }
            if (e.key === 'Tab') {
                e.preventDefault();
                if (e.shiftKey) outdentListItem(); else indentListItem();
                return;
            }
            if (meta && (e.key === 's' || e.key === 'S')) {
                e.preventDefault();
                // Commit the running session against its OWNING slide but stay in it.
                var section = state.textEditSection;
                var index = state.textEditIndex;
                if (section && state.textEditSnapshot && section.isConnected &&
                    liveHTML(section) !== state.textEditSnapshot.live) {
                    pushUndo(index, state.textEditSnapshot, state.newBox ? 'Add Text Box' : 'Typing');
                    // The box is in the history now: it is no longer accidental.
                    state.newBox = null;
                    state.textEditSnapshot = captureSnapshot(section);
                    saveCapturedSection(index, section);
                }
                return;
            }
            if (meta && (e.key === 'b' || e.key === 'B')) { e.preventDefault(); toggleBold(); return; }
            if (meta && (e.key === 'i' || e.key === 'I')) { e.preventDefault(); toggleItalic(); return; }
            if (meta && (e.key === 'u' || e.key === 'U')) { e.preventDefault(); toggleUnderline(); return; }
            // Everything else (typing, ⌘Z native undo, arrows) is native.
            return;
        }

        if (state.selected) {
            // ⌘←/⌘→ (and Ctrl) navigate slides — never nudge. Commit any pending
            // burst, drop the selection, and let the event reach the engine's
            // keyboard navigation (webslides.js listens for ArrowLeft/Right). Do
            // NOT swallow or preventDefault: the whole point is that it propagates.
            if (meta && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
                deselect();
                return;
            }
            e.stopImmediatePropagation();
            if (e.key === 'Backspace' || e.key === 'Delete') { e.preventDefault(); deleteSelected(); return; }
            if (e.key === 'Enter') {
                e.preventDefault();
                if (isEquation(state.selected)) openEquationEditor(state.selected);
                else if (canTextEdit(state.selected)) beginTextEdit(state.selected);
                return;
            }
            // Plain-arrow nudging only (⌘/Ctrl arrows are navigation, handled
            // above or a no-op for up/down). Coalesced: a held/rapid run of
            // arrows is one undo step + one save, flushed on keyup / idle.
            if (!meta) {
                var nudge = e.shiftKey ? NUDGE_STEP_LARGE : NUDGE_STEP;
                if (e.key === 'ArrowLeft') { e.preventDefault(); coalescedNudge(-nudge, 0); return; }
                if (e.key === 'ArrowRight') { e.preventDefault(); coalescedNudge(nudge, 0); return; }
                if (e.key === 'ArrowUp') { e.preventDefault(); coalescedNudge(0, -nudge); return; }
                if (e.key === 'ArrowDown') { e.preventDefault(); coalescedNudge(0, nudge); return; }
            }
            if (meta && (e.key === 'd' || e.key === 'D')) { e.preventDefault(); duplicateSelected(); return; }
            if (meta && (e.key === 'b' || e.key === 'B')) { e.preventDefault(); toggleBold(); return; }
            if (meta && (e.key === 'i' || e.key === 'I')) { e.preventDefault(); toggleItalic(); return; }
            if (meta && (e.key === 'u' || e.key === 'U')) { e.preventDefault(); toggleUnderline(); return; }
            if (meta && (e.key === 'z' || e.key === 'Z')) {
                e.preventDefault();
                if (e.shiftKey) redo(); else undo();
                return;
            }
            // Replace-on-type: a printable key on a text-editable element starts a
            // session and replaces its contents (Keynote-style). The keystroke is
            // NOT preventDefault'd, so it natively replaces the selected contents.
            // Atoms (image/table/equation) ignore it.
            if (!meta && !e.altKey && e.key.length === 1 && canTextEdit(state.selected)) {
                var typeTarget = state.selected;
                if (beginTextEdit(typeTarget)) selectBlockContents(typeTarget);
                return;
            }
            return;
        }

        // Nothing selected: only claim undo/redo when we have history for the slide.
        if (meta && (e.key === 'z' || e.key === 'Z') && wsReady()) {
            var h = historyFor(WS.currentIndex);
            if ((e.shiftKey && h.redo.length > 0) || (!e.shiftKey && h.undo.length > 0)) {
                e.stopImmediatePropagation();
                e.preventDefault();
                if (e.shiftKey) redo(); else undo();
            }
        }
    }

    function onKeyUp(e) {
        if (!state.enabled) return;
        // Releasing an arrow ends the nudge / font-step burst it was driving, so
        // a single press commits promptly (one undo step + one save) — the idle
        // timer is only a fallback if a keyup never arrives.
        if (state.coalesce && (e.key === 'ArrowLeft' || e.key === 'ArrowRight' ||
            e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
            flushBurst();
        }
    }

    function onPaste(e) {
        if (!state.enabled || !state.textEditing) return;
        if (!state.selected || !state.selected.contains(e.target)) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        var text = (e.clipboardData || window.clipboardData).getData('text/plain');
        if (text) document.execCommand('insertText', false, text);
    }

    function onInput(e) {
        if (state.newBox && e.target && state.newBox.contains(e.target)) state.newBoxTyped = true;
    }

    function onNavigate() {
        // A step refresh this layer started (refreshSteps) re-fires 'navigate'
        // for the same slide: keep the session and the selection.
        if (state.suppressNavigate > 0) {
            refreshChrome();
            return;
        }
        // Slide switched underneath an editing session (sidebar click, ⌘←/→).
        // endTextEdit commits to the OWNING slide (captured at session start), so
        // the typed text is saved to the right slide even though WS.currentIndex
        // has already advanced. Flush any coalesced burst to its owning slide too.
        flushBurst();
        if (state.textEditing) endTextEdit(false);
        else if (state.selected && (!state.selected.isConnected ||
                 !activeSlide() || !activeSlide().contains(state.selected))) {
            deselect();
        }
        if (state.popover && state.popover.name === 'equation' && state.equation &&
            state.equation.index !== WS.currentIndex) closePopover();
        // The rail is about to be looked at — render any coalesced thumbnails now.
        flushThumbnails();
        refreshChrome(true);
        postUndoState();
    }

    function onResizeWindow() {
        refreshChrome(true);
    }

    function startWatchdog() {
        stopWatchdog();
        state.watchdogTimer = setInterval(function () {
            // Fallback for external DOM replacement (code-pane save, undo of an
            // external edit) when Swift didn't call notifyExternalReplace. Recover
            // gracefully if our selection detached, and clear the now-stale undo
            // history for the affected slide so ⌘Z can't resurrect old content.
            if (state.selected && !state.selected.isConnected) {
                var staleIndex = state.textEditing ? state.textEditIndex : state.selectedIndex;
                if (typeof staleIndex === 'number' && staleIndex >= 0) {
                    delete state.history[staleIndex];
                    postUndoState();
                }
                closePopover();
                clearSessionState();
                state.selected.classList.remove('presto-wysiwyg-selected',
                    'presto-wysiwyg-hover', 'presto-wysiwyg-editing');
                state.selected = null;
                state.selectedIndex = -1;
                setHover(null);
                updateToolbarState();
                refreshChrome();
                postEditingState();
            }
        }, 300);
    }

    function stopWatchdog() {
        if (state.watchdogTimer) {
            clearInterval(state.watchdogTimer);
            state.watchdogTimer = null;
        }
    }

    function onWindowBlur() {
        if (!state.enabled) return;
        flushBurst();
        if (state.textEditing) endTextEdit(true);
    }

    /* ===== Install / enable ===== */

    var listeners = [
        ['mousedown', onMouseDown, true],
        ['mousemove', onMouseMove, true],
        ['mouseup', onMouseUp, true],
        ['click', onClick, true],
        ['dblclick', onDblClick, true],
        ['keydown', onKeyDown, true],
        ['keyup', onKeyUp, true],
        ['paste', onPaste, true],
        ['input', onInput, true],
        ['focusin', onFocusIn, true],
        ['focusout', onFocusOut, true]
    ];

    function install() {
        if (state.installed) return;
        state.installed = true;
        buildChrome();
        listeners.forEach(function (l) {
            document.addEventListener(l[0], l[1], l[2]);
        });
        window.addEventListener('resize', onResizeWindow);
        // Raw HTML now lives in another WKWebView inside the same native
        // window. Browser blur is the boundary between the two edit surfaces.
        window.addEventListener('blur', onWindowBlur);
        if (window.WS && typeof WS.on === 'function') {
            WS.on('navigate', onNavigate);
        }
        startWatchdog();
    }

    function enable() {
        if (state.enabled) return;
        if (!wsReady()) {
            requestAnimationFrame(enable);
            return;
        }
        // The edit chrome, when installed first, carries the shell's setting
        // (Presto's web views do not persist localStorage).
        var chromeState = window.PrestoEditChrome && typeof PrestoEditChrome.state === 'function'
            ? PrestoEditChrome.state() : null;
        state.hiddenStepOpacity = chromeState && chromeState.installed
            ? (chromeState.ghostFuture ? GHOST_OPACITY : 0)
            : readHiddenStepOpacity();
        install();
        state.enabled = true;
        document.body.classList.add('presto-wysiwyg-on');
        applyHiddenStepOpacity();
        postUndoState();
    }

    function disable() {
        if (!state.enabled) return;
        endTextEdit(false);
        deselect();
        flushThumbnails();
        state.enabled = false;
        document.body.classList.remove('presto-wysiwyg-on');
        if (chrome.toolbar) chrome.toolbar.classList.remove('visible');
        hideHandles();
    }

    // Swift calls this right before it externally replaces a slide's DOM (a
    // code-pane save it originated, or an external-edit undo). Clears that slide's
    // now-stale WYSIWYG undo/redo history so ⌘Z can't resurrect pre-replace
    // content and write it over the code-pane edit, and abandons any live session
    // on that slide WITHOUT saving its (about-to-be-overwritten) content.
    function notifyExternalReplace(index) {
        if (!wsReady()) return false;
        var i = typeof index === 'number' ? index : parseInt(index, 10);
        if (!isFinite(i) || i < 0) return false;
        // Drop (do NOT commit) live edits on the replaced slide — its content
        // is about to be overwritten, so a flush would clobber it.
        dropLiveEditsOn(i);
        delete state.history[i];
        // Drop a selection on the replaced (or now-detached) slide, cleaning up
        // its selection class so it can't linger in the DOM.
        if (state.selected && (!state.selected.isConnected || state.selectedIndex === i)) {
            deselect();
        }
        updateToolbarState();
        refreshChrome();
        postEditingState();
        postUndoState();
        scheduleThumbnail(i);
        return true;
    }

    // Swift calls this on editor close / navigation to flush a running text
    // session to disk before the preview goes away. Returns whether one existed.
    function commitActiveSession() {
        commitFontInputValue();
        flushBurst();   // flush any pending nudge/font burst before the preview goes away
        var had = state.textEditing;
        if (had) endTextEdit(true);
        return had;
    }

    function editEquation() {
        return isEquation(state.selected) ? openEquationEditor(state.selected) : false;
    }

    /* ===== Public API (Swift callbacks + tests) ===== */

    // The shell-facing API (contract: window.PrestoEdit).
    window.PrestoEdit = {
        addTextBox: addTextBoxCentered,
        undo: undo,
        redo: redo,
        historyState: function () { return historyState(); },
        recordExternalChange: recordExternalChange,
        refineHistory: refineHistory,
        editEquation: editEquation,
        handleEscape: handleEscape
    };

    window.__prestoWysiwyg = {
        version: 1,

        enable: enable,
        disable: disable,

        // Cross-flow contract with Presto.swift (see notifyExternalReplace /
        // commitActiveSession above).
        notifyExternalReplace: notifyExternalReplace,
        commitActiveSession: commitActiveSession,
        setSourceSidebarVisible: setSourceSidebarVisible,
        setPDFExportAvailable: setPDFExportAvailable,
        toggleSourceSidebar: requestSourceSidebarToggle,
        exportPDF: requestPDFExport,
        setGhostFuture: setGhostFuture,
        recordExternalChange: recordExternalChange,
        historyState: function (index) { return historyState(index); },
        refineHistory: refineHistory,
        handleEscape: handleEscape,

        saveConfirmed: function (saveId) {
            // Swift may confirm only the newest save of a burst.
            Object.keys(state.pendingHistory).forEach(function (id) {
                if (parseInt(id, 10) <= saveId) delete state.pendingHistory[id];
            });
            if (saveId === state.saveCounter) setStatus('saved', 'Saved ✓');
        },
        saveFailed: function (saveId, message) {
            rollbackHistorySave(saveId);
            setStatus('error', message || 'Save failed');
        },

        // Introspection + programmatic control (used by tests and AppleScript-free E2E).
        state: function () {
            return {
                enabled: state.enabled,
                textEditing: state.textEditing,
                hasSelection: !!state.selected,
                selectedTag: state.selected ? state.selected.tagName.toLowerCase() : null,
                selectionKind: state.selected ? selectionKind(state.selected) : null,
                slideIndex: wsReady() ? WS.currentIndex : -1,
                hiddenStepOpacity: state.hiddenStepOpacity,
                sourceSidebarVisible: state.sourceSidebarVisible,
                pdfExportAvailable: state.pdfExportAvailable,
                popover: state.popover ? state.popover.name : null,
                chromeTextFocus: state.chromeTextFocus,
                undoDepth: wsReady() ? historyFor(WS.currentIndex).undo.length : 0,
                redoDepth: wsReady() ? historyFor(WS.currentIndex).redo.length : 0
            };
        },
        select: function (elOrSelector) {
            var slide = activeSlide();
            if (!slide) return false;
            var el = typeof elOrSelector === 'string' ? slide.querySelector(elOrSelector) : elOrSelector;
            if (!el || !slide.contains(el)) return false;
            var pick = pickSelectable(el, slide) || el;
            select(pick);
            return true;
        },
        deselect: deselect,
        beginTextEdit: function (elOrSelector) {
            if (elOrSelector !== undefined && !this.select(elOrSelector)) return false;
            return beginTextEdit(state.selected);
        },
        endTextEdit: function () { endTextEdit(true); },
        pickSelectable: function (target) {
            var slide = activeSlide();
            return slide ? pickSelectable(target, slide) : null;
        },
        canTextEdit: canTextEdit,
        serializeCurrentSlide: function () {
            var section = activeSlide();
            return section ? serializeSection(section) : null;
        },
        serializeSection: serializeSection,
        addTextBox: addTextBoxAt,
        addTextBoxCentered: addTextBoxCentered,
        deleteSelected: deleteSelected,
        duplicateSelected: duplicateSelected,
        makeBulletedList: makeBulletedList,
        indentListItem: indentListItem,
        outdentListItem: outdentListItem,
        nudge: nudgeSelected,
        setFontSize: setFontSize,
        adjustFontSize: adjustFontSize,
        currentFontSizePx: currentFontSizePx,
        toggleBold: toggleBold,
        toggleItalic: toggleItalic,
        toggleUnderline: toggleUnderline,
        setColor: setColor,
        setAlign: setAlign,
        setBoxColor: setBoxColor,
        themeColors: function () { state.theme = null; return themeColors(); },
        appearsInfo: function () { return appearsInfo(state.selected); },
        setAppearsAt: setAppearsAt,
        editEquation: editEquation,
        applyEquationEdit: applyEquationEdit,
        closePopover: closePopover,
        setLinkTarget: setLinkTarget,
        editLinkLabel: editLinkLabel,
        selectionKind: function (el) { return selectionKind(el || state.selected); },
        undo: undo,
        redo: redo,
        saveNow: saveCurrentSlide,
        slidePointFromClient: function (clientX, clientY) {
            var slide = activeSlide();
            return slide ? slidePointFromClient(slide, clientX, clientY) : null;
        },

        // Test hooks
        set onMessage(fn) { testMessageSink = fn; },
        get onMessage() { return testMessageSink; }
    };
})();

/* Presto presenter console.
 *
 * Presto (Swift) drives this page with two calls:
 *   presenterUpdate(getStateJSON)   mirrors the audience position into the
 *                                   preview iframe (the only navigation path)
 *   PrestoConsole.setState(state)   everything else: phase, sections, talk
 *                                   length, timer, displays, blackout
 * and the page reports back on webkit.messageHandlers.presto with `ready`,
 * `presenterNavigated` and the `console*` messages (handled in
 * PrestoMenus.swift).
 *
 * Pace. Let T be the talk length, N the number of main slides, and the
 * current position main slide n (1-based) at reveal step s of a slide with
 * steps 0..m. The deck is cut into N*(m+1)-style shares: the position covers
 * the share [start, end) with
 *   start = (n - 1 + s / (m + 1)) / N,   end = (n - 1 + (s + 1) / (m + 1)) / N.
 * The expected position is elapsed / T. You are ahead by start*T - elapsed when
 * you reached this share before it was due, behind by elapsed - end*T when you
 * stayed past the time it should have ended, and on pace otherwise (or when
 * the gap is under 30 s). The appendix never counts: while in it, pace uses
 * the last main-slide position shown. No pace without a talk length, before
 * the timer has run, or before any main slide has been shown. A paused timer
 * freezes elapsed, so pace holds still; jumping backwards simply moves the
 * share back.
 */

(function () {
    'use strict';

    var TALK_MAX_MINUTES = 600;
    var ON_PACE_MS = 30000;
    var TIMER_POLL_MS = 250;
    var RESET_CONFIRM_MS = 3000;
    var START_PENDING_MS = 2500;
    var STATE_FALLBACK_MS = 2500;
    var HINT_DEFAULT_MS = 2000;
    var DOTS_MAX = 10;
    var SLIDE_W = 1280;
    var SLIDE_H = 720;
    var STAGE_GAP = 14;

    function byId(id) { return document.getElementById(id); }

    var currentIframe = byId('current-slide');
    var slideFrame = byId('slide-frame');
    var stage = byId('stage');
    var stagebar = byId('stagebar');
    var counterNum = byId('slide-counter-num');
    var counterTotal = byId('slide-counter-total');
    var slideTitleEl = byId('slide-title');
    var stepDots = byId('step-dots');
    var nextHint = byId('next-hint');
    var readyCard = byId('ready-card');
    var readyDeckTitle = byId('ready-deck-title');
    var startBtn = byId('start-btn');
    var startBtnLabel = byId('start-btn-label');
    var talkInput = byId('talk-minutes');
    var audienceRow = byId('audience-row');
    var audienceSelect = byId('audience-select');
    var audienceName = byId('audience-name');
    var swapBtn = byId('swap-btn');
    var oneScreenNote = byId('one-screen-note');
    var clockCard = byId('clock-card');
    var clockLabel = byId('clock-label');
    var timerEl = byId('timer');
    var timerBtn = byId('timer-btn');
    var timerReset = byId('timer-reset');
    var talkOf = byId('talk-of');
    var talkEdit = byId('talk-edit');
    var talkInputLive = byId('talk-minutes-live');
    var paceBar = byId('pace-bar');
    var paceFill = byId('pace-fill');
    var paceMarker = byId('pace-marker');
    var clockRow = byId('clock-row');
    var endsAt = byId('ends-at');
    var paceText = byId('pace-text');
    var sectionsCard = byId('sections-card');
    var sectionsList = byId('sections-list');
    var blackoutVeil = byId('blackout-veil');
    var blackoutOff = byId('blackout-off');
    var hintEl = byId('console-hint');

    /* ===== Small helpers (tolerant of the minimal DOM stubs in tests) ===== */

    function setText(el, text) {
        if (el && el.textContent !== text) el.textContent = text;
    }

    function setHidden(el, hidden) {
        if (el && el.hidden !== hidden) el.hidden = hidden;
    }

    function setClass(el, name, on) {
        if (!el || !el.classList) return;
        if (on) el.classList.add(name);
        else el.classList.remove(name);
    }

    function setStyle(el, prop, value) {
        if (el && el.style && el.style[prop] !== value) el.style[prop] = value;
    }

    function later(fn, ms) {
        return typeof setTimeout === 'function' ? setTimeout(fn, ms) : null;
    }

    function cancel(handle) {
        if (handle !== null && typeof clearTimeout === 'function') clearTimeout(handle);
    }

    function escapeHTML(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function warn(message, detail) {
        if (typeof console !== 'undefined' && console.warn) console.warn(message, detail);
    }

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
            warn('Presto: failed to post ' + type, e);
        }
    }

    function blurTarget(event) {
        var target = event && event.currentTarget;
        if (target && typeof target.blur === 'function') target.blur();
    }

    function parseStrictNonNegativeInt(value) {
        if (typeof value !== 'number') return null;
        if (!Number.isSafeInteger(value) || value < 0) return null;
        return value;
    }

    function parseStrictPositiveInt(value) {
        var parsed = parseStrictNonNegativeInt(value);
        return parsed !== null && parsed > 0 ? parsed : null;
    }

    function parseDisplayPositiveInt(value) {
        if (typeof value === 'string') {
            value = value.trim();
            if (!/^\d+$/.test(value)) return null;
            value = Number(value);
        }
        return parseStrictPositiveInt(value);
    }

    function finiteNumber(value) {
        return typeof value === 'number' && isFinite(value) ? value : null;
    }

    function cleanString(value) {
        return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
    }

    function parseTalkMinutes(value) {
        var minutes = parseStrictPositiveInt(value);
        return minutes !== null && minutes <= TALK_MAX_MINUTES ? minutes : null;
    }

    /* ===== Pure model functions (exported on PrestoConsole._internals) ===== */

    function defaultTimer() {
        return { running: false, elapsedMs: 0, startedAtMs: null };
    }

    function normalizeTimer(raw, nowMs) {
        if (!raw || typeof raw !== 'object') return defaultTimer();
        var elapsed = finiteNumber(raw.elapsedMs);
        var started = finiteNumber(raw.startedAtMs);
        var running = raw.running === true;
        if (running && started === null) started = nowMs;
        return {
            running: running,
            elapsedMs: elapsed !== null && elapsed > 0 ? elapsed : 0,
            startedAtMs: running ? started : null
        };
    }

    function timerElapsed(timer, nowMs) {
        if (!timer) return 0;
        var elapsed = timer.elapsedMs || 0;
        if (timer.running && timer.startedAtMs !== null) {
            elapsed += Math.max(0, nowMs - timer.startedAtMs);
        }
        return elapsed;
    }

    function displayId(value) {
        if (typeof value === 'string' && value !== '') return value;
        if (typeof value === 'number' && isFinite(value)) return value;
        return null;
    }

    function normalizeState(raw, nowMs) {
        raw = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
        var now = typeof nowMs === 'number' ? nowMs : Date.now();
        var mainSlideCount = parseStrictPositiveInt(raw.mainSlideCount);
        var totalSlides = parseStrictPositiveInt(raw.totalSlides);
        if (totalSlides !== null && mainSlideCount !== null && totalSlides < mainSlideCount) totalSlides = null;
        var slideIndex = parseStrictNonNegativeInt(raw.slideIndex);
        if (slideIndex === null) slideIndex = 0;
        var maxStep = parseStrictNonNegativeInt(raw.maxStep);
        if (maxStep === null) maxStep = 0;
        var step = parseStrictNonNegativeInt(raw.step);
        step = step === null ? 0 : Math.min(step, maxStep > 0 ? maxStep : step);
        var isAppendix = raw.isAppendix === true;
        var slideNumber = isAppendix ? null : parseDisplayPositiveInt(raw.slideNumber);
        if (slideNumber !== null && mainSlideCount !== null && slideNumber > mainSlideCount) slideNumber = null;

        var sections = [];
        if (Array.isArray(raw.sections)) {
            raw.sections.forEach(function (sec) {
                if (!sec || typeof sec !== 'object') return;
                var first = parseStrictNonNegativeInt(sec.first);
                var last = parseStrictNonNegativeInt(sec.last);
                if (first === null || last === null || last < first) return;
                sections.push({ title: cleanString(sec.title) || 'Untitled section', first: first, last: last });
            });
            sections.sort(function (a, b) { return a.first - b.first; });
        }

        var displays = [];
        if (Array.isArray(raw.displays)) {
            raw.displays.forEach(function (d) {
                if (!d || typeof d !== 'object') return;
                var id = displayId(d.id);
                if (id === null) return;
                displays.push({ id: id, name: cleanString(d.name) || 'Display' });
            });
        }

        return {
            phase: raw.phase === 'live' ? 'live' : 'ready',
            deckTitle: cleanString(raw.deckTitle),
            slideIndex: slideIndex,
            step: step,
            maxStep: maxStep,
            mainSlideCount: mainSlideCount,
            totalSlides: totalSlides,
            slideNumber: slideNumber,
            isAppendix: isAppendix,
            appendixLabel: cleanString(raw.appendixLabel) || null,
            slideTitle: cleanString(raw.slideTitle),
            sections: sections,
            talkMinutes: parseTalkMinutes(raw.talkMinutes),
            timer: normalizeTimer(raw.timer, now),
            displays: displays,
            audienceDisplayId: displayId(raw.audienceDisplayId),
            blackout: raw.blackout === true
        };
    }

    // Share of the main deck covered by a position, as {start, end} in [0, 1].
    function slideProgress(slideNumber, step, maxStep, mainSlideCount) {
        var total = parseStrictPositiveInt(mainSlideCount);
        var number = parseStrictPositiveInt(slideNumber);
        if (total === null || number === null) return null;
        number = Math.min(number, total);
        var states = (parseStrictNonNegativeInt(maxStep) || 0) + 1;
        var s = Math.min(parseStrictNonNegativeInt(step) || 0, states - 1);
        return {
            start: (number - 1 + s / states) / total,
            end: (number - 1 + (s + 1) / states) / total
        };
    }

    function computePace(input) {
        var out = {
            hasTalk: false,
            talkMs: 0,
            elapsedMs: 0,
            timeFraction: 0,
            position: null,
            overtimeMs: 0,
            endsAtMs: null,
            tone: 'none',
            minutes: 0,
            label: ''
        };
        var elapsed = finiteNumber(input.elapsedMs);
        out.elapsedMs = elapsed !== null && elapsed > 0 ? elapsed : 0;
        var talkMinutes = parseTalkMinutes(input.talkMinutes);
        if (talkMinutes === null) return out;
        var now = finiteNumber(input.nowMs);
        out.hasTalk = true;
        out.talkMs = talkMinutes * 60000;
        out.timeFraction = Math.min(1, out.elapsedMs / out.talkMs);
        out.overtimeMs = Math.max(0, out.elapsedMs - out.talkMs);
        out.endsAtMs = now === null ? null : now + (out.talkMs - out.elapsedMs);
        var progress = input.progress;
        if (!progress || typeof progress.start !== 'number' || typeof progress.end !== 'number') return out;
        out.position = Math.max(0, Math.min(1, progress.start));
        if (!input.timerStarted) return out;

        var dueStart = progress.start * out.talkMs;
        var dueEnd = progress.end * out.talkMs;
        var gap = 0;
        if (out.elapsedMs < dueStart) gap = dueStart - out.elapsedMs;
        else if (out.elapsedMs > dueEnd) gap = dueEnd - out.elapsedMs;
        if (Math.abs(gap) < ON_PACE_MS) {
            out.tone = 'on';
            out.label = 'On pace';
            return out;
        }
        var minutes = Math.max(1, Math.round(Math.abs(gap) / 60000));
        out.tone = gap > 0 ? 'ahead' : 'behind';
        out.minutes = gap > 0 ? minutes : -minutes;
        out.label = minutes + ' min ' + (gap > 0 ? 'ahead of pace' : 'behind pace');
        return out;
    }

    function pad2(n) { return (n < 10 ? '0' : '') + n; }

    function formatDuration(ms) {
        var totalSeconds = Math.floor(Math.max(0, finiteNumber(ms) || 0) / 1000);
        var hours = Math.floor(totalSeconds / 3600);
        var minutes = Math.floor((totalSeconds % 3600) / 60);
        var seconds = totalSeconds % 60;
        return hours > 0
            ? hours + ':' + pad2(minutes) + ':' + pad2(seconds)
            : pad2(minutes) + ':' + pad2(seconds);
    }

    function formatClockTime(epochMs) {
        var d = new Date(epochMs);
        if (isNaN(d.getTime())) return '';
        return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    }

    function appendixCount(st) {
        if (st.totalSlides === null || st.mainSlideCount === null) return 0;
        return Math.max(0, st.totalSlides - st.mainSlideCount);
    }

    function appendixOrdinal(st) {
        if (st.mainSlideCount === null) return null;
        var ordinal = st.slideIndex - st.mainSlideCount + 1;
        return ordinal >= 1 ? ordinal : null;
    }

    // A main slide's number: the deck's own label, else its position.
    function mainNumber(st) {
        return st.slideNumber !== null ? st.slideNumber : st.slideIndex + 1;
    }

    function rangeText(first, last) {
        return first === last ? String(first) : first + '–' + last;
    }

    // anchorIndex: the main slide that decides done/current (null = none yet).
    function buildSectionRows(st, anchorIndex) {
        var rows = [];
        st.sections.forEach(function (sec) {
            var count = sec.last - sec.first + 1;
            var row = {
                title: sec.title,
                status: 'next',
                range: rangeText(sec.first + 1, sec.last + 1),
                index: sec.first,
                appendix: false
            };
            if (anchorIndex !== null && anchorIndex !== undefined) {
                if (sec.last < anchorIndex) {
                    row.status = 'done';
                } else if (!st.isAppendix && sec.first <= anchorIndex && anchorIndex <= sec.last) {
                    row.status = 'now';
                    row.index = null;
                    if (count > 1) row.range = (anchorIndex - sec.first + 1) + ' of ' + count;
                }
            }
            rows.push(row);
        });
        var extra = appendixCount(st);
        if (extra > 0 || st.isAppendix) {
            var appendixRow = {
                title: 'Appendix',
                status: 'next',
                range: extra > 1 ? 'A1–A' + extra : (extra === 1 ? 'A1' : ''),
                index: extra > 0 ? st.mainSlideCount : null,
                appendix: true
            };
            if (st.isAppendix) {
                var ordinal = appendixOrdinal(st);
                appendixRow.status = 'now';
                appendixRow.index = null;
                appendixRow.range = ordinal !== null && extra > 1 && ordinal <= extra
                    ? ordinal + ' of ' + extra
                    : (st.appendixLabel || '');
            }
            rows.push(appendixRow);
        }
        return rows;
    }

    function counterParts(st) {
        if (st.isAppendix) {
            var ordinal = appendixOrdinal(st);
            return { num: 'Appendix', total: st.appendixLabel || (ordinal !== null ? 'A' + ordinal : '') };
        }
        return { num: String(mainNumber(st)), total: st.mainSlideCount !== null ? '/ ' + st.mainSlideCount : '' };
    }

    function nextHintText(st) {
        var keys = ' · → or Space';
        if (st.step < st.maxStep) return 'Next: step ' + (st.step + 1) + ' of ' + st.maxStep + keys;
        if (st.isAppendix) {
            if (st.totalSlides !== null && st.slideIndex + 1 >= st.totalSlides) return 'Last slide';
            var ordinal = appendixOrdinal(st);
            return ordinal !== null ? 'Next: A' + (ordinal + 1) + keys : '';
        }
        var number = mainNumber(st);
        if (st.mainSlideCount !== null && number >= st.mainSlideCount) {
            return appendixCount(st) > 0 ? 'Last main slide' : 'Last slide';
        }
        return 'Next: slide ' + (number + 1) + keys;
    }

    function parseTalkInput(text) {
        var value = String(text === undefined || text === null ? '' : text).trim();
        if (value === '') return { ok: true, minutes: null };
        if (!/^\d{1,3}$/.test(value)) return { ok: false, minutes: null };
        var minutes = Number(value);
        if (minutes < 1 || minutes > TALK_MAX_MINUTES) return { ok: false, minutes: null };
        return { ok: true, minutes: minutes };
    }

    /* ===== Page state ===== */

    var model = normalizeState({});
    var lastMain = null; // {index, slideNumber, step, maxStep} of the last main slide shown
    var receivedState = false;
    var startPending = false;
    var startPendingTimer = null;
    var resetArmedUntil = 0;
    var resetArmTimer = null;
    var hintTimer = null;
    var renderedSectionsKey = null;
    var renderedDotsKey = null;
    var renderedDisplaysKey = null;

    function noteMainPosition() {
        if (model.isAppendix) {
            if (lastMain && model.mainSlideCount !== null && lastMain.index >= model.mainSlideCount) lastMain = null;
            return;
        }
        lastMain = {
            index: model.slideIndex,
            slideNumber: mainNumber(model),
            step: model.step,
            maxStep: model.maxStep
        };
    }

    function anchorIndex() {
        if (!model.isAppendix) return model.slideIndex;
        return lastMain ? lastMain.index : null;
    }

    function currentProgress() {
        var pos = model.isAppendix ? lastMain : {
            slideNumber: mainNumber(model),
            step: model.step,
            maxStep: model.maxStep
        };
        if (!pos) return null;
        return slideProgress(pos.slideNumber, pos.step, pos.maxStep, model.mainSlideCount);
    }

    function titleFromPreview(index) {
        try {
            var w = currentIframe.contentWindow;
            var slide = w && w.WS && w.WS.slides ? w.WS.slides[index] : null;
            var heading = slide && slide.querySelector ? slide.querySelector('h1, h2') : null;
            if (!heading) return '';
            var copy = heading.cloneNode(true);
            var hidden = copy.querySelectorAll('.katex-mathml, .ws-section-toc, aside');
            for (var i = 0; i < hidden.length; i++) hidden[i].parentNode.removeChild(hidden[i]);
            var breaks = copy.querySelectorAll('br');
            for (var j = 0; j < breaks.length; j++) {
                breaks[j].parentNode.replaceChild(copy.ownerDocument.createTextNode(' '), breaks[j]);
            }
            return cleanString(copy.textContent);
        } catch (e) {
            return '';
        }
    }

    /* ===== Rendering ===== */

    function revealSide() {
        setClass(document.body, 'awaiting-state', false);
    }

    function renderPosition() {
        var parts = counterParts(model);
        setText(counterNum, parts.num);
        setText(counterTotal, parts.total);
        setText(slideTitleEl, model.slideTitle);
        setText(nextHint, nextHintText(model));
        renderDots(model.step, model.maxStep);
    }

    function renderDots(step, maxStep) {
        if (!stepDots) return;
        var key = step + '/' + maxStep;
        if (key === renderedDotsKey) return;
        renderedDotsKey = key;
        if (!(maxStep > 0)) {
            stepDots.innerHTML = '';
            setHidden(stepDots, true);
            return;
        }
        var html = '';
        if (maxStep > DOTS_MAX) {
            html = '<span class="steps-text">Step ' + step + ' of ' + maxStep + '</span>';
        } else {
            for (var i = 0; i <= maxStep; i++) {
                if (i > 0) html += '<i></i>';
                html += '<b class="' + (i < step ? 'done' : (i === step ? 'on' : '')) + '">' + i + '</b>';
            }
        }
        stepDots.innerHTML = html;
        setHidden(stepDots, false);
    }

    function renderSections() {
        if (!sectionsList) return;
        var rows = buildSectionRows(model, anchorIndex());
        var key = JSON.stringify(rows);
        if (key === renderedSectionsKey) return;
        renderedSectionsKey = key;
        var html = '';
        rows.forEach(function (row) {
            var cls = [row.status, row.appendix ? 'appendix' : ''].join(' ').trim();
            var clickable = row.index !== null && row.index !== undefined;
            html += '<li class="' + cls + '"' +
                (clickable ? ' data-index="' + row.index + '" title="Go to ' + escapeHTML(row.title) + '"' : '') +
                (row.status === 'now' ? ' aria-current="step"' : '') + '>' +
                '<span class="mk"></span><span class="t">' + escapeHTML(row.title) + '</span>' +
                '<em>' + escapeHTML(row.range) + '</em></li>';
        });
        sectionsList.innerHTML = html;
        setHidden(sectionsCard, rows.length === 0);
    }

    function talkFieldValue() {
        return model.talkMinutes === null ? '' : String(model.talkMinutes);
    }

    function renderReady() {
        setText(readyDeckTitle, model.deckTitle);
        if (talkInput && document.activeElement !== talkInput) {
            if (talkInput.value !== talkFieldValue()) talkInput.value = talkFieldValue();
            setClass(talkInput, 'invalid', false);
        }
        setText(startBtnLabel, startPending ? 'Starting…' : 'Start');
        if (startBtn) startBtn.disabled = startPending;

        var displays = model.displays;
        setHidden(audienceRow, displays.length === 0);
        setHidden(oneScreenNote, displays.length !== 0);
        setHidden(swapBtn, displays.length === 0);
        var several = displays.length > 1;
        setHidden(audienceSelect, !several);
        setHidden(audienceName, several);
        if (several) {
            var key = JSON.stringify(displays);
            if (key !== renderedDisplaysKey && audienceSelect) {
                renderedDisplaysKey = key;
                audienceSelect.innerHTML = displays.map(function (d, i) {
                    return '<option value="' + i + '">' + escapeHTML(d.name) + '</option>';
                }).join('');
            }
            if (audienceSelect) {
                var selected = 0;
                displays.forEach(function (d, i) {
                    if (String(d.id) === String(model.audienceDisplayId)) selected = i;
                });
                if (String(audienceSelect.value) !== String(selected)) audienceSelect.value = String(selected);
            }
            setText(audienceName, '');
        } else {
            setText(audienceName, displays.length ? displays[0].name : '');
        }
    }

    function renderBlackout() {
        setHidden(blackoutVeil, !model.blackout);
    }

    var lastFillWidth = null;

    function renderClock(nowMs) {
        var now = typeof nowMs === 'number' ? nowMs : Date.now();
        var timer = model.timer;
        var elapsed = timerElapsed(timer, now);
        setText(timerEl, formatDuration(elapsed));
        setClass(timerEl, 'paused', !timer.running);
        setText(clockLabel, !timer.running && elapsed > 0 ? 'Paused' : 'Elapsed');

        var timerLabel = timer.running ? 'Pause' : (elapsed > 0 ? 'Resume' : 'Start');
        setText(timerBtn, timerLabel);
        if (timerBtn) timerBtn.title = timerLabel + ' timer';
        if (timerReset) {
            var armed = now < resetArmedUntil;
            setText(timerReset, armed ? 'Confirm reset' : 'Reset');
            setClass(timerReset, 'confirming', armed);
            timerReset.disabled = elapsed <= 0 && !armed;
        }

        var pace = computePace({
            elapsedMs: elapsed,
            talkMinutes: model.talkMinutes,
            progress: currentProgress(),
            timerStarted: timer.running || elapsed > 0,
            nowMs: now
        });
        var editing = talkEdit && !talkEdit.hidden;
        setHidden(talkOf, !!editing);
        setText(talkOf, pace.hasTalk ? 'of ' + formatDuration(pace.talkMs) : 'Set talk length');
        setClass(talkOf, 'unset', !pace.hasTalk);

        setHidden(paceBar, !pace.hasTalk);
        setHidden(clockRow, !pace.hasTalk);
        if (!pace.hasTalk) return;
        var fill = (pace.timeFraction * 100).toFixed(2) + '%';
        if (fill !== lastFillWidth) {
            lastFillWidth = fill;
            setStyle(paceFill, 'width', fill);
        }
        setClass(paceBar, 'over', pace.overtimeMs > 0);
        setHidden(paceMarker, pace.position === null);
        if (pace.position !== null) setStyle(paceMarker, 'left', (pace.position * 100).toFixed(2) + '%');

        if (pace.overtimeMs > 0) {
            setText(endsAt, pace.overtimeMs < 60000 ? 'Time is up' : 'Over by ' + Math.floor(pace.overtimeMs / 60000) + ' min');
        } else {
            setText(endsAt, pace.endsAtMs === null ? '' : 'Ends at ' + formatClockTime(pace.endsAtMs));
        }
        setClass(endsAt, 'over', pace.overtimeMs > 0);
        setText(paceText, pace.label);
        setClass(paceText, 'ahead', pace.tone === 'ahead');
        setClass(paceText, 'behind', pace.tone === 'behind');
        setClass(paceText, 'on', pace.tone === 'on');
    }

    function render() {
        var live = model.phase === 'live';
        setClass(document.body, 'phase-live', live);
        setClass(document.body, 'phase-ready', !live);
        setHidden(readyCard, live);
        setHidden(clockCard, !live);
        renderPosition();
        renderReady();
        renderSections();
        renderBlackout();
        renderClock(Date.now());
        layoutStage();
        scheduleTicking();
    }

    /* ===== Clock ticking =====
       MessageChannel keeps timer updates reliable in WKWebView. Pace the
       channel with a short timeout so it does not become a CPU-burning busy
       loop; render only what changed. */

    var timerChannel = new MessageChannel();
    var ticking = false;

    function shouldTick() {
        return model.phase === 'live' || model.timer.running;
    }

    function scheduleTicking() {
        if (ticking || !shouldTick()) return;
        ticking = true;
        timerChannel.port2.postMessage('');
    }

    timerChannel.port1.onmessage = function () {
        if (!shouldTick()) {
            ticking = false;
            return;
        }
        renderClock(Date.now());
        later(function () {
            if (shouldTick()) {
                timerChannel.port2.postMessage('');
            } else {
                ticking = false;
            }
        }, TIMER_POLL_MS);
    };

    /* ===== Timer controls (the timer itself is owned by Presto) ===== */

    function timerAction(action) {
        var now = Date.now();
        var timer = model.timer;
        if (action === 'start') {
            if (!timer.running) {
                timer.running = true;
                timer.startedAtMs = now;
            }
        } else if (action === 'pause') {
            if (timer.running) {
                timer.elapsedMs = timerElapsed(timer, now);
                timer.running = false;
                timer.startedAtMs = null;
            }
        } else if (action === 'reset') {
            timer.elapsedMs = 0;
            timer.startedAtMs = timer.running ? now : null;
        } else {
            return;
        }
        post('consoleTimer', { action: action });
        renderClock(now);
        scheduleTicking();
    }

    function disarmReset() {
        resetArmedUntil = 0;
        cancel(resetArmTimer);
        resetArmTimer = null;
        renderClock(Date.now());
    }

    if (timerBtn) {
        timerBtn.addEventListener('click', function (event) {
            timerAction(model.timer.running ? 'pause' : 'start');
            blurTarget(event);
        });
    }

    // Reset needs a second click within 3 s, so a stray click mid-talk is harmless.
    if (timerReset) {
        timerReset.addEventListener('click', function (event) {
            var now = Date.now();
            if (now < resetArmedUntil) {
                disarmReset();
                timerAction('reset');
            } else {
                resetArmedUntil = now + RESET_CONFIRM_MS;
                cancel(resetArmTimer);
                resetArmTimer = later(disarmReset, RESET_CONFIRM_MS);
                renderClock(now);
            }
            blurTarget(event);
        });
    }

    /* ===== Talk length ===== */

    function commitTalk(input) {
        var parsed = parseTalkInput(input.value);
        if (!parsed.ok) {
            setClass(input, 'invalid', true);
            return false;
        }
        setClass(input, 'invalid', false);
        if (parsed.minutes !== model.talkMinutes) {
            model.talkMinutes = parsed.minutes;
            post('consoleSetTalkMinutes', { minutes: parsed.minutes });
        }
        input.value = talkFieldValue();
        renderClock(Date.now());
        return true;
    }

    function closeLiveTalkEdit() {
        setHidden(talkEdit, true);
        renderClock(Date.now());
    }

    function wireTalkInput(input, onDone) {
        if (!input) return;
        input.addEventListener('focus', function () {
            post('consoleTextEditing', { active: true });
        });
        input.addEventListener('input', function () {
            var digits = String(input.value).replace(/[^\d]/g, '').slice(0, 3);
            if (digits !== input.value) input.value = digits;
            setClass(input, 'invalid', false);
        });
        input.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                event.preventDefault();
                if (commitTalk(input)) input.blur();
            } else if (event.key === 'Escape') {
                event.preventDefault();
                input.value = talkFieldValue();
                setClass(input, 'invalid', false);
                input.blur();
            }
        });
        input.addEventListener('blur', function () {
            if (!commitTalk(input)) {
                input.value = talkFieldValue();
                setClass(input, 'invalid', false);
            }
            post('consoleTextEditing', { active: false });
            if (onDone) onDone();
        });
    }

    wireTalkInput(talkInput, null);
    wireTalkInput(talkInputLive, closeLiveTalkEdit);

    if (talkOf) {
        talkOf.addEventListener('click', function () {
            if (!talkInputLive) return;
            setHidden(talkEdit, false);
            setHidden(talkOf, true);
            talkInputLive.value = talkFieldValue();
            talkInputLive.focus();
            if (typeof talkInputLive.select === 'function') talkInputLive.select();
        });
    }

    /* ===== Ready card ===== */

    function clearStartPending() {
        if (!startPending) return;
        startPending = false;
        cancel(startPendingTimer);
        startPendingTimer = null;
        renderReady();
    }

    if (startBtn) {
        startBtn.addEventListener('click', function (event) {
            if (startPending) return;
            if (talkInput && document.activeElement === talkInput) commitTalk(talkInput);
            startPending = true;
            startPendingTimer = later(clearStartPending, START_PENDING_MS);
            renderReady();
            post('consoleStart', {});
            blurTarget(event);
        });
    }

    if (audienceSelect) {
        audienceSelect.addEventListener('change', function () {
            var display = model.displays[Number(audienceSelect.value)];
            if (!display) return;
            model.audienceDisplayId = display.id;
            post('consoleSetAudienceDisplay', { id: display.id });
        });
    }

    if (swapBtn) {
        swapBtn.addEventListener('click', function (event) {
            post('consoleSwapScreens', {});
            blurTarget(event);
        });
    }

    if (blackoutOff) {
        blackoutOff.addEventListener('click', function (event) {
            model.blackout = false;
            renderBlackout();
            post('consoleBlackout', { on: false });
            blurTarget(event);
        });
    }

    if (sectionsList) {
        sectionsList.addEventListener('click', function (event) {
            var target = event.target;
            var row = target && target.closest ? target.closest('li[data-index]') : null;
            if (!row) return;
            var index = Number(row.getAttribute('data-index'));
            if (!Number.isSafeInteger(index) || index < 0) return;
            post('consoleNavigate', { index: index, step: 0 });
        });
    }

    /* ===== Hint toast ===== */

    function hint(text, ms) {
        if (!hintEl) return;
        setText(hintEl, cleanString(String(text === undefined || text === null ? '' : text)));
        setClass(hintEl, 'visible', true);
        cancel(hintTimer);
        var duration = finiteNumber(ms);
        hintTimer = later(function () {
            setClass(hintEl, 'visible', false);
        }, duration !== null && duration > 0 ? duration : HINT_DEFAULT_MS);
    }

    /* ===== Stage layout ===== */

    function layoutStage() {
        if (!stage || !slideFrame || !currentIframe) return;
        var width = stage.clientWidth;
        var height = stage.clientHeight;
        if (!(width > 0 && height > 0)) return;
        var barHeight = (stagebar && stagebar.offsetHeight) || 28;
        var scale = Math.min(width / SLIDE_W, (height - barHeight - STAGE_GAP) / SLIDE_H);
        if (!(scale > 0)) return;
        var frameW = Math.max(1, Math.floor(SLIDE_W * scale));
        var frameH = Math.max(1, Math.floor(SLIDE_H * scale));
        setStyle(slideFrame, 'width', frameW + 'px');
        setStyle(slideFrame, 'height', frameH + 'px');
        setStyle(stagebar, 'width', frameW + 'px');
        setStyle(currentIframe, 'transform', 'scale(' + (frameW / SLIDE_W) + ')');
    }

    window.addEventListener('resize', layoutStage);
    if (typeof ResizeObserver === 'function' && stage) {
        try {
            new ResizeObserver(layoutStage).observe(stage);
        } catch (e) {
            warn('Presto: stage resize observer failed', e);
        }
    }

    /* ===== Preview chrome =====
       The engine's preview mode hides the deck's footer and progress line.
       The console frames the slide as the audience sees it, so it shows them
       again inside its own iframe only (other preview users keep the bare
       preview). The iframe is exactly 1280x720, so the engine's own layout
       puts the footer on the slide's bottom edge and the progress line on its
       top edge. The slide grid (overview) keeps them hidden. Re-applied on
       every load of the iframe (initPresenter, a refresh, a reload). */

    var PREVIEW_CHROME_STYLE_ID = 'presto-console-preview-chrome';
    var PREVIEW_CHROME_CSS = '@media screen {' +
        ' html body.ws-mode-preview:not(.ws-overview-active) #ws-footer { display: flex !important; }' +
        ' html body.ws-mode-preview:not(.ws-overview-active) #ws-progress { display: block !important; }' +
        ' }';

    function showPreviewChrome() {
        try {
            var doc = currentIframe.contentDocument;
            if (!doc || !doc.head || doc.getElementById(PREVIEW_CHROME_STYLE_ID)) return;
            var style = doc.createElement('style');
            style.id = PREVIEW_CHROME_STYLE_ID;
            style.textContent = PREVIEW_CHROME_CSS;
            doc.head.appendChild(style);
        } catch (e) {
            warn('Presto: could not show the footer in the console preview', e);
        }
    }

    if (currentIframe) currentIframe.addEventListener('load', showPreviewChrome);

    /* ===== Initialization ===== */

    window.buildPresenterPreviewURL = function (url) {
        var hash = '';
        var hashIndex = url.indexOf('#');
        if (hashIndex >= 0) {
            hash = url.slice(hashIndex);
            url = url.slice(0, hashIndex);
        }
        var query = '';
        var queryIndex = url.indexOf('?');
        if (queryIndex >= 0) {
            query = url.slice(queryIndex + 1);
            url = url.slice(0, queryIndex);
        }
        var params = new URLSearchParams(query);
        params.delete('mode');
        params.set('mode', 'preview');
        return url + '?' + params.toString() + hash;
    };

    /* ===== Presenter-initiated navigation ===== */

    // True while presenterUpdate mirrors the audience, so that echo is not
    // reported back to Presto as a new navigation.
    var applyingNativeUpdate = false;

    function copyReturnStack(stack) {
        if (!Array.isArray(stack)) return [];
        var copy = [];
        for (var i = 0; i < stack.length; i++) {
            var entry = stack[i] || {};
            var index = parseStrictNonNegativeInt(entry.index);
            var step = parseStrictNonNegativeInt(entry.step);
            if (index === null || step === null) return null;
            copy.push({ index: index, step: step });
        }
        return copy;
    }

    // An appendix or back button clicked in the presenter view, or a slide
    // picked in its overview, must move the audience too.
    function reportPreviewNavigation(previewWS) {
        if (!previewWS || typeof previewWS.on !== 'function' || previewWS.__prestoPresenterNavigationHooked) return;
        previewWS.__prestoPresenterNavigationHooked = true;
        previewWS.on('navigate', function (state) {
            if (applyingNativeUpdate || !state) return;
            var returnStack = copyReturnStack(previewWS.returnStack);
            if (returnStack === null) return;
            post('presenterNavigated', {
                index: state.index,
                step: state.step,
                returnStack: returnStack
            });
        });
    }

    window.initPresenter = function (deckFileURL) {
        var deckURL = deckFileURL;
        // If Presto never sends a state (an older shell), show the cards anyway.
        later(revealSide, STATE_FALLBACK_MS);

        function signalReadyWhenPreviewEngineExists(attemptsRemaining) {
            // Ensure preview mode is set (WKWebView may strip query params)
            try {
                var w = currentIframe.contentWindow;
                if (!w || !w.WS || !w.WS.slides || !w.WS.slides.length) {
                    if (attemptsRemaining > 0) {
                        setTimeout(function () {
                            signalReadyWhenPreviewEngineExists(attemptsRemaining - 1);
                        }, 50);
                    } else {
                        console.warn('Presto: preview iframe did not become ready');
                    }
                    return;
                }
                if (w.WS.mode !== 'preview') {
                    w.WS.mode = 'preview';
                    w.document.body.classList.add('ws-mode-preview');
                }
                reportPreviewNavigation(w.WS);
            } catch (e) {
                console.warn('Presto: failed to initialize preview iframe', e);
                return;
            }

            layoutStage();
            if (!model.slideTitle) {
                model.slideTitle = titleFromPreview(model.slideIndex);
                renderPosition();
            }
            post('ready', {});
        }

        function onIframeLoad() {
            signalReadyWhenPreviewEngineExists(50);
        }

        currentIframe.addEventListener('load', onIframeLoad, { once: true });
        currentIframe.src = window.buildPresenterPreviewURL(deckURL);
    };

    /* ===== Navigation Update ===== */

    window.presenterUpdate = function (state) {
        if (!state || typeof state !== 'object') {
            console.warn('Presto: ignored malformed presenter update', state);
            return;
        }
        var index = parseStrictNonNegativeInt(state.index);
        var step = state.step === undefined || state.step === null
            ? 0
            : parseStrictNonNegativeInt(state.step);
        var hasTotalSlides = state.totalSlides !== undefined && state.totalSlides !== null;
        var totalSlides = !hasTotalSlides
            ? null
            : parseStrictPositiveInt(state.totalSlides);
        if (index === null || step === null || (hasTotalSlides && totalSlides === null)) {
            console.warn('Presto: ignored malformed presenter update', state);
            return;
        }
        if (totalSlides !== null && index >= totalSlides) {
            console.warn('Presto: ignored malformed presenter update', state);
            return;
        }
        if (state.isAppendix !== undefined && state.isAppendix !== null && typeof state.isAppendix !== 'boolean') {
            console.warn('Presto: ignored malformed presenter update', state);
            return;
        }

        // Update current slide iframe, including the audience's appendix
        // return stack so the presenter view's back buttons pop the same entry.
        var returnStack = copyReturnStack(state.returnStack);
        applyingNativeUpdate = true;
        try {
            var currentWS = currentIframe.contentWindow.WS;
            if (currentWS && currentWS.goToSlide) {
                if (Array.isArray(state.returnStack) && returnStack !== null) {
                    currentWS.returnStack = returnStack;
                }
                currentWS.goToSlide(index, step);
            }
        } catch (e) {
            console.warn('Presto: failed to update current iframe', e);
        } finally {
            applyingNativeUpdate = false;
        }

        // Mirror the position into the console chrome.
        var isAppendix = state.isAppendix === true;
        var mainSlides = parseDisplayPositiveInt(state.mainSlides);
        var slideNumber = isAppendix ? null : parseDisplayPositiveInt(state.slideNumber);
        if (slideNumber !== null && mainSlides !== null && slideNumber > mainSlides) slideNumber = null;
        if (slideNumber === null && !isAppendix) slideNumber = index + 1;
        if (mainSlides !== null && slideNumber !== null && slideNumber > mainSlides) mainSlides = null;
        var maxSteps = parseStrictNonNegativeInt(state.maxSteps);

        var indexChanged = index !== model.slideIndex;
        model.slideIndex = index;
        model.step = step;
        model.maxStep = maxSteps !== null ? maxSteps : (indexChanged ? 0 : model.maxStep);
        if (model.step > model.maxStep) model.maxStep = model.step;
        model.isAppendix = isAppendix;
        model.slideNumber = slideNumber;
        model.mainSlideCount = mainSlides;
        if (totalSlides !== null) model.totalSlides = totalSlides;
        if (indexChanged) {
            // Presto's next setState brings the new slide's title and label.
            model.appendixLabel = null;
            model.slideTitle = titleFromPreview(index);
        }
        noteMainPosition();
        renderPosition();
        renderSections();
        renderClock(Date.now());
    };

    /* ===== Overview Toggle ===== */

    window.presenterToggleOverview = function () {
        try {
            var currentWS = currentIframe.contentWindow.WS;
            if (currentWS && currentWS.toggleOverview) {
                currentWS.toggleOverview();
            }
        } catch (e) {
            console.warn('Presto: failed to toggle overview', e);
        }
    };

    /* ===== Console state from Presto ===== */

    function setState(raw) {
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
            warn('Presto: ignored malformed console state', raw);
            return;
        }
        var next = normalizeState(raw, Date.now());
        if (!next.slideTitle) {
            next.slideTitle = next.slideIndex === model.slideIndex && model.slideTitle
                ? model.slideTitle
                : titleFromPreview(next.slideIndex);
        }
        var wasReady = model.phase !== 'live';
        model = next;
        receivedState = true;
        revealSide();
        if (model.phase === 'live') clearStartPending();
        noteMainPosition();
        render();
        if (wasReady && model.phase !== 'live' && startBtn && typeof startBtn.focus === 'function' &&
            (!document.activeElement || document.activeElement === document.body)) {
            startBtn.focus();
        }
    }

    window.PrestoConsole = {
        setState: setState,
        hint: hint,
        getState: function () {
            return JSON.parse(JSON.stringify(model));
        },
        hasReceivedState: function () { return receivedState; },
        _internals: {
            normalizeState: normalizeState,
            slideProgress: slideProgress,
            computePace: computePace,
            formatDuration: formatDuration,
            formatClockTime: formatClockTime,
            buildSectionRows: buildSectionRows,
            counterParts: counterParts,
            nextHintText: nextHintText,
            parseTalkInput: parseTalkInput,
            timerElapsed: timerElapsed
        }
    };

})();

/* Presto — Editor View Logic */

(function () {
    'use strict';

    var editor = document.getElementById('editor');
    var highlightingContent = document.getElementById('highlighting-content');
    var highlightingPre = document.getElementById('highlighting');
    var slideLabel = document.getElementById('slide-label');
    var saveStatus = document.getElementById('save-status');
    var saveError = document.getElementById('save-error');
    var saveErrorText = document.getElementById('save-error-text');
    var saveRetry = document.getElementById('save-retry');
    var sectionOpen = document.getElementById('section-open');
    var sectionClose = document.getElementById('section-close');
    var editorContainer = document.getElementById('editor-container');
    var inspectorClose = document.getElementById('inspector-close');

    var currentIndex = -1;
    var bodyStartLine = 1;
    var debounceTimer = null;
    var DEBOUNCE_MS = 600;
    var STATUS_CLEAR_MS = 2000;
    var SELECTION_ANCHOR_RADIUS = 48;
    var SELECTION_ANCHOR_MAX_GAP = 160;
    var TAB_INSERT = '    ';
    var dirty = false;
    var pendingSave = null;
    var nextSaveId = 1;
    var lastCleanBodyHTML = '';

    /* ===== HTML Escape ===== */

    function esc(s) {
        return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    /* ===== Syntax Highlighting ===== */

    // Single-pass tokenizer: regex alternatives match different HTML token types.
    // Order matters — first matching alternative wins. Maths ($…$, $$…$$,
    // \(…\), \[…\]) is coloured only where it holds no tag.
    var TOKEN_RE = /(<!--[\s\S]*?-->)|(<\/?\w[\w-]*)|([\w-:]+)(=)|("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\/?>)|(&\w+;|&#\d+;|&#x[\da-fA-F]+;)|(\$\$[^$<]+?\$\$|\$(?!\$)[^$<\n]+?\$|\\\([^<]*?\\\)|\\\[[^<]*?\\\])/g;

    function tokenizeHTML(code) {
        var tokens = [];
        var lastIndex = 0;
        var m;

        function push(cls, text) {
            if (text) tokens.push({ cls: cls, text: text });
        }

        TOKEN_RE.lastIndex = 0;

        while ((m = TOKEN_RE.exec(code)) !== null) {
            // Text before this match
            if (m.index > lastIndex) {
                push(null, code.slice(lastIndex, m.index));
            }

            if (m[1] !== undefined) {
                // Comment
                push('hl-comment', m[1]);
            } else if (m[2] !== undefined) {
                // Tag opening: <tagname or </tagname
                var raw = m[2];
                var bracketLen = raw[1] === '/' ? 2 : 1;
                push('hl-bracket', raw.slice(0, bracketLen));
                push('hl-tag', raw.slice(bracketLen));
            } else if (m[3] !== undefined) {
                // Attribute name followed by =
                push('hl-attr', m[3]);
                push('hl-bracket', '=');
            } else if (m[5] !== undefined) {
                // Quoted string (attribute value)
                push('hl-string', m[5]);
            } else if (m[6] !== undefined) {
                // Closing bracket > or />
                push('hl-bracket', m[6]);
            } else if (m[7] !== undefined) {
                // HTML entity
                push('hl-entity', m[7]);
            } else if (m[8] !== undefined) {
                // TeX maths
                push('hl-math', m[8]);
            }

            lastIndex = m.index + m[0].length;
        }

        // Remaining text
        if (lastIndex < code.length) {
            push(null, code.slice(lastIndex));
        }
        return tokens;
    }

    // One block per source line, so the gutter can number lines without
    // changing where the textarea above wraps them. Empty lines hold a
    // zero-width space so they keep their height; a trailing newline gives a
    // final empty line, matching the textarea's caret line.
    function highlightHTML(code) {
        var lines = [''];
        tokenizeHTML(code).forEach(function (token) {
            var parts = token.text.split('\n');
            for (var i = 0; i < parts.length; i++) {
                if (i > 0) lines.push('');
                if (!parts[i]) continue;
                lines[lines.length - 1] += token.cls
                    ? '<span class="' + token.cls + '">' + esc(parts[i]) + '</span>'
                    : esc(parts[i]);
            }
        });
        return lines.map(function (line) {
            return '<span class="hl-line">' + (line || '​') + '</span>';
        }).join('');
    }

    function setGutter(lineCount) {
        var lastLine = bodyStartLine + Math.max(1, lineCount) - 1;
        var digits = Math.max(2, String(lastLine).length);
        if (editorContainer && editorContainer.style && editorContainer.style.setProperty) {
            editorContainer.style.setProperty('--gutter', 'calc(' + digits + 'ch + 22px)');
        }
        if (highlightingContent && highlightingContent.style) {
            highlightingContent.style.counterReset = 'line ' + (bodyStartLine - 1);
        }
    }

    function updateHighlighting() {
        highlightingContent.innerHTML = highlightHTML(editor.value);
        setGutter(editor.value.split('\n').length);
    }

    function editorMessageHandler() {
        if (!window.webkit || !window.webkit.messageHandlers) return null;
        return window.webkit.messageHandlers.editor || null;
    }

    /* ===== Scroll Sync ===== */

    editor.addEventListener('scroll', function () {
        highlightingPre.scrollTop = editor.scrollTop;
        highlightingPre.scrollLeft = editor.scrollLeft;
    });

    /* ===== Called from Swift ===== */

    function splitSectionHTML(html) {
        var openStart = html.search(/<section\b/i);
        if (openStart < 0) {
            return { openingTag: '<section>', body: html || '', closingTag: '</section>' };
        }
        var openEnd = findTagEnd(html, openStart);
        var closeMatch = html.match(/<\/section>\s*$/i);
        if (openEnd < 0 || !closeMatch) {
            return { openingTag: '<section>', body: html || '', closingTag: '</section>' };
        }
        var closeStart = html.length - closeMatch[0].length;
        return {
            openingTag: html.slice(openStart, openEnd + 1),
            body: html.slice(openEnd + 1, closeStart),
            closingTag: html.slice(closeStart).trim()
        };
    }

    function findTagEnd(html, start) {
        var quote = null;
        for (var i = start; i < html.length; i++) {
            var ch = html[i];
            if (quote) {
                if (ch === quote) quote = null;
            } else if (ch === '"' || ch === "'") {
                quote = ch;
            } else if (ch === '>') {
                return i;
            }
        }
        return -1;
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

    function warnMalformedEditorContent() {
        if (typeof console !== 'undefined' && console.warn) {
            console.warn('Presto: ignored malformed editor content');
        }
    }

    function captureSelectionState(nextIndex) {
        var sameSlide = currentIndex === nextIndex;
        return {
            sameSlide: sameSlide,
            focused: document.activeElement === editor,
            start: editor.selectionStart,
            end: editor.selectionEnd,
            scrollTop: editor.scrollTop,
            scrollLeft: editor.scrollLeft,
            value: editor.value,
            beforeStart: editor.value.slice(Math.max(0, editor.selectionStart - SELECTION_ANCHOR_RADIUS), editor.selectionStart),
            afterStart: editor.value.slice(editor.selectionStart, Math.min(editor.value.length, editor.selectionStart + SELECTION_ANCHOR_RADIUS)),
            beforeEnd: editor.value.slice(Math.max(0, editor.selectionEnd - SELECTION_ANCHOR_RADIUS), editor.selectionEnd),
            afterEnd: editor.value.slice(editor.selectionEnd, Math.min(editor.value.length, editor.selectionEnd + SELECTION_ANCHOR_RADIUS))
        };
    }

    function anchoredOffset(nextValue, before, after, fallback) {
        if (before && after) {
            var beforeIdx = nextValue.indexOf(before);
            while (beforeIdx >= 0) {
                var candidate = beforeIdx + before.length;
                var afterIdx = nextValue.indexOf(after, candidate);
                if (afterIdx >= 0 && afterIdx - candidate < SELECTION_ANCHOR_MAX_GAP) return candidate;
                beforeIdx = nextValue.indexOf(before, beforeIdx + 1);
            }
        }
        if (before) {
            var b = nextValue.indexOf(before);
            if (b >= 0) return b + before.length;
        }
        if (after) {
            var a = nextValue.indexOf(after);
            if (a >= 0) return a;
        }
        return Math.max(0, Math.min(nextValue.length, fallback));
    }

    function restoreSelectionState(state) {
        if (!state || !state.sameSlide) return;
        var start = anchoredOffset(editor.value, state.beforeStart, state.afterStart, state.start);
        var end = anchoredOffset(editor.value, state.beforeEnd, state.afterEnd, state.end);
        if (end < start) end = start;
        editor.selectionStart = start;
        editor.selectionEnd = end;
        editor.scrollTop = state.scrollTop;
        editor.scrollLeft = state.scrollLeft;
        highlightingPre.scrollTop = editor.scrollTop;
        highlightingPre.scrollLeft = editor.scrollLeft;
        if (state.focused) editor.focus();
    }

    // bodyStartLineArg (optional): 1-based line in the deck file of the
    // body's first line, so the gutter shows real file line numbers.
    window.setContent = function (openingOrHTML, bodyOrIndex, closingOrSlideNumber, slideIndexOrTotal, slideNumber, totalSlides, discardLocalChanges, bodyStartLineArg) {
        var openingTag;
        var bodyHTML;
        var closingTag;
        var slideIndex;

        // Backward compatibility for older callers: setContent(fullSection, index, num, total).
        if (typeof slideIndexOrTotal === 'undefined' || typeof bodyOrIndex === 'number') {
            var parts = splitSectionHTML(openingOrHTML || '');
            openingTag = parts.openingTag;
            bodyHTML = parts.body;
            closingTag = parts.closingTag;
            slideIndex = bodyOrIndex;
            slideNumber = closingOrSlideNumber;
            totalSlides = slideIndexOrTotal;
        } else {
            openingTag = openingOrHTML || '<section>';
            bodyHTML = bodyOrIndex || '';
            closingTag = closingOrSlideNumber || '</section>';
            slideIndex = slideIndexOrTotal;
        }

        var parsedSlideIndex = parseStrictNonNegativeInt(slideIndex);
        var parsedSlideNumber = parseStrictPositiveInt(slideNumber);
        var parsedTotalSlides = parseStrictPositiveInt(totalSlides);
        if (
            parsedSlideIndex === null ||
            parsedSlideNumber === null ||
            parsedTotalSlides === null ||
            parsedSlideIndex >= parsedTotalSlides ||
            parsedSlideNumber > parsedTotalSlides ||
            parsedSlideNumber !== parsedSlideIndex + 1 ||
            typeof openingTag !== 'string' ||
            typeof bodyHTML !== 'string' ||
            typeof closingTag !== 'string'
        ) {
            warnMalformedEditorContent();
            return;
        }

        slideIndex = parsedSlideIndex;
        slideNumber = parsedSlideNumber;
        totalSlides = parsedTotalSlides;

        var selectionState = captureSelectionState(slideIndex);
        var sameSlide = selectionState.sameSlide;
        // A delayed visual-editor mirror must not replace newer source typing.
        // The native bridge explicitly acknowledges this buffer after accepting it.
        if (sameSlide && dirty && !discardLocalChanges) return;
        currentIndex = slideIndex;
        if (!sameSlide || discardLocalChanges) {
            dirty = false;
            pendingSave = null;
            clearTimeout(debounceTimer);
            postDirtyState();
        }
        if (!sameSlide || !dirty) {
            lastCleanBodyHTML = bodyHTML;
        }
        sectionOpen.textContent = openingTag;
        sectionClose.textContent = closingTag;
        sectionOpen.style.webkitUserSelect = 'none';
        sectionOpen.style.userSelect = 'none';
        sectionClose.style.webkitUserSelect = 'none';
        sectionClose.style.userSelect = 'none';
        if (editor.value !== bodyHTML) {
            editor.value = bodyHTML;
        }
        var parsedStartLine = parseStrictPositiveInt(bodyStartLineArg);
        bodyStartLine = parsedStartLine === null ? 1 : parsedStartLine;
        slideLabel.textContent = 'Slide ' + slideNumber + ' / ' + totalSlides;
        showStatus('');
        updateHighlighting();
        restoreSelectionState(selectionState);
    };

    /* ===== Send changes to Swift ===== */

    function postChange() {
        if (currentIndex < 0 || !dirty) return false;
        showStatus('Saving…', 'saving');
        pendingSave = {
            id: nextSaveId++,
            index: currentIndex,
            html: editor.value
        };
        var handler = editorMessageHandler();
        if (handler) {
            handler.postMessage({
                type: 'editorChanged',
                html: pendingSave.html,
                index: pendingSave.index,
                saveId: pendingSave.id
            });
        }
        return true;
    }

    window.flushPendingChange = function () {
        clearTimeout(debounceTimer);
        return postChange();
    };

    window.focusEditor = function () {
        editor.focus();
    };

    window.getPendingEditorChange = function () {
        var wasDirty = dirty;
        if (wasDirty) {
            clearTimeout(debounceTimer);
        }
        return {
            index: currentIndex,
            html: editor.value,
            dirty: wasDirty
        };
    };

    // Any status other than an error also clears the save-error banner.
    function showStatus(text, cls) {
        saveStatus.textContent = text;
        saveStatus.className = cls || '';
        saveStatus.title = '';
        if (cls !== 'error') {
            if (saveError) saveError.hidden = true;
            if (saveErrorText) saveErrorText.textContent = '';
        }
    }

    // A failed save stays visible (header and banner) until the slide saves.
    function showSaveError(message) {
        var text = message || 'Save failed';
        showStatus('Not saved', 'error');
        saveStatus.title = text;
        if (saveErrorText) saveErrorText.textContent = text;
        if (saveError) saveError.hidden = false;
    }

    if (saveRetry) {
        saveRetry.addEventListener('click', function () {
            clearTimeout(debounceTimer);
            if (!postChange()) showStatus('');
        });
    }

    function markCurrentBufferClean() {
        dirty = false;
        pendingSave = null;
        lastCleanBodyHTML = editor.value;
        postDirtyState();
    }

    // Called from Swift after successful save
    window.saveConfirmed = function (saveId) {
        if (!pendingSave || pendingSave.id !== saveId) {
            return;
        }
        if (pendingSave.index !== currentIndex || pendingSave.html !== editor.value) {
            pendingSave = null;
            return;
        }
        markCurrentBufferClean();
        showStatus('Saved', 'saved');
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () {
            showStatus('');
        }, STATUS_CLEAR_MS);
    };

    // Called from Swift after it synchronously applies a pending navigation flush.
    window.markEditorSaved = function (index, html) {
        if (index !== currentIndex || html !== editor.value) return;
        markCurrentBufferClean();
        if (saveStatus.className === 'error') showStatus('');
    };

    // Called from Swift after a failed disk write
    window.saveFailed = function (saveIdOrMessage, message) {
        var hasSaveId = typeof saveIdOrMessage === 'number';
        if (hasSaveId) {
            if (!pendingSave || pendingSave.id !== saveIdOrMessage) return;
            if (pendingSave.index !== currentIndex || pendingSave.html !== editor.value) {
                pendingSave = null;
                return;
            }
        }
        dirty = dirty || editor.value !== lastCleanBodyHTML;
        pendingSave = null;
        showSaveError((hasSaveId ? message : saveIdOrMessage) || 'Save failed');
        clearTimeout(debounceTimer);
    };

    /* ===== Input handling ===== */

    function postDirtyState() {
        var handler = editorMessageHandler();
        if (handler) handler.postMessage({ type: 'editorDirty', index: currentIndex, dirty: dirty });
    }

    function scheduleChange() {
        dirty = true;
        postDirtyState();
        updateHighlighting();
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(postChange, DEBOUNCE_MS);
    }

    editor.addEventListener('input', function () {
        scheduleChange();
    });

    editor.addEventListener('blur', function () {
        clearTimeout(debounceTimer);
        postChange();
    });

    // Cmd+S: save immediately
    document.addEventListener('keydown', function (e) {
        if ((e.metaKey || e.ctrlKey) && e.key === 's') {
            e.preventDefault();
            clearTimeout(debounceTimer);
            postChange();
        }
    });

    // Tab key: insert spaces instead of changing focus
    editor.addEventListener('keydown', function (e) {
        if (e.key === 'Tab') {
            e.preventDefault();
            var start = editor.selectionStart;
            var end = editor.selectionEnd;
            var value = editor.value;
            var selected = value.substring(start, end);
            function unindentLine(line) {
                if (line.indexOf(TAB_INSERT) === 0) return line.slice(TAB_INSERT.length);
                if (line.indexOf('\t') === 0) return line.slice(1);
                return line;
            }
            if (start !== end && selected.indexOf('\n') >= 0) {
                var lineStart = value.lastIndexOf('\n', start - 1) + 1;
                var block = value.substring(lineStart, end);
                var lines = block.split('\n');
                var replacement = e.shiftKey
                    ? lines.map(unindentLine).join('\n')
                    : lines.map(function (line) {
                        return TAB_INSERT + line;
                    }).join('\n');
                editor.value = value.substring(0, lineStart) + replacement + value.substring(end);
                editor.selectionStart = lineStart;
                editor.selectionEnd = lineStart + replacement.length;
            } else {
                if (e.shiftKey) {
                    var singleLineStart = value.lastIndexOf('\n', start - 1) + 1;
                    var singleLineEnd = value.indexOf('\n', singleLineStart);
                    if (singleLineEnd < 0) singleLineEnd = value.length;
                    var singleLine = value.substring(singleLineStart, singleLineEnd);
                    var unindentedLine = unindentLine(singleLine);
                    if (unindentedLine === singleLine) return;
                    var removed = singleLine.length - unindentedLine.length;
                    editor.value = value.substring(0, singleLineStart) + unindentedLine + value.substring(singleLineStart + singleLine.length);
                    editor.selectionStart = Math.max(singleLineStart, start - removed);
                    editor.selectionEnd = Math.max(editor.selectionStart, end - removed);
                } else {
                    editor.value = value.substring(0, start) + TAB_INSERT + value.substring(end);
                    editor.selectionStart = editor.selectionEnd = start + TAB_INSERT.length;
                }
            }
            scheduleChange();
        }
    });

    /* ===== AI Refine Bar ===== */

    var refineForm = document.getElementById('refine-form');
    var refineInput = document.getElementById('refine-input');
    var refineSubmit = document.getElementById('refine-submit');
    var refineUndoBtn = document.getElementById('refine-undo');
    var refineRedoBtn = document.getElementById('refine-redo');
    var refineHistoryToggle = document.getElementById('refine-history-toggle');
    var refineHistoryPanel = document.getElementById('refine-history-panel');
    var refineHistoryList = document.getElementById('refine-history-list');
    var refineHistoryEmpty = document.getElementById('refine-history-empty');
    var refineStatusEl = document.getElementById('refine-status');
    var refineStatusText = document.getElementById('refine-status-text');
    var refineErrorEl = document.getElementById('refine-error');
    var refineErrorText = document.getElementById('refine-error-text');
    var refineErrorDismiss = document.getElementById('refine-error-dismiss');

    // Latest state from Swift (per-slide; replaced wholesale on each slide change or task event)
    var refineState = {
        slideIndex: -1,
        running: null,
        queued: [],
        history: [],
        canUndo: false,
        canRedo: false,
        lastError: null
    };

    // Entries come in two shapes: Presto's own {instructions, timestamp,
    // applied, status} and the unified history's {id, prompt, at}. An entry
    // from the unified history is on this slide's undo stack, so it counts as
    // applied.
    function normalizeRefineEntry(entry) {
        if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
            return null;
        }
        var status = typeof entry.status === 'string' ? entry.status : '';
        var instructions = typeof entry.instructions === 'string'
            ? entry.instructions
            : (typeof entry.prompt === 'string' ? entry.prompt : '');
        var timestamp = entry.timestamp !== undefined ? entry.timestamp : entry.at;
        return {
            id: typeof entry.id === 'string' || typeof entry.id === 'number' ? entry.id : null,
            status: status,
            applied: typeof entry.applied === 'boolean' ? entry.applied : status !== 'error',
            instructions: instructions,
            timestamp: typeof timestamp === 'string' || typeof timestamp === 'number'
                ? timestamp
                : ''
        };
    }

    function normalizeRefineState(newState) {
        if (!newState || typeof newState !== 'object' || Array.isArray(newState)) {
            return refineState;
        }
        var running = normalizeRefineEntry(newState.running);
        var queued = Array.isArray(newState.queued)
            ? newState.queued.map(normalizeRefineEntry).filter(Boolean)
            : [];
        var history = Array.isArray(newState.history)
            ? newState.history.map(normalizeRefineEntry).filter(Boolean)
            : [];
        return {
            slideIndex: typeof newState.slideIndex === 'number' && Number.isSafeInteger(newState.slideIndex)
                ? newState.slideIndex
                : -1,
            running: running,
            queued: queued,
            history: history,
            canUndo: newState.canUndo === true,
            canRedo: newState.canRedo === true,
            undoLabel: typeof newState.undoLabel === 'string' ? newState.undoLabel : '',
            redoLabel: typeof newState.redoLabel === 'string' ? newState.redoLabel : '',
            lastError: typeof newState.lastError === 'string' ? newState.lastError : null
        };
    }

    function earlierChangeCount(history) {
        var count = 0;
        for (var i = 0; i < history.length; i++) {
            if (history[i].applied && history[i].status !== 'error') count++;
        }
        return count;
    }

    function setAttr(el, name, value) {
        if (el && typeof el.setAttribute === 'function') el.setAttribute(name, value);
    }

    function postRefineMessage(payload) {
        var handler = editorMessageHandler();
        if (handler) handler.postMessage(payload);
    }

    function renderRefineState() {
        var queuedCount = refineState.queued ? refineState.queued.length : 0;

        // Status line: the queued count comes before the running task's
        // instructions, so the one-line ellipsis never cuts it off.
        if (refineState.running) {
            refineStatusEl.hidden = false;
            var queuedNote = queuedCount > 0 ? ' (+' + queuedCount + ' queued)' : '';
            var runText = 'Refining' + queuedNote + ': ' + (refineState.running.instructions || 'this slide');
            refineStatusText.textContent = runText;
            refineStatusText.title = runText;
        } else if (queuedCount > 0) {
            refineStatusEl.hidden = false;
            refineStatusText.textContent = queuedCount === 1 ? '1 change queued' : queuedCount + ' changes queued';
            refineStatusText.title = refineStatusText.textContent;
        } else {
            refineStatusEl.hidden = true;
            refineStatusText.textContent = '';
        }

        // Error
        if (refineState.lastError) {
            refineErrorEl.hidden = false;
            refineErrorText.textContent = refineState.lastError;
        } else {
            refineErrorEl.hidden = true;
            refineErrorText.textContent = '';
        }

        // Undo last / Redo (the unified history, routed through Presto)
        refineUndoBtn.disabled = !refineState.canUndo;
        refineUndoBtn.title = refineState.canUndo && refineState.undoLabel ? refineState.undoLabel : 'Undo last change';
        refineRedoBtn.disabled = !refineState.canRedo;
        refineRedoBtn.hidden = !refineState.canRedo;
        refineRedoBtn.title = refineState.redoLabel || 'Redo';

        // "n earlier changes on this slide" opens the list of earlier prompts
        var count = earlierChangeCount(refineState.history || []);
        refineHistoryToggle.hidden = count === 0;
        refineHistoryToggle.textContent = count === 1
            ? '1 earlier change on this slide'
            : count + ' earlier changes on this slide';
        if (count === 0 && (refineState.history || []).length === 0) {
            refineHistoryPanel.hidden = true;
        }
        setAttr(refineHistoryToggle, 'aria-expanded', refineHistoryPanel.hidden ? 'false' : 'true');

        // History panel (only re-render if visible)
        if (!refineHistoryPanel.hidden) {
            renderHistoryPanel();
        }
    }

    function renderHistoryPanel() {
        var history = refineState.history || [];
        if (history.length === 0) {
            refineHistoryEmpty.hidden = false;
            refineHistoryList.innerHTML = '';
            return;
        }
        refineHistoryEmpty.hidden = true;
        refineHistoryList.innerHTML = '';
        // Show newest first
        for (var i = history.length - 1; i >= 0; i--) {
            var entry = history[i];
            var li = document.createElement('li');
            li.className = 'refine-history-item';

            var marker = document.createElement('span');
            marker.className = 'refine-history-marker';
            if (entry.status === 'error') {
                marker.classList.add('error');
            } else if (entry.applied) {
                marker.classList.add('applied');
            } else {
                marker.classList.add('unapplied');
            }
            li.appendChild(marker);

            var text = document.createElement('span');
            text.className = 'refine-history-text';
            text.textContent = entry.instructions;
            text.title = entry.instructions;
            li.appendChild(text);

            var time = document.createElement('span');
            time.className = 'refine-history-time';
            try {
                var d = new Date(entry.timestamp);
                time.textContent = Number.isNaN(d.getTime())
                    ? ''
                    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            } catch (e) {
                time.textContent = '';
            }
            li.appendChild(time);

            refineHistoryList.appendChild(li);
        }
    }

    var REFINE_INPUT_MAX_PX = 132;

    function autosizeRefineInput() {
        if (!refineInput.style || typeof refineInput.scrollHeight !== 'number') return;
        refineInput.style.height = 'auto';
        refineInput.style.height = Math.min(refineInput.scrollHeight, REFINE_INPUT_MAX_PX) + 'px';
    }

    function updateRefineSubmitState() {
        if (!refineSubmit || !refineSubmit.classList) return;
        if (String(refineInput.value || '').trim()) refineSubmit.classList.remove('empty');
        else refineSubmit.classList.add('empty');
    }

    function focusRefine() {
        refineInput.focus();
        if (typeof refineInput.scrollIntoView === 'function') refineInput.scrollIntoView({ block: 'nearest' });
    }

    // Called from Swift on slide change, on task complete, on enqueue, etc.;
    // focus() is Presto's ⌘J (Refine with AI…) after it opens the inspector.
    window.prestoRefine = {
        setState: function (newState) {
            refineState = normalizeRefineState(newState);
            renderRefineState();
        },
        focus: focusRefine
    };
    window.focusRefine = focusRefine;

    function submitRefine() {
        var instructions = String(refineInput.value || '').trim();
        if (!instructions) {
            focusRefine();
            return;
        }
        postRefineMessage({ type: 'refineRequested', instructions: instructions });
        refineInput.value = '';
        autosizeRefineInput();
        updateRefineSubmitState();
        // Swift pushes the real running/queued state moments later; nothing is
        // faked here, so a rejected request cannot drift.
    }

    refineForm.addEventListener('submit', function (e) {
        e.preventDefault();
        submitRefine();
    });

    // ⌘↩ sends; a plain Return is a new line in the request.
    refineInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submitRefine();
        }
    });

    refineInput.addEventListener('input', function () {
        autosizeRefineInput();
        updateRefineSubmitState();
    });
    updateRefineSubmitState();

    // While the prompt has focus Presto leaves ⌘Z, ⌘A and ⌘-arrows to it.
    refineInput.addEventListener('focus', function () {
        postRefineMessage({ type: 'refineTextEditing', active: true });
    });
    refineInput.addEventListener('blur', function () {
        postRefineMessage({ type: 'refineTextEditing', active: false });
    });

    if (inspectorClose) {
        inspectorClose.addEventListener('click', function () {
            var handler = editorMessageHandler();
            if (handler) handler.postMessage({ type: 'inspectorClose' });
        });
    }

    refineUndoBtn.addEventListener('click', function () {
        if (refineUndoBtn.disabled) return;
        postRefineMessage({ type: 'refineUndo' });
    });

    refineRedoBtn.addEventListener('click', function () {
        if (refineRedoBtn.disabled) return;
        postRefineMessage({ type: 'refineRedo' });
    });

    refineHistoryToggle.addEventListener('click', function () {
        refineHistoryPanel.hidden = !refineHistoryPanel.hidden;
        setAttr(refineHistoryToggle, 'aria-expanded', refineHistoryPanel.hidden ? 'false' : 'true');
        if (!refineHistoryPanel.hidden) {
            renderHistoryPanel();
        }
    });

    refineErrorDismiss.addEventListener('click', function () {
        postRefineMessage({ type: 'refineDismissError' });
    });

    // ⌘J focuses Refine from anywhere in the inspector (the menu item does the
    // same from the rest of the window). ⌘I is left alone: it is Italic.
    document.addEventListener('keydown', function (e) {
        if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'j' || e.key === 'J')) {
            e.preventDefault();
            focusRefine();
        }
    });

    /* ===== Signal ready ===== */

    var readyHandler = editorMessageHandler();
    if (readyHandler) {
        readyHandler.postMessage({ type: 'ready' });
    }

})();

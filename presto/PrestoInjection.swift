import Cocoa
import WebKit

// What Presto adds to deck pages once they load: the shared UI tokens, the
// layout script (presto/edit-layout.js), the edit chrome and visual editor in
// the edit preview, and the audience scripts (plus the one-screen control
// bar) in the audience view. Files are read from the webslides checkout
// that serves the deck; a missing or unreadable one is named in the edit
// view's status instead of leaving a half-working editor.

struct InjectedScript {
    let name: String
    let source: String
}

extension AppDelegate {

    // MARK: - Injected Files

    func prestoResourceURL(_ name: String, for deckURL: URL) -> URL {
        resolveDeckResourceRoots(for: deckURL).webSlidesRoot.appendingPathComponent("presto/" + name)
    }

    // Overridden in tests to simulate dehydrated or missing files.
    @objc func readPrestoResource(_ name: String, for deckURL: URL) -> String? {
        let url = prestoResourceURL(name, for: deckURL)
        do {
            return try String(contentsOf: url, encoding: .utf8)
        } catch {
            NSLog("Presto: cannot read %@: %@", url.path, error.localizedDescription)
            return nil
        }
    }

    static func styleInjectionJS(id: String, css: String, literal: (String) -> String) -> String {
        """
        (function (id, css) {
            if (!document.head || document.getElementById(id)) return;
            var style = document.createElement('style');
            style.id = id;
            style.textContent = css;
            document.head.appendChild(style);
        })(\(literal(id)), \(literal(css)));
        """
    }

    // Runs the scripts one evaluation at a time, in order, so one broken
    // file is reported by name and does not stop the others.
    func evaluateInOrder(_ scripts: [InjectedScript], in webView: WKWebView, generation: Int,
                         failures: [String] = [], completion: @escaping ([String], Any?) -> Void) {
        guard let first = scripts.first else {
            completion(failures, nil)
            return
        }
        webView.evaluateJavaScript(first.source) { [weak self, weak webView] result, error in
            guard let self, let webView, self.sessionGeneration == generation else { return }
            var failures = failures
            if let error {
                NSLog("Presto: %@ failed in the page: %@", first.name, "\(error)")
                failures.append(first.name)
            }
            let rest = Array(scripts.dropFirst())
            if rest.isEmpty {
                completion(failures, result)
            } else {
                self.evaluateInOrder(rest, in: webView, generation: generation, failures: failures,
                                     completion: completion)
            }
        }
    }

    // MARK: - Page Load

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let role = webContentRole(for: webView) else {
            NSLog("Presto: ignored didFinish from stale webview")
            return
        }
        NSLog("Presto: didFinish for %@", role.rawValue)
        let generation = sessionGeneration

        switch role {
        case .export:
            NSLog("Presto: export webview loaded, waiting for WS init")
            maybeSimulateWebContentTermination(role: .export, webView: webView)
            startExportCapture(webView: webView, generation: exportGeneration)
        case .presenter:
            guard let deckURL = currentDeckURL else { return }
            let deckURLLiteral = jsStringLiteral(deckURL.absoluteString)
            webView.evaluateJavaScript("initPresenter(\(deckURLLiteral))") { [weak self, weak webView] _, error in
                guard let self, let webView,
                      self.sessionGeneration == generation,
                      self.consoleWebView === webView else { return }
                if let error = error {
                    NSLog("Presto: initPresenter error: %@", "\(error)")
                } else {
                    NSLog("Presto: initPresenter success")
                }
            }
        case .editorPreview:
            injectEditPreview(into: webView, generation: generation)
        case .editor:
            NSLog("Presto: editor HTML loaded")
            // editorReady will be set when we receive the 'ready' message
        case .audience:
            injectAudience(into: webView, generation: generation)
        case .decks:
            break  // decks.js posts decksReady
        case .thumbnail:
            thumbnailPageLoaded(webView)
        }
    }

    // Deck pages never navigate away: a clicked web link opens in the
    // browser, and a file or other page is refused, so a stray click cannot
    // replace the editor or the audience's slide with something else.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard navigationAction.targetFrame?.isMainFrame != false,
              navigationAction.navigationType == .linkActivated,
              let target = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        func withoutFragment(_ url: URL?) -> String? {
            guard let url, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
            parts.fragment = nil
            return parts.string
        }
        if withoutFragment(target) == withoutFragment(webView.url) {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        if let scheme = target.scheme?.lowercased(), scheme == "http" || scheme == "https" || scheme == "mailto" {
            NSWorkspace.shared.open(target)
        } else {
            NSLog("Presto: refused navigation to %@", target.absoluteString)
        }
    }

    // MARK: - Edit Preview

    static let editRailWidth = 232
    static let editStepStripHeight = 72
    static let editCanvasPadding = 28

    func editLayoutOptionsJS() -> String {
        "{ mode: 'edit', insetLeft: \(railVisible ? AppDelegate.editRailWidth : 0), insetRight: 0, insetTop: 0, insetBottom: \(AppDelegate.editStepStripHeight), padding: \(AppDelegate.editCanvasPadding) }"
    }

    func applyEditLayout() {
        guard isEditorMode, let preview = previewWebView else { return }
        preview.evaluateJavaScript("""
        window.__prestoLayoutOptions = \(editLayoutOptionsJS());
        if (typeof window.__prestoApplyLayout === 'function') window.__prestoApplyLayout();
        """) { _, _ in }
    }

    func injectEditPreview(into webView: WKWebView, generation: Int) {
        guard isEditorMode, let deckURL = currentDeckURL else { return }
        // A fresh page has no selection or text session yet, even when the
        // editor's script below cannot be read (keys go to navigation).
        wysiwygTextEditingActive = false
        wysiwygHasSelection = false
        editHistoryAvailable = false
        editChromeAvailable = false
        selectionKind = nil

        var missing: [String] = []
        func read(_ name: String, required: Bool = true) -> String? {
            let source = readPrestoResource(name, for: deckURL)
            if source == nil && required { missing.append(name) }
            return source
        }
        let tokens = read("ui-tokens.css", required: false)
        let layout = read("edit-layout.js")
        let chromeCSS = read("edit-chrome.css")
        let chrome = read("edit-chrome.js")
        let guides = read("wysiwyg-guides.js", required: false)
        let wysiwyg = read("wysiwyg.js")

        var first: [InjectedScript] = []
        if let tokens {
            first.append(InjectedScript(name: "ui-tokens.css", source: AppDelegate.styleInjectionJS(
                id: "presto-ui-tokens", css: tokens, literal: jsStringLiteral)))
        }
        if let layout { first.append(InjectedScript(name: "edit-layout.js", source: layout)) }
        if let chromeCSS {
            first.append(InjectedScript(name: "edit-chrome.css", source: AppDelegate.styleInjectionJS(
                id: "presto-edit-chrome-style", css: chromeCSS, literal: jsStringLiteral)))
        }
        if let chrome { first.append(InjectedScript(name: "edit-chrome.js", source: chrome)) }
        first.append(InjectedScript(name: "Presto preview script",
                                    source: previewCoreJS(slideIndex: currentEditorSlideIndex,
                                                          slideStep: currentEditorStep)))

        evaluateInOrder(first, in: webView, generation: generation) { [weak self, weak webView] failures, _ in
            guard let self, let webView, self.previewWebView === webView, self.isEditorMode else { return }
            NSLog("Presto: editor preview injected")
            missing += failures
            self.maybeSimulateWebContentTermination(role: .editorPreview, webView: webView)
            guard let wysiwyg else {
                // No visual editor: the rail still navigates.
                webView.evaluateJavaScript(
                    "typeof window.__prestoStartEditChrome === 'function' && window.__prestoStartEditChrome()"
                ) { _, _ in }
                self.finishEditInjection(missing: missing, webView: webView)
                return
            }
            var editor: [InjectedScript] = []
            if let guides { editor.append(InjectedScript(name: "wysiwyg-guides.js", source: guides)) }
            editor.append(InjectedScript(name: "wysiwyg.js", source: wysiwyg))
            editor.append(InjectedScript(name: "visual editor start", source: """
                (function () {
                    if (typeof window.__prestoStartEditChrome === 'function') { window.__prestoStartEditChrome(); }
                    if (window.__prestoWysiwyg) { window.__prestoWysiwyg.enable(); }
                    return JSON.stringify({
                        editor: !!window.__prestoWysiwyg,
                        history: !!(window.PrestoEdit && typeof PrestoEdit.undo === 'function'),
                        chrome: !!(window.PrestoEditChrome && typeof PrestoEditChrome.install === 'function')
                    });
                })()
                """))
            self.evaluateInOrder(editor, in: webView, generation: generation) { [weak self, weak webView] failures, result in
                guard let self, let webView, self.previewWebView === webView, self.isEditorMode else { return }
                missing += failures
                if let json = result as? String, let data = json.data(using: .utf8),
                   let found = try? JSONSerialization.jsonObject(with: data) as? [String: Bool] {
                    if found["editor"] != true && !missing.contains("wysiwyg.js") { missing.append("wysiwyg.js") }
                    self.editHistoryAvailable = found["history"] == true
                    self.editChromeAvailable = found["chrome"] == true
                }
                if !missing.contains("wysiwyg.js") {
                    NSLog("Presto: wysiwyg layer injected")
                    self.updateEditorSourceToggle()
                    self.setEditorPDFExportAvailable(!self.exportRequestActive)
                    self.requestUndoState()
                }
                self.finishEditInjection(missing: missing, webView: webView)
            }
        }
    }

    func finishEditInjection(missing: [String], webView: WKWebView) {
        let unique = missing.reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        if unique.isEmpty {
            editInjectionProblem = nil
        } else if unique.contains("wysiwyg.js") {
            editInjectionProblem = "Visual editing unavailable: could not load " + unique.joined(separator: ", ")
            wysiwygTextEditingActive = false
            wysiwygHasSelection = false
        } else {
            editInjectionProblem = "Could not load " + unique.joined(separator: ", ")
        }
        if let problem = editInjectionProblem { NSLog("Presto: %@", problem) }
        updateToolbarState()
    }

    func requestUndoState() {
        previewWebView?.evaluateJavaScript(
            "JSON.stringify(window.PrestoEdit && typeof PrestoEdit.historyState === 'function' ? PrestoEdit.historyState() : null)"
        ) { [weak self] result, _ in
            guard let self, let json = result as? String, let data = json.data(using: .utf8),
                  let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            self.applyUndoState(state)
        }
    }

    // The preview's own script: deck-ready helpers, the layout glue for
    // presto/edit-layout.js, the in-place slide patch, the edit chrome's
    // install and the navigation reports. It must run without any of the
    // other injected files (each is optional at this point).
    func previewCoreJS(slideIndex: Int, slideStep: Int) -> String {
        """
        (function() {
        \(AppDelegate.deckReadyHelpersJS)
            window.__prestoLayoutOptions = \(editLayoutOptionsJS());
            function prestoApplyLayout() {
                try {
                    if (window.PrestoLayout && typeof window.PrestoLayout.apply === 'function') {
                        return window.PrestoLayout.apply(window.__prestoLayoutOptions);
                    }
                    if (typeof window.applyPrestoEditViewport === 'function') return window.applyPrestoEditViewport();
                } catch (e) {}
                return null;
            }
            window.__prestoApplyLayout = prestoApplyLayout;
        \(AppDelegate.slidePatchJS)
            function prestoPost(message) {
                if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.presto) {
                    window.webkit.messageHandlers.presto.postMessage(message);
                }
            }
            // The rail, step strip and link card go in once the visual editor
            // has loaded (the last injected script calls this); the chrome
            // itself waits for the deck's slides.
            var prestoChromeStarted = false;
            window.__prestoStartEditChrome = function () {
                if (prestoChromeStarted) return;
                prestoChromeStarted = true;
                var opts = { railVisible: \(railVisible ? "true" : "false"), ghostFuture: \(ghostFutureSteps ? "true" : "false") };
                if (window.PrestoEditChrome && typeof window.PrestoEditChrome.install === 'function') {
                    try { window.PrestoEditChrome.install(opts); } catch (e) {}
                }
                prestoApplyLayout();
            };
            function waitAndApply() {
                if (typeof WS === 'undefined' || !WS.slides || WS.slides.length === 0) {
                    requestAnimationFrame(waitAndApply);
                    return;
                }
                prestoApplyLayout();
                window.addEventListener('resize', prestoApplyLayout);
                var style = document.createElement('style');
                style.textContent = 'svg [data-step] { will-change: opacity; }';
                document.head.appendChild(style);
                // Re-fit tables under the final layout (fixes overlay alignment).
                // Tables still loading are fitted by the engine's final fit before WS.ready.
                if (typeof WS.refitTables === 'function') { WS.refitTables(); }
                WS.goToSlide(\(slideIndex), \(slideStep));
                prestoRestoreStepWhenReady(\(slideIndex), \(slideStep));
                WS.on('navigate', function(state) {
                    prestoPost({ type: 'slideChanged', index: state.index, step: state.step });
                });
                prestoPost({ type: 'slideChanged', index: WS.currentIndex, step: WS.currentStep || 0 });
                // Once external tables and math are in (the edit chrome
                // rebuilds its thumbnails then too), lay out again and tell
                // Presto the deck has settled.
                prestoWhenDeckReady(function() {
                    prestoApplyLayout();
                    prestoPost({ type: 'prestoPreviewSettled' });
                });
            }
            waitAndApply();
        })();
        """
    }

    // MARK: - Slide Patch

    // Replaces one slide in place after a source commit, a Refine result or
    // an outside change. `opts` says which (see previewSlidePatchJS): an
    // outside change clears that slide's visual history; a source or Refine
    // commit is recorded in it, so ⌘Z can take it back; the visual editor's
    // own undo replay neither clears nor records.
    static let slidePatchJS = """
        function parsePrestoStrictInt(raw) {
            if (raw === undefined || raw === null || raw === '') return null;
            raw = String(raw).trim();
            if (!/^\\d+$/.test(raw)) return null;
            var n = Number(raw);
            return Number.isSafeInteger(n) ? n : null;
        }
        function parsePrestoStepValue(raw) {
            if (raw === undefined || raw === null) return { start: 1, end: undefined };
            raw = String(raw).trim();
            if (raw === '') return { start: 1, end: undefined };
            if (raw.endsWith('+')) {
                var plusStart = parsePrestoStrictInt(raw.slice(0, -1));
                return plusStart === null ? null : { start: plusStart, end: undefined };
            }
            if (raw.indexOf('-') >= 0) {
                var parts = raw.split('-');
                if (parts.length !== 2) return null;
                var rangeStart = parsePrestoStrictInt(parts[0]);
                var rangeEnd = parsePrestoStrictInt(parts[1]);
                if (rangeStart === null || rangeEnd === null || rangeEnd < rangeStart) return null;
                return { start: rangeStart, end: rangeEnd };
            }
            var single = parsePrestoStrictInt(raw);
            return single === null ? null : { start: single, end: undefined };
        }
        function parsePrestoMaxStep(slide) {
            var max = 0;
            slide.querySelectorAll('[data-step], [data-highlight], [data-dim], [data-class]').forEach(function(el) {
                ['step', 'highlight', 'dim', 'class'].forEach(function(attr) {
                    var raw = el.dataset[attr];
                    if (raw === undefined) return;
                    var range = raw;
                    if (attr === 'class') {
                        var sep = raw.lastIndexOf(':');
                        if (sep <= 0) return;
                        range = raw.slice(sep + 1);
                    }
                    var parsed = parsePrestoStepValue(range);
                    if (!parsed) return;
                    max = Math.max(max, parsed.start, parsed.end || 0);
                });
            });
            return max;
        }
        function isPrestoStepActive(range, step) {
            return !!range && step >= range.start && (range.end === undefined || step <= range.end);
        }
        function applyPrestoStep(slide, step) {
            slide.querySelectorAll('[data-step]').forEach(function(el) {
                el.classList.toggle('ws-visible', isPrestoStepActive(parsePrestoStepValue(el.dataset.step), step));
            });
            slide.querySelectorAll('[data-highlight]').forEach(function(el) {
                el.classList.toggle('ws-highlighted', isPrestoStepActive(parsePrestoStepValue(el.dataset.highlight), step));
            });
            slide.querySelectorAll('[data-dim]').forEach(function(el) {
                el.classList.toggle('ws-dimmed', isPrestoStepActive(parsePrestoStepValue(el.dataset.dim), step));
            });
            slide.querySelectorAll('[data-class]').forEach(function(el) {
                var raw = el.dataset.class || '';
                var sep = raw.lastIndexOf(':');
                if (sep <= 0) return;
                var cls = raw.slice(0, sep).trim();
                if (!cls || /[\\t\\n\\f\\r ]/.test(cls)) return;
                el.classList.toggle(cls, isPrestoStepActive(parsePrestoStepValue(raw.slice(sep + 1)), step));
            });
        }
        function replacePrestoSlideReference(slides, current, replacement) {
            if (!slides) return;
            var referenceIndex = slides.indexOf(current);
            if (referenceIndex >= 0) slides[referenceIndex] = replacement;
        }
        function refreshPrestoThumbnail(index, replacement) {
            try {
                if (window.PrestoEditChrome && typeof window.PrestoEditChrome.refreshThumbnail === 'function') {
                    window.PrestoEditChrome.refreshThumbnail(index);
                } else if (typeof WS.updatePrestoEditSidebarThumbnail === 'function') {
                    WS.updatePrestoEditSidebarThumbnail(index, replacement);
                } else if (typeof window.updatePrestoEditSidebarThumbnail === 'function') {
                    window.updatePrestoEditSidebarThumbnail(index, replacement);
                } else if (typeof WS.rebuildPrestoEditSidebar === 'function') {
                    WS.rebuildPrestoEditSidebar();
                } else if (typeof window.rebuildPrestoEditSidebar === 'function') {
                    window.rebuildPrestoEditSidebar();
                }
            } catch (e) {}
        }
        window.replacePrestoEditSlideHTML = function(index, sectionHTML, opts) {
            if (!window.WS || !WS.slides || !Array.isArray(WS.slides)) return false;
            if (!Number.isSafeInteger(index) || index < 0 || index >= WS.slides.length) return false;
            if (typeof sectionHTML !== 'string') return false;
            var current = WS.slides[index];
            if (!current || !current.parentNode) return false;
            var template = document.createElement('template');
            template.innerHTML = sectionHTML.trim();
            var replacement = template.content.firstElementChild;
            if (!replacement || replacement.tagName !== 'SECTION' || !replacement.classList.contains('slide')) return false;
            if (replacement.querySelector('[data-table-src], img[data-inline]')) return false;
            if (current.classList.contains('appendix') !== replacement.classList.contains('appendix')) return false;
            var origin = opts && typeof opts.origin === 'string' ? opts.origin : null;
            var wasActive = current.classList.contains('ws-active');
            replacement.dataset.wsIndex = String(index);
            if (current.dataset.wsMainNumber) replacement.dataset.wsMainNumber = current.dataset.wsMainNumber;
            replacement.classList.toggle('ws-active', wasActive);
            replacement.style.cssText = current.style.cssText;
            current.replaceWith(replacement);
            WS.slides[index] = replacement;
            replacePrestoSlideReference(WS.mainSlides, current, replacement);
            replacePrestoSlideReference(WS.appendixSlides, current, replacement);
            // Authored source carries only data-reveal="each"; let the
            // engine write the per-item steps before they are counted.
            if (typeof WS.applyRevealShortcuts === 'function') {
                try { WS.applyRevealShortcuts(replacement); } catch (e) {}
            }
            var maxStep = parsePrestoMaxStep(replacement);
            if (WS.slideSteps && typeof WS.slideSteps.delete === 'function') WS.slideSteps.delete(current);
            if (WS.slideSteps && typeof WS.slideSteps.set === 'function') WS.slideSteps.set(replacement, maxStep);
            if (wasActive && typeof WS.goToSlide === 'function') {
                WS.goToSlide(index, Math.min(WS.currentStep || 0, maxStep));
            } else {
                applyPrestoStep(replacement, 0);
            }
            // The replacement is raw authored source ($...$ math, not the
            // rendered .katex nodes), so re-run KaTeX auto-render on it —
            // otherwise code-pane saves and willEdit flushes leave literal
            // dollar-delimited math in the preview until a full reload.
            if (typeof window.renderMathInElement === 'function') {
                try {
                    var wsKatex = window.__wsKatexOptions || {
                        delimiters: [
                            { left: '$$', right: '$$', display: true },
                            { left: '$', right: '$', display: false }
                        ],
                        throwOnError: false
                    };
                    window.renderMathInElement(replacement, wsKatex);
                } catch (e) { /* never let a math error break the patch */ }
            }
            // Lay out again after the math (edit-layout.js corrects KaTeX under zoom).
            if (typeof window.__prestoApplyLayout === 'function') window.__prestoApplyLayout();
            refreshPrestoThumbnail(index, replacement);
            // Tell the visual editor what happened to this slide's history.
            // Guarded: each half of the contract may be absent.
            if (origin === 'source' || origin === 'refine') {
                if (window.PrestoEdit && typeof window.PrestoEdit.recordExternalChange === 'function') {
                    try {
                        window.PrestoEdit.recordExternalChange({
                            index: index, beforeHTML: String(opts.beforeHTML || ''), afterHTML: sectionHTML, origin: origin,
                            prompt: typeof opts.prompt === 'string' ? opts.prompt : null
                        });
                    } catch (e) {}
                } else if (window.__prestoWysiwyg && typeof window.__prestoWysiwyg.notifyExternalReplace === 'function') {
                    try { window.__prestoWysiwyg.notifyExternalReplace(index); } catch (e) {}
                }
            } else if (origin !== 'history') {
                if (window.__prestoWysiwyg && typeof window.__prestoWysiwyg.notifyExternalReplace === 'function') {
                    try { window.__prestoWysiwyg.notifyExternalReplace(index); } catch (e) {}
                }
            }
            return true;
        };
    """

    // MARK: - Audience

    func injectAudience(into webView: WKWebView, generation: Int) {
        guard let deckURL = currentDeckURL else { return }
        var scripts: [InjectedScript] = []
        if let tokens = readPrestoResource("ui-tokens.css", for: deckURL) {
            scripts.append(InjectedScript(name: "ui-tokens.css", source: AppDelegate.styleInjectionJS(
                id: "presto-ui-tokens", css: tokens, literal: jsStringLiteral)))
        }
        if let layout = readPrestoResource("edit-layout.js", for: deckURL) {
            scripts.append(InjectedScript(name: "edit-layout.js", source: layout))
        }
        scripts.append(InjectedScript(name: "Presto audience script", source: audienceCoreJS()))
        // The control bar comes after the audience guards. On a second
        // display it is installed inert (oneScreen: false).
        if let hudCSS = readPrestoResource("present-hud.css", for: deckURL) {
            scripts.append(InjectedScript(name: "present-hud.css", source: AppDelegate.styleInjectionJS(
                id: "presto-present-hud-style", css: hudCSS, literal: jsStringLiteral)))
        }
        if let hud = readPrestoResource("present-hud.js", for: deckURL) {
            scripts.append(InjectedScript(name: "present-hud.js", source: hud))
        }
        scripts.append(InjectedScript(name: "Presto audience state", source: """
            (function () {
                if (window.PrestoHUD && typeof window.PrestoHUD.install === 'function') {
                    try { window.PrestoHUD.install({ oneScreen: \(audienceOneScreen ? "true" : "false") }); } catch (e) {}
                }
                return JSON.stringify({
                    index: WS.currentIndex,
                    step: WS.currentStep,
                    returnStackDepth: Array.isArray(WS.returnStack) ? WS.returnStack.length : -1
                });
            })()
            """))
        let expectedIndex = lastAudienceSlideIndex
        let expectedStep = lastAudienceStep
        let expectedReturnStackDepth = lastAudienceReturnStack.count
        evaluateInOrder(scripts, in: webView, generation: generation) { [weak self, weak webView] failures, result in
            guard let self, let webView, self.audienceWebView === webView else { return }
            if failures.contains("Presto audience script") || failures.contains("Presto audience state") {
                NSLog("Presto: audience mode injection error")
                return
            }
            if !failures.isEmpty { NSLog("Presto: audience view missing %@", failures.joined(separator: ", ")) }
            NSLog("Presto: audience mode injected")
            if self.webContentRecoveryAttempts[.audience, default: 0] > 0 {
                var matches = false
                if let json = result as? String,
                   let data = json.data(using: .utf8),
                   let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   self.finiteInt(state["index"]) == expectedIndex,
                   self.finiteInt(state["step"]) == expectedStep,
                   self.finiteInt(state["returnStackDepth"]) == expectedReturnStackDepth {
                    matches = true
                }
                NSLog("PrestoTestWebContent: restored audience index=%d step=%d stackDepth=%d match=%d",
                      expectedIndex, expectedStep, expectedReturnStackDepth, matches ? 1 : 0)
            }
            self.syncPresenterFromAudience()
            self.refreshDeckOutline()
            self.maybeSimulateWebContentTermination(role: .audience, webView: webView)
        }
    }

    // The audience page: engine audience mode, Presto's layout, the talk's
    // slide and step, navigation reports, and guards so keys and plain
    // slide clicks never move the deck on their own.
    func audienceCoreJS() -> String {
        """
        WS.mode = 'audience';
        document.body.classList.add('ws-mode-audience');
        (function () {
            var options = { mode: 'audience', insetLeft: 0, insetRight: 0, insetTop: 0, insetBottom: 0, padding: 0 };
            function prestoApplyLayout() {
                try {
                    if (window.PrestoLayout && typeof window.PrestoLayout.apply === 'function') {
                        window.PrestoLayout.apply(options);
                    }
                } catch (e) {}
            }
            function whenSlides() {
                if (!window.WS || !WS.slides || !WS.slides.length) { requestAnimationFrame(whenSlides); return; }
                prestoApplyLayout();
                if (typeof WS.refitTables === 'function') { WS.refitTables(); }
            }
            window.addEventListener('resize', prestoApplyLayout);
            whenSlides();
        })();
        \(audienceStateRestoreJS())
        \(AppDelegate.blackoutJS(blackoutActive));
        // Navigation the audience page makes itself (a clicked appendix
        // or back button) is reported so Presto can mirror it in the
        // presenter view; moves made by Presto's own commands are flagged
        // and skipped.
        (function hookPrestoAudienceNavigation() {
            if (!window.WS || typeof WS.on !== 'function') {
                requestAnimationFrame(hookPrestoAudienceNavigation);
                return;
            }
            if (WS.__prestoAudienceNavigationHooked) return;
            WS.__prestoAudienceNavigationHooked = true;
            WS.on('navigate', function() {
                if (window.__prestoRemoteNavigation) return;
                if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.presto) {
                    window.webkit.messageHandlers.presto.postMessage({ type: 'audienceNavigated' });
                }
            });
        })();
        document.addEventListener('keydown', function(e) {
            if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'TEXTAREA') {
                if (e.key === 'Escape' && !e.repeat && window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.presto) {
                    window.webkit.messageHandlers.presto.postMessage({ type: 'endPresentationRequested' });
                }
                e.stopImmediatePropagation();
                e.preventDefault();
            }
        }, true);
        // Slide clicks must not advance or trigger deck scripts, but links
        // and buttons (appendix jumps, back buttons, header links) go to
        // the engine so it keeps the appendix return stack.
        document.addEventListener('click', function(e) {
            var target = e.target && e.target.closest ? e.target : null;
            if (!target || !target.closest('.slide')) return;
            if (target.closest(\(jsStringLiteral(AppDelegate.audienceClickThroughSelector)))) return;
            e.stopImmediatePropagation();
        }, true);
        JSON.stringify({
            index: WS.currentIndex,
            step: WS.currentStep,
            returnStackDepth: Array.isArray(WS.returnStack) ? WS.returnStack.length : -1
        });
        """
    }
}

import Cocoa
import WebKit

// The one main window: Decks, Edit and the presenter console share it, each
// with its own native toolbar. This file owns mode switching, the deck
// session's lifetime, the edit surfaces, Decks mode and the hidden thumbnail
// renderer.

// Every Presto web view. A file dropped on any of them opens the deck in
// Edit (or is ignored); it never navigates the view to the dropped file.
final class PrestoWebView: WKWebView {
    weak var dropHandler: AppDelegate?
    var dropActive = false

    func droppedDeck(_ sender: NSDraggingInfo) -> URL? {
        let urls = sender.draggingPasteboard.readObjects(
            forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        return urls.lazy.compactMap { self.dropHandler?.deckFile(for: $0) }.first
    }

    func hasFileURLs(_ sender: NSDraggingInfo) -> Bool {
        sender.draggingPasteboard.canReadObject(forClasses: [NSURL.self],
                                                options: [.urlReadingFileURLsOnly: true])
    }

    func setDropActive(_ active: Bool) {
        guard dropActive != active else { return }
        dropActive = active
        dropHandler?.webViewDropActiveChanged(self, active: active)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard hasFileURLs(sender) else { return super.draggingEntered(sender) }
        let deck = droppedDeck(sender)
        setDropActive(deck != nil)
        return deck != nil ? .copy : []
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        guard hasFileURLs(sender) else { return super.draggingUpdated(sender) }
        return droppedDeck(sender) != nil ? .copy : []
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        if let sender, !hasFileURLs(sender) { super.draggingExited(sender); return }
        setDropActive(false)
    }

    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard hasFileURLs(sender) else { return super.prepareForDragOperation(sender) }
        return droppedDeck(sender) != nil
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard hasFileURLs(sender) else { return super.performDragOperation(sender) }
        setDropActive(false)
        guard let deck = droppedDeck(sender) else { return false }
        DispatchQueue.main.async { [weak self] in self?.dropHandler?.openDeck(deck, in: .edit) }
        return true
    }

    override func concludeDragOperation(_ sender: NSDraggingInfo?) {
        if let sender, !hasFileURLs(sender) { super.concludeDragOperation(sender) }
    }
}

// The toolbar's one primary action (Play from here): an accent-blue capsule
// that keeps its colour when the window is inactive, so its white title
// always stays legible.
final class PrestoPrimaryButton: NSButton {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        isBordered = false
        imageHugsTitle = true
        wantsLayer = true
        layer?.cornerRadius = 7
        layer?.backgroundColor = NSColor.prestoAccent.cgColor
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
    }

    override var isEnabled: Bool {
        didSet { layer?.backgroundColor = NSColor.prestoAccent.withAlphaComponent(isEnabled ? 1 : 0.45).cgColor }
    }

    override var intrinsicContentSize: NSSize {
        let size = super.intrinsicContentSize
        return NSSize(width: size.width + 24, height: max(size.height + 8, 28))
    }
}

extension NSColor {
    convenience init(prestoHex hex: UInt32, alpha: CGFloat = 1) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255,
                  alpha: alpha)
    }

    // presto/ui-tokens.css, for native chrome.
    static let prestoCanvas = NSColor(prestoHex: 0xE7EAEE)
    static let prestoInk = NSColor(prestoHex: 0x1C2532)
    static let prestoInk2 = NSColor(prestoHex: 0x485263)
    static let prestoDim = NSColor(prestoHex: 0x7B8390)
    static let prestoAccent = NSColor(prestoHex: 0x2F6BD8)
    static let prestoLive = NSColor(prestoHex: 0xD4462C)
    static let prestoOK = NSColor(prestoHex: 0x2E8B57)
}

extension AppDelegate: NSToolbarDelegate, NSSearchFieldDelegate, NSSplitViewDelegate {

    // MARK: - Main Window

    static let mainWindowAutosaveName = "PrestoMainWindow"

    @objc func showMainWindow() {
        let window = mainWindow ?? makeMainWindow()
        NSApp.activate(ignoringOtherApps: true)
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        window.makeKeyAndOrderFront(nil)
    }

    func makeMainWindow() -> NSWindow {
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let size = NSSize(width: min(1440, visible.width), height: min(900, visible.height))
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.isReleasedWhenClosed = false
        window.title = "Presto"
        window.titleVisibility = .hidden
        window.toolbarStyle = .unified
        window.minSize = NSSize(width: 900, height: 560)
        window.collectionBehavior = [.fullScreenPrimary]
        window.backgroundColor = .prestoCanvas
        window.appearance = NSAppearance(named: .aqua)
        window.delegate = self
        if !window.setFrameUsingName(AppDelegate.mainWindowAutosaveName) {
            window.setFrame(NSRect(x: visible.midX - size.width / 2, y: visible.midY - size.height / 2,
                                   width: size.width, height: size.height), display: false)
        }
        window.setFrameAutosaveName(AppDelegate.mainWindowAutosaveName)
        let container = NSView(frame: NSRect(origin: .zero, size: size))
        container.autoresizingMask = [.width, .height]
        window.contentView = container
        mainWindow = window
        installToolbar(for: mode)
        return window
    }

    // Swaps what fills the main window; the old view stays alive elsewhere.
    func setMainContent(_ view: NSView?) {
        guard let container = mainWindow?.contentView else { return }
        for subview in container.subviews where subview !== view {
            subview.removeFromSuperview()
        }
        guard let view else { return }
        if view.superview !== container {
            view.removeFromSuperview()
            view.frame = container.bounds
            view.autoresizingMask = [.width, .height]
            container.addSubview(view)
        } else {
            view.frame = container.bounds
        }
    }

    func updateWindowTitle() {
        guard let window = mainWindow else { return }
        if let deckURL = currentDeckURL {
            window.title = "Presto — \(deckName(for: deckURL.path))"
        } else {
            window.title = "Presto"
        }
    }

    // MARK: - Modes

    // Shows a mode's view and toolbar. Callers have already flushed edits.
    func showMode(_ newMode: PrestoMode) {
        if mainWindow == nil { _ = makeMainWindow() }
        mode = newMode
        switch newMode {
        case .decks:
            setMainContent(ensureDecksWebView())
            if decksReady { scheduleDecksRender() }
        case .edit:
            setMainContent(editorSplitView)
            if let preview = previewWebView, !editorSidebarVisible || !(mainWindow?.firstResponder is WKWebView) {
                mainWindow?.makeFirstResponder(preview)
            }
            applyEditLayout()
        case .present:
            if presentPhase == .live && audienceOneScreen {
                setMainContent(audienceWebView)
            } else {
                setMainContent(consoleWebView)
                if let console = consoleWebView { mainWindow?.makeFirstResponder(console) }
            }
        }
        installToolbar(for: newMode)
        updateWindowTitle()
        NSLog("Presto: mode=%@", AppDelegate.modeName(newMode))
    }

    static func modeName(_ mode: PrestoMode) -> String {
        switch mode {
        case .decks: return "decks"
        case .edit: return "edit"
        case .present: return "present"
        }
    }

    func modeEnabled(_ target: PrestoMode) -> Bool {
        switch target {
        case .decks: return presentPhase != .live
        case .edit: return currentDeckURL != nil && presentPhase != .live
        case .present: return currentDeckURL != nil
        }
    }

    // A mode change asked for by the user (segment, ⌘1-3, menu). Leaving
    // Edit flushes every edit surface first; a failed save keeps Edit open.
    func requestMode(_ target: PrestoMode) {
        guard target != mode else { return }
        guard modeEnabled(target) else {
            NSSound.beep()
            updateToolbarState()
            return
        }
        leaveCurrentMode { [weak self] left in
            guard let self, left else { self?.updateToolbarState(); return }
            switch target {
            case .decks:
                if self.presentPhase == .ready { self.leavePresentReady() }
                self.showMode(.decks)
            case .edit:
                if self.presentPhase == .ready { self.leavePresentReady() }
                guard self.ensureEditSurfaces() else { return }
                self.showMode(.edit)
            case .present:
                let start = self.positionForPresenting()
                self.enterPresentReady(origin: self.mode, index: start.index, step: start.step)
            }
        }
    }

    // Flush before Edit's views go off screen, and remember where Duncan was.
    func leaveCurrentMode(_ completion: @escaping (Bool) -> Void) {
        guard mode == .edit, isEditorMode else {
            completion(true)
            return
        }
        flushActiveEditor { [weak self] saved in
            guard let self else { return }
            if saved { self.recordDeckPosition(index: self.currentEditorSlideIndex, step: self.currentEditorStep) }
            completion(saved)
        }
    }

    // Where Present starts: the edit view's slide, else the deck's last place.
    func positionForPresenting() -> (index: Int, step: Int) {
        if isEditorMode { return (currentEditorSlideIndex, currentEditorStep) }
        if let deckURL = currentDeckURL, let last = deckLibrary.lastPosition(deckURL) {
            return (last.slide, last.step)
        }
        return (0, 0)
    }

    func recordDeckPosition(index: Int, step: Int, picture: Bool = true) {
        guard let deckURL = currentDeckURL else { return }
        let slide = sectionInfos.isEmpty ? max(0, index) : max(0, min(index, sectionInfos.count - 1))
        deckLibrary.setLastPosition(deckURL, slide: slide, step: max(0, step))
        if picture { enqueueThumbnail(deckURL, slide: slide, step: max(0, step), position: true) }
        scheduleDecksRender()
    }

    // MARK: - Opening Decks

    func startPresentation(deckURL: URL) {
        openDeck(deckURL, in: .present)
    }

    func startEditor(deckURL: URL) {
        openDeck(deckURL, in: .edit)
    }

    // Opens a deck in Edit (at its last position) or Present (ready, or live
    // at once). The current deck's edits are flushed first; a failed save
    // keeps it open. A live talk is never replaced.
    func openDeck(_ url: URL, in target: PrestoMode, goLive: Bool = false) {
        guard presentPhase != .live else {
            NSLog("Presto: refused to open %@ during a live presentation", url.path)
            NSSound.beep()
            return
        }
        showMainWindow()
        if let current = currentDeckURL,
           current.resolvingSymlinksInPath().standardizedFileURL == url.resolvingSymlinksInPath().standardizedFileURL {
            switchOpenDeck(to: target, goLive: goLive)
            return
        }
        guard FileManager.default.fileExists(atPath: url.path) else {
            showMissingDeckAlert(path: url.path)
            return
        }
        if isEditorMode {
            flushActiveEditor { [weak self] saved in
                if saved { self?.openNewDeck(url, in: target, goLive: goLive) }
            }
        } else {
            openNewDeck(url, in: target, goLive: goLive)
        }
    }

    func switchOpenDeck(to target: PrestoMode, goLive: Bool) {
        switch target {
        case .decks:
            requestMode(.decks)
        case .edit:
            requestMode(.edit)
        case .present:
            if goLive {
                leaveCurrentMode { [weak self] left in
                    guard let self, left else { return }
                    let start = self.positionForPresenting()
                    self.goLive(index: start.index, step: start.step, origin: self.mode == .present ? self.presentOrigin : self.mode)
                }
            } else if mode == .decks && presentPhase == .none {
                // Decks' "Present from start".
                enterPresentReady(origin: .decks, index: 0, step: 0)
            } else {
                requestMode(.present)
            }
        }
    }

    // After the old deck's edits are safe: validate, tear down, begin.
    @objc func openNewDeck(_ url: URL, in target: PrestoMode, goLive: Bool) {
        NSLog("Presto: opening %@ in %@", url.lastPathComponent, AppDelegate.modeName(target))
        guard let content = readDeckContentForOpening(deckURL: url),
              validateDeckHasSlides(deckURL: url, content: content) else {
            return
        }
        // Cleanup still belongs to the old URL until its writes have completed.
        if currentDeckURL != nil {
            if deckWrittenThisSession, let old = currentDeckURL { enqueueThumbnail(old, slide: 0, step: 0, position: false) }
            guard cleanupPresentation() else { return }
        }
        let origin: PrestoMode = mode == .present ? .decks : mode
        beginDeckSession(url, content: content)
        let last = deckLibrary.lastPosition(url)
        switch target {
        case .edit, .decks:
            pendingEditPosition = last.map { (index: $0.slide, step: $0.step) }
            guard ensureEditSurfaces() else { return }
            showMode(.edit)
        case .present:
            if goLive {
                // `--play` and opening straight into a show start at slide 1.
                self.goLive(index: 0, step: 0, origin: origin == .edit ? .decks : origin)
            } else {
                // Decks and the CLI open Present ready at the first slide.
                enterPresentReady(origin: .decks, index: 0, step: 0)
            }
        }
    }

    func beginDeckSession(_ url: URL, content: String) {
        currentDeckURL = url
        fileContent = content
        lastPersistedContent = content
        sectionInfos = extractSections(from: content)
        deckTitle = deckDisplayTitle(content: content, deckURL: url)
        deckWrittenThisSession = false
        isEndingPresentation = false
        talkTimer.reset()
        talkTimerStartedThisSession = false
        blackoutActive = false
        saveStatus = .saved
        deckLibrary.recordOpened(url)
        scheduleDecksRender()
        startFileWatcher()
        updateWindowTitle()
    }

    // Views and windows torn down in the last round (a show ending, the
    // ready console closing, a deck session ending) are kept until the next
    // round, so in-flight WebKit callbacks never reach a freed object;
    // earlier rounds are released here.
    func beginTeardownRound() {
        staleWindows.removeAll()
        staleWebViews.removeAll()
    }

    // A torn-down page stops running (the console's clock, a deck's timers).
    func retireWebView(_ webView: WKWebView) {
        webView.stopLoading()
        webView.navigationDelegate = nil
        if let blank = URL(string: "about:blank") { webView.load(URLRequest(url: blank)) }
        staleWebViews.append(webView)
    }

    // Ends the deck session (deck switch, window close). The debounced write
    // is flushed first; if it fails nothing is torn down.
    @discardableResult
    func cleanupPresentation() -> Bool {
        NSLog("Presto: cleanupPresentation")
        guard flushPendingDeckWrite() else { return false }
        cancelEditorMirror()
        sessionGeneration &+= 1

        stopFileWatcher()
        lastPresentationExitEscape = nil

        beginTeardownRound()
        teardownAudience()
        teardownConsole()
        presentPhase = .none
        oneScreenExitPending = false
        oneScreenFullScreenPending = false
        mainWindow?.toolbar?.isVisible = true

        previewWebView?.configuration.userContentController.removeScriptMessageHandler(forName: "presto")
        editorWebView?.configuration.userContentController.removeScriptMessageHandler(forName: "editor")
        for webView in [previewWebView, editorWebView].compactMap({ $0 }) {
            retireWebView(webView)
        }
        editorSplitView?.removeFromSuperview()

        previewWebView = nil
        editorWebView = nil
        editorSplitView = nil
        editorSidebarVisible = false
        isEditorMode = false
        wysiwygTextEditingActive = false
        wysiwygHasSelection = false
        refineTextEditing = false
        editUndoState = EditUndoState()
        editHistoryAvailable = false
        editChromeAvailable = false
        selectionKind = nil
        editInjectionProblem = nil
        deckOutline = DeckOutline()

        presenterReady = false
        editorReady = false
        editorRecoveryNeedsWarning = false
        currentEditorSlideIndex = 0
        currentEditorStep = 0
        lastAudienceSlideIndex = 0
        lastAudienceStep = 0
        lastAudienceReturnStack = []
        webContentRecoveryAttempts = [:]
        simulatedWebContentTerminationRoles = []
        saveTransactionCount = 0
        fileContent = nil
        lastPersistedContent = nil
        lastPrestoWrittenContent = nil
        isPresentingExternalConflict = false
        externalMergeDeferred = false
        sourceEditorDirty = false
        assetReloadPending = false
        sectionInfos = []
        saveStatus = .saved

        refineQueues = [:]
        refineRunningInfo = [:]
        refineErrors = [:]
        pendingEditorSaveID = nil
        pendingWysiwygSaveID = nil
        return true
    }

    @objc func openDeckFromMenu(_ sender: Any?) {
        openDeckFileInMode(editMode: true)
    }

    // File > Open… always opens in Edit, at the deck's last position.
    @objc func openDeckFileInMode(editMode: Bool) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.html, .folder]
        panel.message = "Choose a WebSlides deck (its index.html or its folder)"
        panel.prompt = "Open"
        guard panel.runModal() == .OK, let picked = panel.url else { return }
        guard let deck = deckFile(for: picked) else {
            NSLog("Presto: %@ is not a WebSlides deck", picked.path)
            NSSound.beep()
            return
        }
        DispatchQueue.main.async {
            self.openDeck(deck, in: editMode ? .edit : .present)
        }
    }

    @objc func addFolderToDecks(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "Choose a folder of decks to show in Decks"
        panel.prompt = "Add Folder"
        guard panel.runModal() == .OK, let folder = panel.url else { return }
        deckLibrary.addFolder(folder)
        scheduleDecksRender()
        if mode != .decks && presentPhase != .live { requestMode(.decks) }
    }

    // MARK: - Edit Surfaces

    // Creates the preview (the deck with the visual editor) and the inspector
    // once per deck session. They then live until the deck closes, presenting
    // included.
    @discardableResult
    func ensureEditSurfaces() -> Bool {
        if isEditorMode { return true }
        guard let deckURL = currentDeckURL else { return false }
        let resourceRoots = resolveDeckResourceRoots(for: deckURL)
        let editorHTML = resourceRoots.webSlidesRoot.appendingPathComponent("presto/editor.html")

        isEditorMode = true
        wysiwygTextEditingActive = false
        wysiwygHasSelection = false
        editorReady = false
        editUndoState = EditUndoState()
        editHistoryAvailable = false
        editChromeAvailable = false
        selectionKind = nil
        editInjectionProblem = nil
        if let position = pendingEditPosition {
            currentEditorSlideIndex = position.index
            currentEditorStep = position.step
            pendingEditPosition = nil
        }
        clampCurrentEditorSlideIndex()
        isReloadingPreview = false
        refineErrors = [:]
        railVisible = UserDefaults.standard.object(forKey: "editRailVisible") as? Bool ?? true
        ghostFutureSteps = UserDefaults.standard.object(forKey: "editGhostFutureSteps") as? Bool ?? true

        let previewConfig = makeBaseConfig()
        previewConfig.userContentController.add(self, name: "presto")
        let previewWV = makePrestoWebView(configuration: previewConfig)
        previewWebView = previewWV

        let editorConfig = makeBaseConfig()
        editorConfig.userContentController.add(self, name: "editor")
        let editorWV = makePrestoWebView(configuration: editorConfig)
        editorWebView = editorWV

        // The visual editor is primary; the inspector (source and Refine) is
        // a 360 px pane on the right, hidden by default.
        let splitView = NSSplitView(frame: mainWindow?.contentView?.bounds ?? NSRect(x: 0, y: 0, width: 1200, height: 800))
        splitView.autoresizingMask = [.width, .height]
        splitView.isVertical = true
        splitView.dividerStyle = .thin
        splitView.addArrangedSubview(previewWV)
        splitView.addArrangedSubview(editorWV)
        splitView.setHoldingPriority(.defaultHigh, forSubviewAt: 1)
        splitView.delegate = self
        editorWV.isHidden = true
        editorSidebarVisible = false
        editorSplitView = splitView
        collapseInspector(in: splitView)

        NSLog("Presto: loading deck for preview")
        previewWV.loadFileURL(deckURL, allowingReadAccessTo: resourceRoots.readAccessRoot)
        NSLog("Presto: loading editor HTML")
        editorWV.loadFileURL(editorHTML, allowingReadAccessTo: resourceRoots.readAccessRoot)
        NSLog("Presto: editor mode started")
        return true
    }

    func makePrestoWebView(configuration: WKWebViewConfiguration,
                           frame: NSRect = .zero) -> PrestoWebView {
        let webView = PrestoWebView(frame: frame, configuration: configuration)
        webView.navigationDelegate = self
        webView.dropHandler = self
        #if PRESTO_TESTING
        let inspectable = true
        #else
        let inspectable = ProcessInfo.processInfo.environment["PRESTO_WEBVIEW_INSPECTABLE"] == "1"
        #endif
        if #available(macOS 13.3, *) { webView.isInspectable = inspectable }
        return webView
    }

    func webViewDropActiveChanged(_ webView: PrestoWebView, active: Bool) {
        guard webView === decksWebView else { return }
        webView.evaluateJavaScript(
            "window.PrestoDecks && typeof PrestoDecks.setDropActive === 'function' && PrestoDecks.setDropActive(\(active ? "true" : "false"))"
        ) { _, _ in }
    }

    func updateEditorSourceToggle() {
        guard isEditorMode, let previewWV = previewWebView else { return }
        let visible = editorSidebarVisible ? "true" : "false"
        let js = """
        if (window.__prestoWysiwyg &&
            typeof window.__prestoWysiwyg.setSourceSidebarVisible === 'function') {
            window.__prestoWysiwyg.setSourceSidebarVisible(\(visible));
        }
        """
        previewWV.evaluateJavaScript(js) { _, _ in }
    }

    func setEditorSourceSidebarVisible(_ visible: Bool, focusSource: Bool = true) {
        guard isEditorMode,
              let splitView = editorSplitView,
              let editorWV = editorWebView else { return }

        editorSidebarVisible = visible
        editorWV.isHidden = !visible
        if visible {
            splitView.adjustSubviews()
        } else {
            collapseInspector(in: splitView)
        }

        if visible {
            let dividerPosition = max(0, splitView.bounds.width - inspectorWidth)
            splitView.setPosition(dividerPosition, ofDividerAt: 0)
            if focusSource {
                mainWindow?.makeFirstResponder(editorWV)
                editorWV.evaluateJavaScript("window.focusEditor && window.focusEditor()") { _, _ in }
            }
        } else {
            mainWindow?.makeFirstResponder(previewWebView)
        }

        updateEditorSourceToggle()
        applyEditLayout()
        updateToolbarState()
        NSLog("Presto: source sidebar visible=%d", visible ? 1 : 0)
    }

    // ⇧⌘X and the inspector toggle. Opening finishes any visual text session
    // first; closing commits the source buffer, so neither surface can hold
    // an edit the other has not seen.
    func toggleEditorSourceSidebar() {
        guard isEditorMode else { return }
        if !editorSidebarVisible {
            showInspector(focusSource: true)
        } else {
            hideInspector()
        }
    }

    func showInspector(focusSource: Bool, then: (() -> Void)? = nil) {
        guard isEditorMode else { return }
        guard !editorSidebarVisible else {
            if focusSource, let editorWV = editorWebView {
                mainWindow?.makeFirstResponder(editorWV)
                editorWV.evaluateJavaScript("window.focusEditor && window.focusEditor()") { _, _ in }
            }
            then?()
            return
        }
        // Finish any visual text session before exposing its source so the
        // sidebar never starts with an older copy of the slide.
        commitActiveWysiwygSession { [weak self] in
            guard let self else { return }
            self.flushEditorMirror()
            self.setEditorSourceSidebarVisible(true, focusSource: focusSource)
            then?()
        }
    }

    func hideInspector() {
        guard isEditorMode, editorSidebarVisible else { return }
        guard let editorWV = editorWebView else {
            setEditorSourceSidebarVisible(false)
            return
        }
        let generation = sessionGeneration
        editorWV.evaluateJavaScript(pendingEditorChangeJS()) { [weak self, weak editorWV] result, error in
            guard let self, let editorWV,
                  self.sessionGeneration == generation,
                  self.editorWebView === editorWV else { return }
            if let error {
                self.reportEditorSaveFailed("Cannot close HTML source: \(error.localizedDescription)")
                return
            }
            let pending = self.applyPendingEditorChangePayload(result)
            guard pending.ok else { return }
            if let savedIndex = pending.savedIndex {
                self.refreshPreviewSlide(savedIndex, recording: pending.change)
            }
            guard self.flushPendingDeckWrite() else { return }
            self.setEditorSourceSidebarVisible(false)
        }
    }

    // The inspector is 360 px by default; dragging its edge resizes it
    // between 300 and 640 px, and the width is kept.
    static let inspectorWidthRange: ClosedRange<CGFloat> = 300...640

    var inspectorWidth: CGFloat {
        let saved = CGFloat(UserDefaults.standard.double(forKey: "inspectorWidth"))
        return AppDelegate.inspectorWidthRange.contains(saved) ? saved : 360
    }

    // The width limits apply while the inspector is shown; hidden, it
    // collapses to nothing (setPosition also asks these).
    func splitView(_ splitView: NSSplitView, constrainMinCoordinate proposedMinimumPosition: CGFloat,
                   ofSubviewAt dividerIndex: Int) -> CGFloat {
        guard splitView === editorSplitView, editorSidebarVisible else { return proposedMinimumPosition }
        return max(proposedMinimumPosition, splitView.bounds.width - AppDelegate.inspectorWidthRange.upperBound)
    }

    func splitView(_ splitView: NSSplitView, constrainMaxCoordinate proposedMaximumPosition: CGFloat,
                   ofSubviewAt dividerIndex: Int) -> CGFloat {
        guard splitView === editorSplitView, editorSidebarVisible else { return proposedMaximumPosition }
        return min(proposedMaximumPosition, splitView.bounds.width - AppDelegate.inspectorWidthRange.lowerBound)
    }

    // A hidden inspector takes no space and leaves no divider: the canvas
    // runs to the window's edge, with no hairline at the inspector's old
    // edge. setPosition un-hides a collapsed pane (leaving it 0 wide with
    // its divider drawn), so the pane is hidden after the divider moves.
    func collapseInspector(in splitView: NSSplitView) {
        if splitView.subviews.count > 1 { splitView.setPosition(splitView.bounds.width, ofDividerAt: 0) }
        editorWebView?.isHidden = true
        splitView.adjustSubviews()
    }

    func splitView(_ splitView: NSSplitView, shouldHideDividerAt dividerIndex: Int) -> Bool {
        splitView === editorSplitView && !editorSidebarVisible
    }

    func splitView(_ splitView: NSSplitView, shouldAdjustSizeOfSubview view: NSView) -> Bool {
        // Window resizes go to the slide canvas; the inspector keeps its width.
        !(splitView === editorSplitView && view === editorWebView && editorSidebarVisible)
    }

    func splitViewDidResizeSubviews(_ notification: Notification) {
        guard (notification.object as? NSSplitView) === editorSplitView, editorSidebarVisible,
              let inspector = editorWebView, !inspector.isHidden,
              AppDelegate.inspectorWidthRange.contains(inspector.frame.width) else { return }
        UserDefaults.standard.set(Double(inspector.frame.width), forKey: "inspectorWidth")
    }

    func setRailVisible(_ visible: Bool) {
        railVisible = visible
        UserDefaults.standard.set(visible, forKey: "editRailVisible")
        previewWebView?.evaluateJavaScript(
            "window.PrestoEditChrome && typeof PrestoEditChrome.setRailVisible === 'function' && PrestoEditChrome.setRailVisible(\(visible ? "true" : "false"))"
        ) { _, _ in }
        applyEditLayout()
        updateToolbarState()
    }

    func setGhostFutureSteps(_ on: Bool, fromPage: Bool = false) {
        ghostFutureSteps = on
        UserDefaults.standard.set(on, forKey: "editGhostFutureSteps")
        if !fromPage {
            previewWebView?.evaluateJavaScript(
                "window.PrestoEditChrome && typeof PrestoEditChrome.setGhostFuture === 'function' && PrestoEditChrome.setGhostFuture(\(on ? "true" : "false"))"
            ) { _, _ in }
        }
    }

    // MARK: - Status

    func setSaveStatus(_ status: SaveStatus) {
        guard saveStatus != status else { return }
        saveStatus = status
        updateToolbarTitle()
    }

    // MARK: - Toolbar

    static let toolbarMode = NSToolbarItem.Identifier("presto.mode")
    static let toolbarRail = NSToolbarItem.Identifier("presto.rail")
    static let toolbarSearch = NSToolbarItem.Identifier("presto.search")
    static let toolbarOpen = NSToolbarItem.Identifier("presto.open")
    static let toolbarTitle = NSToolbarItem.Identifier("presto.title")
    static let toolbarUndo = NSToolbarItem.Identifier("presto.undo")
    static let toolbarRedo = NSToolbarItem.Identifier("presto.redo")
    static let toolbarTextBox = NSToolbarItem.Identifier("presto.textbox")
    static let toolbarExport = NSToolbarItem.Identifier("presto.export")
    static let toolbarInspector = NSToolbarItem.Identifier("presto.inspector")
    static let toolbarPlay = NSToolbarItem.Identifier("presto.play")
    static let toolbarStatus = NSToolbarItem.Identifier("presto.status")
    static let toolbarSwap = NSToolbarItem.Identifier("presto.swap")
    static let toolbarBlackout = NSToolbarItem.Identifier("presto.blackout")
    static let toolbarEnd = NSToolbarItem.Identifier("presto.end")

    static func toolbarIdentifiers(for mode: PrestoMode) -> [NSToolbarItem.Identifier] {
        switch mode {
        case .decks:
            return [toolbarMode, .flexibleSpace, toolbarSearch, toolbarOpen]
        case .edit:
            return [toolbarRail, toolbarMode, .flexibleSpace, toolbarTitle, .flexibleSpace,
                    toolbarUndo, toolbarRedo, .space, toolbarTextBox, .space,
                    toolbarExport, toolbarInspector, toolbarPlay]
        case .present:
            return [toolbarMode, .flexibleSpace, toolbarStatus, .flexibleSpace,
                    toolbarSwap, toolbarBlackout, toolbarEnd]
        }
    }

    func installToolbar(for mode: PrestoMode) {
        guard let window = mainWindow else { return }
        let identifier = "PrestoToolbar." + AppDelegate.modeName(mode)
        if window.toolbar?.identifier != identifier {
            toolbarItems = [:]
            let toolbar = NSToolbar(identifier: identifier)
            toolbar.delegate = self
            toolbar.displayMode = .iconOnly
            toolbar.allowsUserCustomization = false
            toolbar.autosavesConfiguration = false
            if mode == .edit { toolbar.centeredItemIdentifier = AppDelegate.toolbarTitle }
            if mode == .present { toolbar.centeredItemIdentifier = AppDelegate.toolbarStatus }
            window.toolbar = toolbar
        }
        // One-screen live: the window shows only the deck.
        window.toolbar?.isVisible = !(presentPhase == .live && audienceOneScreen)
        updateToolbarState()
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        AppDelegate.toolbarIdentifiers(for: mode)
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        AppDelegate.toolbarIdentifiers(for: mode)
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier itemIdentifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        let item: NSToolbarItem
        switch itemIdentifier {
        case AppDelegate.toolbarMode:
            item = NSToolbarItem(itemIdentifier: itemIdentifier)
            let control = NSSegmentedControl(labels: ["Decks", "Edit", "Present"], trackingMode: .selectOne,
                                             target: self, action: #selector(modeControlChanged(_:)))
            for (segment, symbol) in ["square.grid.2x2", "pencil", "play"].enumerated() {
                control.setImage(NSImage(systemSymbolName: symbol, accessibilityDescription: nil), forSegment: segment)
                control.setImageScaling(.scaleProportionallyDown, forSegment: segment)
            }
            control.setToolTip("Decks (⌘1)", forSegment: 0)
            control.setToolTip("Edit (⌘2)", forSegment: 1)
            control.setToolTip("Present (⌘3)", forSegment: 2)
            item.view = control
            item.label = "Mode"
        case AppDelegate.toolbarRail:
            item = symbolItem(itemIdentifier, symbol: "sidebar.left", label: "Slide Rail",
                              tip: "Show or hide the slide rail", action: #selector(toggleSlideRail(_:)))
        case AppDelegate.toolbarSearch:
            let search = NSSearchToolbarItem(itemIdentifier: itemIdentifier)
            search.searchField.placeholderString = "Search decks and slides"
            search.searchField.delegate = self
            search.searchField.sendsSearchStringImmediately = true
            search.preferredWidthForSearchField = 230
            item = search
        case AppDelegate.toolbarOpen:
            item = buttonItem(itemIdentifier, title: "Open…", shortcut: "⌘O", symbol: "folder",
                              bordered: true, action: #selector(openDeckFromMenu(_:)))
        case AppDelegate.toolbarTitle:
            item = NSToolbarItem(itemIdentifier: itemIdentifier)
            let label = NSTextField(labelWithString: "")
            label.lineBreakMode = .byTruncatingMiddle
            label.alignment = .center
            item.view = label
            item.label = "Deck"
        case AppDelegate.toolbarUndo:
            item = symbolItem(itemIdentifier, symbol: "arrow.uturn.backward", label: "Undo",
                              tip: "Undo (⌘Z)", action: #selector(undoEdit(_:)))
        case AppDelegate.toolbarRedo:
            item = symbolItem(itemIdentifier, symbol: "arrow.uturn.forward", label: "Redo",
                              tip: "Redo (⇧⌘Z)", action: #selector(redoEdit(_:)))
        case AppDelegate.toolbarTextBox:
            item = buttonItem(itemIdentifier, title: "Text box", shortcut: nil, symbol: "textformat",
                              bordered: false, action: #selector(addTextBox(_:)))
            item.toolTip = "Add a text box (⌥⌘T, or double-click empty space)"
        case AppDelegate.toolbarExport:
            item = symbolItem(itemIdentifier, symbol: "square.and.arrow.down", label: "Export PDF",
                              tip: "Export PDF… (⌘E)", action: #selector(exportPDF(_:)))
        case AppDelegate.toolbarInspector:
            item = symbolItem(itemIdentifier, symbol: "sidebar.right", label: "Inspector",
                              tip: "Show or hide the inspector (⌥⌘I)", action: #selector(toggleInspector(_:)))
        case AppDelegate.toolbarPlay:
            item = NSToolbarItem(itemIdentifier: itemIdentifier)
            let button = PrestoPrimaryButton(title: "", target: self, action: #selector(playFromCurrentSlide(_:)))
            button.image = NSImage(systemSymbolName: "play.fill", accessibilityDescription: nil)
            button.imagePosition = .imageLeading
            button.contentTintColor = .white
            button.attributedTitle = AppDelegate.buttonTitle("Play from here", shortcut: "⌘↩",
                                                             color: .white, shortcutColor: NSColor(white: 1, alpha: 0.75))
            button.toolTip = "Play from this slide (⌘↩); ⇧⌘↩ plays from the start"
            item.view = button
            item.label = "Play"
        case AppDelegate.toolbarStatus:
            item = NSToolbarItem(itemIdentifier: itemIdentifier)
            let label = NSTextField(labelWithString: "")
            label.lineBreakMode = .byTruncatingTail
            label.alignment = .center
            item.view = label
            item.label = "Status"
        case AppDelegate.toolbarSwap:
            item = buttonItem(itemIdentifier, title: "Swap screens", shortcut: nil,
                              symbol: "arrow.left.arrow.right", bordered: false,
                              action: #selector(swapDisplays(_:)))
        case AppDelegate.toolbarBlackout:
            item = buttonItem(itemIdentifier, title: "Black out", shortcut: "B", symbol: "display",
                              bordered: false, action: #selector(toggleBlackScreen(_:)))
        case AppDelegate.toolbarEnd:
            item = NSToolbarItem(itemIdentifier: itemIdentifier)
            let button = NSButton(title: "", target: self, action: #selector(endFromToolbar(_:)))
            button.bezelStyle = .rounded
            item.view = button
            item.label = "End"
        default:
            return nil
        }
        item.autovalidates = false
        toolbarItems[itemIdentifier] = item
        return item
    }

    func symbolItem(_ identifier: NSToolbarItem.Identifier, symbol: String, label: String,
                    tip: String, action: Selector) -> NSToolbarItem {
        let item = NSToolbarItem(itemIdentifier: identifier)
        item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: label)
        item.label = label
        item.toolTip = tip
        item.target = self
        item.action = action
        item.isBordered = true
        return item
    }

    func buttonItem(_ identifier: NSToolbarItem.Identifier, title: String, shortcut: String?,
                    symbol: String, bordered: Bool, action: Selector) -> NSToolbarItem {
        let item = NSToolbarItem(itemIdentifier: identifier)
        let button = NSButton(title: "", target: self, action: action)
        if bordered {
            button.bezelStyle = .rounded
        } else {
            button.isBordered = false
        }
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        button.imagePosition = .imageLeading
        button.contentTintColor = .prestoInk2
        button.attributedTitle = AppDelegate.buttonTitle(title, shortcut: shortcut, color: .prestoInk,
                                                         shortcutColor: .prestoDim)
        item.view = button
        item.label = title
        return item
    }

    static func buttonTitle(_ title: String, shortcut: String?, color: NSColor,
                            shortcutColor: NSColor) -> NSAttributedString {
        let font = NSFont.systemFont(ofSize: 13, weight: .regular)
        let result = NSMutableAttributedString(string: " " + title,
                                               attributes: [.font: font, .foregroundColor: color])
        if let shortcut {
            result.append(NSAttributedString(string: "  " + shortcut, attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .regular), .foregroundColor: shortcutColor
            ]))
        }
        return result
    }

    @objc func modeControlChanged(_ sender: NSSegmentedControl) {
        guard let target = PrestoMode(rawValue: sender.selectedSegment) else { return }
        requestMode(target)
        updateToolbarState()
    }

    func updateToolbarState() {
        guard mainWindow != nil else { return }
        if let control = toolbarItems[AppDelegate.toolbarMode]?.view as? NSSegmentedControl {
            control.selectedSegment = mode.rawValue
            control.setEnabled(modeEnabled(.decks) || mode == .decks, forSegment: 0)
            control.setEnabled(modeEnabled(.edit) || mode == .edit, forSegment: 1)
            control.setEnabled(modeEnabled(.present), forSegment: 2)
        }
        let editing = mode == .edit && isEditorMode
        toolbarItems[AppDelegate.toolbarRail]?.isEnabled = editing
        toolbarItems[AppDelegate.toolbarUndo]?.isEnabled = editing && editUndoState.canUndo
        toolbarItems[AppDelegate.toolbarRedo]?.isEnabled = editing && editUndoState.canRedo
        toolbarItems[AppDelegate.toolbarUndo]?.toolTip = editUndoState.undoTitle + " (⌘Z)"
        toolbarItems[AppDelegate.toolbarRedo]?.toolTip = editUndoState.redoTitle + " (⇧⌘Z)"
        (toolbarItems[AppDelegate.toolbarTextBox]?.view as? NSButton)?.isEnabled = editing && editHistoryAvailable
        toolbarItems[AppDelegate.toolbarExport]?.isEnabled = currentDeckURL != nil && !exportRequestActive
        toolbarItems[AppDelegate.toolbarInspector]?.isEnabled = editing
        (toolbarItems[AppDelegate.toolbarPlay]?.view as? NSButton)?.isEnabled = currentDeckURL != nil && presentPhase != .live
        let live = presentPhase == .live
        (toolbarItems[AppDelegate.toolbarSwap]?.view as? NSButton)?.isEnabled = canSwapDisplays
        if let blackout = toolbarItems[AppDelegate.toolbarBlackout]?.view as? NSButton {
            blackout.isEnabled = live
            blackout.attributedTitle = AppDelegate.buttonTitle(blackoutActive ? "Show slide" : "Black out", shortcut: "B",
                                                               color: blackoutActive ? .prestoLive : .prestoInk,
                                                               shortcutColor: .prestoDim)
        }
        if let end = toolbarItems[AppDelegate.toolbarEnd]?.view as? NSButton {
            end.attributedTitle = AppDelegate.buttonTitle(live ? "End" : "Close", shortcut: live ? "Esc twice" : nil,
                                                          color: .prestoLive, shortcutColor: .prestoDim)
            end.isEnabled = presentPhase != .none
        }
        updateToolbarTitle()
        updatePresentStatus()
    }

    // The Edit toolbar's deck title: "Headline · Occasion" when the deck's
    // <title> carries an occasion (" - TCD 2026", " (BREAD 2026)"), else the
    // headline, split the way Decks splits it. The window title is unchanged.
    static func toolbarDeckTitle(_ title: String) -> String {
        let split = DeckLibrary.splitTitle(title)
        guard let occasion = split.occasion else { return split.headline }
        return split.headline + " · " + occasion
    }

    func updateToolbarTitle() {
        guard let label = toolbarItems[AppDelegate.toolbarTitle]?.view as? NSTextField else { return }
        let shown = deckTitle.isEmpty ? "Presto" : AppDelegate.toolbarDeckTitle(deckTitle)
        let title = NSMutableAttributedString(string: shown, attributes: [
            .font: NSFont(name: "NewYorkMedium-Semibold", size: 15) ?? NSFont(name: "Georgia-Bold", size: 15)
                ?? NSFont.boldSystemFont(ofSize: 15),
            .foregroundColor: NSColor.prestoInk
        ])
        let small = NSFont.systemFont(ofSize: 12)
        if let problem = editInjectionProblem {
            title.append(NSAttributedString(string: "   ⚠︎ " + problem,
                                            attributes: [.font: small, .foregroundColor: NSColor.prestoLive]))
        } else {
            switch saveStatus {
            case .saved:
                title.append(NSAttributedString(string: "   ✓ Saved",
                                                attributes: [.font: small, .foregroundColor: NSColor.prestoOK]))
            case .saving:
                title.append(NSAttributedString(string: "   Saving…",
                                                attributes: [.font: small, .foregroundColor: NSColor.prestoDim]))
            case .failed(let reason):
                title.append(NSAttributedString(string: "   Not saved: " + reason,
                                                attributes: [.font: small, .foregroundColor: NSColor.prestoLive]))
            }
        }
        label.attributedStringValue = title
        label.toolTip = editInjectionProblem ?? {
            if case .failed(let reason) = saveStatus { return "Not saved: \(reason)" }
            return nil
        }()
        label.sizeToFit()
        label.frame.size.width = min(max(label.frame.width, 200), 640)
    }

    // MARK: - Search

    func controlTextDidChange(_ obj: Notification) {
        guard let field = obj.object as? NSSearchField else { return }
        filterDecks(field.stringValue)
    }

    func filterDecks(_ query: String) {
        decksWebView?.evaluateJavaScript(
            "window.PrestoDecks && typeof PrestoDecks.filter === 'function' && PrestoDecks.filter(\(jsStringLiteral(query)))"
        ) { _, _ in }
    }

    @objc func focusDeckSearch(_ sender: Any?) {
        if mode != .decks { requestMode(.decks) }
        DispatchQueue.main.async {
            (self.toolbarItems[AppDelegate.toolbarSearch] as? NSSearchToolbarItem)?.beginSearchInteraction()
        }
    }

    // MARK: - Decks Mode

    var webSlidesRootForApp: URL {
        if let override = ProcessInfo.processInfo.environment["PRESTO_WEBSLIDES_ROOT"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        if let deckURL = currentDeckURL { return resolveDeckResourceRoots(for: deckURL).webSlidesRoot }
        return prestoDefaultWebSlidesRoot()
    }

    @objc func ensureDecksWebView() -> WKWebView {
        if let existing = decksWebView { return existing }
        let config = makeBaseConfig()
        config.userContentController.add(self, name: "presto")
        let webView = makePrestoWebView(configuration: config)
        decksWebView = webView
        decksReady = false
        let root = webSlidesRootForApp
        let page = root.appendingPathComponent("presto/decks.html")
        if FileManager.default.isReadableFile(atPath: page.path) {
            webView.loadFileURL(page, allowingReadAccessTo: root)
        } else {
            NSLog("Presto: Decks page missing at %@", page.path)
            webView.loadHTMLString(AppDelegate.missingPageHTML(
                title: "Decks are unavailable",
                detail: "Presto could not read \(page.path). Use File > Open… (⌘O) to open a deck."), baseURL: nil)
        }
        return webView
    }

    static func missingPageHTML(title: String, detail: String) -> String {
        func escape(_ s: String) -> String {
            s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        }
        return """
        <!doctype html><html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;\
        background:#E7EAEE;font:13px -apple-system,sans-serif;color:#1C2532">\
        <div style="background:#fff;border-radius:12px;padding:24px 28px;max-width:460px;\
        box-shadow:0 0 0 .5px rgba(0,0,0,.08),0 1px 3px rgba(0,0,0,.05)">\
        <div style="font-weight:600;font-size:15px;margin-bottom:6px">\(escape(title))</div>\
        <div style="color:#485263;line-height:1.45">\(escape(detail))</div></div></body></html>
        """
    }

    func scheduleDecksRender() {
        decksRenderPending?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.decksRenderPending = nil
            self.renderDecks()
        }
        decksRenderPending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: work)
    }

    func renderDecks() {
        guard decksReady, let webView = decksWebView else { return }
        let json = deckLibrary.modelJSON()
        webView.evaluateJavaScript("window.PrestoDecks && typeof PrestoDecks.render === 'function' && PrestoDecks.render(\(json))") { _, error in
            if let error { NSLog("Presto: Decks render failed: %@", error.localizedDescription) }
        }
    }

    func handleDecksMessage(type: String, body: [String: Any]) {
        let path = (body["path"] as? String).map { URL(fileURLWithPath: $0) }
        switch type {
        case "decksReady":
            decksReady = true
            renderDecks()
        case "decksOpen":
            guard let path else { return }
            openDeck(path, in: (body["mode"] as? String) == "present" ? .present : .edit)
        case "decksOpenPanel":
            openDeckFileInMode(editMode: true)
        case "decksAddFolder":
            addFolderToDecks(nil)
        case "decksRemoveFolder":
            guard let path else { return }
            deckLibrary.removeFolder(path)
            scheduleDecksRender()
        case "decksReveal":
            guard let path else { return }
            NSWorkspace.shared.activateFileViewerSelecting([path])
        case "decksPin":
            guard let path else { return }
            deckLibrary.setPinned(path, (body["pinned"] as? Bool) ?? true)
            scheduleDecksRender()
        case "decksRemoveRecent":
            guard let path else { return }
            deckLibrary.removeRecent(path)
            scheduleDecksRender()
        case "decksNeedThumbnail":
            guard let path else { return }
            enqueueThumbnail(path, slide: 0, step: 0, position: false)
        default:
            break
        }
    }

    // Recent decks for File > Open Recent, newest first.
    func recentDeckURLs(limit: Int = 12) -> [(url: URL, title: String)] {
        let model = deckLibrary.model()
        return model.decks.filter { $0.inRecent }
            .sorted { ($0.lastOpened ?? 0) > ($1.lastOpened ?? 0) }
            .prefix(limit)
            .map { (URL(fileURLWithPath: $0.path), $0.title) }
    }

    // MARK: - Thumbnails

    // Slide pictures for Decks, rendered from disk one at a time in a hidden
    // 1280x720 web view (the PDF export's snapshot technique). Paused while live.
    struct ThumbnailJob: Equatable {
        let deck: URL
        let slide: Int
        let step: Int
        let position: Bool

        var failureKey: String { deck.path + (position ? "#position" : "") }
    }

    @objc func enqueueThumbnail(_ deck: URL, slide: Int, step: Int, position: Bool) {
        let job = ThumbnailJob(deck: deck, slide: slide, step: step, position: position)
        guard position || !thumbnailFailures.contains(job.failureKey) else { return }
        thumbnailQueue.removeAll { $0.deck == deck && $0.position == position }
        thumbnailQueue.append(job)
        processThumbnailQueue()
    }

    func processThumbnailQueue() {
        guard thumbnailJob == nil, presentPhase != .live, !thumbnailQueue.isEmpty else { return }
        let job = thumbnailQueue.removeFirst()
        guard FileManager.default.fileExists(atPath: job.deck.path) else {
            processThumbnailQueue()
            return
        }
        thumbnailJob = job
        thumbnailGeneration &+= 1
        let generation = thumbnailGeneration
        let config = makeBaseConfig()
        let webView = makePrestoWebView(configuration: config, frame: NSRect(x: 0, y: 0, width: 1280, height: 720))
        thumbnailWebView = webView
        webView.loadFileURL(job.deck, allowingReadAccessTo: findReadAccessRoot(deckURL: job.deck))
        // A deck that never settles is skipped rather than holding the queue.
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            guard let self, self.thumbnailGeneration == generation, self.thumbnailJob != nil else { return }
            NSLog("Presto: thumbnail timed out for %@", job.deck.path)
            self.finishThumbnail(png: nil)
        }
    }

    // A talk gets the machine to itself: an unfinished picture waits.
    func pauseThumbnailRenderer() {
        guard let job = thumbnailJob else { return }
        thumbnailQueue.insert(job, at: 0)
        stopThumbnailRenderer()
    }

    // Ends the current job. The renderer is kept one more turn: it may be
    // inside a WebKit callback right now.
    func stopThumbnailRenderer() {
        thumbnailJob = nil
        thumbnailGeneration &+= 1
        thumbnailWebView?.stopLoading()
        thumbnailWebView?.navigationDelegate = nil
        retiredThumbnailWebView = thumbnailWebView
        thumbnailWebView = nil
    }

    func thumbnailPageLoaded(_ webView: WKWebView, attempt: Int = 0) {
        guard webView === thumbnailWebView, let job = thumbnailJob else { return }
        let generation = thumbnailGeneration
        webView.evaluateJavaScript(AppDelegate.deckReadyCheckJS) { [weak self, weak webView] result, error in
            guard let self, let webView, self.thumbnailGeneration == generation else { return }
            if error != nil || attempt > 150 { return self.finishThumbnail(png: nil) }
            guard result as? Bool == true else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                    self.thumbnailPageLoaded(webView, attempt: attempt + 1)
                }
                return
            }
            let navigate = """
            (function () {
                WS.mode = 'audience';
                document.body.classList.add('ws-mode-audience');
                var index = Math.max(0, Math.min(\(job.slide), WS.slides.length - 1));
                WS.goToSlide(index, \(job.step));
                return true;
            })()
            """
            webView.evaluateJavaScript(navigate) { [weak self, weak webView] _, _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                    guard let self, let webView, self.thumbnailGeneration == generation else { return }
                    let config = WKSnapshotConfiguration()
                    config.snapshotWidth = 640 as NSNumber
                    webView.takeSnapshot(with: config) { [weak self] image, _ in
                        guard let self, self.thumbnailGeneration == generation else { return }
                        self.finishThumbnail(png: image.flatMap(AppDelegate.pngData))
                    }
                }
            }
        }
    }

    static func pngData(_ image: NSImage) -> Data? {
        guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff) else { return nil }
        return bitmap.representation(using: .png, properties: [:])
    }

    func finishThumbnail(png: Data?) {
        guard let job = thumbnailJob else { return }
        stopThumbnailRenderer()
        if let png {
            let saved = job.position
                ? deckLibrary.savePositionThumbnail(png, for: job.deck, slide: job.slide, step: job.step)
                : deckLibrary.saveThumbnail(png, for: job.deck)
            if saved { scheduleDecksRender() } else { thumbnailFailures.insert(job.failureKey) }
        } else {
            thumbnailFailures.insert(job.failureKey)
        }
        DispatchQueue.main.async { [weak self] in
            self?.processThumbnailQueue()
        }
    }

    // The deck's first slide, once it is open in Edit and settled, if the
    // saved picture is missing or older than the deck.
    func captureDeckThumbnailIfNeeded() {
        guard let deckURL = currentDeckURL, deckLibrary.thumbnailURL(deckURL) == nil else { return }
        enqueueThumbnail(deckURL, slide: 0, step: 0, position: false)
    }

    // MARK: - Closing The Window

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard sender === mainWindow else { return true }
        // ⌘W during a talk must not end it: End or Escape twice does that.
        guard presentPhase != .live else {
            NSSound.beep()
            showPresentationHint("End the presentation first (Esc twice or ⌘.)")
            return false
        }
        closeMainWindow()
        return false
    }

    // Closing keeps Presto running (the Dock icon brings back Decks). The
    // deck's edits are saved first; a failed save keeps everything open.
    func closeMainWindow() {
        let finish: () -> Void = { [weak self] in
            guard let self else { return }
            if self.presentPhase == .ready { self.leavePresentReady() }
            if self.currentDeckURL != nil {
                if self.isEditorMode {
                    self.recordDeckPosition(index: self.currentEditorSlideIndex, step: self.currentEditorStep)
                }
                if self.deckWrittenThisSession, let deck = self.currentDeckURL {
                    self.enqueueThumbnail(deck, slide: 0, step: 0, position: false)
                }
                guard self.cleanupPresentation() else { return }
                self.currentDeckURL = nil
                self.deckTitle = ""
            }
            self.showMode(.decks)
            self.mainWindow?.orderOut(nil)
            NSLog("Presto: main window closed")
        }
        if isEditorMode {
            flushActiveEditor { saved in if saved { finish() } }
        } else {
            finish()
        }
    }

    func windowWillClose(_ notification: Notification) {
        guard let window = notification.object as? NSWindow else { return }
        if window === audienceWindow && presentPhase == .live && !isEndingPresentation {
            NSLog("Presto: audience window closed by user")
            requestEndPresentation()
        }
    }

    func windowDidResignKey(_ notification: Notification) {
        // When the main window loses focus to another app, commit any live
        // WYSIWYG text session. Moving between its two embedded web views is
        // covered by the preview's JavaScript blur handler.
        guard isEditorMode, wysiwygTextEditingActive,
              let window = notification.object as? NSWindow,
              window === mainWindow else { return }
        commitActiveWysiwygSession { }
    }

    func windowDidResize(_ notification: Notification) {
        guard (notification.object as? NSWindow) === mainWindow, mode == .edit else { return }
        applyEditLayout()
    }
}

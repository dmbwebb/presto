import Cocoa
import WebKit

// Present mode: the presenter console in the main window (ready, then live),
// the audience window on a second display, the one-screen show in the main
// window's native full screen, and the talk timer, black screen and display
// handling that go with them.

// The projector's window: borderless at the display's full frame, above
// its menu bar and Dock. It never becomes key, so keys, alerts and sheets
// stay with the main window on the laptop.
final class PrestoAudienceWindow: NSWindow {
    var allowsKey = false
    override var canBecomeKey: Bool { allowsKey }
    override var canBecomeMain: Bool { allowsKey }
    // AppKit keeps titled windows below the menu bar; this one must cover it.
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
        styleMask.contains(.titled) ? super.constrainFrameRect(frameRect, to: screen) : frameRect
    }
}

// A deck's slide and section structure, read from the engine (WS).
struct DeckOutline {
    var mainSlideCount = 0
    var totalSlides = 0
    var sections: [[String: Any]] = []
    var slides: [[String: Any]] = []
}

extension AppDelegate {

    static let presentationHintDuration: TimeInterval = 2.0

    // MARK: - Displays

    func currentDisplays() -> [PrestoDisplay] {
        NSScreen.screens.compactMap { screen in
            guard let id = displayID(of: screen) else { return nil }
            return PrestoDisplay(id: id, name: screen.localizedName, frame: screen.frame,
                                 visibleFrame: screen.visibleFrame, isBuiltin: CGDisplayIsBuiltin(id) != 0)
        }
    }

    func displayID(of screen: NSScreen?) -> CGDirectDisplayID? {
        guard let number = screen?.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else {
            return nil
        }
        return CGDirectDisplayID(number.uint32Value)
    }

    func mainWindowDisplayID() -> CGDirectDisplayID? {
        displayID(of: mainWindow?.screen ?? NSScreen.main)
    }

    // The display Duncan last chose for the audience, matched by name
    // (display IDs can change when a cable is replugged).
    func preferredAudienceDisplayID(_ displays: [PrestoDisplay]) -> CGDirectDisplayID? {
        if let id = audienceDisplayID, displays.contains(where: { $0.id == id }) { return id }
        guard let name = UserDefaults.standard.string(forKey: "audienceDisplayName") else { return nil }
        return displays.first { $0.name == name }?.id
    }

    func rememberAudienceDisplay(_ id: CGDirectDisplayID, displays: [PrestoDisplay]) {
        audienceDisplayID = id
        if let name = displays.first(where: { $0.id == id })?.name {
            UserDefaults.standard.set(name, forKey: "audienceDisplayName")
        }
    }

    // Pure, so the tests can feed it any arrangement of displays.
    // Two or more displays: the audience gets a whole display (the chosen
    // one, else an external one), borderless at its full frame above the
    // menu bar. If the console's window is on that display it moves to
    // another one, the built-in display first. One display: the main window
    // itself shows the deck. Forced dual-screen testing splits one display.
    static func audienceWindowPlan(displays: [PrestoDisplay], consoleDisplayID: CGDirectDisplayID?,
                                   preferredID: CGDirectDisplayID?, forceDual: Bool) -> AudienceWindowPlan {
        if displays.count == 1, forceDual, let only = displays.first {
            let gap: CGFloat = 8
            let half = (only.visibleFrame.width - gap) / 2
            let audience = NSRect(x: only.visibleFrame.minX + half + gap, y: only.visibleFrame.minY,
                                  width: half, height: only.visibleFrame.height)
            let console = NSRect(x: only.visibleFrame.minX, y: only.visibleFrame.minY,
                                 width: half, height: only.visibleFrame.height)
            return AudienceWindowPlan(displayID: only.id, frame: audience, borderless: false,
                                      level: .normal, consoleFrame: console)
        }
        guard displays.count > 1 else {
            return AudienceWindowPlan(displayID: nil, frame: displays.first?.frame ?? .zero,
                                      borderless: false, level: .normal)
        }
        let target = displays.first { $0.id == preferredID }
            ?? displays.first { !$0.isBuiltin && $0.id != consoleDisplayID }
            ?? displays.first { !$0.isBuiltin }
            ?? displays.first { $0.id != consoleDisplayID }
            ?? displays[0]
        var consoleFrame: NSRect? = nil
        if consoleDisplayID == nil || consoleDisplayID == target.id {
            let home = displays.first { $0.isBuiltin && $0.id != target.id }
                ?? displays.first { $0.id != target.id }
            consoleFrame = home?.visibleFrame
        }
        return AudienceWindowPlan(displayID: target.id, frame: target.frame, borderless: true,
                                  level: .screenSaver, consoleFrame: consoleFrame)
    }

    // The display the console window is on once the talk starts: the plan
    // moves it off the audience's display.
    static func consoleDisplayAtStart(displays: [PrestoDisplay], consoleDisplayID: CGDirectDisplayID?,
                                      plan: AudienceWindowPlan) -> CGDirectDisplayID? {
        guard let frame = plan.consoleFrame else { return consoleDisplayID }
        return displays.first { $0.visibleFrame == frame }?.id ?? consoleDisplayID
    }

    // Swap screens before Start: the audience takes the console's display
    // (the console moves off it at Start), or, if it already has it, gives
    // it back. Nil when there is nothing to swap.
    static func readySwapAudienceID(displays: [PrestoDisplay], consoleDisplayID: CGDirectDisplayID?,
                                    plannedAudienceID: CGDirectDisplayID?) -> CGDirectDisplayID? {
        guard displays.count > 1, let console = consoleDisplayID,
              displays.contains(where: { $0.id == console }) else { return nil }
        if plannedAudienceID != console { return console }
        let plan = audienceWindowPlan(displays: displays, consoleDisplayID: console, preferredID: console, forceDual: false)
        let home = consoleDisplayAtStart(displays: displays, consoleDisplayID: console, plan: plan)
        return home == console ? nil : home
    }

    func plannedAudience() -> AudienceWindowPlan {
        let displays = currentDisplays()
        return AppDelegate.audienceWindowPlan(displays: displays, consoleDisplayID: mainWindowDisplayID(),
                                              preferredID: preferredAudienceDisplayID(displays),
                                              forceDual: forceDualScreenForTesting)
    }

    func observeScreenChanges() {
        guard screenParametersObserver == nil else { return }
        screenParametersObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { [weak self] _ in
            self?.handleScreensChanged()
        }
    }

    // A display came or went during the talk. Unplugged: hide the audience
    // window (it must never cover the console) and keep the console live.
    // Plugged back in: put the audience back on it.
    func handleScreensChanged() {
        NSLog("Presto: screens changed (%d)", NSScreen.screens.count)
        defer {
            pushConsoleState()
            updateToolbarState()
        }
        guard presentPhase == .live, !audienceOneScreen, !forceDualScreenForTesting,
              let window = audienceWindow else { return }
        let plan = plannedAudience()
        guard let id = plan.displayID, plan.borderless else {
            window.orderOut(nil)
            if !audienceDisplayMissing {
                audienceDisplayMissing = true
                showPresentationHint("Audience display disconnected. Reconnect it to continue on the projector.")
            }
            return
        }
        if let consoleFrame = plan.consoleFrame, mainWindow?.styleMask.contains(.fullScreen) != true {
            mainWindow?.setFrame(consoleFrame, display: true)
        }
        audienceDisplayID = id
        window.setFrame(plan.frame, display: true)
        window.orderFrontRegardless()
        mainWindow?.makeKeyAndOrderFront(nil)
        if audienceDisplayMissing {
            audienceDisplayMissing = false
            showPresentationHint("Audience display reconnected")
        }
    }

    // AppKit may move the audience window before the screen notification
    // arrives; it must never sit on the console's display.
    func windowDidChangeScreen(_ notification: Notification) {
        guard let window = notification.object as? NSWindow, window === audienceWindow,
              presentPhase == .live, !forceDualScreenForTesting,
              displayID(of: window.screen) == mainWindowDisplayID() else { return }
        handleScreensChanged()
    }

    // MARK: - Ready

    // Present mode's first phase: the console with Start, talk length and the
    // audience display. Nothing is shown to the audience yet.
    func enterPresentReady(origin: PrestoMode, index: Int, step: Int) {
        guard currentDeckURL != nil else { return }
        if presentPhase == .none {
            presentOrigin = origin == .present ? .decks : origin
            lastAudienceSlideIndex = max(0, index)
            lastAudienceStep = max(0, step)
            lastAudienceReturnStack = []
        }
        presentPhase = .ready
        ensureConsoleWebView()
        showMode(.present)
        syncConsoleToPosition()
    }

    // Leaving the ready console for Decks or Edit. Nothing was shown, so
    // the deck's last position stays the edit view's.
    func leavePresentReady() {
        guard presentPhase == .ready else { return }
        beginTeardownRound()
        teardownConsole()
        presentPhase = .none
    }

    @objc func ensureConsoleWebView() {
        guard consoleWebView == nil, let deckURL = currentDeckURL else { return }
        let resourceRoots = resolveDeckResourceRoots(for: deckURL)
        let presenterHTML = resourceRoots.webSlidesRoot.appendingPathComponent("presto/presenter.html")
        NSLog("Presto: deckRoot=%@, webSlidesRoot=%@, readAccessRoot=%@, presenterHTML exists=%d", resourceRoots.deckRoot.path, resourceRoots.webSlidesRoot.path, resourceRoots.readAccessRoot.path, FileManager.default.fileExists(atPath: presenterHTML.path) ? 1 : 0)
        let config = makeBaseConfig()
        config.userContentController.add(self, name: "presto")
        let webView = makePrestoWebView(configuration: config)
        consoleWebView = webView
        presenterReady = false
        // A new console has its own WebContent recovery allowance (and, in
        // tests, its own simulated termination).
        webContentRecoveryAttempts[.presenter] = nil
        simulatedWebContentTerminationRoles.remove(.presenter)
        NSLog("Presto: loading presenter HTML")
        webView.loadFileURL(presenterHTML, allowingReadAccessTo: resourceRoots.readAccessRoot)
    }

    func teardownConsole() {
        guard let console = consoleWebView else { return }
        console.configuration.userContentController.removeScriptMessageHandler(forName: "presto")
        console.removeFromSuperview()
        retireWebView(console)
        consoleWebView = nil
        presenterReady = false
        consoleTextEditing = false
    }

    // Puts the console's preview on the talk's slide. Live, the audience is
    // the source of truth; before Start it is Presto's own start position.
    func syncConsoleToPosition() {
        if audienceWebView != nil {
            syncPresenterFromAudience()
            return
        }
        guard presenterReady, let console = consoleWebView else { return }
        let state: [String: Any] = ["index": lastAudienceSlideIndex, "step": lastAudienceStep,
                                    "returnStack": lastAudienceReturnStack]
        guard let data = try? JSONSerialization.data(withJSONObject: state),
              let json = String(data: data, encoding: .utf8) else { return }
        console.evaluateJavaScript("presenterUpdate(\(json))") { _, error in
            if let error { NSLog("Presto: presenter update failed: %@", error.localizedDescription) }
        }
        refreshDeckOutline()
    }

    // Moves the console's own preview before Start (arrow keys, sections).
    func runConsolePreviewCommand(_ command: String) {
        consoleWebView?.evaluateJavaScript("""
        (function () {
            var frame = document.getElementById('current-slide') || document.querySelector('iframe');
            var WS = frame && frame.contentWindow && frame.contentWindow.WS;
            if (!WS || !WS.slides || !WS.slides.length) return false;
            \(command);
            return true;
        })()
        """) { _, _ in }
    }

    // MARK: - Live

    @objc func playFromCurrentSlide(_ sender: Any?) {
        startShow(fromStart: false)
    }

    @objc func playFromStart(_ sender: Any?) {
        startShow(fromStart: true)
    }

    // ⌘↩ / ⇧⌘↩ and the Play button go live at once; Start in the ready
    // console goes live where its preview stands.
    func startShow(fromStart: Bool) {
        guard currentDeckURL != nil, presentPhase != .live else { return }
        if presentPhase == .ready {
            goLive(index: fromStart ? 0 : lastAudienceSlideIndex, step: fromStart ? 0 : lastAudienceStep,
                   origin: presentOrigin)
            return
        }
        let origin = mode
        leaveCurrentMode { [weak self] left in
            guard let self, left else { return }
            let start = fromStart ? (index: 0, step: 0) : self.positionForPresenting()
            self.goLive(index: start.index, step: start.step, origin: origin)
        }
    }

    @objc func goLive(index: Int, step: Int, origin: PrestoMode) {
        guard let deckURL = currentDeckURL, presentPhase != .live else { return }
        if presentPhase == .none {
            presentOrigin = origin == .present ? .decks : origin
            lastAudienceReturnStack = []
        }
        if index != lastAudienceSlideIndex || step != lastAudienceStep { lastAudienceReturnStack = [] }
        lastAudienceSlideIndex = max(0, index)
        lastAudienceStep = max(0, step)
        let plan = plannedAudience()
        presentPhase = .live
        isEndingPresentation = false
        lastPresentationExitEscape = nil
        lastLiveQuitPress = nil
        // ⌘↩ can start the talk from the ready card's talk-length field: the
        // keys belong to the talk now, not to a field that is about to hide.
        consoleTextEditing = false
        consoleWebView?.evaluateJavaScript(AppDelegate.blurConsoleFieldJS) { _, _ in }
        // Each show has its own WebContent recovery allowance (and, in tests,
        // its own simulated termination).
        webContentRecoveryAttempts[.audience] = nil
        simulatedWebContentTerminationRoles.remove(.audience)
        audienceOneScreen = plan.displayID == nil
        audienceDisplayMissing = false
        if let id = plan.displayID, !forceDualScreenForTesting { rememberAudienceDisplay(id, displays: currentDisplays()) }
        else { audienceDisplayID = plan.displayID }
        blackoutActive = false
        if !talkTimerStartedThisSession {
            talkTimer.reset()
            talkTimerStartedThisSession = true
        }
        talkTimer.start()
        beginDisplaySleepBlock()
        pauseThumbnailRenderer()

        let config = makeBaseConfig()
        config.userContentController.add(self, name: "presto")
        let aWebView = makePrestoWebView(configuration: config)
        audienceWebView = aWebView
        NSLog("Presto: loading audience deck")
        aWebView.loadFileURL(deckURL, allowingReadAccessTo: findReadAccessRoot(deckURL: deckURL))

        if audienceOneScreen {
            startOneScreenShow()
        } else {
            startTwoScreenShow(plan)
        }
        updateToolbarState()
        pushConsoleState()
        NSLog("Presto: presentation started")
    }

    static let blurConsoleFieldJS = """
        (function () {
            var field = document.activeElement;
            if (field && field !== document.body && typeof field.blur === 'function') field.blur();
            return true;
        })()
        """

    func startTwoScreenShow(_ plan: AudienceWindowPlan) {
        guard let deckURL = currentDeckURL, let mainWindow else { return }
        ensureConsoleWebView()
        if let consoleFrame = plan.consoleFrame, !mainWindow.styleMask.contains(.fullScreen) {
            mainWindowFrameBeforeShow = mainWindow.frame
            mainWindow.setFrame(consoleFrame, display: true)
        }
        showMode(.present)
        let window = PrestoAudienceWindow(contentRect: plan.frame,
                                          styleMask: plan.borderless ? [.borderless] : [.titled, .closable, .miniaturizable, .resizable],
                                          backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.title = forceDualScreenForTesting
            ? "Presto [Audience] — \(deckName(for: deckURL.path))"
            : "Presto Audience — \(deckName(for: deckURL.path))"
        window.contentView = audienceWebView
        // Letterboxing shows the slide's own white, never black chrome.
        window.backgroundColor = .white
        window.level = plan.level
        window.hidesOnDeactivate = false
        window.isMovable = !plan.borderless
        window.hasShadow = !plan.borderless
        if plan.borderless {
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        }
        window.allowsKey = !plan.borderless
        window.delegate = self
        audienceWindow = window
        window.setFrame(plan.frame, display: true)
        window.orderFrontRegardless()
        mainWindow.makeKeyAndOrderFront(nil)
        if forceDualScreenForTesting {
            NSLog("PrestoTestDualScreen: started simulated=%d screens=%d",
                  NSScreen.screens.count == 1 ? 1 : 0, NSScreen.screens.count)
        }
    }

    // One screen: the main window goes native full screen showing only the
    // deck, with the control bar; leaving full screen ends the show.
    func startOneScreenShow() {
        guard let mainWindow else { return }
        mainWindowWasFullScreenBeforeShow = mainWindow.styleMask.contains(.fullScreen)
        showMode(.present)
        if let audience = audienceWebView { mainWindow.makeFirstResponder(audience) }
        if !mainWindowWasFullScreenBeforeShow {
            oneScreenFullScreenPending = true
            mainWindow.toggleFullScreen(nil)
        }
        NSLog("Presto: single-screen presentation started")
    }

    // The one-screen show hides the menu bar and Dock outright, so the
    // pointer at the top edge never brings them (or the title bar) over the
    // slide. AppKit rejects hideMenuBar without hideDock. Every other full
    // screen keeps the proposed options, and leaving full screen restores
    // the normal menu bar and Dock.
    static let oneScreenShowPresentationOptions: NSApplication.PresentationOptions = [.fullScreen, .hideDock, .hideMenuBar]

    func window(_ window: NSWindow,
                willUseFullScreenPresentationOptions proposedOptions: NSApplication.PresentationOptions = []
    ) -> NSApplication.PresentationOptions {
        guard window === mainWindow, presentPhase == .live, audienceOneScreen else { return proposedOptions }
        oneScreenShowHidDock = true
        return AppDelegate.oneScreenShowPresentationOptions
    }

    func windowDidEnterFullScreen(_ notification: Notification) {
        guard (notification.object as? NSWindow) === mainWindow else { return }
        oneScreenFullScreenPending = false
    }

    func windowDidFailToEnterFullScreen(_ window: NSWindow) {
        guard window === mainWindow else { return }
        // The show carries on in the window.
        oneScreenFullScreenPending = false
        NSLog("Presto: could not enter full screen for the one-screen show")
    }

    func windowDidExitFullScreen(_ notification: Notification) {
        guard let window = notification.object as? NSWindow, window === mainWindow else { return }
        if oneScreenShowHidDock {
            oneScreenShowHidDock = false
            // After hideDock AppKit can leave the window unable to minimize
            // (Apple DTS, FB24169722); toggling the style bit restores it.
            if window.styleMask.contains(.miniaturizable) {
                window.styleMask.remove(.miniaturizable)
                window.styleMask.insert(.miniaturizable)
            }
        }
        if oneScreenExitPending {
            oneScreenExitPending = false
            finishPresentationTeardown()
            return
        }
        // The green button or ⌃⌘F during a one-screen show ends it.
        if presentPhase == .live && audienceOneScreen && !isEndingPresentation {
            NSLog("Presto: one-screen show left full screen")
            isEndingPresentation = true
            finishPresentationTeardown()
        }
    }

    // MARK: - Ending

    @objc func requestEndPresentation() {
        guard presentPhase != .none, !isEndingPresentation else { return }
        isEndingPresentation = true
        let generation = sessionGeneration
        DispatchQueue.main.async { [weak self] in
            self?.finishEndRequest(generation: generation)
        }
    }

    func finishEndRequest(generation: Int) {
        guard sessionGeneration == generation else {
            isEndingPresentation = false
            return
        }
        endPresentation()
    }

    func endPresentation() {
        guard isEndingPresentation else { return }
        NSLog("Presto: endPresentation")
        if presentPhase == .live, audienceOneScreen, let window = mainWindow,
           window.styleMask.contains(.fullScreen), !mainWindowWasFullScreenBeforeShow {
            // Finish once the window is back, so Edit does not draw mid-animation.
            oneScreenExitPending = true
            window.toggleFullScreen(nil)
            let generation = sessionGeneration
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                guard let self, self.sessionGeneration == generation, self.oneScreenExitPending else { return }
                self.oneScreenExitPending = false
                self.finishPresentationTeardown()
            }
            return
        }
        finishPresentationTeardown()
    }

    // Back to the mode the talk came from, at the slide and step it ended on.
    func finishPresentationTeardown() {
        guard presentPhase != .none else {
            isEndingPresentation = false
            return
        }
        let index = lastAudienceSlideIndex
        let step = lastAudienceStep
        recordDeckPosition(index: index, step: step)
        beginTeardownRound()
        teardownAudience()
        teardownConsole()
        presentPhase = .none
        talkTimer.pause()
        isEndingPresentation = false
        mainWindow?.toolbar?.isVisible = true
        if presentOrigin == .edit, isEditorMode {
            currentEditorSlideIndex = index
            currentEditorStep = step
            showMode(.edit)
            navigateEditorPreview(command: "WS.goToSlide(\(index), \(step))")
        } else {
            showMode(.decks)
        }
        processThumbnailQueue()
        let sheets = sheetsAfterTalk
        sheetsAfterTalk = []
        sheets.forEach { $0() }
        NSLog("Presto: presentation ended")
    }

    // A sheet on the console or the full-screen slide would take the keyboard
    // from the talk, so sheets raised while live wait until it ends.
    func presentSheetWhenNotLive(_ show: @escaping () -> Void) {
        if presentPhase == .live {
            sheetsAfterTalk.append(show)
        } else {
            show()
        }
    }

    func teardownAudience() {
        if let audience = audienceWebView {
            audience.configuration.userContentController.removeScriptMessageHandler(forName: "presto")
            audience.removeFromSuperview()
            retireWebView(audience)
        }
        if let window = audienceWindow {
            window.delegate = nil
            window.orderOut(nil)
            staleWindows.append(window)
        }
        audienceWebView = nil
        audienceWindow = nil
        audienceOneScreen = false
        audienceDisplayMissing = false
        blackoutActive = false
        lastPresentationExitEscape = nil
        if let frame = mainWindowFrameBeforeShow, mainWindow?.styleMask.contains(.fullScreen) != true {
            mainWindow?.setFrame(frame, display: true)
        }
        mainWindowFrameBeforeShow = nil
        endDisplaySleepBlock()
    }

    // The projector must not sleep in the middle of a talk.
    func beginDisplaySleepBlock() {
        guard displaySleepActivity == nil else { return }
        displaySleepActivity = ProcessInfo.processInfo.beginActivity(
            options: [.idleDisplaySleepDisabled, .userInitiated], reason: "Presenting a talk")
    }

    func endDisplaySleepBlock() {
        if let activity = displaySleepActivity { ProcessInfo.processInfo.endActivity(activity) }
        displaySleepActivity = nil
    }

    // "Edit this slide" from the one-screen bar ends the show into Edit.
    func endShowIntoEdit() {
        guard presentPhase != .none else { return }
        guard ensureEditSurfaces() else { return }
        presentOrigin = .edit
        requestEndPresentation()
    }

    // MARK: - Escape

    // Escape in presentation mode, in Edit's order: close the slide grid if
    // it is open (the audience page's on one screen, the console preview's on
    // two), else leave the appendix, else end the talk only on a second
    // press within 2 seconds.
    func handlePresentationEscape() {
        let gridView = audienceOneScreen ? audienceWebView : consoleWebView
        guard let gridView else {
            handlePresentationEscapeOutsideGrid()
            return
        }
        let generation = sessionGeneration
        let oneScreen = audienceOneScreen
        let js = AppDelegate.closeOverviewJS(wsExpression: oneScreen ? "window.WS" : AppDelegate.consolePreviewWSExpression)
        gridView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self, self.sessionGeneration == generation, self.presentPhase == .live,
                  (oneScreen ? self.audienceWebView : self.consoleWebView) === gridView else { return }
            if result as? Bool == true {
                self.lastPresentationExitEscape = nil
            } else {
                self.handlePresentationEscapeOutsideGrid()
            }
        }
    }

    // The console's preview iframe's engine.
    static let consolePreviewWSExpression =
        "(function(){ var f = document.getElementById('current-slide') || document.querySelector('iframe'); return f && f.contentWindow && f.contentWindow.WS; })()"

    // Closes an open slide grid; true when there was one. Presto's own
    // move, so the audience page does not report it back.
    static func closeOverviewJS(wsExpression: String) -> String {
        """
        (function (WS) {
            if (!WS || !WS.overviewActive || typeof WS.toggleOverview !== 'function') return false;
            window.__prestoRemoteNavigation = true;
            try { WS.toggleOverview(); } finally { window.__prestoRemoteNavigation = false; }
            return true;
        })(\(wsExpression))
        """
    }

    func handlePresentationEscapeOutsideGrid() {
        guard let aWebView = audienceWebView else {
            requestPresentationExitWithConfirmation()
            return
        }
        let generation = sessionGeneration
        aWebView.evaluateJavaScript("(window.WS && Array.isArray(WS.returnStack)) ? WS.returnStack.length : 0") { [weak self] result, _ in
            guard let self = self,
                  self.sessionGeneration == generation,
                  self.audienceWebView === aWebView else { return }
            if let depth = self.finiteInt(result), depth > 0 {
                self.lastPresentationExitEscape = nil
                self.navigateAudience(command: "WS.returnFromAppendix()")
            } else {
                self.requestPresentationExitWithConfirmation()
            }
        }
    }

    // First Escape outside the appendix arms a 2s window and shows a hint on
    // the presenter's screen (the console, or the deck itself on one
    // screen); a second Escape inside that window ends the presentation.
    func requestPresentationExitWithConfirmation(now: Date = Date()) {
        if let last = lastPresentationExitEscape,
           now.timeIntervalSince(last) < AppDelegate.presentationExitConfirmWindow {
            lastPresentationExitEscape = nil
            requestEndPresentation()
        } else {
            lastPresentationExitEscape = now
            showHint("Press Escape again to end the presentation",
                     in: presentationHintWebView,
                     duration: AppDelegate.presentationExitConfirmWindow)
        }
    }

    var presentationHintWebView: WKWebView? {
        audienceOneScreen ? audienceWebView : (consoleWebView ?? audienceWebView)
    }

    func showPresentationHint(_ message: String) {
        showHint(message, in: presentationHintWebView, duration: AppDelegate.presentationHintDuration)
    }

    // Problems and notices for whatever Duncan is looking at.
    @objc func showPresenterHint(_ message: String) {
        if presentPhase != .none {
            showPresentationHint(message)
        } else {
            showHint(message, in: previewWebView ?? consoleWebView, duration: 2.5)
        }
    }

    // A light toast: the one-screen bar's or the console's own when the page
    // has one, else a plain light one.
    @objc func showHint(_ message: String, in webView: WKWebView?, duration: TimeInterval) {
        guard let webView else { return }
        let milliseconds = Int(duration * 1000)
        let js = """
        (function(m, ms){
            try {
                if (window.PrestoHUD && typeof PrestoHUD.hint === 'function') { PrestoHUD.hint(m, ms); return; }
                if (window.PrestoConsole && typeof PrestoConsole.hint === 'function') { PrestoConsole.hint(m, ms); return; }
                var id = '__prestoHint';
                var el = document.getElementById(id);
                if (!el) {
                    el = document.createElement('div');
                    el.id = id;
                    el.style.cssText = 'position:fixed;left:50%;top:14px;transform:translateX(-50%);z-index:2147483647;background:var(--p-surface,#fff);color:var(--p-ink,#1C2532);padding:7px 14px;border-radius:8px;font:500 12.5px -apple-system,system-ui,sans-serif;pointer-events:none;transition:opacity .2s;box-shadow:var(--p-shadow-pop,0 1px 2px rgba(16,24,40,.08),0 8px 24px rgba(16,24,40,.14),0 0 0 .5px rgba(0,0,0,.08))';
                    document.body.appendChild(el);
                }
                el.textContent = m;
                el.style.opacity = '1';
                clearTimeout(el.__t);
                el.__t = setTimeout(function(){ el.style.opacity = '0'; }, ms);
            } catch (e) {}
        })(\(jsStringLiteral(message)), \(milliseconds));
        """
        webView.evaluateJavaScript(js) { _, _ in }
    }

    // MARK: - Talk Controls

    @objc func toggleBlackScreen(_ sender: Any?) {
        setBlackout(!blackoutActive)
    }

    func setBlackout(_ on: Bool) {
        guard presentPhase == .live else { return }
        blackoutActive = on
        audienceWebView?.evaluateJavaScript(AppDelegate.blackoutJS(on)) { _, _ in }
        updateToolbarState()
        pushConsoleState()
    }

    // The black screen is content (a full-viewport cover), not chrome.
    static func blackoutJS(_ on: Bool) -> String {
        """
        (function (on) {
            var cover = document.getElementById('presto-blackout');
            if (!on) { if (cover) cover.remove(); return false; }
            if (!cover) {
                cover = document.createElement('div');
                cover.id = 'presto-blackout';
                cover.style.cssText = 'position:fixed;inset:0;background:#000;z-index:2147483646;pointer-events:none';
                document.body.appendChild(cover);
            }
            return true;
        })(\(on ? "true" : "false"))
        """
    }

    // The toolbar's Swap screens and the menu's Swap Displays.
    var canSwapDisplays: Bool {
        (presentPhase == .ready || (presentPhase == .live && !audienceOneScreen)) && currentDisplays().count > 1
    }

    @objc func swapDisplays(_ sender: Any?) {
        if presentPhase == .ready {
            swapDisplaysBeforeStart()
            return
        }
        guard presentPhase == .live, !audienceOneScreen, !forceDualScreenForTesting,
              let mainWindow, let audienceWindow else { return }
        let displays = currentDisplays()
        guard let consoleID = mainWindowDisplayID(), let audienceID = audienceDisplayID,
              consoleID != audienceID,
              let consoleDisplay = displays.first(where: { $0.id == consoleID }),
              let audienceDisplay = displays.first(where: { $0.id == audienceID }) else { return }
        guard !mainWindow.styleMask.contains(.fullScreen) else {
            showPresentationHint("Leave full screen in the console window to swap screens")
            return
        }
        rememberAudienceDisplay(consoleID, displays: displays)
        mainWindow.setFrame(audienceDisplay.visibleFrame, display: true)
        audienceWindow.setFrame(consoleDisplay.frame, display: true)
        audienceWindow.orderFrontRegardless()
        mainWindow.makeKeyAndOrderFront(nil)
        updateToolbarState()
        pushConsoleState()
        NSLog("Presto: swapped screens; audience on %@", consoleDisplay.name)
    }

    // Nothing is on screen yet: only the choice of audience display changes,
    // and the ready card shows it.
    func swapDisplaysBeforeStart() {
        guard !forceDualScreenForTesting else { return }
        let displays = currentDisplays()
        guard let target = AppDelegate.readySwapAudienceID(displays: displays, consoleDisplayID: mainWindowDisplayID(),
                                                           plannedAudienceID: plannedAudience().displayID) else { return }
        rememberAudienceDisplay(target, displays: displays)
        pushConsoleState()
        updateToolbarState()
    }

    func setAudienceDisplay(_ id: CGDirectDisplayID) {
        let displays = currentDisplays()
        guard displays.contains(where: { $0.id == id }) else { return }
        if presentPhase == .live, !audienceOneScreen {
            if id == mainWindowDisplayID() {
                swapDisplays(nil)
            } else if let display = displays.first(where: { $0.id == id }) {
                rememberAudienceDisplay(id, displays: displays)
                audienceWindow?.setFrame(display.frame, display: true)
                audienceWindow?.orderFrontRegardless()
            }
        } else {
            rememberAudienceDisplay(id, displays: displays)
        }
        pushConsoleState()
        updatePresentStatus()
    }

    func handleConsoleTimer(_ action: String) {
        switch action {
        case "start":
            talkTimer.start()
            talkTimerStartedThisSession = true
        case "pause":
            talkTimer.pause()
        case "reset":
            let wasRunning = talkTimer.running
            talkTimer.reset()
            if wasRunning { talkTimer.start() }
        default:
            return
        }
        pushConsoleState()
        pushHUDState()
    }

    func setTalkMinutes(_ minutes: Int?) {
        guard let deckURL = currentDeckURL else { return }
        let valid = minutes.flatMap { $0 > 0 && $0 <= 600 ? $0 : nil }
        deckLibrary.setTalkMinutes(deckURL, valid)
        pushConsoleState()
        scheduleDecksRender()
    }

    @objc func endFromToolbar(_ sender: Any?) {
        if presentPhase == .live {
            requestEndPresentation()
        } else if presentPhase == .ready {
            let origin = presentOrigin
            leavePresentReady()
            if origin == .edit && isEditorMode { showMode(.edit) } else { showMode(.decks) }
        }
    }

    // MARK: - Console And Bar State

    // Toolbar, console and one-screen bar after the talk moved.
    func updatePresentationChrome() {
        pushConsoleState()
        pushHUDState()
        updatePresentStatus()
    }

    func refreshDeckOutline() {
        let source: (WKWebView, String)?
        if let audience = audienceWebView, !audience.isLoading {
            source = (audience, "window.WS")
        } else if let preview = previewWebView, !preview.isLoading {
            source = (preview, "window.WS")
        } else if let console = consoleWebView {
            source = (console, AppDelegate.consolePreviewWSExpression)
        } else {
            source = nil
        }
        guard let (webView, wsExpression) = source else { return }
        let generation = sessionGeneration
        webView.evaluateJavaScript(AppDelegate.deckOutlineJS(wsExpression: wsExpression)) { [weak self] result, _ in
            guard let self, self.sessionGeneration == generation,
                  let json = result as? String, let data = json.data(using: .utf8),
                  let outline = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            var parsed = DeckOutline()
            parsed.mainSlideCount = self.finiteInt(outline["mainSlideCount"]) ?? 0
            parsed.totalSlides = self.finiteInt(outline["totalSlides"]) ?? 0
            parsed.sections = outline["sections"] as? [[String: Any]] ?? []
            parsed.slides = outline["slides"] as? [[String: Any]] ?? []
            self.deckOutline = parsed
            self.pushConsoleState()
            self.pushHUDState()
            self.updatePresentStatus()
        }
    }

    // Sections are 0-based inclusive WS indices over main slides only.
    static func deckOutlineJS(wsExpression: String) -> String {
        """
        (function (WS) {
            if (!WS || !WS.slides || !WS.slides.length) return null;
            // A heading's words: line breaks become spaces and KaTeX's
            // hidden MathML copy is dropped.
            function headingText(heading) {
                if (!heading) return '';
                var copy = heading.cloneNode ? heading.cloneNode(true) : heading;
                if (copy.querySelectorAll) {
                    copy.querySelectorAll('.katex-mathml').forEach(function (n) { n.remove(); });
                    copy.querySelectorAll('br').forEach(function (n) { n.replaceWith(' '); });
                }
                return String(copy.textContent || '').replace(/\\s+/g, ' ').trim();
            }
            var slides = WS.slides.map(function (slide, i) {
                var heading = slide.querySelector('h1, h2');
                var max = 0;
                try { max = WS.slideSteps && WS.slideSteps.get ? (WS.slideSteps.get(slide) || 0) : 0; } catch (e) {}
                return {
                    title: headingText(heading),
                    appendix: slide.classList.contains('appendix'),
                    number: slide.dataset.wsMainNumber || null,
                    maxStep: max
                };
            });
            var appendixCount = 0;
            slides.forEach(function (s) { if (s.appendix) { appendixCount += 1; s.label = 'A' + appendixCount; } });
            var mainIndices = [];
            slides.forEach(function (s, i) { if (!s.appendix) mainIndices.push(i); });
            var lastMain = mainIndices.length ? mainIndices[mainIndices.length - 1] : -1;
            var starts = (Array.isArray(WS.sections) ? WS.sections : []).filter(function (s) {
                return s && typeof s.slideIndex === 'number' && s.slideIndex <= lastMain;
            });
            var sections = [];
            starts.forEach(function (s, i) {
                var next = i + 1 < starts.length ? starts[i + 1].slideIndex : lastMain + 1;
                var last = Math.max(s.slideIndex, next - 1);
                var previous = sections[sections.length - 1];
                if (previous && previous.title === s.name) { previous.last = last; return; }
                if (previous && previous.first === s.slideIndex) { sections.pop(); }
                sections.push({ title: s.name, first: s.slideIndex, last: last });
            });
            return JSON.stringify({
                mainSlideCount: mainIndices.length,
                totalSlides: slides.length,
                sections: sections,
                slides: slides
            });
        })(\(wsExpression))
        """
    }

    // The outline's entry for the talk's slide (empty until the outline is read).
    var currentOutlineSlide: [String: Any] {
        lastAudienceSlideIndex < deckOutline.slides.count ? deckOutline.slides[lastAudienceSlideIndex] : [:]
    }

    func consoleState() -> [String: Any] {
        let index = lastAudienceSlideIndex
        let slide = currentOutlineSlide
        let isAppendix = (slide["appendix"] as? Bool) ?? false
        let displays = currentDisplays()
        let consoleID = mainWindowDisplayID()
        // Before Start the choices exclude the display the console will be
        // on, so after Swap the card names the console's current display.
        let readyPlan = presentPhase == .live ? nil : plannedAudience()
        let homeID = readyPlan.map {
            AppDelegate.consoleDisplayAtStart(displays: displays, consoleDisplayID: consoleID, plan: $0)
        } ?? consoleID
        let others = audienceOneScreen && presentPhase == .live
            ? [] : displays.filter { $0.id != homeID || forceDualScreenForTesting }
        let audienceID = presentPhase == .live ? audienceDisplayID : (others.isEmpty ? nil : readyPlan?.displayID)
        var timer: [String: Any] = [
            "running": talkTimer.running,
            "elapsedMs": talkTimer.accumulatedMs
        ]
        timer["startedAtMs"] = talkTimer.startedAt.map { $0.timeIntervalSince1970 * 1000 } ?? NSNull()
        var state: [String: Any] = [
            "phase": presentPhase == .live ? "live" : "ready",
            "deckTitle": deckTitle,
            "slideIndex": index,
            "step": lastAudienceStep,
            "maxStep": finiteInt(slide["maxStep"]) ?? 0,
            "mainSlideCount": deckOutline.mainSlideCount,
            "totalSlides": deckOutline.totalSlides,
            "isAppendix": isAppendix,
            "appendixLabel": isAppendix ? (slide["label"] as? String ?? "") : "",
            "slideTitle": slide["title"] as? String ?? "",
            "sections": deckOutline.sections,
            "timer": timer,
            "displays": others.map { ["id": String($0.id), "name": $0.name] },
            "blackout": blackoutActive
        ]
        state["slideNumber"] = (slide["number"] as? String).flatMap { Int($0) } ?? NSNull()
        state["talkMinutes"] = currentDeckURL.flatMap { deckLibrary.talkMinutes($0) } ?? NSNull()
        state["audienceDisplayId"] = audienceID.map { String($0) } ?? NSNull()
        return state
    }

    func pushConsoleState() {
        guard presentPhase != .none, presenterReady, let console = consoleWebView,
              let data = try? JSONSerialization.data(withJSONObject: consoleState()),
              let json = String(data: data, encoding: .utf8) else { return }
        console.evaluateJavaScript(
            "window.PrestoConsole && typeof PrestoConsole.setState === 'function' && PrestoConsole.setState(\(json))"
        ) { _, _ in }
    }

    func pushHUDState() {
        guard presentPhase == .live, let audience = audienceWebView else { return }
        let index = lastAudienceSlideIndex
        let slide = currentOutlineSlide
        let isAppendix = (slide["appendix"] as? Bool) ?? false
        let number: Any = isAppendix ? (slide["label"] as? String ?? "") :
            ((slide["number"] as? String).flatMap { Int($0) } ?? (index + 1))
        let state: [String: Any] = [
            "slideNumber": number,
            "total": deckOutline.mainSlideCount,
            "isAppendix": isAppendix,
            "elapsedMs": talkTimer.elapsedMs(),
            "timerVisible": hudTimerVisible,
            "timerRunning": talkTimer.running
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: state),
              let json = String(data: data, encoding: .utf8) else { return }
        audience.evaluateJavaScript(
            "window.PrestoHUD && typeof PrestoHUD.setState === 'function' && PrestoHUD.setState(\(json))"
        ) { _, _ in }
    }

    func toggleHUDTimer() {
        hudTimerVisible.toggle()
        pushHUDState()
    }

    func updatePresentStatus() {
        guard let label = toolbarItems[AppDelegate.toolbarStatus]?.view as? NSTextField else { return }
        let font = NSFont.systemFont(ofSize: 13)
        let text = NSMutableAttributedString()
        if presentPhase == .live {
            text.append(NSAttributedString(string: "● ", attributes: [.font: font, .foregroundColor: NSColor.prestoLive]))
            if audienceOneScreen {
                text.append(NSAttributedString(string: "Live on this screen", attributes: [.font: font, .foregroundColor: NSColor.prestoInk]))
            } else if audienceDisplayMissing {
                text.append(NSAttributedString(string: "Audience display disconnected",
                                               attributes: [.font: font, .foregroundColor: NSColor.prestoLive]))
            } else {
                let name = currentDisplays().first { $0.id == audienceDisplayID }?.name ?? "the audience display"
                text.append(NSAttributedString(string: "Live on ", attributes: [.font: font, .foregroundColor: NSColor.prestoInk2]))
                text.append(NSAttributedString(string: name, attributes: [
                    .font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: NSColor.prestoInk]))
            }
            let slide = currentOutlineSlide
            let shown = (slide["appendix"] as? Bool) == true ? (slide["label"] as? String ?? "appendix")
                : ((slide["number"] as? String) ?? String(lastAudienceSlideIndex + 1))
            text.append(NSAttributedString(string: blackoutActive ? "  ·  screen is black" : "  ·  audience sees slide \(shown)",
                                           attributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: NSColor.prestoDim]))
        } else {
            text.append(NSAttributedString(string: "Ready", attributes: [
                .font: NSFont.systemFont(ofSize: 13, weight: .medium), .foregroundColor: NSColor.prestoInk]))
        }
        label.attributedStringValue = text
        label.sizeToFit()
        label.frame.size.width = min(max(label.frame.width, 160), 560)
    }
}

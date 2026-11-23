import Cocoa
import Foundation
import JavaScriptCore
import WebKit

// The one-window shell: mode transitions and their save guarantees, menu
// validation, Escape and ⌘Z routing, the audience window plan for single and
// dual screens, the console's state, and the visible status when an injected
// file is missing. Compiled into the transaction test binary (PRESTO_TESTING)
// and run from its main(); no window is ever ordered front.

// A web view that records every script and answers like a settled page.
final class RecordingView: WKWebView {
    var scripts: [String] = []
    var scriptError: Error?
    var pending = #"{"dirty":false,"index":0,"html":""}"#
    // A script containing the key gets the value as its result.
    var answers: [String: Any] = [:]
    var loads: [URL] = []

    override func evaluateJavaScript(_ javaScriptString: String,
                                     completionHandler: ((Any?, Error?) -> Void)? = nil) {
        scripts.append(javaScriptString)
        if let scriptError {
            completionHandler?(nil, scriptError)
        } else if javaScriptString.contains("getPendingEditorChange") {
            completionHandler?(pending, nil)
        } else if javaScriptString.contains("history: !!(window.PrestoEdit") {
            completionHandler?(#"{"editor":true,"history":true,"chrome":true}"#, nil)
        } else if let answer = answers.first(where: { javaScriptString.contains($0.key) }) {
            completionHandler?(answer.value, nil)
        } else {
            completionHandler?(nil, nil)
        }
    }

    override func load(_ request: URLRequest) -> WKNavigation? {
        if let url = request.url { loads.append(url) }
        return nil
    }

    func ran(_ fragment: String) -> Bool { scripts.contains { $0.contains(fragment) } }
}

final class ShellApp: StubbedUIApp {
    var missingResources: Set<String> = []
    var lives: [(index: Int, step: Int, origin: PrestoMode)] = []
    var hints: [String] = []
    var endRequests = 0
    var countEndRequests = false
    var sheets: [String] = []

    override func showEditorRecoveredSheet() { sheets.append("editor recovered") }
    override func showWebContentRecoveryFailure(role: WebContentRole) { sheets.append(role.rawValue) }

    override func readPrestoResource(_ name: String, for deckURL: URL) -> String? {
        missingResources.contains(name) ? nil : "/* \(name) */"
    }
    override func ensureConsoleWebView() {
        if consoleWebView == nil { consoleWebView = RecordingView() }
    }
    override func ensureDecksWebView() -> WKWebView {
        if decksWebView == nil { decksWebView = RecordingView() }
        return decksWebView!
    }
    override func goLive(index: Int, step: Int, origin: PrestoMode) {
        lives.append((index, step, origin))
    }
    override func showHint(_ message: String, in webView: WKWebView?, duration: TimeInterval) {
        hints.append(message)
    }
    override func requestEndPresentation() {
        if countEndRequests { endRequests += 1 } else { super.requestEndPresentation() }
    }
    override func reportEditorSaveFailed(_ message: String, saveId: Int? = nil) {}
    override func startFileWatcher() {}
    override func stopFileWatcher() {}
}

// Real shows (goLive, teardown) with no main window, so nothing enters full
// screen or is ordered front.
final class LiveApp: StubbedUIApp {
    var hints: [String] = []
    override func showHint(_ message: String, in webView: WKWebView?, duration: TimeInterval) {
        hints.append(message)
    }
    override func startFileWatcher() {}
    override func stopFileWatcher() {}

    static func session(_ name: String) -> LiveApp {
        let app = LiveApp()
        app.allowsGoingLive = true
        let url = ShellModeTests.deckDirectory.appendingPathComponent("\(name).html")
        let content = deckHTML(["One", "Two", "Three"])
        try? content.write(to: url, atomically: true, encoding: .utf8)
        app.beginDeckSession(url, content: content)
        return app
    }

    // Ends the show the way End does; the window teardown makes stays hidden.
    func endShow() {
        isEndingPresentation = true
        endPresentation()
        mainWindow = nil
    }
}

// The real edit preview over a repo deck: saves stay in memory.
final class NoDiskApp: StubbedUIApp {
    var writes = 0
    override func writeDeckContent(_ content: String, to deckURL: URL,
                                   successMessage: String, failurePrefix: String) -> Error? {
        writes += 1
        return nil
    }
}

enum ShellModeTests {
    static func check(_ condition: Bool, _ name: String) { TransactionTests.check(condition, name) }

    static func key(_ code: UInt16, _ characters: String, _ flags: NSEvent.ModifierFlags = [],
                    repeat isRepeat: Bool = false, in window: NSWindow? = nil) -> NSEvent {
        NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags, timestamp: 0,
                         windowNumber: window?.windowNumber ?? 0, context: nil, characters: characters,
                         charactersIgnoringModifiers: characters, isARepeat: isRepeat, keyCode: code)!
    }

    static func menuItem(_ menu: NSMenu, _ action: Selector) -> NSMenuItem? {
        for item in menu.items {
            if item.action == action { return item }
            if let submenu = item.submenu, let found = menuItem(submenu, action) { return found }
        }
        return nil
    }

    static func enabled(_ app: AppDelegate, _ menu: NSMenu, _ action: Selector) -> Bool {
        guard let item = menuItem(menu, action) else { return false }
        return app.validateMenuItem(item)
    }

    static var deckDirectory: URL = {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("presto-shell-tests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }()

    // A deck open in Edit with stand-in web views and a hidden main window.
    static func editSession(preview: RecordingView = RecordingView(),
                            source: RecordingView = RecordingView()) -> (ShellApp, RecordingView, RecordingView) {
        let app = ShellApp()
        let url = deckDirectory.appendingPathComponent("index.html")
        let content = deckHTML(["One", "Two", "Three"])
        try? content.write(to: url, atomically: true, encoding: .utf8)
        app.mainWindow = app.makeMainWindow()
        app.beginDeckSession(url, content: content)
        app.isEditorMode = true
        app.previewWebView = preview
        app.editorWebView = source
        app.editorReady = true
        app.editorSplitView = NSSplitView()
        app.showMode(.edit)
        return (app, preview, source)
    }

    static func run() {
        launchTests()
        audiencePlanTests()
        modeTransitionTests()
        liveGuardTests()
        showLifecycleTests()
        staleViewTests()
        sheetTests()
        fullScreenOptionTests()
        escapeTests()
        undoRoutingTests()
        playTests()
        menuValidationTests()
        toolbarTests()
        inspectorSplitTests()
        injectionStatusTests()
        consoleStateTests()
        pageScriptTests()
        realPageTests()
        _ = try? FileManager.default.trashItem(at: deckDirectory, resultingItemURL: nil)
    }

    // MARK: Launch and opening

    static func launchTests() {
        let app = ShellApp()
        let deck = deckDirectory.appendingPathComponent("launch.html")
        try? deckHTML(["Launch"]).write(to: deck, atomically: true, encoding: .utf8)
        check(app.launchRequest(arguments: [deck.path]) == .present(deck),
              "launch: `Presto <deck>` opens Present (ready)")
        check(app.launchRequest(arguments: ["--edit", deck.path]) == .edit(deck), "launch: --edit opens Edit")
        check(app.launchRequest(arguments: ["--play", deck.path]) == .play(deck), "launch: --play goes live at once")
        check(app.launchRequest(arguments: ["-NSDocumentRevisionsDebugMode", "YES"]) == nil &&
              app.launchRequest(arguments: ["/no/such/deck.html"]) == nil && app.launchRequest(arguments: []) == nil,
              "launch: system flags and missing files open Decks")

        let folder = deckDirectory.appendingPathComponent("folder-deck", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let index = folder.appendingPathComponent("index.html")
        try? deckHTML(["Folder"]).write(to: index, atomically: true, encoding: .utf8)
        let notes = deckDirectory.appendingPathComponent("notes.txt")
        try? "notes".write(to: notes, atomically: true, encoding: .utf8)
        check(app.deckFile(for: folder) == index && app.deckFile(for: deck) == deck &&
              app.deckFile(for: notes) == nil && app.deckFile(for: deckDirectory.appendingPathComponent("empty")) == nil,
              "open: a folder opens its index.html, an .html file opens itself, anything else is ignored")
        check(app.deckDisplayTitle(content: "<html><head><title> Treatment &amp; Policy · TCD </title></head></html>",
                                   deckURL: deck) == "Treatment & Policy · TCD" &&
              app.deckDisplayTitle(content: "<html></html>", deckURL: index) == "folder-deck",
              "open: the toolbar shows the deck's <title>, else its folder name")
    }

    // MARK: Audience window plan

    static func audiencePlanTests() {
        let laptop = PrestoDisplay(id: 1, name: "Built-in Retina Display",
                                   frame: NSRect(x: 0, y: 0, width: 1512, height: 982),
                                   visibleFrame: NSRect(x: 0, y: 0, width: 1512, height: 944), isBuiltin: true)
        let projector = PrestoDisplay(id: 7, name: "LG UltraFine",
                                      frame: NSRect(x: 1512, y: 0, width: 1920, height: 1080),
                                      visibleFrame: NSRect(x: 1512, y: 0, width: 1920, height: 1055), isBuiltin: false)
        let second = PrestoDisplay(id: 9, name: "EPSON PJ",
                                   frame: NSRect(x: -1280, y: 0, width: 1280, height: 800),
                                   visibleFrame: NSRect(x: -1280, y: 0, width: 1280, height: 775), isBuiltin: false)

        let one = AppDelegate.audienceWindowPlan(displays: [laptop], consoleDisplayID: 1, preferredID: nil, forceDual: false)
        check(one.displayID == nil, "audience plan: one screen shows the deck in the main window itself")

        let forced = AppDelegate.audienceWindowPlan(displays: [laptop], consoleDisplayID: 1, preferredID: nil, forceDual: true)
        check(forced.displayID == 1 && !forced.borderless && forced.level == .normal &&
              forced.frame.minX > laptop.visibleFrame.midX - 8 && forced.consoleFrame?.maxX ?? 2000 < laptop.visibleFrame.midX,
              "audience plan: forced dual-screen testing splits one display into a titled audience half and the console half")

        let dual = AppDelegate.audienceWindowPlan(displays: [laptop, projector], consoleDisplayID: 1, preferredID: nil, forceDual: false)
        check(dual.displayID == 7 && dual.frame == projector.frame && dual.borderless &&
              dual.level == .screenSaver && dual.consoleFrame == nil,
              "audience plan: the projector gets its whole frame (no menu bar or Dock), borderless above the menu bar")

        let consoleOnProjector = AppDelegate.audienceWindowPlan(displays: [laptop, projector], consoleDisplayID: 7,
                                                                preferredID: nil, forceDual: false)
        check(consoleOnProjector.displayID == 7 && consoleOnProjector.consoleFrame == laptop.visibleFrame,
              "audience plan: a console window left on the projector moves to the laptop first")

        let swapped = AppDelegate.audienceWindowPlan(displays: [laptop, projector], consoleDisplayID: 1,
                                                     preferredID: 1, forceDual: false)
        check(swapped.displayID == 1 && swapped.frame == laptop.frame && swapped.consoleFrame == projector.visibleFrame,
              "audience plan: after Swap screens the chosen display wins and the console moves off it")

        let three = AppDelegate.audienceWindowPlan(displays: [laptop, projector, second], consoleDisplayID: 1,
                                                   preferredID: 9, forceDual: false)
        check(three.displayID == 9 && three.frame == second.frame,
              "audience plan: with several displays the one Duncan chose is used")

        let lidClosed = AppDelegate.audienceWindowPlan(displays: [projector, second], consoleDisplayID: 7,
                                                       preferredID: nil, forceDual: false)
        check(lidClosed.displayID == 9 && lidClosed.consoleFrame == nil,
              "audience plan: two external displays put the audience on the one without the console")

        // Swap screens before Start (the ready card's button).
        let pair = [laptop, projector]
        check(AppDelegate.readySwapAudienceID(displays: pair, consoleDisplayID: 1, plannedAudienceID: 7) == 1 &&
              AppDelegate.readySwapAudienceID(displays: pair, consoleDisplayID: 1, plannedAudienceID: 1) == 7,
              "ready swap: the audience takes the console's display, and a second Swap gives it back")
        check(AppDelegate.readySwapAudienceID(displays: [laptop], consoleDisplayID: 1, plannedAudienceID: nil) == nil,
              "ready swap: one display has nothing to swap")
        let afterSwap = AppDelegate.audienceWindowPlan(displays: pair, consoleDisplayID: 1, preferredID: 1, forceDual: false)
        check(AppDelegate.consoleDisplayAtStart(displays: pair, consoleDisplayID: 1, plan: afterSwap) == 7 &&
              AppDelegate.consoleDisplayAtStart(displays: pair, consoleDisplayID: 1, plan: dual) == 1,
              "ready swap: after Swap the console will start on the projector, so the card offers the laptop")
        let (ready, _, _) = editSession()
        ready.requestMode(.present)
        ready.handleConsoleMessage(type: "consoleSwapScreens", body: [:], webView: nil)
        check(ready.presentPhase == .ready, "ready swap: Swap before Start keeps the console ready (nothing goes live)")
        ready.requestMode(.edit)
    }

    // MARK: Mode transitions

    static func modeTransitionTests() {
        let (app, preview, source) = editSession()
        app.currentEditorSlideIndex = 2
        app.currentEditorStep = 1
        app.requestMode(.decks)
        check(app.mode == .decks && app.isEditorMode && app.previewWebView === preview && app.editorWebView === source,
              "modes: Edit to Decks keeps the edit views alive")
        check(preview.ran("commitActiveSession") && source.ran("getPendingEditorChange"),
              "modes: leaving Edit commits the visual session and the source buffer first")
        check(app.deckLibrary.lastPosition(app.currentDeckURL!).map { $0.slide == 2 && $0.step == 1 } == true,
              "modes: leaving Edit records the slide and step for Continue")
        app.requestMode(.edit)
        check(app.mode == .edit && app.previewWebView === preview, "modes: Decks back to Edit is instant (same views)")

        app.requestMode(.present)
        check(app.mode == .present && app.presentPhase == .ready && app.presentOrigin == .edit &&
              app.lastAudienceSlideIndex == 2 && app.lastAudienceStep == 1 && app.consoleWebView != nil,
              "modes: Present opens the console ready, at the edit view's slide and step")
        app.requestMode(.edit)
        check(app.mode == .edit && app.presentPhase == .none && app.consoleWebView == nil && app.previewWebView === preview,
              "modes: leaving the ready console returns to the same edit views")

        // Ending a talk returns to where it came from, at the slide it ended on.
        app.presentPhase = .live
        app.presentOrigin = .edit
        app.mode = .present
        app.lastAudienceSlideIndex = 1
        app.lastAudienceStep = 2
        app.isEndingPresentation = true
        app.endPresentation()
        check(app.mode == .edit && app.presentPhase == .none && app.currentEditorSlideIndex == 1 &&
              app.currentEditorStep == 2 && preview.ran("WS.goToSlide(1, 2)"),
              "modes: ending a talk started from Edit returns to Edit at the slide and step it ended on")
        check(app.deckLibrary.lastPosition(app.currentDeckURL!).map { $0.slide == 1 && $0.step == 2 } == true,
              "modes: the talk's last slide is recorded for Continue")

        app.presentPhase = .live
        app.presentOrigin = .decks
        app.mode = .present
        app.isEndingPresentation = true
        app.endPresentation()
        check(app.mode == .decks && app.isEditorMode, "modes: a talk started from Decks returns to Decks")

        let failing = RecordingView()
        failing.scriptError = NSError(domain: "TestWebKit", code: 1)
        let (stuck, _, _) = editSession(preview: failing)
        stuck.requestMode(.decks)
        check(stuck.mode == .edit, "modes: a failed save keeps Edit open")

        let (closing, _, _) = editSession()
        check(!closing.windowShouldClose(closing.mainWindow!) && closing.currentDeckURL == nil &&
              !closing.isEditorMode && closing.mode == .decks,
              "modes: closing the window saves, ends the deck session and leaves Presto running in Decks")

        let none = ShellApp()
        none.mainWindow = none.makeMainWindow()
        none.requestMode(.edit)
        none.requestMode(.present)
        check(none.mode == .decks && !none.modeEnabled(.edit) && !none.modeEnabled(.present),
              "modes: Edit and Present are unavailable with no deck open")
    }

    static func liveGuardTests() {
        let (app, _, _) = editSession()
        app.presentPhase = .live
        app.presentOrigin = .edit
        app.mode = .present
        app.requestMode(.edit)
        app.requestMode(.decks)
        check(app.mode == .present && !app.modeEnabled(.decks) && !app.modeEnabled(.edit),
              "live: the Decks and Edit modes are disabled during a talk")
        check(app.handleKey(key(19, "2", [.command])) == nil && app.handleKey(key(13, "w", [.command])) == nil &&
              app.mode == .present, "live: ⌘2 and ⌘W cannot leave the talk")
        check(!app.windowShouldClose(app.mainWindow!) && app.currentDeckURL != nil && app.presentPhase == .live,
              "live: closing the window does not end the talk")

        // ⌘Q needs a second press within 2 s while live.
        let quit = key(12, "q", [.command])
        check(app.handleKey(quit) == nil && app.hints.last == "Press ⌘Q again to quit",
              "live: the first ⌘Q only shows the hint")
        check(app.handleKey(key(12, "q", [.command], repeat: true)) == nil, "live: a held ⌘Q is not a second press")
        let second = key(12, "q", [.command])
        check(app.handleKey(second) === second, "live: a second ⌘Q within 2 seconds goes on to Quit")
        app.lastAudienceSlideIndex = 2
        app.lastAudienceStep = 1
        app.recordPositionBeforeQuit()
        check(app.deckLibrary.lastPosition(app.currentDeckURL!).map { $0.slide == 2 && $0.step == 1 } == true,
              "quit: quitting during a talk records its slide and step for Continue")
        app.presentPhase = .none
        app.mode = .edit
        app.currentEditorSlideIndex = 1
        app.currentEditorStep = 0
        app.recordPositionBeforeQuit()
        check(app.deckLibrary.lastPosition(app.currentDeckURL!).map { $0.slide == 1 && $0.step == 0 } == true,
              "quit: quitting from Edit records the edit view's slide and step")
        let fromDecks = LiveApp.session("quit")
        fromDecks.presentPhase = .live
        fromDecks.lastAudienceSlideIndex = 2
        check(fromDecks.applicationShouldTerminate(NSApp) == .terminateNow &&
              fromDecks.deckLibrary.lastPosition(fromDecks.currentDeckURL!).map { $0.slide == 2 } == true,
              "quit: applicationShouldTerminate records the position before it answers")
        fromDecks.presentPhase = .none
    }

    // Real goLive and teardown, round after round.
    static func showLifecycleTests() {
        let app = LiveApp.session("lifecycle")
        let console = RecordingView()
        app.consoleWebView = console
        app.presentPhase = .ready
        app.consoleTextEditing = true
        app.goLive(index: 1, step: 0, origin: .decks)
        check(app.presentPhase == .live && !app.consoleTextEditing && console.ran("document.activeElement"),
              "live: ⌘↩ from the talk-length field hands the keys to the talk and blurs the field")
        guard let firstAudience = app.audienceWebView else {
            check(false, "live: the show has an audience view")
            return
        }
        app.webViewWebContentProcessDidTerminate(firstAudience)
        check(app.webContentRecoveryAttempts[.audience] == 1, "recovery: the audience view is reloaded once in a show")
        app.endShow()

        app.goLive(index: 0, step: 0, origin: .decks)
        if let secondAudience = app.audienceWebView {
            // StubbedUIApp fails the run if the recovery-failure sheet is reached.
            app.webViewWebContentProcessDidTerminate(secondAudience)
        }
        check(app.webContentRecoveryAttempts[.audience] == 1,
              "recovery: the next show gets its own audience recovery (the allowance is per show)")
        app.endShow()

        app.webContentRecoveryAttempts[.presenter] = 1
        app.ensureConsoleWebView()
        check(app.consoleWebView != nil && app.webContentRecoveryAttempts[.presenter] == nil,
              "recovery: a new console gets its own presenter recovery")
        _ = app.cleanupPresentation()
    }

    // The one-screen show's native full screen hides the menu bar and Dock.
    static func fullScreenOptionTests() {
        let (app, _, _) = editSession()
        guard let window = app.mainWindow else { return check(false, "full screen: a main window") }
        let proposed: NSApplication.PresentationOptions = [.fullScreen, .autoHideMenuBar, .autoHideDock]
        check(app.window(window, willUseFullScreenPresentationOptions: proposed) == proposed,
              "full screen: outside a one-screen show the window keeps the normal full-screen options")
        app.presentPhase = .live
        app.audienceOneScreen = true
        let show = app.window(window, willUseFullScreenPresentationOptions: proposed)
        check(show == [.fullScreen, .hideDock, .hideMenuBar] && !show.contains(.autoHideMenuBar) &&
              !show.contains(.autoHideDock),
              "full screen: a one-screen show hides the menu bar and Dock outright (hideMenuBar with hideDock, as AppKit requires)")
        check(app.window(NSWindow(), willUseFullScreenPresentationOptions: proposed) == proposed,
              "full screen: only the main window's show hides them")
        app.presentPhase = .none
        app.audienceOneScreen = false
        app.windowDidExitFullScreen(Notification(name: NSWindow.didExitFullScreenNotification, object: window))
        check(!app.oneScreenShowHidDock && window.styleMask.contains(.miniaturizable) &&
              app.window(window, willUseFullScreenPresentationOptions: proposed) == proposed,
              "full screen: after the show the window can minimize and full screen is normal again")
    }

    // A sheet would take the keyboard from a live talk: they wait for its end.
    static func sheetTests() {
        let (app, _, source) = editSession()
        app.presentPhase = .live
        app.presentOrigin = .edit
        app.mode = .present
        app.editorRecoveryNeedsWarning = true
        app.handleEditorMessage(type: "ready", body: [:], webView: nil)
        app.webContentRecoveryAttempts[.editor] = 1
        app.webViewWebContentProcessDidTerminate(source)
        check(app.sheets.isEmpty, "sheets: Editor recovered and a failed recovery wait while the talk is live")
        app.isEndingPresentation = true
        app.finishPresentationTeardown()
        check(app.sheets == ["editor recovered", "editor"], "sheets: they appear once the talk has ended, in order")
        app.webViewWebContentProcessDidTerminate(source)
        check(app.sheets.count == 3, "sheets: outside a talk they appear at once")
    }

    // Torn-down console and audience views: blanked, and kept one round only.
    static func staleViewTests() {
        let (app, _, _) = editSession()
        func showRound() -> (RecordingView, RecordingView) {
            let console = RecordingView()
            let audience = RecordingView()
            app.consoleWebView = console
            app.audienceWebView = audience
            app.presentPhase = .live
            app.presentOrigin = .edit
            app.mode = .present
            app.isEndingPresentation = true
            app.finishPresentationTeardown()
            return (console, audience)
        }
        func kept(_ view: WKWebView) -> Bool { app.staleWebViews.contains { $0 === view } }
        let (console1, audience1) = showRound()
        check(console1.loads.last?.absoluteString == "about:blank" && audience1.loads.last?.absoluteString == "about:blank" &&
              kept(console1) && kept(audience1),
              "teardown: an ended show's console and audience load about:blank (their clocks stop) and stay referenced")
        let (console2, audience2) = showRound()
        check(!kept(console1) && !kept(audience1) && kept(console2) && kept(audience2) && app.staleWebViews.count == 2,
              "teardown: the next show releases the previous show's views; only the latest round is kept")
        let ready = RecordingView()
        app.consoleWebView = ready
        app.presentPhase = .ready
        app.leavePresentReady()
        check(app.staleWebViews.count == 1 && kept(ready) && ready.loads.last?.absoluteString == "about:blank",
              "teardown: closing the ready console is a round of its own")
    }

    // MARK: Escape

    static func escapeTests() {
        let (app, preview, _) = editSession()
        app.countEndRequests = true
        check(app.handleKey(key(53, "\u{1b}")) == nil && app.handleKey(key(53, "\u{1b}")) == nil,
              "escape in Edit: the key is handled natively")
        check(preview.ran("PrestoEdit.handleEscape") && app.endRequests == 0 && app.mode == .edit && app.isEditorMode,
              "escape in Edit: it asks the visual editor (popover, deselect, overview) and never leaves the editor")
        check(AppDelegate.editEscapeJS.contains("ws-overview-active") && !AppDelegate.editEscapeJS.contains("'ws-overview'"),
              "escape in Edit: without the visual editor it closes the overview by its real class (BACKLOG 1)")
        app.escapeKeyFromMenu(nil)
        app.endPresentationFromMenu(nil)
        check(app.endRequests == 0 && app.mode == .edit,
              "escape in Edit: the hidden Escape item and ⌘. do nothing outside a talk")
        app.wysiwygHasSelection = true
        let selected = key(53, "\u{1b}")
        check(app.handleKey(selected) === selected, "escape in Edit: with a selection the page deselects itself")
        app.wysiwygHasSelection = false

        app.presentPhase = .live
        app.mode = .present
        let start = app.hints.count
        _ = app.handleKey(key(53, "\u{1b}"))
        check(app.endRequests == 0 && app.hints.count == start + 1 && app.hints.last == "Press Escape again to end the presentation",
              "escape live: the first press only shows the hint")
        _ = app.handleKey(key(53, "\u{1b}", repeat: true))
        check(app.endRequests == 0, "escape live: a held key is not a second press")
        _ = app.handleKey(key(53, "\u{1b}"))
        check(app.endRequests == 1, "escape live: a second press within 2 seconds ends the talk")
        app.escapeKeyFromMenu(nil)
        _ = app.handleKey(key(53, "\u{1b}"))
        check(app.endRequests == 2, "escape live: the hidden menu item goes through the same two-press guard")
        app.handleAudienceMessage(type: "endPresentationRequested", body: [:])
        check(app.endRequests == 2, "escape live: the audience page's Escape bridge arms the guard too")
        _ = app.handleKey(key(47, ".", [.command]))
        check(app.endRequests == 3, "live: ⌘. ends the talk")
        _ = app.handleKey(key(11, "b"))
        check(app.blackoutActive, "live: B blacks out the audience")
        _ = app.handleKey(key(11, "b"))
        check(!app.blackoutActive, "live: B again brings the slide back")
        app.consoleTextEditing = true
        let typed = key(124, "\u{F703}")
        check(app.handleKey(typed) === typed, "live: arrows reach the console's talk length field while it has focus")
        app.consoleTextEditing = false

        // The slide grid closes before the guard is armed (Edit's order).
        let closesGrid = "WS.toggleOverview(); } finally"
        let console = RecordingView()
        console.answers = [closesGrid: true]
        app.consoleWebView = console
        app.lastPresentationExitEscape = nil
        let hintsBefore = app.hints.count
        _ = app.handleKey(key(53, "\u{1b}"))
        _ = app.handleKey(key(53, "\u{1b}"))
        check(console.ran("current-slide") && app.hints.count == hintsBefore && app.endRequests == 3 &&
              app.lastPresentationExitEscape == nil,
              "escape live, two screens: with the console's grid open Escape closes it and never arms the guard")
        console.answers = [closesGrid: false]
        _ = app.handleKey(key(53, "\u{1b}"))
        check(app.hints.count == hintsBefore + 1 && app.endRequests == 3,
              "escape live, two screens: with the grid closed the first press shows the hint again")
        app.lastPresentationExitEscape = nil
        let audience = RecordingView()
        audience.answers = [closesGrid: true]
        app.audienceWebView = audience
        app.audienceOneScreen = true
        _ = app.handleKey(key(53, "\u{1b}"))
        _ = app.handleKey(key(53, "\u{1b}"))
        check(audience.ran("window.WS") && app.hints.count == hintsBefore + 1 && app.endRequests == 3,
              "escape live, one screen: the audience page's grid closes first, twice, with no hint and no end")
        app.audienceOneScreen = false
        app.audienceWebView = nil
        app.consoleWebView = nil
        app.presentPhase = .none
    }

    // MARK: Undo

    static func undoRoutingTests() {
        let (app, preview, source) = editSession()
        app.editHistoryAvailable = true
        check(app.handleKey(key(6, "z", [.command])) == nil && preview.ran("PrestoEdit.undo()"),
              "undo: ⌘Z in Edit goes to the unified history")
        check(app.handleKey(key(6, "Z", [.command, .shift])) == nil && preview.ran("PrestoEdit.redo()"),
              "undo: ⇧⌘Z redoes")
        app.wysiwygTextEditingActive = true
        let typing = key(6, "z", [.command])
        check(app.handleKey(typing) === typing, "undo: during a visual text session the page undoes the typing")
        app.wysiwygTextEditingActive = false
        app.editHistoryAvailable = false
        let old = key(6, "z", [.command])
        check(app.handleKey(old) === old, "undo: without PrestoEdit the page keeps its own ⌘Z")
        app.editHistoryAvailable = true

        source.pending = #"{"dirty":true,"index":0,"html":"<h1>Typed in source</h1>"}"#
        app.sourceEditorDirty = true
        let before = preview.scripts.count
        app.undoEdit(nil)
        let after = Array(preview.scripts[before...])
        check(app.fileContent?.contains("Typed in source") == true &&
              after.firstIndex(where: { $0.contains("recordExternalChange") || $0.contains("origin: \"source\"") }) ?? 99 <
              after.firstIndex(where: { $0.contains("PrestoEdit.undo()") }) ?? -1,
              "undo: uncommitted source typing is committed into the history before ⌘Z takes it back")
        app.sourceEditorDirty = false
        source.pending = #"{"dirty":false,"index":0,"html":""}"#

        let refineBefore = preview.scripts.count
        app.handleEditorMessage(type: "refineUndo", body: [:], webView: nil)
        check(preview.scripts[refineBefore...].contains { $0.contains("PrestoEdit.undo()") },
              "undo: Refine's Undo last uses the same history")

        preview.answers["replacePrestoEditSlideHTML"] = true
        let refineStart = preview.scripts.count
        let preRefine = app.sectionInfos[0].content
        app.applyRefineResult(slideIndex: 0, taskID: UUID(), instructions: "Shorter", preHTML: preRefine,
                              refinedRaw: "<section class=\"slide\"><h1>Refined</h1></section>")
        let refineScripts = Array(preview.scripts[refineStart...])
        let patchedAt = refineScripts.firstIndex { $0.contains("replacePrestoEditSlideHTML") } ?? 99
        let historyReadAt = refineScripts.lastIndex { $0.contains("PrestoEdit.refineHistory") } ?? -1
        check(patchedAt < historyReadAt,
              "undo: after a Refine the inspector's earlier-changes list is read once the Refine is recorded")
        let refusalStart = preview.scripts.count
        app.reportHistoryRefusal(index: 0, saveId: 7, message: "This slide changed since; nothing was undone")
        let refusal = Array(preview.scripts[refusalStart...])
        let failedAt = refusal.firstIndex { $0.contains("saveFailed(7") } ?? 99
        let patchAt = refusal.firstIndex { $0.contains("replacePrestoEditSlideHTML") } ?? -1
        check(patchAt >= 0 && failedAt < patchAt && refusal[patchAt].contains("origin: \"history\""),
              "undo: a refused history change rolls the editor back first, then shows Presto's copy without clearing the slide's history")
        preview.answers = [:]

        let menu = app.buildMainMenu()
        app.applyUndoState(["canUndo": true, "canRedo": false, "undoLabel": "Undo Refine", "redoLabel": "Redo"])
        let undoItem = menuItem(menu, #selector(AppDelegate.undoEdit(_:)))!
        check(app.validateMenuItem(undoItem) && undoItem.title == "Undo Refine" &&
              !enabled(app, menu, #selector(AppDelegate.redoEdit(_:))),
              "undo: the Edit menu shows the history's own titles and enables from undoState")

        // Typing in the Refine prompt: ⌘Z and the caret moves stay there.
        app.editorSidebarVisible = true
        app.mainWindow?.contentView?.addSubview(source)
        _ = app.mainWindow?.makeFirstResponder(source)
        let focused = app.sourcePaneIsFirstResponder
        app.handleEditorMessage(type: "refineTextEditing", body: ["active": true], webView: nil)
        let undosBefore = preview.scripts.filter { $0.contains("PrestoEdit.undo()") }.count
        let promptUndo = key(6, "z", [.command], in: app.mainWindow)
        let promptEnd = key(124, "\u{F703}", [.command], in: app.mainWindow)
        check(focused && app.refineTextEditing && promptUndo.window === app.mainWindow && app.handleKey(promptUndo) === promptUndo &&
              app.handleKey(promptEnd) === promptEnd &&
              preview.scripts.filter { $0.contains("PrestoEdit.undo()") }.count == undosBefore,
              "undo: ⌘Z and ⌘→ in the Refine prompt stay in the prompt; the slide is not undone")
        check(app.validateMenuItem(undoItem) && undoItem.title == "Undo Typing",
              "undo: the Edit menu offers Undo Typing while the Refine prompt has focus")
        app.handleEditorMessage(type: "refineTextEditing", body: ["active": false], webView: nil)
        let afterPrompt = key(6, "z", [.command], in: app.mainWindow)
        check(app.handleKey(afterPrompt) == nil, "undo: once the prompt loses focus ⌘Z is the slide history again")
    }

    static func playTests() {
        let (app, _, _) = editSession()
        app.currentEditorSlideIndex = 2
        app.currentEditorStep = 1
        check(app.handleKey(key(36, "\r", [.command])) == nil && app.lives.last.map { $0.index == 2 && $0.step == 1 && $0.origin == .edit } == true,
              "play: ⌘↩ goes live at the current slide and step")
        check(app.handleKey(key(36, "\r", [.command, .shift])) == nil && app.lives.last.map { $0.index == 0 && $0.step == 0 } == true,
              "play: ⇧⌘↩ goes live from the start")
        app.wysiwygTextEditingActive = true
        let commit = key(36, "\r", [.command])
        let count = app.lives.count
        check(app.handleKey(commit) === commit && app.lives.count == count,
              "play: ⌘↩ commits an equation or text field instead of playing")
        app.wysiwygTextEditingActive = false
    }

    // MARK: Menus

    static func menuValidationTests() {
        let empty = ShellApp()
        let menu = empty.buildMainMenu()
        check(enabled(empty, menu, #selector(AppDelegate.showDecksMode(_:))) &&
              !enabled(empty, menu, #selector(AppDelegate.showEditMode(_:))) &&
              !enabled(empty, menu, #selector(AppDelegate.showPresentMode(_:))) &&
              !enabled(empty, menu, #selector(AppDelegate.playFromCurrentSlide(_:))) &&
              !enabled(empty, menu, #selector(AppDelegate.exportPDF(_:))) &&
              !enabled(empty, menu, #selector(AppDelegate.endPresentationFromMenu(_:))) &&
              !enabled(empty, menu, #selector(AppDelegate.nextStepFromMenu(_:))) &&
              enabled(empty, menu, #selector(AppDelegate.openDeckFromMenu(_:))),
              "menus: with no deck only Decks, Open and library items are enabled")
        check(menuItem(menu, #selector(AppDelegate.refineWithAI(_:)))?.keyEquivalent == "j" &&
              menuItem(menu, #selector(AppDelegate.toggleInspector(_:)))?.keyEquivalentModifierMask == [.command, .option] &&
              menuItem(menu, #selector(AppDelegate.addTextBox(_:)))?.keyEquivalent == "t" &&
              menuItem(menu, #selector(AppDelegate.showPresentMode(_:)))?.keyEquivalent == "3" &&
              menuItem(menu, #selector(AppDelegate.playFromStart(_:)))?.keyEquivalentModifierMask == [.command, .shift] &&
              menuItem(menu, #selector(AppDelegate.endPresentationFromMenu(_:)))?.keyEquivalent == ".",
              "menus: Refine is ⌘J (⌘I stays Italic), Inspector ⌥⌘I, Text Box ⌥⌘T, Present ⌘3, Play from Start ⇧⌘↩, End ⌘.")

        let (app, _, _) = editSession()
        let editMenu = app.buildMainMenu()
        check(enabled(app, editMenu, #selector(AppDelegate.playFromCurrentSlide(_:))) &&
              enabled(app, editMenu, #selector(AppDelegate.toggleInspector(_:))) &&
              enabled(app, editMenu, #selector(AppDelegate.nextStepFromMenu(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.endPresentationFromMenu(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.escapeKeyFromMenu(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.toggleBlackScreen(_:))),
              "menus: in Edit, Play and the edit views are enabled; End, Escape and Black Screen are not")
        check(!enabled(app, editMenu, #selector(AppDelegate.editEquation(_:))), "menus: Edit Equation needs a selected equation")
        app.selectionKind = "equation"
        check(enabled(app, editMenu, #selector(AppDelegate.editEquation(_:))), "menus: Edit Equation is enabled for an equation")
        app.wysiwygHasSelection = true
        check(!enabled(app, editMenu, #selector(AppDelegate.nextStepFromMenu(_:))),
              "menus: the bare arrow equivalents step aside while the visual editor has a selection")
        app.wysiwygHasSelection = false
        app.wysiwygTextEditingActive = true
        check(!enabled(app, editMenu, #selector(AppDelegate.playFromCurrentSlide(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.playFromStart(_:))),
              "menus: during a visual text session ⌘↩ and ⇧⌘↩ reach the page (equation Apply), not Play")
        app.wysiwygTextEditingActive = false
        if let source = app.editorWebView {
            app.editorSidebarVisible = true
            app.mainWindow?.contentView?.addSubview(source)
            _ = app.mainWindow?.makeFirstResponder(source)
            check(app.sourcePaneIsFirstResponder && !enabled(app, editMenu, #selector(AppDelegate.playFromCurrentSlide(_:))),
                  "menus: with the inspector focused ⌘↩ reaches the page (Refine's send), not Play")
            _ = app.mainWindow?.makeFirstResponder(nil)
            app.editorSidebarVisible = false
            check(enabled(app, editMenu, #selector(AppDelegate.playFromCurrentSlide(_:))),
                  "menus: Play is back once the slide has the keys")
        }

        app.mode = .decks
        check(!enabled(app, editMenu, #selector(AppDelegate.playFromCurrentSlide(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.playFromStart(_:))),
              "menus: in Decks, Return and ⌘Return belong to the cards")
        app.mode = .present
        app.presentPhase = .live
        check(!enabled(app, editMenu, #selector(AppDelegate.showDecksMode(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.showEditMode(_:))) &&
              enabled(app, editMenu, #selector(AppDelegate.endPresentationFromMenu(_:))) &&
              enabled(app, editMenu, #selector(AppDelegate.escapeKeyFromMenu(_:))) &&
              enabled(app, editMenu, #selector(AppDelegate.toggleBlackScreen(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.reloadPresenter(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.openDeckFromMenu(_:))) &&
              !enabled(app, editMenu, #selector(AppDelegate.playFromCurrentSlide(_:))),
              "menus: while live, End, Escape and Black Screen are enabled; mode changes, Reload and Open are not")
        app.presentPhase = .none
    }

    static func toolbarTests() {
        check(AppDelegate.toolbarIdentifiers(for: .decks) == [AppDelegate.toolbarMode, .flexibleSpace,
                                                               AppDelegate.toolbarSearch, AppDelegate.toolbarOpen] &&
              AppDelegate.toolbarIdentifiers(for: .edit).first == AppDelegate.toolbarRail &&
              AppDelegate.toolbarIdentifiers(for: .edit).last == AppDelegate.toolbarPlay &&
              AppDelegate.toolbarIdentifiers(for: .present).suffix(3) == [AppDelegate.toolbarSwap, AppDelegate.toolbarBlackout,
                                                                          AppDelegate.toolbarEnd],
              "toolbar: each mode has the mockup's items")
        let (app, _, _) = editSession()
        check(app.mainWindow?.toolbar?.identifier == "PrestoToolbar.edit" &&
              NSApp.appearance == nil || app.mainWindow?.appearance?.name == .aqua,
              "toolbar: Edit installs its toolbar on a light window")
        if let control = app.toolbarItems[AppDelegate.toolbarMode]?.view as? NSSegmentedControl {
            check(control.selectedSegment == 1 && control.isEnabled(forSegment: 2),
                  "toolbar: the mode control shows Edit with Present available")
        } else {
            check(app.mainWindow?.toolbar?.items.isEmpty != false, "toolbar: mode control (items load lazily offscreen)")
        }
        check(app.mainWindow?.backgroundColor == .prestoCanvas, "toolbar: no black window background")

        check(AppDelegate.toolbarDeckTitle("Treatment-Effect Persistence and Policy Comparisons - TCD 2026") ==
                  "Treatment-Effect Persistence and Policy Comparisons · TCD 2026" &&
              AppDelegate.toolbarDeckTitle("Menstrual Stigma, Hygiene, and Human Capital (BREAD/ITAM/CAF 2026)") ==
                  "Menstrual Stigma, Hygiene, and Human Capital · BREAD/ITAM/CAF 2026" &&
              AppDelegate.toolbarDeckTitle("Discussion: Norms Rewritten?") == "Discussion: Norms Rewritten?" &&
              AppDelegate.toolbarDeckTitle("Long-run effects") == "Long-run effects",
              "toolbar: the deck title reads \"Headline · Occasion\" (Decks' split), else the headline")
        app.deckTitle = "Treatment-Effect Persistence and Policy Comparisons - TCD 2026"
        app.updateToolbarTitle()
        if let label = app.toolbarItems[AppDelegate.toolbarTitle]?.view as? NSTextField {
            check(label.stringValue.hasPrefix("Treatment-Effect Persistence and Policy Comparisons · TCD 2026") &&
                  app.mainWindow?.title.hasPrefix("Presto — ") == true,
                  "toolbar: Edit shows the split title; the window title keeps the folder name")
        } else {
            check(app.mainWindow?.toolbar?.items.isEmpty != false, "toolbar: title label (items load lazily offscreen)")
        }
    }

    // MARK: Inspector split

    // The real edit surfaces (a temporary deck): a hidden inspector leaves no
    // divider hairline on the canvas at its old edge.
    static func inspectorSplitTests() {
        let app = ShellApp()
        let url = deckDirectory.appendingPathComponent("split.html")
        let content = deckHTML(["One", "Two"])
        try? content.write(to: url, atomically: true, encoding: .utf8)
        app.mainWindow = app.makeMainWindow()
        app.beginDeckSession(url, content: content)
        check(app.ensureEditSurfaces(), "inspector: Edit builds its preview and inspector")
        guard let split = app.editorSplitView, let preview = app.previewWebView, let inspector = app.editorWebView else {
            check(false, "inspector: the split view exists")
            return
        }
        // setPosition asks the width limits too and un-hides a collapsed pane;
        // the pane must end hidden with the canvas over the whole split.
        func canvasFills() -> Bool {
            inspector.isHidden && preview.frame.minX == 0 && abs(preview.frame.width - split.bounds.width) < 0.5
        }
        check(app.splitView(split, shouldHideDividerAt: 0) && canvasFills(),
              "inspector: hidden at creation, its divider hidden and the canvas filling the split")
        app.showMode(.edit)
        check(split.superview === app.mainWindow?.contentView && canvasFills() &&
              app.splitView(split, shouldHideDividerAt: 0),
              "inspector: still fully collapsed once Edit fills the window")
        app.setEditorSourceSidebarVisible(true, focusSource: false)
        check(!inspector.isHidden && !app.splitView(split, shouldHideDividerAt: 0) &&
              abs(inspector.frame.width - app.inspectorWidth) < 1.5 &&
              abs(preview.frame.width + split.dividerThickness + inspector.frame.width - split.bounds.width) < 1,
              "inspector: shown at its width beside the canvas, with a visible divider")
        app.setEditorSourceSidebarVisible(false, focusSource: false)
        check(app.splitView(split, shouldHideDividerAt: 0) && canvasFills(),
              "inspector: hiding collapses it fully and hides its divider")
        _ = app.cleanupPresentation()
    }

    // MARK: Injection status

    static func injectionStatusTests() {
        let (app, _, _) = editSession()
        let page = RecordingView()
        app.previewWebView = page
        app.wysiwygTextEditingActive = true
        app.wysiwygHasSelection = true
        app.missingResources = ["wysiwyg.js"]
        app.injectEditPreview(into: page, generation: app.sessionGeneration)
        check(!app.wysiwygTextEditingActive && !app.wysiwygHasSelection,
              "injection: stale key-gating flags are reset before anything is read (BACKLOG 2)")
        check(app.editInjectionProblem?.contains("wysiwyg.js") == true &&
              app.editInjectionProblem?.hasPrefix("Visual editing unavailable") == true,
              "injection: an unreadable wysiwyg.js is named in the edit view's status")
        check(!page.ran("__prestoWysiwyg.enable"), "injection: no half-working editor is started without wysiwyg.js")

        let complete = RecordingView()
        app.previewWebView = complete
        app.missingResources = []
        app.injectEditPreview(into: complete, generation: app.sessionGeneration)
        let order = ["presto-ui-tokens", "/* edit-layout.js */", "presto-edit-chrome-style", "/* edit-chrome.js */",
                     "window.replacePrestoEditSlideHTML", "/* wysiwyg-guides.js */", "/* wysiwyg.js */",
                     "__prestoWysiwyg.enable"].map { fragment in complete.scripts.firstIndex { $0.contains(fragment) } ?? -1 }
        check(app.editInjectionProblem == nil && !order.contains(-1) && order == order.sorted(),
              "injection: tokens, edit-layout.js, edit chrome, Presto's script, then the visual editor, in that order")
        check(app.editHistoryAvailable && app.editChromeAvailable,
              "injection: Presto notes when PrestoEdit and PrestoEditChrome are present")

        let layoutless = RecordingView()
        app.previewWebView = layoutless
        app.missingResources = ["edit-layout.js"]
        app.injectEditPreview(into: layoutless, generation: app.sessionGeneration)
        check(app.editInjectionProblem == "Could not load edit-layout.js",
              "injection: a missing layout script is reported without blocking editing")
    }

    // MARK: Console state

    static func consoleStateTests() {
        var timer = PresentationTimer()
        let t0 = Date(timeIntervalSince1970: 1_000)
        timer.start(now: t0)
        timer.pause(now: t0.addingTimeInterval(90))
        timer.start(now: t0.addingTimeInterval(100))
        check(timer.running && timer.accumulatedMs == 90_000 &&
              abs(timer.elapsedMs(now: t0.addingTimeInterval(130)) - 120_000) < 1,
              "timer: accumulated time plus the current run")
        timer.reset()
        check(!timer.running && timer.elapsedMs() == 0, "timer: reset is visible and complete")

        let (app, _, _) = editSession()
        app.setTalkMinutes(45)
        app.presentPhase = .ready
        app.deckOutline = DeckOutline(mainSlideCount: 2, totalSlides: 3,
                                      sections: [["title": "Intro", "first": 0, "last": 1]],
                                      slides: [["title": "One", "appendix": false, "number": "1", "maxStep": 2],
                                               ["title": "Two", "appendix": false, "number": "2", "maxStep": 0],
                                               ["title": "Extra", "appendix": true, "label": "A1", "maxStep": 0]])
        app.lastAudienceSlideIndex = 2
        app.talkTimer = PresentationTimer(accumulatedMs: 5_000, startedAt: nil)
        let state = app.consoleState()
        let timerState = state["timer"] as? [String: Any]
        check(state["phase"] as? String == "ready" && state["talkMinutes"] as? Int == 45 &&
              state["isAppendix"] as? Bool == true && state["appendixLabel"] as? String == "A1" &&
              state["mainSlideCount"] as? Int == 2 && (state["sections"] as? [[String: Any]])?.count == 1 &&
              timerState?["elapsedMs"] as? Double == 5_000 && timerState?["startedAtMs"] is NSNull &&
              state["displays"] is [Any],
              "console state: phase, talk length from Presto's settings, appendix label, sections and timer")
        check(app.deckLibrary.talkMinutes(app.currentDeckURL!) == 45 && !(app.fileContent ?? "").contains("45"),
              "console state: talk length lives in the deck library, never in the deck file")
        app.handleConsoleMessage(type: "consoleTimer", body: ["action": "start"], webView: nil)
        check(app.talkTimer.running, "console: Start runs the Swift-owned timer")
        app.handleConsoleMessage(type: "consoleTimer", body: ["action": "reset"], webView: nil)
        check(app.talkTimer.running && app.talkTimer.accumulatedMs == 0, "console: reset keeps a running timer running from zero")
        app.handleConsoleMessage(type: "consoleNavigate", body: ["index": 1, "step": 0], webView: nil)
        check(app.lastAudienceSlideIndex == 1 && app.lastAudienceStep == 0,
              "console: picking a section before Start moves where the talk begins")
        app.presentPhase = .none
    }

    // MARK: Page scripts

    static func pageScriptTests() {
        let context = JSContext()!
        var errors: [String] = []
        context.exceptionHandler = { _, exception in errors.append(exception?.toString() ?? "error") }
        context.evaluateScript("""
            function slide(title, appendix, number) {
                return {
                    title: title, dataset: number ? { wsMainNumber: number } : {},
                    classList: { contains: function (c) { return c === 'appendix' && appendix; } },
                    querySelector: function () { return { textContent: '  ' + title + '\\n ' }; }
                };
            }
            var slides = [slide('Title', false, '1'), slide('Plan', false, '2'), slide('Data', false, '3'),
                          slide('More', false, '4'), slide('Spec', true), slide('Recovery', true)];
            var steps = new Map(); steps.set(slides[1], 2);
            var WS = { slides: slides, slideSteps: steps,
                       sections: [{ name: 'Intro', slideIndex: 0 }, { name: 'Data', slideIndex: 2 }, { name: 'Data', slideIndex: 3 }] };
            """)
        let json = context.evaluateScript(AppDelegate.deckOutlineJS(wsExpression: "WS"))?.toString() ?? ""
        let outline = (try? JSONSerialization.jsonObject(with: Data(json.utf8))) as? [String: Any]
        let sections = outline?["sections"] as? [[String: Any]] ?? []
        let slides = outline?["slides"] as? [[String: Any]] ?? []
        check(outline?["mainSlideCount"] as? Int == 4 && sections.count == 2 &&
              sections[0]["first"] as? Int == 0 && sections[0]["last"] as? Int == 1 &&
              sections[1]["title"] as? String == "Data" && sections[1]["last"] as? Int == 3 &&
              slides[4]["label"] as? String == "A1" && slides[1]["maxStep"] as? Int == 2 &&
              slides[0]["title"] as? String == "Title" && errors.isEmpty,
              "outline: sections are 0-based inclusive main-slide ranges; appendix slides are A1, A2")

        // A heading "Policy Comparisons:<br>Evidence from <KaTeX 4,051> Meta-Analyses",
        // in a minimal DOM (clone, remove, replaceWith, querySelectorAll, textContent).
        let titles = JSContext()!
        titles.exceptionHandler = { _, exception in errors.append(exception?.toString() ?? "error") }
        titles.evaluateScript("""
            function N(tag, cls, kids) {
                var n = { tag: tag, cls: cls || '', kids: [] };
                (kids || []).forEach(function (k) {
                    if (typeof k === 'string') k = { text: k };
                    k.parent = n;
                    n.kids.push(k);
                });
                n.cloneNode = function () {
                    return N(tag, cls, n.kids.map(function (k) { return k.text !== undefined ? k.text : k.cloneNode(true); }));
                };
                n.remove = function () { n.parent.kids.splice(n.parent.kids.indexOf(n), 1); };
                n.replaceWith = function (s) { n.parent.kids.splice(n.parent.kids.indexOf(n), 1, { text: s, parent: n.parent }); };
                n.querySelectorAll = function (sel) {
                    var out = [];
                    (function walk(x) {
                        x.kids.forEach(function (k) {
                            if (k.text !== undefined) return;
                            if (sel === 'br' ? k.tag === 'br' : (sel === '.katex-mathml' && k.cls === 'katex-mathml')) out.push(k);
                            walk(k);
                        });
                    })(n);
                    return out;
                };
                Object.defineProperty(n, 'textContent', { get: function () {
                    return n.kids.map(function (k) { return k.text !== undefined ? k.text : k.textContent; }).join('');
                } });
                return n;
            }
            var heading = N('h1', '', ['Policy Comparisons:', N('br'), 'Evidence from ',
                N('span', 'katex', [N('span', 'katex-mathml', ['4,051']), N('span', 'katex-html', ['4,051'])]),
                '  Meta-Analyses']);
            var WS = { slides: [{ dataset: { wsMainNumber: '1' },
                                  classList: { contains: function () { return false; } },
                                  querySelector: function () { return heading; } }],
                       sections: [] };
            """)
        let titleJSON = titles.evaluateScript(AppDelegate.deckOutlineJS(wsExpression: "WS"))?.toString() ?? ""
        let titleOutline = (try? JSONSerialization.jsonObject(with: Data(titleJSON.utf8))) as? [String: Any]
        let firstTitle = (titleOutline?["slides"] as? [[String: Any]])?.first?["title"] as? String
        check(firstTitle == "Policy Comparisons: Evidence from 4,051 Meta-Analyses" &&
              titles.evaluateScript("heading.textContent")?.toString() == "Policy Comparisons:Evidence from 4,0514,051  Meta-Analyses" &&
              errors.isEmpty,
              "outline: slide titles read <br> as a space and drop KaTeX's MathML copy, without changing the slide")

        let console = JSContext()!
        console.exceptionHandler = { _, exception in errors.append(exception?.toString() ?? "error") }
        console.evaluateScript("var calls = [];")
        let lastSlide = ShellApp().lastSlideNavigationCommand()
        console.evaluateScript("""
            (function () {
                var WS = { slides: [1, 2, 3, 4],
                           mainSlides: [{ dataset: { wsIndex: '0' } }, { dataset: { wsIndex: '2' } }],
                           goToSlide: function (i, s) { calls.push(i + ':' + s); } };
                \(lastSlide);
            })()
            """)
        check(console.evaluateScript("calls.join(',')")?.toString() == "2:end" && errors.isEmpty,
              "Last Slide works in the ready console, whose preview WS is a local binding")

        let page = JSContext()!
        page.evaluateScript("""
            var nodes = {};
            var document = { body: { appendChild: function (n) { nodes[n.id] = n; } },
                             getElementById: function (id) { return nodes[id] || null; },
                             createElement: function () { var n = { style: {} }; n.remove = function () { delete nodes[n.id]; }; return n; } };
            """)
        check(page.evaluateScript(AppDelegate.blackoutJS(true))?.toBool() == true &&
              page.evaluateScript("nodes['presto-blackout'].style.cssText.indexOf('#000') >= 0")?.toBool() == true &&
              page.evaluateScript(AppDelegate.blackoutJS(false))?.toBool() == false &&
              page.evaluateScript("!nodes['presto-blackout']")?.toBool() == true,
              "black screen: a full-viewport cover goes on and comes off")

        let escape = JSContext()!
        escape.evaluateScript("""
            var posted = [], backs = 0, answer = 'none';
            var document = { body: { classList: { contains: function () { return false; } } } };
            var window = this;
            window.webkit = { messageHandlers: { presto: { postMessage: function (m) { posted.push(m); } } } };
            var PrestoEdit = { handleEscape: function () { return answer; } };
            var chromeStack = [{ index: 1, step: 0 }];
            var PrestoEditChrome = { state: function () { return { backStack: chromeStack }; },
                                     back: function () { backs += 1; chromeStack = []; return true; } };
            var WS = { returnStack: [{ index: 3, step: 2 }], returnFromAppendix: function () {} };
            """)
        let escapeBack = escape.evaluateScript(AppDelegate.editEscapeJS)?.toString()
        let escapeAppendix = escape.evaluateScript(AppDelegate.editEscapeJS)?.toString()
        let appendixMessage = escape.evaluateScript("JSON.stringify(posted)")?.toString() ?? ""
        let escapeNone = escape.evaluateScript(AppDelegate.editEscapeJS)?.toString()
        escape.evaluateScript("answer = 'deselect'; chromeStack = [{ index: 1, step: 0 }];")
        let escapeDeselect = escape.evaluateScript(AppDelegate.editEscapeJS)?.toString()
        check(escapeBack == "back" && escapeAppendix == "appendix" && appendixMessage.contains(#""index":3"#) &&
              appendixMessage.contains(#""step":2"#) && escapeNone == "none" && escapeDeselect == "deselect" &&
              escape.evaluateScript("backs")?.toInt32() == 1,
              "escape in Edit: with nothing else open it goes back (the Back chip, then the appendix return) through Presto's navigation")

        let ready = JSContext()!
        ready.evaluateScript("""
            var calls = 0;
            var document = { documentElement: { dataset: { wsReady: '1' } } };
            var WS = { ready: { then: function () {} } };
            """)
        ready.evaluateScript(AppDelegate.deckReadyHelpersJS)
        ready.evaluateScript("prestoWhenDeckReady(function () { calls += 1; });")
        let readyNow = ready.evaluateScript("calls")?.toInt32() == 1
        ready.evaluateScript("document.documentElement.dataset.wsReady = ''; var waiting; WS.ready.then = function (f) { waiting = f; };")
        ready.evaluateScript("prestoWhenDeckReady(function () { calls += 1; });")
        let waits = ready.evaluateScript("calls")?.toInt32() == 1
        ready.evaluateScript("waiting();")
        check(readyNow && waits && ready.evaluateScript("calls")?.toInt32() == 2,
              "deck ready: a deck already ready calls back at once (the first thumbnail is captured); otherwise on WS.ready")

        let view = RecordingView()
        StubbedUIApp().showHint("Hello", in: view, duration: 1)
        let hint = view.scripts.last ?? ""
        check(hint.contains("PrestoHUD.hint") && hint.contains("PrestoConsole.hint") &&
              hint.contains("--p-surface") && !hint.contains("rgba(20,20,20"),
              "hints: the one-screen bar's or console's own toast, else a light one (no dark toast)")

        var compiled = false
        StubbedUIApp().compileContentRules { compiled = true }
        check(TransactionTests.waitFor({ compiled }, timeout: 5) && AppDelegate.cdnKaTeXRuleList != nil,
              "content rules: the jsDelivr KaTeX block compiles, so decks use the engine's vendored KaTeX")
    }

    // MARK: Real pages (offscreen WebKit, read-only deck)

    // The production injection against the real engine, layout, console,
    // control bar and Decks pages in this checkout. Nothing is written: the
    // deck is only loaded and patched in memory.
    static func realPageTests() {
        let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let deck = root.appendingPathComponent("examples/kitchen-sink/index.html")
        guard let content = try? String(contentsOf: deck, encoding: .utf8) else {
            check(false, "real pages: kitchen-sink is readable")
            return
        }
        func js(_ script: String, _ view: WKWebView) -> Any? { TransactionTests.javaScript(script, in: view) }
        func page(_ app: AppDelegate) -> WKWebView {
            let config = app.makeBaseConfig()
            config.userContentController.add(app, name: "presto")
            let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 800), configuration: config)
            view.navigationDelegate = app
            return view
        }

        // Audience, one screen: layout in audience mode, the control bar, the guards.
        let audienceApp = StubbedUIApp()
        audienceApp.currentDeckURL = deck
        audienceApp.fileContent = content
        audienceApp.presentPhase = .live
        audienceApp.audienceOneScreen = true
        audienceApp.lastAudienceSlideIndex = 2
        let audience = page(audienceApp)
        audienceApp.audienceWebView = audience
        audience.loadFileURL(deck, allowingReadAccessTo: audienceApp.findReadAccessRoot(deckURL: deck))
        check(TransactionTests.waitFor({
            js("document.body.classList.contains('ws-mode-audience') && !!window.PrestoHUD && WS.currentIndex === 2", audience) as? Bool == true
        }, timeout: 15), "real audience: audience mode, the one-screen bar and the talk's slide")
        check(js("document.documentElement.classList.contains('presto-layout-audience') && !!(window.PrestoLayout && PrestoLayout.current())", audience) as? Bool == true,
              "real audience: PrestoLayout lays the deck out in audience mode")
        check(TransactionTests.waitFor({ audienceApp.deckOutline.totalSlides > 0 }, timeout: 5) &&
              audienceApp.deckOutline.mainSlideCount > 0,
              "real audience: Presto reads the deck's outline (sections, appendix) from the engine")
        audienceApp.setBlackout(true)
        check(TransactionTests.waitFor({ js("!!document.getElementById('presto-blackout')", audience) as? Bool == true }),
              "real audience: Black out covers the slide")
        _ = js("WS.toggleOverview(); WS.overviewActive", audience)
        audienceApp.lastPresentationExitEscape = nil
        audienceApp.handlePresentationEscape()
        check(TransactionTests.waitFor({ js("WS.overviewActive", audience) as? Bool == false }) &&
              audienceApp.lastPresentationExitEscape == nil,
              "real audience: Escape closes the slide grid and does not arm the end-of-talk guard")
        audienceApp.presentPhase = .none
        _ = audienceApp.cleanupPresentation()

        // Edit preview: the layout in edit mode, the visual editor, a recorded patch.
        let editApp = NoDiskApp()
        editApp.currentDeckURL = deck
        editApp.fileContent = content
        editApp.lastPersistedContent = content
        editApp.sectionInfos = editApp.extractSections(from: content)
        editApp.isEditorMode = true
        let preview = page(editApp)
        editApp.previewWebView = preview
        preview.loadFileURL(deck, allowingReadAccessTo: editApp.findReadAccessRoot(deckURL: deck))
        check(TransactionTests.waitFor({
            js("!!window.__prestoWysiwyg && document.documentElement.classList.contains('presto-layout-edit')", preview) as? Bool == true
        }, timeout: 15), "real edit preview: the visual editor and the edit layout are in")
        check(TransactionTests.waitFor({ editApp.editHistoryAvailable || editApp.editInjectionProblem != nil }, timeout: 5) &&
              editApp.editInjectionProblem == nil && editApp.editHistoryAvailable,
              "real edit preview: every injected file loads and PrestoEdit is there (status: \(editApp.editInjectionProblem ?? "none"))")
        check(TransactionTests.waitFor({
            editApp.editChromeAvailable && js("document.body.classList.contains('presto-edit-chrome-on')", preview) as? Bool == true
        }, timeout: 10), "real edit preview: the edit chrome installs and reports editChromeReady")
        editApp.setRailVisible(false)
        check(TransactionTests.waitFor({ js("document.body.classList.contains('presto-rail-hidden')", preview) as? Bool == true }),
              "real edit preview: hiding the slide rail reaches the edit chrome")
        editApp.setRailVisible(true)
        UserDefaults.standard.removeObject(forKey: "editRailVisible")
        check(TransactionTests.waitFor({ editApp.editUndoState.undoLabel == "Undo" || !editApp.editUndoState.canUndo }, timeout: 2),
              "real edit preview: the history's state arrives (nothing to undo yet)")
        let before = editApp.sectionInfos[1].content
        let patched = before.replacingOccurrences(of: "</section>", with: "<p id=\"shell-patch\">patched</p></section>")
        editApp.sectionInfos[1] = (editApp.sectionInfos[1].range, patched)
        editApp.refreshPreviewSlide(1, recording: RecordedSlideChange(origin: "source", beforeHTML: before))
        check(TransactionTests.waitFor({ js("!!document.getElementById('shell-patch')", preview) as? Bool == true }),
              "real edit preview: a recorded source patch replaces the slide in place")

        // Refine, a visual edit, ⌘Z, ⌘Z: the second undo must reach the slide
        // before the Refine (the visual edit re-serialises the body, so the
        // first undo has to put back the Refine's exact bytes).
        editApp.mode = .edit
        let refineSlide = 4
        _ = js("WS.goToSlide(\(refineSlide), 0); true", preview)
        check(TransactionTests.waitFor({ editApp.currentEditorSlideIndex == refineSlide }),
              "real edit preview: the slide change is reported")
        let preRefine = editApp.sectionInfos[refineSlide].content
        editApp.applyRefineResult(slideIndex: refineSlide, taskID: UUID(), instructions: "Add a line", preHTML: preRefine,
                                  refinedRaw: preRefine.replacingOccurrences(
                                    of: "</section>", with: "<p id='refined-p' class=refined>Refined<br/>text</p></section>"))
        let refinedSection = editApp.sectionInfos[refineSlide].content
        check(refinedSection != preRefine && TransactionTests.waitFor({
            js("!!document.getElementById('refined-p') && PrestoEdit.historyState().undoLabel === 'Undo Refine'", preview) as? Bool == true
        }), "real edit preview: a Refine result is patched in and recorded")
        _ = js("__prestoWysiwyg.select('#refined-p'); __prestoWysiwyg.nudge(4, 0); __prestoWysiwyg.deselect(); true", preview)
        check(TransactionTests.waitFor({ editApp.sectionInfos[refineSlide].content != refinedSection }),
              "real edit preview: the nudge is saved")
        editApp.routeHistory(redo: false)
        check(TransactionTests.waitFor({ editApp.sectionInfos[refineSlide].content == refinedSection }),
              "real edit preview: ⌘Z after the nudge restores the Refine's exact bytes")
        editApp.routeHistory(redo: false)
        check(TransactionTests.waitFor({ editApp.sectionInfos[refineSlide].content == preRefine }) &&
              js("!document.getElementById('refined-p')", preview) as? Bool == true,
              "real edit preview: Refine, nudge, ⌘Z, ⌘Z restores the pre-Refine slide")

        // Escape with nothing open goes back from an appendix jump.
        editApp.editorWebView = RecordingView()
        let appendixIndex = (js("WS.appendixSlides.length ? WS.slides.indexOf(WS.appendixSlides[0]) : -1", preview) as? Int) ?? -1
        _ = js("__prestoWysiwyg.deselect(); WS.returnStack.push({ index: 2, step: 0 }); WS.goToSlide(\(appendixIndex), 0); true", preview)
        editApp.handleEditEscape()
        check(appendixIndex > 0 && TransactionTests.waitFor({
            js("WS.currentIndex === 2 && WS.returnStack.length === 0", preview) as? Bool == true
        }), "real edit preview: Escape with nothing open returns from the appendix")
        _ = js("PrestoEditChrome.goToSlide(\(appendixIndex), 0, { recordBack: true }); true", preview)
        check(TransactionTests.waitFor({ js("WS.currentIndex === \(appendixIndex)", preview) as? Bool == true }),
              "real edit preview: a link jump reaches the appendix")
        editApp.handleEditEscape()
        check(TransactionTests.waitFor({ js("WS.currentIndex === 2 && !PrestoEditChrome.state().backStack.length", preview) as? Bool == true }),
              "real edit preview: Escape then goes back like the Back chip")
        editApp.isEditorMode = false
        _ = editApp.cleanupPresentation()

        // Console: ready, then Presto's state is accepted.
        let consoleApp = StubbedUIApp()
        consoleApp.currentDeckURL = deck
        consoleApp.fileContent = content
        consoleApp.presentPhase = .ready
        let console = page(consoleApp)
        consoleApp.consoleWebView = console
        console.loadFileURL(root.appendingPathComponent("presto/presenter.html"),
                            allowingReadAccessTo: consoleApp.findReadAccessRoot(deckURL: deck))
        check(TransactionTests.waitFor({ consoleApp.presenterReady }, timeout: 15), "real console: presenter.html reports ready")
        check(TransactionTests.waitFor({
            js("!!(window.PrestoConsole && PrestoConsole.hasReceivedState && PrestoConsole.hasReceivedState())", console) as? Bool == true
        }, timeout: 5), "real console: the ready console receives Presto's state")
        consoleApp.presentPhase = .none
        _ = consoleApp.cleanupPresentation()

        // Decks: the page asks for the model and renders it.
        setenv("PRESTO_WEBSLIDES_ROOT", root.path, 1)
        let decksApp = StubbedUIApp()
        let decks = decksApp.ensureDecksWebView()
        check(TransactionTests.waitFor({ decksApp.decksReady }, timeout: 15), "real Decks: decks.html reports ready")
        decksApp.renderDecks()
        check(TransactionTests.waitFor({
            js("!!window.PrestoDecks && document.body.innerText.length > 0", decks) as? Bool == true
        }, timeout: 5), "real Decks: the page renders the library model")
        unsetenv("PRESTO_WEBSLIDES_ROOT")
    }
}

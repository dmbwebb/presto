import Cocoa
import Darwin
import PDFKit
import WebKit

/// The WebSlides engine root Presto falls back to when a deck does not link to
/// one itself: $PRESTO_WEBSLIDES_ROOT, else the `PrestoWebSlidesRoot` key that
/// presto/app/install.sh writes into the installed bundle's Info.plist, else the
/// repo containing an uninstalled bundle (<root>/presto/app/Presto.app).
func prestoDefaultWebSlidesRoot() -> URL {
    if let override = ProcessInfo.processInfo.environment["PRESTO_WEBSLIDES_ROOT"], !override.isEmpty {
        return URL(fileURLWithPath: override)
    }
    if let installed = Bundle.main.object(forInfoDictionaryKey: "PrestoWebSlidesRoot") as? String,
       !installed.isEmpty {
        return URL(fileURLWithPath: installed)
    }
    return Bundle.main.bundleURL
        .deletingLastPathComponent()   // presto/app
        .deletingLastPathComponent()   // presto
        .deletingLastPathComponent()   // repo root
}

// MARK: - AI Refine Types

struct RefineTask {
    let id: UUID
    let slideIndex: Int
    let instructions: String
}

struct SectionParts {
    let openingTag: String
    let body: String
    let closingTag: String
}

// A Presto commit patched into the preview that the unified undo history
// records: origin "source" or "refine", and the slide's section before it.
// Origin "history" patches without clearing or recording.
struct RecordedSlideChange: Equatable {
    let origin: String
    let beforeHTML: String
    var prompt: String? = nil   // a Refine's instructions, shown in its history
}

struct PendingEditorChangeResult {
    let ok: Bool
    let savedIndex: Int?
    var change: RecordedSlideChange? = nil

    static let failed = PendingEditorChangeResult(ok: false, savedIndex: nil)
    static let clean = PendingEditorChangeResult(ok: true, savedIndex: nil)

    static func saved(index: Int, beforeHTML: String? = nil) -> PendingEditorChangeResult {
        return PendingEditorChangeResult(ok: true, savedIndex: index,
                                         change: beforeHTML.map { RecordedSlideChange(origin: "source", beforeHTML: $0) })
    }
}

struct DeckResourceRoots {
    let deckRoot: URL
    let webSlidesRoot: URL
    let readAccessRoot: URL
}

struct DeckAssetStamp: Equatable {
    let modified: Date?
    let size: UInt64
    let inode: UInt64
}

enum WebContentRole: String {
    case audience
    case presenter
    case editorPreview = "editor-preview"
    case editor
    case export
    case decks
    case thumbnail
}

enum PDFExportMode: String {
    case stepped
    case final
}

// MARK: - Window Mode Types

// The one main window shows one of three modes. Raw values are the
// segments of the toolbar's mode control. (@objc so the methods taking it,
// declared in extensions, can be overridden in tests.)
@objc enum PrestoMode: Int {
    case decks = 0
    case edit = 1
    case present = 2
}

// Present mode opens the console "ready"; Start makes it "live".
enum PresentPhase {
    case none
    case ready
    case live
}

enum SaveStatus: Equatable {
    case saved
    case saving
    case failed(String)
}

// The unified edit history's state, as posted by wysiwyg.js (undoState).
struct EditUndoState: Equatable {
    var canUndo = false
    var canRedo = false
    var undoLabel = ""
    var redoLabel = ""

    // The menu's and toolbar's wording: the history's label, else plain Undo/Redo.
    var undoTitle: String { undoLabel.isEmpty ? "Undo" : undoLabel }
    var redoTitle: String { redoLabel.isEmpty ? "Redo" : redoLabel }
}

// The talk timer belongs to Swift so the console page, the one-screen
// control bar and a reloaded view all show the same time.
struct PresentationTimer {
    var accumulatedMs: Double = 0
    var startedAt: Date?

    var running: Bool { startedAt != nil }

    func elapsedMs(now: Date = Date()) -> Double {
        guard let startedAt else { return accumulatedMs }
        return accumulatedMs + max(0, now.timeIntervalSince(startedAt) * 1000)
    }

    mutating func start(now: Date = Date()) {
        if startedAt == nil { startedAt = now }
    }

    mutating func pause(now: Date = Date()) {
        accumulatedMs = elapsedMs(now: now)
        startedAt = nil
    }

    mutating func reset() {
        accumulatedMs = 0
        startedAt = nil
    }
}

// A display as Presto plans windows on it. Built from NSScreen in the app
// and by hand in tests.
struct PrestoDisplay: Equatable {
    let id: CGDirectDisplayID
    let name: String
    let frame: NSRect
    let visibleFrame: NSRect
    let isBuiltin: Bool
}

// Where and how the audience sees the deck. `displayID == nil` means the
// main window itself shows the deck (one screen). `consoleFrame` is where
// the main window must move first so it never sits under the audience.
struct AudienceWindowPlan: Equatable {
    let displayID: CGDirectDisplayID?
    let frame: NSRect
    let borderless: Bool
    let level: NSWindow.Level
    var consoleFrame: NSRect? = nil
}

// MARK: - External Change Merge Types

enum ExternalMergeChoice {
    case keepMine, useDisk, reloadFromDisk

    var buttonTitle: String {
        switch self {
        case .keepMine: return "Keep My Versions"
        case .useDisk: return "Use Disk Versions"
        case .reloadFromDisk: return "Reload from Disk"
        }
    }
}

// A deck split at its slide sections. `gap` is the text between the previous
// slide (or, for slide 0, the head) and this slide's <section>.
struct DeckParts {
    let head: String
    let slides: [(gap: String, content: String)]
    let tail: String
}

struct DeckMergeOutcome {
    let content: String
    // Human-readable, e.g. "slide 4", "slide 7 (slide 8 on disk)", "the deck head".
    let conflicts: [String]
    // Presto slide index -> merged slide index.
    let oursIndexMap: [Int: Int]
    // 1-based merged slide numbers whose disk version was taken.
    let takenFromDisk: [Int]
    let keptLocal: [Int]
    let addedOnDisk: Int
    let deletedOnDisk: Int
    let headFromDisk: Bool

    static func slideList(_ numbers: [Int]) -> String {
        let labels = numbers.map(String.init)
        switch labels.count {
        case 0: return "no slides"
        case 1: return "slide \(labels[0])"
        default: return "slides " + labels.dropLast().joined(separator: ", ") + " and " + labels.last!
        }
    }

    var diskSummary: String {
        var parts: [String] = []
        if !takenFromDisk.isEmpty { parts.append(DeckMergeOutcome.slideList(takenFromDisk)) }
        if addedOnDisk > 0 { parts.append("\(addedOnDisk) added") }
        if deletedOnDisk > 0 { parts.append("\(deletedOnDisk) deleted") }
        if headFromDisk { parts.append("deck head") }
        return parts.isEmpty ? "none" : parts.joined(separator: ", ")
    }

    var mergeSummary: String {
        let local = keptLocal.isEmpty ? "no other edits of yours" : "your edits to \(DeckMergeOutcome.slideList(keptLocal))"
        let numbering = addedOnDisk + deletedOnDisk > 0 ? " (slide numbers as after the merge)" : ""
        return "outside changes (\(diskSummary)) are taken and \(local) are kept\(numbering)"
    }
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler {

    // Windows. The main window holds Decks, Edit and the presenter console;
    // the audience window exists only while live on a second display.
    var mainWindow: NSWindow?
    var audienceWindow: NSWindow?

    // WebViews (optional — not implicitly unwrapped, to avoid crashes)
    var decksWebView: WKWebView?
    var previewWebView: WKWebView?      // the deck in Edit mode, with the visual editor
    var editorWebView: WKWebView?       // the inspector (editor.html: source and Refine)
    var consoleWebView: WKWebView?      // presenter.html in Present mode
    var audienceWebView: WKWebView?     // the deck the audience sees while live
    var editorSplitView: NSSplitView?
    var editorSidebarVisible = false    // the inspector is open

    // Mode state
    var mode: PrestoMode = .decks
    var presentPhase: PresentPhase = .none
    // Where a presentation returns to, and whether that mode had a deck open.
    var presentOrigin: PrestoMode = .decks
    var decksReady = false
    var railVisible = true
    var ghostFutureSteps = true
    var editUndoState = EditUndoState()
    var editHistoryAvailable = false
    var editChromeAvailable = false
    var selectionKind: String?
    var saveStatus: SaveStatus = .saved
    // A missing or unreadable injected file: the edit view says so instead
    // of offering a half-working editor.
    var editInjectionProblem: String?
    var deckTitle = ""
    var pendingOpenURLs: [URL] = []
    var pendingEditPosition: (index: Int, step: Int)?
    var consoleTextEditing = false
    // The inspector's Refine prompt has focus (editor.js refineTextEditing).
    var refineTextEditing = false

    // Presentation state
    var audienceOneScreen = false
    var audienceDisplayID: CGDirectDisplayID?
    var audienceDisplayMissing = false
    var blackoutActive = false
    var hudTimerVisible = false
    var talkTimer = PresentationTimer()
    var talkTimerStartedThisSession = false
    var displaySleepActivity: NSObjectProtocol?
    var mainWindowFrameBeforeShow: NSRect?
    var mainWindowWasFullScreenBeforeShow = false
    var oneScreenFullScreenPending = false
    var oneScreenExitPending = false
    // The show's full screen hid the menu bar and Dock (see
    // window(_:willUseFullScreenPresentationOptions:)).
    var oneScreenShowHidDock = false
    var screenParametersObserver: NSObjectProtocol?
    var deckOutline = DeckOutline()
    // Sheets raised during a talk, shown once it ends.
    var sheetsAfterTalk: [() -> Void] = []

    // Native chrome
    var toolbarItems: [NSToolbarItem.Identifier: NSToolbarItem] = [:]
    weak var undoMenuItem: NSMenuItem?
    weak var redoMenuItem: NSMenuItem?
    var openRecentMenu: NSMenu?
    // Views and windows from a finished session, kept until the next one so
    // late WebKit callbacks never reach a deallocated object.
    var staleWindows: [NSWindow] = []
    var staleWebViews: [WKWebView] = []

    // Deck library (Decks mode) and the hidden thumbnail renderer
    lazy var deckLibrary: DeckLibrary = makeDeckLibrary()
    var thumbnailQueue: [ThumbnailJob] = []
    var thumbnailJob: ThumbnailJob?
    var thumbnailWebView: WKWebView?
    var retiredThumbnailWebView: WKWebView?
    var thumbnailGeneration = 0
    var thumbnailFailures: Set<String> = []
    var decksRenderPending: DispatchWorkItem?
    var deckWrittenThisSession = false

    // State
    var currentDeckURL: URL?
    var presenterReady = false
    var isEndingPresentation = false
    // True while the deck's edit surfaces (preview and inspector) exist. They
    // stay alive while presenting, so returning to Edit is instant.
    var isEditorMode = false
    var wysiwygTextEditingActive = false
    var wysiwygHasSelection = false
    var didHandleInitialLaunch = false
    var sessionGeneration = 0
    var webContentRecoveryAttempts: [WebContentRole: Int] = [:]
    var simulatedWebContentTerminationRoles: Set<WebContentRole> = []
    var lastAudienceSlideIndex = 0
    var lastAudienceStep = 0
    var lastAudienceReturnStack: [[String: Int]] = []

    var testNavigationLoggingEnabled: Bool {
        ProcessInfo.processInfo.environment["PRESTO_TEST_LOG_NAVIGATION"] == "1"
    }

    var foregroundTestsEnabled: Bool {
        ProcessInfo.processInfo.environment["PRESTO_ALLOW_FOREGROUND_TESTS"] == "1"
    }

    var forceDualScreenForTesting: Bool {
        foregroundTestsEnabled &&
            ProcessInfo.processInfo.environment["PRESTO_TEST_FORCE_DUAL_SCREEN"] == "1"
    }

    var simulatedWebContentTerminationRequests: Set<WebContentRole> {
        guard foregroundTestsEnabled,
              let raw = ProcessInfo.processInfo.environment["PRESTO_TEST_SIMULATE_WEB_CONTENT_TERMINATION"] else {
            return []
        }
        return Set(raw.split(separator: ",").compactMap {
            WebContentRole(rawValue: String($0).trimmingCharacters(in: .whitespaces))
        })
    }

    var testExportOutputURL: URL? {
        guard foregroundTestsEnabled,
              let path = ProcessInfo.processInfo.environment["PRESTO_TEST_EXPORT_OUTPUT"],
              !path.isEmpty else { return nil }
        return URL(fileURLWithPath: path)
    }

    var testPDFExportMode: PDFExportMode? {
        guard testExportOutputURL != nil else { return nil }
        switch ProcessInfo.processInfo.environment["PRESTO_TEST_EXPORT_MODE"]?.lowercased() {
        case "final": return .final
        default: return .stepped
        }
    }

    var simulatedWebContentTerminationDelay: TimeInterval {
        guard foregroundTestsEnabled,
              let raw = ProcessInfo.processInfo.environment["PRESTO_TEST_WEB_CONTENT_TERMINATION_DELAY_MS"],
              let milliseconds = Int(raw), milliseconds >= 0, milliseconds <= 10_000 else {
            return 0.15
        }
        return Double(milliseconds) / 1000
    }

    // Editor state
    var fileContent: String?
    var lastPersistedContent: String?
    var sourceEditorDirty = false
    var sectionInfos: [(range: Range<String.Index>, content: String)] = []
    var currentEditorSlideIndex = 0
    var currentEditorStep = 0
    var editorReady = false
    var editorRecoveryNeedsWarning = false
    var isReloadingPreview = false
    var saveTransactionCount = 0

    // Per-commit fan-out debounce. Decks live on Dropbox, so a whole-deck atomic
    // write per keystroke (and a full code-pane re-highlight per commit) means
    // constant FSEvents/hashing/upload churn — coalesce both on a trailing edge,
    // flushing on every teardown/reload/navigation path.
    var pendingDeckWrite: DispatchWorkItem?
    var deckWriteDirty = false
    var pendingEditorMirror: DispatchWorkItem?
    var pendingEditorSaveID: Int?
    var pendingWysiwygSaveID: Int?
    var editorFlushInProgress = false
    var terminationPending = false

    // File watcher
    var fileWatchSource: DispatchSourceFileSystemObject?
    var directoryWatchSource: DispatchSourceFileSystemObject?
    var assetWatchTimer: DispatchSourceTimer?
    var assetSnapshot: [URL: DeckAssetStamp] = [:]
    var assetReloadPending = false
    var fileWatchFD: Int32 = -1
    var fileWatchDebounce: DispatchWorkItem?
    var fileWatchRearmAttempts = 0
    var fileReloadFailureCount = 0
    var isPresentingExternalConflict = false
    // An outside change arrived during a live text session; merge when it ends.
    var externalMergeDeferred = false
    // The bytes of Presto's last successful write. Disk content that differs
    // from it was written by someone else and is backed up before replacement.
    var lastPrestoWrittenContent: String?

    // AI Refine state (per slide index). Applied results live in the visual
    // editor's unified undo history (PrestoEdit.recordExternalChange).
    var refineQueues: [Int: [RefineTask]] = [:]              // queued tasks (FIFO)
    var refineRunningInfo: [Int: RefineTask] = [:]            // task currently in flight
    var refineErrors: [Int: String] = [:]                     // last error message per slide

    // Keyboard
    var keyMonitor: Any?
    // Presentation teardown: Escape outside the appendix arms a 2s window and
    // only a second Escape inside it ends the talk.
    var lastPresentationExitEscape: Date?
    // ⌘Q while live arms the same 2s window; a second ⌘Q in it quits.
    var lastLiveQuitPress: Date?

    // PDF export state
    var exportWebView: WKWebView?
    var exportOutputURL: URL?
    var exportPDFDoc: PDFDocument?
    var exportSlideSteps: [(slideIndex: Int, step: Int)] = []
    var exportPageIndex = 0
    var exportPollCount = 0
    var exportPageMap: [String: Int] = [:]  // "slideIndex,step" → pageIndex
    var exportPageLinks: [Int: [[String: Any]]] = [:]  // pageIndex → link info
    var exportProgressWindow: NSWindow?
    var exportProgressIndicator: NSProgressIndicator?
    var exportProgressLabel: NSTextField?
    var exportDeckURL: URL?
    var exportGeneration = 0
    var exportIsWriting = false
    var exportRequestActive = false
    var exportMode: PDFExportMode = .stepped

    // MARK: - Launch

    func applicationWillFinishLaunching(_ notification: Notification) {
        // Light only: Presto never follows the system's dark appearance.
        NSApp.appearance = NSAppearance(named: .aqua)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSLog("Presto: applicationDidFinishLaunching")
        // Deck pages load only after the CDN rule is ready (or has failed).
        compileContentRules {
            DispatchQueue.main.async { self.handleInitialLaunch() }
        }
    }

    enum LaunchRequest: Equatable {
        case present(URL)
        case edit(URL)
        case play(URL)
    }

    // `Presto <deck>` opens Present (ready), `--edit <deck>` opens Edit and
    // `--play <deck>` goes live at once. Anything else opens Decks.
    func launchRequest(arguments: [String]) -> LaunchRequest? {
        guard let first = arguments.first else { return nil }
        let flagged = first == "--edit" || first == "--play"
        guard let path = flagged ? arguments.dropFirst().first : first,
              !path.hasPrefix("-"),
              FileManager.default.fileExists(atPath: path) else { return nil }
        let url = URL(fileURLWithPath: path)
        switch first {
        case "--edit": return .edit(url)
        case "--play": return .play(url)
        default: return .present(url)
        }
    }

    func handleInitialLaunch() {
        guard !didHandleInitialLaunch else { return }
        didHandleInitialLaunch = true
        setupKeyboardMonitor()
        observeScreenChanges()
        showMainWindow()

        if let request = launchRequest(arguments: Array(ProcessInfo.processInfo.arguments.dropFirst())) {
            switch request {
            case .present(let url):
                NSLog("Presto: launching with CLI deck: %@", url.path)
                openDeck(url, in: .present)
            case .edit(let url):
                NSLog("Presto: launching with CLI deck for editing: %@", url.path)
                openDeck(url, in: .edit)
            case .play(let url):
                NSLog("Presto: launching with CLI deck, playing: %@", url.path)
                openDeck(url, in: .present, goLive: true)
            }
            return
        }
        if !pendingOpenURLs.isEmpty {
            let urls = pendingOpenURLs
            pendingOpenURLs = []
            application(NSApp, open: urls)
            return
        }
        showMode(.decks)
    }

    // Finder double-click, Open With and Dock drops: open in Edit at the
    // deck's last position. A folder opens its index.html.
    func application(_ application: NSApplication, open urls: [URL]) {
        guard didHandleInitialLaunch else {
            pendingOpenURLs.append(contentsOf: urls)
            return
        }
        guard let url = urls.lazy.compactMap({ self.deckFile(for: $0) }).first else {
            NSLog("Presto: no WebSlides deck in opened items")
            return
        }
        openDeck(url, in: .edit)
    }

    func deckFile(for url: URL) -> URL? {
        var isDirectory = ObjCBool(false)
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) else { return nil }
        if isDirectory.boolValue {
            let index = url.appendingPathComponent("index.html")
            return FileManager.default.fileExists(atPath: index.path) ? index : nil
        }
        let ext = url.pathExtension.lowercased()
        return ext == "html" || ext == "htm" ? url : nil
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }

    // Closing the window leaves Presto running; the Dock icon brings back Decks.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            showMainWindow()
            if currentDeckURL == nil { showMode(.decks) }
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Persist any debounced edit synchronously before the process exits.
        flushPendingDeckWrite()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        recordPositionBeforeQuit()
        guard isEditorMode else {
            return flushPendingDeckWrite() ? .terminateNow : .terminateCancel
        }
        guard !terminationPending else { return .terminateLater }
        terminationPending = true
        // Return terminateLater before replying, including when the flush fails
        // synchronously. WebKit still needs to deliver any active text session.
        DispatchQueue.main.async { [weak self] in
            guard let self else { sender.reply(toApplicationShouldTerminate: false); return }
            self.flushActiveEditor { saved in
                self.terminationPending = false
                sender.reply(toApplicationShouldTerminate: saved)
            }
        }
        return .terminateLater
    }

    // Continue reopens where Duncan stopped: the live slide, or Edit's. No
    // picture: the renderer cannot finish one before the process exits.
    func recordPositionBeforeQuit() {
        guard currentDeckURL != nil else { return }
        if presentPhase == .live {
            recordDeckPosition(index: lastAudienceSlideIndex, step: lastAudienceStep, picture: false)
        } else if mode == .edit && isEditorMode {
            recordDeckPosition(index: currentEditorSlideIndex, step: currentEditorStep, picture: false)
        }
    }

    // MARK: - Deck Library

    // Overridden in tests so they never touch the real library.
    func makeDeckLibrary() -> DeckLibrary {
        DeckLibrary(storeURL: DeckLibrary.defaultStoreURL(), registryURL: DeckLibrary.defaultRegistryURL())
    }

    func deckName(for path: String) -> String {
        let url = URL(fileURLWithPath: path)
        return url.deletingLastPathComponent().lastPathComponent
    }

    // The deck's own <title>, else its folder name.
    func deckDisplayTitle(content: String?, deckURL: URL) -> String {
        if let content,
           let open = content.range(of: "<title", options: .caseInsensitive),
           let openEnd = openingTagEnd(in: content, from: open.lowerBound),
           let close = content.range(of: "</title>", options: .caseInsensitive,
                                     range: content.index(after: openEnd)..<content.endIndex) {
            let raw = String(content[content.index(after: openEnd)..<close.lowerBound])
            let title = raw.replacingOccurrences(of: "&amp;", with: "&")
                .replacingOccurrences(of: "&middot;", with: "·")
                .replacingOccurrences(of: "&mdash;", with: "—")
                .replacingOccurrences(of: "&ndash;", with: "–")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !title.isEmpty { return title }
        }
        return deckName(for: deckURL.path)
    }

    func showMissingDeckAlert(path: String) {
        deckLibrary.removeRecent(URL(fileURLWithPath: path))
        scheduleDecksRender()
        let alert = NSAlert()
        alert.messageText = "Deck not found"
        alert.informativeText = "The file at \(path) no longer exists. It has been removed from recent decks."
        alert.runModal()
    }

    // After a deck fails to open nothing has changed; with no deck open the
    // window shows Decks.
    func returnToDecksIfNoDeck() {
        showMainWindow()
        if currentDeckURL == nil && mode != .decks { showMode(.decks) }
    }

    func showDeckOpenError(deckURL: URL, error: Error) {
        NSLog("Presto: failed to read deck file %@: %@", deckURL.path, error.localizedDescription)
        let alert = NSAlert()
        alert.messageText = "Unable to open deck"
        alert.informativeText = "Presto could not read \(deckURL.lastPathComponent): \(error.localizedDescription)"
        alert.runModal()
        returnToDecksIfNoDeck()
    }

    func showNoSlidesAlert(deckURL: URL) {
        let alert = NSAlert()
        alert.messageText = "No slides found"
        alert.informativeText = "Presto could not find any <section> slides in \(deckURL.lastPathComponent)."
        alert.runModal()
        returnToDecksIfNoDeck()
    }

    func showDeckStructureAlert(deckURL: URL, issues: [String]) {
        let alert = NSAlert()
        alert.messageText = "Invalid deck structure"
        alert.informativeText = "Presto could not open \(deckURL.lastPathComponent):\n" + issues.joined(separator: "\n")
        alert.runModal()
        returnToDecksIfNoDeck()
    }

    func readDeckContentForOpening(deckURL: URL) -> String? {
        do {
            return try String(contentsOf: deckURL, encoding: .utf8)
        } catch {
            showDeckOpenError(deckURL: deckURL, error: error)
            return nil
        }
    }

    func validateDeckHasSlides(deckURL: URL, content: String) -> Bool {
        let structureIssues = deckStructureIssues(in: content)
        guard structureIssues.isEmpty else {
            showDeckStructureAlert(deckURL: deckURL, issues: structureIssues)
            return false
        }

        let sections = extractSections(from: content)
        NSLog("Presto: validated %d sections in %@", sections.count, deckURL.lastPathComponent)
        guard !sections.isEmpty else {
            showNoSlidesAlert(deckURL: deckURL)
            return false
        }
        return true
    }

    // MARK: - Section Extraction

    func attributeNameStart(in s: String, before idx: String.Index) -> Bool {
        if idx == s.startIndex { return true }
        let prev = s[s.index(before: idx)]
        return prev.isWhitespace || prev == "<"
    }

    func attributeNameEnd(in s: String, after idx: String.Index) -> Bool {
        var i = idx
        while i < s.endIndex, s[i].isWhitespace {
            i = s.index(after: i)
        }
        return i < s.endIndex && s[i] == "="
    }

    func isInsideHTMLComment(_ idx: String.Index, in html: String) -> Bool {
        let before = html.startIndex..<idx
        let lastOpen = html.range(of: "<!--", options: .backwards, range: before)
        let lastClose = html.range(of: "-->", options: .backwards, range: before)
        guard let open = lastOpen else { return false }
        guard let close = lastClose else { return true }
        return open.lowerBound > close.lowerBound
    }

    // Every `<!-- ... -->` span, precomputed once with a single forward scan so
    // hot tokenizer loops can test comment membership via `isInsideRawText` in
    // O(#comments) instead of the two O(n) `.backwards` searches that
    // isInsideHTMLComment runs per tag (the source of extractSections' O(n²)
    // per-save cost). An unterminated comment runs to end-of-string, matching
    // isInsideHTMLComment's "an unclosed comment swallows the rest" behavior.
    func commentRanges(in html: String) -> [Range<String.Index>] {
        var ranges: [Range<String.Index>] = []
        var searchFrom = html.startIndex
        while let open = html.range(of: "<!--", range: searchFrom..<html.endIndex) {
            let closeEnd = html.range(of: "-->", range: open.upperBound..<html.endIndex)?.upperBound
                ?? html.endIndex
            ranges.append(open.lowerBound..<closeEnd)
            searchFrom = closeEnd
        }
        return ranges
    }

    func rawTextRanges(in html: String) -> [Range<String.Index>] {
        var ranges: [Range<String.Index>] = []
        for tagName in ["script", "style"] {
            var searchFrom = html.startIndex
            while let openTag = html.range(of: "<\(tagName)", options: .caseInsensitive,
                                           range: searchFrom..<html.endIndex) {
                guard let openEnd = openingTagEnd(in: html, from: openTag.lowerBound) else {
                    break
                }
                let contentStart = html.index(after: openEnd)
                if isInsideHTMLComment(openTag.lowerBound, in: html) {
                    searchFrom = contentStart
                    continue
                }
                guard let closeTag = html.range(of: "</\(tagName)>", options: .caseInsensitive,
                                                range: contentStart..<html.endIndex) else {
                    break
                }
                ranges.append(contentStart..<closeTag.lowerBound)
                searchFrom = closeTag.upperBound
            }
        }
        return ranges
    }

    func templateRanges(in html: String) -> [Range<String.Index>] {
        var ranges: [Range<String.Index>] = []
        let rawRanges = rawTextRanges(in: html)
        var searchFrom = html.startIndex
        while let openTag = html.range(of: "<template", options: .caseInsensitive,
                                       range: searchFrom..<html.endIndex) {
            guard let openEnd = openingTagEnd(in: html, from: openTag.lowerBound) else {
                break
            }
            let contentStart = html.index(after: openEnd)
            if !tagNameEndAfterLiteral("<template", in: html, at: openTag.lowerBound) ||
               isInsideHTMLComment(openTag.lowerBound, in: html) ||
               isInsideRawText(openTag.lowerBound, ranges: rawRanges) {
                searchFrom = contentStart
                continue
            }
            guard let closeTag = html.range(of: "</template>", options: .caseInsensitive,
                                            range: contentStart..<html.endIndex) else {
                break
            }
            ranges.append(contentStart..<closeTag.lowerBound)
            searchFrom = closeTag.upperBound
        }
        return ranges
    }

    func isInsideRawText(_ idx: String.Index, ranges: [Range<String.Index>]) -> Bool {
        ranges.contains { $0.contains(idx) }
    }

    func hasClass(_ className: String, inOpeningTag openingTag: String) -> Bool {
        var searchFrom = openingTag.startIndex
        var classRange: Range<String.Index>? = nil
        while let candidate = openingTag.range(of: "class", options: .caseInsensitive, range: searchFrom..<openingTag.endIndex) {
            if attributeNameStart(in: openingTag, before: candidate.lowerBound) &&
               attributeNameEnd(in: openingTag, after: candidate.upperBound) {
                classRange = candidate
                break
            }
            searchFrom = candidate.upperBound
        }

        guard let classRange,
              let equalsRange = openingTag.range(of: "=", range: classRange.upperBound..<openingTag.endIndex) else {
            return false
        }

        var valueStart = equalsRange.upperBound
        while valueStart < openingTag.endIndex, openingTag[valueStart].isWhitespace {
            valueStart = openingTag.index(after: valueStart)
        }
        guard valueStart < openingTag.endIndex else { return false }

        let quote = openingTag[valueStart]
        if quote == "\"" || quote == "'" {
            let contentStart = openingTag.index(after: valueStart)
            guard let contentEnd = openingTag[contentStart..<openingTag.endIndex].firstIndex(of: quote) else {
                return false
            }
            return openingTag[contentStart..<contentEnd].split(whereSeparator: { $0.isWhitespace }).contains {
                $0.caseInsensitiveCompare(className) == .orderedSame
            }
        }

        let contentEnd = openingTag[valueStart..<openingTag.endIndex].firstIndex { $0.isWhitespace || $0 == ">" } ?? openingTag.endIndex
        return openingTag[valueStart..<contentEnd].split(whereSeparator: { $0.isWhitespace }).contains {
            $0.caseInsensitiveCompare(className) == .orderedSame
        }
    }

    func deckContentRange(in html: String) -> Range<String.Index>? {
        var scanFrom = html.startIndex
        var deckContentStart: String.Index? = nil
        var deckTagName: String? = nil
        let comments = commentRanges(in: html)
        let rawRanges = rawTextRanges(in: html) + templateRanges(in: html)
        while let token = nextHTMLTag(in: html, from: scanFrom, within: html.startIndex..<html.endIndex) {
            scanFrom = token.end
            if token.isClosing || isInsideRawText(token.start, ranges: comments) || isInsideRawText(token.start, ranges: rawRanges) {
                continue
            }
            if hasClass("ws-deck", inOpeningTag: token.text) {
                deckContentStart = token.end
                deckTagName = token.name
                break
            }
        }

        guard let contentStart = deckContentStart, let deckTagName else { return nil }

        var depth = 1
        var searchFrom = contentStart
        while let token = nextHTMLTag(in: html, from: searchFrom, within: contentStart..<html.endIndex) {
            searchFrom = token.end
            if token.name != deckTagName || isInsideRawText(token.start, ranges: comments) || isInsideRawText(token.start, ranges: rawRanges) {
                continue
            }
            if token.isClosing {
                depth -= 1
                if depth == 0 {
                    return contentStart..<token.start
                }
            } else if !token.isVoid {
                depth += 1
            }
        }

        return nil
    }

    func deckStructureIssues(in html: String) -> [String] {
        var scanFrom = html.startIndex
        var tagStack: [(name: String, isDeck: Bool)] = []
        var deckCount = 0
        var nestedDeckCount = 0
        let comments = commentRanges(in: html)
        let rawRanges = rawTextRanges(in: html) + templateRanges(in: html)

        while let token = nextHTMLTag(in: html, from: scanFrom, within: html.startIndex..<html.endIndex) {
            scanFrom = token.end
            if isInsideRawText(token.start, ranges: comments) || isInsideRawText(token.start, ranges: rawRanges) {
                continue
            }

            if token.isClosing {
                if let matchIndex = tagStack.lastIndex(where: { $0.name == token.name }) {
                    tagStack.removeSubrange(matchIndex..<tagStack.endIndex)
                }
                continue
            }

            let isDeck = hasClass("ws-deck", inOpeningTag: token.text)
            if isDeck {
                deckCount += 1
                if tagStack.contains(where: { $0.isDeck }) {
                    nestedDeckCount += 1
                }
            }
            if !token.isVoid {
                tagStack.append((name: token.name, isDeck: isDeck))
            }
        }

        var issues: [String] = []
        if deckCount == 0 {
            issues.append("MISSING DECK: no .ws-deck container found")
        }
        if deckCount > 1 {
            issues.append("MULTIPLE DECKS: found \(deckCount) .ws-deck containers; expected exactly 1")
        }
        if nestedDeckCount > 0 {
            issues.append("NESTED DECK: .ws-deck containers must not be nested")
        }

        let unclosedDeckTags = tagStack.filter { $0.isDeck }.map { $0.name }
        if unclosedDeckTags.count == 1 {
            issues.append("UNCLOSED DECK: .ws-deck is missing closing </\(unclosedDeckTags[0])>")
        } else if unclosedDeckTags.count > 1 {
            let tagList = unclosedDeckTags.map { "</\($0)>" }.joined(separator: ", ")
            issues.append("UNCLOSED DECK: \(unclosedDeckTags.count) .ws-deck containers are missing closing tags: \(tagList)")
        }
        return issues
    }

    struct HTMLTagToken {
        let name: String
        let start: String.Index
        let end: String.Index
        let text: String
        let isClosing: Bool
        let isVoid: Bool
    }

    let htmlVoidTags: Set<String> = [
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"
    ]

    func isHTMLTagNameCharacter(_ ch: Character) -> Bool {
        ch.isLetter || ch.isNumber || ch == "-" || ch == "_" || ch == ":"
    }

    func tagNameEndAfterLiteral(_ literal: String, in html: String, at start: String.Index) -> Bool {
        let after = html.index(start, offsetBy: literal.count)
        guard after < html.endIndex else { return true }
        return !isHTMLTagNameCharacter(html[after])
    }

    func nextHTMLTag(in html: String, from start: String.Index, within bounds: Range<String.Index>) -> HTMLTagToken? {
        var scanFrom = start
        while scanFrom < bounds.upperBound {
            guard let lt = html.range(of: "<", range: scanFrom..<bounds.upperBound)?.lowerBound else {
                return nil
            }

            var i = html.index(after: lt)
            while i < bounds.upperBound, html[i].isWhitespace {
                i = html.index(after: i)
            }

            var isClosing = false
            if i < bounds.upperBound, html[i] == "/" {
                isClosing = true
                i = html.index(after: i)
                while i < bounds.upperBound, html[i].isWhitespace {
                    i = html.index(after: i)
                }
            }

            guard i < bounds.upperBound, html[i].isLetter else {
                scanFrom = html.index(after: lt)
                continue
            }

            let nameStart = i
            while i < bounds.upperBound, isHTMLTagNameCharacter(html[i]) {
                i = html.index(after: i)
            }
            let name = String(html[nameStart..<i]).lowercased()

            guard let close = openingTagEnd(in: html, from: lt) else {
                return nil
            }
            let end = html.index(after: close)
            let text = String(html[lt..<end])
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return HTMLTagToken(
                name: name,
                start: lt,
                end: end,
                text: text,
                isClosing: isClosing,
                isVoid: htmlVoidTags.contains(name) || trimmed.hasSuffix("/>")
            )
        }
        return nil
    }

    func matchingSectionEnd(
        in html: String,
        from start: String.Index,
        within bounds: Range<String.Index>,
        comments: [Range<String.Index>],
        rawRanges: [Range<String.Index>]
    ) -> String.Index? {
        var scanFrom = start
        var sectionDepth = 1
        while let token = nextHTMLTag(in: html, from: scanFrom, within: bounds) {
            scanFrom = token.end
            if token.name != "section" || isInsideRawText(token.start, ranges: comments) || isInsideRawText(token.start, ranges: rawRanges) {
                continue
            }
            if token.isClosing {
                sectionDepth -= 1
                if sectionDepth == 0 {
                    return token.end
                }
            } else if !token.isVoid {
                sectionDepth += 1
            }
        }
        return nil
    }

    func extractSections(from html: String) -> [(range: Range<String.Index>, content: String)] {
        var sections: [(Range<String.Index>, String)] = []
        let comments = commentRanges(in: html)
        let rawRanges = rawTextRanges(in: html) + templateRanges(in: html)
        let searchBounds = deckContentRange(in: html) ?? html.startIndex..<html.endIndex
        var searchFrom = searchBounds.lowerBound
        var deckChildDepth = 0

        while let token = nextHTMLTag(in: html, from: searchFrom, within: searchBounds) {
            searchFrom = token.end
            if isInsideRawText(token.start, ranges: comments) || isInsideRawText(token.start, ranges: rawRanges) {
                continue
            }

            if token.isClosing {
                deckChildDepth = max(0, deckChildDepth - 1)
                continue
            }

            if token.name == "section", deckChildDepth == 0, hasClass("slide", inOpeningTag: token.text),
               let closeEnd = matchingSectionEnd(in: html, from: token.end, within: searchBounds, comments: comments, rawRanges: rawRanges) {
                let range = token.start..<closeEnd
                sections.append((range, String(html[range])))
                searchFrom = closeEnd
                continue
            }

            if !token.isVoid {
                deckChildDepth += 1
            }
        }

        return sections
    }

    // MARK: - JS String Escaping

    func jsStringLiteral(_ s: String) -> String {
        // Use JSONSerialization to properly escape for JS
        guard let data = try? JSONSerialization.data(withJSONObject: [s]),
              let json = String(data: data, encoding: .utf8) else {
            return "\"\""
        }
        // json is ["...escaped..."], extract just the string (with quotes)
        let start = json.index(json.startIndex, offsetBy: 1)
        let end = json.index(json.endIndex, offsetBy: -1)
        return String(json[start..<end])
    }

    func splitSection(_ html: String) -> SectionParts? {
        guard let openStart = html.range(of: "<section", options: .caseInsensitive),
              let openEnd = openingTagEnd(in: html, from: openStart.lowerBound),
              let close = html.range(of: "</section>", options: [.caseInsensitive, .backwards]) else {
            return nil
        }
        return SectionParts(
            openingTag: String(html[openStart.lowerBound...openEnd]),
            body: String(html[html.index(after: openEnd)..<close.lowerBound]),
            closingTag: String(html[close.lowerBound..<close.upperBound])
        )
    }

    func openingTagEnd(in html: String, from start: String.Index) -> String.Index? {
        var i = start
        var quote: Character? = nil
        while i < html.endIndex {
            let ch = html[i]
            if let q = quote {
                if ch == q { quote = nil }
            } else if ch == "\"" || ch == "'" {
                quote = ch
            } else if ch == ">" {
                return i
            }
            i = html.index(after: i)
        }
        return nil
    }

    func sectionHTML(openingTag: String, body: String, closingTag: String) -> String {
        openingTag + body + closingTag
    }

    // MARK: - Editor Sync

    func sendCurrentSlideToEditor(discardLocalChanges: Bool = false) {
        // A direct mirror supersedes any debounced one.
        cancelEditorMirror()
        guard isEditorMode, editorReady,
              currentEditorSlideIndex >= 0,
              currentEditorSlideIndex < sectionInfos.count,
              let eWV = editorWebView,
              let parts = splitSection(sectionInfos[currentEditorSlideIndex].content) else { return }

        let opening = jsStringLiteral(parts.openingTag)
        let body = jsStringLiteral(parts.body)
        let closing = jsStringLiteral(parts.closingTag)
        let total = sectionInfos.count
        let slideNum = currentEditorSlideIndex + 1
        let bodyLine = bodyStartLine(forSlide: currentEditorSlideIndex, openingTag: parts.openingTag)
        let js = "setContent(\(opening), \(body), \(closing), \(currentEditorSlideIndex), \(slideNum), \(total), \(discardLocalChanges ? "true" : "false"), \(bodyLine))"
        eWV.evaluateJavaScript(js) { _, error in
            if let error = error {
                NSLog("Presto: setContent error: %@", "\(error)")
            }
        }
        // Also push refine state for the slide we just navigated to
        pushRefineStateToEditor()
    }

    // The 1-based line of the deck file on which the slide's body starts, so
    // the inspector can number source lines as the file does.
    func bodyStartLine(forSlide index: Int, openingTag: String) -> Int {
        guard let content = fileContent, index >= 0, index < sectionInfos.count else { return 1 }
        let start = sectionInfos[index].range.lowerBound
        let bodyStart = content.index(start, offsetBy: openingTag.count, limitedBy: content.endIndex) ?? start
        return content[content.startIndex..<bodyStart].reduce(1) { $1 == "\n" ? $0 + 1 : $0 }
    }

    func clearSaveTransactionSoon() {
        let generation = sessionGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            guard let self = self, self.sessionGeneration == generation else { return }
            self.saveTransactionCount = max(0, self.saveTransactionCount - 1)
        }
    }

    func writeDeckContent(_ content: String, to deckURL: URL,
                          successMessage: String, failurePrefix: String) -> Error? {
        guard !isPresentingExternalConflict else {
            return NSError(domain: "Presto.save", code: 1,
                           userInfo: [NSLocalizedDescriptionKey: "Resolve the external deck change before saving"])
        }
        saveTransactionCount += 1
        do {
            // Recheck at the write boundary: the watcher may not have delivered
            // an external editor's change before the debounce fires.
            if let baseline = lastPersistedContent {
                let disk = try String(contentsOf: deckURL, encoding: .utf8)
                if disk != baseline && disk != content {
                    if !isPresentingExternalConflict {
                        presentExternalChangeConflict(diskContent: disk)
                    }
                    let reason = externalMergeDeferred
                        ? "Deck changed on disk; Presto will merge it when you finish this text edit"
                        : "Deck changed on disk; merging it with your edits"
                    throw NSError(domain: "Presto.save", code: 1,
                                  userInfo: [NSLocalizedDescriptionKey: reason])
                }
                // Never replace bytes Presto did not write without keeping a copy.
                if disk != content && disk != lastPrestoWrittenContent {
                    do {
                        try backupDeckVersion(disk, of: deckURL)
                    } catch {
                        throw NSError(domain: "Presto.save", code: 2, userInfo: [
                            NSLocalizedDescriptionKey: "Could not back up the disk version, so it was not overwritten: \(error.localizedDescription)"
                        ])
                    }
                }
            }
            try content.write(to: deckURL, atomically: true, encoding: .utf8)
            lastPersistedContent = content
            lastPrestoWrittenContent = content
            deckWrittenThisSession = true
            rearmFileWatcherAfterOwnWrite()
            NSLog("%@", successMessage)
            clearSaveTransactionSoon()
            return nil
        } catch {
            NSLog("%@: %@", failurePrefix, error.localizedDescription)
            clearSaveTransactionSoon()
            return error
        }
    }

    // MARK: - Debounced Disk Write

    // handleEditorChanged updates fileContent/sectionInfos synchronously and
    // schedules a single trailing-edge write here so a burst of nudges/keystrokes
    // produces one disk write instead of 15-30/sec. flushPendingDeckWrite runs on
    // every teardown/reload path. Save confirmations follow a successful write.
    func scheduleDeckWrite() {
        deckWriteDirty = true
        setSaveStatus(.saving)
        pendingDeckWrite?.cancel()
        guard !isPresentingExternalConflict else { pendingDeckWrite = nil; return }
        let generation = sessionGeneration
        let work = DispatchWorkItem { [weak self] in
            guard let self = self, self.sessionGeneration == generation else { return }
            self.flushPendingDeckWrite()
        }
        pendingDeckWrite = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: work)
    }

    @discardableResult
    func flushPendingDeckWrite() -> Bool {
        pendingDeckWrite?.cancel()
        pendingDeckWrite = nil
        guard deckWriteDirty else { return true }
        guard !isPresentingExternalConflict else { return false }
        guard let content = fileContent, let deckURL = currentDeckURL else { return false }
        if let error = writeDeckContent(content, to: deckURL,
                                        successMessage: "Presto: saved deck to disk (debounced)",
                                        failurePrefix: "Presto: failed to save deck") {
            // Keep the in-memory version dirty so Save, close, or quit can retry.
            reportEditorSaveFailed("Save failed: \(error.localizedDescription)")
            previewWebView?.evaluateJavaScript(
                "window.__prestoWysiwyg && __prestoWysiwyg.saveFailed(0, \(jsStringLiteral("Save failed: \(error.localizedDescription)")))"
            ) { _, _ in }
            setSaveStatus(.failed(error.localizedDescription))
            return false
        }
        deckWriteDirty = false
        setSaveStatus(.saved)
        confirmPendingSaves()
        return true
    }

    func confirmPendingSaves() {
        if let saveId = pendingEditorSaveID {
            editorWebView?.evaluateJavaScript("saveConfirmed(\(saveId))") { _, _ in }
        }
        if let saveId = pendingWysiwygSaveID {
            previewWebView?.evaluateJavaScript(
                "window.__prestoWysiwyg && __prestoWysiwyg.saveConfirmed(\(saveId))"
            ) { _, _ in }
        }
        pendingEditorSaveID = nil
        pendingWysiwygSaveID = nil
    }

    func cancelPendingDeckWrite() {
        pendingDeckWrite?.cancel()
        pendingDeckWrite = nil
        deckWriteDirty = false
    }

    // Debounced code-pane mirror: setContent re-tokenizes and re-highlights the
    // whole slide, so coalesce rapid WYSIWYG commits. Any direct
    // sendCurrentSlideToEditor supersedes a pending one.
    func scheduleEditorMirror() {
        pendingEditorMirror?.cancel()
        let generation = sessionGeneration
        let work = DispatchWorkItem { [weak self] in
            guard let self = self, self.sessionGeneration == generation else { return }
            self.pendingEditorMirror = nil
            self.sendCurrentSlideToEditor()
        }
        pendingEditorMirror = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: work)
    }

    func flushEditorMirror() {
        guard pendingEditorMirror != nil else { return }
        sendCurrentSlideToEditor()
    }

    func cancelEditorMirror() {
        pendingEditorMirror?.cancel()
        pendingEditorMirror = nil
    }

    // Shown on both edit surfaces: the source pane is hidden by default, so
    // the visual editor's status line carries it too.
    func reportDeckSaveProblem(_ message: String) {
        reportEditorSaveFailed(message)
        previewWebView?.evaluateJavaScript(
            "window.__prestoWysiwyg && __prestoWysiwyg.saveFailed(0, \(jsStringLiteral(message)))"
        ) { _, _ in }
        setSaveStatus(.failed(message))
    }

    func reportEditorSaveFailed(_ message: String, saveId: Int? = nil) {
        let js: String
        if let saveId = saveId {
            js = "saveFailed(\(saveId), \(jsStringLiteral(message)))"
        } else {
            js = "saveFailed(\(jsStringLiteral(message)))"
        }
        editorWebView?.evaluateJavaScript(js) { _, _ in }
    }

    // A slide patch either reflects an outside change (no `change`: the visual
    // editor drops that slide's history) or a Presto commit that ⌘Z must be
    // able to undo (a committed source edit or a Refine result).
    func previewSlidePatchJS(index: Int, change: RecordedSlideChange? = nil) -> String? {
        guard index >= 0, index < sectionInfos.count else { return nil }
        let sectionHTML = jsStringLiteral(sectionInfos[index].content)
        var options = ""
        if let change {
            let prompt = change.prompt.map { jsStringLiteral($0) } ?? "null"
            options = ", { origin: \(jsStringLiteral(change.origin)), beforeHTML: \(jsStringLiteral(change.beforeHTML)), prompt: \(prompt) }"
        }
        return """
            (function() {
                if (typeof window.replacePrestoEditSlideHTML !== 'function') { return false; }
                return window.replacePrestoEditSlideHTML(\(index), \(sectionHTML)\(options));
            })()
        """
    }

    func previewSlidePatchStatement(index: Int?, change: RecordedSlideChange? = nil) -> String {
        guard let index = index, let js = previewSlidePatchJS(index: index, change: change) else {
            return "window.__prestoLastPatchOK = true;"
        }
        return "window.__prestoLastPatchOK = (\(js));"
    }

    func previewNavigationJS(command: String, savedIndex: Int?, change: RecordedSlideChange? = nil) -> String {
        let patchStatement = previewSlidePatchStatement(index: savedIndex, change: change)
        let resultJSON = "JSON.stringify({ index: WS.currentIndex, patchOK: window.__prestoLastPatchOK !== false })"
        return "\(patchStatement) \(command); \(resultJSON)"
    }

    func refreshPreviewSlide(_ index: Int, recording change: RecordedSlideChange? = nil) {
        guard let pWV = previewWebView, let js = previewSlidePatchJS(index: index, change: change) else { return }
        let generation = sessionGeneration
        pWV.evaluateJavaScript(js) { [weak self] result, error in
            guard let self = self,
                  self.sessionGeneration == generation,
                  self.previewWebView === pWV else { return }
            if let error = error {
                NSLog("Presto: preview slide patch error: %@", "\(error)")
            }
            if (result as? Bool) != true {
                self.reloadPreviewAtSlide(self.currentEditorSlideIndex)
            }
        }
    }

    func handleEditorChanged(html: String, index: Int, reloadPreview: Bool = true, saveId: Int? = nil) -> Bool {
        guard isEditorMode,
              index >= 0,
              index < sectionInfos.count,
              var content = fileContent,
              currentDeckURL != nil,
              let parts = splitSection(sectionInfos[index].content) else {
            reportEditorSaveFailed("Cannot apply editor change", saveId: saveId)
            return false
        }

        // Update stored section
        let oldRange = sectionInfos[index].range
        let before = sectionInfos[index].content
        let replacement = sectionHTML(
            openingTag: parts.openingTag,
            body: html,
            closingTag: parts.closingTag
        )
        content.replaceSubrange(oldRange, with: replacement)
        let updatedSections = extractSections(from: content)

        guard updatedSections.count == sectionInfos.count,
              updatedSections[index].content == replacement,
              deckStructureIssues(in: content).isEmpty else {
            reportEditorSaveFailed("Editor change altered slide boundaries", saveId: saveId)
            return false
        }

        // Apply in memory immediately (so later commits, navigation, and preview
        // patches see the latest content); debounce the whole-deck disk write.
        // Acknowledge only after the pending version reaches disk.
        fileContent = content
        sectionInfos = updatedSections
        scheduleDeckWrite()

        // Confirm save to editor
        if let saveId = saveId {
            pendingEditorSaveID = saveId
        }

        // Refresh preview to reflect changes without reloading the whole deck.
        // A committed source edit joins the visual editor's undo history; a
        // commit that changed nothing leaves the slide (and its history) alone.
        if reloadPreview && before != replacement {
            refreshPreviewSlide(index, recording: RecordedSlideChange(origin: "source", beforeHTML: before))
        }
        return true
    }

    // Undo or redo of a recorded source or Refine change replaces the whole
    // section (a Refine may have changed its opening tag). It applies only
    // while Presto's copy of the slide is still what the history expects, so
    // an undo never overwrites a newer edit, merge or disk version.
    @discardableResult
    func applyHistorySection(index: Int, sectionHTML replacement: String, expected: String?,
                             saveId: Int?) -> Bool {
        let refusal = "This slide changed since; nothing was undone"
        guard isEditorMode, index >= 0, index < sectionInfos.count,
              var content = fileContent, currentDeckURL != nil else {
            reportHistoryRefusal(index: index, saveId: saveId, message: refusal)
            return false
        }
        guard expected == nil || sectionInfos[index].content == expected else {
            NSLog("Presto: refused history change on slide %d: the slide changed since", index)
            reportHistoryRefusal(index: index, saveId: saveId, message: refusal)
            return false
        }
        content.replaceSubrange(sectionInfos[index].range, with: replacement)
        let updatedSections = extractSections(from: content)
        guard updatedSections.count == sectionInfos.count,
              updatedSections[index].content == replacement,
              deckStructureIssues(in: content).isEmpty else {
            reportHistoryRefusal(index: index, saveId: saveId,
                                 message: "That undo would change the slide boundaries; nothing was undone")
            return false
        }
        fileContent = content
        sectionInfos = updatedSections
        scheduleDeckWrite()
        if let saveId { pendingWysiwygSaveID = saveId }
        scheduleEditorMirror()
        return true
    }

    // The visual editor already shows the refused version: it puts the entry
    // back on its stack first (saveFailed), then the slide shows Presto's
    // copy through a history patch, which neither clears nor records, so the
    // slide's undo history survives the refusal.
    func reportHistoryRefusal(index: Int, saveId: Int?, message: String) {
        previewWebView?.evaluateJavaScript(
            "window.__prestoWysiwyg && __prestoWysiwyg.saveFailed(\(saveId ?? 0), \(jsStringLiteral(message)))"
        ) { _, _ in }
        refreshPreviewSlide(index, recording: RecordedSlideChange(origin: "history", beforeHTML: ""))
    }

    func reloadPreviewAtSlide(_ index: Int) {
        guard let deckURL = currentDeckURL, let pWV = previewWebView else { return }
        // A full reload loads from disk, so persist any debounced edit first or
        // the reloaded page would drop it. (No-op when the caller already
        // cancelled the pending write, e.g. an external-change reload.)
        guard flushPendingDeckWrite() else { return }
        isReloadingPreview = true
        currentEditorSlideIndex = index
        if pWV.url?.standardizedFileURL.path == deckURL.standardizedFileURL.path {
            // Re-opening a file URL can reuse stale CSS and JavaScript. A reload
            // from origin refreshes those resources along with the deck.
            pWV.reloadFromOrigin()
        } else {
            let readAccessRoot = findReadAccessRoot(deckURL: deckURL)
            pWV.loadFileURL(deckURL, allowingReadAccessTo: readAccessRoot)
        }
    }

    func clampCurrentEditorSlideIndex() {
        if sectionInfos.isEmpty {
            currentEditorSlideIndex = 0
        } else {
            currentEditorSlideIndex = max(0, min(currentEditorSlideIndex, sectionInfos.count - 1))
        }
    }

    func pendingEditorChangeJS() -> String {
        return "JSON.stringify(window.getPendingEditorChange ? window.getPendingEditorChange() : null)"
    }

    func handleEditorPreviewNavigationResult(_ result: Any?) {
        var targetIndex: Int? = finiteInt(result)
        var patchOK = true

        if let json = result as? String,
           let data = json.data(using: .utf8),
           let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            targetIndex = finiteInt(payload["index"])
            patchOK = (payload["patchOK"] as? Bool) ?? true
        }

        guard let idx = targetIndex, idx >= 0, idx < sectionInfos.count else { return }
        if !patchOK {
            reloadPreviewAtSlide(idx)
            return
        }

        let changed = currentEditorSlideIndex != idx
        currentEditorSlideIndex = idx
        if changed {
            sendCurrentSlideToEditor()
        }
    }

    func applyPendingEditorChangePayload(_ result: Any?) -> PendingEditorChangeResult {
        guard let json = result as? String,
              let data = json.data(using: .utf8),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let dirty = payload["dirty"] as? Bool else {
            reportEditorSaveFailed("Cannot read pending editor change")
            return .failed
        }
        guard dirty else { return .clean }
        guard let body = payload["html"] as? String,
              let editorIndex = finiteInt(payload["index"]) else {
            reportEditorSaveFailed("Cannot read pending editor change")
            return .failed
        }
        let before = editorIndex >= 0 && editorIndex < sectionInfos.count ? sectionInfos[editorIndex].content : nil
        let saved = handleEditorChanged(html: body, index: editorIndex, reloadPreview: false)
        guard saved else { return .failed }
        let js = "markEditorSaved(\(editorIndex), \(jsStringLiteral(body)))"
        editorWebView?.evaluateJavaScript(js) { _, _ in }
        // Unchanged: nothing to patch, so the slide's undo history is kept.
        guard let before, before != sectionInfos[editorIndex].content else { return .clean }
        return .saved(index: editorIndex, beforeHTML: before)
    }

    func flushEditorThenNavigate(to index: Int, step: Int = 0) {
        guard isEditorMode, index >= 0, index < sectionInfos.count,
              let eWV = editorWebView, let pWV = previewWebView else {
            return
        }
        let step = max(0, step)
        let generation = sessionGeneration
        let proceed: () -> Void = { [weak self] in
            guard let self = self,
                  self.sessionGeneration == generation,
                  self.editorWebView === eWV,
                  self.previewWebView === pWV else { return }
            eWV.evaluateJavaScript(self.pendingEditorChangeJS()) { [weak self] result, _ in
                guard let self = self,
                      self.sessionGeneration == generation,
                      self.editorWebView === eWV,
                      self.previewWebView === pWV else { return }
                let pending = self.applyPendingEditorChangePayload(result)
                guard pending.ok else { return }
                let js = self.previewNavigationJS(command: "WS.goToSlide(\(index), \(step))", savedIndex: pending.savedIndex,
                                                  change: pending.change)
                pWV.evaluateJavaScript(js) { [weak self] navResult, _ in
                    guard let self = self,
                          self.sessionGeneration == generation,
                          self.previewWebView === pWV else { return }
                    self.handleEditorPreviewNavigationResult(navResult)
                }
            }
        }
        // A live text session is committed first so navigating away can't drop
        // the just-typed text or race the code-pane flush.
        if wysiwygTextEditingActive { commitActiveWysiwygSession(proceed) } else { proceed() }
    }

    func flushEditorThenRunPreviewCommand(_ command: String) {
        guard isEditorMode, let eWV = editorWebView, let pWV = previewWebView else {
            navigateEditorPreview(command: command)
            return
        }
        let generation = sessionGeneration
        let proceed: () -> Void = { [weak self] in
            guard let self = self,
                  self.sessionGeneration == generation,
                  self.editorWebView === eWV,
                  self.previewWebView === pWV else { return }
            eWV.evaluateJavaScript(self.pendingEditorChangeJS()) { [weak self] result, _ in
                guard let self = self,
                      self.sessionGeneration == generation,
                      self.editorWebView === eWV,
                      self.previewWebView === pWV else { return }
                let pending = self.applyPendingEditorChangePayload(result)
                guard pending.ok else { return }
                let js = self.previewNavigationJS(command: command, savedIndex: pending.savedIndex,
                                                  change: pending.change)
                pWV.evaluateJavaScript(js) { [weak self] navResult, _ in
                    guard let self = self,
                          self.sessionGeneration == generation,
                          self.previewWebView === pWV else { return }
                    self.handleEditorPreviewNavigationResult(navResult)
                }
            }
        }
        if wysiwygTextEditingActive { commitActiveWysiwygSession(proceed) } else { proceed() }
    }

    // MARK: - AI Refine

    func endOfOpeningTag(in html: String, from start: String.Index) -> String.Index? {
        var i = start
        var quote: Character?
        while i < html.endIndex {
            let ch = html[i]
            if let q = quote {
                if ch == q {
                    quote = nil
                }
            } else if ch == "\"" || ch == "'" {
                quote = ch
            } else if ch == ">" {
                return html.index(after: i)
            }
            i = html.index(after: i)
        }
        return nil
    }

    func isInHTMLComment(_ position: String.Index, in html: String) -> Bool {
        let prefix = html[..<position]
        guard let lastOpen = prefix.range(of: "<!--", options: .backwards) else {
            return false
        }
        guard let lastClose = prefix.range(of: "-->", options: .backwards) else {
            return true
        }
        return lastClose.lowerBound < lastOpen.lowerBound
    }

    func extractDeckHead(from html: String) -> String {
        var searchStart = html.startIndex
        let ignoredRanges = templateRanges(in: html)
        while let openTag = html.range(of: "<head", options: .caseInsensitive,
                                       range: searchStart..<html.endIndex) {
            searchStart = openTag.upperBound
            if isInHTMLComment(openTag.lowerBound, in: html) || isInsideRawText(openTag.lowerBound, ranges: ignoredRanges) {
                continue
            }
            guard let headContentStart = endOfOpeningTag(in: html, from: openTag.upperBound) else {
                return ""
            }
            var closeSearchStart = headContentStart
            while let closeTag = html.range(of: "</head>", options: .caseInsensitive,
                                            range: closeSearchStart..<html.endIndex) {
                closeSearchStart = closeTag.upperBound
                if isInHTMLComment(closeTag.lowerBound, in: html) || isInsideRawText(closeTag.lowerBound, ranges: ignoredRanges) {
                    continue
                }
                return String(html[headContentStart..<closeTag.lowerBound])
            }
            return ""
        }
        return ""
    }

    func buildRefinePrompt(slideHTML: String, deckHead: String, instructions: String) -> String {
        return """
        You are editing one slide of a WebSlides HTML deck.

        IMPORTANT: First, load the /webslides skill for syntax reference (data-step, data-overlays, slide types, emphasis classes, etc.). Use it as your authoritative reference for all WebSlides-specific attributes.

        The deck's <head> contents (CSS classes, theme — read only, do NOT output):

        <<<HEAD
        \(deckHead)
        HEAD

        The current slide HTML (a single <section>...</section> block):

        <<<SLIDE
        \(slideHTML)
        SLIDE

        Apply this instruction to the slide:

        \(instructions)

        Strict rules:
        - Output ONLY the refined <section>...</section> block.
        - Do NOT wrap the output in markdown fences (```), do NOT add commentary, do NOT explain.
        - Output must start with <section and end with </section>.
        - Preserve the WebSlides structure unless the instruction asks otherwise.
        - Do not modify the <head> — it is reference only.
        """
    }

    func sanitizeRefineOutput(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        // Strip leading ```html or ``` fence
        if s.hasPrefix("```") {
            if let firstNL = s.firstIndex(of: "\n") {
                s = String(s[s.index(after: firstNL)...])
            } else {
                s = String(s.dropFirst(3))
            }
            if s.hasSuffix("```") {
                s = String(s.dropLast(3))
            }
            s = s.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let lower = s.lowercased()
        if !lower.hasPrefix("<section") || !lower.hasSuffix("</section>") {
            return ""
        }
        return s
    }

    func findClaudeBinary() -> String? {
        let home = NSHomeDirectory()
        let candidates = [
            "\(home)/.local/bin/claude",
            "\(home)/.claude/local/claude",
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
            "\(home)/bin/claude"
        ]
        for path in candidates {
            if FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }
        return nil
    }

    func runClaudeRefineSubprocess(prompt: String) throws -> String {
        guard let claudePath = findClaudeBinary() else {
            throw NSError(domain: "Presto.refine", code: 10,
                          userInfo: [NSLocalizedDescriptionKey: "claude binary not found in ~/.local/bin, ~/.claude/local, /usr/local/bin, /opt/homebrew/bin, or ~/bin"])
        }

        let arguments = [
            "-p",
            "--model", "sonnet",
            "--output-format", "json",
            "--dangerously-skip-permissions"
        ]
        // Inherit a usable PATH so claude can find node, etc.
        var env = ProcessInfo.processInfo.environment
        let extraPaths = "\(NSHomeDirectory())/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
        if let existing = env["PATH"], !existing.isEmpty {
            env["PATH"] = "\(extraPaths):\(existing)"
        } else {
            env["PATH"] = extraPaths
        }
        let result = try runRefineSubprocess(
            executableURL: URL(fileURLWithPath: claudePath),
            arguments: arguments,
            environment: env,
            input: Data(prompt.utf8),
            timeout: 90
        )

        guard result.terminationStatus == 0 else {
            let errMsg = String(decoding: result.stderr.prefix(4096), as: UTF8.self)
            throw NSError(domain: "Presto.refine", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "claude failed (exit \(result.terminationStatus)): \(errMsg.prefix(500))"])
        }

        guard let output = String(data: result.stdout, encoding: .utf8), !output.isEmpty else {
            throw NSError(domain: "Presto.refine", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "empty output from claude"])
        }

        // Parse JSON: {"result": "..."} or {"type":"result","result":"..."}
        guard let jsonData = output.data(using: .utf8),
              let dict = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] else {
            throw NSError(domain: "Presto.refine", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "non-JSON output: \(output.prefix(300))"])
        }

        if let result = dict["result"] as? String {
            return result
        }
        throw NSError(domain: "Presto.refine", code: 4,
                      userInfo: [NSLocalizedDescriptionKey: "missing 'result' field in claude output"])
    }

    func enqueueRefine(slideIndex: Int, instructions: String) {
        let trimmed = instructions.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              slideIndex >= 0, slideIndex < sectionInfos.count else { return }

        let task = RefineTask(id: UUID(), slideIndex: slideIndex, instructions: trimmed)
        refineQueues[slideIndex, default: []].append(task)
        NSLog("Presto: refine queued for slide %d: %@", slideIndex, trimmed)

        if slideIndex == currentEditorSlideIndex {
            pushRefineStateToEditor()
        }
        processRefineQueue(for: slideIndex)
    }

    func processRefineQueue(for slideIndex: Int) {
        // Always called on main thread.
        guard refineRunningInfo[slideIndex] == nil else { return }
        guard var queue = refineQueues[slideIndex], !queue.isEmpty else { return }

        let task = queue.removeFirst()
        refineQueues[slideIndex] = queue

        guard slideIndex < sectionInfos.count, let content = fileContent else {
            // Slide gone — drop the task
            return
        }
        let slideHTML = sectionInfos[slideIndex].content
        let deckHead = extractDeckHead(from: content)
        let prompt = buildRefinePrompt(slideHTML: slideHTML,
                                       deckHead: deckHead,
                                       instructions: task.instructions)
        let preHTML = slideHTML

        refineRunningInfo[slideIndex] = task
        if slideIndex == currentEditorSlideIndex {
            pushRefineStateToEditor()
        }

        let taskID = task.id
        let instructions = task.instructions
        let targetSlide = slideIndex
        let generation = sessionGeneration
        let targetDeckURL = currentDeckURL

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            do {
                let raw = try self.runClaudeRefineSubprocess(prompt: prompt)
                DispatchQueue.main.async {
                    guard self.sessionGeneration == generation,
                          self.currentDeckURL == targetDeckURL,
                          self.refineRunningInfo[targetSlide]?.id == taskID else {
                        NSLog("Presto: discarded refine result from stale session")
                        return
                    }
                    // Clear running BEFORE apply so the state pushed to editor is fresh
                    self.refineRunningInfo[targetSlide] = nil
                    self.applyRefineResult(slideIndex: targetSlide, taskID: taskID,
                                           instructions: instructions,
                                           preHTML: preHTML, refinedRaw: raw)
                    self.processRefineQueue(for: targetSlide)
                }
            } catch {
                DispatchQueue.main.async {
                    guard self.sessionGeneration == generation,
                          self.currentDeckURL == targetDeckURL,
                          self.refineRunningInfo[targetSlide]?.id == taskID else {
                        NSLog("Presto: discarded refine error from stale session")
                        return
                    }
                    // Clear running BEFORE apply so the state pushed to editor is fresh
                    self.refineRunningInfo[targetSlide] = nil
                    self.applyRefineError(slideIndex: targetSlide, taskID: taskID,
                                          instructions: instructions,
                                          preHTML: preHTML, error: error)
                    self.processRefineQueue(for: targetSlide)
                }
            }
        }
    }

    func applyRefineResult(slideIndex: Int, taskID: UUID, instructions: String,
                           preHTML: String, refinedRaw: String) {
        let cleaned = sanitizeRefineOutput(refinedRaw)
        guard !cleaned.isEmpty else {
            let err = NSError(domain: "Presto.refine", code: 5,
                              userInfo: [NSLocalizedDescriptionKey: "Claude returned invalid HTML (no <section>)"])
            applyRefineError(slideIndex: slideIndex, taskID: taskID,
                             instructions: instructions, preHTML: preHTML, error: err)
            return
        }
        guard slideIndex >= 0, slideIndex < sectionInfos.count,
              var content = fileContent, let deckURL = currentDeckURL else { return }

        guard sectionInfos[slideIndex].content == preHTML,
              !(slideIndex == currentEditorSlideIndex && (wysiwygTextEditingActive || sourceEditorDirty)) else {
            let err = NSError(domain: "Presto.refine", code: 6,
                              userInfo: [NSLocalizedDescriptionKey: "Slide changed while refine was running; skipped stale AI output"])
            applyRefineError(slideIndex: slideIndex, taskID: taskID,
                             instructions: instructions, preHTML: preHTML, error: err)
            return
        }

        // Splice into in-memory deck and write to disk
        let oldRange = sectionInfos[slideIndex].range
        content.replaceSubrange(oldRange, with: cleaned)
        let updatedSections = extractSections(from: content)

        guard updatedSections.count == sectionInfos.count,
              updatedSections[slideIndex].content == cleaned,
              deckStructureIssues(in: content).isEmpty else {
            let error = NSError(domain: "Presto.refine", code: 5,
                                userInfo: [NSLocalizedDescriptionKey: "AI output must replace exactly one valid slide"])
            applyRefineError(slideIndex: slideIndex, taskID: taskID,
                             instructions: instructions, preHTML: preHTML, error: error)
            return
        }
        if let error = writeDeckContent(content, to: deckURL,
                                        successMessage: "Presto: refine saved slide \(slideIndex) to disk",
                                        failurePrefix: "Presto: failed to save refine") {
            let err = NSError(domain: "Presto.refine", code: 7,
                              userInfo: [NSLocalizedDescriptionKey: "Failed to save refined slide: \(error.localizedDescription)"])
            applyRefineError(slideIndex: slideIndex, taskID: taskID,
                             instructions: instructions, preHTML: preHTML, error: err)
            return
        }

        fileContent = content
        sectionInfos = updatedSections
        cancelPendingDeckWrite()  // disk now matches fileContent
        setSaveStatus(.saved)
        confirmPendingSaves()
        refineErrors[slideIndex] = nil

        // Update editor textarea + preview if visible. The patch records the
        // result in the unified undo history, so ⌘Z (or Refine's Undo last)
        // takes it back through the same checked save path as any edit.
        if slideIndex == currentEditorSlideIndex {
            sendCurrentSlideToEditor()
        }
        refreshPreviewSlide(slideIndex, recording: RecordedSlideChange(origin: "refine", beforeHTML: preHTML,
                                                                       prompt: instructions))
        // After the patch, so the Refine history it reads includes this one.
        if slideIndex == currentEditorSlideIndex {
            pushRefineStateToEditor()
        }
    }

    func applyRefineError(slideIndex: Int, taskID: UUID, instructions: String,
                          preHTML: String, error: Error) {
        let msg = error.localizedDescription
        NSLog("Presto: refine failed for slide %d: %@", slideIndex, msg)
        // Error toast only — do not pollute the history/undo stack with failed attempts.
        refineErrors[slideIndex] = "[\(instructions)] → \(msg)"

        if slideIndex == currentEditorSlideIndex {
            pushRefineStateToEditor()
        }
    }

    // Refine's state for the inspector. Its history and Undo last come from
    // the unified undo history in the preview (PrestoEdit.refineHistory).
    func pushRefineStateToEditor() {
        guard isEditorMode, editorReady, let eWV = editorWebView else { return }
        let idx = currentEditorSlideIndex
        let generation = sessionGeneration

        var state: [String: Any] = [
            "slideIndex": idx,
            "running": NSNull(),
            "queued": [],
            "history": [],
            "canUndo": editUndoState.canUndo,
            "canRedo": editUndoState.canRedo,
            "undoLabel": editUndoState.undoLabel,
            "redoLabel": editUndoState.redoLabel,
            "lastError": NSNull()
        ]

        if let task = refineRunningInfo[idx] {
            state["running"] = ["instructions": task.instructions]
        }
        if let queue = refineQueues[idx], !queue.isEmpty {
            state["queued"] = queue.map { ["instructions": $0.instructions] }
        }
        if let err = refineErrors[idx] {
            state["lastError"] = err
        }

        let send: ([Any]) -> Void = { [weak self, weak eWV] history in
            guard let self, let eWV, self.sessionGeneration == generation,
                  self.editorWebView === eWV else { return }
            state["history"] = history
            guard let data = try? JSONSerialization.data(withJSONObject: state),
                  let json = String(data: data, encoding: .utf8) else { return }
            let js = "if (window.prestoRefine && window.prestoRefine.setState) { window.prestoRefine.setState(\(json)); }"
            eWV.evaluateJavaScript(js) { _, error in
                if let error = error {
                    NSLog("Presto: pushRefineStateToEditor error: %@", "\(error)")
                }
            }
        }
        guard let preview = previewWebView else { return send([]) }
        let historyJS = "JSON.stringify((window.PrestoEdit && typeof PrestoEdit.refineHistory === 'function') ? (PrestoEdit.refineHistory(\(idx)) || []) : [])"
        preview.evaluateJavaScript(historyJS) { result, _ in
            var history: [Any] = []
            if let json = result as? String, let data = json.data(using: .utf8),
               let parsed = try? JSONSerialization.jsonObject(with: data) as? [Any] {
                history = parsed
            }
            send(history)
        }
    }

    // MARK: - WKWebView Config

    func makeBaseConfig() -> WKWebViewConfiguration {
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "allowFileAccessFromFileURLs")
        config.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        addFetchPolyfill(to: config)
        if let rules = AppDelegate.cdnKaTeXRuleList {
            config.userContentController.add(rules)
        }
        return config
    }

    // Decks link KaTeX from jsDelivr; the engine falls back to its vendored
    // copy when that load fails. Blocking it outright means a hanging venue
    // network can never delay a deck, and the maths always matches the
    // vendored version. Compiled once at launch; without it Presto still works.
    static var cdnKaTeXRuleList: WKContentRuleList?
    static let cdnKaTeXRules = #"[{"trigger":{"url-filter":"^https?://cdn\\.jsdelivr\\.net/npm/katex@"},"action":{"type":"block"}}]"#

    func compileContentRules(completion: @escaping () -> Void) {
        guard AppDelegate.cdnKaTeXRuleList == nil, let store = WKContentRuleListStore.default() else {
            completion()
            return
        }
        var finished = false
        let finish = {
            guard !finished else { return }
            finished = true
            completion()
        }
        store.compileContentRuleList(forIdentifier: "presto-block-cdn-katex",
                                     encodedContentRuleList: AppDelegate.cdnKaTeXRules) { list, error in
            DispatchQueue.main.async {
                if let list {
                    AppDelegate.cdnKaTeXRuleList = list
                } else {
                    NSLog("Presto: could not compile the KaTeX CDN rule: %@", error?.localizedDescription ?? "unknown error")
                }
                finish()
            }
        }
        // Never hold up launch for long.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { finish() }
    }

    // MARK: - Deck Readiness

    // True once the deck can be measured or captured. The engine's WS.ready
    // resolves, and sets <html data-ws-ready="1">, after external tables,
    // KaTeX, fonts and the final table fit; WS.slides exists well before.
    // Engines older than WS.ready only offer the slide list.
    static let deckReadyCheckJS = """
        (typeof WS !== 'undefined' && !!WS.slides && WS.slides.length > 0 &&
         (!WS.ready || document.documentElement.dataset.wsReady === '1'))
        """

    // PDF export polls deckReadyCheckJS every 0.1 s for up to 30 s, the tools'
    // limit: the engine bounds each of its outside waits at 5 s.
    static let exportReadyPollLimit = 300

    // For scripts that run at first paint, before WS.ready. A deck that is
    // already ready (or whose engine predates WS.ready) calls back at once.
    static let deckReadyHelpersJS = """
        function prestoWhenDeckReady(callback) {
            if (!WS.ready || typeof WS.ready.then !== 'function' ||
                document.documentElement.dataset.wsReady === '1') { callback(); return; }
            WS.ready.then(callback);
        }
        // goToSlide clamps the step to the steps known so far, and reveals in
        // external tables count only once the tables load. Re-apply a clamped
        // step when the deck is ready, unless the slide or step moved since.
        function prestoRestoreStepWhenReady(index, step) {
            if (WS.currentIndex !== index || WS.currentStep >= step) return;
            var clamped = WS.currentStep;
            prestoWhenDeckReady(function () {
                if (WS.currentIndex === index && WS.currentStep === clamped) WS.goToSlide(index, step);
            });
        }
        """

    // Flushes any pending code-pane edit before a WYSIWYG gesture mutates the
    // preview DOM, so the two edit surfaces can't silently overwrite each other.
    func flushEditorPaneForWysiwyg() {
        guard isEditorMode, let eWV = editorWebView else { return }
        let generation = sessionGeneration
        eWV.evaluateJavaScript(pendingEditorChangeJS()) { [weak self] result, _ in
            guard let self = self,
                  self.sessionGeneration == generation,
                  self.editorWebView === eWV else { return }
            let pending = self.applyPendingEditorChangePayload(result)
            if pending.ok, let savedIndex = pending.savedIndex {
                self.refreshPreviewSlide(savedIndex, recording: pending.change)
            }
        }
    }

    // Synchronously ends+commits any live WYSIWYG text session on the preview so
    // text typed since the session began is saved before we tear down, reload,
    // or navigate away. commitActiveSession is an additive JS contract (added by
    // the wysiwyg flow); guard so this is a safe no-op when it's absent. The
    // completion runs after the commit's evaluateJavaScript returns, by which
    // point the resulting wysiwygChanged has been queued for handling.
    func commitActiveWysiwygSession(_ completion: @escaping () -> Void) {
        guard isEditorMode, let pWV = previewWebView else {
            completion()
            return
        }
        let generation = sessionGeneration
        let js = "(window.__prestoWysiwyg && typeof __prestoWysiwyg.commitActiveSession === 'function') ? !!__prestoWysiwyg.commitActiveSession() : false"
        pWV.evaluateJavaScript(js) { [weak self, weak pWV] _, error in
            guard let self, let pWV,
                  self.sessionGeneration == generation,
                  self.previewWebView === pWV else { return }
            if let error {
                self.reportEditorSaveFailed("Could not read visual editor: \(error.localizedDescription)")
                return
            }
            completion()
        }
    }

    func handleWysiwygChanged(html: String, index: Int, saveId: Int?) {
        let saved = handleEditorChanged(html: html, index: index, reloadPreview: false)
        if let saveId = saveId {
            if saved {
                pendingWysiwygSaveID = saveId
            } else {
                previewWebView?.evaluateJavaScript(
                    "window.__prestoWysiwyg && __prestoWysiwyg.saveFailed(\(saveId), 'Save failed')"
                ) { _, _ in }
            }
        }
        if saved {
            // Mirror the change into the code pane (debounced — setContent
            // re-tokenizes/re-highlights the whole slide on every commit).
            scheduleEditorMirror()
        }
    }

    // All destructive editor transitions use the same checked save boundary.
    // A timeout or invalid buffer leaves the editor open and its data recoverable.
    func flushActiveEditor(completion: @escaping (Bool) -> Void) {
        guard !editorFlushInProgress, !isPresentingExternalConflict else {
            completion(false)
            return
        }
        guard isEditorMode else {
            completion(flushPendingDeckWrite())
            return
        }
        editorFlushInProgress = true
        let generation = sessionGeneration
        var completed = false
        let finish: (Bool) -> Void = { [weak self] success in
            guard !completed else { return }
            completed = true
            if let self, self.sessionGeneration == generation {
                self.editorFlushInProgress = false
                if !success {
                    self.showPresenterHint("Could not save. Your editor is still open; retry Save.")
                }
            }
            completion(success)
        }
        let timeout = DispatchWorkItem { [weak self] in
            guard !completed else { return }
            self?.reportEditorSaveFailed("Editor did not respond. Changes have not been discarded.")
            finish(false)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: timeout)

        let flushSource: () -> Void = { [weak self] in
            guard let self, !completed, self.sessionGeneration == generation else {
                finish(false)
                return
            }
            guard self.editorReady, let editorWV = self.editorWebView else {
                timeout.cancel()
                finish(self.flushPendingDeckWrite())
                return
            }
            editorWV.evaluateJavaScript(self.pendingEditorChangeJS()) { [weak self, weak editorWV] result, error in
                guard let self, let editorWV, !completed,
                      self.sessionGeneration == generation,
                      self.editorWebView === editorWV else {
                    finish(false)
                    return
                }
                timeout.cancel()
                if let error {
                    self.reportEditorSaveFailed("Could not read HTML source: \(error.localizedDescription)")
                    finish(false)
                    return
                }
                let pending = self.applyPendingEditorChangePayload(result)
                guard pending.ok else { finish(false); return }
                guard self.flushPendingDeckWrite() else { finish(false); return }
                if let index = pending.savedIndex { self.refreshPreviewSlide(index, recording: pending.change) }
                finish(true)
            }
        }
        guard let preview = previewWebView else {
            flushSource()
            return
        }
        let js = "window.__prestoWysiwyg && __prestoWysiwyg.commitActiveSession()"
        preview.evaluateJavaScript(js) { [weak self, weak preview] _, error in
            guard let self, let preview, !completed,
                  self.sessionGeneration == generation,
                  self.previewWebView === preview else {
                finish(false)
                return
            }
            if let error {
                timeout.cancel()
                self.reportEditorSaveFailed("Could not read visual editor: \(error.localizedDescription)")
                finish(false)
                return
            }
            flushSource()
        }
    }

    func webContentRole(for webView: WKWebView) -> WebContentRole? {
        if webView === audienceWebView { return .audience }
        if webView === consoleWebView { return .presenter }
        if webView === previewWebView { return .editorPreview }
        if webView === editorWebView { return .editor }
        if webView === exportWebView { return .export }
        if webView === decksWebView { return .decks }
        if webView === thumbnailWebView { return .thumbnail }
        return nil
    }

    func webViewForRole(_ role: WebContentRole) -> WKWebView? {
        switch role {
        case .audience: return audienceWebView
        case .presenter: return consoleWebView
        case .editorPreview: return previewWebView
        case .editor: return editorWebView
        case .export: return exportWebView
        case .decks: return decksWebView
        case .thumbnail: return thumbnailWebView
        }
    }

    func maybeSimulateWebContentTermination(role: WebContentRole, webView: WKWebView) {
        guard simulatedWebContentTerminationRequests.contains(role),
              !simulatedWebContentTerminationRoles.contains(role) else { return }
        simulatedWebContentTerminationRoles.insert(role)
        let generation = sessionGeneration
        NSLog("PrestoTestWebContent: simulate role=%@", role.rawValue)
        DispatchQueue.main.asyncAfter(deadline: .now() + simulatedWebContentTerminationDelay) { [weak self, weak webView] in
            guard let self, let webView,
                  self.sessionGeneration == generation,
                  self.webViewForRole(role) === webView else { return }
            self.webViewWebContentProcessDidTerminate(webView)
        }
    }

    // Every sheet goes on the main window, on the laptop: never on the
    // borderless audience window above the projector's menu bar.
    func webContentFailureWindow(for role: WebContentRole) -> NSWindow? {
        switch role {
        case .export: return exportProgressWindow ?? mainWindow
        default: return mainWindow
        }
    }

    func showWebContentRecoveryFailure(role: WebContentRole) {
        let alert = NSAlert()
        alert.messageText = "Presto could not recover this view"
        alert.informativeText = "The \(role.rawValue) web content process stopped twice. Close the presentation and reopen the deck."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        if let window = webContentFailureWindow(for: role) {
            alert.beginSheetModal(for: window)
        } else {
            NSLog("Presto: no window available for exhausted %@ recovery", role.rawValue)
        }
    }

    func restartExportAfterWebContentTermination(_ webView: WKWebView) {
        guard exportWebView === webView, let deckURL = exportDeckURL, !exportIsWriting else { return }
        exportGeneration &+= 1
        exportPDFDoc = PDFDocument()
        exportSlideSteps = []
        exportPageIndex = 0
        exportPollCount = 0
        exportPageMap = [:]
        exportPageLinks = [:]
        updateExportProgress(0, of: 0)
        webView.reloadFromOrigin()
        NSLog("Presto: export restarted from page 1 after web content termination for %@", deckURL.lastPathComponent)
    }

    // MARK: - Fetch Polyfill

    // WKWebView's fetch() doesn't work with file:// URLs even with allowFileAccessFromFileURLs.
    // XMLHttpRequest does work, so inject a polyfill that replaces fetch() with XHR for file:// pages.
    func addFetchPolyfill(to config: WKWebViewConfiguration) {
        let polyfill = """
        (function() {
            if (window.location.protocol !== 'file:') return;
            window.fetch = function(url, options) {
                return new Promise(function(resolve, reject) {
                    var xhr = new XMLHttpRequest();
                    xhr.open((options && options.method) || 'GET', url);
                    xhr.onload = function() {
                        resolve({
                            ok: xhr.status === 0 || (xhr.status >= 200 && xhr.status < 300),
                            status: xhr.status || 200,
                            text: function() { return Promise.resolve(xhr.responseText); },
                            json: function() { return Promise.resolve(JSON.parse(xhr.responseText)); },
                            headers: new Headers()
                        });
                    };
                    xhr.onerror = function() {
                        reject(new TypeError('Fetch failed for ' + url));
                    };
                    xhr.send();
                });
            };
        })();
        """
        let script = WKUserScript(source: polyfill, injectionTime: .atDocumentStart, forMainFrameOnly: false)
        config.userContentController.addUserScript(script)
    }

    // MARK: - File Watcher

    // Events that mean the watched inode no longer is the deck path: atomic
    // saves (Presto's own included) and sync clients replace the file, which
    // the old descriptor only sees as a delete or rename.
    static let fileWatchInvalidatingEvents: DispatchSource.FileSystemEvent = [.delete, .rename, .revoke]

    func startFileWatcher() {
        guard let deckURL = currentDeckURL else { return }
        startAssetWatcher(deckURL: deckURL)
        // Replace only the descriptor; a pending debounced check still runs.
        cancelFileWatchSource()

        // Agents and editors commonly save by replacing the file. Watch its
        // directory too: the old inode can report only deletion, and the new
        // file may not exist yet when that event arrives.
        let directoryFD = open(deckURL.deletingLastPathComponent().path, O_EVTONLY)
        if directoryFD >= 0 {
            let directorySource = DispatchSource.makeFileSystemObjectSource(
                fileDescriptor: directoryFD, eventMask: [.write, .rename, .delete], queue: .main
            )
            directorySource.setEventHandler { [weak self, weak directorySource] in
                guard let self, let directorySource,
                      self.directoryWatchSource === directorySource else { return }
                self.handleFileChanged()
            }
            directorySource.setCancelHandler { close(directoryFD) }
            directoryWatchSource = directorySource
            directorySource.resume()
        } else {
            NSLog("Presto: failed to watch deck directory: %@", deckURL.deletingLastPathComponent().path)
        }

        let fd = open(deckURL.path, O_EVTONLY)
        guard fd >= 0 else {
            NSLog("Presto: failed to open file for watching: %@", deckURL.path)
            scheduleFileWatcherRearm()
            return
        }
        fileWatchFD = fd
        fileWatchRearmAttempts = 0

        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .extend, .rename, .delete, .revoke],
            queue: .main
        )

        source.setEventHandler { [weak self, weak source] in
            guard let self, let source, self.fileWatchSource === source else { return }
            let events = source.data
            if !events.isDisjoint(with: AppDelegate.fileWatchInvalidatingEvents) {
                self.startFileWatcher()
            }
            self.handleFileChanged(eventMask: events)
        }

        source.setCancelHandler { [weak self] in
            close(fd)
            if self?.fileWatchFD == fd {
                self?.fileWatchFD = -1
            }
        }

        fileWatchSource = source
        source.resume()
        NSLog("Presto: file watcher started for %@", deckURL.lastPathComponent)
    }

    // A replaced file can be briefly absent (delete, then create). Keep
    // trying for a few seconds so the watcher is not silently lost.
    func scheduleFileWatcherRearm() {
        guard currentDeckURL != nil, fileWatchRearmAttempts < 20 else {
            NSLog("Presto: file watcher could not re-open the deck")
            return
        }
        fileWatchRearmAttempts += 1
        let generation = sessionGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            guard let self, self.sessionGeneration == generation,
                  self.currentDeckURL != nil, self.fileWatchSource == nil,
                  !self.isPresentingExternalConflict else { return }
            self.startFileWatcher()
            if self.fileWatchSource != nil { self.handleFileChanged() }
        }
    }

    // After Presto's own atomic write the old descriptor watches a deleted
    // inode; watch the new file so later outside edits are still seen.
    func rearmFileWatcherAfterOwnWrite() {
        guard fileWatchSource != nil else { return }
        startFileWatcher()
    }

    func cancelFileWatchSource() {
        directoryWatchSource?.cancel()
        directoryWatchSource = nil
        if let source = fileWatchSource {
            fileWatchSource = nil
            fileWatchFD = -1
            source.cancel()
        }
    }

    func stopFileWatcher() {
        assetWatchTimer?.cancel()
        assetWatchTimer = nil
        assetSnapshot = [:]
        fileWatchDebounce?.cancel()
        fileWatchDebounce = nil
        cancelFileWatchSource()
    }

    // Directory vnode events do not report writes inside figures/, and an
    // unchanged index.html does not mean its SVGs, tables, or theme are unchanged.
    // Check metadata off the main queue, including the shared, symlinked engine.
    // This also survives atomic replacement and Dropbox synchronization.
    func startAssetWatcher(deckURL: URL) {
        guard assetWatchTimer == nil else { return }
        let roots = [deckURL.deletingLastPathComponent(),
                     findDeckRoot(from: deckURL).appendingPathComponent("engine")]
        assetSnapshot = Self.snapshotAssets(roots: roots, excluding: deckURL)
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + 1, repeating: 1, leeway: .milliseconds(100))
        timer.setEventHandler { [weak self, weak timer] in
            let snapshot = Self.snapshotAssets(roots: roots, excluding: deckURL)
            DispatchQueue.main.async { [weak self, weak timer] in
                guard let self, let timer, self.assetWatchTimer === timer else { return }
                if snapshot != self.assetSnapshot {
                    self.assetSnapshot = snapshot
                    self.assetReloadPending = true
                }
                // Retry a deferred asset refresh after the user finishes typing.
                if self.assetReloadPending { self.handleFileChanged() }
            }
        }
        assetWatchTimer = timer
        timer.resume()
    }

    nonisolated static func snapshotAssets(roots: [URL], excluding deckURL: URL) -> [URL: DeckAssetStamp] {
        let extensions: Set<String> = ["html", "htm", "css", "js", "mjs", "json", "svg",
                                       "png", "jpg", "jpeg", "gif", "webp", "avif", "ico",
                                       "woff", "woff2", "ttf", "otf", "mp4", "webm", "mp3", "wav"]
        let excluded = deckURL.resolvingSymlinksInPath().standardizedFileURL
        var result: [URL: DeckAssetStamp] = [:]
        var visited: Set<URL> = []
        var pending = roots
        while let next = pending.popLast() {
            let directory = next.resolvingSymlinksInPath().standardizedFileURL
            guard visited.insert(directory).inserted else { continue }
            guard let children = try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles]
            ) else { continue } // Missing asset folders are picked up when recreated.
            for child in children {
                let url = child.resolvingSymlinksInPath().standardizedFileURL
                if (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true {
                    if child.lastPathComponent != "node_modules" { pending.append(url) }
                } else if url != excluded, extensions.contains(url.pathExtension.lowercased()),
                          let attributes = try? FileManager.default.attributesOfItem(atPath: url.path) {
                    result[url] = DeckAssetStamp(modified: attributes[.modificationDate] as? Date,
                                                size: (attributes[.size] as? NSNumber)?.uint64Value ?? 0,
                                                inode: (attributes[.systemFileNumber] as? NSNumber)?.uint64Value ?? 0)
                }
            }
        }
        return result
    }

    func handleFileChanged(eventMask: DispatchSource.FileSystemEvent = []) {
        // Compare actual bytes after the debounce; a time-based suppression
        // window can hide another editor's write immediately after our own.
        fileWatchDebounce?.cancel()
        let generation = sessionGeneration
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.sessionGeneration == generation else { return }
            self.reloadFromDisk()
        }
        fileWatchDebounce = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: work)
    }

    func reloadFromDisk(forcePreviewReload: Bool = false) {
        guard let deckURL = currentDeckURL, !isPresentingExternalConflict else { return }
        let content: String
        do {
            content = try String(contentsOf: deckURL, encoding: .utf8)
        } catch {
            NSLog("Presto: failed to reload deck from disk: %@", error.localizedDescription)
            startFileWatcher()
            scheduleReloadFromDiskRetry()
            return
        }

        fileReloadFailureCount = 0

        // Atomic replacement invalidates the old file descriptor even when the
        // replacement has identical contents, so always re-arm in that case.
        if content == fileContent || content == lastPersistedContent {
            if forcePreviewReload || assetReloadPending {
                // Asset changes do not conflict with authored HTML. Keep the
                // refresh pending until local text/source edits have been saved.
                if isEditorMode && (wysiwygTextEditingActive || sourceEditorDirty || deckWriteDirty) { return }
                NSLog("Presto: refreshing deck resources from disk")
                assetReloadPending = false
                // The edit view can stay alive while presenting: refresh both.
                if isEditorMode { reloadPreviewAtSlide(currentEditorSlideIndex) }
                reloadPresentationViews()
            }
            startFileWatcher()
            return
        }

        // Unsaved local edits — a live text session or a pending debounced write —
        // mean a full reload would silently discard the local change (and, for a
        // text session, wipe this slide's undo history). Surface the conflict and
        // let the user decide rather than picking a winner behind their back.
        if isEditorMode && (wysiwygTextEditingActive || sourceEditorDirty || deckWriteDirty) && !isPresentingExternalConflict {
            presentExternalChangeConflict(diskContent: content)
            return
        }
        if isPresentingExternalConflict { return }

        applyDiskReload(content)
    }

    func applyDiskReload(_ content: String) {
        let sections = extractSections(from: content)
        guard !sections.isEmpty, deckStructureIssues(in: content).isEmpty else {
            reportEditorSaveFailed("External deck is invalid; keeping the current editor open")
            startFileWatcher()
            return
        }
        // External version wins — drop any pending local write.
        cancelPendingDeckWrite()
        assetReloadPending = false
        NSLog("Presto: file changed on disk, hot-reloading")
        fileContent = content
        lastPersistedContent = content
        deckTitle = deckDisplayTitle(content: content, deckURL: currentDeckURL ?? URL(fileURLWithPath: "/"))
        // The edit view can stay alive while presenting: refresh both.
        reloadPresentationViews()
        if !isEditorMode {
            sectionInfos = sections
            startFileWatcher()
            return
        }
        sourceEditorDirty = false
        pendingEditorSaveID = nil
        pendingWysiwygSaveID = nil
        sectionInfos = sections
        clampCurrentEditorSlideIndex()
        reloadPreviewAtSlide(currentEditorSlideIndex)
        sendCurrentSlideToEditor(discardLocalChanges: true)
        setSaveStatus(.saved)

        // Re-start watcher (rename events invalidate the fd)
        startFileWatcher()
    }

    func reloadPresentationViews() {
        // didFinish restores the audience's slide/step. Reload only the
        // presenter's deck iframe so its timer and panel layout keep running.
        audienceWebView?.reloadFromOrigin()
        if let deckURL = currentDeckURL, let presenter = consoleWebView {
            presenterReady = false
            let generation = sessionGeneration
            // The presenter iframe shares this view's cache. Clear only cached
            // resources so reinitializing it fetches new CSS/JS without resetting
            // the presenter page or its timer.
            presenter.configuration.websiteDataStore.removeData(
                ofTypes: [WKWebsiteDataTypeMemoryCache, WKWebsiteDataTypeDiskCache],
                modifiedSince: .distantPast
            ) { [weak self, weak presenter] in
                guard let self, let presenter, self.sessionGeneration == generation,
                      self.consoleWebView === presenter else { return }
                presenter.evaluateJavaScript("initPresenter(\(self.jsStringLiteral(deckURL.absoluteString)))") { _, error in
                    if let error {
                        NSLog("Presto: failed to refresh presenter deck: %@", error.localizedDescription)
                    }
                }
            }
        }
    }

    // Entry point when the deck changed on disk while Presto holds unsaved
    // edits. Presto merges slide by slide (see mergeDeckVersions) and asks only
    // about slides both sides changed.
    func presentExternalChangeConflict(diskContent: String) {
        guard !isPresentingExternalConflict else { return }
        // Committing a live text session here would end it under the user's
        // fingers (the next keystroke would replace the selected text block),
        // so merge as soon as the session ends instead.
        if wysiwygTextEditingActive {
            if !externalMergeDeferred {
                externalMergeDeferred = true
                showPresenterHint("The deck changed on disk. Presto will merge it when you finish this text edit.")
            }
            return
        }
        externalMergeDeferred = false
        isPresentingExternalConflict = true
        // Pause watching and freeze the debounced write so a burst of external
        // writes can't stack decisions or race the merge.
        stopFileWatcher()
        pendingDeckWrite?.cancel()
        pendingDeckWrite = nil

        let generation = sessionGeneration
        // Async: the caller may be inside writeDeckContent.
        DispatchQueue.main.async { [weak self] in
            guard let self, self.sessionGeneration == generation else { return }
            self.collectLocalEditsForMerge { [weak self] collected in
                guard let self, self.sessionGeneration == generation else { return }
                guard collected else {
                    self.isPresentingExternalConflict = false
                    self.reportEditorSaveFailed("Could not read your unsaved edits, so the outside change was not merged. Nothing was discarded; save again to retry.")
                    self.startFileWatcher()
                    return
                }
                self.resolveExternalChange()
            }
        }
    }

    // Pull edits still held in either surface into fileContent without writing.
    // Called only when no text session is live; this flushes nudge and font
    // bursts plus the HTML-source buffer.
    func collectLocalEditsForMerge(_ completion: @escaping (Bool) -> Void) {
        let generation = sessionGeneration
        var finished = false
        let finish: (Bool) -> Void = { ok in
            guard !finished else { return }
            finished = true
            completion(ok)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { finish(false) }

        let collectSource: () -> Void = { [weak self] in
            guard let self, self.sessionGeneration == generation else { return finish(false) }
            guard self.editorReady, let editorWV = self.editorWebView else { return finish(true) }
            editorWV.evaluateJavaScript(self.pendingEditorChangeJS()) { [weak self, weak editorWV] result, error in
                guard let self, let editorWV, self.sessionGeneration == generation,
                      self.editorWebView === editorWV, error == nil else { return finish(false) }
                finish(self.applyPendingEditorChangePayload(result).ok)
            }
        }
        guard isEditorMode, let preview = previewWebView else { return collectSource() }
        preview.evaluateJavaScript("window.__prestoWysiwyg && __prestoWysiwyg.commitActiveSession()") { [weak self, weak preview] _, error in
            guard let self, let preview, self.sessionGeneration == generation,
                  self.previewWebView === preview, error == nil else { return finish(false) }
            collectSource()
        }
    }

    func resolveExternalChange() {
        guard let deckURL = currentDeckURL, let ours = fileContent else {
            isPresentingExternalConflict = false
            return
        }
        let theirs: String
        do {
            theirs = try String(contentsOf: deckURL, encoding: .utf8)
        } catch {
            NSLog("Presto: failed to read changed deck: %@", error.localizedDescription)
            isPresentingExternalConflict = false
            reportEditorSaveFailed("Cannot read the changed deck; local edits are retained: \(error.localizedDescription)")
            startFileWatcher()
            return
        }
        let base = lastPersistedContent ?? theirs
        guard let preferMine = mergeDeckVersions(base: base, ours: ours, theirs: theirs, preferOurs: true) else {
            askExternalMergeDecision(
                message: "The deck changed on disk while you were editing",
                details: "Presto cannot match the two versions slide by slide. Keep My Versions writes Presto's whole deck over the disk version; Reload from Disk discards your unsaved edits. Either way Presto first saves a copy of the version it replaces in \(deckBackupDisplayPath).",
                buttons: [.keepMine, .reloadFromDisk]
            ) { [weak self] choice in
                self?.finishExternalChange(choice: choice, base: base, decidedTheirs: theirs, wholeDeck: true)
            }
            return
        }
        if preferMine.conflicts.isEmpty {
            applyMergedDeck(preferMine.content, oursIndexMap: preferMine.oursIndexMap, diskVersion: theirs)
            if preferMine.diskSummary != "none" {
                showPresenterHint("Merged outside changes (\(preferMine.diskSummary)) with your edits")
            }
            return
        }
        let details = """
        Changed both in Presto and on disk: \(preferMine.conflicts.joined(separator: "; ")).

        Keep My Versions keeps Presto's version of these; Use Disk Versions takes the disk's. Either way the rest is merged: \(preferMine.mergeSummary).

        Presto first saves a copy of the version that loses in \(deckBackupDisplayPath): the disk version for Keep My Versions, yours for Use Disk Versions. Reload from Disk instead discards all your unsaved edits (a copy of them is saved there too).
        """
        askExternalMergeDecision(
            message: "The deck changed on disk while you were editing",
            details: details,
            buttons: [.keepMine, .useDisk, .reloadFromDisk]
        ) { [weak self] choice in
            self?.finishExternalChange(choice: choice, base: base, decidedTheirs: theirs, wholeDeck: false)
        }
    }

    func finishExternalChange(choice: ExternalMergeChoice, base: String, decidedTheirs: String, wholeDeck: Bool) {
        guard let deckURL = currentDeckURL, let ours = fileContent else {
            isPresentingExternalConflict = false
            return
        }
        let latestDisk: String
        do {
            latestDisk = try String(contentsOf: deckURL, encoding: .utf8)
        } catch {
            isPresentingExternalConflict = false
            reportEditorSaveFailed("Cannot read the changed deck; local edits are retained: \(error.localizedDescription)")
            startFileWatcher()
            return
        }
        // The disk moved again while the dialog was open: decide afresh.
        guard latestDisk == decidedTheirs else {
            resolveExternalChange()
            return
        }
        // Save the version that loses before anything is replaced: the disk's
        // for Keep My Versions, Presto's for the other two. A conflict pairs
        // slides by position, so the loser may hold a whole replacement slide.
        // Without that copy, nothing changes and both versions stay put.
        do {
            if choice == .keepMine {
                try backupDeckVersion(latestDisk, of: deckURL, label: "disk")
            } else {
                try backupDeckVersion(ours, of: deckURL, label: "presto-unsaved")
            }
        } catch {
            isPresentingExternalConflict = false
            let lost = choice == .keepMine ? "the disk version" : "your unsaved edits"
            reportDeckSaveProblem("Could not back up \(lost), so \(choice.buttonTitle) was not applied. Your edits are still open and the disk is unchanged; save again to retry. (\(error.localizedDescription))")
            startFileWatcher()
            return
        }
        switch choice {
        case .reloadFromDisk:
            isPresentingExternalConflict = false
            applyDiskReload(latestDisk)
        case .keepMine where wholeDeck:
            applyMergedDeck(ours, oursIndexMap: nil, diskVersion: latestDisk)
        case .keepMine, .useDisk:
            guard let merge = mergeDeckVersions(base: base, ours: ours, theirs: latestDisk,
                                                preferOurs: choice == .keepMine) else {
                resolveExternalChange()
                return
            }
            applyMergedDeck(merge.content, oursIndexMap: merge.oursIndexMap, diskVersion: latestDisk)
        }
    }

    // Write a merged deck over `diskVersion` (backed up by writeDeckContent),
    // then refresh the preview and source pane to match.
    func applyMergedDeck(_ merged: String, oursIndexMap: [Int: Int]?, diskVersion: String) {
        let previous = fileContent ?? ""
        let previousSections = sectionInfos.map { $0.content }
        let previousIndex = currentEditorSlideIndex
        isPresentingExternalConflict = false
        lastPersistedContent = diskVersion
        fileContent = merged
        sectionInfos = extractSections(from: merged)
        if let mapped = oursIndexMap?[previousIndex] {
            currentEditorSlideIndex = mapped
        }
        clampCurrentEditorSlideIndex()
        deckWriteDirty = true
        if flushPendingDeckWrite() {
            NSLog("Presto: merged outside deck change")
        }

        let mergedSections = sectionInfos.map { $0.content }
        let sameShape = mergedSections.count == previousSections.count &&
            deckOutsideSlides(previous) == deckOutsideSlides(merged)
        if sameShape {
            for index in mergedSections.indices where mergedSections[index] != previousSections[index] {
                refreshPreviewSlide(index)
            }
        } else {
            reloadPreviewAtSlide(currentEditorSlideIndex)
        }
        let currentChanged = !sameShape || currentEditorSlideIndex != previousIndex ||
            mergedSections[currentEditorSlideIndex] != previousSections[currentEditorSlideIndex]
        if currentChanged {
            sendCurrentSlideToEditor(discardLocalChanges: true)
        }
        // A failed write may already have started a fresh merge.
        if !isPresentingExternalConflict {
            startFileWatcher()
        }
    }

    // Head and tail of a deck (everything outside its slide sections).
    func deckOutsideSlides(_ html: String) -> String {
        guard let parts = deckParts(html) else { return html }
        return parts.head + "\u{0}" + parts.tail
    }

    func askExternalMergeDecision(message: String, details: String, buttons: [ExternalMergeChoice],
                                  completion: @escaping (ExternalMergeChoice) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = details
        alert.alertStyle = .warning
        for button in buttons {
            alert.addButton(withTitle: button.buttonTitle)
        }
        let generation = sessionGeneration
        let handler: (NSApplication.ModalResponse) -> Void = { [weak self] response in
            guard let self, self.sessionGeneration == generation else { return }
            let index = response.rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
            completion(buttons.indices.contains(index) ? buttons[index] : .keepMine)
        }
        // The main window is on the laptop even while live.
        if let window = mainWindow, window.isVisible {
            alert.beginSheetModal(for: window, completionHandler: handler)
        } else {
            handler(alert.runModal())
        }
    }

    // MARK: - Three-Way Deck Merge

    func deckParts(_ html: String) -> DeckParts? {
        let sections = extractSections(from: html)
        guard let first = sections.first else { return nil }
        var slides: [(gap: String, content: String)] = []
        var cursor = first.range.lowerBound
        for section in sections {
            slides.append((String(html[cursor..<section.range.lowerBound]), section.content))
            cursor = section.range.upperBound
        }
        return DeckParts(head: String(html[..<first.range.lowerBound]), slides: slides,
                         tail: String(html[cursor...]))
    }

    static let slideIdentityAttribute = try! NSRegularExpression(pattern: #"\b(?:id|src|data-table-src)\s*=\s*"([^"]*)""#)

    // Words of a slide's text plus its ids and image/table sources, for
    // telling an edited slide from an unrelated one (markup alone is shared
    // by every slide, so it is left out).
    func slideTokens(_ content: String) -> Set<String> {
        var text = content.replacingOccurrences(of: "<[^>]*>", with: " ", options: .regularExpression)
        let ns = content as NSString
        for match in AppDelegate.slideIdentityAttribute.matches(in: content, range: NSRange(location: 0, length: ns.length)) {
            text += " " + ns.substring(with: match.range(at: 1))
        }
        return Set(text.lowercased().components(separatedBy: CharacterSet.alphanumerics.inverted).filter { $0.count >= 2 })
    }

    // For each base slide, the index of the same slide in `side` (nil if it
    // was deleted there). Identical slides anchor the alignment; between
    // anchors, equal-length runs pair up in order (edits in place) and
    // unequal runs pair by similarity, the rest being insertions/deletions.
    func alignSlides(base: [String], side: [String]) -> [Int?] {
        let n = base.count, m = side.count
        var lcs = Array(repeating: Array(repeating: 0, count: m + 1), count: n + 1)
        if n > 0 && m > 0 {
            for i in stride(from: n - 1, through: 0, by: -1) {
                for j in stride(from: m - 1, through: 0, by: -1) {
                    lcs[i][j] = base[i] == side[j] ? lcs[i + 1][j + 1] + 1 : max(lcs[i + 1][j], lcs[i][j + 1])
                }
            }
        }
        var anchors: [(Int, Int)] = []
        var i = 0, j = 0
        while i < n && j < m {
            if base[i] == side[j] { anchors.append((i, j)); i += 1; j += 1 }
            else if lcs[i + 1][j] >= lcs[i][j + 1] { i += 1 }
            else { j += 1 }
        }
        var mapping = [Int?](repeating: nil, count: n)
        var previous = (-1, -1)
        for anchor in anchors + [(n, m)] {
            let baseRun = Array((previous.0 + 1)..<anchor.0)
            let sideRun = Array((previous.1 + 1)..<anchor.1)
            if !baseRun.isEmpty && baseRun.count == sideRun.count {
                for (b, s) in zip(baseRun, sideRun) { mapping[b] = s }
            } else if !baseRun.isEmpty && !sideRun.isEmpty {
                // Order-preserving best pairing, counting only similar slides.
                let p = baseRun.count, q = sideRun.count
                let baseTokens = baseRun.map { slideTokens(base[$0]) }
                let sideTokens = sideRun.map { slideTokens(side[$0]) }
                let sim = baseTokens.map { a in
                    sideTokens.map { b -> Double in
                        let union = a.union(b).count
                        return union == 0 ? 0 : Double(a.intersection(b).count) / Double(union)
                    }
                }
                var score = Array(repeating: Array(repeating: 0.0, count: q + 1), count: p + 1)
                for x in stride(from: p - 1, through: 0, by: -1) {
                    for y in stride(from: q - 1, through: 0, by: -1) {
                        let pair = sim[x][y] >= 0.5 ? sim[x][y] + score[x + 1][y + 1] : -1
                        score[x][y] = max(pair, score[x + 1][y], score[x][y + 1])
                    }
                }
                var x = 0, y = 0
                while x < p && y < q {
                    if sim[x][y] >= 0.5 && score[x][y] == sim[x][y] + score[x + 1][y + 1] {
                        mapping[baseRun[x]] = sideRun[y]; x += 1; y += 1
                    } else if score[x + 1][y] >= score[x][y + 1] { x += 1 }
                    else { y += 1 }
                }
            }
            if anchor.0 < n { mapping[anchor.0] = anchor.1 }
            previous = anchor
        }
        return mapping
    }

    // Per-slide three-way merge. base = what Presto last loaded or saved,
    // ours = Presto's in-memory deck, theirs = the disk. A slide changed on one
    // side only takes that side; a slide changed differently on both sides (or
    // deleted on one and edited on the other) is a conflict resolved by
    // `preferOurs`. Returns nil when either deck has no slides or the merged
    // text would not parse back into the expected slides.
    func mergeDeckVersions(base: String, ours: String, theirs: String, preferOurs: Bool) -> DeckMergeOutcome? {
        guard let b = deckParts(base), let o = deckParts(ours), let t = deckParts(theirs) else { return nil }
        let baseContents = b.slides.map { $0.content }
        let oursMap = alignSlides(base: baseContents, side: o.slides.map { $0.content })
        let theirsMap = alignSlides(base: baseContents, side: t.slides.map { $0.content })

        // Slides present on one side only, keyed by the base slide they follow (-1 = start).
        func insertions(_ map: [Int?], count: Int) -> [Int: [Int]] {
            var baseFor = [Int: Int]()
            for (baseIndex, sideIndex) in map.enumerated() { if let s = sideIndex { baseFor[s] = baseIndex } }
            var result = [Int: [Int]]()
            var after = -1
            for sideIndex in 0..<count {
                if let baseIndex = baseFor[sideIndex] { after = baseIndex } else { result[after, default: []].append(sideIndex) }
            }
            return result
        }
        let oursInserts = insertions(oursMap, count: o.slides.count)
        let theirsInserts = insertions(theirsMap, count: t.slides.count)

        func pick(_ baseText: String, _ mine: String, _ disk: String) -> String {
            if mine == baseText { return disk }
            if disk == baseText { return mine }
            return disk
        }

        var conflicts: [String] = []
        func mergeOutsideSlides(_ label: String, _ baseText: String, _ mine: String, _ disk: String) -> String {
            if mine == baseText || mine == disk { return disk }
            if disk == baseText { return mine }
            conflicts.append(label)
            return preferOurs ? mine : disk
        }
        let head = mergeOutsideSlides("the deck head (styles and scripts before the first slide)", b.head, o.head, t.head)
        let tail = mergeOutsideSlides("the end of the deck (after the last slide)", b.tail, o.tail, t.tail)
        let headFromDisk = head != o.head

        var slides: [(gap: String, content: String)] = []
        var oursIndexMap: [Int: Int] = [:]
        var takenFromDisk: [Int] = [], keptLocal: [Int] = []
        var addedOnDisk = 0, deletedOnDisk = 0

        func emitInsertions(after baseIndex: Int) {
            let mineAdded = oursInserts[baseIndex] ?? []
            for sideIndex in mineAdded {
                oursIndexMap[sideIndex] = slides.count
                slides.append(o.slides[sideIndex])
                keptLocal.append(slides.count)
            }
            let mineContents = Set(mineAdded.map { o.slides[$0].content })
            for sideIndex in theirsInserts[baseIndex] ?? [] where !mineContents.contains(t.slides[sideIndex].content) {
                slides.append(t.slides[sideIndex])
                addedOnDisk += 1
            }
        }

        emitInsertions(after: -1)
        for baseIndex in b.slides.indices {
            let baseSlide = b.slides[baseIndex]
            let mine = oursMap[baseIndex].map { o.slides[$0] }
            let disk = theirsMap[baseIndex].map { t.slides[$0] }
            let mineChanged = mine?.content != baseSlide.content
            let diskChanged = disk?.content != baseSlide.content
            var chosen: (gap: String, content: String)?
            var fromMine = false
            if !diskChanged {
                chosen = mine
                fromMine = true
            } else if !mineChanged || mine?.content == disk?.content {
                chosen = disk
                if disk == nil { deletedOnDisk += 1 }
            } else {
                let mineNumber = oursMap[baseIndex].map { "slide \($0 + 1)" } ?? "a slide you deleted"
                let diskNumber = theirsMap[baseIndex].map { "slide \($0 + 1) on disk" } ?? "deleted on disk"
                conflicts.append(mineNumber == diskNumber.replacingOccurrences(of: " on disk", with: "")
                                 ? mineNumber : "\(mineNumber) (\(diskNumber))")
                chosen = preferOurs ? mine : disk
                fromMine = preferOurs
            }
            if let slide = chosen {
                let gap = pick(baseSlide.gap, mine?.gap ?? baseSlide.gap, disk?.gap ?? baseSlide.gap)
                if let oursIndex = oursMap[baseIndex] { oursIndexMap[oursIndex] = slides.count }
                slides.append((gap, slide.content))
                if slide.content != baseSlide.content {
                    if fromMine { keptLocal.append(slides.count) } else { takenFromDisk.append(slides.count) }
                }
            } else if let oursIndex = oursMap[baseIndex] {
                // Deleted in the merge: keep Presto's position pointing nearby.
                oursIndexMap[oursIndex] = max(0, slides.count - 1)
            }
            emitInsertions(after: baseIndex)
        }

        let content = head + slides.map { $0.gap + $0.content }.joined() + tail
        let check = extractSections(from: content)
        guard !slides.isEmpty, check.count == slides.count,
              zip(check, slides).allSatisfy({ $0.content == $1.content }),
              deckStructureIssues(in: content).isEmpty else { return nil }
        return DeckMergeOutcome(content: content, conflicts: conflicts, oursIndexMap: oursIndexMap,
                                takenFromDisk: takenFromDisk, keptLocal: keptLocal,
                                addedOnDisk: addedOnDisk, deletedOnDisk: deletedOnDisk,
                                headFromDisk: headFromDisk)
    }

    // MARK: - Deck Backups

    static let deckBackupRetention = 30

    var deckBackupRootURL: URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return support.appendingPathComponent("Presto/backups", isDirectory: true)
    }

    var deckBackupDisplayPath: String {
        (deckBackupRootURL.path as NSString).abbreviatingWithTildeInPath
    }

    // Readable and stable per deck path: "<folder>-<file>-<hash of full path>".
    func deckBackupIdentifier(for deckURL: URL) -> String {
        let path = deckURL.resolvingSymlinksInPath().standardizedFileURL.path
        var hash: UInt64 = 0xcbf29ce484222325
        for byte in path.utf8 {
            hash ^= UInt64(byte)
            hash = hash &* 0x100000001b3
        }
        let readable = deckURL.deletingLastPathComponent().lastPathComponent + "-" +
            deckURL.deletingPathExtension().lastPathComponent
        let safe = String(readable.map { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" ? $0 : "_" }.prefix(60))
        return safe + "-" + String(format: "%016llx", hash)
    }

    // Keeps a copy outside the synced deck folder, newest 30 per deck.
    @discardableResult
    func backupDeckVersion(_ content: String, of deckURL: URL, label: String? = nil, now: Date = Date()) throws -> URL {
        let directory = deckBackupRootURL.appendingPathComponent(deckBackupIdentifier(for: deckURL), isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        func existingBackups() -> [URL] {
            ((try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? [])
                .filter { $0.pathExtension == "html" }
                .sorted { $0.lastPathComponent < $1.lastPathComponent }
        }
        // A merge can save the same version twice (the losing disk version,
        // then the write boundary's copy of the bytes it replaces).
        if let newest = existingBackups().last,
           (try? String(contentsOf: newest, encoding: .utf8)) == content {
            return newest
        }
        // Fixed-width UTC timestamp with microseconds, so names sort by age
        // (local time repeats an hour when the clocks go back).
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        // One truncated reading for both parts: DateFormatter rounds to the
        // millisecond, so it would show 0.9996 s as the next second.
        let totalMicros = Int64((now.timeIntervalSince1970 * 1_000_000).rounded(.down))
        let wholeSecond = Date(timeIntervalSince1970: TimeInterval(totalMicros / 1_000_000))
        // Every name carries a two-digit collision index, so a same-microsecond
        // backup still sorts after the one before it.
        let stem = formatter.string(from: wholeSecond) + String(format: "-%06lldZ", totalMicros % 1_000_000)
        let tail = label.map { "-\($0)" } ?? ""
        var suffix = 0
        var url = directory.appendingPathComponent(stem + String(format: "-%02d", suffix) + tail + ".html")
        while FileManager.default.fileExists(atPath: url.path) {
            suffix += 1
            url = directory.appendingPathComponent(stem + String(format: "-%02d", suffix) + tail + ".html")
        }
        try content.write(to: url, atomically: true, encoding: .utf8)
        NSLog("Presto: backed up deck version to %@", url.path)
        for old in existingBackups().dropLast(AppDelegate.deckBackupRetention) {
            do {
                try FileManager.default.trashItem(at: old, resultingItemURL: nil)
            } catch {
                NSLog("Presto: could not move old backup %@ to the Trash: %@", old.path, error.localizedDescription)
            }
        }
        return url
    }

    func scheduleReloadFromDiskRetry() {
        fileReloadFailureCount += 1
        guard fileReloadFailureCount <= 3 else {
            NSLog("Presto: giving up on hot reload after %d failed attempts", fileReloadFailureCount)
            return
        }

        fileWatchDebounce?.cancel()
        let generation = sessionGeneration
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.sessionGeneration == generation else { return }
            self.reloadFromDisk()
        }
        fileWatchDebounce = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }

    // MARK: - Helpers

    func findDeckRoot(from deckURL: URL) -> URL {
        var dir = deckURL.deletingLastPathComponent()
        for _ in 0..<10 {
            let engineFile = dir.appendingPathComponent("engine/webslides.js")
            if FileManager.default.fileExists(atPath: engineFile.path) {
                return dir
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return deckURL.deletingLastPathComponent().deletingLastPathComponent()
    }

    func findWebSlidesRoot(from deckURL: URL) -> URL {
        return findWebSlidesRoot(forDeckRoot: findDeckRoot(from: deckURL))
    }

    func findWebSlidesRoot(forDeckRoot deckRoot: URL) -> URL {
        let linkedEngineFile = deckRoot.appendingPathComponent("engine/webslides.js")
        if FileManager.default.fileExists(atPath: linkedEngineFile.path) {
            let resolvedEngineFile = linkedEngineFile.resolvingSymlinksInPath()
            let root = resolvedEngineFile.deletingLastPathComponent().deletingLastPathComponent()
            let presenterHTML = root.appendingPathComponent("presto/presenter.html")
            if FileManager.default.fileExists(atPath: presenterHTML.path) {
                return root
            }
        }

        let localPresenterHTML = deckRoot.appendingPathComponent("presto/presenter.html")
        if FileManager.default.fileExists(atPath: localPresenterHTML.path) {
            return deckRoot
        }

        return prestoDefaultWebSlidesRoot()
    }

    func resolveDeckResourceRoots(for deckURL: URL) -> DeckResourceRoots {
        let deckRoot = findDeckRoot(from: deckURL)
        let webSlidesRoot = findWebSlidesRoot(forDeckRoot: deckRoot)
        let readAccessRoot = commonAncestor([
            deckURL.deletingLastPathComponent(),
            deckRoot,
            webSlidesRoot
        ])
        return DeckResourceRoots(
            deckRoot: deckRoot,
            webSlidesRoot: webSlidesRoot,
            readAccessRoot: readAccessRoot
        )
    }

    func findReadAccessRoot(deckURL: URL) -> URL {
        return resolveDeckResourceRoots(for: deckURL).readAccessRoot
    }

    func commonAncestor(_ urls: [URL]) -> URL {
        let paths = urls.map { $0.resolvingSymlinksInPath().standardizedFileURL.pathComponents }
        guard var prefix = paths.first else {
            return URL(fileURLWithPath: "/")
        }

        for components in paths.dropFirst() {
            var keep = 0
            while keep < prefix.count && keep < components.count && prefix[keep] == components[keep] {
                keep += 1
            }
            prefix = Array(prefix.prefix(keep))
        }

        if prefix.isEmpty {
            return URL(fileURLWithPath: "/")
        }
        return URL(fileURLWithPath: NSString.path(withComponents: prefix))
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        NSLog("Presto: navigation failed: %@", error.localizedDescription)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        NSLog("Presto: provisional navigation failed: %@", error.localizedDescription)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        guard let role = webContentRole(for: webView) else {
            NSLog("Presto: ignored web content termination from stale webview")
            return
        }
        // Decks and the thumbnail renderer hold no unsaved state.
        if role == .decks {
            decksReady = false
            webView.reloadFromOrigin()
            return
        }
        if role == .thumbnail {
            finishThumbnail(png: nil)
            return
        }

        let attempts = webContentRecoveryAttempts[role, default: 0]
        guard attempts < 1 else {
            NSLog("Presto: %@ web content recovery exhausted", role.rawValue)
            if role == .export {
                let generation = exportGeneration
                DispatchQueue.main.async { [weak self] in
                    self?.finishExport(success: false,
                                       message: "The export web process stopped twice",
                                       generation: generation)
                }
            } else {
                presentSheetWhenNotLive { [weak self] in self?.showWebContentRecoveryFailure(role: role) }
            }
            return
        }

        webContentRecoveryAttempts[role] = attempts + 1
        NSLog("Presto: recovering %@ after web content termination (attempt %d)",
              role.rawValue, attempts + 1)

        switch role {
        case .audience:
            webView.reloadFromOrigin()
        case .presenter:
            presenterReady = false
            webView.reloadFromOrigin()
        case .editorPreview:
            // The recovered page reads disk, not our newer in-memory version.
            guard flushPendingDeckWrite() else {
                webContentRecoveryAttempts[role] = attempts
                return
            }
            webView.reloadFromOrigin()
        case .editor:
            editorReady = false
            editorRecoveryNeedsWarning = true
            NSLog("Presto: editor recovery restores the last saved slide content")
            webView.reloadFromOrigin()
        case .export:
            restartExportAfterWebContentTermination(webView)
        case .decks, .thumbnail:
            break
        }
    }

    // MARK: - Navigation Sync (Presentation mode)

    // Clicks on these reach the engine in the audience window (contract C3).
    static let audienceClickThroughSelector =
        "a, button, .ws-appendix-link, .appendix-links *, .header-links *, [data-ws-link]"
    static let presentationExitConfirmWindow: TimeInterval = 2.0

    // Navigation started in the presenter view (appendix or back button,
    // overview pick) moves the audience. The presenter view mirrors the
    // audience afterwards, and its echo of the audience's own state is ignored.
    func handlePresenterNavigated(_ body: [String: Any]) {
        guard let index = finiteInt(body["index"]), index >= 0,
              let step = finiteInt(body["step"]), step >= 0 else { return }
        var returnStack: [[String: Int]] = []
        for entry in body["returnStack"] as? [[String: Any]] ?? [] {
            guard let returnIndex = finiteInt(entry["index"]), returnIndex >= 0,
                  let returnStep = finiteInt(entry["step"]), returnStep >= 0 else { return }
            returnStack.append(["index": returnIndex, "step": returnStep])
        }
        if index == lastAudienceSlideIndex && step == lastAudienceStep &&
            returnStack == lastAudienceReturnStack { return }
        // Before Start there is no audience: the console's own preview picks
        // where the talk begins.
        guard audienceWebView != nil else {
            lastAudienceSlideIndex = index
            lastAudienceStep = step
            lastAudienceReturnStack = returnStack
            pushConsoleState()
            return
        }
        guard let data = try? JSONSerialization.data(withJSONObject: returnStack),
              let stackJSON = String(data: data, encoding: .utf8) else { return }
        navigateAudience(command: "WS.returnStack = \(stackJSON); WS.goToSlide(\(index), \(step))")
    }

    func recordAudienceState(json: String) -> (index: Int, step: Int)? {
        guard let data = json.data(using: .utf8),
              let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let index = finiteInt(state["index"]), index >= 0,
              let step = finiteInt(state["step"]), step >= 0 else { return nil }
        var returnStack: [[String: Int]] = []
        if let entries = state["returnStack"] as? [[String: Any]] {
            for entry in entries {
                guard let returnIndex = finiteInt(entry["index"]), returnIndex >= 0,
                      let returnStep = finiteInt(entry["step"]), returnStep >= 0 else { return nil }
                returnStack.append(["index": returnIndex, "step": returnStep])
            }
        }
        lastAudienceSlideIndex = index
        lastAudienceStep = step
        lastAudienceReturnStack = returnStack
        return (index, step)
    }

    func audienceStateJSONExpression() -> String {
        "JSON.stringify(Object.assign({}, WS.getState(), { returnStack: Array.isArray(WS.returnStack) ? WS.returnStack : [] }))"
    }

    func audienceReturnStackJSLiteral() -> String {
        guard JSONSerialization.isValidJSONObject(lastAudienceReturnStack),
              let data = try? JSONSerialization.data(withJSONObject: lastAudienceReturnStack),
              let json = String(data: data, encoding: .utf8) else { return "[]" }
        return json
    }

    // Puts a reloaded audience page back where the talk was. That first move
    // is Presto's own, so it is not reported back; a step restored once
    // WS.ready resolves is reported, so the presenter view follows it.
    func audienceStateRestoreJS() -> String {
        """
        (function restorePrestoAudienceState() {
            if (!window.WS || !WS.slides || !WS.slides.length || typeof WS.goToSlide !== 'function') {
                requestAnimationFrame(restorePrestoAudienceState);
                return;
            }
        \(AppDelegate.deckReadyHelpersJS)
            WS.returnStack = \(audienceReturnStackJSLiteral());
            var restoredIndex = Math.min(\(lastAudienceSlideIndex), WS.slides.length - 1);
            window.__prestoRemoteNavigation = true;
            try {
                WS.goToSlide(restoredIndex, \(lastAudienceStep));
            } finally {
                window.__prestoRemoteNavigation = false;
            }
            prestoRestoreStepWhenReady(restoredIndex, \(lastAudienceStep));
        })();
        """
    }

    func updatePresenterFromAudienceStateJSON(_ json: String) {
        let expectedState = recordAudienceState(json: json)
        updatePresentationChrome()
        guard let pWV = consoleWebView else { return }
        let generation = sessionGeneration
        let updateJS: String
        if testNavigationLoggingEnabled {
            updateJS = """
            presenterUpdate(\(json));
            (function() {
                var iframe = document.getElementById('current-slide');
                var ws = iframe && iframe.contentWindow && iframe.contentWindow.WS;
                return ws && typeof ws.getState === 'function' ? JSON.stringify(ws.getState()) : null;
            })();
            """
        } else {
            updateJS = "presenterUpdate(\(json))"
        }
        pWV.evaluateJavaScript(updateJS) { [weak self, weak pWV] result, presenterError in
            guard let self, let pWV,
                  self.sessionGeneration == generation,
                  self.consoleWebView === pWV else { return }
            if let presenterError = presenterError {
                NSLog("Presto: presenter update failed: %@", presenterError.localizedDescription)
                return
            }
            if self.testNavigationLoggingEnabled, let expectedState {
                var matches = false
                if let mirroredJSON = result as? String,
                   let data = mirroredJSON.data(using: .utf8),
                   let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   self.finiteInt(state["index"]) == expectedState.index,
                   self.finiteInt(state["step"]) == expectedState.step {
                    matches = true
                }
                NSLog("PrestoTestSync: index=%d step=%d match=%d",
                      expectedState.index, expectedState.step, matches ? 1 : 0)
            }
        }
    }

    func syncPresenterFromAudience() {
        guard let aWebView = audienceWebView, !aWebView.isLoading else { return }
        let generation = sessionGeneration

        aWebView.evaluateJavaScript(audienceStateJSONExpression()) { [weak self] result, stateError in
            guard let self = self,
                  self.sessionGeneration == generation,
                  self.audienceWebView === aWebView else { return }
            if let stateError = stateError {
                NSLog("Presto: failed to read audience state: %@", stateError.localizedDescription)
                return
            }
            guard let json = result as? String else {
                NSLog("Presto: invalid audience state: %@", "\(result ?? "nil")")
                return
            }
            self.updatePresenterFromAudienceStateJSON(json)
        }
    }

    func navigateAudience(command: String) {
        guard let aWebView = audienceWebView else { return }
        let generation = sessionGeneration
        // Flag Presto's own moves so the audience page does not report them back.
        let js = """
        window.__prestoRemoteNavigation = true;
        try { \(command); } finally { window.__prestoRemoteNavigation = false; }
        \(audienceStateJSONExpression())
        """

        aWebView.evaluateJavaScript(js) { [weak self] result, navigationError in
            guard let self = self,
                  self.sessionGeneration == generation,
                  self.audienceWebView === aWebView else { return }
            if let navigationError = navigationError {
                NSLog("Presto: audience navigation failed: %@", navigationError.localizedDescription)
                return
            }
            guard let json = result as? String else {
                NSLog("Presto: invalid audience state after navigation: %@", "\(result ?? "nil")")
                return
            }
            self.logNavigationState(json: json)
            self.updatePresenterFromAudienceStateJSON(json)
        }
    }

    func logNavigationState(json: String) {
        guard testNavigationLoggingEnabled else { return }
        guard let data = json.data(using: .utf8),
              let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let index = finiteInt(state["index"]),
              let step = finiteInt(state["step"]) else {
            NSLog("PrestoTestNavigation: parse-error %@", json)
            return
        }
        // maxSteps lets run-app-tests.sh check "Last Slide" against the deck.
        NSLog("PrestoTestNavigation: index=%d step=%d maxSteps=%d",
              index, step, finiteInt(state["maxSteps"]) ?? -1)
    }

    // MARK: - PDF Export

    func showExportSetupError(_ message: String) {
        NSLog("Presto: export setup error: %@", message)
        let alert = NSAlert()
        alert.messageText = "Cannot Export PDF"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    func validateExportOutputURL(_ outputURL: URL, deckURL: URL) -> Bool {
        let fileManager = FileManager.default
        var isDirectory = ObjCBool(false)
        if fileManager.fileExists(atPath: outputURL.path, isDirectory: &isDirectory),
           isDirectory.boolValue {
            showExportSetupError("Output path is a directory")
            return false
        }

        let outputPath = outputURL.standardizedFileURL.resolvingSymlinksInPath().path
        let deckPath = deckURL.standardizedFileURL.resolvingSymlinksInPath().path
        if outputPath == deckPath {
            showExportSetupError("Output path must not be the open deck")
            return false
        }
        if outputURL.pathExtension.lowercased() != "pdf" {
            showExportSetupError("Output path must be a PDF file")
            return false
        }
        return true
    }

    func setEditorPDFExportAvailable(_ available: Bool) {
        mainWindow?.toolbar?.validateVisibleItems()
        guard isEditorMode, let previewWV = previewWebView else { return }
        let js = """
        if (window.__prestoWysiwyg &&
            typeof window.__prestoWysiwyg.setPDFExportAvailable === 'function') {
            window.__prestoWysiwyg.setPDFExportAvailable(\(available ? "true" : "false"));
        }
        """
        previewWV.evaluateJavaScript(js) { _, _ in }
    }

    func releasePDFExportRequest() {
        exportRequestActive = false
        setEditorPDFExportAvailable(true)
    }

    func requestPDFExport() {
        guard let deckURL = currentDeckURL else {
            NSLog("Presto: no deck loaded for export")
            return
        }
        guard !exportRequestActive,
              exportOutputURL == nil,
              exportWebView == nil,
              !exportIsWriting else {
            showExportSetupError("Another PDF export is already running")
            return
        }

        exportRequestActive = true
        setEditorPDFExportAvailable(false)

        prepareEditorForPDFExport { [weak self] ready in
            guard let self else { return }
            guard ready else {
                self.releasePDFExportRequest()
                return
            }
            guard self.currentDeckURL == deckURL else {
                self.releasePDFExportRequest()
                self.showExportSetupError("The open deck changed before export could start")
                return
            }

            if let testMode = self.testPDFExportMode {
                self.beginPDFExport(mode: testMode, deckURL: deckURL)
                return
            }

            self.choosePDFExportMode { [weak self] mode in
                guard let self else { return }
                guard let mode else {
                    self.releasePDFExportRequest()
                    return
                }
                guard self.currentDeckURL == deckURL else {
                    self.releasePDFExportRequest()
                    self.showExportSetupError("The open deck changed before export could start")
                    return
                }
                self.beginPDFExport(mode: mode, deckURL: deckURL)
            }
        }
    }

    func choosePDFExportMode(completion: @escaping (PDFExportMode?) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Export PDF"
        alert.informativeText = """
        Final Slides creates one page per slide at its final reveal state.
        Every Reveal Step creates a page for each reveal state.
        """
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Final Slides")
        alert.addButton(withTitle: "Every Reveal Step")
        alert.addButton(withTitle: "Cancel")

        let handleResponse: (NSApplication.ModalResponse) -> Void = { response in
            switch response {
            case .alertFirstButtonReturn:
                completion(.final)
            case .alertSecondButtonReturn:
                completion(.stepped)
            default:
                completion(nil)
            }
        }

        if let window = mainWindow, window.isVisible {
            alert.beginSheetModal(for: window, completionHandler: handleResponse)
        } else {
            handleResponse(alert.runModal())
        }
    }

    func prepareEditorForPDFExport(completion: @escaping (Bool) -> Void) {
        flushActiveEditor(completion: completion)
    }

    func beginPDFExport(mode: PDFExportMode, deckURL: URL) {
        guard exportRequestActive else { return }

        let outputURL: URL
        if let testOutputURL = testExportOutputURL {
            outputURL = testOutputURL
            NSLog("PrestoTestExport: using mode=%@ output=%@", mode.rawValue, outputURL.path)
        } else {
            let savePanel = NSSavePanel()
            savePanel.allowedContentTypes = [.pdf]
            savePanel.nameFieldStringValue = deckName(for: deckURL.path) + ".pdf"
            savePanel.title = "Export PDF"
            guard savePanel.runModal() == .OK, let selectedURL = savePanel.url else {
                releasePDFExportRequest()
                return
            }
            outputURL = selectedURL
        }
        guard validateExportOutputURL(outputURL, deckURL: deckURL) else {
            releasePDFExportRequest()
            return
        }
        exportMode = mode
        exportOutputURL = outputURL
        exportDeckURL = deckURL
        exportGeneration &+= 1
        exportIsWriting = false
        webContentRecoveryAttempts[.export] = 0
        exportPDFDoc = PDFDocument()
        exportSlideSteps = []
        exportPageIndex = 0
        exportPollCount = 0
        exportPageMap = [:]
        exportPageLinks = [:]

        showExportProgress("Preparing export...")

        let config = makeBaseConfig()
        let wv = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 720), configuration: config)
        wv.navigationDelegate = self
        exportWebView = wv

        let readAccessRoot = findReadAccessRoot(deckURL: deckURL)
        wv.loadFileURL(deckURL, allowingReadAccessTo: readAccessRoot)
        NSLog("Presto: export — loading deck into offscreen webview (mode=%@)", mode.rawValue)
    }

    func showExportProgress(_ message: String) {
        if exportProgressWindow == nil {
            let window = NSPanel(
                contentRect: NSRect(x: 0, y: 0, width: 320, height: 80),
                styleMask: [.titled, .nonactivatingPanel],
                backing: .buffered,
                defer: false
            )
            window.title = "Exporting PDF"
            window.isFloatingPanel = true
            window.center()

            let label = NSTextField(labelWithString: message)
            label.frame = NSRect(x: 20, y: 44, width: 280, height: 20)
            label.font = NSFont.systemFont(ofSize: 13)
            window.contentView?.addSubview(label)
            exportProgressLabel = label

            let progress = NSProgressIndicator(frame: NSRect(x: 20, y: 16, width: 280, height: 20))
            progress.style = .bar
            progress.isIndeterminate = false
            progress.minValue = 0
            progress.maxValue = 100
            progress.doubleValue = 0
            window.contentView?.addSubview(progress)
            exportProgressIndicator = progress

            exportProgressWindow = window
        }

        exportProgressLabel?.stringValue = message
        exportProgressWindow?.makeKeyAndOrderFront(nil)
    }

    func updateExportProgress(_ current: Int, of total: Int) {
        let pct = total > 0 ? Double(current) / Double(total) * 100.0 : 0
        exportProgressIndicator?.doubleValue = pct
        exportProgressLabel?.stringValue = "Capturing page \(current) of \(total)..."
    }

    func dismissExportProgress() {
        exportProgressWindow?.orderOut(nil)
        exportProgressWindow = nil
        exportProgressIndicator = nil
        exportProgressLabel = nil
    }

    func startExportCapture(webView wv: WKWebView, generation: Int) {
        guard exportWebView === wv, exportGeneration == generation else { return }

        exportPollCount += 1
        if exportPollCount > AppDelegate.exportReadyPollLimit {
            finishExport(success: false, message: "Timed out waiting for deck to load", generation: generation)
            return
        }

        wv.evaluateJavaScript(AppDelegate.deckReadyCheckJS) { [weak self] result, readinessError in
            guard let self = self,
                  self.exportGeneration == generation,
                  self.exportWebView === wv else { return }
            if let readinessError = readinessError {
                NSLog("Presto: export — failed while checking deck readiness: %@",
                      readinessError.localizedDescription)
                self.finishExport(success: false, message: "Failed to check export readiness", generation: generation)
                return
            }
            if result as? Bool == true {
                self.prepareExportAndCapture(webView: wv, generation: generation)
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                    guard self.exportGeneration == generation,
                          self.exportWebView === wv else { return }
                    self.startExportCapture(webView: wv, generation: generation)
                }
            }
        }
    }

    func prepareExportAndCapture(webView wv: WKWebView, generation: Int) {
        guard exportWebView === wv, exportGeneration == generation else { return }

        wv.evaluateJavaScript("WS.prepareForExport(); JSON.stringify(WS.getExportInfo())") { [weak self] result, error in
            guard let self = self,
                  self.exportGeneration == generation,
                  self.exportWebView === wv,
                  let json = result as? String,
                  let data = json.data(using: .utf8),
                  let info = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
                NSLog("Presto: export — failed to get slide info: %@", "\(error?.localizedDescription ?? "parse error")")
                self?.finishExport(success: false, message: "Failed to read slide info", generation: generation)
                return
            }

            // Pre-compute the page plan once. Final mode captures each slide at
            // its final reveal state, while every target step aliases to that
            // single page so internal links remain valid.
            var slideSteps: [(slideIndex: Int, step: Int)] = []
            var pageMap: [String: Int] = [:]
            var seenExportPages = Set<String>()
            for (expectedIdx, dict) in info.enumerated() {
                guard let idx = finiteInt(dict["index"]),
                      let maxStep = finiteInt(dict["maxStep"]),
                      idx == expectedIdx,
                      maxStep >= 0 else {
                    self.finishExport(success: false, message: "Invalid export slide info", generation: generation)
                    return
                }

                switch self.exportMode {
                case .stepped:
                    for step in 0...maxStep {
                        let key = "\(idx),\(step)"
                        guard !seenExportPages.contains(key) else {
                            self.finishExport(success: false, message: "Duplicate export page info", generation: generation)
                            return
                        }
                        seenExportPages.insert(key)
                        pageMap[key] = slideSteps.count
                        slideSteps.append((slideIndex: idx, step: step))
                    }
                case .final:
                    let pageIndex = slideSteps.count
                    slideSteps.append((slideIndex: idx, step: maxStep))
                    for step in 0...maxStep {
                        let key = "\(idx),\(step)"
                        guard !seenExportPages.contains(key) else {
                            self.finishExport(success: false, message: "Duplicate export page info", generation: generation)
                            return
                        }
                        seenExportPages.insert(key)
                        pageMap["\(idx),\(step)"] = pageIndex
                    }
                }
            }
            guard !slideSteps.isEmpty else {
                self.finishExport(success: false, message: "No export pages were captured", generation: generation)
                return
            }
            self.exportSlideSteps = slideSteps
            self.exportPageMap = pageMap
            self.exportPageIndex = 0

            NSLog("Presto: export — %d slides, %d total pages (mode=%@)",
                  info.count, self.exportSlideSteps.count, self.exportMode.rawValue)
            self.updateExportProgress(0, of: self.exportSlideSteps.count)
            self.captureNextExportPage(webView: wv, generation: generation)
        }
    }

    func captureNextExportPage(webView wv: WKWebView, generation: Int) {
        guard exportWebView === wv, exportGeneration == generation else { return }

        if exportPageIndex >= exportSlideSteps.count {
            buildExportPDF(webView: wv, generation: generation)
            return
        }

        let target = exportSlideSteps[exportPageIndex]
        let navJS = """
        (function() {
            if (!window.WS || typeof WS.goToSlide !== 'function' || typeof WS.getState !== 'function') {
                throw new Error('WebSlides export API is unavailable');
            }
            WS.goToSlide(\(target.slideIndex), \(target.step));
            var state = WS.getState();
            if (!state || state.index !== \(target.slideIndex) || state.step !== \(target.step)) {
                throw new Error('Export navigation mismatch: requested \(target.slideIndex)/\(target.step), got ' +
                    (state ? state.index : 'unknown') + '/' + (state ? state.step : 'unknown'));
            }
            return true;
        })()
        """

        wv.evaluateJavaScript(navJS) { [weak self] _, navError in
            guard let self = self,
                  self.exportGeneration == generation,
                  self.exportWebView === wv else { return }
            if let navError = navError {
                NSLog("Presto: export — navigation failed at page %d: %@",
                      self.exportPageIndex, navError.localizedDescription)
                self.finishExport(success: false,
                                  message: "Failed to navigate while capturing page \(self.exportPageIndex + 1)",
                                  generation: generation)
                return
            }

            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                guard self.exportGeneration == generation,
                      self.exportWebView === wv else { return }
                // Collect link info for this page
                wv.evaluateJavaScript("JSON.stringify(WS.getExportLinks())") { [weak self] linksResult, linksError in
                    guard let self = self,
                          self.exportGeneration == generation,
                          self.exportWebView === wv else { return }
                    if let linksError = linksError {
                        NSLog("Presto: export — failed to get page links at page %d: %@",
                              self.exportPageIndex, linksError.localizedDescription)
                        self.finishExport(success: false,
                                          message: "Failed to read export links for page \(self.exportPageIndex + 1)",
                                          generation: generation)
                        return
                    }
                    guard let json = linksResult as? String,
                          let data = json.data(using: .utf8),
                          let links = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
                        NSLog("Presto: export — failed to get page links at page %d: parse error",
                              self.exportPageIndex)
                        self.finishExport(success: false,
                                          message: "Failed to read export links for page \(self.exportPageIndex + 1)",
                                          generation: generation)
                        return
                    }
                    if !links.isEmpty {
                        self.exportPageLinks[self.exportPageIndex] = links
                    }

                    let config = WKSnapshotConfiguration()
                    config.snapshotWidth = 2560 as NSNumber

                    wv.takeSnapshot(with: config) { [weak self] image, error in
                        guard let self = self,
                              self.exportGeneration == generation,
                              self.exportWebView === wv else { return }

                        if let image = image, let pdfPage = makePDFPage(fromSnapshot: image) {
                            guard let pdfDoc = self.exportPDFDoc else {
                                NSLog("Presto: export — PDF document disappeared at page %d",
                                      self.exportPageIndex)
                                self.finishExport(success: false,
                                                  message: "Export PDF document is unavailable",
                                                  generation: generation)
                                return
                            }
                            pdfDoc.insert(pdfPage, at: pdfDoc.pageCount)
                        } else {
                            NSLog("Presto: export — snapshot failed at page %d: %@",
                                  self.exportPageIndex, error?.localizedDescription ?? "unknown")
                            self.finishExport(success: false,
                                              message: "Snapshot failed while capturing page \(self.exportPageIndex + 1)",
                                              generation: generation)
                            return
                        }

                        self.exportPageIndex += 1
                        self.updateExportProgress(self.exportPageIndex, of: self.exportSlideSteps.count)
                        self.captureNextExportPage(webView: wv, generation: generation)
                    }
                }
            }
        }
    }

    func isBooleanValue(_ value: Any?) -> Bool {
        guard let value = value else { return false }
        if let number = value as? NSNumber {
            return CFGetTypeID(number) == CFBooleanGetTypeID()
        }
        return value is Bool
    }

    func finiteDouble(_ value: Any?) -> Double? {
        if isBooleanValue(value) { return nil }
        let number: Double?
        if let doubleValue = value as? Double {
            number = doubleValue
        } else if let intValue = value as? Int {
            number = Double(intValue)
        } else if let nsNumber = value as? NSNumber {
            number = nsNumber.doubleValue
        } else {
            number = nil
        }
        guard let number = number, number.isFinite else { return nil }
        return number
    }

    func finiteInt(_ value: Any?) -> Int? {
        if isBooleanValue(value) { return nil }
        if let intValue = value as? Int { return intValue }
        if let nsNumber = value as? NSNumber {
            let doubleValue = nsNumber.doubleValue
            guard doubleValue.isFinite,
                  doubleValue.rounded() == doubleValue,
                  doubleValue >= Double(Int.min),
                  doubleValue <= Double(Int.max) else { return nil }
            return Int(doubleValue)
        }
        return nil
    }

    func normalizedPDFLinkRect(_ link: [String: Any]) -> NSRect? {
        guard let x = finiteDouble(link["x"]),
              let y = finiteDouble(link["y"]),
              let w = finiteDouble(link["w"]),
              let h = finiteDouble(link["h"]),
              w > 0,
              h > 0 else { return nil }

        let x1 = max(0, x)
        let y1 = max(0, y)
        let x2 = min(1280, x + w)
        let y2 = min(720, y + h)
        guard x2 > x1, y2 > y1 else { return nil }

        // Convert from screen coords (top-left) to PDF coords (bottom-left).
        return NSRect(x: x1, y: 720 - y2, width: x2 - x1, height: y2 - y1)
    }

    func validatePDFOutput(_ url: URL, expectedPageCount: Int) -> Bool {
        guard expectedPageCount > 0,
              let writtenDoc = PDFDocument(url: url),
              writtenDoc.pageCount == expectedPageCount else {
            return false
        }
        return true
    }

    func writePDFAtomically(pdfDoc: PDFDocument, outputURL: URL, expectedPageCount: Int) -> Bool {
        let directory = outputURL.deletingLastPathComponent()
        let tempURL = directory.appendingPathComponent(".\(outputURL.lastPathComponent).tmp-\(UUID().uuidString).pdf")
        let fileManager = FileManager.default

        guard pdfDoc.write(to: tempURL) else {
            try? fileManager.trashItem(at: tempURL, resultingItemURL: nil)
            return false
        }
        guard validatePDFOutput(tempURL, expectedPageCount: expectedPageCount) else {
            NSLog("Presto: export — temp PDF failed validation: %@", tempURL.path)
            try? fileManager.trashItem(at: tempURL, resultingItemURL: nil)
            return false
        }

        do {
            if fileManager.fileExists(atPath: outputURL.path) {
                _ = try fileManager.replaceItemAt(outputURL,
                                                  withItemAt: tempURL,
                                                  backupItemName: nil,
                                                  options: [])
            } else {
                try fileManager.moveItem(at: tempURL, to: outputURL)
            }
            return true
        } catch {
            NSLog("Presto: export — failed to publish PDF atomically: %@", error.localizedDescription)
            try? fileManager.trashItem(at: tempURL, resultingItemURL: nil)
            return false
        }
    }

    func buildExportPDF(webView wv: WKWebView, generation: Int) {
        guard exportWebView === wv, exportGeneration == generation else { return }
        guard let outputURL = exportOutputURL, let pdfDoc = exportPDFDoc else {
            finishExport(success: false, message: "No output path or PDF document", generation: generation)
            return
        }
        guard !exportSlideSteps.isEmpty else {
            finishExport(success: false, message: "No export pages were captured", generation: generation)
            return
        }

        // Add clickable link annotations
        var linkCount = 0
        for (pageIdx, links) in exportPageLinks {
            guard pageIdx >= 0,
                  pageIdx < pdfDoc.pageCount,
                  let pdfPage = pdfDoc.page(at: pageIdx) else {
                finishExport(success: false, message: "Export link source page is not in the PDF document", generation: generation)
                return
            }
            for link in links {
                guard let targetSlide = finiteInt(link["targetSlideIndex"]),
                      let targetStep = finiteInt(link["targetStep"]),
                      finiteDouble(link["x"]) != nil,
                      finiteDouble(link["y"]) != nil,
                      finiteDouble(link["w"]) != nil,
                      finiteDouble(link["h"]) != nil else {
                    finishExport(success: false, message: "Invalid export link metadata", generation: generation)
                    return
                }
                guard let rect = normalizedPDFLinkRect(link) else { continue }
                let key = "\(targetSlide),\(targetStep)"
                guard let targetPageIdx = exportPageMap[key],
                      let targetPage = pdfDoc.page(at: targetPageIdx) else {
                    finishExport(success: false, message: "Export link target is not in the export page plan", generation: generation)
                    return
                }
                guard targetPageIdx != pageIdx else { continue }

                let annotation = PDFAnnotation(bounds: rect, forType: .link, withProperties: nil)
                annotation.action = PDFActionGoTo(destination: PDFDestination(page: targetPage, at: NSPoint(x: 0, y: 720)))
                let border = PDFBorder()
                border.lineWidth = 0
                annotation.border = border
                pdfPage.addAnnotation(annotation)
                linkCount += 1
            }
        }
        NSLog("Presto: export — added %d clickable links", linkCount)

        let pageCount = pdfDoc.pageCount
        guard pageCount == exportSlideSteps.count else {
            finishExport(success: false,
                         message: "Export captured \(pageCount) of \(exportSlideSteps.count) pages",
                         generation: generation)
            return
        }
        NSLog("Presto: export — writing PDF with %d pages", pageCount)
        exportProgressLabel?.stringValue = "Writing PDF..."
        exportIsWriting = true
        wv.navigationDelegate = nil

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let success = self?.writePDFAtomically(pdfDoc: pdfDoc, outputURL: outputURL, expectedPageCount: pageCount) ?? false
            DispatchQueue.main.async {
                guard let self = self,
                      self.exportGeneration == generation,
                      self.exportIsWriting else { return }
                if success {
                    NSLog("Presto: export — PDF saved to %@", outputURL.path)
                    self.finishExport(success: true,
                                      message: "Exported \(pageCount) pages to \(outputURL.lastPathComponent)",
                                      generation: generation)
                } else {
                    self.finishExport(success: false, message: "Failed to write PDF file", generation: generation)
                }
            }
        }
    }

    func finishExport(success: Bool, message: String, generation: Int? = nil) {
        if let generation, generation != exportGeneration { return }
        let completedOutputURL = exportOutputURL
        exportGeneration &+= 1
        dismissExportProgress()

        // Cleanup export webview
        exportWebView?.stopLoading()
        exportWebView?.navigationDelegate = nil
        exportWebView = nil
        exportPDFDoc = nil
        exportSlideSteps = []
        exportPageMap = [:]
        exportPageLinks = [:]
        exportDeckURL = nil
        exportIsWriting = false
        webContentRecoveryAttempts[.export] = nil

        if testExportOutputURL != nil {
            NSLog("PrestoTestExport: success=%d output=%@ message=%@",
                  success ? 1 : 0, completedOutputURL?.path ?? "none", message)
            exportOutputURL = nil
            releasePDFExportRequest()
            return
        }

        let alert = NSAlert()
        alert.messageText = success ? "Export Complete" : "Export Failed"
        alert.informativeText = message
        alert.alertStyle = success ? .informational : .warning
        alert.addButton(withTitle: "OK")
        if success, let url = exportOutputURL {
            alert.addButton(withTitle: "Reveal in Finder")
            let response = alert.runModal()
            if response == .alertSecondButtonReturn {
                NSWorkspace.shared.activateFileViewerSelecting([url])
            }
        } else {
            alert.runModal()
        }

        exportOutputURL = nil
        releasePDFExportRequest()
    }
}

// MARK: - App Setup

#if !PRESTO_TESTING
var retainedAppDelegate: AppDelegate?
@main
struct PrestoApplication {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = AppDelegate()
        retainedAppDelegate = delegate
        app.delegate = delegate
        app.mainMenu = delegate.buildMainMenu()
        app.run()
    }
}
#endif

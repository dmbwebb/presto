import Cocoa
import Foundation
import JavaScriptCore
import WebKit

// Backups written by the code under test go here, never to the real
// ~/Library/Application Support/Presto/backups.
let testBackupRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent("presto-test-backups-\(UUID().uuidString)", isDirectory: true)
// Likewise the deck library and its old-defaults migration.
let testLibraryStore = testBackupRoot.appendingPathComponent("library.json")
let testLibraryDefaults = UserDefaults(suiteName: "presto-tests-\(UUID().uuidString)")!

// Every test app inherits this. Each modal entry point of AppDelegate records
// a failure instead of showing an alert or panel, so a regression that reaches
// UI fails the suite rather than putting a dialog on Duncan's screen. The
// main window is created when needed but never ordered front or activated.
class StubbedUIApp: AppDelegate {
    func unexpectedUI(_ name: String) {
        TransactionTests.check(false, "no modal UI in tests (reached \(name))")
    }
    override var deckBackupRootURL: URL { testBackupRoot }
    override func makeDeckLibrary() -> DeckLibrary {
        DeckLibrary(storeURL: testLibraryStore, registryURL: nil, defaults: testLibraryDefaults)
    }
    override func showMainWindow() {}
    override func enqueueThumbnail(_ deck: URL, slide: Int, step: Int, position: Bool) {}
    override func showMissingDeckAlert(path: String) { unexpectedUI("showMissingDeckAlert") }
    override func showDeckOpenError(deckURL: URL, error: Error) { unexpectedUI("showDeckOpenError") }
    override func showNoSlidesAlert(deckURL: URL) { unexpectedUI("showNoSlidesAlert") }
    override func showDeckStructureAlert(deckURL: URL, issues: [String]) { unexpectedUI("showDeckStructureAlert") }
    override func openDeckFileInMode(editMode: Bool) { unexpectedUI("openDeckFileInMode") }
    override func addFolderToDecks(_ sender: Any?) { unexpectedUI("addFolderToDecks") }
    // Real going live is allowed only with no main window (nothing can
    // enter full screen or be ordered front).
    var allowsGoingLive = false
    override func goLive(index: Int, step: Int, origin: PrestoMode) {
        guard allowsGoingLive, mainWindow == nil else { return unexpectedUI("goLive (opens windows)") }
        super.goLive(index: index, step: step, origin: origin)
    }
    override func showWebContentRecoveryFailure(role: WebContentRole) { unexpectedUI("showWebContentRecoveryFailure") }
    override func showEditorRecoveredSheet() { unexpectedUI("showEditorRecoveredSheet") }
    override func showExportSetupError(_ message: String) { unexpectedUI("showExportSetupError") }
    override func choosePDFExportMode(completion: @escaping (PDFExportMode?) -> Void) { unexpectedUI("choosePDFExportMode") }
    override func beginPDFExport(mode: PDFExportMode, deckURL: URL) { unexpectedUI("beginPDFExport") }
    override func showExportProgress(_ message: String) {}
    override func finishExport(success: Bool, message: String, generation: Int? = nil) { unexpectedUI("finishExport") }
    override func askExternalMergeDecision(message: String, details: String, buttons: [ExternalMergeChoice],
                                           completion: @escaping (ExternalMergeChoice) -> Void) {
        unexpectedUI("askExternalMergeDecision")
    }
}

final class TransactionApp: StubbedUIApp {
    var writes: [(URL, String)] = []
    var failWrites = false
    var refreshed: [Int] = []
    var saveErrors: [String] = []
    var openedDeck: URL?
    var diskReloads = 0

    override func writeDeckContent(_ content: String, to deckURL: URL,
                                   successMessage: String, failurePrefix: String) -> Error? {
        if failWrites {
            return NSError(domain: "TestDisk", code: 1,
                           userInfo: [NSLocalizedDescriptionKey: "Disk unavailable"])
        }
        writes.append((deckURL, content))
        return nil
    }

    override func reportEditorSaveFailed(_ message: String, saveId: Int? = nil) {
        saveErrors.append(message)
    }

    var recordedChanges: [Int: RecordedSlideChange] = [:]
    override func refreshPreviewSlide(_ index: Int, recording change: RecordedSlideChange? = nil) {
        refreshed.append(index)
        if let change { recordedChanges[index] = change }
    }
    override func reloadPreviewAtSlide(_ index: Int) { refreshed.append(index) }
    override func showPresenterHint(_ message: String) {}
    override func startFileWatcher() {}
    override func openNewDeck(_ url: URL, in target: PrestoMode, goLive: Bool) {
        guard cleanupPresentation() else { return }
        currentDeckURL = url
        openedDeck = url
    }
    override func reloadFromDisk(forcePreviewReload: Bool = false) { diskReloads += 1 }
}

final class ScriptView: WKWebView {
    var pending = #"{"dirty":false,"index":0,"html":""}"#
    var onCommit: (() -> Void)?
    var scriptError: Error?

    override func evaluateJavaScript(_ javaScriptString: String,
                                     completionHandler: ((Any?, Error?) -> Void)? = nil) {
        if let scriptError {
            completionHandler?(nil, scriptError)
        } else if javaScriptString.contains("getPendingEditorChange") {
            completionHandler?(pending, nil)
        } else {
            if javaScriptString.contains("commitActiveSession") { onCommit?() }
            completionHandler?(nil, nil)
        }
    }
}

// Real watcher, real disk: only the web views are absent (their guards no-op).
final class WatchApp: StubbedUIApp {
    var appliedDiskReloads: [String] = []
    var conflicts: [String] = []
    override func applyDiskReload(_ content: String) {
        appliedDiskReloads.append(content)
        super.applyDiskReload(content)
    }
    // Never show a real alert from the test binary.
    override func presentExternalChangeConflict(diskContent: String) {
        conflicts.append(diskContent)
        isPresentingExternalConflict = true
        pendingDeckWrite?.cancel()
    }
    override func reportEditorSaveFailed(_ message: String, saveId: Int? = nil) {}
    override func showPresenterHint(_ message: String) {}
}

func deckHTML(_ slides: [String], head: String = "<style>h1{}</style>") -> String {
    let body = slides.map { "<section class=\"slide\"><h1>\($0)</h1></section>" }.joined(separator: "\n")
    return "<html><head>\(head)</head><body><main class=\"ws-deck\">\n\(body)\n</main></body></html>"
}

func pump(_ seconds: TimeInterval) {
    RunLoop.current.run(until: Date().addingTimeInterval(seconds))
}

// Real merge path on a real file; the decision dialog and hints are recorded.
final class MergeApp: StubbedUIApp {
    var hints: [String] = []
    var saveErrors: [String] = []
    var decisions: [(details: String, buttons: [ExternalMergeChoice])] = []
    var nextChoice: ExternalMergeChoice = .keepMine
    var backupRoot = testBackupRoot
    override var deckBackupRootURL: URL { backupRoot }
    override func askExternalMergeDecision(message: String, details: String, buttons: [ExternalMergeChoice],
                                           completion: @escaping (ExternalMergeChoice) -> Void) {
        decisions.append((details, buttons))
        completion(nextChoice)
    }
    override func showPresenterHint(_ message: String) { hints.append(message) }
    override func reportEditorSaveFailed(_ message: String, saveId: Int? = nil) { saveErrors.append(message) }
}

// Presentation-mode navigation and Escape handling, minus windows and WebKit.
final class PresentApp: StubbedUIApp {
    var audienceCommands: [String] = []
    var hints: [String] = []
    var endRequests = 0
    override func navigateAudience(command: String) { audienceCommands.append(command) }
    override func showHint(_ message: String, in webView: WKWebView?, duration: TimeInterval) { hints.append(message) }
    override func requestEndPresentation() { endRequests += 1 }
}

final class AudienceView: WKWebView {
    var returnStackDepth = 0
    override func evaluateJavaScript(_ javaScriptString: String,
                                     completionHandler: ((Any?, Error?) -> Void)? = nil) {
        completionHandler?(javaScriptString.contains("returnStack.length") ? returnStackDepth : nil, nil)
    }
}

final class DiskApp: StubbedUIApp {
    var conflict: String?
    override func presentExternalChangeConflict(diskContent: String) {
        conflict = diskContent
        isPresentingExternalConflict = true
        pendingDeckWrite?.cancel()
    }
    override func reportEditorSaveFailed(_ message: String, saveId: Int? = nil) {}
}

// PDF export readiness against a scripted page; finishing stops the poll
// chain the way the real finishExport does.
final class ExportApp: StubbedUIApp {
    var prepared = 0
    var finished: [String] = []
    override func prepareExportAndCapture(webView wv: WKWebView, generation: Int) { prepared += 1 }
    override func finishExport(success: Bool, message: String, generation: Int? = nil) {
        finished.append(message)
        exportWebView = nil
    }
}

// A deck page simulated in JavaScriptCore: just enough DOM for Presto's
// injected scripts, and an engine whose WS.ready is pending. Before
// finishLoading() slide 3 knows 1 step (its external table has not loaded);
// afterwards it has 3, as the engine reports when WS.ready resolves.
let scriptedDeckPageJS = """
    var window = this;
    var posted = [];
    var refits = 0;
    var frames = [];
    var remoteFlags = [];
    window.webkit = { messageHandlers: { presto: { postMessage: function (m) { posted.push(m); } } } };
    window.innerWidth = 1280;
    window.innerHeight = 720;
    window.addEventListener = function () {};
    window.getComputedStyle = function () { return { textAlign: 'left' }; };
    var getComputedStyle = window.getComputedStyle;
    var requestAnimationFrame = function (f) { frames.push(f); };
    function stubElement() {
        return {
            style: {}, dataset: {}, children: [],
            classList: { add: function () {}, remove: function () {}, toggle: function () {} },
            appendChild: function (child) { this.children.push(child); },
            addEventListener: function () {},
            getBoundingClientRect: function () { return { width: 0, height: 0 }; },
            querySelector: function () { return null; },
            querySelectorAll: function () { return []; },
            cloneNode: function () { return stubElement(); }
        };
    }
    var document = {
        documentElement: { dataset: {} },
        head: stubElement(),
        body: stubElement(),
        querySelector: function () { return null; },
        querySelectorAll: function () { return []; },
        getElementById: function () { return null; },
        createElement: stubElement,
        addEventListener: function () {}
    };
    var resolveReady;
    var WS = {
        slides: [stubElement(), stubElement(), stubElement()],
        currentIndex: 0,
        currentStep: 0,
        stepsKnown: [0, 0, 1],
        returnStack: [],
        ready: new Promise(function (resolve) { resolveReady = resolve; }),
        listeners: [],
        on: function (type, fn) { if (type === 'navigate') this.listeners.push(fn); },
        goToSlide: function (index, step) {
            remoteFlags.push(!!window.__prestoRemoteNavigation);
            this.currentIndex = index;
            this.currentStep = Math.min(step, this.stepsKnown[index]);
            var state = { index: this.currentIndex, step: this.currentStep };
            this.listeners.forEach(function (fn) { fn(state); });
        },
        refitTables: function () { refits += 1; return Promise.resolve(); }
    };
    function finishLoading() {
        WS.stepsKnown = [0, 0, 3];
        document.documentElement.dataset.wsReady = '1';
        resolveReady(WS);
    }
    """

final class JSDeckView: WKWebView {
    let context = JSContext()!
    var scriptErrors: [String] = []

    func setUp() -> JSDeckView {
        context.exceptionHandler = { [weak self] _, exception in
            self?.scriptErrors.append(exception?.toString() ?? "unknown JavaScript error")
        }
        context.evaluateScript(scriptedDeckPageJS)
        return self
    }

    @discardableResult
    func run(_ script: String) -> JSValue? { context.evaluateScript(script) }

    func number(_ expression: String) -> Int { Int(run(expression)?.toInt32() ?? -1) }

    override func evaluateJavaScript(_ javaScriptString: String,
                                     completionHandler: (@MainActor @Sendable (Any?, Error?) -> Void)? = nil) {
        let value = context.evaluateScript(javaScriptString)
        completionHandler?(value?.toObject(), nil)
    }
}

final class WatchedApp: StubbedUIApp {
    var refreshed: [Int] = []
    var conflicts: [String] = []
    var reloadErrors: [String] = []
    override func reloadPreviewAtSlide(_ index: Int) { refreshed.append(index) }
    override func presentExternalChangeConflict(diskContent: String) {
        conflicts.append(diskContent)
        isPresentingExternalConflict = true
        stopFileWatcher()
    }
    override func reportEditorSaveFailed(_ message: String, saveId: Int? = nil) {
        reloadErrors.append(message)
    }
}

@main
struct TransactionTests {
    static var passed = 0
    static var failed = 0

    static func check(_ condition: Bool, _ name: String) {
        if condition { passed += 1; print("PASS: \(name)") }
        else { failed += 1; print("FAIL: \(name)") }
    }

    static func fixture() -> TransactionApp {
        let app = TransactionApp()
        app.isEditorMode = true
        app.currentDeckURL = URL(fileURLWithPath: "/tmp/presto-transaction-deck.html")
        app.fileContent = """
        <html><body><main class="ws-deck"><section class="slide"><h1>Original</h1></section><section class="slide"><h1>Second</h1></section></main></body></html>
        """
        app.sectionInfos = app.extractSections(from: app.fileContent!)
        return app
    }

    static func tempDeckDirectory(_ label: String) throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("presto-\(label)-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    // The deck file is watched for real: Presto's own atomic save and outside
    // replacements both swap the inode, which the watcher must survive.
    static func fileWatcherTests() {
        do {
            let directory = try tempDeckDirectory("watch-test")
            defer { _ = try? FileManager.default.trashItem(at: directory, resultingItemURL: nil) }
            let url = directory.appendingPathComponent("index.html")
            let original = deckHTML(["One", "Two"])
            try original.write(to: url, atomically: true, encoding: .utf8)

            let app = WatchApp()
            app.isEditorMode = true
            app.currentDeckURL = url
            app.fileContent = original
            app.lastPersistedContent = original
            app.sectionInfos = app.extractSections(from: original)
            app.startFileWatcher()
            pump(0.2)

            _ = app.handleEditorChanged(html: "<h1>One edited</h1>", index: 0)
            check(app.flushPendingDeckWrite(), "watched deck: Presto's own atomic save succeeds")
            let saved = try String(contentsOf: url, encoding: .utf8)
            pump(0.7)
            check(app.appliedDiskReloads.isEmpty && app.fileContent == saved,
                  "watched deck: Presto's own save is not treated as an outside change")

            let agentAtomic = deckHTML(["One edited", "Two from agent"])
            try agentAtomic.write(to: url, atomically: true, encoding: .utf8)
            pump(0.8)
            check(app.fileContent == agentAtomic,
                  "watched deck: outside atomic replacement after a Presto save is picked up")

            let agentInPlace = deckHTML(["One edited", "Two in place"])
            try agentInPlace.write(to: url, atomically: false, encoding: .utf8)
            pump(0.8)
            check(app.fileContent == agentInPlace, "watched deck: outside in-place write is picked up")

            let replacement = directory.appendingPathComponent("incoming.html")
            let synced = deckHTML(["One synced", "Two in place"])
            try synced.write(to: replacement, atomically: false, encoding: .utf8)
            _ = try FileManager.default.replaceItemAt(url, withItemAt: replacement)
            pump(0.8)
            check(app.fileContent == synced, "watched deck: sync-client file replacement is picked up")

            unlink(url.path)
            pump(0.15)
            let recreated = deckHTML(["One recreated", "Two in place"])
            try recreated.write(to: url, atomically: false, encoding: .utf8)
            pump(1.2)
            check(app.fileContent == recreated, "watched deck: delete then recreate is picked up")

            _ = app.handleEditorChanged(html: "<h1>Second Presto save</h1>", index: 1)
            check(app.flushPendingDeckWrite(), "watched deck: a later Presto save succeeds")
            pump(0.5)
            let afterSecondSave = deckHTML(["One recreated", "Two after second save"])
            try afterSecondSave.write(to: url, atomically: true, encoding: .utf8)
            pump(0.8)
            check(app.fileContent == afterSecondSave,
                  "watched deck: outside change after a second Presto save is picked up")
            app.stopFileWatcher()
        } catch {
            check(false, "file watcher setup: \(error)")
        }
    }

    static func slideTitles(_ app: AppDelegate, _ html: String?) -> [String] {
        guard let html else { return [] }
        return app.extractSections(from: html).map {
            $0.content.replacingOccurrences(of: "<section class=\"slide\"><h1>", with: "")
                .replacingOccurrences(of: "</h1></section>", with: "")
        }
    }

    // Pure three-way merge: base = last loaded/saved, ours = Presto, theirs = disk.
    static func mergeTests() {
        let app = StubbedUIApp()
        let base = deckHTML(["A", "B", "C", "D"])
        func merge(_ ours: String, _ theirs: String, preferOurs: Bool = true) -> DeckMergeOutcome? {
            app.mergeDeckVersions(base: base, ours: ours, theirs: theirs, preferOurs: preferOurs)
        }

        let split = merge(deckHTML(["A mine", "B", "C", "D"]), deckHTML(["A", "B disk", "C", "D"]))
        check(split?.conflicts.isEmpty == true &&
              slideTitles(app, split?.content) == ["A mine", "B disk", "C", "D"],
              "merge: slides changed on different sides both survive without a decision")
        check(split?.takenFromDisk == [2] && split?.keptLocal == [1],
              "merge: reports which slides came from disk and which edits were kept")

        let adjacent = merge(deckHTML(["A", "B mine", "C", "D"]), deckHTML(["A", "B", "C disk", "D"]))
        check(adjacent?.conflicts.isEmpty == true &&
              slideTitles(app, adjacent?.content) == ["A", "B mine", "C disk", "D"],
              "merge: edits to neighbouring slides are not a conflict")

        let inserted = merge(deckHTML(["A", "B mine", "C", "D"]), deckHTML(["New", "A", "B", "C", "D"]))
        check(inserted?.conflicts.isEmpty == true &&
              slideTitles(app, inserted?.content) == ["New", "A", "B mine", "C", "D"],
              "merge: a slide inserted on disk is aligned by content, not index")
        check(inserted?.oursIndexMap[1] == 2 && inserted?.addedOnDisk == 1,
              "merge: Presto's slide position follows the inserted slide")

        let deleted = merge(deckHTML(["A mine", "B", "C", "D"]), deckHTML(["A", "B", "D"]))
        check(deleted?.conflicts.isEmpty == true &&
              slideTitles(app, deleted?.content) == ["A mine", "B", "D"] && deleted?.deletedOnDisk == 1,
              "merge: a slide deleted on disk and untouched in Presto is removed")

        let results = "Results for the treatment group"
        let richBase = deckHTML(["Intro", results, "Conclusion"])
        let insertAndEdit = app.mergeDeckVersions(
            base: richBase,
            ours: deckHTML(["Intro", results + " (my wording)", "Conclusion"]),
            theirs: deckHTML(["Intro", "Robustness checks appendix", results + " updated", "Conclusion"]),
            preferOurs: true)
        check(insertAndEdit?.conflicts == ["slide 2 (slide 3 on disk)"] &&
              slideTitles(app, insertAndEdit?.content) == ["Intro", "Robustness checks appendix", results + " (my wording)", "Conclusion"],
              "merge: next to an insertion, an edited slide is matched by similarity, not position")

        let both = merge(deckHTML(["A", "B mine", "C", "D"]), deckHTML(["A", "B disk", "C disk", "D"]))
        check(both?.conflicts == ["slide 2"], "merge: a slide changed on both sides is the only decision")
        check(slideTitles(app, both?.content) == ["A", "B mine", "C disk", "D"],
              "merge: keeping mine for the conflict still takes other disk changes")
        let bothDisk = merge(deckHTML(["A", "B mine", "C", "D"]), deckHTML(["A", "B disk", "C disk", "D"]),
                             preferOurs: false)
        check(slideTitles(app, bothDisk?.content) == ["A", "B disk", "C disk", "D"],
              "merge: using the disk version resolves only the conflicting slide")

        let same = merge(deckHTML(["A", "B same", "C", "D"]), deckHTML(["A", "B same", "C", "D"]))
        check(same?.conflicts.isEmpty == true, "merge: an identical change on both sides needs no decision")

        let editVsDelete = merge(deckHTML(["A", "B mine", "C", "D"]), deckHTML(["A", "C", "D"]))
        check(editVsDelete?.conflicts == ["slide 2 (deleted on disk)"],
              "merge: a slide deleted on disk but edited in Presto needs a decision")
        let editVsDeleteDisk = merge(deckHTML(["A", "B mine", "C", "D"]), deckHTML(["A", "C", "D"]), preferOurs: false)
        check(slideTitles(app, editVsDeleteDisk?.content) == ["A", "C", "D"],
              "merge: using the disk version honours the deletion")

        let headDisk = merge(deckHTML(["A mine", "B", "C", "D"]),
                             deckHTML(["A", "B", "C", "D"], head: "<style>h1{color:red}</style>"))
        check(headDisk?.conflicts.isEmpty == true && headDisk?.headFromDisk == true &&
              headDisk?.content.contains("color:red") == true,
              "merge: a head/styles change made only on disk is taken")
        let headBoth = app.mergeDeckVersions(
            base: base,
            ours: deckHTML(["A", "B", "C", "D"], head: "<style>h1{color:blue}</style>"),
            theirs: deckHTML(["A", "B", "C", "D"], head: "<style>h1{color:red}</style>"),
            preferOurs: true)
        check(headBoth?.conflicts.count == 1 && headBoth?.conflicts.first?.hasPrefix("the deck head") == true,
              "merge: a head changed on both sides needs a decision")
        check(app.mergeDeckVersions(base: base, ours: base, theirs: "<html><body>no slides</body></html>",
                                    preferOurs: true) == nil,
              "merge: a disk version without slides is not merged slide by slide")
    }

    static func mergeFixture(_ url: URL, _ content: String) -> MergeApp {
        let app = MergeApp()
        app.isEditorMode = true
        app.currentDeckURL = url
        app.fileContent = content
        app.lastPersistedContent = content
        app.sectionInfos = app.extractSections(from: content)
        return app
    }

    static func backups(for app: AppDelegate, _ url: URL) -> [URL] {
        let directory = testBackupRoot.appendingPathComponent(app.deckBackupIdentifier(for: url))
        return ((try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? [])
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    // The merge at the real save boundary, plus the backups it must leave.
    static func mergeSaveTests() {
        do {
            let directory = try tempDeckDirectory("merge-test")
            defer { _ = try? FileManager.default.trashItem(at: directory, resultingItemURL: nil) }
            let url = directory.appendingPathComponent("index.html")
            let original = deckHTML(["One", "Two", "Three"])

            try original.write(to: url, atomically: true, encoding: .utf8)
            let clean = mergeFixture(url, original)
            _ = clean.handleEditorChanged(html: "<h1>One mine</h1>", index: 0)
            let agent = deckHTML(["One", "Two agent", "Three"])
            try agent.write(to: url, atomically: true, encoding: .utf8)
            check(!clean.flushPendingDeckWrite(), "merge save: the write boundary stops the blind overwrite")
            pump(0.2)
            let cleanDisk = try String(contentsOf: url, encoding: .utf8)
            check(slideTitles(clean, cleanDisk) == ["One mine", "Two agent", "Three"] &&
                  clean.fileContent == cleanDisk && !clean.deckWriteDirty,
                  "merge save: an agent's edit to another slide is merged, not erased")
            check(clean.decisions.isEmpty && clean.hints.contains { $0.contains("slide 2") },
                  "merge save: a clean merge asks nothing and says what came from disk")
            check(backups(for: clean, url).contains { (try? String(contentsOf: $0, encoding: .utf8)) == agent },
                  "merge save: the agent's version is backed up before Presto writes over it")
            check(AppDelegate().deckBackupRootURL.path.hasSuffix("/Library/Application Support/Presto/backups"),
                  "merge save: production backups live in Application Support, not the synced deck folder")
            clean.stopFileWatcher()

            try original.write(to: url, atomically: true, encoding: .utf8)
            let conflicted = mergeFixture(url, original)
            _ = conflicted.handleEditorChanged(html: "<h1>Two mine</h1>", index: 1)
            try deckHTML(["One agent", "Two agent", "Three"]).write(to: url, atomically: true, encoding: .utf8)
            conflicted.nextChoice = .keepMine
            _ = conflicted.flushPendingDeckWrite()
            pump(0.2)
            check(conflicted.decisions.count == 1 &&
                  conflicted.decisions.first?.details.contains("Changed both in Presto and on disk: slide 2.") == true &&
                  conflicted.decisions.first?.buttons == [.keepMine, .useDisk, .reloadFromDisk],
                  "merge save: only the slide changed on both sides is put to the user, by number")
            let keptDisk = try String(contentsOf: url, encoding: .utf8)
            check(slideTitles(conflicted, keptDisk) == ["One agent", "Two mine", "Three"],
                  "merge save: Keep My Versions keeps mine for the conflict and the agent's other edit")
            conflicted.stopFileWatcher()

            try original.write(to: url, atomically: true, encoding: .utf8)
            let useDisk = mergeFixture(url, original)
            _ = useDisk.handleEditorChanged(html: "<h1>One mine</h1>", index: 0)
            _ = useDisk.handleEditorChanged(html: "<h1>Two mine</h1>", index: 1)
            try deckHTML(["One", "Two agent", "Three"]).write(to: url, atomically: true, encoding: .utf8)
            useDisk.nextChoice = .useDisk
            _ = useDisk.flushPendingDeckWrite()
            pump(0.2)
            let useDiskResult = try String(contentsOf: url, encoding: .utf8)
            check(slideTitles(useDisk, useDiskResult) == ["One mine", "Two agent", "Three"],
                  "merge save: Use Disk Versions still keeps my edit to a slide the agent left alone")
            useDisk.stopFileWatcher()

            try original.write(to: url, atomically: true, encoding: .utf8)
            let discard = mergeFixture(url, original)
            _ = discard.handleEditorChanged(html: "<h1>One mine</h1>", index: 0)
            let unsaved = discard.fileContent
            let agentAll = deckHTML(["One agent", "Two", "Three"])
            try agentAll.write(to: url, atomically: true, encoding: .utf8)
            discard.nextChoice = .reloadFromDisk
            _ = discard.flushPendingDeckWrite()
            pump(0.2)
            let discardDisk = try String(contentsOf: url, encoding: .utf8)
            check(discard.fileContent == agentAll && discardDisk == agentAll,
                  "merge save: Reload from Disk loads the disk version untouched")
            check(backups(for: discard, url).contains { (try? String(contentsOf: $0, encoding: .utf8)) == unsaved },
                  "merge save: discarded unsaved edits are kept as a backup")
            discard.stopFileWatcher()

            let firstSave = deckHTML(["Loaded", "Two", "Three"])
            try firstSave.write(to: url, atomically: true, encoding: .utf8)
            let saver = mergeFixture(url, firstSave)
            let before = backups(for: saver, url).count
            _ = saver.handleEditorChanged(html: "<h1>Saved once</h1>", index: 0)
            _ = saver.flushPendingDeckWrite()
            _ = saver.handleEditorChanged(html: "<h1>Saved twice</h1>", index: 0)
            _ = saver.flushPendingDeckWrite()
            let after = backups(for: saver, url)
            check(after.count == before + 1 && (try? String(contentsOf: after.last!, encoding: .utf8)) == firstSave,
                  "merge save: the first save backs up the loaded version; Presto's own writes are not re-backed up")

            // An agent replaces slide 2 outright while Duncan edits it. The
            // merge pairs the two by position, so it is one "slide 2" conflict
            // and whichever version loses must survive in a backup.
            func replacedSlideConflict(_ choice: ExternalMergeChoice) throws -> (MergeApp, mine: String?, agent: String) {
                try original.write(to: url, atomically: true, encoding: .utf8)
                let app = mergeFixture(url, original)
                _ = app.handleEditorChanged(html: "<h1>Two with my rewording</h1>", index: 1)
                let mine = app.fileContent
                let agent = deckHTML(["One", "A brand new slide from the agent", "Three"])
                try agent.write(to: url, atomically: true, encoding: .utf8)
                app.nextChoice = choice
                _ = app.flushPendingDeckWrite()
                pump(0.2)
                app.stopFileWatcher()
                return (app, mine, agent)
            }
            func backedUp(_ app: MergeApp, _ content: String?) -> Bool {
                backups(for: app, url).contains { (try? String(contentsOf: $0, encoding: .utf8)) == content }
            }

            let (useDiskReplaced, mineLost, _) = try replacedSlideConflict(.useDisk)
            check(slideTitles(useDiskReplaced, try String(contentsOf: url, encoding: .utf8)) ==
                  ["One", "A brand new slide from the agent", "Three"] &&
                  useDiskReplaced.decisions.first?.details.contains("slide 2") == true,
                  "merge save: Use Disk Versions takes the agent's replacement slide")
            check(backedUp(useDiskReplaced, mineLost),
                  "merge save: Use Disk Versions first backs up Presto's version, whose slide it drops")

            let (keepMineReplaced, _, agentLost) = try replacedSlideConflict(.keepMine)
            check(slideTitles(keepMineReplaced, try String(contentsOf: url, encoding: .utf8)) ==
                  ["One", "Two with my rewording", "Three"] && backedUp(keepMineReplaced, agentLost),
                  "merge save: Keep My Versions backs up the disk version, whose slide it drops")
            check(backups(for: keepMineReplaced, url).filter {
                      (try? String(contentsOf: $0, encoding: .utf8)) == agentLost }.count == 1,
                  "merge save: the dropped disk version is backed up once, not once per save step")

            // The disk reverts to bytes Presto itself wrote earlier: the write
            // boundary skips those as Presto's own, but they still lose a slide.
            try original.write(to: url, atomically: true, encoding: .utf8)
            let reverted = mergeFixture(url, original)
            _ = reverted.handleEditorChanged(html: "<h1>Two as Presto saved it</h1>", index: 1)
            _ = reverted.flushPendingDeckWrite()
            let prestoWrote = try String(contentsOf: url, encoding: .utf8)
            let agentEdit = deckHTML(["One", "Two as the agent wrote it", "Three"])
            try agentEdit.write(to: url, atomically: true, encoding: .utf8)
            reverted.applyDiskReload(agentEdit)
            _ = reverted.handleEditorChanged(html: "<h1>Two edited again</h1>", index: 1)
            try prestoWrote.write(to: url, atomically: true, encoding: .utf8)
            reverted.nextChoice = .keepMine
            _ = reverted.flushPendingDeckWrite()
            pump(0.2)
            reverted.stopFileWatcher()
            check(slideTitles(reverted, try String(contentsOf: url, encoding: .utf8)) == ["One", "Two edited again", "Three"] &&
                  backedUp(reverted, prestoWrote),
                  "merge save: Keep My Versions backs up a disk version even when Presto wrote those bytes earlier")

            // A backup that cannot be written stops the choice; nothing is lost.
            let blocker = directory.appendingPathComponent("not-a-directory")
            try "file".write(to: blocker, atomically: true, encoding: .utf8)
            for choice in [ExternalMergeChoice.reloadFromDisk, .useDisk, .keepMine] {
                try original.write(to: url, atomically: true, encoding: .utf8)
                let blocked = mergeFixture(url, original)
                blocked.backupRoot = blocker.appendingPathComponent("backups", isDirectory: true)
                _ = blocked.handleEditorChanged(html: "<h1>Two mine, unsaved</h1>", index: 1)
                let unsavedMine = blocked.fileContent
                let agentDisk = deckHTML(["One", "Two agent", "Three"])
                try agentDisk.write(to: url, atomically: true, encoding: .utf8)
                blocked.nextChoice = choice
                _ = blocked.flushPendingDeckWrite()
                pump(0.2)
                blocked.stopFileWatcher()
                let blockedDisk = try String(contentsOf: url, encoding: .utf8)
                check(blocked.fileContent == unsavedMine && blocked.deckWriteDirty &&
                      !blocked.isPresentingExternalConflict && blockedDisk == agentDisk,
                      "merge save: \(choice.buttonTitle) stops when its backup fails, keeping both versions")
                check(blocked.saveErrors.contains { $0.contains("Could not back up") },
                      "merge save: a failed \(choice.buttonTitle) backup is reported to the user")
            }

            // Backup names are UTC, so the autumn clock change (01:59 back to
            // 01:00 local) cannot make a newer backup sort before an older one.
            let clockDeck = directory.appendingPathComponent("clock-change.html")
            let savedZone = NSTimeZone.default
            NSTimeZone.default = TimeZone(identifier: "Europe/Lisbon")!
            let beforeChange = Date(timeIntervalSince1970: 1_792_888_200)  // 2026-10-25 00:30 UTC, 01:30 in Lisbon
            try saver.backupDeckVersion("before the clock change", of: clockDeck, now: beforeChange)
            try saver.backupDeckVersion("after the clock change", of: clockDeck, now: beforeChange.addingTimeInterval(40 * 60))
            NSTimeZone.default = savedZone
            let newestAfterChange = backups(for: saver, clockDeck).last.flatMap { try? String(contentsOf: $0, encoding: .utf8) }
            check(newestAfterChange == "after the clock change",
                  "merge save: backup names sort by age across the autumn clock change (newest \(newestAfterChange ?? "none"))")

            // DateFormatter rounds to the millisecond, so 0.9996 s formats as
            // the next second; the name's microseconds must agree with it.
            let edgeDeck = directory.appendingPathComponent("second-edge.html")
            try saver.backupDeckVersion("just before the second", of: edgeDeck,
                                        now: Date(timeIntervalSince1970: 1_792_888_200.9996))
            try saver.backupDeckVersion("just after the second", of: edgeDeck,
                                        now: Date(timeIntervalSince1970: 1_792_888_201.0001))
            let newestAtEdge = backups(for: saver, edgeDeck).last.flatMap { try? String(contentsOf: $0, encoding: .utf8) }
            check(newestAtEdge == "just after the second",
                  "merge save: backup names sort by age across a second boundary (newest \(newestAtEdge ?? "none"))")

            let retention = AppDelegate.deckBackupRetention
            for n in 0..<(retention + 3) {
                try saver.backupDeckVersion("version \(n)", of: url)
            }
            let kept = backups(for: saver, url)
            let newest = kept.last.flatMap { try? String(contentsOf: $0, encoding: .utf8) } ?? "none"
            check(kept.count == retention && newest == "version \(retention + 2)",
                  "merge save: backups keep the newest \(retention) per deck (kept \(kept.count), newest \"\(newest)\")")
        } catch {
            check(false, "merge save setup: \(error)")
        }
    }

    // Native PDF export must wait for WS.ready (external tables, KaTeX, fonts,
    // the final table fit), not just for the slide list.
    static func exportReadinessTests() {
        let app = ExportApp()
        let page = JSDeckView().setUp()
        app.exportWebView = page
        app.exportGeneration = 7
        app.startExportCapture(webView: page, generation: 7)
        pump(0.35)
        check(app.prepared == 0 && app.finished.isEmpty,
              "export: a deck with slides but WS.ready pending is not captured yet")
        page.run("finishLoading()")
        pump(0.25)
        check(app.prepared == 1 && app.finished.isEmpty, "export: capture starts once the deck reports WS.ready")
        app.exportWebView = nil

        let legacy = ExportApp()
        let legacyPage = JSDeckView().setUp()
        legacyPage.run("delete WS.ready;")
        legacy.exportWebView = legacyPage
        legacy.exportGeneration = 1
        legacy.startExportCapture(webView: legacyPage, generation: 1)
        check(legacy.prepared == 1, "export: an engine without WS.ready is captured once its slides exist")
        legacy.exportWebView = nil

        // The engine bounds each outside wait (KaTeX, fonts, images) at 5 s,
        // so a slow deck can take longer than that in total.
        let slow = ExportApp()
        let slowPage = JSDeckView().setUp()
        slow.exportWebView = slowPage
        slow.exportGeneration = 1
        slow.exportPollCount = 60
        slow.startExportCapture(webView: slowPage, generation: 1)
        check(slow.finished.isEmpty, "export: a deck still settling after 6 s of polling is not abandoned")
        slow.exportPollCount = 100_000
        pump(0.15)
        check(slow.finished == ["Timed out waiting for deck to load"] && slow.prepared == 0,
              "export: a deck that never becomes ready still times out")
        check(page.scriptErrors.isEmpty && legacyPage.scriptErrors.isEmpty && slowPage.scriptErrors.isEmpty,
              "export: the readiness check runs without script errors")
    }

    // First paint does not wait for WS.ready, but a step that only exists once
    // external tables load must be restored when the deck is ready.
    static func previewReadinessTests() {
        let app = StubbedUIApp()
        let page = JSDeckView().setUp()
        page.run(app.previewCoreJS(slideIndex: 2, slideStep: 3))
        check(page.number("WS.currentIndex") == 2 && page.number("WS.currentStep") == 1 &&
              page.number("frames.length") == 0,
              "edit preview: first paint shows the slide at once, with the steps known so far")
        page.run("finishLoading()")
        page.run("var lastSlideChanged = posted.filter(function (m) { return m.type === 'slideChanged'; }).pop();")
        check(page.number("WS.currentStep") == 3 && page.number("lastSlideChanged.step") == 3,
              "edit preview: a step inside a loaded table is restored when WS.ready resolves, and reported to Presto")
        check(page.run("posted[posted.length - 1].type")?.toString() == "prestoPreviewSettled",
              "edit preview: Presto hears when the deck has settled (for its Decks thumbnail)")
        page.run("window.__prestoStartEditChrome();")
        check(page.scriptErrors.isEmpty, "edit preview: injection runs without script errors, edit chrome or not \(page.scriptErrors)")

        // The edit chrome goes in once the visual editor has loaded (the
        // last injected script starts it), with Presto's saved settings,
        // and never the engine's retired rail.
        let chrome = JSDeckView().setUp()
        chrome.run("""
            var chromeCalls = [], layouts = [];
            window.installPrestoEditSidebar = function () { chromeCalls.push('engine-rail'); };
            window.PrestoEditChrome = {
                install: function (opts) { chromeCalls.push('install:' + opts.railVisible + ':' + opts.ghostFuture); },
                rebuild: function () { chromeCalls.push('rebuild'); }
            };
            window.PrestoLayout = { apply: function (opts) { layouts.push(opts); return {}; } };
            """)
        app.railVisible = false
        app.ghostFutureSteps = true
        chrome.run(app.previewCoreJS(slideIndex: 0, slideStep: 0))
        check(chrome.run("chromeCalls.length")?.toInt32() == 0,
              "edit preview: the edit chrome waits for the visual editor")
        chrome.run("window.__prestoStartEditChrome(); window.__prestoStartEditChrome();")
        chrome.run("finishLoading()")
        check(chrome.run("chromeCalls.join(',')")?.toString() == "install:false:true",
              "edit preview: the edit chrome is installed once, with the saved rail and faint-steps settings")
        check(chrome.run("layouts.length > 0 && layouts[0].mode === 'edit' && layouts[0].insetLeft === 0 && layouts[0].insetBottom === 72 && layouts[0].padding === 28")?.toBool() == true,
              "edit preview: PrestoLayout.apply gets the edit insets (no rail: 0 left; step strip 72; padding 28)")
        app.railVisible = true
        check(app.editLayoutOptionsJS().contains("insetLeft: 232"),
              "edit preview: a visible rail insets the canvas by its 232 px")
        check(chrome.scriptErrors.isEmpty, "edit preview: chrome install runs without script errors \(chrome.scriptErrors)")

        let moved = JSDeckView().setUp()
        moved.run(app.previewCoreJS(slideIndex: 2, slideStep: 3))
        moved.run("WS.goToSlide(0, 0);")
        moved.run("finishLoading()")
        check(moved.number("WS.currentIndex") == 0,
              "edit preview: a slide change before WS.ready is not undone by the step restore")

        let ready = JSDeckView().setUp()
        ready.run("finishLoading()")
        ready.run(app.previewCoreJS(slideIndex: 2, slideStep: 3))
        let navigations = ready.number("remoteFlags.length")
        ready.run("Promise.resolve().then(function () {});")
        check(ready.number("WS.currentStep") == 3 && navigations == 1 && ready.number("remoteFlags.length") == 1,
              "edit preview: a deck that is already ready is navigated once")
        check(moved.scriptErrors.isEmpty && ready.scriptErrors.isEmpty,
              "edit preview: injection runs without script errors on every path")

        // A reloaded audience window goes back to the talk's slide and step.
        let presenting = StubbedUIApp()
        presenting.lastAudienceSlideIndex = 2
        presenting.lastAudienceStep = 3
        presenting.lastAudienceReturnStack = [["index": 1, "step": 0]]
        let audience = JSDeckView().setUp()
        audience.run(presenting.audienceStateRestoreJS())
        check(audience.number("WS.currentStep") == 1 && audience.number("WS.returnStack.length") == 1 &&
              audience.run("remoteFlags.join(',')")?.toString() == "true",
              "audience restore: first paint goes to the slide as Presto's own (unreported) move")
        audience.run("finishLoading()")
        check(audience.number("WS.currentStep") == 3 && audience.run("remoteFlags.join(',')")?.toString() == "true,false",
              "audience restore: the table step is restored at WS.ready and reported so the presenter view follows")
        check(audience.scriptErrors.isEmpty, "audience restore: runs without script errors \(audience.scriptErrors)")
    }

    static func presentationTests() {
        let solo = PresentApp()
        let start = Date()
        solo.requestPresentationExitWithConfirmation(now: start)
        check(solo.endRequests == 0 && solo.hints == ["Press Escape again to end the presentation"],
              "present: the first Escape outside the appendix only shows a hint")
        solo.requestPresentationExitWithConfirmation(now: start.addingTimeInterval(1.2))
        check(solo.endRequests == 1, "present: a second Escape within 2 seconds ends the talk")
        solo.requestPresentationExitWithConfirmation(now: start.addingTimeInterval(10))
        solo.requestPresentationExitWithConfirmation(now: start.addingTimeInterval(12.5))
        check(solo.endRequests == 1 && solo.hints.count == 3,
              "present: Escapes more than 2 seconds apart never end the talk")

        let appendix = PresentApp()
        let audience = AudienceView()
        audience.returnStackDepth = 1
        appendix.audienceWebView = audience
        appendix.lastPresentationExitEscape = Date()
        appendix.handlePresentationEscape()
        check(appendix.audienceCommands == ["WS.returnFromAppendix()"] && appendix.endRequests == 0,
              "present: Escape in the appendix goes back with a single press")
        audience.returnStackDepth = 0
        appendix.handlePresentationEscape()
        check(appendix.endRequests == 0 && appendix.hints.count == 1,
              "present: going back resets the exit guard, so the next Escape only arms it")

        let ready = PresentApp()
        ready.handlePresenterNavigated(["index": 5, "step": 1, "returnStack": []])
        check(ready.audienceCommands.isEmpty && ready.lastAudienceSlideIndex == 5 && ready.lastAudienceStep == 1,
              "present: before Start, moving the console's preview sets where the talk begins")

        let driven = PresentApp()
        driven.audienceWebView = AudienceView()
        driven.handlePresenterNavigated(["index": 21, "step": 0,
                                         "returnStack": [["index": 4, "step": 2]]])
        check(driven.audienceCommands.count == 1 &&
              driven.audienceCommands[0].hasSuffix("WS.goToSlide(21, 0)") &&
              driven.audienceCommands[0].contains("\"index\":4") &&
              driven.audienceCommands[0].contains("\"step\":2"),
              "present: an appendix jump in the presenter view moves the audience with its return stack")
        driven.lastAudienceSlideIndex = 21
        driven.lastAudienceStep = 0
        driven.lastAudienceReturnStack = [["index": 4, "step": 2]]
        driven.handlePresenterNavigated(["index": 21, "step": 0,
                                         "returnStack": [["index": 4, "step": 2]]])
        check(driven.audienceCommands.count == 1,
              "present: the presenter view echoing the audience state does not loop")
        driven.handlePresenterNavigated(["index": -1, "step": 0])
        driven.handlePresenterNavigated(["index": 3, "step": 0, "returnStack": [["index": "x"]]])
        check(driven.audienceCommands.count == 1, "present: malformed presenter navigation is ignored")
    }

    static func waitFor(_ condition: () -> Bool, timeout: TimeInterval = 2) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        return condition()
    }

    static func testFileWatcher() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("presto-watch-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { _ = try? FileManager.default.trashItem(at: directory, resultingItemURL: nil) }
        let url = directory.appendingPathComponent("index.html")
        let original = fixture().fileContent!
        try original.write(to: url, atomically: true, encoding: .utf8)
        let app = WatchedApp()
        app.isEditorMode = true
        app.currentDeckURL = url
        app.fileContent = original
        app.lastPersistedContent = original
        app.sectionInfos = app.extractSections(from: original)
        app.currentEditorSlideIndex = 1
        app.currentEditorStep = 2
        app.startFileWatcher()
        defer { app.stopFileWatcher() }

        let first = original.replacingOccurrences(of: "Original", with: "First agent edit")
        try first.write(to: url, atomically: false, encoding: .utf8)
        check(waitFor { app.fileContent == first }, "in-place agent edits refresh automatically")
        let second = original.replacingOccurrences(of: "Original", with: "Atomic agent edit")
        try second.write(to: url, atomically: true, encoding: .utf8)
        check(waitFor { app.fileContent == second }, "atomic agent saves refresh automatically")
        check(app.currentEditorSlideIndex == 1 && app.currentEditorStep == 2 && app.refreshed.last == 1,
              "external edits preserve the editor slide and reveal step")

        // A replacement can keep identical bytes but still detach the watched inode.
        try second.write(to: url, atomically: true, encoding: .utf8)
        RunLoop.current.run(until: Date().addingTimeInterval(0.6))
        let third = original.replacingOccurrences(of: "Original", with: "After identical replacement")
        try third.write(to: url, atomically: false, encoding: .utf8)
        check(waitFor { app.fileContent == third }, "watching continues after identical atomic replacement")

        // Recovery must also work after the deck is absent longer than the retries.
        let movedURL = directory.appendingPathComponent("old.html")
        try FileManager.default.moveItem(at: url, to: movedURL)
        RunLoop.current.run(until: Date().addingTimeInterval(3.6))
        let fourth = original.replacingOccurrences(of: "Original", with: "Recreated agent deck")
        try fourth.write(to: url, atomically: true, encoding: .utf8)
        check(waitFor { app.fileContent == fourth }, "watching recovers when a missing deck is recreated")

        try "<html>incomplete write".write(to: url, atomically: true, encoding: .utf8)
        check(waitFor { !app.reloadErrors.isEmpty }, "incomplete external decks are rejected")
        check(app.fileContent == fourth, "an incomplete save keeps the last valid deck")
        try original.write(to: url, atomically: true, encoding: .utf8)
        check(waitFor { app.fileContent == original }, "the completed deck refreshes after an incomplete save")

        app.sourceEditorDirty = true
        try first.write(to: url, atomically: true, encoding: .utf8)
        check(waitFor { !app.conflicts.isEmpty }, "external edits surface a conflict for unsaved local typing")
        check(app.fileContent == original, "automatic refresh preserves unsaved local edits")
    }

    static func javaScript(_ script: String, in view: WKWebView) -> Any? {
        var finished = false
        var result: Any?
        view.evaluateJavaScript(script) { value, _ in
            result = value
            finished = true
        }
        _ = waitFor({ finished }, timeout: 5)
        return result
    }

    static func testRenderedRefresh() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("presto-rendered-refresh-\(UUID().uuidString)")
        let deckDirectory = directory.appendingPathComponent("deck")
        try FileManager.default.createDirectory(at: deckDirectory, withIntermediateDirectories: true)
        defer { _ = try? FileManager.default.trashItem(at: directory, resultingItemURL: nil) }
        let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let sharedRoot = directory.appendingPathComponent("shared")
        try FileManager.default.createDirectory(at: sharedRoot, withIntermediateDirectories: true)
        try FileManager.default.copyItem(at: root.appendingPathComponent("engine"),
                                         to: sharedRoot.appendingPathComponent("engine"))
        try FileManager.default.createSymbolicLink(at: sharedRoot.appendingPathComponent("presto"),
                                                  withDestinationURL: root.appendingPathComponent("presto"))
        try FileManager.default.createSymbolicLink(at: directory.appendingPathComponent("engine"),
                                                  withDestinationURL: sharedRoot.appendingPathComponent("engine"))
        let figures = deckDirectory.appendingPathComponent("figures")
        try FileManager.default.createDirectory(at: figures, withIntermediateDirectories: true)
        let figureURL = figures.appendingPathComponent("chart.svg")
        let styleURL = sharedRoot.appendingPathComponent("engine/refresh-test.css")
        let scriptURL = deckDirectory.appendingPathComponent("refresh-test.js")
        func figure(_ text: String) -> String {
            "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 50\"><text id=\"asset-label\" y=\"25\">\(text)</text></svg>"
        }
        let url = deckDirectory.appendingPathComponent("index.html")
        let original = try String(contentsOf: root.appendingPathComponent("tests/wysiwyg-e2e-deck.html"),
                                  encoding: .utf8)
            .replacingOccurrences(of: "</head>", with: "<link rel=\"stylesheet\" href=\"../engine/refresh-test.css\"><script src=\"refresh-test.js\"></script></head>")
            .replacingOccurrences(of: "<h2 id=\"e2e-heading\">", with: "<img data-inline src=\"figures/chart.svg\"><h2 id=\"e2e-heading\">")

        for editMode in [true, false] {
            try figure("Before").write(to: figureURL, atomically: true, encoding: .utf8)
            try ":root { --refresh-test: before; }".write(to: styleURL, atomically: true, encoding: .utf8)
            try "window.assetVersion = 'before';".write(to: scriptURL, atomically: true, encoding: .utf8)
            try original.write(to: url, atomically: true, encoding: .utf8)
            let app = StubbedUIApp()
            app.currentDeckURL = url
            app.isEditorMode = editMode
            app.fileContent = original
            app.lastPersistedContent = original
            app.sectionInfos = app.extractSections(from: original)
            let config = app.makeBaseConfig()
            config.userContentController.add(app, name: "presto")
            let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 720), configuration: config)
            view.navigationDelegate = app
            if editMode { app.previewWebView = view }
            else { app.audienceWebView = view }
            view.loadFileURL(url, allowingReadAccessTo: app.findReadAccessRoot(deckURL: url))
            app.startFileWatcher()
            defer { _ = app.cleanupPresentation() }
            let mode = editMode ? "editor" : "presentation"
            check(waitFor({
                javaScript(editMode ? "!!window.__prestoWysiwyg" : "document.body.classList.contains('ws-mode-audience')",
                           in: view) as? Bool == true
            }, timeout: 10), "real WKWebView loads in \(mode) mode")
            check(javaScript("document.querySelector('[id$=asset-label]')?.textContent === 'Before'", in: view) as? Bool == true,
                  "\(mode) loads the external SVG fixture")
            if editMode {
                _ = javaScript("WS.goToSlide(1, 1)", in: view)
                check(waitFor { app.currentEditorSlideIndex == 1 && app.currentEditorStep == 1 },
                      "editor navigation reaches native state before refresh")
            } else {
                app.navigateAudience(command: "WS.goToSlide(1, 1)")
                check(waitFor { app.lastAudienceSlideIndex == 1 && app.lastAudienceStep == 1 },
                      "single-screen presentation remembers navigation without a presenter window")
            }

            for atomic in [false, true] {
                // Add the second-screen presenter after checking single-screen
                // refresh, without creating or foregrounding any native windows.
                if !editMode && atomic {
                    let presenterConfig = app.makeBaseConfig()
                    presenterConfig.userContentController.add(app, name: "presto")
                    let presenter = WKWebView(frame: view.frame, configuration: presenterConfig)
                    presenter.navigationDelegate = app
                    app.consoleWebView = presenter
                    presenter.loadFileURL(root.appendingPathComponent("presto/presenter.html"),
                                          allowingReadAccessTo: app.commonAncestor([root, directory]))
                    check(waitFor({ app.presenterReady }, timeout: 10), "second-screen presenter becomes ready")
                    _ = javaScript("document.getElementById('timer-btn').click(); window.refreshSentinel = 'retained'", in: presenter)
                }
                let heading = "Agent update \(mode) \(atomic)"
                let updated = original.replacingOccurrences(of: "Editable Heading", with: heading)
                try updated.write(to: url, atomically: atomic, encoding: .utf8)
                let expected = app.jsStringLiteral(heading)
                check(waitFor({
                    javaScript("document.getElementById('e2e-heading').textContent === \(expected) && WS.currentIndex === 1 && WS.currentStep === 1",
                               in: view) as? Bool == true
                }, timeout: 10), "\(mode) renders \(atomic ? "atomic" : "in-place") agent updates at the same slide and step")
                if !editMode, let presenter = app.consoleWebView {
                    check(waitFor({
                        javaScript("(function () { var w = document.getElementById('current-slide').contentWindow; return w.document.getElementById('e2e-heading').textContent === \(expected) && w.WS.currentIndex === 1 && w.WS.currentStep === 1; })()",
                                   in: presenter) as? Bool == true
                    }, timeout: 10), "second-screen presenter displays the updated deck in sync")
                    check(javaScript("window.refreshSentinel === 'retained' && document.getElementById('timer-btn').textContent === 'Pause'",
                                     in: presenter) as? Bool == true,
                          "automatic refresh leaves the presenter page and running timer intact")
                }
            }

            let savedHTML = try String(contentsOf: url, encoding: .utf8)
            try figure("After in-place").write(to: figureURL, atomically: false, encoding: .utf8)
            check(waitFor({
                javaScript("document.querySelector('[id$=asset-label]')?.textContent === 'After in-place' && WS.currentIndex === 1 && WS.currentStep === 1", in: view) as? Bool == true
            }, timeout: 4), "\(mode) refreshes an in-place SVG edit without an HTML change")

            if editMode {
                app.wysiwygTextEditingActive = true
                _ = javaScript("window.assetRefreshSentinel = 'retained'", in: view)
            }
            try figure("After atomic").write(to: figureURL, atomically: true, encoding: .utf8)
            try ":root { --refresh-test: after; }".write(to: styleURL, atomically: true, encoding: .utf8)
            try "window.assetVersion = 'after';".write(to: scriptURL, atomically: false, encoding: .utf8)
            if editMode {
                RunLoop.current.run(until: Date().addingTimeInterval(1.8))
                check(javaScript("window.assetRefreshSentinel === 'retained'", in: view) as? Bool == true,
                      "asset refresh preserves a live text edit")
                app.wysiwygTextEditingActive = false
            }
            let assetCheck = "document.querySelector('[id$=asset-label]')?.textContent === 'After atomic' && getComputedStyle(document.documentElement).getPropertyValue('--refresh-test').trim() === 'after' && window.assetVersion === 'after' && WS.currentIndex === 1 && WS.currentStep === 1"
            let assetsUpdated = waitFor({ javaScript(assetCheck, in: view) as? Bool == true }, timeout: 4)
            if !assetsUpdated {
                print("Asset state in \(mode): \(String(describing: javaScript("JSON.stringify({figure: document.querySelector('[id$=asset-label]')?.textContent, style: getComputedStyle(document.documentElement).getPropertyValue('--refresh-test'), script: window.assetVersion, slide: WS.currentIndex, step: WS.currentStep})", in: view)))")
            }
            check(assetsUpdated,
                  "\(mode) refreshes atomic SVG, symlinked engine CSS, and JavaScript changes")
            check(try String(contentsOf: url, encoding: .utf8) == savedHTML,
                  "asset refresh does not rewrite the deck HTML")
            if !editMode, let presenter = app.consoleWebView {
                check(waitFor({
                    javaScript("document.getElementById('current-slide').contentWindow.eval(\(app.jsStringLiteral(assetCheck)))", in: presenter) as? Bool == true
                }, timeout: 4), "second-screen presenter refreshes linked figures, CSS, and scripts")
                check(javaScript("window.refreshSentinel === 'retained' && document.getElementById('timer-btn').textContent === 'Pause'", in: presenter) as? Bool == true,
                      "asset refresh preserves the running presenter timer")
            }
            _ = javaScript("window.assetRefreshSentinel = 'retained'", in: view)
            try "Export output".write(to: deckDirectory.appendingPathComponent("deck.pdf"), atomically: true, encoding: .utf8)
            RunLoop.current.run(until: Date().addingTimeInterval(1.8))
            check(javaScript("window.assetRefreshSentinel === 'retained'", in: view) as? Bool == true,
                  "\(mode) ignores unrelated PDF exports")
        }
    }

    static func main() {
        _ = NSApplication.shared
        // A modal alert or stuck run loop must fail the suite, not hang it.
        DispatchQueue.global().asyncAfter(deadline: .now() + 120) {
            print("FAIL: transaction tests timed out (modal UI or stuck run loop)")
            exit(2)
        }
        let app = fixture()
        check(app.handleEditorChanged(html: "<h1>Edited</h1>", index: 0),
              "valid editor changes enter the save queue")
        app.failWrites = true
        app.flushPendingDeckWrite()
        check(app.deckWriteDirty, "failed disk writes remain dirty")
        check(!app.saveErrors.isEmpty, "failed disk writes are visible")
        app.failWrites = false
        app.flushPendingDeckWrite()
        check(app.writes.last?.1.contains("<h1>Edited</h1>") == true,
              "retry saves the retained edit")
        check(!app.deckWriteDirty, "successful retry clears dirty state")

        let failedClose = fixture()
        _ = failedClose.handleEditorChanged(html: "<h1>Retain me</h1>", index: 0)
        failedClose.failWrites = true
        let closeGeneration = failedClose.sessionGeneration
        check(!failedClose.cleanupPresentation(), "failed save refuses teardown")
        check(failedClose.fileContent?.contains("Retain me") == true &&
              failedClose.sessionGeneration == closeGeneration,
              "failed teardown retains the session and content")
        failedClose.cancelPendingDeckWrite()

        let switched = fixture()
        let oldURL = switched.currentDeckURL!
        let newDeckDirectory = try! tempDeckDirectory("switch-test")
        defer { _ = try? FileManager.default.trashItem(at: newDeckDirectory, resultingItemURL: nil) }
        let newURL = newDeckDirectory.appendingPathComponent("index.html")
        try! deckHTML(["New deck"]).write(to: newURL, atomically: true, encoding: .utf8)
        let visual = ScriptView()
        let source = ScriptView()
        source.pending = #"{"dirty":true,"index":0,"html":"<h1>Last typed source</h1>"}"#
        switched.previewWebView = visual
        switched.editorWebView = source
        switched.editorReady = true
        switched.startPresentation(deckURL: newURL)
        check(switched.writes.last?.0 == oldURL &&
              switched.writes.last?.1.contains("Last typed source") == true,
              "deck switch flushes the source buffer to the original deck")
        check(switched.openedDeck == newURL, "deck switch proceeds after saving")

        let refusedSwitch = fixture()
        let badPreview = ScriptView()
        badPreview.scriptError = NSError(domain: "TestWebKit", code: 1)
        refusedSwitch.previewWebView = badPreview
        refusedSwitch.startPresentation(deckURL: newURL)
        check(refusedSwitch.openedDeck == nil && refusedSwitch.currentDeckURL != newURL,
              "WebKit flush failure cancels deck switching")

        let liveSwitch = fixture()
        liveSwitch.presentPhase = .live
        liveSwitch.startEditor(deckURL: newURL)
        check(liveSwitch.openedDeck == nil && liveSwitch.currentDeckURL == oldURL,
              "a live presentation is never replaced by opening another deck")
        liveSwitch.presentPhase = .none

        let export = fixture()
        _ = export.handleEditorChanged(html: "<h1>Not on disk</h1>", index: 0)
        export.failWrites = true
        var exported = true
        export.prepareEditorForPDFExport { exported = $0 }
        check(!exported && export.deckWriteDirty, "PDF export stops when saving fails")
        export.cancelPendingDeckWrite()

        for badBody in [
            "</section><script>bad()</script><section class=\"slide\">",
            "<div class=\"ws-deck\"><section class=\"slide\">nested</section></div>"
        ] {
            let app = fixture()
            let before = app.fileContent
            check(!app.handleEditorChanged(html: badBody, index: 0),
                  "source edits cannot escape the owning slide")
            check(app.fileContent == before, "invalid source leaves the deck intact")
            app.cancelPendingDeckWrite()
        }

        for output in [
            "<section class=\"slide\">A</section><section class=\"slide\">B</section>",
            "<sectional>Bad</section>",
            "<section>No slide class</section>",
            "<section class=\"slide\"><div class=\"ws-deck\">nested</div></section>"
        ] {
            let app = fixture()
            let before = app.fileContent
            app.applyRefineResult(slideIndex: 0, taskID: UUID(), instructions: "Improve",
                                  preHTML: app.sectionInfos[0].content, refinedRaw: output)
            check(app.writes.isEmpty && app.fileContent == before,
                  "invalid AI output cannot change slide structure")
        }

        let unchanged = fixture()
        check(unchanged.handleEditorChanged(html: "<h1>Original</h1>", index: 0) &&
              unchanged.refreshed.isEmpty && unchanged.recordedChanges.isEmpty,
              "a source commit that changes nothing leaves the preview slide and its undo history alone")
        let samePending = unchanged.applyPendingEditorChangePayload(#"{"dirty":true,"index":0,"html":"<h1>Original</h1>"}"#)
        check(samePending.ok && samePending.savedIndex == nil && samePending.change == nil,
              "an unchanged pending source buffer asks for no slide patch")
        let changedPending = unchanged.applyPendingEditorChangePayload(#"{"dirty":true,"index":0,"html":"<h1>Typed</h1>"}"#)
        check(changedPending.savedIndex == 0 && changedPending.change?.origin == "source",
              "a changed pending source buffer is patched and recorded")
        unchanged.cancelPendingDeckWrite()

        let refined = fixture()
        let before = refined.sectionInfos[1].content
        refined.applyRefineResult(slideIndex: 1, taskID: UUID(), instructions: "Improve",
                                  preHTML: before,
                                  refinedRaw: "<section class=\"slide\"><h1>Refined</h1></section>")
        check(refined.refreshed.contains(1), "background refine updates the offscreen preview slide")
        check(refined.recordedChanges[1] == RecordedSlideChange(origin: "refine", beforeHTML: before, prompt: "Improve"),
              "a Refine result is recorded in the unified undo history with the slide it replaced and its prompt")
        let afterRefine = refined.sectionInfos[1].content
        check(refined.handleEditorChanged(html: "<h1>Manual edit after AI</h1>", index: 1),
              "manual edit after refine is accepted")
        check(refined.recordedChanges[1]?.origin == "source" && refined.recordedChanges[1]?.beforeHTML == afterRefine,
              "a committed source edit is recorded too, so one ⌘Z covers it")
        let manual = refined.fileContent
        // The history replays the Refine's undo, but the slide has changed since.
        check(!refined.applyHistorySection(index: 1, sectionHTML: before, expected: afterRefine, saveId: 9) &&
              refined.fileContent == manual,
              "AI undo cannot overwrite later manual edits (the history's version check)")
        refined.cancelPendingDeckWrite()

        let redo = fixture()
        let redoBefore = redo.sectionInfos[0].content
        let refinedSection = "<section class=\"slide ai\"><h1>AI</h1></section>"
        redo.applyRefineResult(slideIndex: 0, taskID: UUID(), instructions: "Improve",
                               preHTML: redoBefore, refinedRaw: refinedSection)
        check(redo.applyHistorySection(index: 0, sectionHTML: redoBefore, expected: refinedSection, saveId: 1) &&
              redo.sectionInfos[0].content == redoBefore,
              "AI undo restores the whole section, opening tag included")
        check(redo.applyHistorySection(index: 0, sectionHTML: refinedSection, expected: redoBefore, saveId: 2) &&
              redo.sectionInfos[0].content == refinedSection,
              "AI redo reinstates the refined section")
        _ = redo.handleEditorChanged(html: "<h1>Manual after undo</h1>", index: 0)
        let redoManual = redo.fileContent
        check(!redo.applyHistorySection(index: 0, sectionHTML: redoBefore, expected: refinedSection, saveId: 3) &&
              redo.fileContent == redoManual,
              "AI redo preserves edits made after undo")
        check(!redo.applyHistorySection(index: 0, sectionHTML: "<section class=\"slide\">A</section><section class=\"slide\">B</section>",
                                        expected: nil, saveId: 4) && redo.fileContent == redoManual,
              "a history change cannot alter slide boundaries")
        redo.cancelPendingDeckWrite()

        let inProgress = fixture()
        inProgress.sourceEditorDirty = true
        let sourceBefore = inProgress.fileContent
        inProgress.applyRefineResult(slideIndex: 0, taskID: UUID(), instructions: "Improve",
                                     preHTML: inProgress.sectionInfos[0].content,
                                     refinedRaw: "<section class=\"slide\">AI</section>")
        check(inProgress.fileContent == sourceBefore,
              "AI result cannot replace uncommitted source typing")

        let watched = fixture()
        watched.saveTransactionCount = 1
        watched.handleFileChanged(eventMask: .write)
        RunLoop.current.run(until: Date().addingTimeInterval(0.4))
        check(watched.diskReloads == 1, "watcher checks external writes during the old suppression window")

        do {
            try testFileWatcher()
            try testRenderedRefresh()
            let directory = FileManager.default.temporaryDirectory
                .appendingPathComponent("presto-save-test-\(UUID().uuidString)")
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { _ = try? FileManager.default.trashItem(at: directory, resultingItemURL: nil) }
            let url = directory.appendingPathComponent("index.html")
            try "Original".write(to: url, atomically: true, encoding: .utf8)
            let diskApp = DiskApp()
            diskApp.currentDeckURL = url
            diskApp.lastPersistedContent = "Original"
            check(diskApp.writeDeckContent("Local", to: url,
                                           successMessage: "Test save", failurePrefix: "Test failed") == nil,
                  "disk save succeeds against the loaded baseline")
            try "External".write(to: url, atomically: true, encoding: .utf8)
            check(diskApp.writeDeckContent("Another local edit", to: url,
                                           successMessage: "Test save", failurePrefix: "Test failed") != nil,
                  "write boundary detects an external edit before watcher delivery")
            let disk = try String(contentsOf: url, encoding: .utf8)
            check(disk == "External" && diskApp.conflict == "External",
                  "conflicting external bytes are preserved for the user's decision")
        } catch {
            check(false, "disk regression setup: \(error)")
        }

        fileWatcherTests()
        mergeTests()
        mergeSaveTests()
        exportReadinessTests()
        previewReadinessTests()
        presentationTests()
        ShellModeTests.run()
        _ = try? FileManager.default.trashItem(at: testBackupRoot, resultingItemURL: nil)

        print("TRANSACTION_TESTS: \(passed) passed, \(failed) failed")
        exit(failed == 0 ? 0 : 1)
    }
}

import CoreGraphics
import Darwin
import Foundation
import ImageIO

// Standalone tests for presto/DeckLibrary.swift. Compile with:
//   swiftc -warnings-as-errors presto/DeckLibrary.swift tests/test_deck_library.swift -o /tmp/test_deck_library
// Everything runs in a fresh temporary directory with an injected
// UserDefaults suite, so Presto's real Application Support folder and
// defaults are never touched. Set DECK_LIBRARY_REAL_REGISTRY=0 to skip the
// read-only check against the real deck_locations.yaml.

@main
struct DeckLibraryTests {
    static var passed = 0
    static var failed = 0
    static var root = URL(fileURLWithPath: "/nonexistent")
    static var suites: [String] = []

    static func expect(_ condition: @autoclosure () -> Bool, _ label: String,
                       file: StaticString = #file, line: UInt = #line) {
        if condition() {
            passed += 1
        } else {
            failed += 1
            print("FAIL: \(label) (line \(line))")
        }
    }

    // MARK: Fixtures

    static func dir(_ relative: String) -> URL {
        let url = root.appendingPathComponent(relative, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    @discardableResult
    static func write(_ text: String, to url: URL, modified: Date? = nil) -> URL {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? Data(text.utf8).write(to: url)
        if let modified {
            try? FileManager.default.setAttributes([.modificationDate: modified], ofItemAtPath: url.path)
        }
        return url
    }

    static func deckHTML(title: String?, slides: [String]) -> String {
        let titleTag = title.map { "<title>\($0)</title>" } ?? ""
        return """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            \(titleTag)
            <link rel="stylesheet" href="../engine/webslides.css">
        </head>
        <body>
        <div class="ws-deck">
        \(slides.joined(separator: "\n"))
        </div>
        </body>
        </html>
        """
    }

    @discardableResult
    static func makeDeck(_ relative: String, title: String?, slides: [String] = ["<section class=\"slide title\"><h1>T</h1></section>"],
                         modified: Date? = nil) -> URL {
        let folder = dir(relative)
        return write(deckHTML(title: title, slides: slides), to: folder.appendingPathComponent("index.html"), modified: modified)
    }

    static func freshDefaults() -> UserDefaults {
        let name = "presto.decklibrary.tests.\(UUID().uuidString)"
        suites.append(name)
        return UserDefaults(suiteName: name)!
    }

    static func library(_ name: String, registry: URL? = nil, defaults: UserDefaults? = nil,
                        now: @escaping () -> Date = { Date() }) -> DeckLibrary {
        let store = dir("support-\(name)").appendingPathComponent("library.json")
        return DeckLibrary(storeURL: store, registryURL: registry, defaults: defaults ?? freshDefaults(), now: now)
    }

    static func storeJSON(_ lib: DeckLibrary) -> [String: Any] {
        guard let data = try? Data(contentsOf: lib.storeURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return object
    }

    static func inode(_ url: URL) -> UInt64 {
        var st = stat()
        return stat(url.path, &st) == 0 ? UInt64(st.st_ino) : 0
    }

    static func canonical(_ url: URL) -> String { DeckLibrary.canonicalPath(url) }

    static func deck(_ model: DeckLibraryModel, _ url: URL) -> DeckLibraryDeck? {
        let path = canonical(url)
        return model.decks.first { $0.path == path }
    }

    static func png(width: Int, height: Int) -> Data {
        let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                space: CGColorSpaceCreateDeviceRGB(),
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        context.setFillColor(CGColor(red: 0.2, green: 0.3, blue: 0.8, alpha: 1))
        context.fill(CGRect(x: width / 8, y: height / 3, width: width / 2, height: height / 8))
        let image = context.makeImage()!
        let out = NSMutableData()
        let destination = CGImageDestinationCreateWithData(out as CFMutableData, "public.png" as CFString, 1, nil)!
        CGImageDestinationAddImage(destination, image, nil)
        _ = CGImageDestinationFinalize(destination)
        return out as Data
    }

    static func pixelWidth(of url: URL) -> Int {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] else { return 0 }
        return (props[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue ?? 0
    }

    // MARK: Tests

    static func testMigration() {
        let deckA = makeDeck("migrate/a", title: "Deck A")
        let deckB = makeDeck("migrate/b", title: "Deck B")
        let defaults = freshDefaults()
        defaults.set([
            ["path": deckB.path, "lastOpened": NSNumber(value: 100.0)],
            ["path": deckA.path, "lastOpened": "200.5"],
            ["path": deckA.path, "lastOpened": 50.0], // duplicate
            ["path": "", "lastOpened": 1.0],
            ["lastOpened": 3.0]
        ], forKey: "recentDecks")
        let lib = library("migrate", defaults: defaults)
        let model = lib.model()
        expect(model.decks.map { $0.path } == [canonical(deckA), canonical(deckB)] || Set(model.decks.map { $0.path }) == [canonical(deckA), canonical(deckB)],
               "migration brings both old recent decks")
        expect(model.continueDeck == canonical(deckA), "migration keeps lastOpened order (string seconds parsed)")
        expect(deck(model, deckA)?.lastOpened == 200_500, "lastOpened migrated to ms")
        expect(deck(model, deckB)?.lastOpened == 100_000, "NSNumber lastOpened migrated")
        expect(FileManager.default.fileExists(atPath: lib.storeURL.path), "migration writes library.json")
        expect(defaults.bool(forKey: DeckLibrary.migratedDefaultsKey), "migration marks the defaults suite")
        expect(defaults.array(forKey: "recentDecks")?.count == 5, "old recentDecks key left untouched")
        expect((storeJSON(lib)["migratedRecentDecks"] as? Bool) == true, "store records the migration")

        // A second launch never migrates again, even if the old key changes.
        let deckC = makeDeck("migrate/c", title: "Deck C")
        defaults.set([["path": deckC.path, "lastOpened": 999.0]], forKey: "recentDecks")
        let again = DeckLibrary(storeURL: lib.storeURL, registryURL: nil, defaults: defaults)
        expect(deck(again.model(), deckC) == nil, "migration runs once")

        // A new store with the defaults flag already set does not migrate either.
        let fresh = DeckLibrary(storeURL: dir("support-migrate-flag").appendingPathComponent("library.json"),
                                registryURL: nil, defaults: defaults)
        expect(fresh.model().decks.isEmpty, "defaults flag alone blocks migration")
    }

    static func testMissingAndCorruptStore() {
        let lib = library("missing")
        let model = lib.model()
        expect(model.decks.isEmpty && model.continueDeck == nil && model.projects.isEmpty && model.folders.isEmpty,
               "missing store gives an empty model")
        expect(!lib.modelJSON().isEmpty, "empty model encodes")

        let storeDir = dir("support-corrupt")
        let storeURL = storeDir.appendingPathComponent("library.json")
        write("{ this is not json", to: storeURL)
        let defaults = freshDefaults()
        let deckOld = makeDeck("corrupt/old", title: "Old")
        defaults.set([["path": deckOld.path, "lastOpened": 5.0]], forKey: "recentDecks")
        let corrupt = DeckLibrary(storeURL: storeURL, registryURL: nil, defaults: defaults)
        expect(corrupt.model().decks.isEmpty, "corrupt store starts empty without resurrecting old recents")
        let aside = corrupt.recoveredCorruptStoreURL
        expect(aside != nil && (try? String(contentsOf: aside!, encoding: .utf8)) == "{ this is not json",
               "corrupt store is kept beside the new one")
        let deck = makeDeck("corrupt/new", title: "New")
        corrupt.recordOpened(deck)
        expect(storeJSON(corrupt)["recents"] != nil, "store is rewritten as valid JSON after corruption")

        // Wrong types inside an otherwise valid store are tolerated field by field.
        let partialURL = dir("support-partial").appendingPathComponent("library.json")
        write(#"{"version": 1, "migratedRecentDecks": true, "recents": "oops", "folders": ["/tmp/x"], "decks": 7}"#, to: partialURL)
        let partial = DeckLibrary(storeURL: partialURL, registryURL: nil, defaults: freshDefaults())
        expect(partial.recoveredCorruptStoreURL == nil, "partly wrong store is not treated as corrupt")
        expect(partial.model().folders.map { $0.path } == ["/tmp/x"], "valid fields survive a partly wrong store")
    }

    static func testAtomicWrites() {
        let lib = library("atomic")
        let a = makeDeck("atomic/a", title: "A")
        let b = makeDeck("atomic/b", title: "B")
        lib.recordOpened(a)
        let first = inode(lib.storeURL)
        lib.recordOpened(b)
        let second = inode(lib.storeURL)
        expect(first != 0 && second != 0 && first != second, "saves replace library.json atomically (new inode)")
        let leftovers = (try? FileManager.default.contentsOfDirectory(atPath: lib.storeURL.deletingLastPathComponent().path)) ?? []
        expect(leftovers.sorted() == ["library.json"], "no temporary files left beside the store")

        lib.setLastPosition(a, slide: 3, step: 1)
        let afterPosition = inode(lib.storeURL)
        lib.setLastPosition(a, slide: 3, step: 1)
        expect(inode(lib.storeURL) == afterPosition, "unchanged last position does not rewrite the store")

        // A store that cannot be written keeps working in memory and reports the error.
        let lockedDir = dir("support-locked")
        let lockedStore = lockedDir.appendingPathComponent("library.json")
        chmod(lockedDir.path, 0o500)
        let locked = DeckLibrary(storeURL: lockedStore, registryURL: nil, defaults: freshDefaults())
        locked.recordOpened(a)
        expect(locked.lastSaveError != nil, "write failure is reported")
        expect(locked.model().continueDeck == canonical(a), "write failure keeps the in-memory state")
        chmod(lockedDir.path, 0o755)
        locked.recordOpened(b)
        expect(locked.lastSaveError == nil && FileManager.default.fileExists(atPath: lockedStore.path), "a later good write clears the error")
    }

    static func registryText(spaces: URL) -> String {
        let learner = root.appendingPathComponent("reg/learner/presentations/webslides/norms-rewritten-discussant").path
        let drive = spaces.path
        return """
        engine_root: \(root.appendingPathComponent("reg/webslides").path)

        # comment line
        decks:
          norms-rewritten-discussant:
            project_id: learner
            project_root: \(root.appendingPathComponent("reg/learner").path)
            root: \(learner)
            aliases:
              - norms rewritten
              - "henkes: discussant"
          long-run-tcd:
            project_id: long-run
            project_root: "\(root.appendingPathComponent("reg/LONG RUN EFFECTS/data_work").path)"
            root: "\(root.appendingPathComponent("reg/LONG RUN EFFECTS/data_work/presentations/webslides/long-run-tcd").path)"
            aliases:
              - tcd
          mada-bread-2026:
            project_id: madagascar   # trailing comment
            project_root: '\(drive)'
            root: \(drive)/presentations/webslides/mada-bread-2026
            tex_dir: /Users/me/Overleaf/Paper
            aliases:
              - mada bread
          no-project:
            root: \(root.appendingPathComponent("reg/misc/talks/plain-deck/index.html").path)
          no-root:
            project_id: ghost
        """
    }

    static func testRegistry() {
        let drive = dir("reg/CloudStorage/GoogleDrive-me/My Drive/RESEARCH/Project Drive")
        makeDeck("reg/learner/presentations/webslides/norms-rewritten-discussant", title: "Discussion: Norms Rewritten?",
                 modified: Date(timeIntervalSince1970: 1_000))
        let tcd = makeDeck("reg/LONG RUN EFFECTS/data_work/presentations/webslides/long-run-tcd",
                           title: "Treatment-Effect Persistence and Policy Comparisons - TCD 2026",
                           modified: Date(timeIntervalSince1970: 3_000))
        let bread = makeDeck("reg/CloudStorage/GoogleDrive-me/My Drive/RESEARCH/Project Drive/presentations/webslides/mada-bread-2026",
                             title: "Menstrual Stigma, Hygiene, and Human Capital (BREAD/ITAM/CAF 2026)",
                             modified: Date(timeIntervalSince1970: 2_000))
        let curriculum = makeDeck("reg/CloudStorage/GoogleDrive-me/My Drive/RESEARCH/Project Drive/presentations/webslides/curriculum-map",
                                  title: "Curriculum Map", modified: Date(timeIntervalSince1970: 500))
        let plain = makeDeck("reg/misc/talks/plain-deck", title: nil, modified: Date(timeIntervalSince1970: 100))
        let kitchen = makeDeck("reg/webslides/examples/kitchen-sink", title: "Kitchen Sink",
                               modified: Date(timeIntervalSince1970: 200))

        // CRLF line endings and tabs must parse the same.
        let text = registryText(spaces: drive).replacingOccurrences(of: "\n", with: "\r\n")
        let registry = write(text, to: root.appendingPathComponent("reg/deck_locations.yaml"))
        let lib = library("registry", registry: registry)
        lib.recordOpened(curriculum) // under the madagascar project_root, not itself in the registry
        let model = lib.model()

        expect(deck(model, tcd)?.project == "Long-run", "project_id long-run becomes Long-run")
        expect(deck(model, bread)?.project == "Madagascar", "project_id madagascar becomes Madagascar (comment stripped)")
        expect(deck(model, bread)?.sources == ["registry"], "registry deck source")
        expect(deck(model, bread)?.path.contains("My Drive/RESEARCH/Project Drive") == true, "paths with spaces survive")
        expect(deck(model, curriculum)?.project == "Madagascar", "deck under a registry project_root takes its project")
        expect(deck(model, plain)?.project == "misc", "registry deck without project_id uses its folder, skipping generic names")
        expect(deck(model, plain)?.title == "plain-deck", "deck without <title> uses its folder name")
        expect(deck(model, kitchen)?.sources == ["engine"], "kitchen sink found under engine_root")
        expect(deck(model, kitchen)?.project == "Engine" && deck(model, kitchen)?.inRecent == false,
               "engine deck is not a Recent deck")
        expect(!model.projects.contains { $0.name == "Engine" }, "engine is not a project")
        expect(model.projects.map { $0.name } == ["Long-run", "Madagascar", "Learner", "misc"],
               "projects sorted by most recent edit: \(model.projects.map { $0.name })")
        expect(model.projects.first { $0.name == "Madagascar" }?.count == 2, "project counts")
        expect(!model.decks.contains { $0.path.contains("ghost") }, "entry without root is ignored")
        expect(model.decks.first?.path == canonical(tcd), "decks sorted by last edited")
        expect(deck(model, tcd)?.headline == "Treatment-Effect Persistence and Policy Comparisons"
               && deck(model, tcd)?.occasion == "TCD 2026", "title split into headline and occasion")
        expect(deck(model, bread)?.headline == "Menstrual Stigma, Hygiene, and Human Capital"
               && deck(model, bread)?.occasion == "BREAD/ITAM/CAF 2026", "parenthetical occasion")

        let missing = library("registry-missing", registry: root.appendingPathComponent("reg/nope.yaml"))
        expect(missing.model().decks.isEmpty, "missing registry is tolerated")
        let none = library("registry-none", registry: nil)
        expect(none.model().decks.isEmpty, "no registry configured")
        let empty = library("registry-empty", registry: write("", to: root.appendingPathComponent("reg/empty.yaml")))
        expect(empty.model().decks.isEmpty, "empty registry")
        let junk = library("registry-junk", registry: write("decks: [1, 2\n  - : :\n\u{0}\u{1}", to: root.appendingPathComponent("reg/junk.yaml")))
        expect(junk.model().decks.isEmpty, "malformed registry yields nothing and does not crash")

        // Registry edits are picked up (cache keyed on its modification time).
        write(text + "\r\n  extra:\r\n    project_id: extra\r\n    root: \(plain.deletingLastPathComponent().path)\r\n",
              to: registry, modified: Date().addingTimeInterval(5))
        expect(lib.model().decks.filter { $0.path == canonical(plain) }.count == 1, "re-read registry has no duplicates")
    }

    static func testFolders() {
        let folder = dir("folders/Talks 2026")
        let one = makeDeck("folders/Talks 2026/one level", title: "One &amp; Only &mdash; Lisbon")
        let two = makeDeck("folders/Talks 2026/group/two-level", title: "Two")
        makeDeck("folders/Talks 2026/a/b/three-level", title: "Three") // too deep
        makeDeck("folders/Talks 2026/.hidden/deck", title: "Hidden")
        makeDeck("folders/Talks 2026/node_modules/pkg", title: "Package")
        let target = makeDeck("folders/elsewhere/linked-deck", title: "Linked")
        try? FileManager.default.createSymbolicLink(atPath: folder.appendingPathComponent("engine").path,
                                                    withDestinationPath: target.deletingLastPathComponent().path)
        let selfDeck = makeDeck("folders/single-deck", title: "Self")

        let lib = library("folders")
        lib.addFolder(folder)
        lib.addFolder(folder) // duplicate ignored
        lib.addFolder(selfDeck) // an index.html adds its folder
        var model = lib.model()
        let paths = Set(model.decks.map { $0.path })
        expect(paths == [canonical(one), canonical(two), canonical(selfDeck)], "folder scan finds one and two levels deep only: \(paths)")
        expect(deck(model, one)?.title == "One & Only — Lisbon", "title entities decoded")
        expect(deck(model, one)?.sources == ["folder"] && deck(model, one)?.inRecent == true, "folder decks are Recent decks")
        expect(model.folders.count == 2 && model.folders.first?.name == "Talks 2026" && model.folders.first?.count == 2,
               "folder model lists counts")
        expect(deck(model, two)?.project == "group", "folder deck project is its enclosing folder")

        // A new deck appears (directory listing cache follows the folder's mtime).
        let three = makeDeck("folders/Talks 2026/new-deck", title: "New")
        model = lib.model()
        expect(deck(model, three) != nil, "new deck in a watched folder appears")

        // Vanished: rename the folder away (no deletion); the deck drops out.
        try? FileManager.default.moveItem(at: three.deletingLastPathComponent(),
                                          to: root.appendingPathComponent("folders/moved-away"))
        expect(deck(lib.model(), three) == nil, "vanished folder deck drops out")

        lib.removeFolder(selfDeck.deletingLastPathComponent())
        model = lib.model()
        expect(deck(model, selfDeck) == nil && model.folders.count == 1, "removeFolder")
        lib.removeFolder(folder)
        expect(lib.model().decks.isEmpty, "removing the last folder empties the library")

        let gone = library("folders-gone")
        gone.addFolder(root.appendingPathComponent("folders/does-not-exist"))
        let goneModel = gone.model()
        expect(goneModel.folders.first?.available == false && goneModel.folders.first?.count == 0, "missing folder marked unavailable")
    }

    static func testVanishedRecentAndContinue() {
        var clock = Date(timeIntervalSince1970: 10_000)
        let lib = library("vanish", now: { clock })
        let a = makeDeck("vanish/a", title: "A")
        let b = makeDeck("vanish/b", title: "B")
        lib.recordOpened(a)
        clock = clock.addingTimeInterval(60)
        lib.recordOpened(b)
        expect(lib.model().continueDeck == canonical(b), "continue is the last opened deck")
        try? FileManager.default.moveItem(at: b.deletingLastPathComponent(), to: root.appendingPathComponent("vanish/b-moved"))
        let model = lib.model()
        expect(deck(model, b) == nil, "vanished recent deck is left out of the model")
        expect(model.continueDeck == canonical(a), "continue falls back to the next existing deck")
        // It comes back if the folder returns (Drive remounted).
        try? FileManager.default.moveItem(at: root.appendingPathComponent("vanish/b-moved"), to: b.deletingLastPathComponent())
        expect(lib.model().continueDeck == canonical(b), "a returning deck is remembered")

        // recordOpened with a folder URL and an index.html URL are the same deck.
        lib.recordOpened(a.deletingLastPathComponent())
        let recents = storeJSON(lib)["recents"] as? [[String: Any]] ?? []
        expect(recents.count == 2 && (recents.first?["path"] as? String) == canonical(a), "folder and index.html URLs share one identity")

        let capped = library("cap")
        for i in 0..<60 { capped.recordOpened(makeDeck("cap/d\(i)", title: "D\(i)")) }
        expect((storeJSON(capped)["recents"] as? [Any])?.count == DeckLibrary.recentLimit, "recents capped")
    }

    static func testPositionTalkPinsRemove() {
        let slides = [
            "<section class=\"slide title\"><h1><span class=\"lead\">Big</span><br>Title</h1></section>",
            "<!-- <section class=\"slide\"><h2>Commented out</h2></section> -->",
            "<section class=\"slide content\" data-steps=\"3\"><h2>2. Data &amp; design</h2><p data-step=\"1\">x</p></section>",
            "<section class=\"slide section-header\"><h1>Results</h1></section>",
            "<section class=\"slide content\"><h2>3. What we estimate</h2><li data-step=\"2-4\">a</li><li data-step='5'>b</li></section>",
            "<section class=\"slide appendix content\" id=\"a1\"><h2>Appendix: one</h2></section>",
            "<section\n  class=\"appendix slide\"><h3>no h1</h3><h2>Appendix: two</h2></section>",
            "<section class=\"notaslide\"><h2>Not a slide</h2></section>"
        ]
        let deckURL = makeDeck("position/deck", title: "Position Deck", slides: slides)
        let lib = library("position")
        lib.recordOpened(deckURL)
        var model = lib.model()
        var d = deck(model, deckURL)
        expect(d?.slideCount == 4 && d?.appendixCount == 2, "slide counts (comments and non-slide sections ignored)")
        expect(d?.stepCount == 8, "step count sums each slide's highest step: \(String(describing: d?.stepCount))")
        expect(d?.lastSlide == nil && d?.lastSlideLabel == nil, "no position yet")

        lib.setLastPosition(deckURL, slide: 3, step: 2)
        model = lib.model()
        d = deck(model, deckURL)
        expect(lib.lastPosition(deckURL)! == (3, 2), "lastPosition round trip")
        expect(d?.lastSlide == 3 && d?.lastStep == 2 && d?.lastSlideLabel == "4" && d?.lastSlideTitle == "3. What we estimate",
               "last slide label and title")
        lib.setLastPosition(deckURL, slide: 5, step: -3)
        d = deck(lib.model(), deckURL)
        expect(d?.lastSlideLabel == "A2" && d?.lastSlideTitle == "Appendix: two" && d?.lastStep == 0, "appendix label, negative step clamped")
        lib.setLastPosition(deckURL, slide: 99, step: 0)
        d = deck(lib.model(), deckURL)
        expect(d?.lastSlideLabel == "100" && d?.lastSlideTitle == nil, "out-of-range position after a deck shrank")
        lib.setLastPosition(deckURL, slide: 0, step: 0)
        expect(deck(lib.model(), deckURL)?.lastSlideTitle == "Big Title", "title slide heading with nested tags")

        expect(lib.talkMinutes(deckURL) == nil, "no talk length by default")
        lib.setTalkMinutes(deckURL, 45)
        expect(lib.talkMinutes(deckURL) == 45 && deck(lib.model(), deckURL)?.talkMinutes == 45, "talk minutes")
        lib.setTalkMinutes(deckURL, 0)
        expect(lib.talkMinutes(deckURL) == nil, "zero clears talk minutes")
        lib.setTalkMinutes(deckURL, 50)
        lib.setPinned(deckURL, true)
        let reopened = DeckLibrary(storeURL: lib.storeURL, registryURL: nil, defaults: freshDefaults())
        expect(reopened.talkMinutes(deckURL) == 50 && reopened.lastPosition(deckURL)! == (0, 0), "state persists across instances")
        expect(deck(reopened.model(), deckURL)?.pinned == true, "pin persists")
        lib.setTalkMinutes(deckURL, nil)
        expect(lib.talkMinutes(deckURL) == nil, "nil clears talk minutes")

        // Remove from Recent: a pinned deck stays (Pinned view), out of Recent.
        lib.removeRecent(deckURL)
        d = deck(lib.model(), deckURL)
        expect(d?.inRecent == false && d?.pinned == true && d?.sources == ["pinned"], "removed pinned deck stays pinned")
        expect(lib.model().continueDeck == nil, "removed deck is not the Continue deck")
        lib.setPinned(deckURL, false)
        expect(deck(lib.model(), deckURL) == nil, "unpinned removed deck leaves the library")
        lib.recordOpened(deckURL)
        d = deck(lib.model(), deckURL)
        expect(d?.inRecent == true && lib.model().continueDeck == canonical(deckURL), "reopening restores it to Recent")

        // A folder deck removed from Recent is hidden from Recent but stays listed.
        let folderDeck = makeDeck("position/folder/inner", title: "Folder deck")
        lib.addFolder(root.appendingPathComponent("position/folder"))
        lib.removeRecent(folderDeck)
        d = deck(lib.model(), folderDeck)
        expect(d != nil && d?.inRecent == false, "folder deck hidden from Recent only")
    }

    static func testThumbnails() {
        let deckURL = makeDeck("thumbs/deck", title: "Thumb", modified: Date(timeIntervalSince1970: 50_000))
        let other = makeDeck("thumbs/other", title: "Other")
        let lib = library("thumbs")
        lib.recordOpened(deckURL)
        lib.recordOpened(other)
        expect(lib.thumbnailURL(deckURL) == nil, "no thumbnail yet")
        expect(deck(lib.model(), deckURL)?.thumbnailDataURL == nil, "model has no thumbnail yet")
        expect(!lib.saveThumbnail(Data("not an image".utf8), for: deckURL), "invalid image data rejected")

        expect(lib.saveThumbnail(png(width: 1600, height: 900), for: deckURL), "thumbnail saved")
        guard let url = lib.thumbnailURL(deckURL) else { expect(false, "thumbnail URL after save"); return }
        expect(url.deletingLastPathComponent().path == lib.thumbnailsDirectory.path, "thumbnail stored under thumbnails/")
        expect(lib.thumbnailsDirectory.deletingLastPathComponent().path == lib.storeURL.deletingLastPathComponent().path,
               "thumbnails sit beside library.json (Application Support/Presto/thumbnails in the app)")
        expect(url.lastPathComponent.count == 36 && url.pathExtension == "png", "thumbnail named by a path hash")
        expect(pixelWidth(of: url) == 800, "large thumbnails scaled to 800 px")
        expect(lib.thumbnailURL(deckURL.deletingLastPathComponent()) == url, "folder URL finds the same thumbnail")
        expect(lib.thumbnailURL(other) == nil, "thumbnails are per deck")
        var d = deck(lib.model(), deckURL)
        expect(d?.thumbnailDataURL?.hasPrefix("data:image/png;base64,") == true && d?.thumbnailStale == false, "model serves a data URL")
        if let dataURL = d?.thumbnailDataURL,
           let decoded = Data(base64Encoded: String(dataURL.dropFirst("data:image/png;base64,".count))) {
            expect(decoded == (try? Data(contentsOf: url)), "data URL holds the stored PNG")
        }

        let small = png(width: 320, height: 180)
        expect(lib.saveThumbnail(small, for: other), "small thumbnail saved")
        expect((try? Data(contentsOf: lib.thumbnailURL(other)!)) == small, "small PNGs stored as given")

        // Editing the deck (new mtime) invalidates the thumbnail; the old image
        // stays in the model, flagged stale, so cards do not flash.
        write(deckHTML(title: "Thumb edited", slides: []), to: deckURL, modified: Date(timeIntervalSince1970: 60_000))
        expect(lib.thumbnailURL(deckURL) == nil, "deck edit invalidates the thumbnail")
        d = deck(lib.model(), deckURL)
        expect(d?.thumbnailDataURL != nil && d?.thumbnailStale == true && d?.title == "Thumb edited", "stale thumbnail kept and flagged; title re-read")
        lib.saveThumbnail(png(width: 640, height: 360), for: deckURL)
        d = deck(lib.model(), deckURL)
        expect(lib.thumbnailURL(deckURL) != nil && d?.thumbnailStale == false, "fresh thumbnail after recapture")
        expect(pixelWidth(of: lib.thumbnailURL(deckURL)!) == 640, "updated thumbnail replaces the cached data URL")

        // Position thumbnail: shown only while the stored position matches.
        lib.setLastPosition(deckURL, slide: 17, step: 2)
        expect(lib.savePositionThumbnail(png(width: 1280, height: 720), for: deckURL, slide: 17, step: 2), "position thumbnail saved")
        d = deck(lib.model(), deckURL)
        expect(d?.lastSlideThumbnailDataURL?.hasPrefix("data:image/png;base64,") == true, "position thumbnail in the model")
        lib.setLastPosition(deckURL, slide: 18, step: 0)
        expect(deck(lib.model(), deckURL)?.lastSlideThumbnailDataURL == nil, "position thumbnail hidden after moving on")

        // The app's real store directory is untouched.
        let real = DeckLibrary.defaultStoreURL()
        expect(real.path.hasSuffix("Library/Application Support/Presto/library.json"), "default store location")
        expect(!lib.thumbnailsDirectory.path.hasPrefix(real.deletingLastPathComponent().path), "tests use a temporary store")
    }

    static func testCloudPlaceholders() {
        let cloud = makeDeck("cloud/Dropbox/Talks/cloud-deck", title: "Should not be read")
        let local = makeDeck("cloud/Dropbox/Talks/local-deck", title: "Local")
        let cloudFolder = dir("cloud/Dropbox/Talks/cloud-folder")
        makeDeck("cloud/Dropbox/Talks/cloud-folder/inside", title: "Inside a cloud folder")
        let lib = library("cloud")
        var reads: [String] = []
        let cloudPath = canonical(cloud)
        let cloudFolderPath = DeckLibrary.canonicalFolderPath(cloudFolder)
        lib.isDataless = { path, flag in flag || path == cloudPath || path == cloudFolderPath }
        lib.onFileRead = { reads.append($0) }
        lib.addFolder(root.appendingPathComponent("cloud/Dropbox/Talks"))
        let model = lib.model()
        let d = deck(model, cloud)
        expect(d?.cloudOnly == true && d?.title == "cloud-deck" && d?.slideCount == nil, "cloud-only deck listed by folder name")
        expect(!reads.contains(cloudPath), "cloud-only index.html is never read")
        expect(deck(model, local)?.title == "Local" && deck(model, local)?.cloudOnly == false, "local deck read normally")
        expect(!model.decks.contains { $0.title == "Inside a cloud folder" }, "cloud-only folders are not listed")

        // Second model() call reads nothing (cache keyed on mtime and size).
        reads.removeAll()
        _ = lib.model()
        expect(reads.isEmpty, "unchanged decks are not re-read: \(reads)")

        // Real placeholders: the default probe uses SF_DATALESS from stat; an
        // ordinary file never has it.
        let plain = library("cloud-plain")
        plain.recordOpened(local)
        expect(deck(plain.model(), local)?.cloudOnly == false, "ordinary files are not dataless")
    }

    static func testParsingLimits() {
        // Title beyond the first 64 KB is not looked for; a 3 MB+ file is not parsed for slides.
        let padding = String(repeating: "/* padding */\n", count: 6_000)
        let lateTitle = makeDeck("limits/late-title", title: nil)
        write("<html><head><style>\(padding)</style><title>Late</title></head><body>"
              + "<section class=\"slide\"><h1>x</h1></section></body></html>", to: lateTitle)
        let huge = makeDeck("limits/huge", title: nil)
        let bigBody = String(repeating: "<p>filler filler filler filler filler filler filler filler</p>\n", count: 60_000)
        write("<html><head><title>Huge deck</title></head><body><section class=\"slide\"><h1>a</h1></section>\(bigBody)</body></html>", to: huge)
        let lib = library("limits")
        var reads: [String] = []
        lib.onFileRead = { reads.append($0) }
        lib.recordOpened(lateTitle)
        lib.recordOpened(huge)
        let model = lib.model()
        expect(deck(model, lateTitle)?.title == "late-title" && deck(model, lateTitle)?.slideCount == 1, "title searched in the head only")
        expect(deck(model, huge)?.title == "Huge deck" && deck(model, huge)?.slideCount == nil, "huge file: title only")
        expect(reads.count == 2, "each deck read once")
    }

    static func testNames() {
        expect(DeckLibrary.prettyProjectName("long-run") == "Long-run", "pretty long-run")
        expect(DeckLibrary.prettyProjectName("madagascar") == "Madagascar", "pretty madagascar")
        expect(DeckLibrary.prettyProjectName("my_project") == "My project", "pretty underscores")
        expect(DeckLibrary.prettyProjectName("") == nil, "pretty empty")
        expect(DeckLibrary.folderProject(forDeckFolder: "/x/RESEARCH/LONG RUN EFFECTS/data_work/presentations/webslides/long-run-tcd")
               == "LONG RUN EFFECTS", "folder project skips generic containers")
        expect(DeckLibrary.folderProject(forDeckFolder: "/x/AI HEALTH/presentations/webslides/belief") == "AI HEALTH", "folder project")
        expect(DeckLibrary.displayName(ofFolder: "/x/Project Drive/presentations/webslides") == "Project Drive", "folder display name")
        let split = DeckLibrary.splitTitle("Menstrual Stigma and Human Capital — NBER SI 2026 — v0")
        expect(split.headline == "Menstrual Stigma and Human Capital" && split.occasion == "NBER SI 2026 — v0", "split at the first dash")
        expect(DeckLibrary.splitTitle("Discussion: Norms Rewritten?").occasion == nil, "no occasion")
        expect(DeckLibrary.splitTitle("Long-run effects").occasion == nil, "hyphen inside a word is not a separator")
        let tilde = DeckLibrary.canonicalPath(URL(fileURLWithPath: "~/nope-deck"))
        expect(tilde.hasPrefix(NSHomeDirectory()) || tilde.hasPrefix("/Users"), "tilde expanded")
        expect(tilde.hasSuffix("/nope-deck/index.html"), "missing deck folder maps to its index.html")
    }

    static func testJSONAndPerformance() {
        let lib = library("perf")
        let folder = dir("perf/Many Decks")
        var html = ""
        for i in 0..<40 {
            html += "<section class=\"slide content\" data-steps=\"2\"><h2>Slide \(i) &mdash; topic</h2><p data-step=\"1\">a</p><p data-step=\"2\">b</p></section>\n"
        }
        for i in 0..<200 {
            write(deckHTML(title: "Deck \(i) — Venue", slides: [html]),
                  to: folder.appendingPathComponent("group \(i % 10)/deck \(i)/index.html"),
                  modified: Date(timeIntervalSince1970: Double(1_000_000 + i)))
        }
        lib.addFolder(folder)
        let t0 = Date()
        let first = lib.model()
        let firstTime = Date().timeIntervalSince(t0)
        let t1 = Date()
        let second = lib.model()
        let secondTime = Date().timeIntervalSince(t1)
        print("model() with 200 decks: first \(Int(firstTime * 1000)) ms, cached \(Int(secondTime * 1000)) ms")
        expect(first.decks.count == 200 && first == second, "200 decks found, stable model")
        expect(first.decks.first?.title == "Deck 199 — Venue", "most recently edited first")
        expect(first.decks.first?.slideCount == 40 && first.decks.first?.stepCount == 80, "counts on many-slide decks")
        expect(firstTime < 2.0, "first model() is fast")
        expect(secondTime < 0.25, "cached model() is fast")

        let json = lib.modelJSON()
        let decoded = try? JSONDecoder().decode(DeckLibraryModel.self, from: Data(json.utf8))
        expect(decoded == second, "modelJSON round-trips")
        let object = (try? JSONSerialization.jsonObject(with: Data(json.utf8))) as? [String: Any]
        let firstDeck = (object?["decks"] as? [[String: Any]])?.first
        for key in ["path", "title", "headline", "project", "modified", "slideCount", "pinned", "sources", "inRecent", "thumbnailStale", "cloudOnly"] {
            expect(firstDeck?[key] != nil, "JSON deck has \(key)")
        }
        expect(object?["projects"] is [Any] && object?["folders"] is [Any], "JSON has projects and folders")
    }

    /// Read-only check against Duncan's real registry and decks: the store and
    /// thumbnails go to a temporary directory; deck files are only read.
    static func testRealRegistry() {
        let registry = DeckLibrary.defaultRegistryURL()
        guard ProcessInfo.processInfo.environment["DECK_LIBRARY_REAL_REGISTRY"] != "0",
              FileManager.default.fileExists(atPath: registry.path) else {
            print("SKIP: real registry not present")
            return
        }
        let lib = library("real", registry: registry)
        let t0 = Date()
        let model = lib.model()
        let elapsed = Date().timeIntervalSince(t0)
        let registryDecks = model.decks.filter { $0.sources.contains("registry") }
        print("real registry: \(registryDecks.count) decks, projects \(model.projects.map { "\($0.name) \($0.count)" }), \(Int(elapsed * 1000)) ms")
        expect(!registryDecks.isEmpty, "real registry yields decks")
        expect(registryDecks.allSatisfy { $0.project != nil && !$0.title.isEmpty }, "every real deck has a project and title")
        expect(elapsed < 3.0, "real registry model is fast")
        if let tcd = registryDecks.first(where: { $0.path.hasSuffix("/long-run-tcd/index.html") }) {
            expect(tcd.project == "Long-run" && tcd.headline == "Treatment-Effect Persistence and Policy Comparisons",
                   "TCD deck headline and project")
            expect((tcd.slideCount ?? 0) > 10, "TCD slide count")
        }
    }

    static func main() {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("presto-deck-library-tests-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)

        testMigration()
        testMissingAndCorruptStore()
        testAtomicWrites()
        testRegistry()
        testFolders()
        testVanishedRecentAndContinue()
        testPositionTalkPinsRemove()
        testThumbnails()
        testCloudPlaceholders()
        testParsingLimits()
        testNames()
        testJSONAndPerformance()
        testRealRegistry()

        for name in suites { UserDefaults.standard.removePersistentDomain(forName: name) }
        let trash = Process()
        trash.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        trash.arguments = ["trash", root.path]
        trash.standardError = FileHandle.nullDevice
        try? trash.run()
        trash.waitUntilExit()

        print("DeckLibrary tests: \(passed) passed, \(failed) failed")
        exit(failed == 0 ? 0 : 1)
    }
}

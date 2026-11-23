import CryptoKit
import Darwin
import Foundation
import ImageIO

// Presto's deck library: the model behind Decks mode (presto/decks.html).
//
// A pure model with no UI. It merges four sources of decks:
//   - recent decks Presto opened (migrated once from the old UserDefaults
//     `recentDecks` list),
//   - the WebSlides deck registry (`deck_locations.yaml`, read only),
//   - folders Duncan adds (scanned for */index.html one or two levels deep),
//   - the engine's kitchen-sink example (under the registry's `engine_root`).
// State (recents, folders, last position, talk length, pins) persists to
// `library.json` with atomic writes; thumbnails live beside it in
// `thumbnails/`, keyed by a hash of the deck path.
//
// Call it from one thread (the main thread). `model()` is cheap after the
// first call: deck files are parsed once per modification time, directory
// listings once per directory modification time, thumbnails once per file
// modification time. It never hydrates cloud placeholders: dataless
// (cloud-only) files and folders are detected from their stat flags and
// skipped, and every read runs with dataless materialisation turned off for
// the calling thread, so a placeholder that appears mid-read fails fast
// instead of downloading.

// MARK: - Model rendered by decks.js

struct DeckLibraryModel: Codable, Equatable {
    /// Path of the deck shown in the Continue hero (an entry of `decks`), or nil.
    var continueDeck: String?
    var projects: [DeckLibraryProject]
    /// Every known deck, most recently edited first.
    var decks: [DeckLibraryDeck]
    var folders: [DeckLibraryFolder]
}

struct DeckLibraryProject: Codable, Equatable {
    var name: String
    var count: Int
    /// Most recent `modified` among the project's decks (ms since 1970).
    var latestModified: Double?
}

struct DeckLibraryFolder: Codable, Equatable {
    var path: String
    var name: String
    var count: Int
    var available: Bool
}

struct DeckLibraryDeck: Codable, Equatable {
    /// Canonical absolute path of the deck's index.html.
    var path: String
    /// The deck's folder.
    var folder: String
    /// The deck's `<title>`, else its folder name.
    var title: String
    /// `title` before an occasion suffix (" — TCD 2026", " (BREAD 2026)").
    var headline: String
    var occasion: String?
    var project: String?
    /// index.html modification time, ms since 1970.
    var modified: Double?
    /// Last time Presto opened the deck, ms since 1970.
    var lastOpened: Double?
    /// Main (non-appendix) slides.
    var slideCount: Int?
    var appendixCount: Int?
    /// Sum over slides of the highest declared reveal step (approximate).
    var stepCount: Int?
    /// Last position: WS slide index (0-based, appendix included) and step.
    var lastSlide: Int?
    var lastStep: Int?
    /// "18" for main slide 18, "A4" for the fourth appendix slide.
    var lastSlideLabel: String?
    var lastSlideTitle: String?
    var lastSlideThumbnailDataURL: String?
    var pinned: Bool
    /// Listed under Recent (false after Remove from Recent until reopened).
    var inRecent: Bool
    /// Any of "recent", "registry", "folder", "engine", "pinned".
    var sources: [String]
    var thumbnailDataURL: String?
    /// A thumbnail exists but predates the deck's last modification.
    var thumbnailStale: Bool
    /// index.html is a cloud placeholder that was not read.
    var cloudOnly: Bool
    var talkMinutes: Int?
}

// MARK: - Persistent store

private struct DeckLibraryRecent: Codable, Equatable {
    var path: String
    /// Seconds since 1970.
    var lastOpened: Double
}

private struct DeckLibraryPositionThumbnail: Codable, Equatable {
    var slide: Int
    var step: Int
}

private struct DeckLibraryDeckState: Codable, Equatable {
    var lastSlide: Int?
    var lastStep: Int?
    var talkMinutes: Int?
    var pinned: Bool?
    var hiddenFromRecent: Bool?
    /// The deck's modification time (seconds since 1970) when its thumbnail was saved.
    var thumbnailDeckModified: Double?
    var positionThumbnail: DeckLibraryPositionThumbnail?

    var isEmpty: Bool {
        return lastSlide == nil && lastStep == nil && talkMinutes == nil && pinned != true
            && hiddenFromRecent != true && thumbnailDeckModified == nil && positionThumbnail == nil
    }
}

private struct DeckLibraryStore: Codable, Equatable {
    var version: Int = 1
    var migratedRecentDecks: Bool = false
    var recents: [DeckLibraryRecent] = []
    var folders: [String] = []
    var decks: [String: DeckLibraryDeckState] = [:]

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = (try? c.decodeIfPresent(Int.self, forKey: .version)) ?? 1
        migratedRecentDecks = (try? c.decodeIfPresent(Bool.self, forKey: .migratedRecentDecks)) ?? false
        recents = (try? c.decodeIfPresent([DeckLibraryRecent].self, forKey: .recents)) ?? []
        folders = (try? c.decodeIfPresent([String].self, forKey: .folders)) ?? []
        decks = (try? c.decodeIfPresent([String: DeckLibraryDeckState].self, forKey: .decks)) ?? [:]
    }
}

// MARK: - Parsed deck file

private struct DeckLibrarySlide {
    var label: String
    var title: String?
}

private struct DeckLibraryFileInfo {
    var title: String?
    var slides: [DeckLibrarySlide]?
    var mainCount: Int?
    var appendixCount: Int?
    var stepCount: Int?
}

private struct DeckLibraryStat {
    var isDirectory: Bool
    var isSymlink: Bool
    var dataless: Bool
    var mtime: Double
    var size: Int64
}

private struct DeckLibraryRegistry {
    struct Entry {
        var deckPath: String
        var projectName: String?
        var projectRoot: String?
    }
    var engineRoot: String?
    var entries: [Entry] = []
}

// MARK: - DeckLibrary

final class DeckLibrary {
    static let recentLimit = 50
    static let thumbnailMaxPixels = 800
    /// Files larger than this are not parsed for slides; only their head is read for the title.
    static let fullParseLimit = 3 * 1024 * 1024
    static let headReadLimit = 64 * 1024
    static let migratedDefaultsKey = "PrestoDeckLibraryMigratedRecentDecks"
    static let legacyRecentDecksKey = "recentDecks"
    private static let genericFolderNames: Set<String> = [
        "webslides", "presentations", "presentation", "slides", "decks", "talks", "data_work", "examples"
    ]

    let storeURL: URL
    let registryURL: URL?
    let thumbnailsDirectory: URL
    private let defaults: UserDefaults
    private let now: () -> Date
    private var store = DeckLibraryStore()

    /// The error from the most recent failed write, cleared by the next good one.
    private(set) var lastSaveError: Error?
    /// Where a corrupt store was moved on load, if it was.
    private(set) var recoveredCorruptStoreURL: URL?

    /// Test seams: decides whether a path is a cloud placeholder (default: the
    /// SF_DATALESS stat flag), and observes every file content read.
    var isDataless: (String, Bool) -> Bool = { _, statFlag in statFlag }
    var onFileRead: ((String) -> Void)?

    private var fileInfoCache: [String: (mtime: Double, size: Int64, info: DeckLibraryFileInfo)] = [:]
    private var listingCache: [String: (mtime: Double, entries: [String])] = [:]
    private var dataURLCache: [String: (mtime: Double, size: Int64, url: String)] = [:]
    private var registryCache: (mtime: Double, size: Int64, registry: DeckLibraryRegistry)?

    static func defaultStoreURL() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("Presto", isDirectory: true).appendingPathComponent("library.json")
    }

    static func defaultRegistryURL() -> URL {
        return URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent(".claude/skills/webslides/references/deck_locations.yaml")
    }

    init(storeURL: URL, registryURL: URL?, defaults: UserDefaults = .standard, now: @escaping () -> Date = { Date() }) {
        self.storeURL = storeURL
        self.registryURL = registryURL
        self.thumbnailsDirectory = storeURL.deletingLastPathComponent().appendingPathComponent("thumbnails", isDirectory: true)
        self.defaults = defaults
        self.now = now
        loadStore()
        migrateLegacyRecentsIfNeeded()
    }

    // MARK: Public API

    func model() -> DeckLibraryModel {
        let registry = loadRegistry()
        var sources: [String: Set<String>] = [:]
        var order: [String] = []
        func add(_ path: String, _ source: String) {
            if sources[path] == nil { order.append(path) }
            sources[path, default: []].insert(source)
        }

        for recent in store.recents { add(recent.path, "recent") }
        var registryProjects: [String: String] = [:]
        for entry in registry.entries {
            add(entry.deckPath, "registry")
            if let name = entry.projectName { registryProjects[entry.deckPath] = name }
        }
        var folderModels: [DeckLibraryFolder] = []
        for folder in store.folders {
            let found = scanFolder(folder)
            for path in found { add(path, "folder") }
            let available = DeckLibrary.statPath(folder)?.isDirectory ?? false
            folderModels.append(DeckLibraryFolder(path: folder, name: DeckLibrary.displayName(ofFolder: folder),
                                                  count: found.count, available: available))
        }
        if let engineRoot = registry.engineRoot {
            add(DeckLibrary.joinPath(engineRoot, "examples/kitchen-sink/index.html"), "engine")
        }
        for (path, state) in store.decks where state.pinned == true { add(path, "pinned") }

        let projectRoots = registry.entries.compactMap { entry -> (root: String, name: String)? in
            guard let root = entry.projectRoot, let name = entry.projectName else { return nil }
            return (root, name)
        }.sorted { $0.root.count > $1.root.count }

        let recentTimes = Dictionary(store.recents.map { ($0.path, $0.lastOpened) }, uniquingKeysWith: { a, _ in a })
        var decks: [DeckLibraryDeck] = []
        for path in order {
            guard let fileStat = DeckLibrary.statPath(path), !fileStat.isDirectory else { continue } // vanished
            let deckSources = sources[path] ?? []
            let state = store.decks[path] ?? DeckLibraryDeckState()
            let cloudOnly = isDataless(path, fileStat.dataless)
            let info = cloudOnly ? DeckLibraryFileInfo() : fileInfo(path: path, stat: fileStat)
            let folder = (path as NSString).deletingLastPathComponent
            let title = info.title ?? (folder as NSString).lastPathComponent
            let split = DeckLibrary.splitTitle(title)

            var project: String?
            if let name = registryProjects[path] {
                project = name
            } else if let match = projectRoots.first(where: { DeckLibrary.isPath(path, inside: $0.root) }) {
                project = match.name
            } else if let engineRoot = registry.engineRoot, DeckLibrary.isPath(path, inside: engineRoot) {
                project = "Engine"
            } else {
                project = DeckLibrary.folderProject(forDeckFolder: folder)
            }

            var lastSlideLabel: String?
            var lastSlideTitle: String?
            if let index = state.lastSlide {
                if let slides = info.slides, index >= 0, index < slides.count {
                    lastSlideLabel = slides[index].label
                    lastSlideTitle = slides[index].title
                } else {
                    lastSlideLabel = String(index + 1)
                }
            }

            let thumbFile = thumbnailFileURL(forPath: path)
            let thumbDataURL = dataURL(forFile: thumbFile.path)
            let thumbFresh = thumbDataURL != nil && DeckLibrary.sameTime(state.thumbnailDeckModified, fileStat.mtime)
            var positionDataURL: String?
            if let pos = state.positionThumbnail, pos.slide == state.lastSlide, pos.step == state.lastStep {
                positionDataURL = dataURL(forFile: positionThumbnailFileURL(forPath: path).path)
            }

            let inRecent = !deckSources.isDisjoint(with: ["recent", "registry", "folder"]) && state.hiddenFromRecent != true
            let pinned = state.pinned == true
            decks.append(DeckLibraryDeck(
                path: path,
                folder: folder,
                title: title,
                headline: split.headline,
                occasion: split.occasion,
                project: project,
                modified: fileStat.mtime * 1000,
                lastOpened: recentTimes[path].map { $0 * 1000 },
                slideCount: info.mainCount,
                appendixCount: info.appendixCount,
                stepCount: info.stepCount,
                lastSlide: state.lastSlide,
                lastStep: state.lastSlide == nil ? nil : (state.lastStep ?? 0),
                lastSlideLabel: lastSlideLabel,
                lastSlideTitle: lastSlideTitle,
                lastSlideThumbnailDataURL: positionDataURL,
                pinned: pinned,
                inRecent: inRecent,
                sources: ["recent", "registry", "folder", "engine", "pinned"].filter { deckSources.contains($0) },
                thumbnailDataURL: thumbDataURL,
                thumbnailStale: thumbDataURL != nil && !thumbFresh,
                cloudOnly: cloudOnly,
                talkMinutes: state.talkMinutes
            ))
        }

        decks.sort { a, b in
            let am = a.modified ?? 0, bm = b.modified ?? 0
            if am != bm { return am > bm }
            return a.title.localizedStandardCompare(b.title) == .orderedAscending
        }

        var projectCounts: [String: (count: Int, latest: Double?)] = [:]
        for deck in decks where !deck.sources.contains("engine") { // engine examples have their own sidebar group
            guard let name = deck.project else { continue }
            var entry = projectCounts[name] ?? (0, nil)
            entry.count += 1
            if let m = deck.modified { entry.latest = max(entry.latest ?? m, m) }
            projectCounts[name] = entry
        }
        let projects = projectCounts.map { DeckLibraryProject(name: $0.key, count: $0.value.count, latestModified: $0.value.latest) }
            .sorted { a, b in
                let am = a.latestModified ?? 0, bm = b.latestModified ?? 0
                if am != bm { return am > bm }
                return a.name.localizedStandardCompare(b.name) == .orderedAscending
            }

        let continueDeck = decks
            .filter { $0.lastOpened != nil && store.decks[$0.path]?.hiddenFromRecent != true }
            .max { ($0.lastOpened ?? 0) < ($1.lastOpened ?? 0) }?.path

        return DeckLibraryModel(continueDeck: continueDeck, projects: projects, decks: decks, folders: folderModels)
    }

    /// The model as a JSON string, ready for `PrestoDecks.render(<json>)`.
    func modelJSON() -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(model()), let json = String(data: data, encoding: .utf8) else { return "{}" }
        return json
    }

    func recordOpened(_ deck: URL) {
        let path = DeckLibrary.canonicalPath(deck)
        store.recents.removeAll { $0.path == path }
        store.recents.insert(DeckLibraryRecent(path: path, lastOpened: now().timeIntervalSince1970), at: 0)
        if store.recents.count > DeckLibrary.recentLimit {
            store.recents = Array(store.recents.prefix(DeckLibrary.recentLimit))
        }
        updateState(path) { $0.hiddenFromRecent = nil }
        save()
    }

    func setLastPosition(_ deck: URL, slide: Int, step: Int) {
        let path = DeckLibrary.canonicalPath(deck)
        let newSlide = max(0, slide), newStep = max(0, step)
        let current = store.decks[path]
        guard current?.lastSlide != newSlide || current?.lastStep != newStep else { return }
        updateState(path) { $0.lastSlide = newSlide; $0.lastStep = newStep }
        save()
    }

    func lastPosition(_ deck: URL) -> (slide: Int, step: Int)? {
        guard let state = store.decks[DeckLibrary.canonicalPath(deck)], let slide = state.lastSlide else { return nil }
        return (slide, state.lastStep ?? 0)
    }

    func talkMinutes(_ deck: URL) -> Int? {
        return store.decks[DeckLibrary.canonicalPath(deck)]?.talkMinutes
    }

    func setTalkMinutes(_ deck: URL, _ minutes: Int?) {
        let path = DeckLibrary.canonicalPath(deck)
        let value = minutes.flatMap { $0 > 0 ? min($0, 24 * 60) : nil }
        guard store.decks[path]?.talkMinutes != value else { return }
        updateState(path) { $0.talkMinutes = value }
        save()
    }

    func addFolder(_ folder: URL) {
        var path = DeckLibrary.canonicalFolderPath(folder)
        if let st = DeckLibrary.statPath(path), !st.isDirectory {
            path = (path as NSString).deletingLastPathComponent // an index.html was chosen: add its folder
        }
        guard !store.folders.contains(path) else { return }
        store.folders.append(path)
        save()
    }

    func removeFolder(_ folder: URL) {
        var path = DeckLibrary.canonicalFolderPath(folder)
        if path.hasSuffix("/index.html") { path = (path as NSString).deletingLastPathComponent }
        let before = store.folders.count
        store.folders.removeAll { $0 == path }
        if store.folders.count != before { save() }
    }

    func setPinned(_ deck: URL, _ pinned: Bool) {
        let path = DeckLibrary.canonicalPath(deck)
        guard (store.decks[path]?.pinned == true) != pinned else { return }
        updateState(path) { $0.pinned = pinned ? true : nil }
        save()
    }

    /// Drops the deck from Recent. Registry, folder and pinned decks stay in
    /// their other views; reopening the deck brings it back.
    func removeRecent(_ deck: URL) {
        let path = DeckLibrary.canonicalPath(deck)
        store.recents.removeAll { $0.path == path }
        updateState(path) { $0.hiddenFromRecent = true }
        save()
    }

    /// The saved first-slide thumbnail, only while it matches the deck's
    /// current modification time (nil means: capture a new one).
    func thumbnailURL(_ deck: URL) -> URL? {
        let path = DeckLibrary.canonicalPath(deck)
        let file = thumbnailFileURL(forPath: path)
        guard let deckStat = DeckLibrary.statPath(path), !deckStat.isDirectory,
              FileManager.default.fileExists(atPath: file.path),
              DeckLibrary.sameTime(store.decks[path]?.thumbnailDeckModified, deckStat.mtime) else { return nil }
        return file
    }

    /// Stores a first-slide thumbnail (any ImageIO-readable image; scaled to
    /// at most 800 px and saved as PNG). Returns false for unreadable data.
    @discardableResult
    func saveThumbnail(_ png: Data, for deck: URL) -> Bool {
        let path = DeckLibrary.canonicalPath(deck)
        guard let data = DeckLibrary.normalizedPNG(png, maxPixels: DeckLibrary.thumbnailMaxPixels),
              writeThumbnail(data, to: thumbnailFileURL(forPath: path)) else { return false }
        let mtime = DeckLibrary.statPath(path)?.mtime
        updateState(path) { $0.thumbnailDeckModified = mtime }
        save()
        return true
    }

    /// Stores a thumbnail of the slide at the deck's last position (the small
    /// picture beside "You left off on slide n"). Shown only while the stored
    /// last position is still (slide, step).
    @discardableResult
    func savePositionThumbnail(_ png: Data, for deck: URL, slide: Int, step: Int) -> Bool {
        let path = DeckLibrary.canonicalPath(deck)
        guard let data = DeckLibrary.normalizedPNG(png, maxPixels: 320),
              writeThumbnail(data, to: positionThumbnailFileURL(forPath: path)) else { return false }
        updateState(path) { $0.positionThumbnail = DeckLibraryPositionThumbnail(slide: max(0, slide), step: max(0, step)) }
        save()
        return true
    }

    // MARK: Paths

    /// The identity of a deck: the absolute, symlink-resolved path of its
    /// index.html (a deck folder maps to its index.html).
    static func canonicalPath(_ url: URL) -> String {
        var u = URL(fileURLWithPath: (url.path as NSString).expandingTildeInPath).standardizedFileURL
        let ext = u.pathExtension.lowercased()
        if let st = statPath(u.path) {
            if st.isDirectory { u.appendPathComponent("index.html") }
        } else if ext != "html" && ext != "htm" {
            u.appendPathComponent("index.html")
        }
        return u.resolvingSymlinksInPath().path
    }

    static func canonicalFolderPath(_ url: URL) -> String {
        let u = URL(fileURLWithPath: (url.path as NSString).expandingTildeInPath).standardizedFileURL
        return u.resolvingSymlinksInPath().path
    }

    private static func canonicalRegistryPath(_ raw: String) -> String {
        let expanded = (raw as NSString).expandingTildeInPath
        return URL(fileURLWithPath: expanded).standardizedFileURL.resolvingSymlinksInPath().path
    }

    private static func joinPath(_ base: String, _ rest: String) -> String {
        return URL(fileURLWithPath: base).appendingPathComponent(rest).standardizedFileURL.path
    }

    private static func isPath(_ path: String, inside root: String) -> Bool {
        let r = root.hasSuffix("/") ? root : root + "/"
        return path.hasPrefix(r)
    }

    private func thumbnailKey(forPath path: String) -> String {
        let digest = SHA256.hash(data: Data(path.utf8))
        return digest.prefix(16).map { String(format: "%02x", $0) }.joined()
    }

    private func thumbnailFileURL(forPath path: String) -> URL {
        return thumbnailsDirectory.appendingPathComponent(thumbnailKey(forPath: path) + ".png")
    }

    private func positionThumbnailFileURL(forPath path: String) -> URL {
        return thumbnailsDirectory.appendingPathComponent(thumbnailKey(forPath: path) + "-position.png")
    }

    // MARK: Store

    private func updateState(_ path: String, _ change: (inout DeckLibraryDeckState) -> Void) {
        var state = store.decks[path] ?? DeckLibraryDeckState()
        change(&state)
        store.decks[path] = state.isEmpty ? nil : state
    }

    private func loadStore() {
        guard FileManager.default.fileExists(atPath: storeURL.path) else { return }
        do {
            let data = try Data(contentsOf: storeURL)
            store = try JSONDecoder().decode(DeckLibraryStore.self, from: data)
            store.recents = store.recents.filter { !$0.path.isEmpty && $0.lastOpened.isFinite }
            store.folders = store.folders.filter { !$0.isEmpty }
        } catch {
            // Keep the unreadable file for inspection and start empty.
            let stamp = Int(now().timeIntervalSince1970)
            let aside = storeURL.deletingLastPathComponent()
                .appendingPathComponent("library.corrupt-\(stamp).json")
            if (try? FileManager.default.moveItem(at: storeURL, to: aside)) != nil {
                recoveredCorruptStoreURL = aside
            }
            store = DeckLibraryStore()
            store.migratedRecentDecks = true // the old list was migrated before; do not resurrect it
        }
    }

    private func migrateLegacyRecentsIfNeeded() {
        guard !store.migratedRecentDecks, !defaults.bool(forKey: DeckLibrary.migratedDefaultsKey) else { return }
        let raw = defaults.array(forKey: DeckLibrary.legacyRecentDecksKey) as? [[String: Any]] ?? []
        var seen = Set(store.recents.map { $0.path })
        for item in raw {
            guard let rawPath = item["path"] as? String, !rawPath.isEmpty else { continue }
            let path = DeckLibrary.canonicalPath(URL(fileURLWithPath: rawPath))
            guard !seen.contains(path) else { continue }
            seen.insert(path)
            var opened = 0.0
            if let number = item["lastOpened"] as? NSNumber {
                opened = number.doubleValue
            } else if let text = item["lastOpened"] as? String, let value = Double(text) {
                opened = value
            }
            store.recents.append(DeckLibraryRecent(path: path, lastOpened: opened.isFinite ? opened : 0))
        }
        store.recents.sort { $0.lastOpened > $1.lastOpened }
        if store.recents.count > DeckLibrary.recentLimit {
            store.recents = Array(store.recents.prefix(DeckLibrary.recentLimit))
        }
        store.migratedRecentDecks = true
        if save() {
            defaults.set(true, forKey: DeckLibrary.migratedDefaultsKey)
        }
    }

    @discardableResult
    private func save() -> Bool {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(store)
            try FileManager.default.createDirectory(at: storeURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: storeURL, options: .atomic)
            lastSaveError = nil
            return true
        } catch {
            lastSaveError = error
            return false
        }
    }

    private func writeThumbnail(_ data: Data, to url: URL) -> Bool {
        do {
            try FileManager.default.createDirectory(at: thumbnailsDirectory, withIntermediateDirectories: true)
            try data.write(to: url, options: .atomic)
            dataURLCache[url.path] = nil
            return true
        } catch {
            lastSaveError = error
            return false
        }
    }

    // MARK: Files

    private static func statPath(_ path: String) -> DeckLibraryStat? {
        var st = stat()
        guard stat(path, &st) == 0 else { return nil }
        var lst = stat()
        let isSymlink = lstat(path, &lst) == 0 && (lst.st_mode & S_IFMT) == S_IFLNK
        let mtime = Double(st.st_mtimespec.tv_sec) + Double(st.st_mtimespec.tv_nsec) / 1_000_000_000
        return DeckLibraryStat(isDirectory: (st.st_mode & S_IFMT) == S_IFDIR,
                               isSymlink: isSymlink,
                               dataless: (st.st_flags & 0x4000_0000) != 0, // SF_DATALESS
                               mtime: mtime,
                               size: Int64(st.st_size))
    }

    private static func sameTime(_ a: Double?, _ b: Double) -> Bool {
        guard let a else { return false }
        return abs(a - b) < 0.0005
    }

    /// Runs `body` with dataless-file materialisation off for this thread, so
    /// a read of a cloud placeholder fails (EDEADLK) instead of downloading it.
    private static func withoutMaterializing<T>(_ body: () -> T) -> T {
        let type = Int32(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES)
        let scope = Int32(IOPOL_SCOPE_THREAD)
        let previous = getiopolicy_np(type, scope)
        let changed = setiopolicy_np(type, scope, Int32(IOPOL_MATERIALIZE_DATALESS_FILES_OFF)) == 0
        defer {
            if changed { _ = setiopolicy_np(type, scope, previous >= 0 ? previous : Int32(IOPOL_MATERIALIZE_DATALESS_FILES_DEFAULT)) }
        }
        return body()
    }

    private func readHead(_ path: String, limit: Int) -> [UInt8]? {
        onFileRead?(path)
        return DeckLibrary.withoutMaterializing {
            guard let handle = FileHandle(forReadingAtPath: path) else { return nil }
            defer { try? handle.close() }
            guard let data = try? handle.read(upToCount: limit) else { return nil }
            return [UInt8](data)
        }
    }

    private func fileInfo(path: String, stat fileStat: DeckLibraryStat) -> DeckLibraryFileInfo {
        if let cached = fileInfoCache[path], cached.mtime == fileStat.mtime, cached.size == fileStat.size {
            return cached.info
        }
        var info = DeckLibraryFileInfo()
        let full = fileStat.size <= Int64(DeckLibrary.fullParseLimit)
        if let bytes = readHead(path, limit: full ? DeckLibrary.fullParseLimit + 1 : DeckLibrary.headReadLimit) {
            info.title = DeckLibraryHTML.title(in: bytes, searchLimit: DeckLibrary.headReadLimit)
            if full {
                let slides = DeckLibraryHTML.slides(in: bytes)
                info.slides = slides.slides
                info.mainCount = slides.main
                info.appendixCount = slides.appendix
                info.stepCount = slides.steps
            }
            fileInfoCache[path] = (fileStat.mtime, fileStat.size, info)
        }
        return info
    }

    /// Directory entries (names), cached by the directory's modification time.
    /// Dataless (cloud-only) directories are not listed.
    private func listDirectory(_ path: String, stat dirStat: DeckLibraryStat) -> [String] {
        if let cached = listingCache[path], cached.mtime == dirStat.mtime { return cached.entries }
        guard !isDataless(path, dirStat.dataless) else { return [] }
        let names: [String] = DeckLibrary.withoutMaterializing {
            (try? FileManager.default.contentsOfDirectory(atPath: path)) ?? []
        }
        let sorted = names.filter { !$0.hasPrefix(".") }.sorted()
        listingCache[path] = (dirStat.mtime, sorted)
        return sorted
    }

    /// index.html files in `folder` itself and one or two levels below it.
    private func scanFolder(_ folder: String) -> [String] {
        guard let folderStat = DeckLibrary.statPath(folder), folderStat.isDirectory else { return [] }
        var found: [String] = []
        func consider(_ dir: String, _ dirStat: DeckLibraryStat, depth: Int) {
            let names = listDirectory(dir, stat: dirStat)
            if names.contains("index.html") {
                let index = (dir as NSString).appendingPathComponent("index.html")
                if let st = DeckLibrary.statPath(index), !st.isDirectory {
                    found.append(st.isSymlink ? DeckLibrary.canonicalPath(URL(fileURLWithPath: index)) : index)
                }
            }
            guard depth < 2 else { return }
            for name in names where name != "index.html" && name != "node_modules" && !name.hasSuffix(".app") {
                let child = (dir as NSString).appendingPathComponent(name)
                // Symlinked folders (the decks' shared `engine` link) are never decks.
                guard let st = DeckLibrary.statPath(child), st.isDirectory, !st.isSymlink else { continue }
                consider(child, st, depth: depth + 1)
            }
        }
        consider(folder, folderStat, depth: 0)
        return found
    }

    private func dataURL(forFile path: String) -> String? {
        guard let st = DeckLibrary.statPath(path), !st.isDirectory, st.size > 0 else {
            dataURLCache[path] = nil
            return nil
        }
        if let cached = dataURLCache[path], cached.mtime == st.mtime, cached.size == st.size { return cached.url }
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        let url = "data:image/png;base64," + data.base64EncodedString()
        dataURLCache[path] = (st.mtime, st.size, url)
        return url
    }

    private static func normalizedPNG(_ input: Data, maxPixels: Int) -> Data? {
        guard let source = CGImageSourceCreateWithData(input as CFData, nil), CGImageSourceGetCount(source) > 0 else { return nil }
        let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
        let width = (props?[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue ?? 0
        let height = (props?[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue ?? 0
        let type = CGImageSourceGetType(source) as String?
        if type == "public.png", width > 0, height > 0, max(width, height) <= maxPixels { return input }
        let image: CGImage?
        if max(width, height) > maxPixels {
            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceThumbnailMaxPixelSize: maxPixels,
                kCGImageSourceCreateThumbnailWithTransform: true
            ]
            image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        } else {
            image = CGImageSourceCreateImageAtIndex(source, 0, nil)
        }
        guard let cgImage = image else { return nil }
        let out = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(out as CFMutableData, "public.png" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(destination, cgImage, nil)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return out as Data
    }

    // MARK: Registry

    private func loadRegistry() -> DeckLibraryRegistry {
        guard let url = registryURL, let st = DeckLibrary.statPath(url.path), !st.isDirectory else {
            registryCache = nil
            return DeckLibraryRegistry()
        }
        if let cached = registryCache, cached.mtime == st.mtime, cached.size == st.size { return cached.registry }
        guard !isDataless(url.path, st.dataless), let bytes = readHead(url.path, limit: 4 * 1024 * 1024) else {
            return DeckLibraryRegistry()
        }
        let registry = DeckLibrary.parseRegistry(String(decoding: bytes, as: UTF8.self))
        registryCache = (st.mtime, st.size, registry)
        return registry
    }

    /// Parses deck_locations.yaml's simple structure (no YAML library):
    ///
    ///     engine_root: /path
    ///     decks:
    ///       <name>:
    ///         project_id: <id>
    ///         project_root: /path
    ///         root: /path/to/deck
    ///         aliases:
    ///           - ...
    fileprivate static func parseRegistry(_ text: String) -> DeckLibraryRegistry {
        var registry = DeckLibraryRegistry()
        struct Pending { var root: String?; var projectID: String?; var projectRoot: String? }
        var pending: Pending?
        var inDecks = false
        var deckIndent: Int?
        func flush() {
            if let p = pending, let root = p.root, !root.isEmpty {
                let path = canonicalPath(URL(fileURLWithPath: (root as NSString).expandingTildeInPath))
                registry.entries.append(.init(deckPath: path,
                                              projectName: p.projectID.flatMap(prettyProjectName),
                                              projectRoot: p.projectRoot.map(canonicalRegistryPath)))
            }
            pending = nil
        }
        for rawLine in text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n") {
            let line = rawLine.replacingOccurrences(of: "\t", with: "    ")
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") || trimmed.hasPrefix("- ") || trimmed == "-" { continue }
            let indent = line.prefix { $0 == " " }.count
            guard let (key, value) = splitYAMLPair(trimmed) else { continue }
            if indent == 0 {
                flush()
                inDecks = key == "decks"
                deckIndent = nil
                if key == "engine_root", !value.isEmpty { registry.engineRoot = canonicalRegistryPath(value) }
                continue
            }
            guard inDecks else { continue }
            if deckIndent == nil { deckIndent = indent }
            if indent <= deckIndent! {
                flush()
                deckIndent = indent
                pending = Pending()
                continue
            }
            guard pending != nil else { continue }
            switch key {
            case "root": pending?.root = value
            case "project_id": pending?.projectID = value
            case "project_root": pending?.projectRoot = value
            default: break
            }
        }
        flush()
        return registry
    }

    private static func splitYAMLPair(_ line: String) -> (String, String)? {
        guard let colon = line.range(of: ":") else { return nil }
        let key = line[..<colon.lowerBound].trimmingCharacters(in: .whitespaces)
        var value = line[colon.upperBound...].trimmingCharacters(in: .whitespaces)
        guard !key.isEmpty else { return nil }
        if let first = value.first, first == "\"" || first == "'" {
            if let end = value.dropFirst().firstIndex(of: first) {
                value = String(value[value.index(after: value.startIndex)..<end])
            } else {
                value = String(value.dropFirst())
            }
            if first == "'" { value = value.replacingOccurrences(of: "''", with: "'") }
        } else if let comment = value.range(of: " #") {
            value = value[..<comment.lowerBound].trimmingCharacters(in: .whitespaces)
        } else if value.hasPrefix("#") {
            value = ""
        }
        return (key, value)
    }

    // MARK: Names

    /// "long-run" becomes "Long-run", "madagascar" "Madagascar", "my_project" "My project".
    static func prettyProjectName(_ id: String) -> String? {
        let cleaned = id.replacingOccurrences(of: "_", with: " ").trimmingCharacters(in: .whitespaces)
        guard let first = cleaned.first else { return nil }
        return first.uppercased() + cleaned.dropFirst()
    }

    /// A project name for a deck outside the registry: the nearest enclosing
    /// folder that is not a generic container ("webslides", "presentations", ...).
    static func folderProject(forDeckFolder folder: String) -> String? {
        var dir = (folder as NSString).deletingLastPathComponent
        while dir != "/" && !dir.isEmpty {
            let name = (dir as NSString).lastPathComponent
            if !genericFolderNames.contains(name.lowercased()) { return name }
            dir = (dir as NSString).deletingLastPathComponent
        }
        return nil
    }

    static func displayName(ofFolder folder: String) -> String {
        var dir = folder
        while true {
            let name = (dir as NSString).lastPathComponent
            let parent = (dir as NSString).deletingLastPathComponent
            if !genericFolderNames.contains(name.lowercased()) || parent == dir || parent == "/" { return name }
            dir = parent
        }
    }

    /// Splits "Headline — Occasion" or "Headline (Occasion)" titles.
    static func splitTitle(_ title: String) -> (headline: String, occasion: String?) {
        for separator in [" — ", " – ", " - ", " | "] {
            if let range = title.range(of: separator) {
                let head = title[..<range.lowerBound].trimmingCharacters(in: .whitespaces)
                let tail = title[range.upperBound...].trimmingCharacters(in: .whitespaces)
                if !head.isEmpty && !tail.isEmpty { return (head, tail) }
            }
        }
        if title.hasSuffix(")"), let open = title.range(of: " (", options: .backwards) {
            let head = title[..<open.lowerBound].trimmingCharacters(in: .whitespaces)
            let tail = title[open.upperBound..<title.index(before: title.endIndex)].trimmingCharacters(in: .whitespaces)
            if !head.isEmpty && !tail.isEmpty { return (head, tail) }
        }
        return (title, nil)
    }
}

// MARK: - Minimal HTML scanning (bytes, ASCII case-insensitive)

private enum DeckLibraryHTML {
    static func lower(_ b: UInt8) -> UInt8 { return (b >= 65 && b <= 90) ? b + 32 : b }

    static func find(_ pattern: [UInt8], in bytes: [UInt8], from start: Int, to end: Int? = nil) -> Int? {
        let limit = min(end ?? bytes.count, bytes.count)
        guard !pattern.isEmpty, limit - pattern.count >= start else { return nil }
        let first = pattern[0]
        var i = max(0, start)
        while i <= limit - pattern.count {
            if lower(bytes[i]) == first {
                var j = 1
                while j < pattern.count && lower(bytes[i + j]) == pattern[j] { j += 1 }
                if j == pattern.count { return i }
            }
            i += 1
        }
        return nil
    }

    static func isNameEnd(_ bytes: [UInt8], _ i: Int) -> Bool {
        guard i < bytes.count else { return true }
        let b = bytes[i]
        return b == 32 || b == 9 || b == 10 || b == 13 || b == 62 || b == 47 // space tab lf cr > /
    }

    /// End of a start tag beginning at `start` (index after '>'), honouring quotes.
    static func tagEnd(_ bytes: [UInt8], from start: Int) -> Int {
        var quote: UInt8 = 0
        var i = start
        while i < bytes.count {
            let b = bytes[i]
            if quote != 0 {
                if b == quote { quote = 0 }
            } else if b == 34 || b == 39 {
                quote = b
            } else if b == 62 {
                return i + 1
            }
            i += 1
        }
        return bytes.count
    }

    static func attribute(_ name: String, inTag bytes: [UInt8], from start: Int, to end: Int) -> String? {
        let pattern = Array((" " + name + "=").utf8)
        var search = start
        while let at = find(pattern, in: bytes, from: search, to: end) ?? findWhitespaceVariant(name, bytes, search, end) {
            var i = at + pattern.count
            guard i < end else { return nil }
            let q = bytes[i]
            if q == 34 || q == 39 {
                i += 1
                var j = i
                while j < end && bytes[j] != q { j += 1 }
                return String(decoding: bytes[i..<j], as: UTF8.self)
            }
            var j = i
            while j < end && !isNameEnd(bytes, j) { j += 1 }
            if j > i { return String(decoding: bytes[i..<j], as: UTF8.self) }
            search = at + 1
        }
        return nil
    }

    // Attributes after a newline or tab rather than a space.
    private static func findWhitespaceVariant(_ name: String, _ bytes: [UInt8], _ start: Int, _ end: Int) -> Int? {
        for sep in ["\n", "\t", "\r"] {
            if let at = find(Array((sep + name + "=").utf8), in: bytes, from: start, to: end) { return at }
        }
        return nil
    }

    static func stripComments(_ bytes: [UInt8]) -> [UInt8] {
        let open = Array("<!--".utf8), close = Array("-->".utf8)
        guard find(open, in: bytes, from: 0) != nil else { return bytes }
        var out: [UInt8] = []
        out.reserveCapacity(bytes.count)
        var i = 0
        while let start = find(open, in: bytes, from: i) {
            out.append(contentsOf: bytes[i..<start])
            guard let end = find(close, in: bytes, from: start + open.count) else { return out }
            i = end + close.count
        }
        out.append(contentsOf: bytes[i...])
        return out
    }

    static func text(_ bytes: ArraySlice<UInt8>) -> String {
        // Drop tags, turning <br> into a space, then decode entities and collapse whitespace.
        var out: [UInt8] = []
        var inTag = false
        for b in bytes {
            if inTag {
                if b == 62 { inTag = false; out.append(32) }
            } else if b == 60 {
                inTag = true
            } else {
                out.append(b)
            }
        }
        let decoded = decodeEntities(String(decoding: out, as: UTF8.self))
        return decoded.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }

    private static let namedEntities: [String: String] = [
        "amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'", "nbsp": " ",
        "mdash": "—", "ndash": "–", "middot": "·", "hellip": "…", "rsquo": "’", "lsquo": "‘",
        "rdquo": "”", "ldquo": "“", "times": "×", "eacute": "é", "egrave": "è", "aacute": "á",
        "oacute": "ó", "uuml": "ü", "ouml": "ö", "auml": "ä", "ccedil": "ç", "atilde": "ã",
        "otilde": "õ", "rarr": "→", "larr": "←", "minus": "−", "le": "≤", "ge": "≥", "deg": "°"
    ]

    static func decodeEntities(_ s: String) -> String {
        guard s.contains("&") else { return s }
        var result = ""
        var rest = Substring(s)
        while let amp = rest.firstIndex(of: "&") {
            result += rest[..<amp]
            let after = rest[rest.index(after: amp)...]
            if let semi = after.prefix(12).firstIndex(of: ";") {
                let name = String(after[..<semi])
                var replacement: String?
                if name.hasPrefix("#x") || name.hasPrefix("#X"), let code = UInt32(name.dropFirst(2), radix: 16) {
                    replacement = Unicode.Scalar(code).map { String(Character($0)) }
                } else if name.hasPrefix("#"), let code = UInt32(name.dropFirst()) {
                    replacement = Unicode.Scalar(code).map { String(Character($0)) }
                } else {
                    replacement = namedEntities[name]
                }
                if let replacement {
                    result += replacement
                    rest = after[after.index(after: semi)...]
                    continue
                }
            }
            result += "&"
            rest = after
        }
        result += rest
        return result
    }

    static func title(in bytes: [UInt8], searchLimit: Int) -> String? {
        let limit = min(bytes.count, searchLimit)
        var search = 0
        while let open = find(Array("<title".utf8), in: bytes, from: search, to: limit) {
            guard isNameEnd(bytes, open + 6) else { search = open + 6; continue }
            let start = tagEnd(bytes, from: open)
            guard let close = find(Array("</title".utf8), in: bytes, from: start) else { return nil }
            let value = text(bytes[start..<close])
            return value.isEmpty ? nil : value
        }
        return nil
    }

    static func slides(in raw: [UInt8]) -> (slides: [DeckLibrarySlide], main: Int, appendix: Int, steps: Int) {
        let bytes = stripComments(raw)
        let sectionOpen = Array("<section".utf8)
        var opens: [(start: Int, contentStart: Int, classes: [String], steps: Int)] = []
        var i = 0
        while let at = find(sectionOpen, in: bytes, from: i) {
            i = at + sectionOpen.count
            guard isNameEnd(bytes, i) else { continue }
            let end = tagEnd(bytes, from: at)
            let classes = (attribute("class", inTag: bytes, from: at, to: end) ?? "")
                .split(whereSeparator: { $0.isWhitespace }).map(String.init)
            let declared = Int(attribute("data-steps", inTag: bytes, from: at, to: end) ?? "") ?? 0
            opens.append((at, end, classes, declared))
            i = end
        }
        var slides: [DeckLibrarySlide] = []
        var main = 0, appendix = 0, steps = 0
        for (n, open) in opens.enumerated() where open.classes.contains("slide") {
            let nextStart = n + 1 < opens.count ? opens[n + 1].start : bytes.count
            let close = find(Array("</section".utf8), in: bytes, from: open.contentStart, to: nextStart) ?? nextStart
            let isAppendix = open.classes.contains("appendix")
            let label: String
            if isAppendix { appendix += 1; label = "A\(appendix)" } else { main += 1; label = String(main) }
            steps += max(open.steps, highestStep(bytes, from: open.contentStart, to: close))
            slides.append(DeckLibrarySlide(label: label, title: heading(bytes, from: open.contentStart, to: close)))
        }
        return (slides, main, appendix, steps)
    }

    static func heading(_ bytes: [UInt8], from start: Int, to end: Int) -> String? {
        var best: (at: Int, level: UInt8)?
        for level: UInt8 in [49, 50] { // "1", "2"
            var search = start
            while let at = find([60, 104, level], in: bytes, from: search, to: end) { // "<h1" / "<h2"
                if isNameEnd(bytes, at + 3) {
                    if best == nil || at < best!.at { best = (at, level) }
                    break
                }
                search = at + 3
            }
        }
        guard let found = best else { return nil }
        let contentStart = tagEnd(bytes, from: found.at)
        let close = find([60, 47, 104, found.level], in: bytes, from: contentStart, to: end) ?? end
        guard contentStart < close else { return nil }
        let value = text(bytes[contentStart..<close])
        return value.isEmpty ? nil : value
    }

    static func highestStep(_ bytes: [UInt8], from start: Int, to end: Int) -> Int {
        let pattern = Array("data-step=".utf8)
        var highest = 0
        var i = start
        while let at = find(pattern, in: bytes, from: i, to: end) {
            var j = at + pattern.count
            if j < end, bytes[j] == 34 || bytes[j] == 39 { j += 1 }
            var number = 0
            var inNumber = false
            while j < end, bytes[j] != 34, bytes[j] != 39, bytes[j] != 62, bytes[j] != 32 {
                let b = bytes[j]
                if b >= 48 && b <= 57 {
                    number = min(number * 10 + Int(b - 48), 10_000)
                    inNumber = true
                } else if inNumber {
                    highest = max(highest, number)
                    number = 0
                    inNumber = false
                }
                j += 1
            }
            if inNumber { highest = max(highest, number) }
            i = at + pattern.count
        }
        return highest
    }
}

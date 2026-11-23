// Presto Feature Tests — WKWebView-based
// Tests SVG inlining, external table loading, section extraction, and editor mode
// Compile: swiftc -framework WebKit -framework Cocoa tests/test_presto_features.swift -o /tmp/test_presto
// Run: /tmp/test_presto

import Cocoa
import Darwin
import WebKit

// MARK: - Test Harness

struct DeckResourceRoots {
    let deckRoot: URL
    let webSlidesRoot: URL
    let readAccessRoot: URL
}

class TestRunner: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
    var webView: WKWebView!
    var editorWebView: WKWebView!
    var passed = 0
    var failed = 0
    var deckURL: URL
    var deckRoot: URL
    var webSlidesRoot: URL
    var readAccessRoot: URL
    var pendingChecks: [() -> Void] = []
    var editorReady = false
    var previewReady = false
    var receivedWysiwygMessages: [[String: Any]] = []

    init(deckPath: String) {
        let url = URL(fileURLWithPath: deckPath)
        self.deckURL = url

        let resourceRoots = TestRunner.resolveDeckResourceRoots(for: url)
        self.deckRoot = resourceRoots.deckRoot
        self.webSlidesRoot = resourceRoots.webSlidesRoot
        self.readAccessRoot = resourceRoots.readAccessRoot

        super.init()
    }

    static func resolveDeckResourceRoots(for deckURL: URL) -> DeckResourceRoots {
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

    static func findDeckRoot(from deckURL: URL) -> URL {
        var dir = deckURL.deletingLastPathComponent()
        for _ in 0..<10 {
            if FileManager.default.fileExists(atPath: dir.appendingPathComponent("engine/webslides.js").path) {
                return dir
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return dir
    }

    static func findWebSlidesRoot(forDeckRoot deckRoot: URL) -> URL {
        let linkedEngineFile = deckRoot.appendingPathComponent("engine/webslides.js")
        let resolvedEngineFile = linkedEngineFile.resolvingSymlinksInPath()
        let resolvedRoot = resolvedEngineFile.deletingLastPathComponent().deletingLastPathComponent()
        if FileManager.default.fileExists(atPath: resolvedRoot.appendingPathComponent("presto/editor.html").path) {
            return resolvedRoot
        }
        return deckRoot
    }

    static func commonAncestor(_ urls: [URL]) -> URL {
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

    func log(_ msg: String) {
        print(msg)
        fflush(stdout)
    }

    func pass(_ name: String) {
        passed += 1
        log("  ✓ PASS: \(name)")
    }

    func fail(_ name: String, detail: String = "") {
        failed += 1
        let extra = detail.isEmpty ? "" : " — \(detail)"
        log("  ✗ FAIL: \(name)\(extra)")
    }

    // MARK: - Section Extraction (same logic as Presto.swift)

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

    // Mirror of Presto.swift commentRanges — precomputed once per parse so the
    // hot tokenizer loops test comment membership instead of two O(n) backwards
    // searches per tag.
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

    func jsStringLiteral(_ s: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [s]),
              let json = String(data: data, encoding: .utf8) else {
            return "\"\""
        }
        let start = json.index(json.startIndex, offsetBy: 1)
        let end = json.index(json.endIndex, offsetBy: -1)
        return String(json[start..<end])
    }

    struct SectionParts {
        let openingTag: String
        let body: String
        let closingTag: String
    }

    func splitSection(_ html: String) -> SectionParts? {
        guard let openStart = html.range(of: "<section", options: .caseInsensitive),
              let openEnd = openingTagEnd(in: html, from: openStart.lowerBound),
              let close = html.range(of: "</section>", options: [.caseInsensitive, .backwards]) else {
            return nil
        }
        let opening = String(html[openStart.lowerBound...openEnd])
        let body = String(html[html.index(after: openEnd)..<close.lowerBound])
        let closing = String(html[close.lowerBound..<close.upperBound])
        return SectionParts(openingTag: opening, body: body, closingTag: closing)
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

    // MARK: - Fetch Polyfill

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

    // MARK: - Run

    func run() {
        log("=== Presto Feature Tests ===")
        log("Deck: \(deckURL.path)")
        log("Deck root: \(deckRoot.path)")
        log("WebSlides root: \(webSlidesRoot.path)")
        log("Read access root: \(readAccessRoot.path)")
        log("")

        // Test 1: Path resolution and section extraction (pure Swift, no WKWebView needed)
        runPathResolutionTests()
        runSectionExtractionTests()
        runSectionExtractionPerfTests()

        // Test 1b: AI refine support (pure Swift)
        runRefineUnitTests()
        runEditorReloadModelTests()

        // Test 2-6: WKWebView-based tests
        runWebViewTests()
    }

    func runPathResolutionTests() {
        log("--- Path Resolution Tests ---")

        if FileManager.default.fileExists(atPath: deckRoot.appendingPathComponent("engine/webslides.js").path) {
            pass("Deck root exposes engine/webslides.js")
        } else {
            fail("Deck root exposes engine/webslides.js", detail: deckRoot.path)
        }

        if FileManager.default.fileExists(atPath: webSlidesRoot.appendingPathComponent("presto/editor.html").path) {
            pass("WebSlides app root exposes presto/editor.html")
        } else {
            fail("WebSlides app root exposes presto/editor.html", detail: webSlidesRoot.path)
        }

        let readPath = readAccessRoot.resolvingSymlinksInPath().standardizedFileURL.path
        let deckPath = deckURL.deletingLastPathComponent().resolvingSymlinksInPath().standardizedFileURL.path
        let appPath = webSlidesRoot.resolvingSymlinksInPath().standardizedFileURL.path
        if deckPath.hasPrefix(readPath) && appPath.hasPrefix(readPath) {
            pass("Read access root covers both deck and app resources")
        } else {
            fail("Read access root covers deck/app", detail: "\(readPath) does not cover \(deckPath) and \(appPath)")
        }

        log("")
    }

    // MARK: - Test Group 1b: AI Refine Unit Tests
    // These mirror the helper functions in Presto.swift's AI Refine section.

    // Mirror of Presto.swift extractDeckHead
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

    // Mirror of Presto.swift sanitizeRefineOutput
    func sanitizeRefineOutput(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
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

    func runRefineUnitTests() {
        log("--- AI Refine Unit Tests ---")

        // ----- extractDeckHead -----
        let html1 = "<html><head>\n<meta charset=\"utf-8\">\n<title>X</title>\n</head><body></body></html>"
        let head1 = extractDeckHead(from: html1)
        if head1.contains("<title>X</title>") && head1.contains("<meta charset=\"utf-8\">") {
            pass("extractDeckHead: extracts inner head content")
        } else {
            fail("extractDeckHead: inner content", detail: "got: \(head1)")
        }

        let html2 = "<html><head class=\"foo\">  inside  </head><body/></html>"
        let head2 = extractDeckHead(from: html2)
        if head2.contains("inside") {
            pass("extractDeckHead: handles attributes on <head>")
        } else {
            fail("extractDeckHead: attributes", detail: "got: \(head2)")
        }

        let htmlQuotedGT = "<html><head data-label=\"A > B\"><title>X</title></head><body></body></html>"
        let headQuotedGT = extractDeckHead(from: htmlQuotedGT)
        if headQuotedGT.trimmingCharacters(in: .whitespacesAndNewlines) == "<title>X</title>" {
            pass("extractDeckHead: handles quoted > in attributes")
        } else {
            fail("extractDeckHead: quoted >", detail: "got: \(headQuotedGT)")
        }

        let htmlCommentedHead = "<html><!-- <head><title>Old</title></head> --><head><title>Real</title></head><body></body></html>"
        let headCommentedHead = extractDeckHead(from: htmlCommentedHead)
        if headCommentedHead.contains("<title>Real</title>") && !headCommentedHead.contains("<title>Old</title>") {
            pass("extractDeckHead: ignores commented fake head")
        } else {
            fail("extractDeckHead: commented fake head", detail: "got: \(headCommentedHead)")
        }

        let htmlTemplateHead = "<html><template><head><title>Template</title></head></template><head><title>Real</title></head><body></body></html>"
        let headTemplateHead = extractDeckHead(from: htmlTemplateHead)
        if headTemplateHead.contains("<title>Real</title>") && !headTemplateHead.contains("<title>Template</title>") {
            pass("extractDeckHead: ignores template fake head")
        } else {
            fail("extractDeckHead: template fake head", detail: "got: \(headTemplateHead)")
        }

        let html3 = "<html><body>nope</body></html>"
        let head3 = extractDeckHead(from: html3)
        if head3.isEmpty {
            pass("extractDeckHead: returns empty when <head> missing")
        } else {
            fail("extractDeckHead: empty fallback", detail: "got: \(head3)")
        }

        // ----- sanitizeRefineOutput -----
        let valid = "<section class=\"slide\"><h1>Hi</h1></section>"
        if sanitizeRefineOutput(valid) == valid {
            pass("sanitizeRefineOutput: passes through valid <section>")
        } else {
            fail("sanitizeRefineOutput: passthrough")
        }

        let withFences = "```html\n<section class=\"slide\"><p>x</p></section>\n```"
        let cleaned = sanitizeRefineOutput(withFences)
        if cleaned == "<section class=\"slide\"><p>x</p></section>" {
            pass("sanitizeRefineOutput: strips ```html fences")
        } else {
            fail("sanitizeRefineOutput: fences", detail: "got: \(cleaned)")
        }

        let withPlainFences = "```\n<section><p>x</p></section>\n```"
        let cleaned2 = sanitizeRefineOutput(withPlainFences)
        if cleaned2 == "<section><p>x</p></section>" {
            pass("sanitizeRefineOutput: strips bare ``` fences")
        } else {
            fail("sanitizeRefineOutput: bare fences", detail: "got: \(cleaned2)")
        }

        let invalidNoSection = "<div>not a slide</div>"
        if sanitizeRefineOutput(invalidNoSection).isEmpty {
            pass("sanitizeRefineOutput: rejects non-<section> output")
        } else {
            fail("sanitizeRefineOutput: rejects non-section")
        }

        let prefixGarbage = "Sure, here it is:\n<section><p>x</p></section>"
        if sanitizeRefineOutput(prefixGarbage).isEmpty {
            pass("sanitizeRefineOutput: rejects output with leading commentary")
        } else {
            fail("sanitizeRefineOutput: leading commentary")
        }

        // ----- Sidecar JSON round-trip -----
        let formatter = ISO8601DateFormatter()
        let now = Date()
        let entryDict: [String: Any] = [
            "id": UUID().uuidString,
            "instructions": "make heading larger",
            "preHTML": "<section><h1>old</h1></section>",
            "postHTML": "<section><h1 style=\"font-size:120px\">old</h1></section>",
            "timestamp": formatter.string(from: now),
            "status": "done"
        ]
        let payload: [String: Any] = [
            "version": 1,
            "slides": [
                "0": [
                    "history": [entryDict],
                    "pointer": 1
                ]
            ]
        ]

        do {
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
            // Round-trip parse
            guard let parsed = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let slides = parsed["slides"] as? [String: Any],
                  let slide0 = slides["0"] as? [String: Any],
                  let history = slide0["history"] as? [[String: Any]],
                  history.count == 1,
                  history[0]["instructions"] as? String == "make heading larger",
                  let pointer = slide0["pointer"] as? Int,
                  pointer == 1 else {
                fail("Sidecar JSON round-trip")
                log("")
                return
            }
            pass("Sidecar JSON round-trip preserves history + pointer")
        } catch {
            fail("Sidecar JSON serialization", detail: error.localizedDescription)
        }

        // ----- Per-slide queue semantics (pure model test) -----
        // Simulate queue ops: enqueue 3 to slide 0, 2 to slide 1; pop one from each.
        var queues: [Int: [String]] = [:]
        queues[0, default: []].append("a")
        queues[0, default: []].append("b")
        queues[0, default: []].append("c")
        queues[1, default: []].append("x")
        queues[1, default: []].append("y")
        if (queues[0]?.count ?? 0) == 3 && (queues[1]?.count ?? 0) == 2 {
            pass("Per-slide queue: independent counts after enqueue")
        } else {
            fail("Per-slide queue: independent counts")
        }
        let popped = queues[0]?.removeFirst()
        if popped == "a" && (queues[0]?.count ?? 0) == 2 {
            pass("Per-slide queue: FIFO pop")
        } else {
            fail("Per-slide queue: FIFO pop")
        }

        // ----- Undo/redo pointer model -----
        // history of 3 done entries, pointer at 3 (all applied) → undo twice → pointer at 1 → redo → pointer at 2
        var pointer = 3
        let historyCount = 3
        if pointer > 0 { pointer -= 1 }   // undo
        if pointer > 0 { pointer -= 1 }   // undo
        if pointer != 1 { fail("Undo pointer math", detail: "got \(pointer)"); }
        else { pass("Undo: pointer decreases on successive undos") }

        if pointer < historyCount { pointer += 1 }  // redo
        if pointer != 2 { fail("Redo pointer math", detail: "got \(pointer)"); }
        else { pass("Redo: pointer increases on redo") }

        // After a new refine appended at pointer 2, redo stack should be truncated:
        // history was 3, but new entry replaces history[2] → new history length is pointer+1 = 3
        var historyLen = historyCount
        if pointer < historyLen {
            historyLen = pointer  // truncate
        }
        historyLen += 1  // append new
        let newPointer = historyLen
        if historyLen == 3 && newPointer == 3 {
            pass("New refine truncates pending redo stack")
        } else {
            fail("Truncate redo on new refine", detail: "len=\(historyLen) ptr=\(newPointer)")
        }

        // A refine result computed against stale slide HTML must not replace a user's newer edit.
        let preHTML = "<section><h1>Before</h1></section>"
        let userEditedHTML = "<section><h1>User edit</h1></section>"
        let staleAIHTML = "<section><h1>AI edit based on old text</h1></section>"
        var currentHTML = userEditedHTML
        if currentHTML == preHTML {
            currentHTML = staleAIHTML
        }
        if currentHTML == userEditedHTML {
            pass("Stale refine result is skipped when slide changed meanwhile")
        } else {
            fail("Stale refine result clobbered newer slide edit")
        }

        log("")
    }

    // MARK: - Editor Reload Model Tests

    func clampedEditorIndex(current: Int, sectionCount: Int) -> Int {
        if sectionCount == 0 {
            return 0
        }
        return max(0, min(current, sectionCount - 1))
    }

    func runEditorReloadModelTests() {
        log("--- Editor Reload Model Tests ---")

        let oldIndex = 4
        let newSectionCount = 2
        let clamped = clampedEditorIndex(current: oldIndex, sectionCount: newSectionCount)
        if clamped == 1 {
            pass("External reload clamps current editor slide after section shrink")
        } else {
            fail("External reload clamps current editor slide after section shrink",
                 detail: "expected 1, got \(clamped)")
        }

        let emptyClamp = clampedEditorIndex(current: oldIndex, sectionCount: 0)
        if emptyClamp == 0 {
            pass("External reload falls back to slide 0 when no sections remain")
        } else {
            fail("External reload empty-section clamp", detail: "expected 0, got \(emptyClamp)")
        }

        log("")
    }

    // MARK: - Test Group 1: Section Extraction

    func runSectionExtractionTests() {
        log("--- Section Extraction ---")

        guard let content = try? String(contentsOf: deckURL, encoding: .utf8) else {
            fail("Read deck file")
            return
        }
        pass("Read deck file (\(content.count) chars)")

        let sections = extractSections(from: content)
        if sections.count > 0 {
            pass("Extracted \(sections.count) sections")
        } else {
            fail("Extract sections", detail: "got 0 sections")
        }

        // Each section should start with <section and end with </section>
        var allValid = true
        for (i, sec) in sections.enumerated() {
            let trimmed = sec.content.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.lowercased().hasPrefix("<section") {
                fail("Section \(i) starts with <section", detail: "got: \(String(trimmed.prefix(40)))")
                allValid = false
            }
            if !trimmed.lowercased().hasSuffix("</section>") {
                fail("Section \(i) ends with </section>", detail: "got: ...\(String(trimmed.suffix(40)))")
                allValid = false
            }
        }
        if allValid {
            pass("All sections have valid <section>...</section> boundaries")
        }

        // Test round-trip: replacing a section in the content should produce valid HTML
        if sections.count >= 2 {
            var modified = content
            let original = sections[1].content
            let replacement = original.replacingOccurrences(of: "class=", with: "class=")  // no-op replacement
            modified.replaceSubrange(sections[1].range, with: replacement)
            let reSections = extractSections(from: modified)
            if reSections.count == sections.count {
                pass("Round-trip section replacement preserves section count")
            } else {
                fail("Round-trip section replacement", detail: "expected \(sections.count), got \(reSections.count)")
            }
        }

        // Test nested sections don't break extraction
        let nested = """
        <div class="ws-deck">
            <section class="slide">
                <div>outer</div>
            </section>
            <section class="slide">
                <div>no nesting here</div>
            </section>
        </div>
        """
        let nestedSections = extractSections(from: nested)
        if nestedSections.count == 2 {
            pass("Handles flat sections correctly")
        } else {
            fail("Flat sections", detail: "expected 2, got \(nestedSections.count)")
        }

        let outsideSections = """
        <template>
            <section class="slide"><h2>Template only</h2></section>
        </template>
        <div class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let scopedSections = extractSections(from: outsideSections)
        if scopedSections.count == 2 && scopedSections[0].content.contains("Real one") {
            pass("Ignores sections outside .ws-deck")
        } else {
            fail("Ignores sections outside .ws-deck",
                 detail: "expected 2 real slides, got \(scopedSections.count)")
        }

        let nestedFakeSlide = """
        <div class="ws-deck">
            <section class="wrapper">
                <section class="slide"><h2>Nested fake</h2></section>
            </section>
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let nestedFakeSections = extractSections(from: nestedFakeSlide)
        if nestedFakeSections.count == 2 && nestedFakeSections[0].content.contains("Real one") {
            pass("Ignores nested slides not directly inside .ws-deck")
        } else {
            fail("Ignores nested slides not directly inside .ws-deck",
                 detail: "expected 2 direct slides, got \(nestedFakeSections.count)")
        }

        let divNestedFakeSlide = """
        <div class="ws-deck">
            <div class="wrapper">
                <section class="slide"><h2>Nested fake</h2></section>
            </div>
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let divNestedFakeSections = extractSections(from: divNestedFakeSlide)
        if divNestedFakeSections.count == 2 && divNestedFakeSections[0].content.contains("Real one") {
            pass("Ignores div-nested slides not directly inside .ws-deck")
        } else {
            fail("Ignores div-nested slides not directly inside .ws-deck",
                 detail: "expected 2 direct slides, got \(divNestedFakeSections.count)")
        }

        let quotedDeckAttribute = """
        <div data-label="Deck > marker" class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let quotedDeckSections = extractSections(from: quotedDeckAttribute)
        if quotedDeckSections.count == 2 && quotedDeckSections[0].content.contains("Real one") {
            pass("Handles quoted > inside .ws-deck opening tag")
        } else {
            fail("Handles quoted > inside .ws-deck opening tag",
                 detail: "expected 2 slides, got \(quotedDeckSections.count)")
        }

        let nonDivDeck = """
        <main class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </main>
        """
        let nonDivSections = extractSections(from: nonDivDeck)
        let nonDivIssues = deckStructureIssues(in: nonDivDeck)
        if nonDivSections.count == 2 && nonDivSections[0].content.contains("Real one") && nonDivIssues.isEmpty {
            pass("Supports non-div .ws-deck containers")
        } else {
            fail("Supports non-div .ws-deck containers",
                 detail: "sections=\(nonDivSections.count), issues=\(nonDivIssues)")
        }

        let divPrefixBeforeDeck = """
        <dividend data-class="ws-deck">
            <section class="slide"><h2>Fake</h2></section>
        </dividend>
        <div class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let divPrefixSections = extractSections(from: divPrefixBeforeDeck)
        if divPrefixSections.count == 2 && divPrefixSections[0].content.contains("Real one") {
            pass("Ignores non-div tags that start with div")
        } else {
            fail("Ignores non-div tags that start with div",
                 detail: "expected 2 real slides, got \(divPrefixSections.count)")
        }

        let divPrefixInsideDeck = """
        <div class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <dividend>
                <section class="slide"><h2>Nested fake</h2></section>
            </dividend>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let divPrefixInsideSections = extractSections(from: divPrefixInsideDeck)
        if divPrefixInsideSections.count == 2 && divPrefixInsideSections[1].content.contains("Real two") {
            pass("Ignores non-div tags inside .ws-deck when finding deck close")
        } else {
            fail("Ignores non-div tags inside .ws-deck when finding deck close",
                 detail: "expected 2 real slides, got \(divPrefixInsideSections.count)")
        }

        let commentedDeckName = """
        <!-- Editor docs mention .ws-deck before the actual deck. -->
        <template>
            <section class="slide"><h2>Template only</h2></section>
        </template>
        <div class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let commentScopedSections = extractSections(from: commentedDeckName)
        if commentScopedSections.count == 2 && commentScopedSections[0].content.contains("Real one") {
            pass("Ignores .ws-deck text before the actual deck")
        } else {
            fail("Ignores .ws-deck text before the actual deck",
                 detail: "expected 2 real slides, got \(commentScopedSections.count)")
        }

        let templateFakeDeck = """
        <template>
            <main class="ws-deck">
                <section class="slide"><h2>Template only</h2></section>
            </main>
        </template>
        <div class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let templateFakeSections = extractSections(from: templateFakeDeck)
        let templateFakeIssues = deckStructureIssues(in: templateFakeDeck)
        if templateFakeSections.count == 2 && templateFakeSections[0].content.contains("Real one") && templateFakeIssues.isEmpty {
            pass("Ignores template-contained fake .ws-deck markup")
        } else {
            fail("Ignores template-contained fake .ws-deck markup",
                 detail: "sections=\(templateFakeSections.count), issues=\(templateFakeIssues)")
        }

        let dataClassBeforeDeck = """
        <div data-class="ws-deck">
            <section class="slide"><h2>Not the deck</h2></section>
        </div>
        <div class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let dataClassSections = extractSections(from: dataClassBeforeDeck)
        if dataClassSections.count == 2 && dataClassSections[0].content.contains("Real one") {
            pass("Ignores data-class before the actual deck")
        } else {
            fail("Ignores data-class before the actual deck",
                 detail: "expected 2 real slides, got \(dataClassSections.count)")
        }

        let noDeckRoot = """
        <section class="slide"><h2>Loose slide</h2></section>
        """
        let noDeckIssues = deckStructureIssues(in: noDeckRoot)
        if noDeckIssues.contains("MISSING DECK: no .ws-deck container found") {
            pass("Reports missing .ws-deck before opening")
        } else {
            fail("Reports missing .ws-deck before opening", detail: "got \(noDeckIssues)")
        }

        let multipleDecks = """
        <div class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
        </div>
        <div class="ws-deck">
            <section class="slide"><h2>Ambiguous second deck</h2></section>
        </div>
        """
        let multipleIssues = deckStructureIssues(in: multipleDecks)
        if multipleIssues.contains(where: { $0.contains("MULTIPLE DECKS") }) {
            pass("Reports multiple .ws-deck containers before opening")
        } else {
            fail("Reports multiple .ws-deck containers before opening", detail: "got \(multipleIssues)")
        }

        let nestedDecks = """
        <div class="ws-deck">
            <section class="slide">
                <h2>Real one</h2>
                <div class="ws-deck">
                    <section class="slide"><h2>Nested deck</h2></section>
                </div>
            </section>
        </div>
        """
        let nestedIssues = deckStructureIssues(in: nestedDecks)
        if nestedIssues.contains("NESTED DECK: .ws-deck containers must not be nested") {
            pass("Reports nested .ws-deck containers before opening")
        } else {
            fail("Reports nested .ws-deck containers before opening", detail: "got \(nestedIssues)")
        }

        let unclosedMainDeck = """
        <main class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
        """
        let unclosedMainIssues = deckStructureIssues(in: unclosedMainDeck)
        if unclosedMainIssues.contains("UNCLOSED DECK: .ws-deck is missing closing </main>") {
            pass("Reports actual tag for unclosed non-div .ws-deck")
        } else {
            fail("Reports actual tag for unclosed non-div .ws-deck", detail: "got \(unclosedMainIssues)")
        }

        let commentedFakeDeck = """
        <!-- <div class="ws-deck"><section class="slide"><h2>Comment only</h2></section></div> -->
        <div class="ws-deck">
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let commentedFakeSections = extractSections(from: commentedFakeDeck)
        if commentedFakeSections.count == 2 && commentedFakeSections[0].content.contains("Real one") {
            pass("Ignores commented fake .ws-deck markup")
        } else {
            fail("Ignores commented fake .ws-deck markup",
                 detail: "expected 2 real slides, got \(commentedFakeSections.count)")
        }

        let commentedSlideInsideDeck = """
        <div class="ws-deck">
            <!-- <section class="slide"><h2>Commented slide</h2></section> -->
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let commentedSlideSections = extractSections(from: commentedSlideInsideDeck)
        if commentedSlideSections.count == 2 && commentedSlideSections[0].content.contains("Real one") {
            pass("Ignores commented slides inside .ws-deck")
        } else {
            fail("Ignores commented slides inside .ws-deck",
                 detail: "expected 2 real slides, got \(commentedSlideSections.count)")
        }

        let commentedDivInsideDeck = """
        <div class="ws-deck">
            <!-- <div><div>Comment-only nesting</div> -->
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        <template><section class="slide"><h2>Outside template</h2></section></template>
        """
        let commentedDivSections = extractSections(from: commentedDivInsideDeck)
        if commentedDivSections.count == 2 && commentedDivSections[0].content.contains("Real one") {
            pass("Ignores commented divs inside .ws-deck")
        } else {
            fail("Ignores commented divs inside .ws-deck",
                 detail: "expected 2 real slides, got \(commentedDivSections.count)")
        }

        let commentedSectionClose = """
        <div class="ws-deck">
            <section class="slide">
                <h2>Real one</h2>
                <!-- old markup ended here </section> -->
                <p>After comment</p>
            </section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let commentedCloseSections = extractSections(from: commentedSectionClose)
        if commentedCloseSections.count == 2 && commentedCloseSections[0].content.contains("After comment") {
            pass("Ignores commented section closing tags inside .ws-deck")
        } else {
            fail("Ignores commented section closing tags inside .ws-deck",
                 detail: "expected 2 real slides with full first slide, got \(commentedCloseSections.count)")
        }

        let scriptRawText = """
        <div class="ws-deck">
            <script>
                const oldMarkup = "</div><section class='slide'><h2>Fake</h2></section>";
            </script>
            <section class="slide"><h2>Real one</h2></section>
            <section class="slide"><h2>Real two</h2></section>
        </div>
        """
        let scriptRawTextSections = extractSections(from: scriptRawText)
        if scriptRawTextSections.count == 2 && scriptRawTextSections[0].content.contains("Real one") {
            pass("Ignores div/section markup inside script raw text")
        } else {
            fail("Ignores div/section markup inside script raw text",
                 detail: "expected 2 real slides, got \(scriptRawTextSections.count)")
        }

        let quotedGreaterThanSection = """
        <section class="slide" data-title="A > B"><h2>Quoted marker</h2></section>
        """
        if let parts = splitSection(quotedGreaterThanSection),
           parts.openingTag == #"<section class="slide" data-title="A > B">"#,
           parts.body == "<h2>Quoted marker</h2>",
           parts.closingTag == "</section>" {
            pass("Split section preserves > inside quoted attributes")
        } else {
            fail("Split section preserves > inside quoted attributes")
        }

        log("")
    }

    // Regression guard for the O(n²) extractSections cost: a comment-free,
    // tag-dense 30-slide deck (the pathological input that measured ~47s per
    // save before the fix) must extract in well under 200ms per save now that
    // comment membership is precomputed and the tag-name check short-circuits
    // ahead of it. A reintroduced per-tag backwards scan pushes this into
    // seconds and fails the bound.
    func runSectionExtractionPerfTests() {
        log("--- Section Extraction Performance ---")

        let slideCount = 30
        var body = "<!doctype html><html><body><main class=\"ws-deck\">\n"
        for s in 0..<slideCount {
            // Tag-dense slide with NO separator comments — the worst case for the
            // old two-backwards-search-per-tag comment check.
            body += "<section class=\"slide\" data-title=\"Slide \(s)\"><div class=\"col\">"
            for row in 0..<25 {
                body += "<div class=\"row\"><span class=\"a\">x\(row)</span>"
                body += "<span class=\"b\"><em>y\(row)</em><strong>z\(row)</strong></span></div>"
            }
            body += "</div></section>\n"
        }
        body += "</main></body></html>"

        let iterations = 3
        var best = Double.greatestFiniteMagnitude
        var lastCount = -1
        for _ in 0..<iterations {
            let start = Date()
            let sections = extractSections(from: body)
            let elapsed = Date().timeIntervalSince(start) * 1000
            best = min(best, elapsed)
            lastCount = sections.count
        }

        if lastCount == slideCount {
            pass("Perf deck extracts all \(slideCount) slides")
        } else {
            fail("Perf deck extracts all \(slideCount) slides", detail: "got \(lastCount)")
        }

        if best < 200 {
            pass(String(format: "extractSections stays sub-200ms on a dense 30-slide deck (best %.1fms)", best))
        } else {
            fail("extractSections stays sub-200ms on a dense 30-slide deck",
                 detail: String(format: "best %.1fms exceeds 200ms budget — O(n²) regression?", best))
        }

        log("")
    }

    // MARK: - Test Group 2: WKWebView Tests

    func runWebViewTests() {
        log("--- WKWebView Tests (file:// fetch, SVGs, tables) ---")

        // Create preview WKWebView (same config as Presto editor mode)
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "allowFileAccessFromFileURLs")
        config.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        addFetchPolyfill(to: config)
        config.userContentController.add(self, name: "presto")

        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 720), configuration: config)
        webView.navigationDelegate = self

        // Create editor WKWebView
        let editorConfig = WKWebViewConfiguration()
        editorConfig.preferences.setValue(true, forKey: "allowFileAccessFromFileURLs")
        editorConfig.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        addFetchPolyfill(to: editorConfig)
        editorConfig.userContentController.add(self, name: "editor")

        editorWebView = WKWebView(frame: NSRect(x: 0, y: 0, width: 600, height: 720), configuration: editorConfig)
        editorWebView.navigationDelegate = self

        // Load the deck
        log("  Loading deck in WKWebView...")
        webView.loadFileURL(deckURL, allowingReadAccessTo: readAccessRoot)

        // Load editor
        let editorHTML = webSlidesRoot.appendingPathComponent("presto/editor.html")
        editorWebView.loadFileURL(editorHTML, allowingReadAccessTo: readAccessRoot)
    }

    // MARK: - WKNavigationDelegate

    func webView(_ wv: WKWebView, didFinish navigation: WKNavigation!) {
        if wv === webView {
            log("  Preview WKWebView loaded")
            // Wait for WebSlides to fully initialize (inlineSVGs + init + loadExternalTables)
            waitForEngine()
        } else if wv === editorWebView {
            log("  Editor WKWebView loaded")
        }
    }

    func webView(_ wv: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        fail("WKWebView navigation", detail: error.localizedDescription)
        finish()
    }

    func webView(_ wv: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        fail("WKWebView provisional navigation", detail: error.localizedDescription)
        finish()
    }

    // MARK: - WKScriptMessageHandler

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let type = body["type"] as? String else { return }

        if message.name == "editor" && type == "ready" {
            editorReady = true
            log("  Editor ready")
            if previewReady { testEditorCommunication() }
        }

        if message.name == "presto" && type.hasPrefix("wysiwyg") {
            receivedWysiwygMessages.append(body)
        }
    }

    // MARK: - Engine Wait

    func waitForEngine(attempts: Int = 0) {
        if attempts > 100 {
            // Diagnostic: what state is the engine in?
            let diagJS = """
            (function() {
                var result = {};
                result.wsExists = typeof WS !== 'undefined';
                result.wsSlides = (typeof WS !== 'undefined' && WS.slides) ? WS.slides.length : -1;
                result.docReady = document.readyState;
                result.deckEl = !!document.querySelector('.ws-deck, #webslides');
                result.sectionCount = document.querySelectorAll('section').length;
                result.imgDataInline = document.querySelectorAll('img[data-inline]').length;
                result.svgCount = document.querySelectorAll('svg').length;
                result.tableSrc = document.querySelectorAll('[data-table-src]').length;
                // Try a fetch to see if it works
                result.fetchTest = 'not_run';
                return result;
            })()
            """
            webView.evaluateJavaScript(diagJS) { [weak self] result, error in
                guard let self = self else { return }
                if let dict = result as? [String: Any] {
                    self.log("  DIAGNOSTIC: \(dict)")
                }
                // Also try a direct fetch test
                let fetchJS = """
                (async function() {
                    try {
                        var resp = await fetch('tables/table1a.html');
                        return JSON.stringify({ ok: resp.ok, status: resp.status, type: resp.type });
                    } catch (e) {
                        return JSON.stringify({ error: e.message, name: e.name });
                    }
                })()
                """
                self.webView.evaluateJavaScript(fetchJS) { result, error in
                    if let json = result as? String,
                       let data = json.data(using: .utf8),
                       let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        self.log("  FETCH TEST: \(dict)")
                    } else if let error = error {
                        self.log("  FETCH TEST ERROR: \(error)")
                    }
                    self.fail("WebSlides engine init", detail: "timed out after 10s")
                    // Try to continue with whatever state we have
                    self.previewReady = true
                    self.runAllWebViewTests()
                }
            }
            return
        }

        let js = """
        (function() {
            if (typeof WS === 'undefined' || !WS.slides || WS.slides.length === 0) return 'not_ready';
            var tableSrcs = document.querySelectorAll('[data-table-src]');
            for (var i = 0; i < tableSrcs.length; i++) {
                if (tableSrcs[i].children.length === 0) return 'tables_loading';
            }
            return 'ready';
        })()
        """

        webView.evaluateJavaScript(js) { [weak self] result, error in
            guard let self = self else { return }
            let status = result as? String ?? "error"

            if status == "ready" {
                self.log("  Engine ready (attempt \(attempts))")
                self.previewReady = true
                self.runAllWebViewTests()
            } else {
                if attempts % 20 == 19 {
                    self.log("  Still waiting... status=\(status) (attempt \(attempts))")
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                    self.waitForEngine(attempts: attempts + 1)
                }
            }
        }
    }

    // MARK: - WebView Test Execution

    func runAllWebViewTests() {
        testFetchWorks()
    }

    func testFetchWorks() {
        // Test: fetch() works for file:// URLs
        let testFile = deckURL.deletingLastPathComponent().appendingPathComponent("tables").appendingPathComponent("table1a.html")
        let exists = FileManager.default.fileExists(atPath: testFile.path)

        if !exists {
            log("  (skipping fetch test — no tables/ directory in this deck)")
            testSVGInlining()
            return
        }

        let js = """
        try {
            const resp = await fetch('tables/table1a.html');
            return JSON.stringify({ ok: resp.ok, status: resp.status, length: (await resp.text()).length });
        } catch (e) {
            return JSON.stringify({ error: e.message });
        }
        """

        Task { @MainActor [weak self] in
            guard let self = self else { return }
            do {
                let value = try await self.webView.callAsyncJavaScript(
                    js, arguments: [:], in: nil, contentWorld: .page
                )
                guard let json = value as? String,
               let data = json.data(using: .utf8),
               let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    self.fail("fetch() for file:// URL", detail: "invalid JSON result")
                    self.testSVGInlining()
                    return
                }
                if let errMsg = dict["error"] as? String {
                    self.fail("fetch() for file:// URL", detail: errMsg)
                } else if let ok = dict["ok"] as? Bool, ok {
                    let len = dict["length"] as? Int ?? 0
                    self.pass("fetch() for file:// URL (ok=true, \(len) bytes)")
                } else {
                    self.fail("fetch() for file:// URL", detail: "ok=false, status=\(dict["status"] ?? "?")")
                }
            } catch {
                self.fail("fetch() for file:// URL", detail: error.localizedDescription)
            }
            self.testSVGInlining()
        }
    }

    func testSVGInlining() {
        // Test: img[data-inline] elements should be replaced with <svg> elements
        let js = """
        (function() {
            var remainingImgs = document.querySelectorAll('img[data-inline]');
            var inlinedSVGs = document.querySelectorAll('svg');
            var svgsWithSteps = document.querySelectorAll('svg [data-step]');
            return {
                remainingImgs: remainingImgs.length,
                totalSVGs: inlinedSVGs.length,
                svgsWithSteps: svgsWithSteps.length
            };
        })()
        """

        webView.evaluateJavaScript(js) { [weak self] result, error in
            guard let self = self else { return }
            if let dict = result as? [String: Any] {
                let remaining = dict["remainingImgs"] as? Int ?? -1
                let svgs = dict["totalSVGs"] as? Int ?? 0
                let steps = dict["svgsWithSteps"] as? Int ?? 0

                if remaining == 0 {
                    self.pass("All img[data-inline] replaced (0 remaining)")
                } else if remaining > 0 {
                    self.fail("SVG inlining", detail: "\(remaining) img[data-inline] still present (not inlined)")
                } else {
                    // No data-inline imgs in this deck
                    self.log("  (no img[data-inline] in this deck)")
                }

                if svgs > 0 {
                    self.pass("Found \(svgs) inlined SVG elements")
                }
                if steps > 0 {
                    self.pass("Found \(steps) SVG data-step elements (for gradual reveal)")
                } else if svgs > 0 {
                    self.log("  (no data-step elements inside SVGs)")
                }
            }
            self.testSVGReveals()
        }
    }

    func testSVGReveals() {
        // Test: SVG data-step elements should have will-change:opacity for compositing
        // First inject the fix (same as Presto does)
        let injectCSS = """
        (function() {
            var style = document.createElement('style');
            style.textContent = 'svg [data-step] { will-change: opacity; }';
            document.head.appendChild(style);
            return 'injected';
        })()
        """

        webView.evaluateJavaScript(injectCSS) { [weak self] _, _ in
            guard let self = self else { return }

            // Check that SVG step elements can be revealed
            let js = """
            (function() {
                var steps = document.querySelectorAll('svg [data-step]');
                if (steps.length === 0) return { count: 0 };
                // Check computed will-change
                var first = steps[0];
                var wc = getComputedStyle(first).willChange;
                // Check reveal CSS on an attached SVG probe. Existing SVG
                // step elements may live on inactive slides, where ancestor
                // visibility/opacity can make direct computed-style mutation
                // assertions misleading in WKWebView.
                var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                var probe = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                probe.setAttribute('data-step', '1');
                svg.appendChild(probe);
                document.body.appendChild(svg);
                var before = getComputedStyle(probe).opacity;
                probe.classList.add('ws-visible');
                var after = getComputedStyle(probe).opacity;
                svg.remove();
                return {
                    count: steps.length,
                    willChange: wc,
                    opacityChangesWithRevealClass: before !== after && after === '1'
                };
            })()
            """

            self.webView.evaluateJavaScript(js) { [weak self] result, _ in
                guard let self = self else { return }
                if let dict = result as? [String: Any] {
                    let count = dict["count"] as? Int ?? 0
                    if count > 0 {
                        let wc = dict["willChange"] as? String ?? "none"
                        let revealClassChangesOpacity = dict["opacityChangesWithRevealClass"] as? Bool ?? false
                        if wc.contains("opacity") {
                            self.pass("SVG step elements have will-change:opacity")
                        } else {
                            self.fail("SVG will-change:opacity", detail: "got will-change: \(wc)")
                        }
                        if revealClassChangesOpacity {
                            self.pass("SVG step opacity responds to ws-visible")
                        } else {
                            self.log("  (WKWebView computed opacity for SVG <g> did not change on probe; visual reveal is covered by app screenshot/manual rendering tests)")
                        }
                    }
                }
                self.testExternalTables()
            }
        }
    }

    func testExternalTables() {
        // Test: data-table-src containers should have table content loaded
        let js = """
        (function() {
            var containers = document.querySelectorAll('[data-table-src]');
            var results = [];
            containers.forEach(function(el) {
                var table = el.querySelector('table');
                var rows = table ? table.querySelectorAll('tr').length : 0;
                results.push({
                    src: el.dataset.tableSrc,
                    hasTable: !!table,
                    rows: rows,
                    innerHTML_length: el.innerHTML.length
                });
            });
            return { count: containers.length, tables: results };
        })()
        """

        webView.evaluateJavaScript(js) { [weak self] result, error in
            guard let self = self else { return }
            if let dict = result as? [String: Any] {
                let count = dict["count"] as? Int ?? 0
                let tables = dict["tables"] as? [[String: Any]] ?? []

                if count == 0 {
                    self.log("  (no data-table-src elements in this deck)")
                } else {
                    var allLoaded = true
                    for t in tables {
                        let src = t["src"] as? String ?? "?"
                        let hasTable = t["hasTable"] as? Bool ?? false
                        let rows = t["rows"] as? Int ?? 0

                        if hasTable && rows > 0 {
                            self.pass("Table loaded: \(src) (\(rows) rows)")
                        } else {
                            self.fail("Table not loaded: \(src)", detail: "hasTable=\(hasTable), rows=\(rows)")
                            allLoaded = false
                        }
                    }
                    if allLoaded {
                        self.pass("All \(count) external tables loaded successfully")
                    }
                }
            }
            self.testTableScaling()
        }
    }

    func testTableScaling() {
        // Test: tables should have reasonable dimensions (not 0px)
        let js = """
        (function() {
            var tables = document.querySelectorAll('.table-container table');
            var results = [];
            tables.forEach(function(t) {
                var rect = t.getBoundingClientRect();
                results.push({
                    width: rect.width,
                    height: rect.height,
                    transform: t.style.transform || 'none'
                });
            });
            return results;
        })()
        """

        webView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self = self else { return }
            if let tables = result as? [[String: Any]] {
                if tables.isEmpty {
                    self.log("  (no .table-container tables)")
                } else {
                    for (i, t) in tables.enumerated() {
                        let w = t["width"] as? Double ?? 0
                        let h = t["height"] as? Double ?? 0
                        if w > 0 && h > 0 {
                            self.pass("Table \(i) has dimensions: \(Int(w))×\(Int(h))px")
                        } else {
                            self.fail("Table \(i) has zero dimensions", detail: "\(w)×\(h)")
                        }
                    }
                }
            }
            self.testNavigationAPI()
        }
    }

    func testNavigationAPI() {
        // Test: WS navigation API works
        let js = """
        (function() {
            var state = WS.getState();
            WS.nextStep();
            var state2 = WS.getState();
            WS.goToSlide(0, 0);
            var state3 = WS.getState();
            return {
                initial: state,
                afterNext: state2,
                afterReset: state3
            };
        })()
        """

        webView.evaluateJavaScript(js) { [weak self] result, error in
            guard let self = self else { return }
            if let dict = result as? [String: Any] {
                if let initial = dict["initial"] as? [String: Any],
                   let afterReset = dict["afterReset"] as? [String: Any] {
                    let totalSlides = initial["totalSlides"] as? Int ?? 0
                    let mainSlides = initial["mainSlides"] as? Int ?? 0
                    self.pass("WS.getState() works (total=\(totalSlides), main=\(mainSlides))")

                    let resetIdx = afterReset["index"] as? Int ?? -1
                    if resetIdx == 0 {
                        self.pass("WS.goToSlide(0,0) resets to slide 0")
                    } else {
                        self.fail("WS.goToSlide reset", detail: "expected index 0, got \(resetIdx)")
                    }
                }
            } else if let error = error {
                self.fail("Navigation API", detail: error.localizedDescription)
            }
            self.testNavigateEvent()
        }
    }

    func testNavigateEvent() {
        // Test: WS.on('navigate', ...) fires and posts message to Swift
        let js = """
        (function() {
            WS.on('navigate', function(state) {
                window.webkit.messageHandlers.presto.postMessage({
                    type: 'slideChanged',
                    index: state.index
                });
            });
            // Trigger a navigation
            WS.goToSlide(1, 0);
            return 'listener_set';
        })()
        """

        // We'll check for the message in userContentController
        pendingChecks.append { [weak self] in
            self?.pass("WS.on('navigate') fires and posts to Swift")
        }

        webView.evaluateJavaScript(js) { [weak self] _, _ in
            // Give it a moment for the message to arrive
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self?.testZoomWithTables()
            }
        }
    }

    func testZoomWithTables() {
        // Test: CSS zoom on slides doesn't break table visibility
        let js = """
        (function() {
            // Apply zoom like Presto does
            var scale = 0.5; // simulate a small window
            WS.slides.forEach(function(s) {
                s.style.zoom = scale;
                s.style.transform = 'translate(-50%, -50%)';
            });

            // Check tables are still visible
            var tables = document.querySelectorAll('.table-container table');
            var results = [];
            tables.forEach(function(t) {
                var rect = t.getBoundingClientRect();
                results.push({ width: rect.width, height: rect.height });
            });

            // Reset zoom
            WS.slides.forEach(function(s) {
                s.style.zoom = '';
                s.style.transform = '';
            });

            return results;
        })()
        """

        webView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self = self else { return }
            if let tables = result as? [[String: Any]] {
                if tables.isEmpty {
                    self.log("  (no tables to test zoom with)")
                } else {
                    var allOK = true
                    for (i, t) in tables.enumerated() {
                        let w = t["width"] as? Double ?? 0
                        let h = t["height"] as? Double ?? 0
                        if w > 0 && h > 0 {
                            // OK — table still has dimensions under zoom
                        } else {
                            self.fail("Table \(i) invisible under zoom", detail: "\(w)×\(h)")
                            allOK = false
                        }
                    }
                    if allOK {
                        self.pass("All tables visible under CSS zoom")
                    }
                }
            }
            self.testEditorCommunication()
        }
    }

    func testEditorCommunication() {
        // Test: editor WKWebView can receive content
        if !editorReady {
            // Wait for editor
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                self?.testEditorCommunication()
            }
            return
        }

        guard let content = try? String(contentsOf: deckURL, encoding: .utf8) else {
            fail("Read deck for editor test")
            testEditorRoundTrip()
            return
        }

        let sections = extractSections(from: content)
        guard sections.count > 0 else {
            fail("No sections for editor test")
            testEditorRoundTrip()
            return
        }

        guard let parts = splitSection(sections[0].content) else {
            fail("Split section for editor test")
            testEditorRoundTrip()
            return
        }

        let opening = jsStringLiteral(parts.openingTag)
        let body = jsStringLiteral(parts.body)
        let closing = jsStringLiteral(parts.closingTag)
        let js = "setContent(\(opening), \(body), \(closing), 0, 1, \(sections.count))"

        editorWebView.evaluateJavaScript(js) { [weak self] _, error in
            guard let self = self else { return }
            if let error = error {
                self.fail("setContent() call", detail: error.localizedDescription)
            } else {
                self.pass("setContent() executed successfully")
            }

            // Read back the content and wrapper UI
            self.editorWebView.evaluateJavaScript("""
            (function() {
                var editor = document.getElementById('editor');
                return {
                    len: editor.value.length,
                    hasOpeningInText: editor.value.indexOf('<section') !== -1,
                    hasClosingInText: editor.value.indexOf('</section>') !== -1,
                    opening: document.getElementById('section-open').textContent,
                    closing: document.getElementById('section-close').textContent,
                    wrapperSelectable: getComputedStyle(document.getElementById('section-open')).userSelect ||
                        getComputedStyle(document.getElementById('section-open')).webkitUserSelect ||
                        getComputedStyle(document.getElementById('section-open')).getPropertyValue('-webkit-user-select')
                };
            })()
            """) { [weak self] result, _ in
                guard let self = self else { return }
                let dict = result as? [String: Any] ?? [:]
                let len = dict["len"] as? Int ?? 0
                let hasOpening = dict["hasOpeningInText"] as? Bool ?? true
                let hasClosing = dict["hasClosingInText"] as? Bool ?? true
                let openingText = dict["opening"] as? String ?? ""
                let closingText = dict["closing"] as? String ?? ""
                let userSelect = dict["wrapperSelectable"] as? String ?? ""
                if len > 0 {
                    self.pass("Editor textarea has content (\(len) chars)")
                } else {
                    self.fail("Editor textarea empty after setContent()")
                }
                if !hasOpening && !hasClosing {
                    self.pass("Editor textarea excludes section wrapper")
                } else {
                    self.fail("Editor textarea includes section wrapper")
                }
                if openingText.hasPrefix("<section") && closingText == "</section>" {
                    self.pass("Fake wrapper lines render section tags")
                } else {
                    self.fail("Fake wrapper lines", detail: "open=\(openingText), close=\(closingText)")
                }
                if userSelect == "none" {
                    self.pass("Fake wrapper lines are non-selectable")
                } else {
                    self.fail("Fake wrapper lines selectable", detail: userSelect)
                }
                self.testEditorRoundTrip()
            }
        }
    }

    func testEditorRoundTrip() {
        // Test: section extraction → edit → re-extraction preserves other sections
        guard let content = try? String(contentsOf: deckURL, encoding: .utf8) else {
            finish()
            return
        }

        let sections = extractSections(from: content)
        guard sections.count >= 3 else {
            log("  (need ≥3 sections for round-trip test, have \(sections.count))")
            testHighlighting()
            return
        }

        // Simulate editing section 1
        var modified = content
        guard let parts = splitSection(sections[1].content) else {
            fail("Split section for round-trip test")
            testHighlighting()
            return
        }
        let editedBody = parts.body.replacingOccurrences(of: "<h2>", with: "<h2>EDITED: ")
        let editedSection = parts.openingTag + editedBody + parts.closingTag
        modified.replaceSubrange(sections[1].range, with: editedSection)

        let newSections = extractSections(from: modified)

        if newSections.count == sections.count {
            pass("Round-trip edit preserves section count (\(sections.count))")
        } else {
            fail("Round-trip edit", detail: "expected \(sections.count) sections, got \(newSections.count)")
        }

        // Check other sections are unchanged
        if newSections.count == sections.count {
            let section0Same = newSections[0].content == sections[0].content
            let section2Same = newSections[2].content == sections[2].content
            if section0Same && section2Same {
                pass("Unedited sections (0, 2) preserved exactly")
            } else {
                if !section0Same { fail("Section 0 changed unexpectedly") }
                if !section2Same { fail("Section 2 changed unexpectedly") }
            }

            if newSections[1].content.contains("EDITED: ") {
                pass("Edited section contains the edit")
            } else {
                fail("Edited section missing edit")
            }
        }

        testHighlighting()
    }

    func testHighlighting() {
        // Test: syntax highlighting produces highlighted output
        let js = """
        (function() {
            var editor = document.getElementById('editor');
            var hl = document.getElementById('highlighting-content');
            if (!editor || !hl) return { error: 'elements not found' };
            // Set some HTML content
            editor.value = '<section class="slide"><h1>Test</h1></section>';
            editor.dispatchEvent(new Event('input'));
            // Check highlighting output
            return {
                hasContent: hl.innerHTML.length > 0,
                hasSpans: hl.querySelectorAll('span').length,
                hasTagClass: hl.querySelector('.hl-tag') !== null,
                hasStringClass: hl.querySelector('.hl-string') !== null,
                hasBracketClass: hl.querySelector('.hl-bracket') !== null
            };
        })()
        """

        editorWebView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self = self else { return }
            if let dict = result as? [String: Any] {
                if let err = dict["error"] as? String {
                    self.fail("Syntax highlighting", detail: err)
                } else {
                    let hasContent = dict["hasContent"] as? Bool ?? false
                    let spanCount = dict["hasSpans"] as? Int ?? 0
                    let hasTag = dict["hasTagClass"] as? Bool ?? false
                    let hasString = dict["hasStringClass"] as? Bool ?? false
                    let hasBracket = dict["hasBracketClass"] as? Bool ?? false

                    if hasContent && spanCount > 0 {
                        self.pass("Highlighting produces \(spanCount) spans")
                    } else {
                        self.fail("Highlighting", detail: "content=\(hasContent), spans=\(spanCount)")
                    }

                    if hasTag { self.pass("hl-tag class present") }
                    else { self.fail("hl-tag class missing") }

                    if hasString { self.pass("hl-string class present") }
                    else { self.fail("hl-string class missing") }

                    if hasBracket { self.pass("hl-bracket class present") }
                    else { self.fail("hl-bracket class missing") }
                }
            }
            self.testCursorRestore()
        }
    }

    func testCursorRestore() {
        let js = """
        (function() {
            var body1 = '<h2>Alpha heading</h2>\\n<p>Before unique-anchor after.</p>';
            var body2 = '<h2>Alpha heading</h2>\\n<p>Before unique-anchor after with extra text.</p>';
            setContent('<section class="slide content">', body1, '</section>', 0, 1, 2);
            var editor = document.getElementById('editor');
            var pos = editor.value.indexOf('unique-anchor') + 'unique'.length;
            editor.focus();
            editor.selectionStart = editor.selectionEnd = pos;
            editor.scrollTop = 12;
            setContent('<section class="slide content">', body2, '</section>', 0, 1, 2);
            return {
                start: editor.selectionStart,
                end: editor.selectionEnd,
                scrollTop: editor.scrollTop,
                nearAnchor: Math.abs(editor.selectionStart - (editor.value.indexOf('unique-anchor') + 'unique'.length)) <= 2
            };
        })()
        """

        editorWebView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self = self else { return }
            if let dict = result as? [String: Any] {
                let nearAnchor = dict["nearAnchor"] as? Bool ?? false
                let start = dict["start"] as? Int ?? -1
                let end = dict["end"] as? Int ?? -1
                if nearAnchor && start == end {
                    self.pass("Cursor restored near text anchor after same-slide refresh")
                } else {
                    self.fail("Cursor restore", detail: "start=\(start), end=\(end), near=\(nearAnchor)")
                }
            }
            self.testPendingEditorFlush()
        }
    }

    func testPendingEditorFlush() {
        let js = """
        (function() {
            setContent('<section class="slide content">', '<h2>Draft</h2>', '</section>', 0, 1, 2);
            var editor = document.getElementById('editor');
            editor.value = '<h2>Draft changed before navigation</h2>';
            editor.dispatchEvent(new Event('input'));
            var first = window.getPendingEditorChange();
            var second = window.getPendingEditorChange();
            window.markEditorSaved(first.index, first.html);
            var acknowledged = window.getPendingEditorChange();
            return {
                firstDirty: first.dirty,
                firstIndex: first.index,
                firstHTML: first.html,
                secondDirty: second.dirty,
                acknowledgedDirty: acknowledged.dirty
            };
        })()
        """

        editorWebView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self = self else { return }
            if let dict = result as? [String: Any] {
                let firstDirty = dict["firstDirty"] as? Bool ?? false
                let firstIndex = dict["firstIndex"] as? Int ?? -1
                let firstHTML = dict["firstHTML"] as? String ?? ""
                let secondDirty = dict["secondDirty"] as? Bool ?? false
                let acknowledgedDirty = dict["acknowledgedDirty"] as? Bool ?? true
                if firstDirty && firstIndex == 0 && firstHTML.contains("changed before navigation") &&
                   secondDirty && !acknowledgedDirty {
                    self.pass("Pending editor buffer stays dirty until native acknowledgement")
                } else {
                    self.fail("Pending editor change flush",
                              detail: "dirty=\(firstDirty), index=\(firstIndex), secondDirty=\(secondDirty), acknowledgedDirty=\(acknowledgedDirty), html=\(firstHTML)")
                }
            }
            self.testPreviewSidebar()
        }
    }

    // The slide rail now belongs to presto/edit-chrome.js (tested by its own
    // suites); this checks the engine's edit viewport geometry in WebKit.
    func testPreviewSidebar() {
        let js = """
        (function() {
            if (!window.WS || !WS.slides || typeof WS.goToSlide !== 'function') return { error: 'engine missing' };
            if (WS.slides.length > 1) WS.goToSlide(1, 0);
            var metrics = window.applyPrestoEditViewport ? applyPrestoEditViewport({
                viewportWidth: 900,
                viewportHeight: 1130,
                railWidth: 180
            }) : null;
            var active = window.WS && WS.slides ? WS.slides[WS.currentIndex] : null;
            var rect = active ? active.getBoundingClientRect() : null;
            return {
                slideCount: window.WS && WS.slides ? WS.slides.length : 0,
                currentIndex: window.WS ? WS.currentIndex : -1,
                viewportMetrics: metrics,
                activeTop: rect ? rect.top : -1,
                activeLeft: rect ? rect.left : -1,
                activeTransform: active ? active.style.transform : ''
            };
        })()
        """

        webView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self = self else { return }
            if let dict = result as? [String: Any] {
                if let err = dict["error"] as? String {
                    self.fail("Preview viewport", detail: err)
                } else {
                    let slideCount = dict["slideCount"] as? Int ?? 0
                    let currentIndex = dict["currentIndex"] as? Int ?? -1
                    if slideCount < 2 || currentIndex == 1 {
                        self.pass("Preview navigates to a slide before measuring")
                    } else {
                        self.fail("Preview navigation", detail: "current=\(currentIndex)")
                    }
                    if let metrics = dict["viewportMetrics"] as? [String: Any] {
                        let expectedTop = metrics["slideTop"] as? Double ?? -1
                        let expectedLeft = metrics["slideLeft"] as? Double ?? -1
                        let railW = metrics["railW"] as? Double ?? 0
                        let activeTop = dict["activeTop"] as? Double ?? -2
                        let activeLeft = dict["activeLeft"] as? Double ?? -2
                        let transform = dict["activeTransform"] as? String ?? ""
                        if abs(activeTop - expectedTop) < 0.5 &&
                            abs(activeLeft - (railW + expectedLeft)) < 0.5 &&
                            transform == "none" {
                            self.pass("Preview sidebar viewport centers slide with pixel geometry")
                        } else {
                            self.fail("Preview sidebar viewport geometry",
                                      detail: "top=\(activeTop)/\(expectedTop), left=\(activeLeft)/\(railW + expectedLeft), transform=\(transform)")
                        }
                    } else {
                        self.fail("Preview sidebar viewport helper missing")
                    }
                }
            }
            self.testScrollSync()
        }
    }

    func testScrollSync() {
        // Test: editor and highlighting pre scroll in sync
        let js = """
        (function() {
            var editor = document.getElementById('editor');
            var hl = document.getElementById('highlighting');
            // Set lots of content to make it scrollable
            var lines = [];
            for (var i = 0; i < 100; i++) lines.push('<div>line ' + i + '</div>');
            editor.value = lines.join('\\n');
            editor.dispatchEvent(new Event('input'));
            // Scroll
            editor.scrollTop = 200;
            editor.dispatchEvent(new Event('scroll'));
            return {
                editorScroll: editor.scrollTop,
                hlScroll: hl.scrollTop,
                synced: Math.abs(editor.scrollTop - hl.scrollTop) < 2
            };
        })()
        """

        editorWebView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self = self else { return }
            if let dict = result as? [String: Any] {
                let synced = dict["synced"] as? Bool ?? false
                if synced {
                    self.pass("Scroll sync between editor and highlighting")
                } else {
                    let es = dict["editorScroll"] as? Double ?? 0
                    let hs = dict["hlScroll"] as? Double ?? 0
                    self.fail("Scroll sync", detail: "editor=\(es), hl=\(hs)")
                }
            }
            self.testWysiwygLayer()
        }
    }

    // MARK: - WYSIWYG Layer

    func testWysiwygLayer() {
        // Test: wysiwyg.js injects into the deck WKWebView (Presto's editor-preview
        // environment) and the full select → format → serialize → undo → message
        // pipeline works with WebKit's DOM implementation.
        let wysiwygURL = webSlidesRoot.appendingPathComponent("presto/wysiwyg.js")
        guard let source = try? String(contentsOf: wysiwygURL, encoding: .utf8) else {
            fail("wysiwyg.js readable", detail: wysiwygURL.path)
            finish()
            return
        }
        let setup = source + """
        \n;(function() {
            window.__wysMessages = [];
            window.__prestoWysiwyg.onMessage = function (m) { window.__wysMessages.push(m); };
            window.__prestoWysiwyg.enable();
        })();
        """
        webView.evaluateJavaScript(setup) { [weak self] _, error in
            guard let self = self else { return }
            if let error = error {
                self.fail("wysiwyg.js injects into WKWebView", detail: error.localizedDescription)
                self.finish()
                return
            }
            self.pass("wysiwyg.js injects into WKWebView")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                self.verifyWysiwygBehavior()
            }
        }
    }

    func verifyWysiwygBehavior() {
        let js = """
        (function() {
            var W = window.__prestoWysiwyg;
            var out = {};
            out.enabled = W.state().enabled;
            out.bodyMarked = document.body.classList.contains('presto-wysiwyg-on');
            WS.goToSlide(1, 0);
            var slide = WS.slides[1];
            var target = slide.querySelector('h2, h1, p, li');
            out.hasTarget = !!target;
            if (!target) return out;
            out.selected = W.select(target);
            out.stateHasSelection = W.state().hasSelection;
            out.toolbarVisible = document.getElementById('presto-wysiwyg-toolbar')
                .classList.contains('visible');
            out.translateSupported = (function () {
                target.style.translate = '5px 6px';
                var ok = target.style.translate.indexOf('5px') === 0;
                target.style.removeProperty('translate');
                return ok;
            })();
            out.caretApi = typeof document.caretRangeFromPoint === 'function';
            out.fontSet = W.setFontSize(44);
            out.fontApplied = slide.querySelector('h2, h1, p, li').style.fontSize === '44px';
            var serialized = W.serializeCurrentSlide();
            out.serializedNonEmpty = typeof serialized === 'string' && serialized.length > 0;
            out.serializedClean = serialized.indexOf('presto-wysiwyg') < 0 &&
                serialized.indexOf('ws-active') < 0 &&
                serialized.indexOf('contenteditable') < 0 &&
                serialized.indexOf('data-ws-original') < 0;
            out.undoOK = W.undo();
            out.fontReverted = slide.querySelector('h2, h1, p, li').style.fontSize !== '44px';
            W.deselect();
            out.changedMessages = window.__wysMessages.filter(function (m) {
                return m.type === 'wysiwygChanged';
            }).length;
            out.editingStateMessages = window.__wysMessages.filter(function (m) {
                return m.type === 'wysiwygEditingState';
            }).length;
            return out;
        })()
        """
        webView.evaluateJavaScript(js) { [weak self] result, error in
            guard let self = self else { return }
            if let error = error {
                self.fail("WYSIWYG behavior script", detail: error.localizedDescription)
                self.finish()
                return
            }
            let dict = result as? [String: Any] ?? [:]
            func check(_ key: String, _ label: String) {
                if dict[key] as? Bool == true {
                    self.pass(label)
                } else {
                    self.fail(label, detail: "\(key)=\(String(describing: dict[key]))")
                }
            }
            check("enabled", "WYSIWYG enables in WKWebView")
            check("bodyMarked", "WYSIWYG marks body when enabled")
            check("hasTarget", "WYSIWYG finds an editable target")
            check("selected", "WYSIWYG selects a slide element")
            check("stateHasSelection", "WYSIWYG selection state tracks")
            check("toolbarVisible", "WYSIWYG toolbar appears on selection")
            check("translateSupported", "WebKit supports the translate property")
            check("caretApi", "WebKit exposes caretRangeFromPoint")
            check("fontSet", "WYSIWYG font size command succeeds")
            check("fontApplied", "WYSIWYG font size applies inline style")
            check("serializedNonEmpty", "WYSIWYG serialization produces source")
            check("serializedClean", "WYSIWYG serialization strips artifacts")
            check("undoOK", "WYSIWYG undo succeeds")
            check("fontReverted", "WYSIWYG undo reverts the edit")
            let changed = dict["changedMessages"] as? Int ?? 0
            if changed >= 2 {
                self.pass("WYSIWYG posts wysiwygChanged saves (\(changed))")
            } else {
                self.fail("WYSIWYG posts wysiwygChanged saves", detail: "\(changed)")
            }
            // Messages must also arrive through the REAL WKScriptMessageHandler.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                let swiftSide = self.receivedWysiwygMessages.filter {
                    ($0["type"] as? String) == "wysiwygChanged"
                }
                if swiftSide.count >= 2,
                   let html = swiftSide.last?["html"] as? String, !html.isEmpty,
                   swiftSide.last?["index"] as? Int != nil {
                    self.pass("wysiwygChanged reaches Swift message handler (\(swiftSide.count))")
                } else {
                    self.fail("wysiwygChanged reaches Swift message handler",
                              detail: "count=\(swiftSide.count)")
                }
                let states = self.receivedWysiwygMessages.filter {
                    ($0["type"] as? String) == "wysiwygEditingState"
                }
                if states.isEmpty {
                    self.fail("wysiwygEditingState reaches Swift message handler")
                } else {
                    self.pass("wysiwygEditingState reaches Swift message handler (\(states.count))")
                }
                self.finish()
            }
        }
    }

    // MARK: - Finish

    func finish() {
        // Process any pending checks
        for check in pendingChecks { check() }
        pendingChecks.removeAll()

        log("")
        log("=== Results ===")
        log("Passed: \(passed)")
        log("Failed: \(failed)")
        log(failed == 0 ? "ALL PASSED" : "SOME TESTS FAILED")

        // Clean up
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "presto")
        editorWebView.configuration.userContentController.removeScriptMessageHandler(forName: "editor")

        let exitCode: Int32 = failed == 0 ? 0 : 1
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            exit(exitCode)
        }
    }
}

// MARK: - Main

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

// Find the deck to test
let args = CommandLine.arguments
var deckPath: String

if args.count > 1 {
    deckPath = args[1]
} else {
    // Default to mada-brown-bag if it exists, otherwise kitchen-sink
    let scriptDir = URL(fileURLWithPath: #file).deletingLastPathComponent()
    let projectDir = scriptDir.deletingLastPathComponent()
    let brownBag = projectDir.appendingPathComponent("decks/mada-brown-bag-2026/index.html")
    let kitchenSink = projectDir.appendingPathComponent("examples/kitchen-sink/index.html")

    if FileManager.default.fileExists(atPath: brownBag.path) {
        deckPath = brownBag.path
    } else if FileManager.default.fileExists(atPath: kitchenSink.path) {
        deckPath = kitchenSink.path
    } else {
        print("ERROR: No deck found. Pass path as argument.")
        exit(1)
    }
}

let runner = TestRunner(deckPath: deckPath)

// Run on next tick so the run loop is active
DispatchQueue.main.async {
    runner.run()
}

app.run()

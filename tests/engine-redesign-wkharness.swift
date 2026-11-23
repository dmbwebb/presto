// System-WebKit harness for engine tests (the WebKit Presto uses, not
// Playwright's). Loads a page in an offscreen, never-activated window,
// evaluates --inject files after the load (as Presto injects its scripts),
// then runs --script as an async function body and prints "RESULT <value>".
//
//   engine-redesign-wkharness --url URL --script FILE [--inject FILE]...
//       [--width W] [--height H] [--read-access DIR] [--block-network]
//       [--timeout SECONDS]
//
// file:// URLs load with loadFileURL and allowFileAccessFromFileURLs, like
// Presto. --block-network installs a content rule list that blocks every
// http(s) request except 127.0.0.1 and localhost, so a page sees what it would
// see offline. Driven by tests/test_engine_redesign_katex_zoom.py and
// tests/test_engine_redesign_layout.py; compile with
//   swiftc -framework WebKit -framework Cocoa tests/engine-redesign-wkharness.swift -o wkharness
import AppKit
import WebKit

struct Options {
    var url: URL?
    var script = ""
    var injects: [String] = []
    var width = 1280.0
    var height = 720.0
    var readAccess: URL?
    var blockNetwork = false
    var timeout = 60.0
}

func fail(_ message: String) -> Never {
    print("ERROR \(message)")
    fflush(stdout)
    exit(1)
}

func parseOptions() -> Options {
    var o = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    func value() -> String {
        guard !args.isEmpty else { fail("missing value") }
        return args.removeFirst()
    }
    while !args.isEmpty {
        let flag = args.removeFirst()
        switch flag {
        case "--url": o.url = URL(string: value())
        case "--script":
            let path = value()
            guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { fail("cannot read \(path)") }
            o.script = text
        case "--inject":
            let path = value()
            guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { fail("cannot read \(path)") }
            o.injects.append(text)
        case "--width": o.width = Double(value()) ?? o.width
        case "--height": o.height = Double(value()) ?? o.height
        case "--read-access": o.readAccess = URL(fileURLWithPath: value(), isDirectory: true)
        case "--block-network": o.blockNetwork = true
        case "--timeout": o.timeout = Double(value()) ?? o.timeout
        default: fail("unknown argument \(flag)")
        }
    }
    if o.url == nil || o.script.isEmpty { fail("--url and --script are required") }
    return o
}

// Content-blocker regexes have no alternation, hence two exceptions.
let blockRules = """
[{"trigger": {"url-filter": "^https?://"}, "action": {"type": "block"}},
 {"trigger": {"url-filter": "^https?://127\\\\.0\\\\.0\\\\.1[:/]"}, "action": {"type": "ignore-previous-rules"}},
 {"trigger": {"url-filter": "^https?://localhost[:/]"}, "action": {"type": "ignore-previous-rules"}}]
"""

final class Harness: NSObject, WKNavigationDelegate {
    let options: Options
    var view: WKWebView!
    var window: NSWindow!
    init(_ options: Options) { self.options = options }

    func start() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.preferences.setValue(true, forKey: "allowFileAccessFromFileURLs")
        if options.blockNetwork {
            WKContentRuleListStore.default().compileContentRuleList(
                forIdentifier: "engine-redesign-offline-\(getpid())", encodedContentRuleList: blockRules
            ) { list, error in
                guard let list else { fail("content rule list: \(String(describing: error))") }
                config.userContentController.add(list)
                self.open(config)
            }
        } else {
            open(config)
        }
    }

    func open(_ config: WKWebViewConfiguration) {
        let frame = NSRect(x: 0, y: 0, width: options.width, height: options.height)
        view = WKWebView(frame: frame, configuration: config)
        window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: options.width, height: options.height),
                          styleMask: [.borderless], backing: .buffered, defer: false)
        window.ignoresMouseEvents = true
        window.contentView = view
        window.orderFrontRegardless() // offscreen; the app can never become active
        view.navigationDelegate = self
        let url = options.url!
        if url.isFileURL {
            view.loadFileURL(url, allowingReadAccessTo: options.readAccess ?? url.deletingLastPathComponent())
        } else {
            view.load(URLRequest(url: url))
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        Task { @MainActor in
            do {
                for source in self.options.injects {
                    _ = try await self.view.callAsyncJavaScript(source + "\n;return true;", arguments: [:], in: nil, contentWorld: .page)
                }
                let value = try await self.view.callAsyncJavaScript(self.options.script, arguments: [:], in: nil, contentWorld: .page)
                print("RESULT", value as? String ?? String(describing: value ?? "null"))
                fflush(stdout)
                exit(0)
            } catch {
                fail("javascript: \(error)")
            }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        fail("navigation: \(error)")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        fail("provisional navigation: \(error)")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        fail("web content process terminated")
    }
}

let options = parseOptions()
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let harness = Harness(options)
harness.start()
DispatchQueue.main.asyncAfter(deadline: .now() + options.timeout) { fail("timeout after \(options.timeout)s") }
app.run()

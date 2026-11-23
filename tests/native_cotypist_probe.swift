// Appended to the production CotypistEditing helper by test_native_cotypist.py.
// Uses real shipped editor pages and native WebKit, without global key posting,
// application activation, user files or a visible window.
import Cocoa
import WebKit

final class CotypistProbe: NSObject, WKScriptMessageHandler {
    var messages: [[String: Any]] = []
    var failures = 0
    var replacements = 0
    let root = URL(fileURLWithPath: CommandLine.arguments[1])
    let window = NSWindow(contentRect: NSRect(x: -4000, y: -4000, width: 900, height: 700),
                          styleMask: .borderless, backing: .buffered, defer: false)
    var web: WKWebView!

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        if let body = message.body as? [String: Any] { messages.append(body) }
    }
    func check(_ condition: Bool, _ name: String) {
        if !condition { print("FAIL: \(name)"); failures += 1 }
    }
    func wait(_ condition: () -> Bool, seconds: Double = 4) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        repeat {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.01))
        } while Date() < deadline
        return false
    }
    func js(_ script: String) -> Any? {
        var done = false
        var result: Any?
        web.evaluateJavaScript(script) { value, error in
            if let error { print("JS: \(error)") }
            result = value; done = true
        }
        _ = wait({ done })
        return result
    }
    func quoted(_ text: String) -> String {
        String(data: try! JSONSerialization.data(withJSONObject: [text]), encoding: .utf8)! + "[0]"
    }
    func page(_ url: URL) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.preferences.setValue(true, forKey: "allowFileAccessFromFileURLs")
        config.userContentController.addUserScript(WKUserScript(source: "window.probeErrors=[];window.addEventListener('error',e=>probeErrors.push(e.message));", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.userContentController.add(self, name: "editor")
        config.userContentController.add(self, name: "presto")
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 900, height: 700), configuration: config)
        // A wrapper matches Presto's nested split-view responder ancestry.
        let wrapper = NSView(frame: web.frame)
        wrapper.addSubview(web)
        window.contentView = wrapper
        window.orderBack(nil)
        web.loadFileURL(url, allowingReadAccessTo: URL(fileURLWithPath: "/"))
        check(wait({ self.web.url?.path == url.path && self.js("document.readyState === 'complete'") as? Bool == true }, seconds: 12), "page ready")
        _ = js("window.keydowns=0; document.addEventListener('keydown',()=>window.keydowns++); true")
    }
    func event(_ key: CGKeyCode, text: String, down: Bool = true, source: Int64? = nil,
               flags: CGEventFlags = []) -> NSEvent {
        let cg = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState),
                         virtualKey: key, keyDown: down)!
        cg.setIntegerValueField(.eventSourceUnixProcessID, value: source ?? Int64(getpid()))
        cg.flags = flags
        let chars = Array(text.utf16)
        cg.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: chars)
        return NSEvent(cgEvent: cg)!
    }
    func burst(deletes: Int, insertion: String) {
        var packets: [NSEvent] = []
        for _ in 0..<deletes {
            packets += [event(51, text: "\u{8}"), event(51, text: "\u{8}", down: false)]
        }
        packets += [event(49, text: insertion), event(49, text: insertion, down: false)]
        for packet in packets {
            guard CotypistEditing.apply(packet, in: window) else {
                print("FAIL: correction packet bypassed ordered native editing")
                exit(1)
            }
        }
    }
    func textarea(_ selector: String) {
        let cases: [(String, Int, Int, Int, String, String)] = [
            ("This is definately", 18, 18, 5, "itely", "This is definitely"),
            ("First line\nThis is definately", 29, 29, 5, "itely", "First line\nThis is definitely"),
            ("This is definately the result.", 18, 18, 5, "itely", "This is definitely the result."),
            ("A wrong word", 2, 7, 1, "correct", "A correct word"),
            ("A café", 6, 6, 1, "e", "A cafe"),
            ("A 👩🏽‍💻", 9, 9, 1, "person", "A person")
        ]
        for (before, start, end, deletes, insertion, after) in cases {
            _ = js("(()=>{let b=document.querySelector('\(selector)');b.value=\(quoted(before));b.focus();b.setSelectionRange(\(start),\(end));window.keydowns=0;return true})()")
            window.makeFirstResponder(web)
            messages = []
            burst(deletes: deletes, insertion: insertion)
            let read = "document.querySelector('\(selector)').value"
            check(wait({ self.js(read) as? String == after }), "\(selector) replacement: \(after)")
            check(js("window.keydowns") as? Int == 0, "correction bypasses async key interpretation")
            let caret = after.utf16.count - (before.utf16.count - end)
            check(js("document.querySelector('\(selector)').selectionStart") as? Int == caret, "\(selector) caret")
            if selector == "#editor" {
                _ = js("flushPendingChange(); true")
                check(wait({ self.messages.contains { ($0["type"] as? String == "editorChanged") && ($0["html"] as? String == after) } }), "source save receives corrected text")
            } else {
                check(!messages.contains { $0["type"] as? String == "refineRequested" }, "Refine correction never submits prompt")
            }
            check(js("(()=>{let b=document.querySelector('\(selector)');return document.execCommand('undo')&&b.value!==\(quoted(after))&&document.execCommand('redo')&&b.value===\(quoted(after))})()") as? Bool == true,
                  "\(selector) native Undo/Redo")
            replacements += 1
        }
        check(!CotypistEditing.apply(event(51, text: "\u{8}", source: 0), in: window), "ordinary backspace passes through")
        check(!CotypistEditing.apply(event(49, text: " ", source: 0), in: window), "ordinary space passes through")
        check(!CotypistEditing.apply(event(51, text: "\u{8}", flags: .maskCommand), in: window), "modified correction passes through")
        check(!CotypistEditing.apply(event(48, text: "\t"), in: window), "Tab passes through")
        check(!CotypistEditing.apply(event(49, text: "x"), in: nil), "absent window passes through")
        let native = NSTextView(frame: .zero)
        window.contentView?.addSubview(native)
        window.makeFirstResponder(native)
        check(!CotypistEditing.apply(event(49, text: "x"), in: window), "native text field is not intercepted")
        native.removeFromSuperview()
        web.addSubview(native)
        window.makeFirstResponder(native)
        native.setMarkedText("あ", selectedRange: NSRange(location: 1, length: 0),
                             replacementRange: NSRange(location: NSNotFound, length: 0))
        check(native.hasMarkedText() && !CotypistEditing.apply(event(49, text: "x"), in: window),
              "marked composition in web responder is not intercepted")
        native.removeFromSuperview()
        window.makeFirstResponder(web)
    }
    func run() {
        page(root.appendingPathComponent("presto/editor.html"))
        check(wait({ self.js("typeof setContent === 'function'") as? Bool == true }), "source editor initialized")
        _ = js("setContent('<section>', 'Original', '</section>', 0, 1, 1, true); true")
        textarea("#editor")
        textarea("#refine-input")

        // A temporary one-slide deck loads the actual engine and visual editor.
        let fixture = URL(fileURLWithPath: CommandLine.arguments[2])
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><script defer src="\(root.appendingPathComponent("engine/webslides.js").absoluteString)"></script>
        <script defer src="\(root.appendingPathComponent("presto/wysiwyg.js").absoluteString)"></script></head>
        <body><div class="ws-deck"><section class="slide"><p id="text">This is definately</p></section></div></body></html>
        """
        try! html.write(to: fixture, atomically: true, encoding: .utf8)
        page(fixture)
        check((js("window.probeErrors.length") as? Int) == 0, "visual scripts load without errors")
        check(wait({ self.js("!!window.__prestoWysiwyg && !!window.WS && WS.slides.length === 1") as? Bool == true }), "visual editor ready")
        _ = js("__prestoWysiwyg.enable();__prestoWysiwyg.beginTextEdit('#text');let s=getSelection(),r=document.createRange();r.selectNodeContents(document.querySelector('#text'));r.collapse(false);s.removeAllRanges();s.addRange(r);true")
        window.makeFirstResponder(web)
        messages = []
        burst(deletes: 5, insertion: "itely")
        check(wait({ self.js("document.querySelector('#text').textContent") as? String == "This is definitely" }), "visual contenteditable corrected")
        check(js("document.execCommand('undo') && document.execCommand('redo') && document.querySelector('#text').textContent==='This is definitely'") as? Bool == true,
              "visual native Undo/Redo")
        _ = js("__prestoWysiwyg.commitActiveSession();true")
        check(wait({ self.messages.contains { ($0["type"] as? String == "wysiwygChanged") && ($0["html"] as? String)?.contains("This is definitely") == true } }), "visual save receives corrected text")
        check(js("PrestoEdit.undo() && document.querySelector('#text').textContent==='This is definately' && PrestoEdit.redo() && document.querySelector('#text').textContent==='This is definitely'") as? Bool == true,
              "committed visual session uses unified Undo/Redo")
        replacements += 1
        window.orderOut(nil)
        print("COTYPIST_NATIVE: \(replacements) replacements, \(failures) failures")
        exit(failures == 0 ? 0 : 1)
    }
}

@main struct Main {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let probe = CotypistProbe()
        DispatchQueue.global().asyncAfter(deadline: .now() + 90) { exit(2) }
        probe.run()
    }
}

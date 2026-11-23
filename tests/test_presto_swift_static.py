#!/usr/bin/env python3
"""Static guarantees of the Presto app sources (presto/*.swift, app files only).

The app is split across Presto.swift (deck parsing, saving, merging, refine,
export), PrestoWindow.swift (the one window, modes, Decks, thumbnails),
PrestoPresent.swift (console, audience, one-screen show), PrestoInjection.swift
(scripts added to deck pages) and PrestoMenus.swift (menus, keys, messages).
Behaviour is tested in test_presto_transactions.swift and
test_presto_shell_modes.swift; these checks pin the structure those tests
cannot see.
"""
import plistlib
import re
import unittest
from pathlib import Path


PRESTO_DIR = Path(__file__).resolve().parents[1] / "presto"
PRESTO_SWIFT = PRESTO_DIR / "Presto.swift"
REFINE_SUBPROCESS_SWIFT = PRESTO_DIR / "RefineSubprocess.swift"
APP_SOURCES = ["Presto.swift", "PrestoWindow.swift", "PrestoPresent.swift", "PrestoInjection.swift", "PrestoMenus.swift"]
INFO_PLIST = PRESTO_DIR / "app" / "Presto.app" / "Contents" / "Info.plist"


def read(name: str) -> str:
    return (PRESTO_DIR / name).read_text(encoding="utf-8")


def all_sources() -> str:
    return "\n".join(read(name) for name in APP_SOURCES)


def block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin + len(start))]


class PrestoSwiftStaticTests(unittest.TestCase):
    def setUp(self):
        self.source = all_sources()
        self.core = read("Presto.swift")
        self.window = read("PrestoWindow.swift")
        self.present = read("PrestoPresent.swift")
        self.injection = read("PrestoInjection.swift")
        self.menus = read("PrestoMenus.swift")

    # MARK: Launch, window and appearance

    def test_app_delegate_is_retained_for_launchservices_startup(self):
        self.assertRegex(
            self.source,
            r"var\s+retainedAppDelegate\s*:\s*AppDelegate\?",
            "NSApplication.delegate is weak; Presto must retain AppDelegate outside app.delegate",
        )
        self.assertIn("retainedAppDelegate = delegate", self.source)
        self.assertIn("app.mainMenu = delegate.buildMainMenu()", self.source)

    def test_initial_launch_is_explicit_and_idempotent(self):
        self.assertIn("var didHandleInitialLaunch = false", self.source)
        self.assertIn("func handleInitialLaunch()", self.source)
        self.assertIn("DispatchQueue.main.async { self.handleInitialLaunch() }", self.source)
        self.assertNotIn("DispatchQueue.main.async { delegate.handleInitialLaunch() }", self.source)
        self.assertIn("guard !didHandleInitialLaunch else { return }", self.source)

    def test_app_is_forced_light(self):
        self.assertIn("NSApp.appearance = NSAppearance(named: .aqua)", self.source)
        info = plistlib.loads(INFO_PLIST.read_bytes())
        self.assertTrue(info["NSRequiresAquaSystemAppearance"])

    def test_no_dark_chrome_in_the_shell(self):
        for dark in ("#151515", "#202020", "#252526", "#1a1a1a", "rgba(20,20,20", "backgroundColor = .black"):
            self.assertNotIn(dark, self.source, dark)
        self.assertNotIn("presto-slide-rail", self.source, "the slide rail belongs to edit-chrome.js now")

    def test_main_window_is_retained_light_and_activated(self):
        make = block(self.window, "func makeMainWindow()", "// Swaps what fills the main window")
        self.assertIn("window.isReleasedWhenClosed = false", make)
        self.assertIn("window.toolbarStyle = .unified", make)
        self.assertIn("window.backgroundColor = .prestoCanvas", make)
        self.assertLess(make.index("mainWindow = window"), make.index("return window"))
        self.assertNotIn("contentView!", make)
        show = block(self.window, "@objc func showMainWindow()", "func makeMainWindow()")
        self.assertIn("NSRunningApplication.current.activate", show)
        self.assertIn(".activateAllWindows", show)

    def test_closing_the_window_keeps_presto_running(self):
        self.assertIn("func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {\n        return false", self.source)
        self.assertIn("func applicationShouldHandleReopen", self.source)
        should_close = block(self.window, "func windowShouldClose", "func closeMainWindow()")
        self.assertIn("guard presentPhase != .live else", should_close)
        self.assertIn("return false", should_close)
        close = block(self.window, "func closeMainWindow()", "func windowWillClose")
        self.assertIn("flushActiveEditor", close)
        self.assertLess(close.index("cleanupPresentation()"), close.index("self.mainWindow?.orderOut(nil)"))
        self.assertNotIn("NSApp.terminate", self.source)

    def test_all_managed_windows_disable_appkit_auto_release(self):
        self.assertEqual(
            2,
            self.source.count(".isReleasedWhenClosed = false"),
            "the main and audience windows must remain ARC-owned",
        )

    def test_documents_open_in_edit(self):
        info = plistlib.loads(INFO_PLIST.read_bytes())
        types = {t for doc in info["CFBundleDocumentTypes"] for t in doc["LSItemContentTypes"]}
        self.assertEqual(types, {"public.html", "public.folder"})
        open_block = block(self.core, "func application(_ application: NSApplication, open urls: [URL])", "func deckFile(for url: URL)")
        self.assertIn("pendingOpenURLs.append(contentsOf: urls)", open_block)
        self.assertIn("openDeck(url, in: .edit)", open_block)

    def test_web_views_are_inspectable_only_in_tests_or_on_request(self):
        make = block(self.window, "func makePrestoWebView", "func webViewDropActiveChanged")
        self.assertIn("#if PRESTO_TESTING", make)
        self.assertIn('environment["PRESTO_WEBVIEW_INSPECTABLE"] == "1"', make)
        self.assertIn("webView.isInspectable = inspectable", make)

    def test_drops_open_decks_and_never_navigate_a_web_view(self):
        drop = block(self.window, "final class PrestoWebView: WKWebView", "extension NSColor")
        self.assertIn("readObjects(\n            forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true])", drop)
        self.assertIn("override func performDragOperation", drop)
        self.assertIn("dropHandler?.openDeck(deck, in: .edit)", drop)
        self.assertIn("PrestoDecks.setDropActive", self.window)
        policy = block(self.injection, "decidePolicyFor navigationAction", "// MARK: - Edit Preview")
        self.assertIn("navigationAction.navigationType == .linkActivated", policy)
        self.assertIn("decisionHandler(.cancel)", policy)
        self.assertIn("NSWorkspace.shared.open(target)", policy)

    # MARK: Opening decks

    def test_deck_open_errors_return_to_decks(self):
        for name in ("func showDeckOpenError", "func showNoSlidesAlert", "func showDeckStructureAlert"):
            alert = block(self.core, name, "\n    }\n")
            self.assertIn("returnToDecksIfNoDeck()", alert, name)

    def test_deck_open_validates_before_state_mutation(self):
        self.assertIn("readDeckContentForOpening", self.source)
        self.assertIn("deckStructureIssues", self.source)
        self.assertIn("Invalid deck structure", self.source)
        self.assertIn("No slides found", self.source)
        self.assertIn("Unable to open deck", self.source)
        open_block = block(self.window, "@objc func openNewDeck", "func beginDeckSession")
        self.assertLess(open_block.index("readDeckContentForOpening(deckURL: url)"), open_block.index("cleanupPresentation()"))
        self.assertLess(open_block.index("validateDeckHasSlides(deckURL: url, content: content)"), open_block.index("cleanupPresentation()"))
        self.assertLess(open_block.index("cleanupPresentation()"), open_block.index("beginDeckSession(url"))
        session = block(self.window, "func beginDeckSession", "// Ends the deck session")
        self.assertIn("currentDeckURL = url", session)
        self.assertIn("deckLibrary.recordOpened(url)", session)

        validation = block(self.core, "func validateDeckHasSlides", "// MARK: - Section Extraction")
        self.assertLess(validation.index("deckStructureIssues"), validation.index("extractSections"))
        self.assertLess(validation.index("showDeckStructureAlert"), validation.index("extractSections"))
        helper = block(self.core, "func readDeckContentForOpening", "func validateDeckHasSlides")
        self.assertIn("catch", helper)
        self.assertNotIn("try? String(contentsOf: deckURL", helper)

    def test_switching_decks_flushes_first_and_never_replaces_a_live_talk(self):
        open_deck = block(self.window, "func openDeck(_ url: URL, in target: PrestoMode", "func switchOpenDeck")
        self.assertLess(open_deck.index("guard presentPhase != .live else"), open_deck.index("flushActiveEditor"))
        self.assertIn("if saved { self?.openNewDeck(url, in: target, goLive: goLive) }", open_deck)

    def test_recent_decks_come_from_the_deck_library(self):
        self.assertNotIn('UserDefaults.standard.array(forKey: "recentDecks")', self.source)
        self.assertIn("DeckLibrary(storeURL: DeckLibrary.defaultStoreURL(), registryURL: DeckLibrary.defaultRegistryURL())", self.source)
        self.assertIn("func menuNeedsUpdate(_ menu: NSMenu)", self.menus)
        self.assertIn("openDeck(url, in: .edit)", block(self.menus, "@objc func openRecentDeck", "var editTextContextActive"))
        self.assertIn("panel.allowedContentTypes = [.html, .folder]", self.window)
        self.assertIn("@objc func openDeckFromMenu(_ sender: Any?) {\n        openDeckFileInMode(editMode: true)", self.window)

    # MARK: Modes

    def test_leaving_edit_flushes_and_edit_views_survive_presenting(self):
        leave = block(self.window, "func leaveCurrentMode", "// Where Present starts")
        self.assertIn("flushActiveEditor", leave)
        self.assertIn("if saved { self.recordDeckPosition", leave)
        go_live = block(self.present, "@objc func goLive", "func startTwoScreenShow")
        self.assertNotIn("cleanupPresentation", go_live)
        self.assertNotIn("previewWebView = nil", go_live)
        teardown = block(self.present, "func finishPresentationTeardown()", "func teardownAudience()")
        self.assertNotIn("previewWebView = nil", teardown)
        self.assertIn('navigateEditorPreview(command: "WS.goToSlide(\\(index), \\(step))")', teardown)
        self.assertIn("showMode(.decks)", teardown)

    def test_mode_control_and_shortcuts(self):
        self.assertIn('NSSegmentedControl(labels: ["Decks", "Edit", "Present"]', self.window)
        enabled = block(self.window, "func modeEnabled", "// A mode change asked for by the user")
        self.assertIn("case .edit: return currentDeckURL != nil && presentPhase != .live", enabled)
        self.assertIn("case .decks: return presentPhase != .live", enabled)
        for title, key in (("Decks", "1"), ("Edit", "2"), ("Present", "3")):
            self.assertRegex(self.menus, rf'add\(view, "{title}", #selector\(show{title}Mode\(_:\)\), "{key}"\)')

    def test_end_requests_are_deduplicated_before_async_teardown(self):
        request = block(self.present, "@objc func requestEndPresentation()", "func endPresentation()")
        self.assertIn("guard presentPhase != .none, !isEndingPresentation else { return }", request)
        self.assertIn("isEndingPresentation = true", request)
        self.assertLess(request.index("isEndingPresentation = true"), request.index("DispatchQueue.main.async"))
        self.assertIn("finishEndRequest(generation: generation)", request)
        self.assertIn("guard sessionGeneration == generation else {", request)
        self.assertNotIn("DispatchQueue.main.async { self.endPresentation() }", self.source)

    def test_cleanup_invalidates_async_session_callbacks(self):
        cleanup = block(self.window, "func cleanupPresentation()", "@objc func openDeckFromMenu")
        self.assertIn("guard flushPendingDeckWrite() else { return false }", cleanup)
        self.assertIn("sessionGeneration &+= 1", cleanup)
        self.assertIn("saveTransactionCount = 0", cleanup)
        self.assertIn('removeScriptMessageHandler(forName: "editor")', cleanup)

    # MARK: Presenting

    def test_audience_escape_key_is_bridged_to_native_guard(self):
        self.assertIn("window.webkit.messageHandlers.presto.postMessage({ type: 'endPresentationRequested' })", self.injection)
        handler = block(self.menus, "func handleAudienceMessage", "func handlePreviewMessage")
        start = handler.index('case "endPresentationRequested":')
        self.assertIn("handlePresentationEscape()", handler[start : start + 200])
        go_live = block(self.present, "@objc func goLive", "func startTwoScreenShow")
        self.assertIn('config.userContentController.add(self, name: "presto")', go_live)
        self.assertIn('audience.configuration.userContentController.removeScriptMessageHandler(forName: "presto")', self.present)

    def test_no_global_key_monitor_can_end_a_talk_from_another_app(self):
        # A global monitor heard Escape typed into any app and ended the talk.
        self.assertNotIn("addGlobalMonitorForEvents", self.source)
        self.assertNotIn("globalKeyMonitor", self.source)
        self.assertIn("NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .keyUp])", self.source)

    def test_escape_has_menu_key_equivalent_fallback(self):
        self.assertIn('let endPresentationEscItem = NSMenuItem(title: "End Presentation"', self.source)
        self.assertIn('keyEquivalent: "\\u{1b}"', self.source)
        self.assertIn("endPresentationEscItem.keyEquivalentModifierMask = []", self.source)

    def test_presentation_escape_requires_double_press(self):
        self.assertIn("var lastPresentationExitEscape: Date?", self.source)
        self.assertIn("static let presentationExitConfirmWindow: TimeInterval = 2.0", self.source)
        escape = block(self.present, "func handlePresentationEscape()", "func requestPresentationExitWithConfirmation")
        # Appendix return stays a single press; otherwise the two-press gate.
        self.assertIn('navigateAudience(command: "WS.returnFromAppendix()")', escape)
        self.assertIn("self.requestPresentationExitWithConfirmation()", escape)
        self.assertNotIn("requestEndPresentation()", escape)
        self.assertIn("if let depth = self.finiteInt(result), depth > 0", escape)
        self.assertIn("self.audienceWebView === aWebView", escape)
        confirm = block(self.present, "func requestPresentationExitWithConfirmation", "var presentationHintWebView")
        self.assertIn("now.timeIntervalSince(last) < AppDelegate.presentationExitConfirmWindow", confirm)
        self.assertIn("Press Escape again to end the presentation", confirm)
        self.assertIn("in: presentationHintWebView", confirm)
        live = block(self.menus, "func handleLiveKey", "func handleReadyKey")
        escape_case = block(live, "case 53: // Escape", "return nil")
        self.assertIn("if !event.isARepeat {", escape_case)
        self.assertIn("handlePresentationEscape()", escape_case)
        menu_start = self.menus.index("@objc func escapeKeyFromMenu")
        self.assertIn("handlePresentationEscape()", self.menus[menu_start : menu_start + 300])
        self.assertIn("#selector(AppDelegate.escapeKeyFromMenu(_:)), keyEquivalent: \"\\u{1b}\"", self.source)

    def test_end_presentation_shortcut_only_while_live(self):
        end = block(self.menus, "@objc func endPresentationFromMenu", "// The hidden bare-Escape menu item")
        self.assertIn("guard presentPhase == .live else { return }", end)
        validate = block(self.menus, "func validateMenuItem", "// MARK: - Menu Actions")
        self.assertIn("case #selector(endPresentationFromMenu(_:)):\n            return live", validate)

    def test_audience_lets_link_and_button_clicks_reach_the_engine(self):
        self.assertIn(
            '"a, button, .ws-appendix-link, .appendix-links *, .header-links *, [data-ws-link]"', self.source)
        audience = self.injection[self.injection.index("func audienceCoreJS() -> String {"):]
        click = audience[audience.index("document.addEventListener('click'"):]
        self.assertLess(click.index("AppDelegate.audienceClickThroughSelector"), click.index("e.stopImmediatePropagation()"))
        self.assertIn("WS.on('navigate'", audience)
        self.assertIn("if (window.__prestoRemoteNavigation) return;", audience)
        self.assertIn("type: 'audienceNavigated'", audience)

    def test_presenter_navigation_drives_the_audience_without_echo(self):
        console = block(self.menus, "func handleConsoleMessage", "func handleAudienceMessage")
        self.assertIn('case "presenterNavigated":\n            handlePresenterNavigated(body)', console)
        audience = block(self.menus, "func handleAudienceMessage", "func handlePreviewMessage")
        self.assertIn('case "audienceNavigated":', audience)
        navigated = block(self.core, "func handlePresenterNavigated", "func recordAudienceState")
        self.assertIn("returnStack == lastAudienceReturnStack { return }", navigated)
        self.assertIn('navigateAudience(command: "WS.returnStack = \\(stackJSON); WS.goToSlide(\\(index), \\(step))")', navigated)
        nav = block(self.core, "func navigateAudience(command: String)", "func logNavigationState")
        self.assertIn("window.__prestoRemoteNavigation = true;", nav)
        self.assertIn("finally { window.__prestoRemoteNavigation = false; }", nav)

    def test_navigation_state_has_test_only_logging(self):
        self.assertIn("PRESTO_TEST_LOG_NAVIGATION", self.source)
        self.assertIn("logNavigationState", self.source)
        self.assertIn("PrestoTestNavigation:", self.source)
        logging = block(self.core, "func logNavigationState", "// MARK: - PDF Export")
        self.assertIn('let index = finiteInt(state["index"])', logging)
        self.assertIn('let step = finiteInt(state["step"])', logging)
        self.assertNotIn('state["index"] as? Int', logging)

    def test_presentation_navigation_javascript_errors_are_logged(self):
        sync = block(self.core, "func syncPresenterFromAudience", "func navigateAudience")
        self.assertIn("stateError", sync)
        self.assertIn("failed to read audience state", sync)
        self.assertIn("invalid audience state", sync)
        self.assertIn("updatePresenterFromAudienceStateJSON", sync)
        nav = block(self.core, "func navigateAudience", "func logNavigationState")
        self.assertIn("navigationError", nav)
        self.assertIn("audience navigation failed", nav)
        self.assertIn("invalid audience state after navigation", nav)
        update = block(self.core, "func updatePresenterFromAudienceStateJSON", "func syncPresenterFromAudience")
        self.assertIn("presenterError", update)
        self.assertIn("presenter update failed", update)
        self.assertIn("updatePresentationChrome()", update)

    def test_presentation_menu_exposes_navigation_actions(self):
        for title, action in (("Next Step", "nextStepFromMenu"), ("Previous Step", "previousStepFromMenu"),
                              ("First Slide", "firstSlideFromMenu"), ("Last Slide", "lastSlideFromMenu")):
            self.assertIn(f'"{title}", #selector({action}(_:))', self.menus)

    def test_alerts_stay_on_the_laptop_window(self):
        failure = block(self.core, "func webContentFailureWindow", "func showWebContentRecoveryFailure")
        self.assertIn("default: return mainWindow", failure)
        self.assertNotIn("audienceWindow", failure)
        merge = block(self.core, "func askExternalMergeDecision", "// MARK: - Three-Way Deck Merge")
        self.assertIn("if let window = mainWindow, window.isVisible", merge)

    def test_presenter_init_uses_json_js_string_literal(self):
        presenter = block(self.injection, "case .presenter:", "case .editorPreview:")
        self.assertIn("jsStringLiteral(deckURL.absoluteString)", presenter)
        self.assertNotIn("replacingOccurrences(of: \"'\"", presenter)

    def test_talk_length_lives_in_the_deck_library(self):
        talk = block(self.present, "func setTalkMinutes", "@objc func endFromToolbar")
        self.assertIn("deckLibrary.setTalkMinutes(deckURL, valid)", talk)
        self.assertIn('state["talkMinutes"] = currentDeckURL.flatMap { deckLibrary.talkMinutes($0) }', self.present)

    # MARK: Editing

    def test_escape_in_edit_never_leaves_the_editor(self):
        self.assertNotIn("func requestEditorExitWithConfirmation", self.source)
        self.assertNotIn("lastEditorExitEscape", self.source)
        edit = block(self.menus, "func handleEditKey", "func handleEditEscape()")
        escape = block(edit, "if event.keyCode == 53 { // Escape", "}")
        self.assertIn("handleEditEscape()", escape)
        self.assertNotIn("requestEndPresentation", edit)
        self.assertIn("PrestoEdit.handleEscape", self.menus)
        self.assertIn("ws-overview-active", self.menus)
        handler = block(self.menus, "func handleEditEscape()", "static let editEscapeJS")
        self.assertIn("let generation = sessionGeneration", handler)
        self.assertIn("self.sessionGeneration == generation", handler)
        self.assertIn("self.previewWebView === webView", handler)

    def test_unified_undo_routing(self):
        self.assertIn('undoMenuItem = add(edit, "Undo", #selector(undoEdit(_:)), "z")', self.menus)
        self.assertNotIn('Selector(("undo:")), keyEquivalent', self.source)
        route = block(self.menus, "func routeHistory", "func applyUndoState")
        self.assertIn("allowTextUndo && inspectorTextUndoActive", route)
        self.assertIn("sourcePaneIsFirstResponder && (sourceEditorDirty || refineTextEditing)", self.menus)
        self.assertIn("PrestoEdit.\\(call)", route)
        self.assertLess(route.index("pendingEditorChangeJS()"), route.index("run()\n            }"))
        edit = block(self.menus, "func handleEditKey", "func handleEditEscape()")
        self.assertIn('character == "z"', edit)
        self.assertLess(edit.index('character == "z"'), edit.index("wysiwygTextEditingActive || wysiwygHasSelection"))
        messages = block(self.menus, "func handleEditorMessage", "\n}\n")
        self.assertIn('case "refineUndo":\n            // Refine\'s Undo last is the unified history\'s undo.\n            routeHistory(redo: false', messages)

    def test_refine_results_join_the_unified_history_with_version_checks(self):
        self.assertNotIn("func undoRefine", self.source)
        self.assertNotIn("refine_history.json", self.source)
        refine = block(self.core, "func applyRefineResult", "func applyRefineError")
        self.assertIn('RecordedSlideChange(origin: "refine", beforeHTML: preHTML,', refine)
        self.assertIn("prompt: instructions", refine)
        self.assertIn("sectionInfos[slideIndex].content == preHTML", refine)
        history = block(self.core, "func applyHistorySection", "func reportHistoryRefusal")
        self.assertIn("guard expected == nil || sectionInfos[index].content == expected", history)
        self.assertIn("This slide changed since; nothing was undone", history)
        self.assertLess(history.index("deckStructureIssues(in: content).isEmpty"), history.index("scheduleDeckWrite()"))
        patch = AppDelegateScripts(self.injection).slide_patch
        self.assertIn("window.PrestoEdit.recordExternalChange({", patch)
        self.assertIn("} else if (origin !== 'history') {", patch)

    def test_refine_is_command_j_and_command_i_stays_italic(self):
        self.assertIn('add(edit, "Refine with AI…", #selector(refineWithAI(_:)), "j")', self.menus)
        self.assertNotRegex(self.menus, r'#selector\(refineWithAI\(_:\)\), "i"')
        self.assertIn("prestoRefine.focus", self.menus)

    def test_editor_save_failure_is_reported_to_editor_not_confirmed(self):
        changed = block(self.core, "func handleEditorChanged", "func applyHistorySection")
        self.assertIn("-> Bool", changed)
        self.assertIn("Cannot apply editor change", changed)
        self.assertLess(changed.index("Cannot apply editor change"), changed.index("scheduleDeckWrite()"))
        self.assertIn("pendingEditorSaveID = saveId", changed)
        self.assertNotIn("saveConfirmed(", changed)
        self.assertIn("saveId: Int? = nil", changed)
        flush = block(self.core, "func flushPendingDeckWrite", "func cancelPendingDeckWrite")
        self.assertIn("if let error = writeDeckContent(content", flush)
        self.assertIn("reportEditorSaveFailed", flush)
        self.assertIn("__prestoWysiwyg.saveFailed", flush)

    def test_editor_save_rejects_changes_that_alter_slide_boundaries_before_writing(self):
        changed = block(self.core, "func handleEditorChanged", "func applyHistorySection")
        self.assertIn("let updatedSections = extractSections(from: content)", changed)
        self.assertIn('"Editor change altered slide boundaries"', changed)
        self.assertLess(changed.index("updatedSections.count == sectionInfos.count"), changed.index("scheduleDeckWrite()"))
        self.assertIn('RecordedSlideChange(origin: "source", beforeHTML: before)', changed)

    def test_editor_navigation_stops_when_pending_save_fails(self):
        self.assertIn("func applyPendingEditorChangePayload(_ result: Any?) -> PendingEditorChangeResult", self.source)
        for start, end in (("func flushEditorThenNavigate", "func flushEditorThenRunPreviewCommand"),
                           ("func flushEditorThenRunPreviewCommand", "// MARK: - AI Refine")):
            nav = block(self.core, start, end)
            self.assertIn("let pending = self.applyPendingEditorChangePayload(result)", nav)
            self.assertIn("guard pending.ok else { return }", nav)
            self.assertLess(nav.index("guard pending.ok else { return }"), nav.index("pWV.evaluateJavaScript"))
            self.assertNotIn("reloadPreviewAtSlide", nav)
            self.assertIn("if wysiwygTextEditingActive { commitActiveWysiwygSession(proceed) } else { proceed() }", nav)
            self.assertIn("self.previewWebView === pWV", nav)

    def test_editor_navigation_patches_preview_without_full_reload(self):
        helper = block(self.core, "func previewNavigationJS", "func refreshPreviewSlide")
        self.assertIn("previewSlidePatchStatement(index: savedIndex, change: change)", helper)
        self.assertIn("patchOK: window.__prestoLastPatchOK !== false", helper)
        nav = block(self.core, "func flushEditorThenNavigate", "// MARK: - AI Refine")
        self.assertIn('previewNavigationJS(command: "WS.goToSlide(\\(index), \\(step))", savedIndex: pending.savedIndex,', nav)
        self.assertIn("previewNavigationJS(command: command, savedIndex: pending.savedIndex,", nav)
        self.assertNotIn("loadFileURL", nav)

    def test_pending_editor_apply_marks_javascript_buffer_clean_after_successful_write(self):
        pending = block(self.core, "func applyPendingEditorChangePayload", "func flushEditorThenNavigate")
        self.assertIn("let saved = handleEditorChanged", pending)
        self.assertIn("guard saved else { return .failed }", pending)
        self.assertLess(pending.index("guard saved else { return .failed }"), pending.index("markEditorSaved"))
        self.assertIn("jsStringLiteral(body)", pending)
        self.assertIn("return .saved(index: editorIndex", pending)
        self.assertIn('let editorIndex = finiteInt(payload["index"])', pending)
        self.assertNotIn('payload["index"] as? Int', pending)

    def test_editor_preview_navigation_result_uses_strict_integer_decoding(self):
        result = block(self.core, "func handleEditorPreviewNavigationResult", "func applyPendingEditorChangePayload")
        self.assertIn("targetIndex: Int? = finiteInt(result)", result)
        self.assertIn('targetIndex = finiteInt(payload["index"])', result)
        self.assertIn("reloadPreviewAtSlide(idx)", result)
        self.assertNotIn('payload["index"] as? Int', result)

    def test_script_messages_require_current_webview_and_expected_role(self):
        messages = self.menus[self.menus.index("func userContentController"):]
        for identity in ("message.webView === editorWebView", "message.webView === previewWebView",
                         "message.webView === consoleWebView", "message.webView === audienceWebView",
                         "message.webView === decksWebView"):
            self.assertIn(identity, messages)
        self.assertIn("discarded %@ message from stale webview", messages)
        self.assertIn("} else if fromPreview && isEditorMode {", messages)
        self.assertIn('if let index = finiteInt(body["index"])', messages)
        self.assertIn('let saveId = finiteInt(body["saveId"])', messages)
        self.assertNotIn('body["index"] as? Int', messages)
        self.assertNotIn('body["saveId"] as? Int', messages)
        slide_changed = block(messages, 'case "slideChanged":', 'case "sidebarNavigate":')
        self.assertLess(slide_changed.index("index >= 0, index < sectionInfos.count"),
                        slide_changed.index("currentEditorSlideIndex = index"))

    def test_html_source_toggle_flushes_each_edit_surface(self):
        show = block(self.window, "func showInspector", "func hideInspector()")
        self.assertLess(show.index("commitActiveWysiwygSession"), show.index("setEditorSourceSidebarVisible(true"))
        hide = block(self.window, "func hideInspector()", "// The inspector is 360 px by default")
        self.assertLess(hide.index("pendingEditorChangeJS()"), hide.rindex("setEditorSourceSidebarVisible(false)"))
        self.assertIn("guard pending.ok else { return }", hide)
        self.assertIn("refreshPreviewSlide(savedIndex", hide)
        self.assertIn("flushPendingDeckWrite()", hide)
        self.assertIn("window.focusEditor", self.source)

    def test_html_source_button_message_menu_and_shortcut_are_wired(self):
        self.assertIn('case "toggleSourceSidebar":', self.menus)
        self.assertIn('"HTML Source", #selector(toggleEditorSourceSidebarFromMenu(_:))', self.menus)
        self.assertIn("sourceSidebarItem.keyEquivalentModifierMask = [.command, .shift]", self.menus)
        edit = block(self.menus, "func handleEditKey", "func handleEditEscape()")
        self.assertIn('flags == [.command, .shift] && character == "x"', edit)
        self.assertIn("!eventTargetsEditorWebView(event)", edit)
        self.assertLess(edit.index('character == "x"'), edit.index("wysiwygTextEditingActive || wysiwygHasSelection"))
        self.assertLess(edit.rindex("eventTargetsEditorWebView(event)"), edit.index("case 124, 49, 121, 125"))

    def test_edit_uses_one_window_with_default_hidden_inspector(self):
        surfaces = block(self.window, "func ensureEditSurfaces()", "func makePrestoWebView")
        self.assertIn("let splitView = NSSplitView", surfaces)
        self.assertIn("splitView.addArrangedSubview(previewWV)", surfaces)
        self.assertIn("splitView.addArrangedSubview(editorWV)", surfaces)
        self.assertIn("editorWV.isHidden = true", surfaces)
        self.assertIn("editorSidebarVisible = false", surfaces)
        self.assertIn("editorSplitView = splitView", surfaces)
        self.assertNotIn("var editorWindow", self.source)
        self.assertIn("setMainContent(editorSplitView)", self.window)

    def test_cmd_r_reload_is_handled_in_the_editor_preview(self):
        edit = block(self.menus, "func handleEditKey", "func handleEditEscape()")
        self.assertIn('flags == [.command] && character == "r"', edit)
        self.assertIn("reloadPresenter(nil)", edit)
        self.assertLess(edit.index('character == "r"'), edit.index("while the WYSIWYG layer owns the preview window"))
        helper = block(self.menus, "func reloadEditorPreview()", "@objc func undoEdit")
        self.assertIn("pendingEditorChangeJS()", helper)
        self.assertIn("applyPendingEditorChangePayload", helper)
        self.assertIn("reloadFromDisk(forcePreviewReload: true)", helper)
        self.assertIn("commitActiveWysiwygSession(proceed)", helper)
        reload = block(self.core, "func reloadFromDisk", "func applyDiskReload")
        self.assertIn("if forcePreviewReload", reload)
        self.assertIn("reloadPreviewAtSlide(currentEditorSlideIndex)", reload)

    def test_active_wysiwyg_session_is_committed_before_teardown_and_navigation(self):
        self.assertIn("func commitActiveWysiwygSession(_ completion: @escaping () -> Void)", self.source)
        flush = block(self.core, "func flushActiveEditor", "func webContentRole")
        self.assertIn("commitActiveSession()", flush)
        self.assertIn("flushSource()", flush)
        self.assertIn("pendingEditorChangeJS()", flush)
        self.assertIn("guard self.flushPendingDeckWrite()", flush)
        self.assertIn("DispatchQueue.main.asyncAfter(deadline: .now() + 5", flush)
        self.assertIn("Changes have not been discarded", flush)
        resign = block(self.window, "func windowDidResignKey", "func windowDidResize")
        self.assertIn("wysiwygTextEditingActive", resign)
        self.assertIn("window === mainWindow", resign)
        self.assertIn("commitActiveWysiwygSession", resign)

    def test_deck_write_is_debounced_and_flushed_on_every_exit_path(self):
        for needle in (
            "func scheduleDeckWrite()",
            "func flushPendingDeckWrite()",
            "func cancelPendingDeckWrite()",
            "var pendingDeckWrite: DispatchWorkItem?",
            "var deckWriteDirty = false",
        ):
            self.assertIn(needle, self.source)
        changed = block(self.core, "func handleEditorChanged", "func applyHistorySection")
        self.assertIn("scheduleDeckWrite()", changed)
        self.assertNotIn("writeDeckContent(content", changed)
        cleanup = block(self.window, "func cleanupPresentation()", "@objc func openDeckFromMenu")
        self.assertIn("flushPendingDeckWrite()", cleanup)
        term_start = self.core.index("func applicationWillTerminate")
        self.assertIn("flushPendingDeckWrite()", self.core[term_start : term_start + 200])
        reload = block(self.core, "func reloadPreviewAtSlide", "func clampCurrentEditorSlideIndex")
        self.assertIn("flushPendingDeckWrite()", reload)
        self.assertIn("func scheduleEditorMirror()", self.source)
        self.assertIn("flushEditorMirror()", self.source)

    # MARK: Injected page scripts

    def test_injection_order_and_missing_file_status(self):
        inject = block(self.injection, "func injectEditPreview", "func finishEditInjection")
        order = [inject.index(f'"{name}"') for name in
                 ("ui-tokens.css", "edit-layout.js", "edit-chrome.css", "edit-chrome.js", "wysiwyg-guides.js", "wysiwyg.js")]
        self.assertEqual(order, sorted(order))
        self.assertLess(inject.index("wysiwygTextEditingActive = false"), inject.index('read("ui-tokens.css"'))
        finish = block(self.injection, "func finishEditInjection", "func requestUndoState")
        self.assertIn('"Visual editing unavailable: could not load "', finish)
        self.assertIn("updateToolbarState()", finish)
        self.assertNotIn("func zoomInjectionJS", self.source)
        self.assertNotIn("function applyPrestoViewportFromApp", self.source)

    def test_preview_layout_goes_through_presto_layout(self):
        core = block(self.injection, "func previewCoreJS", "// MARK: - Slide Patch")
        self.assertIn("window.PrestoLayout.apply(window.__prestoLayoutOptions)", core)
        self.assertIn("window.addEventListener('resize', prestoApplyLayout);", core)
        self.assertIn("window.PrestoEditChrome.install(opts)", core)
        self.assertNotIn("installPrestoEditSidebar", core, "the engine's rail is retired")
        start = block(self.injection, 'InjectedScript(name: "visual editor start"', "))")
        self.assertLess(start.index("__prestoStartEditChrome()"), start.index("__prestoWysiwyg.enable()"))
        self.assertIn("insetBottom: \\(AppDelegate.editStepStripHeight)", self.injection)
        self.assertIn("static let editRailWidth = 232", self.injection)
        audience = self.injection[self.injection.index("func audienceCoreJS() -> String {"):]
        self.assertIn("mode: 'audience', insetLeft: 0, insetRight: 0, insetTop: 0, insetBottom: 0, padding: 0", audience)
        self.assertIn("window.PrestoHUD.install({ oneScreen:", self.injection)

    def test_slide_patch_refreshes_thumbnail_then_falls_back(self):
        patch = AppDelegateScripts(self.injection).slide_patch
        thumb = block(patch, "function refreshPrestoThumbnail", "window.replacePrestoEditSlideHTML")
        order = [thumb.index(fragment) for fragment in (
            "PrestoEditChrome.refreshThumbnail(index)", "WS.updatePrestoEditSidebarThumbnail(index, replacement)",
            "window.updatePrestoEditSidebarThumbnail(index, replacement)", "WS.rebuildPrestoEditSidebar()")]
        self.assertEqual(order, sorted(order))
        self.assertIn("refreshPrestoThumbnail(index, replacement);", patch)

    def test_slide_patch_expands_reveal_shortcuts_before_counting_steps(self):
        patch = AppDelegateScripts(self.injection).slide_patch
        self.assertIn("if (typeof WS.applyRevealShortcuts === 'function')", patch)
        self.assertLess(patch.index("current.replaceWith(replacement)"), patch.index("WS.applyRevealShortcuts(replacement)"))
        self.assertLess(patch.index("WS.applyRevealShortcuts(replacement)"),
                        patch.index("var maxStep = parsePrestoMaxStep(replacement)"))

    def test_slide_patch_rerenders_katex_and_notifies_wysiwyg(self):
        patch = AppDelegateScripts(self.injection).slide_patch
        self.assertIn("window.renderMathInElement", patch)
        self.assertIn("window.__wsKatexOptions", patch)
        self.assertIn("__prestoWysiwyg.notifyExternalReplace(index)", patch)
        self.assertLess(patch.index("renderMathInElement"), patch.rindex("return true;"))
        self.assertLess(patch.index("renderMathInElement(replacement"), patch.index("window.__prestoApplyLayout()"))

    def test_embedded_javascript_uses_strict_numeric_parsing(self):
        self.assertNotIn("parseInt(", self.source)
        self.assertIn("Number.isSafeInteger", self.source)
        last = block(self.menus, "func lastSlideNavigationCommand", "@objc func nextStepFromMenu")
        self.assertIn("parsePrestoSlideIndex(raw)", last)
        self.assertIn("/^\\\\d+$/.test(raw)", last)
        self.assertIn("Number.isSafeInteger(n)", last)

    # MARK: Refine subprocess and export (unchanged)

    def test_refine_subprocess_timeout_has_forced_kill_fallback(self):
        helper = REFINE_SUBPROCESS_SWIFT.read_text(encoding="utf-8")
        refine = block(self.core, "func runClaudeRefineSubprocess", "func enqueueRefine")
        self.assertIn("func waitForRefineProcess", helper)
        self.assertIn("DispatchSemaphore", helper)
        self.assertIn("SIGKILL", helper)
        self.assertIn("process.processIdentifier", helper)
        self.assertIn("stdoutCapture.drain", helper)
        self.assertIn("stderrCapture.drain", helper)
        self.assertIn("inputWriter.write", helper)
        self.assertLess(helper.index("stdoutCapture.drain"), helper.index("waitForRefineProcess(process"))
        self.assertLess(helper.index("stderrCapture.drain"), helper.index("waitForRefineProcess(process"))
        self.assertNotIn("readDataToEndOfFile", helper)
        self.assertNotIn("process.waitUntilExit()", refine)

    def test_refine_completion_is_scoped_to_deck_session_and_task(self):
        queue = block(self.core, "func processRefineQueue", "func applyRefineResult")
        self.assertIn("let generation = sessionGeneration", queue)
        self.assertIn("let targetDeckURL = currentDeckURL", queue)
        self.assertGreaterEqual(queue.count("self.sessionGeneration == generation"), 2)
        self.assertGreaterEqual(queue.count("self.currentDeckURL == targetDeckURL"), 2)
        self.assertGreaterEqual(queue.count("self.refineRunningInfo[targetSlide]?.id == taskID"), 2)

    def test_export_link_collection_failures_abort_pdf_export(self):
        start = self.core.index('wv.evaluateJavaScript("JSON.stringify(WS.getExportLinks())"')
        links = self.core[start : self.core.index("let config = WKSnapshotConfiguration()", start)]
        self.assertIn("linksError", links)
        self.assertIn("failed to get page links", links)
        self.assertIn("Failed to read export links", links)
        self.assertIn("guard let json = linksResult as? String", links)
        self.assertIn("finishExport(success: false", links)

    def test_export_capture_callbacks_are_scoped_to_webview_and_generation(self):
        for func_name in ("startExportCapture", "prepareExportAndCapture", "captureNextExportPage"):
            start = self.core.index(f"func {func_name}")
            body = self.core[start : self.core.index("\n    func ", start + 1)]
            self.assertIn("exportWebView === wv", body, func_name)
            self.assertIn("exportGeneration == generation", body, func_name)

    def test_pdf_export_entry_points_share_an_explicit_mode_choice(self):
        self.assertIn('case "exportPDFRequested":\n            requestPDFExport()', self.menus)
        self.assertIn('add(file, "Export PDF…", #selector(exportPDF(_:)), "e")', self.menus)
        choose = block(self.core, "func choosePDFExportMode", "func prepareEditorForPDFExport")
        self.assertIn('alert.messageText = "Export PDF"', choose)
        self.assertIn('alert.addButton(withTitle: "Final Slides")', choose)
        self.assertIn('alert.addButton(withTitle: "Every Reveal Step")', choose)
        self.assertIn('alert.addButton(withTitle: "Cancel")', choose)
        self.assertIn("completion(nil)", choose)

    def test_pdf_export_request_flushes_edit_surfaces_and_prevents_duplicates(self):
        request = block(self.core, "func requestPDFExport()", "func choosePDFExportMode")
        self.assertIn("guard !exportRequestActive", request)
        self.assertIn("exportRequestActive = true", request)
        self.assertIn("setEditorPDFExportAvailable(false)", request)
        self.assertIn("prepareEditorForPDFExport", request)
        self.assertIn("guard let mode else", request)
        self.assertIn("self.releasePDFExportRequest()", request)
        flush = block(self.core, "func prepareEditorForPDFExport", "func beginPDFExport")
        self.assertIn("flushActiveEditor(completion: completion)", flush)
        begin = block(self.core, "func beginPDFExport", "func showExportProgress")
        self.assertIn("guard savePanel.runModal() == .OK", begin)
        self.assertIn("releasePDFExportRequest()", begin)

    def test_pdf_export_test_mode_is_deterministic_and_skips_the_choice_sheet(self):
        self.assertIn('environment["PRESTO_TEST_EXPORT_MODE"]', self.core)
        self.assertIn('case "final": return .final', self.core)
        self.assertIn('default: return .stepped', self.core)
        request = block(self.core, "func requestPDFExport()", "func choosePDFExportMode")
        self.assertLess(request.index("if let testMode = self.testPDFExportMode"), request.index("choosePDFExportMode"))
        self.assertIn("beginPDFExport(mode: testMode", request)

    def test_pdf_export_builds_stepped_and_final_page_plans_with_link_aliases(self):
        prepare = block(self.core, "func prepareExportAndCapture", "func captureNextExportPage")
        self.assertIn("switch self.exportMode", prepare)
        self.assertIn("case .stepped:", prepare)
        self.assertIn("case .final:", prepare)
        self.assertIn("slideSteps.append((slideIndex: idx, step: maxStep))", prepare)
        self.assertIn('pageMap["\\(idx),\\(step)"] = pageIndex', prepare)
        self.assertIn("self.exportPageMap = pageMap", prepare)

    def test_web_content_termination_retries_once_and_restores_role_state(self):
        terminate = block(self.core, "func webViewWebContentProcessDidTerminate", "// MARK: - Navigation Sync")
        self.assertIn("webContentRecoveryAttempts[role, default: 0]", terminate)
        self.assertIn("guard attempts < 1", terminate)
        self.assertIn("webView.reloadFromOrigin()", terminate)
        self.assertIn("restartExportAfterWebContentTermination", terminate)
        self.assertIn("showWebContentRecoveryFailure", terminate)
        self.assertIn("lastAudienceSlideIndex", self.source)
        self.assertIn("currentEditorStep", self.source)
        self.assertIn("PRESTO_TEST_EXPORT_OUTPUT", self.source)
        self.assertIn("PrestoTestExport: success=%d", self.source)

    def test_export_readiness_javascript_errors_abort_instead_of_polling_until_timeout(self):
        start = block(self.core, "func startExportCapture", "func prepareExportAndCapture")
        self.assertIn("readinessError", start)
        self.assertIn("failed while checking deck readiness", start)
        self.assertIn("Failed to check export readiness", start)
        self.assertLess(start.index("if let readinessError"), start.index("if result as? Bool == true"))

    def test_export_rejects_bad_output_paths_before_state_mutation(self):
        self.assertIn("func validateExportOutputURL", self.core)
        begin = block(self.core, "func beginPDFExport", "func showExportProgress")
        self.assertIn("validateExportOutputURL(outputURL, deckURL: deckURL)", begin)
        self.assertLess(begin.index("validateExportOutputURL"), begin.index("exportOutputURL = outputURL"))
        self.assertIn("Output path must not be the open deck", self.core)
        self.assertIn("Output path is a directory", self.core)
        validator = block(self.core, "func validateExportOutputURL", "func setEditorPDFExportAvailable")
        self.assertIn('pathExtension.lowercased() != "pdf"', validator)
        self.assertIn("Output path must be a PDF file", validator)

    def test_external_reload_read_failures_are_logged_and_retried(self):
        reload = block(self.core, "func reloadFromDisk", "// MARK: - Helpers")
        self.assertIn("var fileReloadFailureCount", self.core)
        self.assertIn("func scheduleReloadFromDiskRetry", self.core)
        self.assertIn("catch", reload)
        self.assertIn("NSLog(\"Presto: failed to reload deck from disk", reload)
        self.assertIn("scheduleReloadFromDiskRetry()", reload)
        self.assertIn("fileReloadFailureCount = 0", reload)
        self.assertNotIn("try? String(contentsOf: deckURL", reload)

    def test_pdf_export_aborts_on_snapshot_failure_or_missing_pages(self):
        prepare = block(self.core, "func prepareExportAndCapture", "func captureNextExportPage")
        self.assertIn("guard let idx = finiteInt(dict[\"index\"])", prepare)
        self.assertIn("let maxStep = finiteInt(dict[\"maxStep\"])", prepare)
        self.assertIn("Invalid export slide info", prepare)
        self.assertIn("Duplicate export page info", prepare)
        self.assertIn("for (expectedIdx, dict) in info.enumerated()", prepare)
        self.assertIn("idx == expectedIdx", prepare)
        capture = block(self.core, "func captureNextExportPage", "func buildExportPDF")
        self.assertIn("Snapshot failed", capture)
        self.assertLess(capture.index("finishExport(success: false"), capture.index("self.exportPageIndex += 1"))
        self.assertIn("Export PDF document is unavailable", capture)
        self.assertIn("makePDFPage(fromSnapshot: image)", capture)
        self.assertIn("Export navigation mismatch", capture)
        self.assertLess(capture.index("navError"), capture.index("takeSnapshot"))
        build = block(self.core, "func buildExportPDF", "func finishExport")
        self.assertIn("pageCount == exportSlideSteps.count", build)
        self.assertIn("Export link target is not in the export page plan", build)
        self.assertIn("Export link source page is not in the PDF document", build)
        self.assertIn("writePDFAtomically(pdfDoc: pdfDoc, outputURL: outputURL, expectedPageCount: pageCount)", build)
        self.assertNotIn("pdfDoc.write(to: outputURL)", build)

    def test_strict_number_decoding_accepts_json_zero_and_one_but_rejects_bools(self):
        decode = block(self.core, "func isBooleanValue", "func normalizedPDFLinkRect")
        self.assertIn("CFGetTypeID(number) == CFBooleanGetTypeID()", decode)
        self.assertIn("return value is Bool", decode)
        self.assertIn("if isBooleanValue(value) { return nil }", decode)

    # MARK: File watching and merging (unchanged)

    def test_file_watcher_cancel_handler_closes_its_own_descriptor(self):
        watcher = block(self.core, "func startFileWatcher()", "func handleFileChanged")
        cancel = watcher[watcher.index("source.setCancelHandler") : watcher.index("fileWatchSource = source")]
        self.assertIn("close(fd)", cancel)
        self.assertIn("self?.fileWatchFD == fd", cancel)
        release = watcher[watcher.index("func cancelFileWatchSource") : watcher.index("func stopFileWatcher")]
        self.assertLess(release.index("fileWatchFD = -1"), release.index("source.cancel()"))

    def test_file_watcher_survives_atomic_replacement(self):
        watcher = block(self.core, "func startFileWatcher()", "func handleFileChanged")
        self.assertIn("eventMask: [.write, .extend, .rename, .delete, .revoke]", watcher)
        self.assertIn("AppDelegate.fileWatchInvalidatingEvents", watcher)
        write = block(self.core, "func writeDeckContent", "// MARK: - Debounced Disk Write")
        self.assertLess(write.index("content.write(to: deckURL, atomically: true"),
                        write.index("rearmFileWatcherAfterOwnWrite()"))

    def test_disk_reload_refreshes_edit_and_presentation_views(self):
        apply = block(self.core, "func applyDiskReload", "func reloadPresentationViews")
        self.assertLess(apply.index("reloadPresentationViews()"), apply.index("if !isEditorMode {"))
        self.assertIn("reloadPreviewAtSlide(currentEditorSlideIndex)", apply)

    def test_external_change_during_active_session_prompts_instead_of_discarding(self):
        reload = block(self.core, "func reloadFromDisk", "func scheduleReloadFromDiskRetry")
        self.assertIn("(wysiwygTextEditingActive || sourceEditorDirty || deckWriteDirty) && !isPresentingExternalConflict", reload)
        self.assertIn("presentExternalChangeConflict(diskContent: content)", reload)
        conflict = block(self.core, "func presentExternalChangeConflict", "func scheduleReloadFromDiskRetry")
        self.assertNotIn("Keep Editing", self.source)
        self.assertIn("mergeDeckVersions(base: base, ours: ours, theirs: theirs, preferOurs: true)", conflict)
        for title in ('"Keep My Versions"', '"Use Disk Versions"', '"Reload from Disk"'):
            self.assertIn(title, self.core)
        self.assertIn("beginSheetModal", conflict)
        self.assertLess(conflict.index("if wysiwygTextEditingActive {"), conflict.index("collectLocalEditsForMerge"))

    def test_stale_navigation_callbacks_require_current_webview_and_session(self):
        for function_name, identity_check in (
            ("func syncPresenterFromAudience", "self.audienceWebView === aWebView"),
            ("func navigateAudience", "self.audienceWebView === aWebView"),
            ("func flushEditorThenNavigate", "self.editorWebView === eWV"),
            ("func flushEditorThenRunPreviewCommand", "self.editorWebView === eWV"),
        ):
            start = self.core.index(function_name)
            body = self.core[start : self.core.index("\n    func ", start + len(function_name))]
            self.assertIn("sessionGeneration == generation", body)
            self.assertIn(identity_check, body)

    def test_extract_sections_precomputes_comment_ranges_off_the_hot_path(self):
        self.assertIn("func commentRanges(in html: String) -> [Range<String.Index>]", self.core)
        matching = block(self.core, "func matchingSectionEnd", "func extractSections")
        self.assertIn('token.name != "section"', matching)
        self.assertLess(matching.index('token.name != "section"'), matching.index("ranges: comments"))
        self.assertNotIn("isInsideHTMLComment(token.start", matching)
        extract = block(self.core, "func extractSections(from html: String)", "// MARK: - JS String Escaping")
        self.assertIn("let comments = commentRanges(in: html)", extract)
        self.assertIn("comments: comments", extract)
        content = block(self.core, "func deckContentRange", "func deckStructureIssues")
        self.assertIn("let comments = commentRanges(in: html)", content)
        self.assertLess(content.index("token.name != deckTagName"), content.rindex("ranges: comments"))

    def test_menu_items_are_validated(self):
        self.assertIn("extension AppDelegate: NSMenuItemValidation, NSMenuDelegate", self.menus)
        validate = block(self.menus, "func validateMenuItem", "// MARK: - Menu Actions")
        for action in ("showEditMode", "playFromCurrentSlide", "nextStepFromMenu", "toggleBlackScreen",
                       "editEquation", "undoEdit", "exportPDF", "swapDisplays"):
            self.assertIn(f"#selector({action}(_:))", validate, action)
        self.assertIn('return editing && selectionKind == "equation"', validate)


class AppDelegateScripts:
    """The slide patch JavaScript held in PrestoInjection.swift."""

    def __init__(self, injection: str):
        start = injection.index("static let slidePatchJS = \"\"\"")
        self.slide_patch = injection[start : injection.index('"""\n', start + 40)]


if __name__ == "__main__":
    unittest.main()

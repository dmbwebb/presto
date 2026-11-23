import Cocoa
import WebKit

// The menu bar (validated item by item), the keyboard routing that has to
// run before the web views see a key, and the script messages from every
// Presto page.

enum CotypistEditing {
    // Cotypist's characters are already resolved. Going through WebKit's
    // asynchronous key interpretation can insert them before its preceding
    // backspaces finish, so use ordered native editing commands instead.
    static func apply(_ event: NSEvent, in window: NSWindow?) -> Bool {
        guard event.type == .keyDown || event.type == .keyUp,
              event.modifierFlags.intersection([.command, .control, .option, .shift]).isEmpty,
              event.keyCode == 51 || event.keyCode == 49,
              let source = event.cgEvent?.getIntegerValueField(.eventSourceUnixProcessID),
              source > 0, source <= Int64(Int32.max),
              NSRunningApplication(processIdentifier: pid_t(source))?.bundleIdentifier == "app.cotypist.Cotypist",
              let responder = window?.firstResponder as? NSView,
              let client = responder as? NSTextInputClient,
              !client.hasMarkedText() else { return false }
        // Edit mode contains two web views inside a split view, rather than
        // making a web view the window's root content view.
        var ancestor: NSView? = responder
        while let view = ancestor, !(view is WKWebView) { ancestor = view.superview }
        guard ancestor is WKWebView else { return false }
        if event.type == .keyUp { return true }
        if event.keyCode == 51 {
            responder.doCommand(by: NSSelectorFromString("deleteBackward:"))
        } else {
            guard let text = event.characters, !text.isEmpty else { return false }
            client.insertText(text, replacementRange: NSRange(location: NSNotFound, length: 0))
        }
        return true
    }
}

extension AppDelegate: NSMenuItemValidation, NSMenuDelegate {

    // MARK: - Menu Bar

    func buildMainMenu() -> NSMenu {
        let menuBar = NSMenu()
        func submenu(_ title: String) -> NSMenu {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let menu = NSMenu(title: title)
            item.submenu = menu
            menuBar.addItem(item)
            return menu
        }
        func add(_ menu: NSMenu, _ title: String, _ action: Selector?, _ key: String = "",
                 _ modifiers: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = modifiers
            // Presto's own commands target the delegate (and its validation);
            // standard ones (Cut, Close, Full Screen) go to the first responder.
            if let action, self.responds(to: action) {
                item.target = self
            }
            menu.addItem(item)
            return item
        }
        let arrow = { (key: Int) in String(Character(UnicodeScalar(UInt16(key))!)) }

        let appItem = NSMenuItem()
        menuBar.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        _ = add(appMenu, "About Presto", #selector(NSApplication.orderFrontStandardAboutPanel(_:)))
        appMenu.addItem(.separator())
        _ = add(appMenu, "Hide Presto", #selector(NSApplication.hide(_:)), "h")
        _ = add(appMenu, "Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option])
        _ = add(appMenu, "Show All", #selector(NSApplication.unhideAllApplications(_:)))
        appMenu.addItem(.separator())
        _ = add(appMenu, "Quit Presto", #selector(NSApplication.terminate(_:)), "q")

        let file = submenu("File")
        _ = add(file, "Open…", #selector(openDeckFromMenu(_:)), "o")
        let recentItem = NSMenuItem(title: "Open Recent", action: nil, keyEquivalent: "")
        let recentMenu = NSMenu(title: "Open Recent")
        recentMenu.delegate = self
        recentItem.submenu = recentMenu
        file.addItem(recentItem)
        openRecentMenu = recentMenu
        _ = add(file, "Add Folder to Decks…", #selector(addFolderToDecks(_:)))
        file.addItem(.separator())
        _ = add(file, "Export PDF…", #selector(exportPDF(_:)), "e")
        file.addItem(.separator())
        _ = add(file, "Close Window", #selector(NSWindow.performClose(_:)), "w")

        let edit = submenu("Edit")
        undoMenuItem = add(edit, "Undo", #selector(undoEdit(_:)), "z")
        redoMenuItem = add(edit, "Redo", #selector(redoEdit(_:)), "z", [.command, .shift])
        edit.addItem(.separator())
        _ = add(edit, "Cut", #selector(NSText.cut(_:)), "x")
        _ = add(edit, "Copy", #selector(NSText.copy(_:)), "c")
        _ = add(edit, "Paste", #selector(NSText.paste(_:)), "v")
        _ = add(edit, "Select All", #selector(NSText.selectAll(_:)), "a")
        edit.addItem(.separator())
        _ = add(edit, "Add Text Box", #selector(addTextBox(_:)), "t", [.command, .option])
        _ = add(edit, "Edit Equation", #selector(editEquation(_:)))
        _ = add(edit, "Refine with AI…", #selector(refineWithAI(_:)), "j")
        edit.addItem(.separator())
        _ = add(edit, "Find Deck…", #selector(focusDeckSearch(_:)), "f")

        let view = submenu("View")
        _ = add(view, "Decks", #selector(showDecksMode(_:)), "1")
        _ = add(view, "Edit", #selector(showEditMode(_:)), "2")
        _ = add(view, "Present", #selector(showPresentMode(_:)), "3")
        view.addItem(.separator())
        _ = add(view, "Show Slide Rail", #selector(toggleSlideRail(_:)), "s", [.command, .control])
        _ = add(view, "Show Inspector", #selector(toggleInspector(_:)), "i", [.command, .option])
        let sourceSidebarItem = add(view, "HTML Source", #selector(toggleEditorSourceSidebarFromMenu(_:)), "x")
        sourceSidebarItem.keyEquivalentModifierMask = [.command, .shift]
        _ = add(view, "Show Later Steps Faintly", #selector(toggleGhostSteps(_:)), "o", [.command, .shift])
        view.addItem(.separator())
        _ = add(view, "Reload", #selector(reloadPresenter(_:)), "r")
        view.addItem(.separator())
        _ = add(view, "Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control])

        let presentation = submenu("Presentation")
        _ = add(presentation, "Play from Start", #selector(playFromStart(_:)), "\r", [.command, .shift])
        _ = add(presentation, "Play from Current Slide", #selector(playFromCurrentSlide(_:)), "\r")
        presentation.addItem(.separator())
        _ = add(presentation, "Next Step", #selector(nextStepFromMenu(_:)), arrow(NSRightArrowFunctionKey), [])
        _ = add(presentation, "Previous Step", #selector(previousStepFromMenu(_:)), arrow(NSLeftArrowFunctionKey), [])
        _ = add(presentation, "First Slide", #selector(firstSlideFromMenu(_:)), arrow(NSHomeFunctionKey), [])
        _ = add(presentation, "Last Slide", #selector(lastSlideFromMenu(_:)), arrow(NSEndFunctionKey), [])
        presentation.addItem(.separator())
        _ = add(presentation, "Swap Displays", #selector(swapDisplays(_:)))
        _ = add(presentation, "Black Screen", #selector(toggleBlackScreen(_:)), "b", [])
        presentation.addItem(.separator())
        _ = add(presentation, "End Presentation", #selector(endPresentationFromMenu(_:)), ".")
        // Escape's menu path, hidden. Like the key, it goes through the
        // appendix return and the two-press guard.
        let endPresentationEscItem = NSMenuItem(title: "End Presentation", action: #selector(AppDelegate.escapeKeyFromMenu(_:)), keyEquivalent: "\u{1b}")
        endPresentationEscItem.keyEquivalentModifierMask = []
        endPresentationEscItem.isHidden = true
        endPresentationEscItem.target = self
        presentation.addItem(endPresentationEscItem)

        let window = submenu("Window")
        _ = add(window, "Minimize", #selector(NSWindow.miniaturize(_:)), "m")
        _ = add(window, "Zoom", #selector(NSWindow.zoom(_:)))
        window.addItem(.separator())
        _ = add(window, "Bring All to Front", #selector(NSApplication.arrangeInFront(_:)))
        NSApp?.windowsMenu = window
        return menuBar
    }

    // Open Recent is rebuilt each time it opens, from the deck library.
    func menuNeedsUpdate(_ menu: NSMenu) {
        guard menu === openRecentMenu else { return }
        menu.removeAllItems()
        let recents = recentDeckURLs()
        if recents.isEmpty {
            let empty = NSMenuItem(title: "No Recent Decks", action: nil, keyEquivalent: "")
            empty.isEnabled = false
            menu.addItem(empty)
            return
        }
        for recent in recents {
            let item = NSMenuItem(title: recent.title.isEmpty ? deckName(for: recent.url.path) : recent.title,
                                  action: #selector(openRecentDeck(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = recent.url
            item.toolTip = recent.url.path
            menu.addItem(item)
        }
    }

    @objc func openRecentDeck(_ sender: NSMenuItem) {
        guard let url = sender.representedObject as? URL else { return }
        openDeck(url, in: .edit)
    }

    var editTextContextActive: Bool {
        wysiwygTextEditingActive || wysiwygHasSelection || sourcePaneIsFirstResponder
    }

    // ⌘Z belongs to the inspector's own text: typing not yet committed in
    // the source pane, or anything in the Refine prompt.
    var inspectorTextUndoActive: Bool {
        sourcePaneIsFirstResponder && (sourceEditorDirty || refineTextEditing)
    }

    var sourcePaneIsFirstResponder: Bool {
        guard isEditorMode, editorSidebarVisible, let editorWV = editorWebView,
              var view = mainWindow?.firstResponder as? NSView else { return false }
        while view !== editorWV {
            guard let parent = view.superview else { return false }
            view = parent
        }
        return true
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        let live = presentPhase == .live
        let deckOpen = currentDeckURL != nil
        let editing = mode == .edit && isEditorMode && !live
        switch item.action {
        case #selector(showDecksMode(_:)):
            item.state = mode == .decks ? .on : .off
            return modeEnabled(.decks)
        case #selector(showEditMode(_:)):
            item.state = mode == .edit ? .on : .off
            return modeEnabled(.edit)
        case #selector(showPresentMode(_:)):
            item.state = mode == .present ? .on : .off
            return modeEnabled(.present)
        case #selector(toggleSlideRail(_:)):
            item.title = railVisible ? "Hide Slide Rail" : "Show Slide Rail"
            return editing
        case #selector(toggleInspector(_:)):
            item.title = editorSidebarVisible ? "Hide Inspector" : "Show Inspector"
            return editing
        case #selector(toggleEditorSourceSidebarFromMenu(_:)):
            item.state = editing && editorSidebarVisible ? .on : .off
            return editing
        case #selector(toggleGhostSteps(_:)):
            item.state = ghostFutureSteps ? .on : .off
            return editing
        case #selector(reloadPresenter(_:)):
            return deckOpen && !live
        case #selector(openDeckFromMenu(_:)), #selector(addFolderToDecks(_:)):
            return !live
        case #selector(openRecentDeck(_:)):
            guard let url = item.representedObject as? URL else { return false }
            return !live && FileManager.default.fileExists(atPath: url.path)
        case #selector(exportPDF(_:)):
            return deckOpen && !live && !exportRequestActive
        case #selector(undoEdit(_:)):
            if editing && inspectorTextUndoActive {
                item.title = "Undo Typing"
                return true
            }
            item.title = editUndoState.undoTitle
            return editing && editUndoState.canUndo
        case #selector(redoEdit(_:)):
            if editing && inspectorTextUndoActive {
                item.title = "Redo Typing"
                return true
            }
            item.title = editUndoState.redoTitle
            return editing && editUndoState.canRedo
        case #selector(addTextBox(_:)):
            return editing && editHistoryAvailable
        case #selector(editEquation(_:)):
            return editing && selectionKind == "equation"
        case #selector(refineWithAI(_:)):
            return editing
        case #selector(focusDeckSearch(_:)):
            return mode == .decks
        case #selector(playFromStart(_:)), #selector(playFromCurrentSlide(_:)):
            // In Decks, Return and ⌘Return belong to the page's cards; in a
            // visual text session or the inspector ⌘↩ belongs to the page
            // (equation Apply, Refine's send).
            if mode == .edit && (wysiwygTextEditingActive || sourcePaneIsFirstResponder) { return false }
            return deckOpen && !live && mode != .decks
        case #selector(nextStepFromMenu(_:)), #selector(previousStepFromMenu(_:)),
             #selector(firstSlideFromMenu(_:)), #selector(lastSlideFromMenu(_:)):
            // Disabled while a text field has the keys, so the bare arrow
            // and Home/End equivalents never steal them.
            if live { return !consoleTextEditing }
            if mode == .present { return presentPhase == .ready && !consoleTextEditing }
            return editing && !editTextContextActive
        case #selector(swapDisplays(_:)):
            return canSwapDisplays
        case #selector(toggleBlackScreen(_:)):
            item.state = blackoutActive ? .on : .off
            return live && !consoleTextEditing
        case #selector(endPresentationFromMenu(_:)):
            return live
        case #selector(escapeKeyFromMenu(_:)):
            return live && !consoleTextEditing
        default:
            return true
        }
    }

    // MARK: - Menu Actions

    @objc func showDecksMode(_ sender: Any?) { requestMode(.decks) }
    @objc func showEditMode(_ sender: Any?) { requestMode(.edit) }
    @objc func showPresentMode(_ sender: Any?) { requestMode(.present) }

    @objc func toggleSlideRail(_ sender: Any?) {
        guard mode == .edit, isEditorMode else { return }
        setRailVisible(!railVisible)
    }

    @objc func toggleInspector(_ sender: Any?) {
        guard mode == .edit, isEditorMode else { return }
        if editorSidebarVisible { hideInspector() } else { showInspector(focusSource: false) }
    }

    @objc func toggleEditorSourceSidebarFromMenu(_ sender: Any?) {
        guard mode == .edit else { return }
        toggleEditorSourceSidebar()
    }

    @objc func toggleGhostSteps(_ sender: Any?) {
        guard mode == .edit, isEditorMode else { return }
        setGhostFutureSteps(!ghostFutureSteps)
    }

    @objc func reloadPresenter(_ sender: Any?) {
        guard presentPhase != .live else { return }
        if isEditorMode && mode == .edit {
            reloadEditorPreview()
        } else {
            consoleWebView?.reload()
            decksWebView?.reload()
        }
    }

    func reloadEditorPreview() {
        guard isEditorMode, let eWV = editorWebView, let pWV = previewWebView else { return }
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
                guard self.flushPendingDeckWrite() else { return }
                self.reloadFromDisk(forcePreviewReload: true)
            }
        }
        commitActiveWysiwygSession(proceed)
    }

    @objc func undoEdit(_ sender: Any?) { routeHistory(redo: false) }
    @objc func redoEdit(_ sender: Any?) { routeHistory(redo: true) }

    // One ⌘Z for visual edits, committed source edits and Refine: the
    // preview's history (PrestoEdit). Typing not yet committed in the source
    // pane undoes there first, as in any text field.
    func routeHistory(redo: Bool, allowTextUndo: Bool = true) {
        guard mode == .edit, isEditorMode, presentPhase != .live, let preview = previewWebView else { return }
        if allowTextUndo && inspectorTextUndoActive {
            NSApp.sendAction(Selector((redo ? "redo:" : "undo:")), to: nil, from: self)
            return
        }
        let call = redo ? "redo" : "undo"
        let run: () -> Void = { [weak self] in
            preview.evaluateJavaScript(
                "(window.PrestoEdit && typeof PrestoEdit.\(call) === 'function') ? (PrestoEdit.\(call)(), true) : false"
            ) { result, _ in
                if result as? Bool != true { NSLog("Presto: no edit history to %@", call) }
                self?.requestUndoState()
            }
        }
        // Commit a pending source buffer first so it becomes the newest entry
        // instead of being overwritten by an older one.
        if sourceEditorDirty, let editorWV = editorWebView, editorReady {
            let generation = sessionGeneration
            editorWV.evaluateJavaScript(pendingEditorChangeJS()) { [weak self] result, _ in
                guard let self, self.sessionGeneration == generation else { return }
                let pending = self.applyPendingEditorChangePayload(result)
                guard pending.ok else { return }
                if let index = pending.savedIndex { self.refreshPreviewSlide(index, recording: pending.change) }
                run()
            }
        } else {
            run()
        }
    }

    func applyUndoState(_ body: [String: Any]) {
        editUndoState = EditUndoState(canUndo: (body["canUndo"] as? Bool) ?? false,
                                      canRedo: (body["canRedo"] as? Bool) ?? false,
                                      undoLabel: (body["undoLabel"] as? String) ?? "",
                                      redoLabel: (body["redoLabel"] as? String) ?? "")
        undoMenuItem?.title = editUndoState.undoTitle
        redoMenuItem?.title = editUndoState.redoTitle
        updateToolbarState()
        pushRefineStateToEditor()
    }

    @objc func addTextBox(_ sender: Any?) {
        guard mode == .edit, isEditorMode else { return }
        previewWebView?.evaluateJavaScript(
            "window.PrestoEdit && typeof PrestoEdit.addTextBox === 'function' && PrestoEdit.addTextBox()"
        ) { _, _ in }
    }

    @objc func editEquation(_ sender: Any?) {
        guard mode == .edit, isEditorMode else { return }
        previewWebView?.evaluateJavaScript(
            "!!(window.PrestoEdit && typeof PrestoEdit.editEquation === 'function' && PrestoEdit.editEquation())"
        ) { result, _ in
            if result as? Bool != true { NSSound.beep() }
        }
    }

    // ⌘J opens the inspector and puts the cursor in Refine (⌘I stays Italic).
    @objc func refineWithAI(_ sender: Any?) {
        guard mode == .edit, isEditorMode else { return }
        showInspector(focusSource: false) { [weak self] in
            guard let self, let editorWV = self.editorWebView else { return }
            self.mainWindow?.makeFirstResponder(editorWV)
            editorWV.evaluateJavaScript(
                "window.prestoRefine && typeof prestoRefine.focus === 'function' ? (prestoRefine.focus(), true) : (typeof window.focusRefine === 'function' ? (window.focusRefine(), true) : false)"
            ) { _, _ in }
        }
    }

    @objc func exportPDF(_ sender: Any?) {
        requestPDFExport()
    }

    func navigateEditorPreview(command: String) {
        guard let pWV = previewWebView else { return }
        pWV.evaluateJavaScript(command) { _, _ in }
    }

    func runPresentationNavigationCommand(_ command: String) {
        if presentPhase == .live {
            navigateAudience(command: command)
        } else if presentPhase == .ready {
            runConsolePreviewCommand(command)
        } else if isEditorMode && mode == .edit {
            flushEditorThenRunPreviewCommand(command)
        }
    }

    func lastSlideNavigationCommand() -> String {
        """
        (function(){
            // `WS` may be the console preview's local binding, not window.WS.
            if (typeof WS === 'undefined' || !WS || !WS.slides || !WS.slides.length) return;
            function parsePrestoSlideIndex(raw) {
                if (raw === undefined || raw === null || raw === '') return null;
                raw = String(raw).trim();
                if (!/^\\d+$/.test(raw)) return null;
                var n = Number(raw);
                return Number.isSafeInteger(n) ? n : null;
            }
            var target = WS.slides.length - 1;
            if (WS.mainSlides && WS.mainSlides.length > 0) {
                var raw = WS.mainSlides[WS.mainSlides.length - 1].dataset.wsIndex;
                var n = parsePrestoSlideIndex(raw);
                if (n !== null && n >= 0 && n < WS.slides.length) {
                    target = n;
                }
            }
            WS.goToSlide(target, 'end');
        })()
        """
    }

    @objc func nextStepFromMenu(_ sender: Any?) {
        runPresentationNavigationCommand("WS.nextStep()")
    }

    @objc func previousStepFromMenu(_ sender: Any?) {
        runPresentationNavigationCommand("WS.prevStep()")
    }

    @objc func firstSlideFromMenu(_ sender: Any?) {
        runPresentationNavigationCommand("WS.goToSlide(0, 0)")
    }

    @objc func lastSlideFromMenu(_ sender: Any?) {
        runPresentationNavigationCommand(lastSlideNavigationCommand())
    }

    // ⌘. is deliberate, so it ends the talk without the Escape guard; it
    // does nothing outside a live presentation (it used to close the editor).
    @objc func endPresentationFromMenu(_ sender: Any?) {
        guard presentPhase == .live else { return }
        requestEndPresentation()
    }

    // The hidden bare-Escape menu item: in presentation mode it goes through
    // the same appendix-return and two-press guard as the Escape key, and it
    // never leaves the editor.
    @objc func escapeKeyFromMenu(_ sender: Any?) {
        guard presentPhase == .live else { return }
        handlePresentationEscape()
    }

    // MARK: - Keyboard

    // Presto deliberately has no global key monitor: one heard Escape in every
    // app and could end a talk from a browser demo, bypassing any guard.
    func setupKeyboardMonitor() {
        if let monitor = keyMonitor {
            NSEvent.removeMonitor(monitor)
        }

        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .keyUp]) { [weak self] event in
            guard let self = self else { return event }
            if let eventWindow = event.window {
                guard eventWindow === self.mainWindow ||
                      eventWindow === self.audienceWindow else {
                    return event
                }
            } else if self.mainWindow == nil && self.audienceWindow == nil {
                return event
            }
            if CotypistEditing.apply(event, in: event.window) { return nil }
            guard event.type == .keyDown else { return event }
            return self.handleKey(event)
        }
    }

    func eventFromAudienceWindow(_ event: NSEvent) -> Bool {
        guard let window = event.window else { return false }
        return window === audienceWindow
    }

    func eventTargetsEditorWebView(_ event: NSEvent) -> Bool {
        guard event.window === mainWindow else { return false }
        return sourcePaneIsFirstResponder
    }

    static func keyCharacter(_ event: NSEvent) -> String {
        (event.charactersIgnoringModifiers ?? "").lowercased()
    }

    func handleKey(_ event: NSEvent) -> NSEvent? {
        let flags = event.modifierFlags.intersection([.command, .shift, .option, .control])
        if presentPhase == .live { return handleLiveKey(event, flags: flags) }
        // ⌘1-⌘3 switch modes from any surface.
        if flags == [.command], let target = ["1": PrestoMode.decks, "2": .edit, "3": .present][AppDelegate.keyCharacter(event)] {
            requestMode(target)
            return nil
        }
        switch mode {
        case .edit where isEditorMode:
            return handleEditKey(event, flags: flags)
        case .present:
            return handleReadyKey(event, flags: flags)
        default:
            return event
        }
    }

    // While live every key drives the talk, except in the console's talk
    // length field.
    func handleLiveKey(_ event: NSEvent, flags: NSEvent.ModifierFlags) -> NSEvent? {
        let character = AppDelegate.keyCharacter(event)
        if flags.contains(.command) {
            switch character {
            case ".":
                requestEndPresentation()
                return nil
            case "1", "2", "w":
                NSSound.beep()
                return nil
            case "3":
                return nil
            case "q" where flags == [.command]:
                // A stray ⌘Q must not end the talk: the first shows a hint, a
                // second within 2 s quits (the menu's Quit, after the flush).
                if event.isARepeat { return nil }
                if let last = lastLiveQuitPress,
                   Date().timeIntervalSince(last) < AppDelegate.presentationExitConfirmWindow {
                    lastLiveQuitPress = nil
                    return event
                }
                lastLiveQuitPress = Date()
                showHint("Press ⌘Q again to quit", in: presentationHintWebView,
                         duration: AppDelegate.presentationExitConfirmWindow)
                return nil
            default:
                return event
            }
        }
        if consoleTextEditing && !eventFromAudienceWindow(event) && !audienceOneScreen {
            return event
        }
        switch event.keyCode {
        case 124, 49, 121, 125: // Right, Space, PageDown, Down
            navigateAudience(command: "WS.nextStep()")
            return nil
        case 123, 116, 126: // Left, PageUp, Up
            navigateAudience(command: "WS.prevStep()")
            return nil
        case 115: // Home
            navigateAudience(command: "WS.goToSlide(0, 0)")
            return nil
        case 119: // End
            navigateAudience(command: lastSlideNavigationCommand())
            return nil
        case 53: // Escape
            // A held key is not a deliberate second press.
            if !event.isARepeat {
                handlePresentationEscape()
            }
            return nil
        default:
            break
        }
        guard flags.isEmpty || flags == [.shift] else { return event }
        switch character {
        case "b":
            if !event.isARepeat { setBlackout(!blackoutActive) }
            return nil
        case "o", "g":
            if audienceOneScreen {
                navigateAudience(command: "WS.toggleOverview()")
            } else {
                consoleWebView?.evaluateJavaScript("presenterToggleOverview()") { _, _ in }
            }
            return nil
        case "t":
            if audienceOneScreen { toggleHUDTimer() }
            return nil
        default:
            return event
        }
    }

    // Present, before Start: ⌘↩ starts; the arrows move where it starts.
    func handleReadyKey(_ event: NSEvent, flags: NSEvent.ModifierFlags) -> NSEvent? {
        if event.keyCode == 36 || event.keyCode == 76 { // Return, Enter
            if flags == [.command] { startShow(fromStart: false); return nil }
            if flags == [.command, .shift] { startShow(fromStart: true); return nil }
        }
        if flags.contains(.command) || consoleTextEditing { return event }
        switch event.keyCode {
        case 124, 121, 125:
            runConsolePreviewCommand("WS.nextStep()")
            return nil
        case 123, 116, 126:
            runConsolePreviewCommand("WS.prevStep()")
            return nil
        case 115:
            runConsolePreviewCommand("WS.goToSlide(0, 0)")
            return nil
        case 119:
            runConsolePreviewCommand(lastSlideNavigationCommand())
            return nil
        default:
            return event
        }
    }

    func handleEditKey(_ event: NSEvent, flags: NSEvent.ModifierFlags) -> NSEvent? {
        let character = AppDelegate.keyCharacter(event)
        let inSource = eventTargetsEditorWebView(event)
        if flags == [.command] && character == "s" {
            if wysiwygTextEditingActive && !inSource {
                // The visual editor's Cmd-S preserves its caret and text session.
                // Retry any earlier disk failure before letting it post new text.
                flushPendingDeckWrite()
                return event
            }
            flushActiveEditor { _ in }
            return nil
        }
        // The source sidebar toggle belongs to the native shell so it works
        // from either embedded WKWebView, including during a text session.
        if flags == [.command, .shift] && character == "x" {
            toggleEditorSourceSidebar()
            return nil
        }
        // WKWebView consumes Command-R before the menu item sees it. Handle it
        // explicitly in the editor preview, ahead of WYSIWYG key ownership.
        if flags == [.command] && character == "r" {
            reloadPresenter(nil)
            return nil
        }
        // ⌘Z: uncommitted typing (source pane, the Refine prompt or a visual
        // text session) undoes in place; everything else is the unified history.
        if character == "z" && (flags == [.command] || flags == [.command, .shift]) {
            if (inSource && (sourceEditorDirty || refineTextEditing)) || (!inSource && wysiwygTextEditingActive) ||
                !editHistoryAvailable {
                return event
            }
            routeHistory(redo: flags.contains(.shift))
            return nil
        }
        // The Refine prompt's caret moves (⌘←/⌘→, ⌘↑/⌘↓) stay in the prompt.
        if inSource && refineTextEditing && flags == [.command] && [123, 124, 125, 126].contains(event.keyCode) {
            return event
        }
        if event.keyCode == 36 || event.keyCode == 76 {
            if (flags == [.command] || flags == [.command, .shift]) && !inSource && !wysiwygTextEditingActive {
                startShow(fromStart: flags.contains(.shift))
                return nil
            }
        }
        if flags == [.command, .option] && character == "i" {
            toggleInspector(nil)
            return nil
        }
        if flags == [.command, .option] && character == "t" {
            addTextBox(nil)
            return nil
        }
        if flags == [.command, .shift] && character == "o" {
            toggleGhostSteps(nil)
            return nil
        }
        if flags == [.command] && character == "j" && !inSource {
            refineWithAI(nil)
            return nil
        }
        if flags == [.command] && character == "e" {
            requestPDFExport()
            return nil
        }

        // Editor mode: while the WYSIWYG layer owns the preview window (text
        // session or selected element), every key goes to the webview — the
        // layer handles navigation-suppression, nudging, and deletion itself.
        if !eventFromAudienceWindow(event) &&
            !eventTargetsEditorWebView(event) &&
            (wysiwygTextEditingActive || wysiwygHasSelection) {
            return event
        }

        // Editor mode: Cmd+Arrow steps from either edit surface.
        if flags == [.command] {
            switch event.keyCode {
            case 124: // Cmd+Right → next step
                flushEditorThenRunPreviewCommand("WS.nextStep()")
                return nil
            case 123: // Cmd+Left → previous step
                flushEditorThenRunPreviewCommand("WS.prevStep()")
                return nil
            default:
                return event
            }
        }

        if flags.contains(.command) || flags.contains(.control) || flags.contains(.option) {
            return event
        }

        // Only intercept unmodified navigation keys in the visual editor. The
        // source sidebar shares the same NSWindow, so use the first-responder
        // ancestry rather than the window identity.
        if eventTargetsEditorWebView(event) {
            return event
        }

        if event.keyCode == 53 { // Escape
            handleEditEscape()
            return nil
        }

        switch event.keyCode {
        case 124, 49, 121, 125: // Right, Space, PageDown, Down
            flushEditorThenRunPreviewCommand("WS.nextStep()")
            return nil
        case 123, 116, 126: // Left, PageUp, Up
            flushEditorThenRunPreviewCommand("WS.prevStep()")
            return nil
        case 115: // Home
            flushEditorThenRunPreviewCommand("WS.goToSlide(0, 0)")
            return nil
        case 119: // End
            flushEditorThenRunPreviewCommand(lastSlideNavigationCommand())
            return nil
        default:
            return event
        }
    }

    // Escape in Edit never leaves the editor: the visual editor closes a
    // popover, then deselects, then leaves the overview. Without it, Escape
    // only closes the overview or returns from the appendix.
    func handleEditEscape() {
        guard let webView = previewWebView else { return }
        let generation = sessionGeneration
        webView.evaluateJavaScript(AppDelegate.editEscapeJS) { [weak self] result, _ in
            guard let self = self,
                  self.sessionGeneration == generation,
                  self.previewWebView === webView else { return }
            NSLog("Presto: edit Escape -> %@", (result as? String) ?? "none")
        }
    }

    // With nothing else open, Escape goes back like the Back chip: the edit
    // chrome's own record of a link jump, else the engine's appendix return
    // stack. Both navigate through sidebarNavigate, so Swift flushes first.
    static let editEscapeJS = """
        (function () {
            var handled = 'none';
            if (window.PrestoEdit && typeof PrestoEdit.handleEscape === 'function') {
                try { handled = String(PrestoEdit.handleEscape()); } catch (e) {}
            } else if (window.WS && (WS.overviewActive || document.body.classList.contains('ws-overview-active'))) {
                WS.toggleOverview();
                return 'overview';
            }
            if (handled !== 'none') return handled;
            var chrome = window.PrestoEditChrome;
            if (chrome && typeof chrome.back === 'function' && typeof chrome.state === 'function') {
                var chromeState = chrome.state();
                if (chromeState && chromeState.backStack && chromeState.backStack.length > 0 && chrome.back()) return 'back';
            }
            if (window.WS && Array.isArray(WS.returnStack) && WS.returnStack.length > 0) {
                var handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.presto;
                if (!handler) {
                    WS.returnFromAppendix();
                    return 'appendix';
                }
                var target = WS.returnStack.pop();
                handler.postMessage({ type: 'sidebarNavigate', index: target.index, step: target.step || 0 });
                return 'appendix';
            }
            return 'none';
        })()
        """

    // MARK: - Script Messages

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame,
              let body = message.body as? [String: Any],
              let type = body["type"] as? String else { return }

        let fromEditor = message.webView === editorWebView
        let fromPreview = message.webView === previewWebView
        let fromConsole = message.webView === consoleWebView
        let fromAudience = message.webView === audienceWebView
        let fromDecks = message.webView === decksWebView
        if (message.name == "editor" && !fromEditor) ||
            (message.name == "presto" && !fromPreview && !fromConsole && !fromAudience && !fromDecks) {
            NSLog("Presto: discarded %@ message from stale webview", message.name)
            return
        }

        NSLog("Presto: received message name=%@ type=%@", message.name, type)

        if message.name == "editor" {
            handleEditorMessage(type: type, body: body, webView: message.webView)
        } else if fromDecks {
            handleDecksMessage(type: type, body: body)
        } else if fromConsole {
            handleConsoleMessage(type: type, body: body, webView: message.webView)
        } else if fromAudience {
            handleAudienceMessage(type: type, body: body)
        } else if fromPreview && isEditorMode {
            handlePreviewMessage(type: type, body: body)
        }
    }

    func handleConsoleMessage(type: String, body: [String: Any], webView: WKWebView?) {
        switch type {
        case "ready":
            presenterReady = true
            consoleTextEditing = false
            syncConsoleToPosition()
            pushConsoleState()
            refreshDeckOutline()
            if let webView {
                maybeSimulateWebContentTermination(role: .presenter, webView: webView)
            }
        case "presenterNavigated":
            handlePresenterNavigated(body)
        case "consoleStart":
            if presentPhase == .ready { startShow(fromStart: false) }
        case "consoleEnd":
            endFromToolbar(nil)
        case "consoleSetTalkMinutes":
            setTalkMinutes(finiteInt(body["minutes"]))
        case "consoleTimer":
            handleConsoleTimer((body["action"] as? String) ?? "")
        case "consoleBlackout":
            setBlackout((body["on"] as? Bool) ?? !blackoutActive)
        case "consoleSwapScreens":
            swapDisplays(nil)
        case "consoleNavigate":
            guard let index = finiteInt(body["index"]), index >= 0 else { return }
            let step = max(0, finiteInt(body["step"]) ?? 0)
            if presentPhase == .live {
                navigateAudience(command: "WS.goToSlide(\(index), \(step))")
            } else {
                lastAudienceSlideIndex = index
                lastAudienceStep = step
                lastAudienceReturnStack = []
                syncConsoleToPosition()
                pushConsoleState()
            }
        case "consoleSetAudienceDisplay":
            guard let raw = body["id"] as? String ?? finiteInt(body["id"]).map(String.init),
                  let id = UInt32(raw) else { return }
            setAudienceDisplay(CGDirectDisplayID(id))
        case "consoleTextEditing":
            consoleTextEditing = (body["active"] as? Bool) ?? false
        default:
            break
        }
    }

    func handleAudienceMessage(type: String, body: [String: Any]) {
        switch type {
        case "endPresentationRequested":
            // The audience page's own Escape fallback gets the same two-press guard.
            handlePresentationEscape()
        case "audienceNavigated":
            // The audience page moved itself (a clicked link or button).
            syncPresenterFromAudience()
        case "hudNavigate":
            navigateAudience(command: (body["direction"] as? String) == "prev" ? "WS.prevStep()" : "WS.nextStep()")
        case "hudOverview":
            navigateAudience(command: "WS.toggleOverview()")
        case "hudEditHere":
            endShowIntoEdit()
        case "hudEnd":
            requestEndPresentation()
        default:
            break
        }
    }

    func handlePreviewMessage(type: String, body: [String: Any]) {
        switch type {
        case "slideChanged":
            // Slide changed in editor preview
            if let index = finiteInt(body["index"]),
               index >= 0, index < sectionInfos.count {
                isReloadingPreview = false
                let changed = currentEditorSlideIndex != index
                currentEditorSlideIndex = index
                if let step = finiteInt(body["step"]), step >= 0 {
                    currentEditorStep = step
                }
                if changed {
                    sendCurrentSlideToEditor()
                }
            }
        case "sidebarNavigate":
            // Rail clicks, link jumps and the Back chip
            if let index = finiteInt(body["index"]) {
                flushEditorThenNavigate(to: index, step: finiteInt(body["step"]) ?? 0)
            }
        case "toggleSourceSidebar":
            toggleEditorSourceSidebar()
        case "exportPDFRequested":
            requestPDFExport()
        case "wysiwygChanged":
            // WYSIWYG edit on the preview slide; undo/redo of a source or
            // Refine change carries the whole section and the version it expects.
            guard let index = finiteInt(body["index"]) else { return }
            let saveId = finiteInt(body["saveId"])
            if let section = body["sectionHTML"] as? String {
                applyHistorySection(index: index, sectionHTML: section,
                                    expected: body["expectSectionHTML"] as? String, saveId: saveId)
            } else if let html = body["html"] as? String {
                handleWysiwygChanged(html: html, index: index, saveId: saveId)
            }
        case "wysiwygEditingState":
            // WYSIWYG selection / text-editing state (gates native key handling)
            let wasTextEditing = wysiwygTextEditingActive
            wysiwygTextEditingActive = (body["textEditing"] as? Bool) ?? false
            wysiwygHasSelection = (body["hasSelection"] as? Bool) ?? false
            selectionKind = body["selectionKind"] as? String
            // When a text session ends, mirror the slide into the code pane now so
            // it's fresh if the user switches surfaces (instead of waiting out the
            // debounce).
            if wasTextEditing && !wysiwygTextEditingActive {
                flushEditorMirror()
                // The session's own commit was posted before this message, so
                // the deferred merge sees the finished text.
                if externalMergeDeferred {
                    externalMergeDeferred = false
                    reloadFromDisk()
                }
            }
        case "wysiwygWillEdit":
            // WYSIWYG gesture about to mutate the preview — flush the code pane first
            flushEditorPaneForWysiwyg()
        case "undoState":
            applyUndoState(body)
        case "editChromeReady":
            editChromeAvailable = true
            applyEditLayout()
        case "ghostFutureChanged":
            setGhostFutureSteps((body["on"] as? Bool) ?? ghostFutureSteps, fromPage: true)
        case "prestoPreviewSettled":
            captureDeckThumbnailIfNeeded()
            requestUndoState()
        default:
            break
        }
    }

    @objc func showEditorRecoveredSheet() {
        guard let mainWindow else { return }
        let alert = NSAlert()
        alert.messageText = "Editor recovered"
        alert.informativeText = "Presto restored the last saved slide. Keystrokes entered immediately before the web process stopped may need to be re-entered."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: mainWindow)
    }

    func handleEditorMessage(type: String, body: [String: Any], webView: WKWebView?) {
        switch type {
        case "ready":
            NSLog("Presto: editor is ready")
            editorReady = true
            refineTextEditing = false
            sendCurrentSlideToEditor()
            if editorRecoveryNeedsWarning {
                editorRecoveryNeedsWarning = false
                if simulatedWebContentTerminationRequests.contains(.editor) {
                    NSLog("PrestoTestWebContent: editor recovery warning acknowledged by harness")
                } else if mainWindow != nil {
                    presentSheetWhenNotLive { [weak self] in self?.showEditorRecoveredSheet() }
                }
            }
            if let webView {
                maybeSimulateWebContentTermination(role: .editor, webView: webView)
            }
        case "editorDirty":
            if let index = finiteInt(body["index"]), index == currentEditorSlideIndex {
                sourceEditorDirty = (body["dirty"] as? Bool) ?? sourceEditorDirty
            }
        case "editorChanged":
            if let html = body["html"] as? String, let index = finiteInt(body["index"]) {
                let saveId = finiteInt(body["saveId"])
                _ = handleEditorChanged(html: html, index: index, saveId: saveId)
            }
        case "refineRequested":
            // AI refine — new task requested for current slide
            if let instructions = body["instructions"] as? String {
                let index = currentEditorSlideIndex
                flushActiveEditor { [weak self] saved in
                    if saved { self?.enqueueRefine(slideIndex: index, instructions: instructions) }
                }
            }
        case "refineUndo":
            // Refine's Undo last is the unified history's undo.
            routeHistory(redo: false, allowTextUndo: false)
        case "refineRedo":
            routeHistory(redo: true, allowTextUndo: false)
        case "refineDismissError":
            refineErrors[currentEditorSlideIndex] = nil
            pushRefineStateToEditor()
        case "refineTextEditing":
            refineTextEditing = (body["active"] as? Bool) ?? false
        case "inspectorClose":
            hideInspector()
        default:
            break
        }
    }
}

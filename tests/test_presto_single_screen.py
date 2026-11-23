#!/usr/bin/env python3
"""One-screen and two-screen presenting, as planned in PrestoPresent.swift.

One screen: Start puts the main window itself into native full screen showing
only the deck (no separate borderless window). Two screens: the audience gets a
borderless window at the projector's full frame, above its menu bar and Dock,
that never takes keys. Forced dual-screen testing is foreground-test only.
The plan itself is exercised with fake displays in test_presto_shell_modes.swift.
"""
import unittest
from pathlib import Path


PRESTO_DIR = Path(__file__).resolve().parents[1] / "presto"
PRESENT_SWIFT = PRESTO_DIR / "PrestoPresent.swift"
PRESTO_SWIFT = PRESTO_DIR / "Presto.swift"


def block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin + len(start))]


class PrestoSingleScreenTests(unittest.TestCase):
    def setUp(self):
        self.present = PRESENT_SWIFT.read_text(encoding="utf-8")
        self.app = PRESTO_SWIFT.read_text(encoding="utf-8")

    def test_one_screen_show_uses_the_main_windows_native_full_screen(self):
        one = block(self.present, "func startOneScreenShow()", "func windowDidEnterFullScreen")
        self.assertIn("mainWindowWasFullScreenBeforeShow = mainWindow.styleMask.contains(.fullScreen)", one)
        self.assertIn("mainWindow.toggleFullScreen(nil)", one)
        self.assertIn("showMode(.present)", one)
        self.assertNotIn(".borderless", one)
        self.assertNotIn("PrestoAudienceWindow", one)

    def test_leaving_full_screen_ends_the_one_screen_show(self):
        exit_block = block(self.present, "func windowDidExitFullScreen", "// MARK: - Ending")
        self.assertIn("presentPhase == .live && audienceOneScreen && !isEndingPresentation", exit_block)
        self.assertIn("finishPresentationTeardown()", exit_block)
        end = block(self.present, "func endPresentation()", "func finishPresentationTeardown()")
        self.assertIn("oneScreenExitPending = true", end)
        self.assertIn("window.toggleFullScreen(nil)", end)

    def test_projector_window_is_borderless_full_frame_and_never_key(self):
        plan = block(self.present, "static func audienceWindowPlan", "func plannedAudience()")
        self.assertIn("frame: target.frame, borderless: true", plan)
        self.assertIn("level: .screenSaver", plan)
        show = block(self.present, "func startTwoScreenShow", "func startOneScreenShow()")
        self.assertIn("[.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]", show)
        self.assertIn("window.orderFrontRegardless()", show)
        self.assertIn("mainWindow.makeKeyAndOrderFront(nil)", show)
        self.assertLess(show.index("window.orderFrontRegardless()"), show.index("mainWindow.makeKeyAndOrderFront(nil)"))
        self.assertIn("window.backgroundColor = .white", show)
        window_class = block(self.present, "final class PrestoAudienceWindow", "struct DeckOutline")
        self.assertIn("override var canBecomeKey: Bool { allowsKey }", window_class)
        self.assertIn("override func constrainFrameRect", window_class)

    def test_unplugging_the_projector_hides_the_audience_and_keeps_the_console(self):
        changed = block(self.present, "func handleScreensChanged()", "// MARK: - Ready")
        self.assertIn("window.orderOut(nil)", changed)
        self.assertIn("audienceDisplayMissing = true", changed)
        self.assertIn("window.setFrame(plan.frame, display: true)", changed)
        self.assertIn("NSApplication.didChangeScreenParametersNotification", self.present)

    def test_forced_dual_screen_is_foreground_test_only_and_titled(self):
        self.assertIn('ProcessInfo.processInfo.environment["PRESTO_ALLOW_FOREGROUND_TESTS"] == "1"', self.app)
        self.assertIn('environment["PRESTO_TEST_FORCE_DUAL_SCREEN"] == "1"', self.app)
        plan = block(self.present, "static func audienceWindowPlan", "func plannedAudience()")
        self.assertIn("if displays.count == 1, forceDual", plan)
        self.assertIn("borderless: false", plan)
        show = block(self.present, "func startTwoScreenShow", "func startOneScreenShow()")
        self.assertIn('"Presto [Audience] —', show)
        self.assertIn("PrestoTestDualScreen: started", show)

    def test_display_sleep_is_blocked_while_live(self):
        self.assertIn(".idleDisplaySleepDisabled", self.present)
        teardown = block(self.present, "func teardownAudience()", "func beginDisplaySleepBlock()")
        self.assertIn("endDisplaySleepBlock()", teardown)


if __name__ == "__main__":
    unittest.main()

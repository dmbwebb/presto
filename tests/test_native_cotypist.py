"""Production native editing against Presto's real source, Refine and visual UI."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "native AppKit/WebKit check")
class NativeCotypistTests(unittest.TestCase):
    def test_ordered_edits_reach_editor_models_and_undo(self):
        menus = (ROOT / "presto/PrestoMenus.swift").read_text()
        helper = menus[menus.index("enum CotypistEditing {"):menus.index("extension AppDelegate:")]
        # Substitute only the process identity; production still checks the
        # real Cotypist bundle, never an event-provided claim about its source.
        helper = helper.replace('== "app.cotypist.Cotypist"', '== "local.presto.cotypist-test"')
        if os.environ.get("COTYPIST_TEST_BASELINE"):
            helper = "enum CotypistEditing { static func apply(_ event: NSEvent, in window: NSWindow?) -> Bool { false } }"
        directory = Path(tempfile.mkdtemp(prefix="presto-cotypist-"))
        app = directory / "PrestoCotypist.app/Contents"
        (app / "MacOS").mkdir(parents=True)
        (app / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "local.presto.cotypist-test", "CFBundleExecutable": "PrestoCotypist",
            "CFBundleName": "PrestoCotypist",
        }))
        source = directory / "Probe.swift"
        source.write_text("import Cocoa\nimport WebKit\n" + helper + (ROOT / "tests/native_cotypist_probe.swift").read_text())
        binary = app / "MacOS/PrestoCotypist"
        try:
            build = subprocess.run(["/usr/bin/swiftc", "-parse-as-library", str(source), "-o", str(binary)],
                                   capture_output=True, text=True, timeout=90)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            check = subprocess.run([str(binary), str(ROOT), str(directory / "deck.html")],
                                   capture_output=True, text=True, timeout=100)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
            self.assertIn("COTYPIST_NATIVE: 13 replacements, 0 failures", check.stdout)
        finally:
            subprocess.run(["trash", str(directory)], check=True, timeout=30)

    def test_monitor_consumes_only_corrections_before_navigation(self):
        source = (ROOT / "presto/PrestoMenus.swift").read_text()
        monitor = source[source.index("func setupKeyboardMonitor()"):source.index("func eventFromAudienceWindow")]
        self.assertIn("matching: [.keyDown, .keyUp]", monitor)
        self.assertIn("if CotypistEditing.apply(event, in: event.window) { return nil }", monitor)
        self.assertLess(monitor.index("CotypistEditing.apply"), monitor.index("return self.handleKey(event)"))
        self.assertIn("guard event.type == .keyDown else { return event }", monitor)


if __name__ == "__main__":
    unittest.main()

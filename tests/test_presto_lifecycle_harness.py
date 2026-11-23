#!/usr/bin/env python3
import subprocess
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
STRESS_RUNNER = TESTS_DIR / "run-presto-lifecycle-stress.sh"
APP_RUNNER = TESTS_DIR / "run-app-tests.sh"


class PrestoLifecycleHarnessTests(unittest.TestCase):
    def test_stress_runner_shell_syntax_is_valid(self):
        subprocess.run(["bash", "-n", str(STRESS_RUNNER)], check=True)

    def test_stress_runner_owns_one_exact_process(self):
        source = STRESS_RUNNER.read_text(encoding="utf-8")

        self.assertIn("APP_PID=$!", source)
        self.assertIn('/bin/kill -0 "$APP_PID"', source)
        self.assertIn('/bin/kill -TERM "$APP_PID"', source)
        self.assertIn("first process whose unix id is appPID", source)
        self.assertNotIn('tell application "Presto"', source)
        self.assertNotIn("pkill", source)
        self.assertNotIn("killall", source)

    def test_stress_runner_cycles_edit_present_and_decks_in_one_window(self):
        source = STRESS_RUNNER.read_text(encoding="utf-8")

        self.assertIn("Presto: audience mode injected", source)
        self.assertIn("Presto: editor preview injected", source)
        self.assertIn("Presto: editor is ready", source)
        self.assertIn("name=presto type=slideChanged", source)
        # Both ways into a talk and every way out of it.
        self.assertIn('click_menu_item "Presentation" "Play from Current Slide"', source)
        self.assertIn('click_menu_item "View" "Present"', source)
        self.assertIn("press_command_return", source)
        self.assertIn("press_escape_twice", source)
        self.assertIn('click_menu_item "Presentation" "End Presentation"', source)
        self.assertIn('close_window_with_prefix "Presto [Audience]"', source)
        # Ending returns to Edit without rebuilding it.
        self.assertIn("Presto: presentation ended", source)
        self.assertIn("Presto: mode=edit", source)
        self.assertIn("rebuilt the edit view instead of keeping it alive", source)
        self.assertIn('click_menu_item "View" "Decks"', source)
        self.assertIn("Presto: mode=decks", source)
        self.assertIn("toggle_source_sidebar_shortcut", source)
        self.assertIn('press_keys "x" "command down, shift down"', source)
        self.assertIn("Presto: source sidebar visible=1", source)
        self.assertIn("Presto: source sidebar visible=0", source)
        self.assertIn('CYCLES="${PRESTO_E2E_CYCLES:-10}"', source)
        self.assertIn("PRESTO_TEST_FORCE_DUAL_SCREEN", source)
        self.assertIn("PRESTO_TEST_SIMULATE_WEB_CONTENT_TERMINATION", source)
        self.assertIn('wait_for_window_prefix "Presto [Audience]"', source)
        self.assertIn("PrestoTestSync: .* match=1", source)
        self.assertIn("wait_for_editor_recoveries", source)
        self.assertIn("PRESTO_TEST_EXPORT_OUTPUT", source)
        self.assertIn('click_menu_item "File" "Export PDF"', source)
        self.assertIn("whose name starts with menuItem", source)
        self.assertIn("PrestoTestExport: success=1", source)
        self.assertIn('pdfinfo "$EXPORT_OUTPUT"', source)

    def test_closing_the_window_keeps_the_app_running(self):
        source = STRESS_RUNNER.read_text(encoding="utf-8")
        self.assertIn('close_window_with_prefix "Presto"', source)
        self.assertIn("Presto: main window closed", source)
        self.assertIn("closing the window quit Presto", source)

    def test_stress_runner_covers_cross_deck_native_file_panel(self):
        source = STRESS_RUNNER.read_text(encoding="utf-8")

        self.assertIn("PRESTO_SWITCH_DECK", source)
        self.assertIn('menu item "Open…"', source)
        self.assertIn('keystroke "g" using {command down, shift down}', source)
        self.assertIn("cross-deck presentation readiness", source)
        self.assertIn("wait_for_window_name", source)

    def test_file_panel_checks_the_go_to_path_and_confirms_once(self):
        source = STRESS_RUNNER.read_text(encoding="utf-8")
        start = source.index("open_deck_via_file_panel() {")
        helper = source[start : source.index("\n}\n", start)]

        # The Go To field starts with the last path used on the Mac; Return is
        # only pressed once the field holds the deck path.
        check = helper.index("is not deckPath then error")
        self.assertLess(helper.index("keystroke deckPath"), check)
        self.assertLess(check, helper.index("key code 36"))
        # One confirm after the sheet closes, never a Return loop on whatever
        # the panel has selected.
        self.assertEqual(helper.count("key code 36"), 2)
        self.assertIn('if exists window "Open" then key code 36', helper)
        self.assertNotIn("delay 1.0", helper)

    def test_stress_runner_restores_presto_defaults(self):
        source = STRESS_RUNNER.read_text(encoding="utf-8")

        self.assertIn("defaults export com.webslides.presto", source)
        self.assertIn("defaults import com.webslides.presto", source)
        self.assertIn("defaults delete com.webslides.presto", source)

    def test_full_foreground_gate_includes_lifecycle_stress(self):
        source = APP_RUNNER.read_text(encoding="utf-8")

        self.assertIn("run-presto-lifecycle-stress.sh", source)
        self.assertIn('PRESTO_STRESS_DECK="$STRESS_DECK"', source)


if __name__ == "__main__":
    unittest.main()

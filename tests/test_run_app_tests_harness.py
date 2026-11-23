#!/usr/bin/env python3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_APP_TESTS = ROOT / "tests" / "run-app-tests.sh"


class RunAppTestsHarnessTests(unittest.TestCase):
    def setUp(self):
        self.script = RUN_APP_TESTS.read_text(encoding="utf-8")

    def test_shell_syntax_is_valid(self):
        subprocess.run(["bash", "-n", str(RUN_APP_TESTS)], check=True)

    def test_foreground_e2e_requires_explicit_opt_in(self):
        script = self.script
        self.assertIn('PRESTO_ALLOW_FOREGROUND_TESTS:-0', script)
        self.assertIn("foreground tests are disabled by default", script)
        self.assertIn("send real clicks/keys", script)
        self.assertLess(script.index("PRESTO_ALLOW_FOREGROUND_TESTS"), script.index("PROJECT_DIR="))

    def test_launch_paths_cover_decks_present_play_and_edit(self):
        script = self.script
        self.assertIn('open "$APP_BUNDLE"\n', script)
        self.assertIn('open "$APP_BUNDLE" --args "$DECK"', script)
        self.assertIn('"$APP_EXECUTABLE" --play "$DECK" >"$ESCAPE_LOG" 2>&1 &', script)
        self.assertIn('PRESTO_TEST_LOG_NAVIGATION=1 "$APP_EXECUTABLE" --play "$DECK" >"$NAV_LOG" 2>&1 &', script)
        self.assertIn('open "$APP_BUNDLE" --args --edit "$DECK"', script)
        self.assertIn('open "$APP_BUNDLE" --args --edit "$EXTERNAL_DECK"', script)
        self.assertNotIn("click_picker_button", script)
        self.assertIn("ESCAPE_PID=$!", script)

    def test_window_lookups_do_not_need_screen_recording(self):
        start = self.script.index("presto_window_named_contains() {")
        body = self.script[start : self.script.index("\n}\n", start)]
        self.assertIn('tell application "System Events" to tell process "Presto"', body)
        self.assertLess(body.index("System Events"), body.index("CGWindowListCopyWindowInfo"))

    def test_one_screen_show_must_fill_the_display(self):
        self.assertIn("assert_one_screen_show_fills_the_display()", self.script)
        self.assertIn("assert_one_screen_show_fills_the_display || ONE_SCREEN_OK=0", self.script)

    def test_escape_cycle_log_uses_portable_mktemp(self):
        self.assertIn('ESCAPE_LOG="$(mktemp -t presto-escape-cycle.XXXXXX)"', self.script)
        self.assertNotIn('mktemp /tmp/presto-escape-cycle.XXXXXX.log', self.script)

    def test_escape_cycle_quits_owned_process_cleanly(self):
        self.assertIn("finish_escape_process()", self.script)
        self.assertIn('tell application "Presto" to quit', self.script)
        self.assertIn('ESCAPE_PID=""', self.script)

    def test_escape_cycle_checks_the_two_press_exit_guard(self):
        script = self.script
        self.assertIn("press_escape_twice()", script)
        self.assertIn('cliclick "c:${x},${y}" "kp:esc" "w:300" "kp:esc"', script)
        self.assertIn("press_escape_twice || true", script)
        self.assertIn('echo "  FAIL: A single Escape ended the presentation"', script)
        self.assertLess(script.index("sleep 2.5\n    SINGLE_ESCAPE_ENDED=0"), script.index("press_escape_twice || true"))
        self.assertIn('grep -q "Presto: presentation ended" "$ESCAPE_LOG"', script)
        self.assertIn('wait_for_log "Presto: mode=decks" "$ESCAPE_LOG"', script)

    def test_escape_cycle_presents_again_from_the_ready_console(self):
        self.assertIn('click_menu_item "View" "Present"', self.script)
        self.assertIn("press_command_return || true", self.script)
        self.assertIn('wait_for_log "Presto: audience mode injected" "$ESCAPE_LOG" 2', self.script)

    def test_navigation_keys_assert_actual_slide_state(self):
        script = self.script
        self.assertIn("assert_nav_log_contains()", script)
        self.assertIn("PrestoTestNavigation: index=1 step=0", script)
        self.assertIn("PrestoTestNavigation: index=2 step=0", script)
        self.assertIn("PrestoTestNavigation: index=0 step=0", script)
        self.assertIn("Navigation did not reach expected state", script)
        self.assertIn("click_presentation_menu_item()", script)
        for item in ("Next Step", "Previous Step", "First Slide", "Last Slide"):
            self.assertIn(f'click_presentation_menu_item "{item}"', script)

    def test_last_slide_expectation_is_derived_from_the_deck(self):
        script = self.script
        self.assertNotIn("index=19 step=4", script)
        self.assertIn('LAST_MAIN_INDEX="$(last_main_slide_index "$DECK")"', script)
        self.assertIn('assert_nav_log_reached_slide_end "$LAST_MAIN_INDEX" "$NAV_LOG"', script)

        start = script.index("python3 - \"$1\" <<'PY'\n") + len("python3 - \"$1\" <<'PY'\n")
        parser = script[start : script.index("\nPY\n", start)]
        deck_html = """
            <div class="ws-deck">
              <section class="slide title"></section>
              <!-- <section class="slide content">commented out</section> -->
              <section class="slide content"><section class="notes"></section></section>
              <section id="a1" class="slide appendix content"></section>
              <section
                  class='slide thanks'></section>
              <section class="slide appendix"></section>
            </div>
        """
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "index.html"
            deck.write_text(deck_html, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", parser, str(deck)],
                check=True,
                capture_output=True,
                text=True,
            )
        # title, content, appendix, thanks, appendix: the thanks slide is index 3.
        self.assertEqual(result.stdout.strip(), "3")

    def test_last_slide_check_requires_the_final_step(self):
        script = self.script
        start = script.index("assert_nav_log_reached_slide_end() {")
        helper = script[start : script.index("\n}\n", start) + 3]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "nav.log"
            cases = {
                "PrestoTestNavigation: index=21 step=3 maxSteps=3\n": 0,
                "PrestoTestNavigation: index=21 step=0 maxSteps=3\n": 1,
                "PrestoTestNavigation: index=2 step=0 maxSteps=0\n": 1,
                "PrestoTestNavigation: index=21 step=0\n": 1,
            }
            for content, status in cases.items():
                log.write_text(content, encoding="utf-8")
                result = subprocess.run(
                    ["bash", "-c", helper + '\nassert_nav_log_reached_slide_end 21 "$1"', "_", str(log)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, status, content)

    def test_temp_logs_are_cleaned_up_with_trash(self):
        script = self.script
        cleanup_start = script.index("cleanup()")
        cleanup_block = script[cleanup_start : script.index("\n}\n\napp_alive", cleanup_start)]

        self.assertIn('ESCAPE_LOG=""', script)
        self.assertIn('NAV_LOG=""', script)
        self.assertIn("for log_file in", cleanup_block)
        self.assertIn('"$ESCAPE_LOG"', cleanup_block)
        self.assertIn('"$NAV_LOG"', cleanup_block)
        self.assertIn('trash "$log_file"', cleanup_block)


if __name__ == "__main__":
    unittest.main()

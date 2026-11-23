#!/bin/bash
# Foreground Presto App E2E Crash Tests
# Tests the one-window flow: Decks → Present (ready, then live) → navigate →
# Escape → back, Edit, and the lifecycle stress.
# Run with PRESTO_ALLOW_FOREGROUND_TESTS=1 when real app focus/click coverage is needed.
set -e

if [ "${PRESTO_ALLOW_FOREGROUND_TESTS:-0}" != "1" ]; then
    echo "SKIP: Presto app E2E foreground tests are disabled by default."
    echo "Set PRESTO_ALLOW_FOREGROUND_TESTS=1 to run tests that foreground Presto and send real clicks/keys."
    exit 0
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DECK="$PROJECT_DIR/examples/kitchen-sink/index.html"
DECK_NAME="$(basename "$(dirname "$DECK")")"
APP_BUNDLE="$PROJECT_DIR/presto/app/Presto.app"
APP_EXECUTABLE="${PRESTO_APP_EXECUTABLE:-$APP_BUNDLE/Contents/MacOS/Presto}"
# Optional machine-specific settings (e.g. EXTERNAL_DECK, a real deck outside this repo), untracked.
if [ -f "$PROJECT_DIR/tests/local.env" ]; then . "$PROJECT_DIR/tests/local.env"; fi
EXTERNAL_DECK="${EXTERNAL_DECK:-}"
EXTERNAL_DECK_NAME="$(basename "$(dirname "$EXTERNAL_DECK")")"
PASS=0
FAIL=0
ESCAPE_PID=""
ESCAPE_LOG=""
NAV_LOG=""

finish_escape_process() {
    if [ -z "${ESCAPE_PID:-}" ]; then
        return
    fi

    osascript -e 'tell application "Presto" to quit' 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "$ESCAPE_PID" 2>/dev/null; then
            wait "$ESCAPE_PID" 2>/dev/null || true
            ESCAPE_PID=""
            return
        fi
        sleep 0.2
    done

    kill "$ESCAPE_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        if ! kill -0 "$ESCAPE_PID" 2>/dev/null; then
            wait "$ESCAPE_PID" 2>/dev/null || true
            ESCAPE_PID=""
            return
        fi
        sleep 0.2
    done

    kill -9 "$ESCAPE_PID" 2>/dev/null || true
    wait "$ESCAPE_PID" 2>/dev/null || true
    ESCAPE_PID=""
}

cleanup() {
    finish_escape_process
    osascript -e 'tell application "Presto" to quit' 2>/dev/null || true
    sleep 1
    pkill -TERM -f "Presto.app" 2>/dev/null || true
    sleep 0.5
    pkill -9 -f "Presto.app" 2>/dev/null || true
    # Dismiss crash dialogs
    osascript -e '
    tell application "System Events"
        try
            click button "Ignore" of window 1 of process "UserNotificationCenter"
        end try
    end tell
    ' 2>/dev/null || true
    for log_file in "$ESCAPE_LOG" "$NAV_LOG"; do
        if [ -n "$log_file" ] && [ -e "$log_file" ]; then
            trash "$log_file" 2>/dev/null || true
        fi
    done
    sleep 1
}

app_alive() {
    osascript -e 'tell application "System Events" to (name of processes) contains "Presto"' 2>/dev/null | grep -q "true"
}

# Window lookups go through System Events first: CGWindowList omits window
# titles for callers without Screen Recording permission (any SSH session).
presto_window_named_contains() {
    local prefix="$1"
    local needle="$2"
    # AppleScript's `contains ""` is false, so an empty needle checks the prefix only.
    if WINDOW_PREFIX="$prefix" WINDOW_NEEDLE="$needle" osascript \
        -e 'set windowPrefix to system attribute "WINDOW_PREFIX"' \
        -e 'set windowNeedle to system attribute "WINDOW_NEEDLE"' \
        -e 'tell application "System Events" to tell process "Presto"' \
        -e 'if windowNeedle is "" then return exists (first window whose name starts with windowPrefix)' \
        -e 'return exists (first window whose name starts with windowPrefix and name contains windowNeedle)' \
        -e 'end tell' \
        2>/dev/null | grep -q "true"; then
        return 0
    fi
    WINDOW_PREFIX="$prefix" WINDOW_NEEDLE="$needle" swift -e 'import CoreGraphics
import Foundation
let prefix = ProcessInfo.processInfo.environment["WINDOW_PREFIX"] ?? ""
let needle = ProcessInfo.processInfo.environment["WINDOW_NEEDLE"] ?? ""
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
for window in windows {
    guard (window[kCGWindowOwnerName as String] as? String) == "Presto",
          let name = window[kCGWindowName as String] as? String else { continue }
    if name.hasPrefix(prefix) && name.contains(needle) {
        exit(0)
    }
}
exit(1)'
}

wait_for_window_named() {
    local prefix="$1"
    local needle="$2"
    for _ in $(seq 1 40); do
        if presto_window_named_contains "$prefix" "$needle"; then
            return 0
        fi
        sleep 0.25
    done
    return 1
}

# The largest on-screen Presto window: in native full screen the title bar is
# a separate thin window that would otherwise be found first.
presto_front_window_bounds() {
    swift -e 'import CoreGraphics
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
var best: (Double, String)? = nil
for window in windows {
    guard (window[kCGWindowOwnerName as String] as? String) == "Presto",
          (window[kCGWindowLayer as String] as? Int) == 0,
          let bounds = window[kCGWindowBounds as String] as? [String: Any],
          let x = bounds["X"] as? Double,
          let y = bounds["Y"] as? Double,
          let width = bounds["Width"] as? Double,
          let height = bounds["Height"] as? Double else { continue }
    if best == nil || width * height > best!.0 {
        best = (width * height, "\(Int(x)) \(Int(y)) \(Int(width)) \(Int(height))")
    }
}
guard let best else { exit(1) }
print(best.1)'
}

presto_front_window_center() {
    local bounds x y w h
    bounds="$(presto_front_window_bounds)" || return 1
    read -r x y w h <<< "$bounds"
    echo "$((x + w / 2)) $((y + h / 2))"
}

focus_presto_front_window() {
    command -v cliclick >/dev/null 2>&1 || return 1
    local center x y
    center="$(presto_front_window_center)" || return 1
    read -r x y <<< "$center"
    cliclick "c:${x},${y}" >/dev/null 2>&1
}

# One screen: the show is the main window in native full screen, covering
# the whole display (no title bar, menu bar or Dock).
assert_one_screen_show_fills_the_display() {
    local screen_count
    screen_count="$(swift -e 'import AppKit; print(NSScreen.screens.count)')" || return 1
    if [ "$screen_count" != "1" ]; then
        return 0
    fi
    # On a notched display a full-screen window sits below the camera housing,
    # so it is the screen's height minus the safe-area inset; AXFullScreen
    # tells it apart from a merely zoomed window of the same size.
    local screen_size bounds x y w h sw sh notch full
    screen_size="$(swift -e 'import AppKit; let s = NSScreen.screens[0]; print("\(Int(s.frame.width)) \(Int(s.frame.height)) \(Int(s.safeAreaInsets.top.rounded(.up)))")')" || return 1
    read -r sw sh notch <<< "$screen_size"
    for _ in $(seq 1 20); do
        full="$(osascript -e 'tell application "System Events" to tell process "Presto" to get value of attribute "AXFullScreen" of window 1' 2>/dev/null)"
        if bounds="$(presto_front_window_bounds)"; then
            read -r x y w h <<< "$bounds"
            if [ "$full" = "true" ] && [ "$w" -eq "$sw" ] && [ "$h" -ge $((sh - notch - 1)) ]; then
                return 0
            fi
        fi
        sleep 0.25
    done
    echo "  FAIL: The one-screen show is not full screen (window ${w:-?}x${h:-?}, screen ${sw}x${sh}, notch ${notch:-?}, AXFullScreen ${full:-?})"
    return 1
}

press_key() {
    local key="$1"
    if command -v cliclick >/dev/null 2>&1; then
        local center x y
        if center="$(presto_front_window_center)"; then
            read -r x y <<< "$center"
            if cliclick "c:${x},${y}" "kp:${key}" >/dev/null 2>&1; then
                return 0
            fi
        fi
        if cliclick "kp:${key}" >/dev/null 2>&1; then
            return 0
        fi
    fi
    osascript -e 'tell application "System Events" to key code 53' 2>/dev/null
}

# Ending a presentation takes a second Escape within 2 seconds, so both
# presses go out in one cliclick call (window lookup is slow).
press_escape_twice() {
    if command -v cliclick >/dev/null 2>&1; then
        local center x y
        if center="$(presto_front_window_center)"; then
            read -r x y <<< "$center"
            if cliclick "c:${x},${y}" "kp:esc" "w:300" "kp:esc" >/dev/null 2>&1; then
                return 0
            fi
        fi
        if cliclick "kp:esc" "w:300" "kp:esc" >/dev/null 2>&1; then
            return 0
        fi
    fi
    osascript -e 'tell application "System Events"' -e 'key code 53' -e 'delay 0.3' -e 'key code 53' -e 'end tell' 2>/dev/null
}

click_menu_item() {
    local menu="$1"
    local item="$2"
    MENU_NAME="$menu" MENU_ITEM="$item" osascript \
        -e 'set menuName to system attribute "MENU_NAME"' \
        -e 'set menuItem to system attribute "MENU_ITEM"' \
        -e 'tell application "System Events" to tell process "Presto"' \
        -e 'set frontmost to true' \
        -e 'click menu item menuItem of menu 1 of menu bar item menuName of menu bar 1' \
        -e 'end tell' \
        >/dev/null 2>&1
}

click_presentation_menu_item() {
    click_menu_item "Presentation" "$1"
}

press_command_return() {
    osascript -e 'tell application "System Events" to tell process "Presto"' \
        -e 'set frontmost to true' -e 'key code 36 using {command down}' -e 'end tell' >/dev/null 2>&1
}

wait_for_log() {
    local pattern="$1"
    local log_file="$2"
    local count="${3:-1}"
    for _ in $(seq 1 60); do
        if [ "$(grep -c "$pattern" "$log_file" 2>/dev/null || true)" -ge "$count" ]; then
            return 0
        fi
        sleep 0.25
    done
    return 1
}

assert_nav_log_contains() {
    local expected="$1"
    local log_file="$2"
    if grep -q "$expected" "$log_file"; then
        return 0
    fi
    echo "  FAIL: Navigation did not reach expected state: $expected"
    tail -80 "$log_file"
    return 1
}

# Index of the deck's last non-appendix slide, read from the HTML so the Last
# Slide check follows the deck as slides are added.
last_main_slide_index() {
    python3 - "$1" <<'PY'
import re
import sys

html = re.sub(r"<!--.*?-->", "", open(sys.argv[1], encoding="utf-8").read(), flags=re.S)
slides = [
    match.group(2).split()
    for match in re.finditer(r"<section\b[^>]*?\bclass\s*=\s*([\"'])(.*?)\1", html, flags=re.S)
    if "slide" in match.group(2).split()
]
main = [index for index, classes in enumerate(slides) if "appendix" not in classes]
if not main:
    sys.exit("no main slides found in " + sys.argv[1])
print(main[-1])
PY
}

# Passes when the log shows slide $1 at its final step (step equals the
# engine's maxSteps for that slide).
assert_nav_log_reached_slide_end() {
    local index="$1"
    local log_file="$2"
    local line
    while IFS= read -r line; do
        if [[ "$line" =~ step=([0-9]+)\ maxSteps=([0-9]+) ]] &&
           [ "${BASH_REMATCH[1]}" = "${BASH_REMATCH[2]}" ]; then
            return 0
        fi
    done < <(grep "PrestoTestNavigation: index=${index} " "$log_file")
    echo "  FAIL: Navigation did not reach the final step of last main slide $index"
    tail -80 "$log_file"
    return 1
}

crash_count() {
    ls ~/Library/Logs/DiagnosticReports/Presto* 2>/dev/null | wc -l | tr -d ' '
}

trap cleanup EXIT

cleanup

echo "=== Presto App E2E Tests ==="
echo ""

CRASHES_START=$(crash_count)

# ---------- Test 1: Launch → Decks ----------
echo "Test 1: Launch shows Decks in one window"
open "$APP_BUNDLE"
sleep 4

if ! app_alive; then echo "  FAIL: App didn't launch"; FAIL=$((FAIL+1)); cleanup; else
    if wait_for_window_named "Presto" ""; then
        echo "  PASS"
        PASS=$((PASS+1))
    else
        echo "  FAIL: No Presto window after launch"
        FAIL=$((FAIL+1))
    fi
    cleanup
fi

# ---------- Test 2: CLI launch opens Present (ready) ----------
echo "Test 2: CLI launch with --args opens the ready console"
open "$APP_BUNDLE" --args "$DECK"
sleep 5

if ! app_alive; then
    echo "  FAIL: Crashed"
    FAIL=$((FAIL+1))
elif wait_for_window_named "Presto" "$DECK_NAME"; then
    echo "  PASS"
    PASS=$((PASS+1))
else
    echo "  FAIL: The deck's window did not open"
    FAIL=$((FAIL+1))
fi
cleanup

# ---------- Test 3: Play → Escape → Present again ----------
echo "Test 3: Play → single Escape → double Escape → Present again"
ESCAPE_LOG="$(mktemp -t presto-escape-cycle.XXXXXX)"
"$APP_EXECUTABLE" --play "$DECK" >"$ESCAPE_LOG" 2>&1 &
ESCAPE_PID=$!
sleep 5

if ! app_alive; then echo "  FAIL: Didn't launch"; FAIL=$((FAIL+1)); cleanup; else
    ONE_SCREEN_OK=1
    assert_one_screen_show_fills_the_display || ONE_SCREEN_OK=0
    # One Escape only arms the exit guard; let it lapse, then press twice.
    osascript -e 'tell application "Presto" to activate' 2>/dev/null && sleep 0.5
    focus_presto_front_window || true
    press_key "esc" || true
    sleep 2.5
    SINGLE_ESCAPE_ENDED=0
    if grep -q "Presto: presentation ended" "$ESCAPE_LOG"; then SINGLE_ESCAPE_ENDED=1; fi
    press_escape_twice || true

    if ! app_alive; then echo "  FAIL: Crashed on Escape"; FAIL=$((FAIL+1)); cleanup; elif [ "$ONE_SCREEN_OK" = 0 ]; then FAIL=$((FAIL+1)); cleanup; elif [ "$SINGLE_ESCAPE_ENDED" = 1 ]; then echo "  FAIL: A single Escape ended the presentation"; FAIL=$((FAIL+1)); cleanup; elif ! wait_for_log "Presto: presentation ended" "$ESCAPE_LOG" || ! wait_for_log "Presto: mode=decks" "$ESCAPE_LOG"; then echo "  FAIL: Double Escape did not end the show back to Decks"; FAIL=$((FAIL+1)); cleanup; else
        # Present again: View > Present opens the console ready; ⌘↩ starts.
        sleep 1
        click_menu_item "View" "Present" || true
        sleep 2
        press_command_return || true
        if wait_for_log "Presto: audience mode injected" "$ESCAPE_LOG" 2 && app_alive; then
            echo "  PASS"
            PASS=$((PASS+1))
        else
            echo "  FAIL: Could not present again from the ready console"
            tail -40 "$ESCAPE_LOG"
            FAIL=$((FAIL+1))
        fi
        cleanup
    fi
fi
finish_escape_process

# ---------- Test 4: Navigation commands ----------
echo "Test 4: Navigation commands (next, previous, first, last)"
NAV_LOG="$(mktemp -t presto-navigation.XXXXXX)"
PRESTO_TEST_LOG_NAVIGATION=1 "$APP_EXECUTABLE" --play "$DECK" >"$NAV_LOG" 2>&1 &
ESCAPE_PID=$!
sleep 5

if ! app_alive; then echo "  FAIL: Didn't launch"; FAIL=$((FAIL+1)); cleanup; else
    osascript -e 'tell application "Presto" to activate' 2>/dev/null && sleep 0.5
    focus_presto_front_window || true
    click_presentation_menu_item "Next Step" || true
    sleep 0.3
    click_presentation_menu_item "Next Step" || true
    sleep 0.3
    click_presentation_menu_item "Previous Step" || true
    sleep 0.3
    click_presentation_menu_item "First Slide" || true
    sleep 0.3
    click_presentation_menu_item "Last Slide" || true
    sleep 2

    if ! app_alive; then
        echo "  FAIL: Crashed"
        FAIL=$((FAIL+1))
    elif ! LAST_MAIN_INDEX="$(last_main_slide_index "$DECK")"; then
        echo "  FAIL: Could not find the last main slide in $DECK"
        FAIL=$((FAIL+1))
    elif assert_nav_log_contains "PrestoTestNavigation: index=1 step=0" "$NAV_LOG" &&
         assert_nav_log_contains "PrestoTestNavigation: index=2 step=0" "$NAV_LOG" &&
         assert_nav_log_contains "PrestoTestNavigation: index=0 step=0" "$NAV_LOG" &&
         assert_nav_log_reached_slide_end "$LAST_MAIN_INDEX" "$NAV_LOG"; then
        echo "  PASS"
        PASS=$((PASS+1))
    else
        FAIL=$((FAIL+1))
    fi
    cleanup
fi

# ---------- Test 5: CLI Edit ----------
echo "Test 5: CLI --edit opens the deck in Edit"
open "$APP_BUNDLE" --args --edit "$DECK"
sleep 5

if ! app_alive; then echo "  FAIL: Crashed"; FAIL=$((FAIL+1)); elif wait_for_window_named "Presto" "$DECK_NAME"; then echo "  PASS"; PASS=$((PASS+1)); else echo "  FAIL: Edit window did not open"; FAIL=$((FAIL+1)); fi
cleanup

# ---------- Test 6: External project-owned deck in edit mode ----------
echo "Test 6: External project-owned deck in edit mode"
if [ ! -f "$EXTERNAL_DECK" ]; then
    echo "  SKIP: External deck not found: $EXTERNAL_DECK"
else
    open "$APP_BUNDLE" --args --edit "$EXTERNAL_DECK"
    sleep 6

    if ! app_alive; then
        echo "  FAIL: Crashed"
        FAIL=$((FAIL+1))
    elif presto_window_named_contains "Presto" "$EXTERNAL_DECK_NAME"; then
        echo "  PASS"
        PASS=$((PASS+1))
    else
        echo "  FAIL: Unified edit window did not open for external deck"
        FAIL=$((FAIL+1))
    fi
    cleanup
fi

# ---------- Test 7: Installed app launches into Decks ----------
echo "Test 7: Installed app launches into Decks"
open -a Presto
sleep 4

if ! app_alive; then echo "  FAIL: App didn't launch"; FAIL=$((FAIL+1)); cleanup; else
    if wait_for_window_named "Presto" ""; then echo "  PASS"; PASS=$((PASS+1)); else echo "  FAIL: No window"; FAIL=$((FAIL+1)); fi
    cleanup
fi

# ---------- Test 8: Repeated Edit ↔ Present lifecycle stress ----------
echo "Test 8: Repeated Edit ↔ Present ↔ Decks lifecycle stress"
STRESS_DECK="$DECK"
if [ -f "$EXTERNAL_DECK" ]; then
    STRESS_DECK="$EXTERNAL_DECK"
fi
if PRESTO_ALLOW_FOREGROUND_TESTS=1 \
   PRESTO_APP_EXECUTABLE="$APP_EXECUTABLE" \
   PRESTO_STRESS_DECK="$STRESS_DECK" \
   PRESTO_E2E_CYCLES="${PRESTO_E2E_CYCLES:-10}" \
   bash "$PROJECT_DIR/tests/run-presto-lifecycle-stress.sh"; then
    echo "  PASS"
    PASS=$((PASS+1))
else
    echo "  FAIL"
    FAIL=$((FAIL+1))
fi

# ---------- Summary ----------
CRASHES_END=$(crash_count)
NEW_CRASHES=$((CRASHES_END - CRASHES_START))

echo ""
echo "=== Results ==="
echo "Passed: $PASS / $((PASS + FAIL))"
echo "New crash reports: $NEW_CRASHES"

if [ "$FAIL" -gt 0 ] || [ "$NEW_CRASHES" -gt 0 ]; then
    echo "FAILED"
    exit 1
else
    echo "ALL PASSED"
    exit 0
fi

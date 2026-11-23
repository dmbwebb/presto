#!/bin/bash
# Foreground lifecycle stress for the one-window Presto: Edit, Decks and
# Present (ready and live) in one exact owned process.
set -euo pipefail

if [ "${PRESTO_ALLOW_FOREGROUND_TESTS:-0}" != "1" ]; then
    echo "SKIP: Presto lifecycle stress is disabled by default."
    exit 0
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_EXECUTABLE="${PRESTO_APP_EXECUTABLE:-$PROJECT_DIR/presto/app/Presto.app/Contents/MacOS/Presto}"
DECK="${PRESTO_STRESS_DECK:-$PROJECT_DIR/examples/kitchen-sink/index.html}"
SWITCH_DECK="${PRESTO_SWITCH_DECK:-$PROJECT_DIR/examples/kitchen-sink/index.html}"
CYCLES="${PRESTO_E2E_CYCLES:-10}"
FORCE_DUAL_SCREEN="${PRESTO_TEST_FORCE_DUAL_SCREEN:-0}"
RECOVERY_ROLES="${PRESTO_TEST_SIMULATE_WEB_CONTENT_TERMINATION:-}"
LOG_FILE="$(mktemp -t presto-lifecycle-stress.XXXXXX)"
DEFAULTS_BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/presto-defaults.XXXXXX")"
DEFAULTS_BACKUP="$DEFAULTS_BACKUP_DIR/com.webslides.presto.plist"
EXPORT_OUTPUT="$DEFAULTS_BACKUP_DIR/presto-export.pdf"
DEFAULTS_EXISTED=0
APP_PID=""

if ! [[ "$CYCLES" =~ ^[1-9][0-9]*$ ]]; then
    echo "FAIL: PRESTO_E2E_CYCLES must be a positive integer" >&2
    exit 1
fi
if [ "$FORCE_DUAL_SCREEN" = "1" ] && [ "$CYCLES" -lt 2 ]; then
    echo "FAIL: forced dual-screen stress needs at least 2 cycles to use both end paths" >&2
    exit 1
fi

if defaults export com.webslides.presto "$DEFAULTS_BACKUP" >/dev/null 2>&1; then
    DEFAULTS_EXISTED=1
fi

cleanup() {
    if [ -n "$APP_PID" ] && /bin/kill -0 "$APP_PID" 2>/dev/null; then
        /bin/kill -TERM "$APP_PID" 2>/dev/null || true
        wait "$APP_PID" 2>/dev/null || true
    fi
    if [ -e "$LOG_FILE" ]; then
        trash "$LOG_FILE" 2>/dev/null || true
    fi
    if [ "$DEFAULTS_EXISTED" -eq 1 ]; then
        defaults import com.webslides.presto "$DEFAULTS_BACKUP" >/dev/null 2>&1 || true
    else
        defaults delete com.webslides.presto >/dev/null 2>&1 || true
    fi
    if [ -e "$DEFAULTS_BACKUP_DIR" ]; then
        trash "$DEFAULTS_BACKUP_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if [ ! -x "$APP_EXECUTABLE" ]; then
    echo "FAIL: Presto executable is missing: $APP_EXECUTABLE" >&2
    exit 1
fi
if [ ! -f "$DECK" ]; then
    echo "FAIL: stress deck is missing: $DECK" >&2
    exit 1
fi
if [ ! -f "$SWITCH_DECK" ]; then
    echo "FAIL: switch deck is missing: $SWITCH_DECK" >&2
    exit 1
fi
if pgrep -x Presto >/dev/null 2>&1; then
    echo "FAIL: another Presto process is already running; close it before the stress gate" >&2
    exit 1
fi

crash_count() {
    find "$HOME/Library/Logs/DiagnosticReports" -maxdepth 1 -name 'Presto-*.ips' | wc -l | tr -d ' '
}

log_count() {
    local pattern="$1"
    local count
    count="$(rg -c "$pattern" "$LOG_FILE" 2>/dev/null || true)"
    echo "${count:-0}"
}

wait_for_log_count() {
    local pattern="$1"
    local target="$2"
    local label="$3"
    local max_attempts="${4:-80}"
    local attempt
    for ((attempt = 0; attempt < max_attempts; attempt++)); do
        if ! /bin/kill -0 "$APP_PID" 2>/dev/null; then
            echo "FAIL: Presto exited while waiting for $label" >&2
            tail -100 "$LOG_FILE" >&2
            return 1
        fi
        if [ "$(log_count "$pattern")" -ge "$target" ]; then
            return 0
        fi
        sleep 0.25
    done
    echo "FAIL: timed out waiting for $label" >&2
    tail -100 "$LOG_FILE" >&2
    return 1
}

wait_for_log_increment() {
    local pattern="$1"
    local baseline="$2"
    local label="$3"
    wait_for_log_count "$pattern" $((baseline + 1)) "$label"
}

role_requested() {
    local role="$1"
    [[ ",$RECOVERY_ROLES," == *",$role,"* ]]
}

click_menu_item() {
    local menu="$1"
    local item="$2"
    APP_PID="$APP_PID" MENU_NAME="$menu" MENU_ITEM="$item" osascript \
        -e 'set appPID to (system attribute "APP_PID") as integer' \
        -e 'set menuName to system attribute "MENU_NAME"' \
        -e 'set menuItem to system attribute "MENU_ITEM"' \
        -e 'tell application "System Events" to tell first process whose unix id is appPID' \
        -e 'set frontmost to true' \
        -e 'click (first menu item of menu 1 of menu bar item menuName of menu bar 1 whose name starts with menuItem)' \
        -e 'end tell' \
        >/dev/null
}

press_keys() {
    # keystroke text, using modifiers (AppleScript list, may be empty)
    local text="$1"
    local modifiers="$2"
    APP_PID="$APP_PID" KEY_TEXT="$text" osascript \
        -e 'set appPID to (system attribute "APP_PID") as integer' \
        -e 'set keyText to system attribute "KEY_TEXT"' \
        -e 'tell application "System Events" to tell first process whose unix id is appPID' \
        -e 'set frontmost to true' \
        -e "keystroke keyText using {$modifiers}" \
        -e 'end tell' \
        >/dev/null
}

# Two Escapes in one script: the second press must land within 2 seconds.
press_escape_twice() {
    APP_PID="$APP_PID" osascript \
        -e 'set appPID to (system attribute "APP_PID") as integer' \
        -e 'tell application "System Events" to tell first process whose unix id is appPID' \
        -e 'set frontmost to true' \
        -e 'key code 53' \
        -e 'delay 0.3' \
        -e 'key code 53' \
        -e 'end tell' \
        >/dev/null
}

# Command-Return on the console's Start (ready state).
press_command_return() {
    APP_PID="$APP_PID" osascript \
        -e 'set appPID to (system attribute "APP_PID") as integer' \
        -e 'tell application "System Events" to tell first process whose unix id is appPID' \
        -e 'set frontmost to true' \
        -e 'key code 36 using {command down}' \
        -e 'end tell' \
        >/dev/null
}

toggle_source_sidebar_shortcut() {
    press_keys "x" "command down, shift down"
}

exercise_source_sidebar() {
    local open_before
    local close_before
    open_before="$(log_count "Presto: source sidebar visible=1")"
    toggle_source_sidebar_shortcut
    wait_for_log_increment "Presto: source sidebar visible=1" "$open_before" "HTML source sidebar opening"

    close_before="$(log_count "Presto: source sidebar visible=0")"
    toggle_source_sidebar_shortcut
    wait_for_log_increment "Presto: source sidebar visible=0" "$close_before" "HTML source sidebar closing"
}

close_window_with_prefix() {
    local prefix="$1"
    APP_PID="$APP_PID" WINDOW_PREFIX="$prefix" osascript \
        -e 'set appPID to (system attribute "APP_PID") as integer' \
        -e 'set windowPrefix to system attribute "WINDOW_PREFIX"' \
        -e 'tell application "System Events" to tell first process whose unix id is appPID' \
        -e 'set frontmost to true' \
        -e 'set targetWindow to first window whose name starts with windowPrefix' \
        -e 'click (first button of targetWindow whose subrole is "AXCloseButton")' \
        -e 'end tell' \
        >/dev/null
}

# The Go To Folder sheet opens prefilled with the last path anyone went to on
# this Mac, and a Go To file path is returned even if it is not .html. So wait
# for each panel state, check the field holds deckPath before pressing Return,
# and confirm the panel exactly once instead of pressing Return until it closes
# (which opened whatever the prefilled path pointed at).
open_deck_via_file_panel() {
    local deck_path="$1"
    APP_PID="$APP_PID" DECK_PATH="$deck_path" osascript \
        -e 'set appPID to (system attribute "APP_PID") as integer' \
        -e 'set deckPath to system attribute "DECK_PATH"' \
        -e 'tell application "System Events" to tell first process whose unix id is appPID' \
        -e 'set frontmost to true' \
        -e 'click menu item "Open…" of menu 1 of menu bar item "File" of menu bar 1' \
        -e 'repeat 40 times' \
        -e 'if exists window "Open" then exit repeat' \
        -e 'delay 0.25' \
        -e 'end repeat' \
        -e 'if not (exists window "Open") then error "Open panel did not appear"' \
        -e 'repeat 3 times' \
        -e 'delay 0.5' \
        -e 'if not (exists sheet 1 of window "Open") then keystroke "g" using {command down, shift down}' \
        -e 'repeat 12 times' \
        -e 'if exists text field 1 of sheet 1 of window "Open" then exit repeat' \
        -e 'delay 0.25' \
        -e 'end repeat' \
        -e 'if exists text field 1 of sheet 1 of window "Open" then exit repeat' \
        -e 'end repeat' \
        -e 'if not (exists text field 1 of sheet 1 of window "Open") then error "Go To Folder sheet did not appear"' \
        -e 'set goField to text field 1 of sheet 1 of window "Open"' \
        -e 'repeat 3 times' \
        -e 'try' \
        -e 'set focused of goField to true' \
        -e 'end try' \
        -e 'keystroke "a" using {command down}' \
        -e 'keystroke deckPath' \
        -e 'delay 0.3' \
        -e 'if (value of goField as text) is deckPath then exit repeat' \
        -e 'end repeat' \
        -e 'if (value of goField as text) is not deckPath then error "Go To Folder field holds " & (value of goField as text) & " instead of " & deckPath' \
        -e 'key code 36' \
        -e 'repeat 40 times' \
        -e 'if not (exists sheet 1 of window "Open") then exit repeat' \
        -e 'delay 0.25' \
        -e 'end repeat' \
        -e 'if exists sheet 1 of window "Open" then error "Go To Folder sheet did not accept " & deckPath' \
        -e 'if exists window "Open" then key code 36' \
        -e 'repeat 40 times' \
        -e 'if not (exists window "Open") then exit repeat' \
        -e 'delay 0.25' \
        -e 'end repeat' \
        -e 'if exists window "Open" then error "Open panel did not accept " & deckPath' \
        -e 'end tell' \
        >/dev/null
}

wait_for_window_name() {
    local needle="$1"
    local attempt
    for ((attempt = 0; attempt < 80; attempt++)); do
        if ! /bin/kill -0 "$APP_PID" 2>/dev/null; then
            echo "FAIL: Presto exited while waiting for window containing $needle" >&2
            return 1
        fi
        if APP_PID="$APP_PID" WINDOW_NEEDLE="$needle" osascript \
            -e 'set appPID to (system attribute "APP_PID") as integer' \
            -e 'set windowNeedle to system attribute "WINDOW_NEEDLE"' \
            -e 'tell application "System Events" to tell first process whose unix id is appPID to return exists (first window whose name contains windowNeedle)' \
            2>/dev/null | grep -q true; then
            return 0
        fi
        sleep 0.25
    done
    echo "FAIL: timed out waiting for window containing $needle" >&2
    tail -100 "$LOG_FILE" >&2
    return 1
}

wait_for_window_prefix() {
    local prefix="$1"
    local attempt
    for ((attempt = 0; attempt < 80; attempt++)); do
        if ! /bin/kill -0 "$APP_PID" 2>/dev/null; then
            echo "FAIL: Presto exited while waiting for window starting with $prefix" >&2
            return 1
        fi
        if APP_PID="$APP_PID" WINDOW_PREFIX="$prefix" osascript \
            -e 'set appPID to (system attribute "APP_PID") as integer' \
            -e 'set windowPrefix to system attribute "WINDOW_PREFIX"' \
            -e 'tell application "System Events" to tell first process whose unix id is appPID to return exists (first window whose name starts with windowPrefix)' \
            2>/dev/null | grep -q true; then
            return 0
        fi
        sleep 0.25
    done
    echo "FAIL: timed out waiting for window starting with $prefix" >&2
    tail -100 "$LOG_FILE" >&2
    return 1
}

wait_for_editor_recoveries() {
    local preview_ready_baseline="$1"
    local editor_ready_baseline="$2"
    local preview_recovery_baseline="$3"
    local editor_recovery_baseline="$4"
    if role_requested "editor-preview"; then
        wait_for_log_increment "Presto: recovering editor-preview" "$preview_recovery_baseline" "editor preview recovery"
        wait_for_log_count "Presto: editor preview injected" $((preview_ready_baseline + 2)) "recovered editor preview readiness"
    fi
    if role_requested "editor"; then
        wait_for_log_increment "Presto: recovering editor after" "$editor_recovery_baseline" "editor bridge recovery"
        wait_for_log_count "Presto: editor is ready" $((editor_ready_baseline + 2)) "recovered editor bridge readiness"
    fi
}

wait_for_edit_session() {
    local preview_before="$1"
    local editor_before="$2"
    local nav_before="$3"
    local preview_recovery_before="$4"
    local editor_recovery_before="$5"
    local label="$6"
    wait_for_log_increment "Presto: editor preview injected" "$preview_before" "$label editor readiness"
    wait_for_log_increment "Presto: editor is ready" "$editor_before" "$label editor bridge readiness"
    wait_for_log_increment "name=presto type=slideChanged" "$nav_before" "$label editor navigation readiness"
    wait_for_editor_recoveries "$preview_before" "$editor_before" "$preview_recovery_before" "$editor_recovery_before"
}

# Goes live from Edit (alternating Play from Current Slide and Present then
# ⌘↩ on the console's Start), checks the audience (and in forced dual-screen
# mode the console sync and recovery), then ends the talk (alternating
# Escape twice and End Presentation, or closing the audience window) and
# checks that Edit is back without being rebuilt.
present_cycle() {
    local cycle="$1"
    local label="$2"
    local audience_ready_before presenter_ready_before audience_recovery_before presenter_recovery_before
    local audience_restore_before dual_start_before ended_before edit_mode_before preview_before
    audience_ready_before="$(log_count "Presto: audience mode injected")"
    presenter_ready_before="$(log_count "name=presto type=ready")"
    audience_recovery_before="$(log_count "Presto: recovering audience")"
    presenter_recovery_before="$(log_count "Presto: recovering presenter")"
    audience_restore_before="$(log_count "PrestoTestWebContent: restored audience .* match=1")"
    dual_start_before="$(log_count "PrestoTestDualScreen: started")"
    preview_before="$(log_count "Presto: editor preview injected")"

    if [ $((cycle % 2)) -eq 1 ]; then
        click_menu_item "Presentation" "Play from Current Slide"
    else
        local ready_before
        ready_before="$(log_count "Presto: mode=present")"
        click_menu_item "View" "Present"
        wait_for_log_increment "Presto: mode=present" "$ready_before" "$label ready console"
        wait_for_log_increment "name=presto type=ready" "$presenter_ready_before" "$label console readiness"
        presenter_ready_before="$(log_count "name=presto type=ready")"
        press_command_return
    fi
    wait_for_log_increment "Presto: audience mode injected" "$audience_ready_before" "$label audience readiness"

    if role_requested "audience" && [ "$FORCE_DUAL_SCREEN" != "1" ]; then
        wait_for_log_increment "Presto: recovering audience" "$audience_recovery_before" "$label audience recovery"
        wait_for_log_count "Presto: audience mode injected" $((audience_ready_before + 2)) "$label recovered audience readiness"
    fi

    if [ "$FORCE_DUAL_SCREEN" = "1" ]; then
        wait_for_log_increment "PrestoTestDualScreen: started" "$dual_start_before" "$label dual-screen start"
        wait_for_window_prefix "Presto [Audience]"
        wait_for_log_count "name=presto type=ready" $((presenter_ready_before + 1)) "$label presenter readiness" 120 || true
        local sync_before
        sync_before="$(log_count "PrestoTestSync: .* match=1")"
        click_menu_item "Presentation" "Next Step"
        wait_for_log_increment "PrestoTestSync: .* match=1" "$sync_before" "$label audience/presenter sync"
        if role_requested "audience"; then
            wait_for_log_increment "Presto: recovering audience" "$audience_recovery_before" "$label audience recovery"
            wait_for_log_count "Presto: audience mode injected" $((audience_ready_before + 2)) "$label recovered audience readiness"
            wait_for_log_increment "PrestoTestWebContent: restored audience .* match=1" "$audience_restore_before" "$label restored audience state"
        fi
        if role_requested "presenter"; then
            wait_for_log_increment "Presto: recovering presenter" "$presenter_recovery_before" "$label presenter recovery"
        fi
    else
        sleep 1.5  # the one-screen show's full-screen animation
    fi

    ended_before="$(log_count "Presto: presentation ended")"
    edit_mode_before="$(log_count "Presto: mode=edit")"
    if [ "$FORCE_DUAL_SCREEN" = "1" ] && [ $((cycle % 2)) -eq 0 ]; then
        close_window_with_prefix "Presto [Audience]"
    elif [ $((cycle % 2)) -eq 1 ]; then
        press_escape_twice
    else
        click_menu_item "Presentation" "End Presentation"
    fi
    wait_for_log_increment "Presto: presentation ended" "$ended_before" "$label end"
    wait_for_log_increment "Presto: mode=edit" "$edit_mode_before" "$label return to Edit"
    if [ "$(log_count "Presto: editor preview injected")" -ne "$preview_before" ] && ! role_requested "editor-preview"; then
        echo "FAIL: $label rebuilt the edit view instead of keeping it alive" >&2
        exit 1
    fi
}

decks_round_trip() {
    local label="$1"
    local decks_before edit_before
    decks_before="$(log_count "Presto: mode=decks")"
    click_menu_item "View" "Decks"
    wait_for_log_increment "Presto: mode=decks" "$decks_before" "$label Decks"
    edit_before="$(log_count "Presto: mode=edit")"
    click_menu_item "View" "Edit"
    wait_for_log_increment "Presto: mode=edit" "$edit_before" "$label back to Edit"
}

CRASHES_START="$(crash_count)"
initial_preview_ready="$(log_count "Presto: editor preview injected")"
initial_editor_ready="$(log_count "Presto: editor is ready")"
initial_editor_nav="$(log_count "name=presto type=slideChanged")"
initial_preview_recovery="$(log_count "Presto: recovering editor-preview")"
initial_editor_recovery="$(log_count "Presto: recovering editor after")"
PRESTO_TEST_LOG_NAVIGATION=1 PRESTO_TEST_EXPORT_OUTPUT="$EXPORT_OUTPUT" \
    PRESTO_TEST_WEB_CONTENT_TERMINATION_DELAY_MS="${PRESTO_TEST_WEB_CONTENT_TERMINATION_DELAY_MS:-1500}" \
    "$APP_EXECUTABLE" --edit "$DECK" >"$LOG_FILE" 2>&1 &
APP_PID=$!
wait_for_edit_session "$initial_preview_ready" "$initial_editor_ready" "$initial_editor_nav" \
    "$initial_preview_recovery" "$initial_editor_recovery" "initial"
exercise_source_sidebar

for ((cycle = 1; cycle <= CYCLES; cycle++)); do
    present_cycle "$cycle" "cycle $cycle"
    exercise_source_sidebar
    decks_round_trip "cycle $cycle"
    rss_kb="$(ps -o rss= -p "$APP_PID" | tr -d ' ')"
    echo "PASS cycle $cycle/$CYCLES (pid=$APP_PID rss_kb=$rss_kb)"
done

# Close the window (Presto keeps running), then open a second deck through
# the native file panel: it opens in Edit, and plays from there.
closed_before="$(log_count "Presto: main window closed")"
close_window_with_prefix "Presto"
wait_for_log_increment "Presto: main window closed" "$closed_before" "window close keeps the app running"
if ! /bin/kill -0 "$APP_PID" 2>/dev/null; then
    echo "FAIL: closing the window quit Presto" >&2
    exit 1
fi
SWITCH_DECK_NAME="$(basename "$(dirname "$SWITCH_DECK")")"
preview_ready_before="$(log_count "Presto: editor preview injected")"
editor_ready_before="$(log_count "Presto: editor is ready")"
editor_nav_before="$(log_count "name=presto type=slideChanged")"
preview_recovery_before="$(log_count "Presto: recovering editor-preview")"
editor_recovery_before="$(log_count "Presto: recovering editor after")"
open_deck_via_file_panel "$SWITCH_DECK"
wait_for_edit_session "$preview_ready_before" "$editor_ready_before" "$editor_nav_before" \
    "$preview_recovery_before" "$editor_recovery_before" "cross-deck"
wait_for_window_name "$SWITCH_DECK_NAME"
exercise_source_sidebar
present_cycle 1 "cross-deck presentation readiness"

if role_requested "export"; then
    export_recovery_before="$(log_count "Presto: recovering export")"
    export_success_before="$(log_count "PrestoTestExport: success=1")"
    # `system attribute` decodes the environment as MacRoman, so pass only
    # the ASCII start of "Export PDF…"; click_menu_item matches by prefix.
    click_menu_item "File" "Export PDF"
    wait_for_log_increment "Presto: recovering export" "$export_recovery_before" "export web content recovery"
    wait_for_log_count "PrestoTestExport: success=1" $((export_success_before + 1)) "successful recovered PDF export" 400
    if [ ! -s "$EXPORT_OUTPUT" ]; then
        echo "FAIL: recovered PDF export did not create a nonempty file" >&2
        exit 1
    fi
    pdfinfo "$EXPORT_OUTPUT" >"$DEFAULTS_BACKUP_DIR/presto-export-pdfinfo.txt"
    if ! rg -q '^Pages:[[:space:]]+[1-9][0-9]*$' "$DEFAULTS_BACKUP_DIR/presto-export-pdfinfo.txt"; then
        echo "FAIL: recovered PDF export is not a valid nonempty PDF" >&2
        exit 1
    fi
fi

for requested_role in audience presenter editor-preview editor export; do
    if role_requested "$requested_role" && [ "$(log_count "Presto: recovering $requested_role")" -lt 1 ]; then
        echo "FAIL: requested $requested_role recovery was not exercised" >&2
        exit 1
    fi
done

sleep 2
CRASHES_END="$(crash_count)"
if [ "$CRASHES_END" -ne "$CRASHES_START" ]; then
    echo "FAIL: lifecycle stress produced $((CRASHES_END - CRASHES_START)) crash report(s)" >&2
    exit 1
fi

echo "PASS: $CYCLES edit/present/decks lifecycle cycles plus source-sidebar, window-close, cross-deck, and recovery gates, exact PID remained alive, no crash reports"

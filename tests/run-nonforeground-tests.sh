#!/bin/bash
# Non-foreground verification suite.
# Avoids native app E2E tests that activate Presto or send real window clicks/keys.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# Optional machine-specific settings (WEBSLIDES_PYTHON, WEBSLIDES_POPPLER_BIN), untracked.
if [ -f "$SCRIPT_DIR/local.env" ]; then . "$SCRIPT_DIR/local.env"; fi
PYTHON_BIN="${WEBSLIDES_PYTHON:-$HOME/.venvs/webslides-export/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3)"
fi
PYTHON_BIN_DIR="$(dirname "$PYTHON_BIN")"
export PATH="$PYTHON_BIN_DIR:$PATH"
if [ -n "${WEBSLIDES_POPPLER_BIN:-}" ]; then
    export PATH="$WEBSLIDES_POPPLER_BIN:$PATH"
fi
STEPPED_PDF="/tmp/webslides-nonforeground-stepped.pdf"
FLAT_PDF="/tmp/webslides-nonforeground-flat.pdf"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/presto-test-build.XXXXXX")"
TMP_BINARY="$BUILD_DIR/Presto"
REFINE_TEST_BINARY="$BUILD_DIR/test_refine_subprocess"
PDF_SNAPSHOT_TEST_BINARY="$BUILD_DIR/test_pdf_snapshot"
TRANSACTION_TEST_BINARY="$BUILD_DIR/test_presto_transactions"
DECK_LIBRARY_TEST_BINARY="$BUILD_DIR/test_deck_library"

cleanup() {
    for path in \
        "$STEPPED_PDF" \
        "$FLAT_PDF" \
        "/tmp/webslides-edit-layout.png" \
        "/tmp/webslides-preview-layout.png" \
        "$BUILD_DIR"; do
        if [ -e "$path" ]; then
            trash "$path" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT

cd "$PROJECT_DIR"

echo "=== Non-Foreground WebSlides/Presto Tests ==="
echo ""

python3 -m unittest discover -s tests -p 'test_*.py'
swiftc -warnings-as-errors \
    presto/RefineSubprocess.swift \
    tests/test_refine_subprocess.swift \
    -o "$REFINE_TEST_BINARY"
"$REFINE_TEST_BINARY"
swiftc -warnings-as-errors \
    -framework Cocoa -framework PDFKit \
    presto/PDFSnapshot.swift \
    tests/test_pdf_snapshot.swift \
    -o "$PDF_SNAPSHOT_TEST_BINARY"
"$PDF_SNAPSHOT_TEST_BINARY"
swiftc -warnings-as-errors \
    presto/DeckLibrary.swift \
    tests/test_deck_library.swift \
    -o "$DECK_LIBRARY_TEST_BINARY"
"$DECK_LIBRARY_TEST_BINARY"
APP_SOURCES=(
    presto/RefineSubprocess.swift
    presto/PDFSnapshot.swift
    presto/DeckLibrary.swift
    presto/Presto.swift
    presto/PrestoWindow.swift
    presto/PrestoPresent.swift
    presto/PrestoInjection.swift
    presto/PrestoMenus.swift
)
swiftc -D PRESTO_TESTING -framework WebKit -framework Cocoa -framework PDFKit \
    "${APP_SOURCES[@]}" \
    tests/test_presto_shell_modes.swift \
    tests/test_presto_transactions.swift \
    -o "$TRANSACTION_TEST_BINARY"
"$TRANSACTION_TEST_BINARY"
swiftc -framework WebKit -framework Cocoa -framework PDFKit \
    -target "$(uname -m)-apple-macos13.0" \
    "${APP_SOURCES[@]}" \
    -o "$TMP_BINARY"
chmod +x "$TMP_BINARY"
test -x "$TMP_BINARY"
bash tests/run-tests.sh
node tests/presto-edit-transition-perf.js
node tests/chrome-wysiwyg-e2e.js
swift tests/test_presto_features.swift
python3 tools/validate_links.py examples/kitchen-sink
python3 tools/check_overflow.py examples/kitchen-sink --all --strict
python3 tools/check_style.py examples/kitchen-sink
python3 tools/export_pdf.py examples/kitchen-sink --output "$STEPPED_PDF"
python3 tools/export_pdf.py examples/kitchen-sink --flat --output "$FLAT_PDF"

echo ""
echo "NON-FOREGROUND TESTS PASSED"

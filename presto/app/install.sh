#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_BUNDLE="$SCRIPT_DIR/Presto.app"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/presto-build.XXXXXX")"
TMP_BINARY="$BUILD_DIR/Presto"
INSTALL_STAGING="/Applications/.Presto.install.$$.app"

echo "=== Presto App Installer ==="

# Ensure app bundle directories exist
mkdir -p "$SCRIPT_DIR/Presto.app/Contents/MacOS"
mkdir -p "$SCRIPT_DIR/Presto.app/Contents/Resources"

# Generate icon
echo "Generating icon..."
chmod +x "$SCRIPT_DIR/generate_icon.swift"
swift "$SCRIPT_DIR/generate_icon.swift"

# Compile Swift app
echo "Compiling Presto..."
APP_BINARY="$APP_BUNDLE/Contents/MacOS/Presto"
cleanup_tmp_binary() {
    for path in "$BUILD_DIR" "$INSTALL_STAGING"; do
        if [ ! -e "$path" ] && [ ! -L "$path" ]; then
            continue
        fi
        if command -v trash >/dev/null 2>&1; then
            trash "$path" || true
        else
            echo "Temporary build artifact left at $path" >&2
        fi
    done
}
trap cleanup_tmp_binary EXIT
TARGET_ARCH="$(uname -m)"
swiftc -framework WebKit -framework Cocoa -framework PDFKit \
    -target "${TARGET_ARCH}-apple-macos13.0" \
    "$PROJECT_DIR/presto/RefineSubprocess.swift" \
    "$PROJECT_DIR/presto/PDFSnapshot.swift" \
    "$PROJECT_DIR/presto/DeckLibrary.swift" \
    "$PROJECT_DIR/presto/Presto.swift" \
    "$PROJECT_DIR/presto/PrestoWindow.swift" \
    "$PROJECT_DIR/presto/PrestoPresent.swift" \
    "$PROJECT_DIR/presto/PrestoInjection.swift" \
    "$PROJECT_DIR/presto/PrestoMenus.swift" \
    -o "$TMP_BINARY"
chmod +x "$TMP_BINARY"
mv "$TMP_BINARY" "$APP_BINARY"

echo "Signing and verifying Presto.app..."
codesign --force --deep --sign - "$APP_BUNDLE"
codesign --verify --deep --strict "$APP_BUNDLE"
echo "Compiled and verified successfully."

# Publish a verified copy to /Applications. Keeping the installed bundle
# separate prevents tests or source rebuilds from changing the live app.
echo "Installing Presto.app to /Applications..."
ditto "$APP_BUNDLE" "$INSTALL_STAGING"
# The installed app lives outside this repo, so record where the engine is
# (read by prestoDefaultWebSlidesRoot in presto/Presto.swift), then re-sign.
plutil -replace PrestoWebSlidesRoot -string "$PROJECT_DIR" "$INSTALL_STAGING/Contents/Info.plist"
codesign --force --deep --sign - "$INSTALL_STAGING"
codesign --verify --deep --strict "$INSTALL_STAGING"
if [ -e "/Applications/Presto.app" ] || [ -L "/Applications/Presto.app" ]; then
    if ! command -v trash >/dev/null 2>&1; then
        echo "Existing /Applications/Presto.app found, but the trash command is not installed." >&2
        echo "Install trash or move /Applications/Presto.app to Trash manually, then rerun." >&2
        exit 1
    fi
    echo "Existing /Applications/Presto.app found; moving it to Trash."
    trash "/Applications/Presto.app"
fi
mv "$INSTALL_STAGING" "/Applications/Presto.app"

# Touch the app bundle to refresh Finder/Spotlight icon cache
touch "/Applications/Presto.app"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then
    "$LSREGISTER" -f "/Applications/Presto.app"
fi
codesign --verify --deep --strict "/Applications/Presto.app"

echo "=== Done! ==="
echo "Open Presto from /Applications or Spotlight."
echo "Or run: open -a Presto"
echo "Or with a deck: open -a Presto --args /path/to/deck/index.html"

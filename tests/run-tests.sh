#!/bin/bash
# WebSlides Engine Test Runner
# Starts a local server and runs browser fixtures in headless Chromium.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/run_browser_tests.py"

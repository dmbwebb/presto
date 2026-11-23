#!/usr/bin/env python3
"""Run the standard WebSlides validation suite for a deck."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
LOCATE = SKILL_DIR / "scripts" / "locate_deck.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
from _engine import engine_root_or_exit  # noqa: E402

WEBSLIDES_ROOT = engine_root_or_exit()
VALIDATE_LINKS = WEBSLIDES_ROOT / "tools" / "validate_links.py"
CHECK_OVERFLOW = WEBSLIDES_ROOT / "tools" / "check_overflow.py"
CHECK_STYLE = WEBSLIDES_ROOT / "tools" / "check_style.py"
# check_overflow needs Playwright; system python3 is 3.9 without it.
EXPORT_PYTHON = Path.home() / ".venvs" / "webslides-export" / "bin" / "python"
TOOL_PYTHON = str(EXPORT_PYTHON) if EXPORT_PYTHON.exists() else sys.executable


def resolve_index(deck: str) -> Path:
    candidate = Path(deck).expanduser()
    if candidate.exists():
        if candidate.is_dir():
            return candidate / "index.html"
        return candidate

    result = subprocess.run(
        [sys.executable, str(LOCATE), deck, "--json"],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    return Path(payload["index"])


def run(cmd: list[str]) -> int:
    print("+ " + " ".join(cmd))
    proc = subprocess.run(cmd)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deck", help="Deck id, alias, directory, or index.html")
    parser.add_argument("--skip-overflow", action="store_true")
    parser.add_argument("--skip-style", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    index = resolve_index(args.deck).resolve()
    if not index.exists():
        print(f"Missing deck index: {index}", file=sys.stderr)
        return 1

    rc = run([TOOL_PYTHON, str(VALIDATE_LINKS), str(index)])
    if rc != 0:
        return rc

    if not args.skip_overflow:
        cmd = [TOOL_PYTHON, str(CHECK_OVERFLOW), str(index), "--all", "--strict"]
        if args.verbose:
            cmd.append("--verbose")
        rc = run(cmd)

    if not args.skip_style:
        # Errors (tiny text, clipped or footer-overlapping content, failed
        # loads, KaTeX errors) fail; warnings are printed for the author.
        # Runs even when overflow failed, so one run shows everything.
        rc = run([TOOL_PYTHON, str(CHECK_STYLE), str(index)]) or rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Export a deck to PDF (resolves deck aliases)."""

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
EXPORT_PDF = WEBSLIDES_ROOT / "tools" / "export_pdf.py"
# The tool needs Python >= 3.10 and Playwright; system python3 is 3.9 without it.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deck", help="Deck id, alias, directory, or index.html")
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="Flags passed through (e.g. --output talk.pdf, --flat)")
    args = parser.parse_args()

    index = resolve_index(args.deck).resolve()
    if not index.exists():
        print(f"Missing deck index: {index}", file=sys.stderr)
        return 1

    cmd = [TOOL_PYTHON, str(EXPORT_PDF), str(index), *args.rest]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())

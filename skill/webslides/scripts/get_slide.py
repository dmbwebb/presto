#!/usr/bin/env python3
"""Print HTML for one or more slides in a deck (resolves deck aliases)."""

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
GET_SLIDE = WEBSLIDES_ROOT / "tools" / "get_slide.py"


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
                        help="Slide numbers, HTML ids, and/or flags passed to engine get_slide.py "
                             "(e.g. 5 6 7, appendix-pubbias, --list, --title-only)")
    args = parser.parse_args()

    index = resolve_index(args.deck).resolve()
    if not index.exists():
        print(f"Missing deck index: {index}", file=sys.stderr)
        return 1

    cmd = [sys.executable, str(GET_SLIDE), str(index), *args.rest]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())

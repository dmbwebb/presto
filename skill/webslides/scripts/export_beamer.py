#!/usr/bin/env python3
"""Export a WebSlides deck to a Beamer/Overleaf project (resolves deck aliases).

Thin wrapper around the engine tool <engine>/tools/export_beamer.py (the
engine repo is found by scripts/_engine.py). It runs the tool under the export
venv (Playwright + bs4 + lxml + pypdf + Pillow), resolves deck aliases via
locate_deck.py, and passes a deck's `tex_dir` from deck_locations.yaml as
`--tex-dir` unless one is given.

The deck is a single argument, so quote a multi-word alias.

Examples:
    python3 scripts/export_beamer.py "my talk" --output /path/to/out --compile --zip
    python3 scripts/export_beamer.py my-talk --output out --compile --slides 1-28
"""

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
EXPORT_BEAMER = WEBSLIDES_ROOT / "tools" / "export_beamer.py"
EXPORT_PYTHON = Path("~/.venvs/webslides-export/bin/python").expanduser()


def resolve_deck(deck: str) -> dict:
    candidate = Path(deck).expanduser()
    if candidate.exists():
        index = candidate / "index.html" if candidate.is_dir() else candidate
        return {"index": str(index)}

    result = subprocess.run(
        [sys.executable, str(LOCATE), deck, "--json"],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("deck", help="Deck id, alias, directory, or index.html")
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="Flags passed through to tools/export_beamer.py "
                             "(--output DIR, --compile, --zip, --slides, --tables, "
                             "--raster-slides, --tex-dir, ...)")
    args = parser.parse_args()

    if not EXPORT_PYTHON.exists():
        print(f"Export venv missing: {EXPORT_PYTHON}\n"
              "Create it with: ~/.local/bin/python3.12 -m venv ~/.venvs/webslides-export && "
              "~/.venvs/webslides-export/bin/pip install playwright beautifulsoup4 lxml pypdf pillow "
              "&& ~/.venvs/webslides-export/bin/python -m playwright install chromium",
              file=sys.stderr)
        return 1
    if not EXPORT_BEAMER.exists():
        print(f"Engine tool missing: {EXPORT_BEAMER}", file=sys.stderr)
        return 1

    info = resolve_deck(args.deck)
    index = Path(info["index"]).resolve()
    if not index.exists():
        print(f"Missing deck index: {index}", file=sys.stderr)
        return 1

    rest = list(args.rest)
    tex_dir = info.get("tex_dir")
    if tex_dir and "--tex-dir" not in rest:
        rest += ["--tex-dir", str(Path(tex_dir).expanduser())]

    cmd = [str(EXPORT_PYTHON), str(EXPORT_BEAMER), str(index), *rest]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())

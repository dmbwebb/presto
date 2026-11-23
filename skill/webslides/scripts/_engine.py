#!/usr/bin/env python3
"""Find the WebSlides engine repo (the directory holding engine/webslides.js).

Resolution order:
  1. the WEBSLIDES_HOME environment variable;
  2. "engine_root" in the private file scripts/local.json next to this script;
  3. the repo that contains this skill, when the skill lives at
     <repo>/skill/webslides/ (the layout of the public WebSlides repo).

Run it directly to print the resolved root:

    python3 ~/.claude/skills/webslides/scripts/_engine.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG = SCRIPTS_DIR / "local.json"
MARKER = Path("engine") / "webslides.js"


class EngineNotFound(RuntimeError):
    """The engine repo could not be located."""


def _is_engine(path: Path) -> bool:
    return (path / MARKER).is_file()


def _checked(path: Path, source: str) -> Path:
    path = path.expanduser().absolute()
    if not _is_engine(path):
        raise EngineNotFound(f"{source} points to {path}, which has no {MARKER}.")
    return path


def engine_root() -> Path:
    env = os.environ.get("WEBSLIDES_HOME")
    if env:
        return _checked(Path(env), "WEBSLIDES_HOME")

    if LOCAL_CONFIG.is_file():
        try:
            configured = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8")).get("engine_root")
        except (OSError, ValueError) as exc:
            raise EngineNotFound(f"Could not read {LOCAL_CONFIG}: {exc}") from exc
        if configured:
            return _checked(Path(configured), str(LOCAL_CONFIG))

    # SCRIPTS_DIR is <repo>/skill/webslides/scripts, so parents[2] is <repo>
    parents = SCRIPTS_DIR.parents
    if len(parents) > 2 and _is_engine(parents[2]):
        return parents[2]

    raise EngineNotFound(
        "Cannot find the WebSlides engine repo (the folder containing engine/webslides.js).\n"
        "Set WEBSLIDES_HOME to its path, e.g. add to your shell profile:\n"
        "    export WEBSLIDES_HOME=~/code/webslides\n"
        f"or create {LOCAL_CONFIG} containing:\n"
        '    {"engine_root": "/path/to/webslides"}\n'
        "or install the skill as a symlink into the repo:\n"
        "    ln -s /path/to/webslides/skill/webslides ~/.claude/skills/webslides"
    )


def engine_root_or_exit() -> Path:
    """engine_root() for command-line wrappers: print the problem and exit instead of a traceback."""
    try:
        return engine_root()
    except EngineNotFound as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    print(engine_root_or_exit())

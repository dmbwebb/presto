#!/usr/bin/env python3
"""Forward to the engine repo's tools/ws.py (outline, get, put, check, diff, layout, steps).

tools/ws.py resolves deck aliases itself and re-runs browser commands under the
export venv, so this wrapper only saves typing the repo path (found by
scripts/_engine.py).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _engine import engine_root_or_exit  # noqa: E402

WS = str(engine_root_or_exit() / "tools" / "ws.py")

os.execv(sys.executable, [sys.executable, WS] + sys.argv[1:])

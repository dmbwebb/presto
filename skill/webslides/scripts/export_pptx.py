#!/usr/bin/env python3
"""Export a deck to PowerPoint.

    python3 export_pptx.py <deck-alias|dir|index.html> --output talk.pptx
    python3 export_pptx.py <claude-slides-project-dir> --output talk.pptx
    python3 export_pptx.py <deck> --output talk.pptx --images [--width 1920] [--quality 86]

Default: an editable file (engine tool tools/export_pptx.py). One PowerPoint slide per
deck slide, with native text, bullets, tables, shapes, charts and click animations.

--images: the older picture export, one full-slide image per reveal step, with speaker
notes split at literal "[click]" markers. Nothing is editable, but it looks identical on
any computer, so keep it for presenting on an unfamiliar machine.
Size: 1920 px JPEG at quality 86 gave 8.7 MB for 40 steps (PNG at 2560 was 70 MB).
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from _engine import engine_root_or_exit  # noqa: E402

ENGINE_TOOL = engine_root_or_exit() / "tools" / "export_pptx.py"
EXPORT_PYTHON = Path.home() / ".venvs" / "webslides-export" / "bin" / "python"
_LOCAL_PDFTOPPM = Path.home() / ".local/bin/pdftoppm"
if _LOCAL_PDFTOPPM.exists():
    PDFTOPPM = str(_LOCAL_PDFTOPPM)
else:
    PDFTOPPM = shutil.which("pdftoppm") or "pdftoppm"

try:
    from pptx import Presentation
    from pptx.util import Emu
except ImportError:  # re-run under the export venv, which has python-pptx
    if EXPORT_PYTHON.exists() and Path(sys.executable) != EXPORT_PYTHON:
        os.execv(str(EXPORT_PYTHON), [str(EXPORT_PYTHON), __file__, *sys.argv[1:]])
    raise

sys.path.insert(0, str(SCRIPTS))
from export_pdf import resolve_index  # noqa: E402


def step_counts(index: Path) -> list[int]:
    out = subprocess.run([sys.executable, str(SCRIPTS / "ws.py"), "outline", str(index)],
                         check=True, text=True, capture_output=True).stdout
    counts = []
    for line in out.splitlines():
        m = re.search(r"\[(\d+)(?:-(\d+))?\]", line)
        if m:
            counts.append(int(m.group(2) or m.group(1)) + 1)
    return counts


def notes_per_step(index: Path, counts: list[int]) -> list[list[str]]:
    src = index.read_text()
    sections = re.findall(r'<section class="slide[^"]*"[^>]*>(.*?)</section>', src, re.S)
    if len(sections) != len(counts):
        sys.exit(f"{len(sections)} <section> blocks but ws outline lists {len(counts)} slides")
    result = []
    for sec, n in zip(sections, counts):
        m = re.search(r'<aside class="notes">(.*?)</aside>', sec, re.S)
        text = html.unescape(m.group(1).strip()) if m else ""
        spoken, _, extra = text.partition("\n\n(")
        parts = [p.strip() for p in spoken.split("[click]")]
        if len(parts) > n:  # more markers than steps: fold the overflow into the last step
            parts = parts[: n - 1] + [" ".join(parts[n - 1:])]
        parts += [""] * (n - len(parts))
        if extra:
            parts[-1] = (parts[-1] + "\n\n(" + extra).strip()
        result.append(parts)
    return result


def export_images(args) -> int:
    index = resolve_index(args.deck).resolve()
    counts = step_counts(index)
    notes = notes_per_step(index, counts)
    total = sum(counts)
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "stepped.pdf"
        subprocess.run([sys.executable, str(SCRIPTS / "export_pdf.py"), str(index), "--output", str(pdf)], check=True)
        h = round(args.width * 9 / 16)
        subprocess.run([PDFTOPPM, "-jpeg", "-jpegopt", f"quality={args.quality}", "-scale-to-x", str(args.width),
                        "-scale-to-y", str(h), str(pdf), f"{tmp}/p"], check=True)
        pages = sorted(Path(tmp).glob("p-*.jpg"))
        if len(pages) != total:
            sys.exit(f"stepped PDF has {len(pages)} pages but ws outline counts {total} steps")
        prs = Presentation()
        prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)  # 13.333 x 7.5 in
        step_notes = [text for parts in notes for text in parts]
        for page, text in zip(pages, step_notes):
            s = prs.slides.add_slide(prs.slide_layouts[6])
            s.shapes.add_picture(str(page), 0, 0, prs.slide_width, prs.slide_height)
            s.notes_slide.notes_text_frame.text = text
        prs.save(args.output)
    size = Path(args.output).stat().st_size / 1e6
    print(f"wrote {args.output}: {total} slides from {len(counts)} deck slides, {size:.1f} MB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("deck")
    ap.add_argument("--output", required=True)
    ap.add_argument("--images", action="store_true", help="picture export: one image slide per reveal step")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--quality", type=int, default=86)
    args = ap.parse_args()
    if args.images:
        return export_images(args)
    project = Path(args.deck).expanduser()
    deck = str(project) if (project / "deck.json").exists() else str(resolve_index(args.deck).resolve())
    return subprocess.run([str(EXPORT_PYTHON), str(ENGINE_TOOL), deck, "--output", args.output]).returncode


if __name__ == "__main__":
    raise SystemExit(main())

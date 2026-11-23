#!/usr/bin/env python3
"""Screenshot slides of a WebSlides deck exactly as the audience sees them.

Usage:
    python3 tools/screenshot_slide.py deck/index.html 25                 # every step of slide 25
    python3 tools/screenshot_slide.py deck/index.html 25 --step 2 --output /tmp/s.png
    python3 tools/screenshot_slide.py deck/index.html 25 --output /tmp/s25   # /tmp/s25_step_N.png
    python3 tools/screenshot_slide.py deck/index.html 3 7 appendix-balance  # several slides
    python3 tools/screenshot_slide.py deck/index.html 5-12 --contact-sheet /tmp/sheet.png
    python3 tools/screenshot_slide.py deck/index.html all --final-only --contact-sheet /tmp/deck.png

Slides are 1-based deck positions (the same N as ``?slide=N`` and
``#slide-N``; appendix slides count), ranges such as ``5-12``, slide or
element ids (``#`` optional), or ``all``.

The full deck is loaded once in headless Chromium (served over local HTTP;
nothing is written next to the deck), the tool waits for ``WS.ready`` (or the
fallback poll on older engines), then navigates with ``WS.goToSlide`` and
captures the viewport: the real footer, slide number, section navigation and
progress bar, JS-drawn charts and end-of-body scripts included. Step counts
come from the engine, so declared ``data-steps`` is not needed.

Outputs: by default ``/tmp/slide_<N>_step_<S>.png``. ``--output`` is the exact
PNG path for a single slide and step, otherwise a prefix (``<prefix>_step_<S>.png``
for one slide, ``<prefix>_slide_<N>_step_<S>.png`` for several).
``--contact-sheet PATH`` writes labelled grids instead (24 images per sheet;
more become PATH-2.png, ...), plus individual PNGs only when ``--output`` is
also given.
"""

from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import uuid
from io import BytesIO
from pathlib import Path

if __name__ == "__main__":
    from _common import ensure_tool_python

    ensure_tool_python(("playwright", "PIL"))

from _common import (  # noqa: E402
    DECK_STRUCTURE_JS,
    SLIDE_HEIGHT,
    SLIDE_INFO_JS,
    SLIDE_WIDTH,
    DeckServer,
    check_deck_file,
    parse_slide_selection,
    raise_for_page_errors,
    resolve_deck_path,
    validate_browser_deck_structure,
    wait_for_deck_ready_sync,
)

NO_TRANSITIONS_CSS = "*, *::before, *::after { transition: none !important; animation: none !important; }"

NAVIGATE_JS = """([index, step]) => {
    WS.goToSlide(index, step);
    const state = WS.getState();
    if (!state || state.index !== index || state.step !== step) {
        throw new Error(`navigation mismatch: requested ${index}/${step}, got ${state ? state.index + '/' + state.step : 'none'}`);
    }
    const refit = typeof WS.refitTables === 'function' ? WS.refitTables() : Promise.resolve();
    return Promise.resolve(refit).then(() => new Promise(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)));
    }));
}"""

SHEET_PAGE_SIZE = 24


# ---------------------------------------------------------------- output helpers
def validate_runtime_options(args) -> None:
    """Validate numeric CLI options before launching the browser."""
    if args.step is not None and args.step < 0:
        sys.exit("ERROR: step must be >= 0.")
    if args.scale < 1:
        sys.exit("ERROR: scale must be >= 1.")
    if args.timeout <= 0:
        sys.exit("ERROR: timeout must be > 0.")
    if args.columns < 1:
        sys.exit("ERROR: columns must be >= 1.")
    if args.thumb_width < 160:
        sys.exit("ERROR: thumb-width must be >= 160.")


def prepare_output_paths(outputs) -> None:
    """Check (step-or-key, path) outputs and create parent directories before capture."""
    seen_outputs = set()
    for _key, output in outputs:
        output = Path(output).expanduser()
        resolved = output.resolve()
        if resolved in seen_outputs:
            raise ValueError(f"Duplicate screenshot output path: {output}")
        seen_outputs.add(resolved)

    for _key, output in outputs:
        output = Path(output).expanduser()
        if output.exists() and output.is_dir():
            raise ValueError(f"Output path is a directory, not a PNG file: {output}")
        if output.suffix.lower() != ".png":
            raise ValueError(f"Output path is not a PNG file: {output}")
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"could not prepare output directory {output.parent}: {exc}") from exc


def png_dimensions(output: Path) -> tuple[int, int]:
    """Return PNG dimensions from the IHDR header."""
    with Path(output).open("rb") as f:
        header = f.read(24)
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        raise ValueError("screenshot is not a PNG")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ValueError(f"screenshot has invalid dimensions ({width}×{height})")
    return width, height


def validate_screenshot_output(
    output: Path,
    expected_width: int | None = None,
    expected_height: int | None = None,
) -> int:
    """Return screenshot size in bytes, rejecting corrupt or implausible files."""
    output = Path(output).expanduser()
    if not output.exists():
        raise ValueError("screenshot not created")
    size = output.stat().st_size
    if size < 1024:
        raise ValueError(f"screenshot too small ({size} bytes)")
    width, height = png_dimensions(output)
    if (
        expected_width is not None
        and expected_height is not None
        and (width != expected_width or height != expected_height)
    ):
        raise ValueError(
            f"screenshot has unexpected dimensions ({width}×{height}, expected "
            f"{expected_width}×{expected_height})"
        )
    return size


def trash_path(path: Path, *, required: bool = True) -> None:
    """Move a generated path to Trash when it exists."""
    if not path.exists():
        return
    try:
        subprocess.run(["trash", str(path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        if required:
            raise RuntimeError(f"Could not move existing output to Trash: {path}") from exc
        print(f"Warning: could not move temporary path to Trash: {path}", file=sys.stderr)


def write_png_atomically(data: bytes, output: Path, expected_width=None, expected_height=None) -> int:
    """Write PNG bytes through a validated sibling temp file, then replace the output."""
    output = Path(output).expanduser()
    temp_output = output.with_name(f".{output.stem}.tmp-{uuid.uuid4().hex}.png")
    try:
        temp_output.write_bytes(data)
        size = validate_screenshot_output(temp_output, expected_width, expected_height)
        temp_output.replace(output)
        return size
    finally:
        trash_path(temp_output, required=False)


# ---------------------------------------------------------------- planning
def plan_captures(selected, slide_infos, step=None, final_only=False):
    """Return [(slide_index, step)] in deck order, validating --step against engine counts."""
    plan = []
    for index in selected:
        max_step = slide_infos[index]["maxStep"]
        if step is not None:
            if step > max_step:
                raise ValueError(
                    f"step {step} out of range for slide {index + 1} (it has steps 0-{max_step})"
                )
            plan.append((index, step))
        elif final_only:
            plan.append((index, max_step))
        else:
            plan.extend((index, s) for s in range(max_step + 1))
    return plan


def output_paths(plan, output=None, one_step=False):
    """Map each (slide_index, step) to a PNG path following the CLI rules.

    ``one_step``: the user asked for a single step (``--step`` or
    ``--final-only``), so ``--output`` names the file itself when only one
    slide is captured.
    """
    if output is None:
        return [Path(f"/tmp/slide_{i + 1}_step_{s}.png") for i, s in plan]
    output = Path(output).expanduser()
    single_slide = len({i for i, _s in plan}) == 1
    if single_slide and one_step:
        return [output]
    base = output.parent / output.stem if output.suffix.lower() == ".png" else output
    if single_slide:
        return [Path(f"{base}_step_{s}.png") for _i, s in plan]
    return [Path(f"{base}_slide_{i + 1}_step_{s}.png") for i, s in plan]


def sheet_paths(path, count):
    path = Path(path).expanduser()
    pages = max(1, math.ceil(count / SHEET_PAGE_SIZE))
    return [path] + [path.with_name(f"{path.stem}-{n}{path.suffix}") for n in range(2, pages + 1)]


# ---------------------------------------------------------------- capture
def capture_deck(deck_path, specs, *, step=None, final_only=False, scale=2, timeout_ms=30000, before_capture=None):
    """Load the deck once and return (slide_infos, [(slide_index, step, png_bytes)]).

    ``before_capture(slide_infos, plan)`` runs after the plan is known and
    before any screenshot, so callers can validate output paths first.
    """
    from playwright.sync_api import sync_playwright

    with DeckServer(deck_path) as served, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT},
                device_scale_factor=scale,
            )
            page_errors = []
            page.on("pageerror", lambda e: page_errors.append(e))
            page.goto(served.url, wait_until="load")
            validate_browser_deck_structure(page.evaluate(DECK_STRUCTURE_JS))
            raise_for_page_errors(page_errors)
            wait_for_deck_ready_sync(page, page_errors, timeout_ms=timeout_ms)
            page.add_style_tag(content=NO_TRANSITIONS_CSS)
            slide_infos = page.evaluate(SLIDE_INFO_JS)
            selected = parse_slide_selection(specs, slide_infos)
            if not selected:
                raise ValueError("no slides selected")
            plan = plan_captures(selected, slide_infos, step=step, final_only=final_only)
            if before_capture is not None:
                before_capture(slide_infos, plan)
            shots = []
            for index, s in plan:
                page.evaluate(NAVIGATE_JS, [index, s])
                shots.append((index, s, page.screenshot()))
            raise_for_page_errors(page_errors)
            return slide_infos, shots
        finally:
            browser.close()


def label_for(info, step):
    name = f"Slide {info['index'] + 1}"
    if info.get("isAppendix"):
        name += " (appendix)"
    if info.get("id"):
        name += f" #{info['id']}"
    return f"{name} · step {step}/{info['maxStep']}"


def build_contact_sheet(shots, slide_infos, title, columns=4, thumb_width=480):
    """Compose (slide_index, step, png_bytes) shots into one labelled PNG grid."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size):
        for candidate in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default()

    thumb_h = round(thumb_width * SLIDE_HEIGHT / SLIDE_WIDTH)
    pad, label_h, header_h = 14, 26, 40
    cols = min(columns, len(shots))
    rows = math.ceil(len(shots) / cols)
    sheet = Image.new("RGB", (pad + cols * (thumb_width + pad), header_h + rows * (label_h + thumb_h + pad)), "white")
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, 10), title, fill="#222222", font=font(18))
    label_font = font(14)
    for n, (index, step, data) in enumerate(shots):
        r, c = divmod(n, cols)
        x = pad + c * (thumb_width + pad)
        y = header_h + r * (label_h + thumb_h + pad)
        info = slide_infos[index]
        label = label_for(info, step)
        if info.get("title"):
            label += f"  {info['title']}"
        while len(label) > 8 and draw.textlength(label, font=label_font) > thumb_width:
            label = label[:-2].rstrip() + "…"
        draw.text((x, y + 4), label, fill="#333333", font=label_font)
        thumb = Image.open(BytesIO(data)).convert("RGB").resize((thumb_width, thumb_h), Image.LANCZOS)
        sheet.paste(thumb, (x, y + label_h))
        draw.rectangle([x - 1, y + label_h - 1, x + thumb_width, y + label_h + thumb_h], outline="#BBBBBB")
    out = BytesIO()
    sheet.save(out, "PNG")
    return out.getvalue(), sheet.size


# ---------------------------------------------------------------- main
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Screenshot WebSlides slides as the audience sees them (one browser session).",
    )
    parser.add_argument("deck", help="Path to deck index.html or deck directory")
    parser.add_argument("slides", nargs="+",
                        help="1-based slide numbers (deck order, appendix included), ranges like 5-12, "
                             "slide or element ids, or 'all'")
    parser.add_argument("--step", type=int, default=None, help="Capture this step only (default: every step)")
    parser.add_argument("--final-only", action="store_true", help="Capture only each slide's last step")
    parser.add_argument("--output", "-o", default=None,
                        help="Exact PNG path for one slide and step, otherwise a path prefix")
    parser.add_argument("--contact-sheet", default=None,
                        help="Write a labelled grid PNG of all captures (24 per sheet)")
    parser.add_argument("--columns", type=int, default=4, help="Contact-sheet columns (default 4)")
    parser.add_argument("--thumb-width", type=int, default=480, help="Contact-sheet thumbnail width (default 480)")
    parser.add_argument("--scale", type=int, default=2, help="Device scale factor (default: 2)")
    parser.add_argument("--timeout", type=int, default=30000, help="Deck readiness timeout in ms (default: 30000)")
    return parser.parse_args(argv)


def run(args):
    deck_path = check_deck_file(resolve_deck_path(args.deck))
    deck_resolved = deck_path.resolve()
    write_individual = args.contact_sheet is None or args.output is not None
    one_step = args.step is not None or args.final_only
    planned = {}

    def check_outputs(slide_infos, plan):
        outputs = output_paths(plan, args.output, one_step=one_step) if write_individual else []
        sheets = sheet_paths(args.contact_sheet, len(plan)) if args.contact_sheet else []
        all_paths = outputs + sheets
        for path in all_paths:
            if Path(path).expanduser().resolve() == deck_resolved:
                raise ValueError(f"Output path must not be the input deck: {deck_path}")
        prepare_output_paths(list(enumerate(all_paths)))
        slides = len({i for i, _s in plan})
        if slides == 1 and not one_step:
            first = slide_infos[plan[0][0]]
            print(f"Slide {first['index'] + 1}: {len(plan)} step(s) (0-{first['maxStep']})")
        elif slides > 1:
            print(f"{slides} slide(s), {len(plan)} capture(s)")
        planned["outputs"], planned["sheets"] = outputs, sheets

    slide_infos, shots = capture_deck(
        deck_path, args.slides, step=args.step, final_only=args.final_only,
        scale=args.scale, timeout_ms=args.timeout, before_capture=check_outputs,
    )
    for (_index, _step, data), output in zip(shots, planned["outputs"]):
        size = write_png_atomically(data, output, SLIDE_WIDTH * args.scale, SLIDE_HEIGHT * args.scale)
        print(f"Screenshot saved: {output} ({size} bytes)")
    deck_title = f"{deck_path.parent.name}/{deck_path.name}"
    for n, sheet_path in enumerate(planned["sheets"]):
        chunk = shots[n * SHEET_PAGE_SIZE:(n + 1) * SHEET_PAGE_SIZE]
        title = deck_title
        if len(planned["sheets"]) > 1:
            title += f"  (sheet {n + 1} of {len(planned['sheets'])})"
        data, (w, h) = build_contact_sheet(chunk, slide_infos, title, args.columns, args.thumb_width)
        size = write_png_atomically(data, sheet_path, w, h)
        print(f"Contact sheet saved: {sheet_path} ({len(chunk)} capture(s), {size} bytes)")


def main(argv=None):
    args = parse_args(argv)
    validate_runtime_options(args)
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        PlaywrightError = RuntimeError
    try:
        run(args)
    except (OSError, RuntimeError, ValueError, ImportError, PlaywrightError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

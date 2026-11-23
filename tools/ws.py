#!/usr/bin/env python3
"""ws: one short-output entry point for editing WebSlides decks.

    ws outline DECK [--steps] [--json]          one line per slide (and per step)
    ws get DECK SLIDE [--out FILE]              the slide's <section> exactly as in the file
    ws put DECK SLIDE FILE|- [--expect-hash H]  replace exactly that <section> (atomic)
    ws check DECK --slides 10,12-13 [--steps 3,4|all|last] [--diff] [--pdf [PATH]]
    ws diff DECK --slides 12 [--steps all]      visual diff against the last capture
    ws layout DECK SLIDE [--step 3] [--json]    overlaps, overflow, clipping, small text
    ws steps DECK SLIDE insert-after 2 | delete 5 | swap 3 4 [--include-svg] [--dry-run]

DECK is an index.html, a deck directory, or a deck alias (resolved with the
WebSlides skill's locate_deck.py). SLIDE is a 1-based deck position (appendix
slides count, as in ``?slide=N``), a range ``12-13``, a stable slide id (the
section's ``id`` or ``data-id``, else a slug of its title, as printed by
``ws outline``), the id of an element inside the slide, or part of the title.

Captures are at device scale 1. Every capture also becomes the baseline that
``--diff`` compares against, cached in ~/.cache/webslides/shots/<deck-key>/.
Images go to /tmp/ws/<deck-key>/ unless ``--out DIR``.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BROWSER_COMMANDS = {"outline", "check", "diff", "layout"}

if __name__ == "__main__":
    from _common import ensure_tool_python

    if len(sys.argv) > 1 and sys.argv[1] in BROWSER_COMMANDS:
        ensure_tool_python(("playwright", "PIL"))

import ws_deck  # noqa: E402
import ws_steps  # noqa: E402
from ws_deck import DeckError  # noqa: E402
from ws_steps import StepsError  # noqa: E402


def emit(line=""):
    print(line, flush=True)


def outline_line(slide: dict, max_step) -> str:
    kind = "A " if slide["is_appendix"] else ""
    steps = "?" if max_step is None else (f"0-{max_step}" if max_step else "0")
    title = slide["title"] or "(untitled)"
    if len(title) > 70:
        title = title[:68] + "…"
    return f"{slide['number']:>3} {kind}#{slide['key']}  [{steps}]  {title}"


# ---------------------------------------------------------------- get / put
def cmd_get(args):
    index, _text, slides = ws_deck.load_deck(args.deck)
    slide = ws_deck.select_one(args.slide, slides)
    header = (f"slide {slide['number']} #{slide['key']}  {index.name} lines {slide['line_start']}-"
              f"{slide['line_end']}  hash {slide['hash']}  (put back: ws put DECK {slide['number']} FILE "
              f"--expect-hash {slide['hash']})")
    if args.out:
        out = Path(args.out).expanduser()
        out.write_text(slide["html"] + "\n", encoding="utf-8")
        emit(header.replace("  (put back", f"  -> {out}  (put back"))
        return 0
    print(header, file=sys.stderr, flush=True)
    sys.stdout.write(slide["html"] + "\n")
    return 0


def cmd_put(args):
    index = ws_deck.resolve_deck(args.deck)
    if args.file == "-":
        new_html = sys.stdin.read()
    else:
        new_html = Path(args.file).expanduser().read_text(encoding="utf-8")
    old, new = ws_deck.put_slide(index, args.slide, new_html, expect_hash=args.expect_hash)
    max_step = static_max_step(index, new)
    same = " (unchanged)" if old["html"] == new["html"] else ""
    emit(outline_line(new, max_step) + f"   lines {new['line_start']}-{new['line_end']}  "
         f"hash {new['hash']}{same}")
    return 0


def static_max_step(index: Path, slide: dict):
    try:
        model = ws_steps.build_model(index, ws_deck.read_deck(index), slide)
        return model["max"]
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- steps
def cmd_steps(args):
    index, text, slides = ws_deck.load_deck(args.deck)
    slide = ws_deck.select_one(args.slide, slides)
    op = args.op
    nums = args.numbers
    need = 2 if op == "swap" else 1
    if len(nums) != need:
        raise StepsError(f"{op} takes {need} step number(s)")
    a = nums[0]
    b = nums[1] if op == "swap" else None
    if op == "swap" and a > b:
        a, b = b, a
    report, texts = ws_steps.renumber(index, text, slide, op, a, b, merge=args.merge,
                                      include_assets=args.include_svg, all_slides=slides,
                                      html_only=args.html_only)
    for line in ws_steps.format_report(report, slide, dry_run=args.dry_run):
        emit(line)
    if report["errors"]:
        emit("nothing written")
        return 1
    if not texts:
        emit("nothing to change")
        return 0
    if args.dry_run:
        return 0
    # Write assets first, the deck last (Presto watches the deck).
    ws_deck.ensure_unchanged(index, text)
    for path, new_text in sorted(texts.items(), key=lambda kv: kv[0] == index):
        data = new_text.encode("utf-8", errors="surrogateescape")
        ws_deck.atomic_write(path, data)
    return 0


# ---------------------------------------------------------------- browser commands
def cmd_browser(args):
    import ws_browser

    return ws_browser.run(args)


# ---------------------------------------------------------------- CLI
def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="ws", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    o = sub.add_parser("outline", help="one line per slide: number, id, [steps], title")
    o.add_argument("deck")
    o.add_argument("slides", nargs="*", help="only these slides (default: all)")
    o.add_argument("--steps", action="store_true", help="also one indented line per step")
    o.add_argument("--timeout", type=int, default=30000, help="deck readiness timeout in ms")
    o.add_argument("--json", action="store_true")

    g = sub.add_parser("get", help="print a slide's <section> exactly as in the file")
    g.add_argument("deck")
    g.add_argument("slide")
    g.add_argument("--out", help="write the section to this file instead of stdout")

    u = sub.add_parser("put", help="replace a slide's <section> from FILE or - (stdin)")
    u.add_argument("deck")
    u.add_argument("slide")
    u.add_argument("file")
    u.add_argument("--expect-hash", help="refuse unless the slide still has this hash (printed by get)")

    for name in ("check", "diff"):
        c = sub.add_parser(name, help=("links, overflow, style and layout checks plus captures"
                                       if name == "check" else "capture and compare with the last capture"))
        c.add_argument("deck")
        c.add_argument("slides_pos", nargs="*", metavar="SLIDES", help="slides (same as --slides)")
        c.add_argument("--slides", action="append", default=[], help="e.g. 10,12-13 or an id")
        c.add_argument("--steps", default="last", help="steps to capture: 3,4 | 2-4 | all | last (default)")
        c.add_argument("--out", help="directory for images (default /tmp/ws/<deck-key>)")
        c.add_argument("--each", action="store_true", help="one PNG per step instead of a contact sheet")
        c.add_argument("--before", action="store_true", help="with --diff: put the old crop above the new one")
        c.add_argument("--no-shots", action="store_true", help="checks only, no images")
        c.add_argument("--timeout", type=int, default=30000, help="deck readiness timeout in ms")
        c.add_argument("--json", action="store_true")
        if name == "check":
            c.add_argument("--diff", action="store_true", help="write only crops of what changed")
            c.add_argument("--pdf", nargs="?", const="", default=None, metavar="PATH",
                           help="also export the stepped PDF (default path: export_pdf's)")
            c.add_argument("--strict", action="store_true", help="exit 1 on warnings too")

    lay = sub.add_parser("layout", help="overlapping text, overflow, clipped text, small text")
    lay.add_argument("deck")
    lay.add_argument("slides", nargs="+")
    lay.add_argument("--step", default="all", help="a step, 2-4, all (default) or last")
    lay.add_argument("--floor", type=float, default=None,
                     help="font-size floor in px (default: 20 on main slides with footnote-style classes at 14, "
                          "14 on appendix slides)")
    lay.add_argument("--marks", action="store_true", help="also report SVG text over data marks")
    lay.add_argument("--timeout", type=int, default=30000)
    lay.add_argument("--json", action="store_true")

    s = sub.add_parser("steps", help="renumber reveal steps on one slide")
    s.add_argument("deck")
    s.add_argument("slide")
    s.add_argument("op", choices=["insert-after", "delete", "swap"])
    s.add_argument("numbers", nargs="+", type=int)
    s.add_argument("--merge", choices=["next", "prev"], default="next",
                   help="delete: step K's changes happen with the next step (default) or the previous one")
    s.add_argument("--include-svg", action="store_true",
                   help="also renumber inside the slide's inlined SVGs and data-table-src tables")
    s.add_argument("--html-only", action="store_true",
                   help="renumber the deck HTML even though inlined files then fall out of step")
    s.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    args.started = time.monotonic()
    handlers = {"get": cmd_get, "put": cmd_put, "steps": cmd_steps}
    try:
        handler = handlers.get(args.command, cmd_browser)
        code = handler(args)
    except (DeckError, StepsError, OSError) as exc:
        print(f"ws {args.command}: error: {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:  # Playwright and friends: one line, no traceback
        name = type(exc).__name__
        if name in ("Error", "TimeoutError", "DeckNotReady", "RuntimeError", "ValueError"):
            print(f"ws {args.command}: error: {exc}", file=sys.stderr)
            code = 2
        else:
            raise
    sys.exit(code)


if __name__ == "__main__":
    main()

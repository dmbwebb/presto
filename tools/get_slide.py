#!/usr/bin/env python3
"""Print the HTML content of slides by main-deck slide number or HTML id.

Slide numbers match what the engine shows in the footer — appendix slides are
excluded from the count, matching webslides.js behaviour (wsMainNumber).

Usage:
    python3 tools/get_slide.py decks/my-deck/index.html 34
    python3 tools/get_slide.py decks/my-deck/index.html 2 3 4 5
    python3 tools/get_slide.py decks/my-deck/index.html appendix-pubbias
    python3 tools/get_slide.py decks/my-deck/index.html '#appendix-pubbias'
    python3 tools/get_slide.py decks/my-deck/index.html 34 --title-only
    python3 tools/get_slide.py decks/my-deck/index.html --list
"""

import argparse
import html as html_lib
import re
import sys
import urllib.parse
from pathlib import Path

from validate_links import deck_structure_issues


WEBSLIDES_ROOT = Path(__file__).resolve().parent.parent

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def format_listing_line(slide: dict) -> str:
    marker = f"{slide['main_number']:>3}" if not slide['is_appendix'] else "APP"
    slide_id = f"  #{slide['id']}" if slide['id'] else ""
    return f"{marker}  {slide['title']}{slide_id}"


def target_candidates(display_id: str) -> list[str]:
    """Return engine-style target candidates: decoded fragment first, then raw."""
    decoded = urllib.parse.unquote(display_id)
    return [decoded] if decoded == display_id else [decoded, display_id]


def resolve_deck_path(deck_arg: str) -> Path:
    """Resolve a deck argument that may be an index.html file or deck directory."""
    path = Path(deck_arg).expanduser()
    if path.is_dir():
        path = path / "index.html"
    if path.exists():
        return path

    candidate = WEBSLIDES_ROOT / deck_arg
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if candidate.exists():
        return candidate

    return path


def extract_deck_html(html: str) -> str:
    start, end = extract_deck_span(html)
    return html[start:end]


def extract_deck_span(html: str) -> tuple[int, int]:
    """Offsets of the .ws-deck container's inner HTML within ``html``."""
    structural_issues = [
        issue
        for issue in deck_structure_issues(html)
        if issue.startswith("MULTIPLE DECKS:") or issue.startswith("NESTED DECK:")
    ]
    if structural_issues:
        sys.exit(f"ERROR: {'; '.join(structural_issues)}")

    def in_html_comment(pos: int) -> bool:
        last_open = html.rfind('<!--', 0, pos)
        if last_open < 0:
            return False
        last_close = html.rfind('-->', 0, pos)
        return last_close < last_open

    ignored_ranges = raw_text_ranges(html) + template_ranges(html)
    match = None
    for candidate in iter_html_tags(html):
        if candidate["closing"] or in_html_comment(candidate["start"]) or in_ranges(candidate["start"], ignored_ranges):
            continue
        attrs = attrs_for(candidate["text"])
        if "ws-deck" in attrs.get("class", "").split():
            match = candidate
            break
    if not match:
        sys.exit("ERROR: No .ws-deck found in the HTML file.")

    depth = 1
    start = match["end"]
    deck_tag = match["tag"]
    for token in iter_html_tags(html, start):
        if in_html_comment(token["start"]) or in_ranges(token["start"], ignored_ranges):
            continue
        if token["tag"] != deck_tag:
            continue
        if token["closing"]:
            depth -= 1
            if depth == 0:
                return start, token["start"]
        elif not token["void"]:
            depth += 1

    sys.exit(f"ERROR: .ws-deck closing </{deck_tag}> not found.")


def tag_end(html: str, start: int) -> int:
    quote = None
    i = start
    while i < len(html):
        ch = html[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in {'"', "'"}:
            quote = ch
        elif ch == ">":
            return i + 1
        i += 1
    return -1


def iter_tags(html: str, tag_name: str, start: int = 0):
    pattern = re.compile(rf"</?\s*{re.escape(tag_name)}\b", re.IGNORECASE)
    pos = start
    while True:
        match = pattern.search(html, pos)
        if not match:
            return
        end = tag_end(html, match.start())
        if end < 0:
            return
        text = html[match.start():end]
        yield {
            "start": match.start(),
            "end": end,
            "text": text,
            "closing": text.lstrip().lower().startswith(f"</{tag_name.lower()}"),
        }
        pos = end


def iter_html_tags(html: str, start: int = 0):
    """Yield quote-aware HTML tags with normalized names."""
    pattern = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w:-]*)\b", re.IGNORECASE)
    pos = start
    while True:
        match = pattern.search(html, pos)
        if not match:
            return
        end = tag_end(html, match.start())
        if end < 0:
            return
        text = html[match.start():end]
        tag_name = match.group(2).lower()
        yield {
            "start": match.start(),
            "end": end,
            "text": text,
            "tag": tag_name,
            "closing": bool(match.group(1)),
            "void": tag_name in VOID_TAGS or text.rstrip().endswith("/>"),
        }
        pos = end


def raw_text_ranges(html: str) -> list[tuple[int, int]]:
    """Return script/style content ranges where markup-looking text is raw text."""
    ranges = []
    for tag_name in ("script", "style"):
        for opening in iter_tags(html, tag_name):
            if opening["closing"]:
                continue
            closing = next(
                (t for t in iter_tags(html, tag_name, opening["end"]) if t["closing"]),
                None,
            )
            if closing:
                ranges.append((opening["end"], closing["start"]))
    return ranges


def template_ranges(html: str) -> list[tuple[int, int]]:
    """Return whole-template ranges; template content is inert in the live DOM."""
    ranges = []
    for opening in iter_tags(html, "template"):
        if opening["closing"]:
            continue
        closing = next(
            (t for t in iter_tags(html, "template", opening["end"]) if t["closing"]),
            None,
        )
        if closing:
            ranges.append((opening["start"], closing["end"]))
    return ranges


def in_ranges(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(?:(['"])(.*?)\2|([^\s"'=<>`]+))""", re.DOTALL)


def attr_match_value(match) -> str:
    raw_value = match.group(3) if match.group(3) is not None else match.group(4)
    return html_lib.unescape(raw_value)


def attrs_for(opening_tag: str) -> dict[str, str]:
    return {m.group(1).lower(): attr_match_value(m) for m in ATTR_RE.finditer(opening_tag)}


def strip_html_tags(fragment: str) -> str:
    """Strip tags from an HTML fragment while respecting quoted > in attributes."""
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.DOTALL)
    pieces = []
    last = 0
    for tag in iter_html_tags(fragment):
        pieces.append(fragment[last:tag["start"]])
        last = tag["end"]
    pieces.append(fragment[last:])
    return "".join(pieces)


def extract_title(slide_html: str) -> str:
    """Extract first h1/h2 title text, respecting quoted > in attributes."""
    def in_html_comment(pos: int) -> bool:
        last_open = slide_html.rfind('<!--', 0, pos)
        if last_open < 0:
            return False
        last_close = slide_html.rfind('-->', 0, pos)
        return last_close < last_open

    ignored_ranges = raw_text_ranges(slide_html) + template_ranges(slide_html)
    for heading in iter_html_tags(slide_html):
        if (
            heading["closing"]
            or heading["tag"] not in {"h1", "h2"}
            or in_html_comment(heading["start"])
            or in_ranges(heading["start"], ignored_ranges)
        ):
            continue
        closing = next(
            (
                t for t in iter_tags(slide_html, heading["tag"], heading["end"])
                if t["closing"]
                and not in_html_comment(t["start"])
                and not in_ranges(t["start"], ignored_ranges)
            ),
            None,
        )
        if not closing:
            continue
        raw = slide_html[heading["end"]:closing["start"]]
        return html_lib.unescape(strip_html_tags(raw)).strip()
    return ""


def parse_slides(html: str) -> list[dict]:
    """
    Extract slides from .ws-deck in order, assigning main-slide numbers to
    non-appendix slides exactly as webslides.js does.
    """
    deck_start, deck_end = extract_deck_span(html)
    deck_html = html[deck_start:deck_end]
    slides = []
    main_number = 0

    def in_html_comment(pos: int) -> bool:
        last_open = deck_html.rfind('<!--', 0, pos)
        if last_open < 0:
            return False
        last_close = deck_html.rfind('-->', 0, pos)
        return last_close < last_open

    ignored_ranges = raw_text_ranges(deck_html) + template_ranges(deck_html)
    tokens = list(iter_html_tags(deck_html))
    depth = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if in_html_comment(token["start"]) or in_ranges(token["start"], ignored_ranges):
            continue

        if token["closing"]:
            depth = max(0, depth - 1)
            continue

        if token["tag"] == "section" and depth == 0:
            attrs = attrs_for(token["text"])
            classes = set(attrs.get('class', '').split())
            if 'slide' in classes:
                section_depth = 1
                close_end = None
                j = i
                while j < len(tokens):
                    nested = tokens[j]
                    j += 1
                    if in_html_comment(nested["start"]) or in_ranges(nested["start"], ignored_ranges) or nested["tag"] != "section":
                        continue
                    if nested["closing"]:
                        section_depth -= 1
                        if section_depth == 0:
                            close_end = nested["end"]
                            break
                    elif not nested["void"]:
                        section_depth += 1
                if close_end is None:
                    slide_id = attrs.get('id')
                    label = f' id="{slide_id}"' if slide_id else ""
                    sys.exit(f"ERROR: slide{label} missing closing </section>.")

                full = deck_html[token["start"]:close_end]
                is_appendix = 'appendix' in classes
                slide_id = attrs.get('id')
                title = extract_title(full)

                if not is_appendix:
                    main_number += 1

                slides.append({
                    'main_number': main_number if not is_appendix else None,
                    'is_appendix': is_appendix,
                    'id': slide_id,
                    'title': title,
                    'html': full,
                    # Offsets of the <section>...</section> text in ``html``.
                    'start': deck_start + token["start"],
                    'end': deck_start + close_end,
                    'attrs': attrs,
                })

                while i < len(tokens) and tokens[i]["start"] < close_end:
                    i += 1
                continue

        if not token["void"]:
            depth += 1

    if not slides:
        sys.exit("ERROR: No .ws-deck slides found in the HTML file.")

    return slides


def main():
    parser = argparse.ArgumentParser(
        description='Get slide content by main-deck slide number or HTML id.')
    parser.add_argument('deck', help='Path to deck index.html')
    parser.add_argument('slides', nargs='*',
                        help='Slide number(s) (1-indexed, main slides only) or HTML id(s)')
    parser.add_argument('--list', action='store_true',
                        help='List all main slides with titles')
    parser.add_argument('--title-only', action='store_true',
                        help='Print only the slide title, not the full HTML')
    args = parser.parse_args()

    deck_path = resolve_deck_path(args.deck)
    if not deck_path.exists():
        sys.exit(f"ERROR: {deck_path} not found.")
    if deck_path.is_dir():
        sys.exit(f"ERROR: {deck_path} is a directory, not an HTML file.")
    if not deck_path.is_file():
        sys.exit(f"ERROR: {deck_path} is not a file.")
    if deck_path.suffix.lower() != '.html':
        sys.exit(f"ERROR: {deck_path} is not an HTML file.")
    try:
        html = deck_path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        sys.exit(f"ERROR: could not read deck {deck_path}: {exc}")
    slides = parse_slides(html)
    main_slides = [s for s in slides if not s['is_appendix']]

    if args.list:
        for slide in slides:
            print(format_listing_line(slide))
        return

    if not args.slides:
        parser.error('Provide one or more slide numbers/HTML ids, or use --list.')

    selected = []
    for selector in args.slides:
        if selector.isdigit():
            num = int(selector)
            if num < 1 or num > len(main_slides):
                sys.exit(f"ERROR: Slide {num} out of range (1–{len(main_slides)}).")
            selected.append(main_slides[num - 1])
            continue

        slide_id = selector.removeprefix('#')
        match = None
        duplicate_count = 0
        for candidate in target_candidates(slide_id):
            matches = [slide for slide in slides if slide['id'] == candidate]
            if len(matches) > 1:
                duplicate_count = len(matches)
                break
            if len(matches) == 1:
                match = matches[0]
                break
        if duplicate_count:
            sys.exit(f"ERROR: Multiple slides with id #{slide_id} ({duplicate_count} matches).")
        if match is None:
            sys.exit(f"ERROR: No slide with id #{slide_id}.")
        selected.append(match)

    for i, slide in enumerate(selected):
        if args.title_only:
            print(slide['title'])
        else:
            if i > 0:
                print('\n')
            marker = f"Slide {slide['main_number']}" if not slide['is_appendix'] else "Appendix slide"
            slide_id = f" #{slide['id']}" if slide['id'] else ""
            print(f"<!-- {marker}{slide_id}: {slide['title']} -->\n")
            print(slide['html'])


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
from __future__ import annotations

"""Validate appendix links and back buttons in a WebSlides deck.

Checks:
  1. Every ws-appendix-link href="#appendix-*" points to an existing id
  2. Every appendix slide (non-section-header) has a ws-back-link
  3. No ws-appendix-link used as a back button (href="#" with back text)
  4. No orphan appendix slides (appendix slides with no link pointing to them)
  5. Local CSS/JS/image/media/table references exist on disk

Usage:
    python3 tools/validate_links.py decks/my-deck/index.html
"""

import argparse
import posixpath
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

WEBSLIDES_ROOT = Path(__file__).resolve().parent.parent

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

CSS_URL_RE = re.compile(
    r"url\(\s*(?:'([^']*)'|\"([^\"]*)\"|([^)]*?))\s*\)",
    re.IGNORECASE,
)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:'([^']*)'|\"([^\"]*)\")",
    re.IGNORECASE,
)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


class DeckLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.all_ids = set()
        self.deck_id_counts = {}
        self.outside_deck_id_counts = {}
        self.appendix_links = {}
        self.hash_links = []
        self.wrong_back_count = 0
        self.img_srcs = set()
        self.local_resource_srcs = set()
        self.document_resource_srcs = set()
        self.stylesheet_srcs = set()
        self.css_resource_srcs = set()
        self.invalid_table_srcs = []
        self.slides = []
        self.appendix_slides = []
        self.appendix_target_slide_ids = {}
        self._section_stack = []
        self._open_section_slide_ids = []
        self._deck_stack = []
        self._deck_child_depth = 0
        self._deck_depth_pushes = []
        self._style_depth = 0
        self._template_depth = 0

    @staticmethod
    def class_tokens(attrs):
        class_value = attrs.get("class", "")
        return set(class_value.split())

    @staticmethod
    def srcset_urls(srcset):
        urls = []
        i = 0
        length = len(srcset)
        while i < length:
            while i < length and (srcset[i].isspace() or srcset[i] == ","):
                i += 1
            if i >= length:
                break

            start = i
            if srcset.startswith("data:", i):
                while i < length and not srcset[i].isspace():
                    i += 1
            else:
                while i < length and not srcset[i].isspace() and srcset[i] != ",":
                    i += 1

            url = srcset[start:i]
            if url:
                urls.append(url)

            while i < length and srcset[i] != ",":
                i += 1
        return urls

    @staticmethod
    def css_urls(css_text):
        urls = []
        css_text = CSS_COMMENT_RE.sub("", css_text or "")
        for match in CSS_URL_RE.finditer(css_text):
            url = next((group for group in match.groups() if group is not None), "")
            url = url.strip()
            if url:
                urls.append(url)
        for match in CSS_IMPORT_RE.finditer(css_text):
            url = next((group for group in match.groups() if group is not None), "")
            url = url.strip()
            if url:
                urls.append(url)
        return urls

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._template_depth:
            if tag == "template":
                self._template_depth += 1
            return

        elem_id = attrs.get("id")
        classes = self.class_tokens(attrs)
        href = attrs.get("href", "")

        is_deck = "ws-deck" in classes
        in_deck = is_deck or (self._deck_stack[-1] if self._deck_stack else False)
        is_direct_deck_child = in_deck and not is_deck and self._deck_child_depth == 0
        if elem_id:
            self.all_ids.add(elem_id)
            if in_deck:
                self.deck_id_counts[elem_id] = self.deck_id_counts.get(elem_id, 0) + 1
            else:
                self.outside_deck_id_counts[elem_id] = self.outside_deck_id_counts.get(elem_id, 0) + 1
        if tag not in VOID_TAGS:
            self._deck_stack.append(in_deck)
            deck_depth_push = 1 if in_deck and not is_deck else 0
            self._deck_depth_pushes.append(deck_depth_push)
            self._deck_child_depth += deck_depth_push

        if tag == "script":
            src = attrs.get("src", "")
            if src:
                self.document_resource_srcs.add(src)

        if tag == "link":
            href = attrs.get("href", "")
            if href:
                self.document_resource_srcs.add(href)
                if "stylesheet" in attrs.get("rel", "").lower().split():
                    self.stylesheet_srcs.add(href)

        if tag == "style":
            self._style_depth += 1

        if tag == "template":
            self._template_depth += 1

        if tag == "img" and in_deck:
            src = attrs.get("src", "")
            if src:
                self.img_srcs.add(src)

        if tag in {"audio", "video", "source"} and in_deck:
            src = attrs.get("src", "")
            if src:
                self.local_resource_srcs.add(src)

        if tag == "video" and in_deck:
            poster = attrs.get("poster", "")
            if poster:
                self.local_resource_srcs.add(poster)

        if tag in {"track", "embed", "iframe"} and in_deck:
            src = attrs.get("src", "")
            if src:
                self.local_resource_srcs.add(src)

        if tag == "object" and in_deck:
            data = attrs.get("data", "")
            if data:
                self.local_resource_srcs.add(data)

        if tag in {"use", "image"} and in_deck:
            href = attrs.get("href") or attrs.get("xlink:href") or ""
            if href:
                self.local_resource_srcs.add(href)

        if tag in {"img", "source"} and in_deck:
            for src in self.srcset_urls(attrs.get("srcset", "")):
                self.img_srcs.add(src)

        if in_deck:
            for src in self.css_urls(attrs.get("style", "")):
                self.css_resource_srcs.add(src)

            table_src = attrs.get("data-table-src")
            if table_src is not None and not table_src.strip():
                self.invalid_table_srcs.append("INVALID TABLE SRC: data-table-src is empty")
            elif table_src:
                self.local_resource_srcs.add(table_src)

        if in_deck and "ws-appendix-link" in classes:
            if href.startswith("#") and href != "#":
                self.appendix_links[unquote(href[1:])] = href[1:]
            elif href == "#":
                self.wrong_back_count += 1
        elif in_deck and tag == "a" and href.startswith("#") and href != "#":
            self.hash_links.append((unquote(href[1:]), href[1:]))
        elif in_deck and tag == "a" and href and not href.startswith("#"):
            self.local_resource_srcs.add(href)

        is_direct_section = tag == "section" and is_direct_deck_child and not self._section_stack
        is_appendix_section = (
            is_direct_section
            and "slide" in classes
            and "appendix" in classes
            and "section-header" not in classes
        )
        is_slide_section = is_direct_section and "slide" in classes
        current_appendix_slide = self._section_stack[-1] if self._section_stack else None
        if elem_id and current_appendix_slide is not None and current_appendix_slide.get("id"):
            self.appendix_target_slide_ids[elem_id] = current_appendix_slide["id"]
        if is_slide_section and not self._section_stack:
            self.slides.append({
                "id": elem_id or "",
                "is_appendix": "appendix" in classes,
            })
        if is_appendix_section:
            slide = {"id": elem_id or "", "has_back": "ws-back-link" in classes}
            self.appendix_slides.append(slide)
            if elem_id:
                self.appendix_target_slide_ids[elem_id] = elem_id
            self._section_stack.append(slide)
            self._open_section_slide_ids.append(elem_id or "")
        elif tag == "section":
            current_slide = self._section_stack[-1] if self._section_stack else None
            self._section_stack.append(current_slide)
            self._open_section_slide_ids.append((elem_id or "") if is_slide_section else None)
        elif self._section_stack and self._section_stack[-1] is not None:
            if "ws-back-link" in classes:
                self._section_stack[-1]["has_back"] = True

    def handle_endtag(self, tag):
        if self._template_depth:
            if tag == "template":
                self._template_depth -= 1
                if self._template_depth > 0:
                    return
            else:
                return
        if tag == "section" and self._section_stack:
            self._section_stack.pop()
            if self._open_section_slide_ids:
                self._open_section_slide_ids.pop()
        if tag == "style" and self._style_depth:
            self._style_depth -= 1
        if tag not in VOID_TAGS and self._deck_stack:
            self._deck_stack.pop()
            if self._deck_depth_pushes:
                self._deck_child_depth = max(0, self._deck_child_depth - self._deck_depth_pushes.pop())

    def handle_data(self, data):
        if self._template_depth:
            return
        if self._style_depth:
            for src in self.css_urls(data):
                self.css_resource_srcs.add(src)

    def unclosed_slide_ids(self):
        return [slide_id for slide_id in self._open_section_slide_ids if slide_id is not None]


class DeckStructureParser(HTMLParser):
    """Track structural deck container closure without browser-style recovery."""

    def __init__(self):
        super().__init__()
        self._template_depth = 0
        self._element_stack = []
        self.deck_count = 0
        self.nested_deck_count = 0

    def handle_starttag(self, tag, attrs):
        if self._template_depth:
            if tag == "template":
                self._template_depth += 1
            return
        if tag == "template":
            self._template_depth += 1
            return
        classes = set(dict(attrs).get("class", "").split())
        is_deck = "ws-deck" in classes
        if is_deck:
            self.deck_count += 1
            if any(stack_is_deck for _, stack_is_deck in self._element_stack):
                self.nested_deck_count += 1
        if tag not in VOID_TAGS:
            self._element_stack.append((tag, is_deck))

    def handle_endtag(self, tag):
        if self._template_depth:
            if tag == "template":
                self._template_depth -= 1
            return
        if tag in VOID_TAGS or not self._element_stack:
            return
        matching_index = None
        for i in range(len(self._element_stack) - 1, -1, -1):
            if self._element_stack[i][0] == tag:
                matching_index = i
                break
        if matching_index is None:
            return
        if any(is_deck for _, is_deck in self._element_stack[matching_index + 1:]):
            return
        del self._element_stack[matching_index:]

    def unclosed_deck_tags(self):
        return [tag for tag, is_deck in self._element_stack if is_deck]


def deck_structure_issues(html: str) -> list[str]:
    parser = DeckStructureParser()
    parser.feed(html)
    issues = []
    if parser.deck_count == 0:
        issues.append("MISSING DECK: no .ws-deck container found")
    if parser.deck_count > 1:
        issues.append(f"MULTIPLE DECKS: found {parser.deck_count} .ws-deck containers; expected exactly 1")
    if parser.nested_deck_count:
        issues.append("NESTED DECK: .ws-deck containers must not be nested")
    unclosed_tags = parser.unclosed_deck_tags()
    count = len(unclosed_tags)
    if count <= 0:
        return issues
    if count == 1:
        issues.append(f"UNCLOSED DECK: .ws-deck is missing closing </{unclosed_tags[0]}>")
    else:
        tag_list = ", ".join(f"</{tag}>" for tag in unclosed_tags)
        issues.append(f"UNCLOSED DECK: {count} .ws-deck containers are missing closing tags: {tag_list}")
    return issues


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


def find_webslides_root(deck_path: Path) -> Path:
    """Find the server root used for root-relative local resource references."""
    d = deck_path.parent
    while True:
        if (d / "engine" / "webslides.js").exists():
            return d
        parent = d.parent
        if parent == d:
            return deck_path.parent
        d = parent


def local_path_for(base_dir: Path, src: str, root_dir: Path | None = None) -> Path | None:
    """Resolve a browser-relative local resource path, ignoring external URLs."""
    parsed = urlparse(src)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    decoded_path = unquote(parsed.path)
    if parsed.path.startswith("/"):
        if root_dir is None:
            return None
        virtual_path = posixpath.normpath(posixpath.join("/", decoded_path.lstrip("/")))
        return root_dir / virtual_path.lstrip("/")
    if root_dir is not None:
        try:
            base_rel = base_dir.resolve().relative_to(root_dir.resolve()).as_posix()
        except ValueError:
            return base_dir / decoded_path
        virtual_path = posixpath.normpath(posixpath.join("/", base_rel, decoded_path))
        return root_dir / virtual_path.lstrip("/")
    return base_dir / decoded_path


def check_local_resource(
    base_dir: Path,
    src: str,
    label: str | None = None,
    root_dir: Path | None = None,
) -> str | None:
    """Return a missing-file issue for a local resource reference, if missing."""
    parsed = urlparse(src)
    if parsed.scheme.lower() == "file":
        if label:
            return f"INVALID FILE URL: {label} -> {src}"
        return f"INVALID FILE URL: {src}"

    filepath = local_path_for(base_dir, src, root_dir=root_dir)
    if filepath is None:
        return None
    if filepath.exists():
        if filepath.is_dir():
            if label:
                return f"INVALID FILE: {label} -> {src} is a directory"
            return f"INVALID FILE: {src} is a directory"
        return None
    if label:
        return f"MISSING FILE: {label} -> {src}"
    return f"MISSING FILE: {src}"


def target_candidates(display_id: str) -> list[str]:
    """Return engine-style target candidates: decoded fragment first, then raw."""
    decoded = unquote(display_id)
    return [decoded] if decoded == display_id else [decoded, display_id]


def first_matching_target(display_id: str, ids: set[str] | dict[str, object]) -> str | None:
    for candidate in target_candidates(display_id):
        if candidate in ids:
            return candidate
    return None


def stylesheet_label(deck_dir: Path, stylesheet_path: Path) -> str:
    deck_dir = deck_dir.resolve()
    stylesheet_path = stylesheet_path.resolve()
    try:
        return stylesheet_path.relative_to(deck_dir).as_posix()
    except ValueError:
        return stylesheet_path.as_posix()


def stylesheet_css_issues_for_path(
    deck_dir: Path,
    stylesheet_path: Path,
    visited: set[Path],
    root_dir: Path | None = None,
) -> list[str]:
    """Validate url(...) resources inside a concrete local stylesheet path."""
    if not stylesheet_path.exists():
        return []
    if stylesheet_path.is_dir():
        return []
    stylesheet_path = stylesheet_path.resolve()
    if stylesheet_path in visited:
        return []
    visited.add(stylesheet_path)

    issues = []
    label = stylesheet_label(deck_dir, stylesheet_path)
    try:
        css_text = stylesheet_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"ERROR: could not read stylesheet {label}: {exc}"]

    for src in DeckLinkParser.css_urls(css_text):
        issue = check_local_resource(stylesheet_path.parent, src, label=label, root_dir=root_dir)
        if issue:
            issues.append(issue)
            continue

        nested_path = local_path_for(stylesheet_path.parent, src, root_dir=root_dir)
        if nested_path is not None and nested_path.suffix.lower() == ".css":
            issues.extend(stylesheet_css_issues_for_path(deck_dir, nested_path, visited=visited, root_dir=root_dir))
    return issues


def stylesheet_css_issues(
    deck_dir: Path,
    stylesheet_src: str,
    visited: set[Path] | None = None,
    root_dir: Path | None = None,
) -> list[str]:
    """Validate url(...) resources inside a local stylesheet reference."""
    if visited is None:
        visited = set()
    stylesheet_path = local_path_for(deck_dir, stylesheet_src, root_dir=root_dir)
    if stylesheet_path is None:
        return []
    return stylesheet_css_issues_for_path(deck_dir, stylesheet_path, visited=visited, root_dir=root_dir)


def validate(html_path: str) -> list[str]:
    html_path = resolve_deck_path(html_path)
    if not html_path.exists():
        return [f"ERROR: {html_path} not found"]
    if html_path.is_dir():
        return [f"ERROR: {html_path} is a directory, not an HTML file"]
    if not html_path.is_file():
        return [f"ERROR: {html_path} is not a file"]
    if html_path.suffix.lower() != ".html":
        return [f"ERROR: {html_path} is not an HTML file"]

    try:
        html = html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"ERROR: could not read deck {html_path}: {exc}"]

    deck_dir = html_path.parent
    root_dir = find_webslides_root(html_path)
    issues = []
    issues.extend(deck_structure_issues(html))

    parser = DeckLinkParser()
    parser.feed(html)
    all_ids = parser.all_ids
    appendix_ids = {
        slide["id"]
        for slide in parser.appendix_slides
        if slide["id"]
    }

    linked_ids = set()
    if not parser.slides and not any(issue.startswith("MISSING DECK:") for issue in issues):
        issues.append("NO SLIDES: .ws-deck contains no direct .slide sections")

    for elem_id, count in sorted(parser.deck_id_counts.items()):
        if count > 1:
            issues.append(f'DUPLICATE ID: id="{elem_id}" appears {count} times inside .ws-deck')

    for slide_id in parser.unclosed_slide_ids():
        label = f' id="{slide_id}"' if slide_id else ""
        issues.append(f"UNCLOSED SECTION: slide{label} is missing closing </section>")

    targeted_ids = set()
    for display_id in parser.appendix_links.values():
        target_id = first_matching_target(display_id, parser.deck_id_counts) or unquote(display_id)
        targeted_ids.add(target_id)
    for _target_id, display_id in parser.hash_links:
        target_id = first_matching_target(display_id, parser.deck_id_counts) or unquote(display_id)
        targeted_ids.add(target_id)
    for elem_id in sorted(targeted_ids):
        if parser.deck_id_counts.get(elem_id, 0) and parser.outside_deck_id_counts.get(elem_id, 0):
            issues.append(
                f'DUPLICATE TARGET ID: id="{elem_id}" appears outside .ws-deck and can intercept navigation'
            )

    for slide in parser.slides:
        if slide["is_appendix"] and not slide["id"]:
            issues.append("MISSING ID: appendix slide has no id")

    for _target_id, display_id in parser.appendix_links.items():
        target_id = first_matching_target(display_id, all_ids)
        if target_id is not None:
            appendix_slide_id = parser.appendix_target_slide_ids.get(target_id)
            if appendix_slide_id:
                linked_ids.add(appendix_slide_id)
            else:
                linked_ids.add(target_id)
        if target_id is None:
            issues.append(f"BROKEN LINK: href=\"#{display_id}\" — no element with this id")
        elif target_id not in parser.appendix_target_slide_ids:
            issues.append(f"BROKEN LINK: href=\"#{display_id}\" — no appendix slide with this id")

    for _target_id, display_id in parser.hash_links:
        target_id = first_matching_target(display_id, all_ids)
        if target_id is None:
            issues.append(f"BROKEN HASH LINK: href=\"#{display_id}\" — no element with this id")
        elif first_matching_target(display_id, parser.deck_id_counts) is None:
            issues.append(f"BROKEN HASH LINK: href=\"#{display_id}\" — no element inside .ws-deck with this id")

    # --- 2. Check appendix slides have back links ---
    for slide in parser.appendix_slides:
        slide_id = slide["id"]
        if not slide["has_back"]:
            issues.append(f"MISSING BACK BUTTON: appendix slide id=\"{slide_id}\"")

    # --- 3. Check for wrong-class back buttons ---
    if parser.wrong_back_count:
        issues.append(
            f"WRONG CLASS: {parser.wrong_back_count} back button(s) use ws-appendix-link instead of ws-back-link"
        )

    # --- 4. Orphan appendix slides (no link points to them) ---
    for aid in appendix_ids:
        if aid == "appendix-start":
            continue  # section header, doesn't need a link
        if aid not in linked_ids:
            issues.append(f"ORPHAN: appendix slide id=\"{aid}\" — no link points here")

    # --- 5. Check figure/image/table references exist ---
    resource_srcs = set(parser.img_srcs)
    resource_srcs.update(parser.local_resource_srcs)
    resource_srcs.update(parser.document_resource_srcs)
    resource_srcs.update(parser.css_resource_srcs)
    for src in resource_srcs:
        issue = check_local_resource(deck_dir, src, root_dir=root_dir)
        if issue:
            issues.append(issue)

    for src in parser.stylesheet_srcs:
        issues.extend(stylesheet_css_issues(deck_dir, src, root_dir=root_dir))

    issues.extend(parser.invalid_table_srcs)

    return issues


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate appendix links, back buttons, IDs, and local resource files in a WebSlides deck.",
    )
    parser.add_argument("deck", help="Path to a deck index.html or deck directory.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    issues = validate(args.deck)

    if not issues:
        print("✓ All links, back buttons, and file references are valid.")
        sys.exit(0)

    print(f"Found {len(issues)} issue(s):\n")
    for issue in issues:
        print(f"  • {issue}")
    sys.exit(1)


if __name__ == "__main__":
    main()

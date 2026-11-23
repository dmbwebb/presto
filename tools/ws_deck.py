"""Source-level deck model for ``ws``: deck lookup, slide addressing, get/put.

Everything here works on the deck file's text, without a browser, so it stays
fast and runs under the system Python 3.9.

Slides are addressed (see ``select_slides``) by

- 1-based deck position (the ``?slide=N`` number; appendix slides count),
- a range ``A-B``,
- a stable id: the section's ``id``, else its ``data-id``, else a slug of its
  title (``slide_key``); ``#`` is optional,
- the id of any element inside the slide,
- a case-insensitive substring of the title, which must match one slide,
- ``all``.

Offsets are character offsets into the file decoded from its exact bytes
(no newline translation), so replacing a section and re-encoding keeps every
other byte of the file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import get_slide  # noqa: E402

LOCATE_DECK = Path.home() / ".claude" / "skills" / "webslides" / "scripts" / "locate_deck.py"
HASH_LEN = 12
SLUG_MAX = 32


class DeckError(ValueError):
    """A user-facing problem with the deck or the arguments."""


# ---------------------------------------------------------------- deck lookup
def resolve_deck(arg: str) -> Path:
    """Return the deck's index.html for a path, a deck directory, or a deck alias."""
    path = Path(arg).expanduser()
    if path.is_dir():
        path = path / "index.html"
    if path.is_file():
        return path.resolve()
    if not any(sep in arg for sep in ("/", os.sep)) and LOCATE_DECK.exists():
        try:
            proc = subprocess.run(
                [sys.executable, str(LOCATE_DECK), arg, "--json"],
                capture_output=True, text=True, timeout=20,
            )
            data = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else {}
        except (OSError, ValueError, subprocess.SubprocessError):
            data = {}
        index = Path(data.get("index") or "")
        if data.get("id") and index.is_file():
            return index.resolve()
    raise DeckError(f"no deck at {arg!r} (give an index.html, a deck directory, or a deck alias "
                    f"known to {LOCATE_DECK.name})")


def deck_key(index: Path) -> str:
    """Short, filesystem-safe key per deck file (directory name plus a path hash)."""
    index = Path(index).resolve()
    digest = hashlib.sha1(str(index).encode("utf-8")).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", index.parent.name).strip("-") or "deck"
    return f"{name}-{digest}"


def read_deck(index: Path) -> str:
    data = Path(index).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeckError(f"{index} is not UTF-8: {exc}") from exc


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LEN]


# ---------------------------------------------------------------- slides
def slugify(text: str, limit: int = SLUG_MAX) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"['’]", "", text)
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = ""
    for word in words:
        candidate = f"{out}-{word}" if out else word
        if len(candidate) > limit and out:
            break
        out = candidate[:limit]
    return out


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def parse_deck(text: str) -> list[dict]:
    """Slides in deck order with offsets, line range, stable key and hash."""
    try:
        slides = get_slide.parse_slides(text)
    except SystemExit as exc:  # get_slide reports problems with sys.exit
        message = str(exc.code) if exc.code is not None else "could not parse deck"
        raise DeckError(message.removeprefix("ERROR: ")) from None
    used = {s["attrs"][attr] for s in slides for attr in ("id", "data-id") if s["attrs"].get(attr)}
    for n, s in enumerate(slides):
        if re.search(r"<br\b", s["html"], re.IGNORECASE):
            s["title"] = re.sub(r"\s+", " ", get_slide.extract_title(
                re.sub(r"<br\b[^>]*>", " ", s["html"], flags=re.IGNORECASE))).strip()
        s["index"] = n
        s["number"] = n + 1
        s["data_id"] = s["attrs"].get("data-id") or ""
        s["id"] = s["attrs"].get("id") or ""
        if s["id"]:
            key, source = s["id"], "id"
        elif s["data_id"]:
            key, source = s["data_id"], "data-id"
        else:
            key, source = slugify(s["title"]) or f"slide-{n + 1}", "title"
            base, k = key, 2
            while key in used:
                key = f"{base}-{k}"
                k += 1
            used.add(key)
        s["key"] = key
        s["key_source"] = source
        s["line_start"] = line_of(text, s["start"])
        s["line_end"] = line_of(text, s["end"])
        s["hash"] = text_hash(s["html"])
        s["inner_ids"] = re.findall(r"""\sid\s*=\s*["']([^"']+)["']""", s["html"][1:])
    return slides


def _split_specs(specs) -> list[str]:
    out = []
    for raw in specs if isinstance(specs, (list, tuple)) else [specs]:
        for part in str(raw).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def select_slides(specs, slides: list[dict]) -> list[int]:
    """Map slide specs (see module docstring) to slide indexes, de-duplicated in order."""
    total = len(slides)
    by_key = {}
    for s in slides:
        for k in (s["key"], s["id"], s["data_id"]):
            if k:
                by_key.setdefault(k, s["index"])
    by_inner = {}
    for s in slides:
        for inner in s["inner_ids"]:
            by_inner.setdefault(inner, s["index"])
    chosen = []
    for spec in _split_specs(specs):
        if spec.lower() == "all":
            chosen.extend(range(total))
            continue
        if spec.isdigit():
            n = int(spec)
            if not 1 <= n <= total:
                raise DeckError(f"slide {n} out of range (deck has {total} slides)")
            chosen.append(n - 1)
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", spec)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b or a < 1 or b > total:
                raise DeckError(f"slide range {spec} out of range (deck has {total} slides)")
            chosen.extend(range(a - 1, b))
            continue
        key = spec[1:] if spec.startswith("#") else spec
        if key in by_key:
            chosen.append(by_key[key])
            continue
        if key in by_inner:
            chosen.append(by_inner[key])
            continue
        needle = key.lower()
        hits = [s for s in slides if needle in s["title"].lower()]
        if len(hits) == 1:
            chosen.append(hits[0]["index"])
            continue
        if len(hits) > 1:
            listed = "; ".join(f"{s['number']} {s['title'][:40]!r}" for s in hits[:6])
            raise DeckError(f"{spec!r} matches {len(hits)} slide titles ({listed}); use a number or id")
        raise DeckError(f"unknown slide {spec!r} (use a number, a range like 3-7, a slide id, "
                        "or part of a title; `ws outline DECK` lists them)")
    return list(dict.fromkeys(chosen))


def load_deck(arg: str) -> tuple[Path, str, list[dict]]:
    """Resolve a deck argument and parse it: (index.html path, file text, slides)."""
    index = resolve_deck(arg)
    text = read_deck(index)
    return index, text, parse_deck(text)


def select_one(spec, slides: list[dict]) -> dict:
    picked = select_slides(spec, slides)
    if len(picked) != 1:
        raise DeckError(f"{spec!r} selects {len(picked)} slides; give exactly one")
    return slides[picked[0]]


# ---------------------------------------------------------------- put
def atomic_write(path: Path, data: bytes) -> None:
    """Write through a sibling temp file and rename it over ``path`` (keeps the file mode)."""
    path = Path(path)
    mode = None
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".ws-tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def ensure_unchanged(index: Path, text: str) -> None:
    """Refuse to write when the deck file no longer holds ``text`` (another editor saved it)."""
    if read_deck(index) != text:
        raise DeckError(f"{index} changed while ws was editing it; nothing written, try again")


def check_replacement(new_html: str) -> str:
    """Validate that ``new_html`` is exactly one slide section; return it stripped."""
    stripped = new_html.strip()
    if not re.match(r"<section\b", stripped, re.IGNORECASE):
        raise DeckError("replacement must start with <section class=\"slide ...\">")
    probe = f'<div class="ws-deck">\n{stripped}\n</div>'
    try:
        parsed = get_slide.parse_slides(probe)
    except SystemExit as exc:
        raise DeckError(f"replacement is not one complete slide: {exc.code}") from None
    if len(parsed) != 1:
        raise DeckError(f"replacement holds {len(parsed)} slide sections; give exactly one")
    one = parsed[0]
    rest = probe[:one["start"]] + probe[one["end"]:]
    if re.sub(r"\s+", "", rest) != '<divclass="ws-deck"></div>':
        raise DeckError("replacement has content outside its <section>...</section>")
    return stripped


def put_slide(index: Path, spec, new_html: str, expect_hash: str | None = None) -> tuple[dict, dict]:
    """Replace one slide's section text; return (old slide, new slide)."""
    text = read_deck(index)
    slides = parse_deck(text)
    old = select_one(spec, slides)
    if expect_hash is not None and expect_hash != old["hash"]:
        raise DeckError(
            f"slide {old['number']} changed since `ws get` (hash {old['hash']}, expected {expect_hash}); "
            "run `ws get` again and redo the edit"
        )
    replacement = check_replacement(new_html)
    new_text = text[:old["start"]] + replacement + text[old["end"]:]
    new_slides = parse_deck(new_text)
    if len(new_slides) != len(slides):
        raise DeckError("the replacement would change the number of slides; refusing")
    if new_text == text:
        return old, new_slides[old["index"]]
    ensure_unchanged(index, text)
    atomic_write(index, new_text.encode("utf-8"))
    return old, new_slides[old["index"]]

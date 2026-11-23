#!/usr/bin/env python3
"""Resolve a WebSlides deck name or alias to its project-owned path.

Aliases come from the per-user registry references/deck_locations.yaml (format:
references/deck_locations.example.yaml). Without a registry, pass the deck's
folder or index.html instead of an alias.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
MANIFEST = SKILL_DIR / "references" / "deck_locations.yaml"
EXAMPLE_MANIFEST = SKILL_DIR / "references" / "deck_locations.example.yaml"
DeckManifest = dict[str, object]


def parse_manifest(path: Path) -> DeckManifest:
    """Parse the small deck manifest without requiring PyYAML (empty if the file is absent)."""
    if not path.is_file():
        return {"decks": {}}
    lines = path.read_text(encoding="utf-8").splitlines()
    data: DeckManifest = {"decks": {}}
    current_deck: str | None = None
    current_key: str | None = None

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()

        if indent == 0 and line.endswith(":"):
            current_key = line[:-1]
            continue
        if indent == 0 and ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
            current_key = key.strip()
            continue
        if current_key == "decks" and indent == 2 and line.endswith(":"):
            current_deck = line[:-1]
            data["decks"][current_deck] = {"aliases": []}
            continue
        if current_key == "decks" and current_deck and indent == 4 and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value:
                data["decks"][current_deck][key] = value
            else:
                data["decks"][current_deck][key] = []
        elif current_key == "decks" and current_deck and indent == 6 and line.startswith("- "):
            data["decks"][current_deck].setdefault("aliases", []).append(line[2:].strip())

    return data


def normalize(value: str) -> str:
    return " ".join(value.lower().replace("_", "-").split())


def iter_candidates(deck_id: str, info: dict) -> list[str]:
    return [deck_id, deck_id.replace("-", " ")] + info.get("aliases", [])


def manifest_decks(manifest: DeckManifest) -> dict[str, dict]:
    decks = manifest.get("decks", {})
    return decks if isinstance(decks, dict) else {}


def resolve(query: str, manifest: DeckManifest) -> tuple[str, dict] | None:
    q = normalize(query)
    for deck_id, info in manifest_decks(manifest).items():
        candidates = iter_candidates(deck_id, info)
        if q in {normalize(c) for c in candidates}:
            return deck_id, info

    matches = []
    for deck_id, info in manifest_decks(manifest).items():
        candidates = iter_candidates(deck_id, info)
        if any(q in normalize(c) or normalize(c) in q for c in candidates):
            matches.append((deck_id, info))
    if len(matches) == 1:
        return matches[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deck", nargs="?", help="Deck id, alias, or path")
    parser.add_argument("--json", action="store_true", help="Emit JSON metadata")
    parser.add_argument("--list", action="store_true", help="List known decks")
    args = parser.parse_args()

    manifest = parse_manifest(MANIFEST)

    if args.list:
        if not MANIFEST.is_file():
            print(f"No deck registry at {MANIFEST}. To use aliases, copy {EXAMPLE_MANIFEST.name} "
                  "to deck_locations.yaml and list your decks.", file=sys.stderr)
        for deck_id, info in manifest_decks(manifest).items():
            print(f"{deck_id}\t{info.get('root', '')}")
        return 0

    if not args.deck:
        parser.error("deck is required unless --list is used")

    candidate = Path(args.deck).expanduser()
    if candidate.exists():
        root = candidate if candidate.is_dir() else candidate.parent
        info = {"root": str(root), "index": str(root / "index.html")}
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print(info["root"])
        return 0

    resolved = resolve(args.deck, manifest)
    if not resolved:
        if MANIFEST.is_file():
            print(f"Deck not found in {MANIFEST}: {args.deck}", file=sys.stderr)
        else:
            print(f"{args.deck!r} is not an existing path, and there is no deck registry at "
                  f"{MANIFEST} to look it up as an alias.\n"
                  "Pass the deck's folder or index.html, or copy "
                  f"{EXAMPLE_MANIFEST} to deck_locations.yaml and add the deck.",
                  file=sys.stderr)
        return 1

    deck_id, info = resolved
    root = Path(info["root"]).expanduser()
    payload = {
        "id": deck_id,
        "root": str(root),
        "index": str(root / "index.html"),
        "project_id": info.get("project_id"),
        "project_root": info.get("project_root"),
        "engine_root": manifest.get("engine_root"),
    }
    if info.get("tex_dir"):
        payload["tex_dir"] = info["tex_dir"]
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload["root"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

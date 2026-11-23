#!/usr/bin/env python3
"""List every stat a WebSlides deck binds to a file, with its value; flag missing ones.

A deck declares its stats source on the deck root (paths relative to the deck):

    <div class="ws-deck" data-stats-dir="../../outputs/stats">   one NAME.tex per value
    <div class="ws-deck" data-stats-src="stats.json">             {"NAME": value, ...}

and uses ``<span data-stat="NAME">placeholder</span>`` (also on SVG ``<text>`` /
``<tspan>`` in inlined figures and in loaded tables) or ``\\stat{NAME}`` inside
``$...$`` math. The engine fills them at load; this tool reads the same files
without a browser and prints one line per stat:

    n_analyses = 4,051                         slides 3, 7
    half_life_months = 24 (round 0)            slide 9   placeholder "25"
    MISSING no_such_stat                       slide 4   stats/no_such_stat.tex: not found

Usage:
    python3 tools/check_stats.py deck/index.html
    python3 tools/check_stats.py deck/index.html --json
    python3 tools/check_stats.py deck/index.html --stats-dir ../outputs/stats   # deck declares none

Exit status 1 when a stat is missing or the deck declares no source.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _common import deck_root_attributes, resolve_deck_path  # noqa: E402
from get_slide import VOID_TAGS  # noqa: E402

STAT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:+-]*$")
STAT_MACRO_RE = re.compile(r"\\stat\s*\{\s*([^{}]*?)\s*\}")


# ---------------------------------------------------------------- normalisation
# Keep in step with statTexToText / roundStatText in engine/webslides.js
# (tests/test_deck_content.py checks both on the fixture deck).
def strip_tex_comment(raw):
    return " ".join(re.sub(r"(^|[^\\])%.*$", r"\1", line) for line in re.split(r"\r?\n", str(raw)))


def stat_tex_to_text(raw):
    s = strip_tex_comment(raw)
    for pattern, repl in (
        (r"\\textless\b\s*", "<"), (r"\\textgreater\b\s*", ">"),
        (r"\\(?:times)\b\s*", "\u00d7"), (r"\\pm\b\s*", "\u00b1"),
        (r"\\(?:leq?)\b\s*", "\u2264"), (r"\\(?:geq?)\b\s*", "\u2265"),
        (r"\\(?:,|thinspace\b\s*)", "\u2009"), (r"(^|[^\\])~", "\\1\u00a0"),
        (r"(^|[^\\])\$", r"\1"), (r"(^|[^\\])\$", r"\1"),
        (r"\\([%$&#_{}])", r"\1"),
        (r"\{,\}", ","), (r"\{\.\}", "."), (r"[{}]", ""),
        (r"---", "\u2014"), (r"--", "\u2013"),
        (r"(^|[\s(\[,;:=<>/])-(?=\.?\d)", "\\1\u2212"),
        (r"\s+", " "),
    ):
        s = re.sub(pattern, repl, s)
    return s.strip()


def round_stat_text(text, digits_raw):
    try:
        digits = int(str(digits_raw).strip())
    except ValueError:
        return text
    if digits < 0 or digits > 20:
        return text

    def one(m):
        num, exp = m.group(1), m.group(2)
        try:
            value = float(num.replace(",", "") + ("e" + exp.replace("\u2212", "-") if exp else ""))
        except ValueError:
            return m.group(0)
        whole, _, frac = f"{value:.{digits}f}".partition(".")
        if "," in num:
            whole = f"{int(whole):,}" if whole.lstrip("-").isdigit() else whole
        return whole + ("." + frac if frac else "")

    return re.sub(r"(\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE]([+\-\u2212]?\d+))?", one, text)


# ---------------------------------------------------------------- deck scanning
class StatScanner(HTMLParser):
    """Collect data-stat elements, \\stat{} macros, inlined SVGs and tables."""

    def __init__(self, where, slide=None):
        super().__init__(convert_charrefs=True)
        self.where = where
        self.slide = slide
        self.slide_count = 0
        self.uses = []
        self.figures = []  # (src, slide) for <img data-inline> and data-table-src
        self.open = []     # [tag, depth, use] for data-stat elements being read

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and "slide" in (a.get("class") or "").split():
            self.slide_count += 1
            self.slide = self.slide_count
        for entry in self.open:
            if entry[0] == tag:
                entry[1] += 1
        if tag == "img" and "data-inline" in a and a.get("src"):
            self.figures.append(("svg", a["src"], self.slide))
        if a.get("data-table-src"):
            self.figures.append(("table", a["data-table-src"], self.slide))
        if "data-stat" in a:
            use = {"name": (a.get("data-stat") or "").strip(), "slide": self.slide, "where": self.where,
                   "kind": "element", "round": a.get("data-stat-round"), "placeholder": ""}
            self.uses.append(use)
            if tag not in VOID_TAGS:
                self.open.append([tag, 1, use])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for entry in list(self.open):
            if entry[0] == tag:
                entry[1] -= 1
                if entry[1] == 0:
                    self.open.remove(entry)

    def handle_data(self, data):
        for entry in self.open:
            entry[2]["placeholder"] += data
        for m in STAT_MACRO_RE.finditer(data):
            self.uses.append({"name": m.group(1), "slide": self.slide, "where": self.where,
                              "kind": "macro", "round": None, "placeholder": None})


def local_file(base_dir, src):
    parsed = urllib.parse.urlparse(src)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return (base_dir / urllib.parse.unquote(parsed.path)).resolve()


def scan_deck(html_path):
    deck_dir = html_path.parent
    top = StatScanner("index.html")
    top.feed(html_path.read_text(encoding="utf-8", errors="replace"))
    uses = list(top.uses)
    for kind, src, slide in top.figures:
        path = local_file(deck_dir, src)
        if path is None or not path.is_file():
            continue
        sub = StatScanner(f"{kind} {src}", slide=slide)
        sub.feed(path.read_text(encoding="utf-8", errors="replace"))
        uses.extend(sub.uses)
    return uses


# ---------------------------------------------------------------- resolution
class StatsSource:
    def __init__(self, dir_path=None, src_path=None, dir_label=None, src_label=None):
        self.dir_path = dir_path
        self.src_path = src_path
        self.dir_label = dir_label or (str(dir_path) if dir_path else None)
        self.src_label = src_label or (str(src_path) if src_path else None)
        self.json = None
        self.json_error = None
        if src_path is not None:
            try:
                data = json.loads(src_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.json = data
                else:
                    self.json_error = "not a JSON object"
            except (OSError, ValueError) as exc:
                self.json_error = str(exc)

    def resolve(self, name):
        """(raw value, source label) or (None, problem)."""
        if not STAT_NAME_RE.match(name or ""):
            return None, "invalid stat name"
        if self.src_path is not None:
            label = f"{self.src_label}#{name}"
            if self.json is not None and name in self.json:
                value = self.json[name]
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    return None, f"{label}: value is not a string or number"
                return (value if isinstance(value, str) else json.dumps(value)), label
            if self.dir_path is None:
                return None, f"{label}: " + (f"stats file failed to load: {self.json_error}" if self.json_error else "not in stats file")
        path = self.dir_path / f"{name}.tex"
        label = f"{self.dir_label.rstrip('/')}/{name}.tex"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None, f"{label}: not found"
        if stat_tex_to_text(raw) == "":
            return None, f"{label}: empty value"
        return raw, label


def deck_source(html_path, args):
    deck_dir = html_path.parent
    if args.stats_dir or args.stats_src:
        d = Path(args.stats_dir).expanduser().resolve() if args.stats_dir else None
        s = Path(args.stats_src).expanduser().resolve() if args.stats_src else None
        return StatsSource(d, s)
    attrs = deck_root_attributes(html_path)
    d = (attrs.get("data-stats-dir") or "").strip()
    s = (attrs.get("data-stats-src") or "").strip()
    if not d and not s:
        return None
    return StatsSource(local_file(deck_dir, d) if d else None, local_file(deck_dir, s) if s else None,
                       dir_label=d or None, src_label=s or None)


def same_text(a, b):
    norm = lambda s: re.sub(r"\s+", " ", (s or "").replace("\u2212", "-")).strip()  # noqa: E731
    return norm(a) == norm(b)


def check(html_path, args):
    uses = scan_deck(html_path)
    source = deck_source(html_path, args)
    rows = {}
    for use in uses:
        key = (use["name"], use["round"])
        row = rows.setdefault(key, {"name": use["name"], "round": use["round"], "slides": [], "places": 0,
                                    "value": None, "source": None, "problem": None, "placeholders": []})
        row["places"] += 1
        if use["slide"] is not None and use["slide"] not in row["slides"]:
            row["slides"].append(use["slide"])
        if use["placeholder"] is not None:
            row["placeholders"].append(use["placeholder"].strip())
    for row in rows.values():
        if source is None:
            row["problem"] = "no stats source"
            continue
        raw, label = source.resolve(row["name"])
        if raw is None:
            row["problem"] = label
            continue
        text = stat_tex_to_text(raw)
        row["value"] = round_stat_text(text, row["round"]) if row["round"] else text
        row["source"] = label
        row["stale"] = sorted({p for p in row["placeholders"] if not same_text(p, row["value"])})
    return source, list(rows.values())


def slides_label(slides):
    if not slides:
        return ""
    return ("slide " if len(slides) == 1 else "slides ") + ", ".join(str(s) for s in sorted(slides))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("deck", help="deck index.html, directory, or deck alias")
    parser.add_argument("--json", action="store_true", help="print every stat as JSON")
    parser.add_argument("--stats-dir", help="stats directory to use instead of the deck's data-stats-dir")
    parser.add_argument("--stats-src", help="stats JSON file to use instead of the deck's data-stats-src")
    args = parser.parse_args(argv)

    html_path = resolve_deck_path(args.deck)
    if not html_path.is_file():
        import ws_deck

        try:
            html_path = ws_deck.resolve_deck(args.deck)
        except ws_deck.DeckError:
            pass
    if not html_path.is_file():
        print(f"ERROR: {html_path} not found", file=sys.stderr)
        return 2
    source, rows = check(html_path, args)
    missing = [r for r in rows if r["value"] is None]

    if args.json:
        print(json.dumps({"deck": str(html_path),
                          "source": None if source is None else {"dir": source.dir_label, "src": source.src_label},
                          "stats": rows}, indent=1, ensure_ascii=False))
        return 1 if missing or source is None else 0

    if source is None:
        print(f"{len(rows)} data-stat uses, but the deck declares no stats source "
              "(data-stats-dir / data-stats-src on .ws-deck; or pass --stats-dir)")
        return 1
    width = max([len(r["name"]) + len(r["value"] or "") + 12 for r in rows] + [20])
    stale = 0
    for row in sorted(rows, key=lambda r: (min(r["slides"]) if r["slides"] else 0, r["name"])):
        where = slides_label(row["slides"])
        if row["value"] is None:
            print(f"MISSING {row['name']}".ljust(width) + f"  {where}  {row['problem']}".rstrip())
            continue
        rnd = f" (round {row['round']})" if row["round"] else ""
        note = ""
        if row.get("stale"):
            stale += 1
            note = "  placeholder " + ", ".join(f'"{p}"' for p in row["stale"])
        print(f"{row['name']} = {row['value']}{rnd}".ljust(width) + f"  {where}{note}".rstrip())
    places = sum(r["places"] for r in rows)
    print(f"{len(rows)} stats ({places} places), {len(missing)} missing, {stale} with a different placeholder")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())

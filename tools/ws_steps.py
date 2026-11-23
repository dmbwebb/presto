"""Static reveal-step model of one slide, and step renumbering for ``ws steps``.

The model reads the slide's ``<section>`` text plus, in document order, the
SVG files it inlines (``<img data-inline src=...svg>``) and the tables it loads
(``data-table-src``). For every reveal attribute it records where the numbers
sit in which file, so renumbering rewrites only those characters.

Reveal attributes (pinned contract):

- ``data-step``, ``data-highlight``, ``data-dim``: ranges ``3``, ``3+``,
  ``2-4``, ``-3``; ``data-step`` also takes ``+`` / ``=`` (``+:N`` / ``=:N``),
  which are resolved for display but never rewritten.
- ``data-class="cls:R"``: one or more ``cls:R`` entries (separated by spaces,
  ``;`` or ``,``); only each ``R`` changes.
- ``data-fade-after="N"`` (or ``+`` / ``=``).
- ``data-focus="R:SELECTOR"`` entries separated by ``;``; only each ``R`` changes.
- ``data-reveal-start="N"`` on ``data-reveal="each"`` lists.
- the section's ``data-steps`` (ignored by the engine) is kept equal to the new
  step count when it was correct before.

Renumbering is a map on *transition times*: an element shown during ``s..e``
appears at ``s`` and hides at ``e + 1``; both move through the map.

- ``insert-after K``: new step ``K+1`` starts as a copy of step ``K``
  (``t > K`` becomes ``t + 1``).
- ``delete K``: step ``K`` disappears; what changed at ``K`` now changes at the
  step that follows it (``t > K`` becomes ``t - 1``). With ``merge="prev"``
  step ``K``'s changes happen one step earlier instead (``t >= K`` becomes
  ``t - 1``), which is the way to delete a last step that still reveals
  something.
- ``swap A B``: what happens at step ``A`` happens at ``B`` and vice versa.
  An element whose two transitions would cross (shown only at ``A``, say)
  keeps the steps it is shown at, permuted.
"""

from __future__ import annotations

import html as html_lib
import re
from pathlib import Path

import get_slide

RANGE_ATTRS = ("data-step", "data-highlight", "data-dim")
REVEAL_ATTRS = RANGE_ATTRS + ("data-class", "data-fade-after", "data-focus", "data-reveal-start")
ATTR_RE = re.compile(r"""([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""")
CLASS_ENTRY_RE = re.compile(r"([^\s:;,]+)\s*:\s*([^\s;,]+)")
VOID = get_slide.VOID_TAGS
RAW_TEXT = ("script", "style")


class StepsError(ValueError):
    pass


# ---------------------------------------------------------------- range values
def parse_range(raw: str):
    """Return (form, start, end) for an absolute range, ('rel', ...) or None.

    ``start`` None means "from the beginning" (``-3``); ``end`` None means open.
    """
    v = raw.strip()
    if v == "":
        return ("empty", 1, None)
    m = re.fullmatch(r"([+=])(?:\s*:\s*(\d+))?", v)
    if m:
        return ("rel", m.group(1), int(m.group(2)) if m.group(2) else None)
    if re.fullmatch(r"\d+", v):
        return ("n", int(v), None)
    m = re.fullmatch(r"(\d+)\s*\+", v)
    if m:
        return ("n+", int(m.group(1)), None)
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", v)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return ("a-b", a, b) if b >= a else None
    m = re.fullmatch(r"-\s*(\d+)", v)
    if m:
        return ("-b", None, int(m.group(1)))
    return None


def format_range(raw: str, start, end) -> str:
    """Rewrite the numbers in ``raw`` to ``start``/``end``, keeping its form and spacing."""
    form = parse_range(raw)
    if form is None or form[0] in ("rel", "empty"):
        return raw
    values = {"n": [start], "n+": [start], "a-b": [start, end], "-b": [end]}[form[0]]
    out, last = [], 0
    for m, value in zip(re.finditer(r"\d+", raw), values):
        out.append(raw[last:m.start()])
        out.append(str(value))
        last = m.end()
    out.append(raw[last:])
    return "".join(out)


# ---------------------------------------------------------------- tokenising
def _skip_to(text, pos, needle):
    end = text.find(needle, pos)
    return len(text) if end < 0 else end + len(needle)


def scan_elements(text: str, start: int = 0, end: int | None = None):
    """Yield element dicts (tag, attrs with value spans, offsets, parent) in document order."""
    end = len(text) if end is None else end
    tag_re = re.compile(r"<(/?)([A-Za-z][\w:.-]*)")
    pos = start
    stack = []
    elements = []
    while pos < end:
        lt = text.find("<", pos, end)
        if lt < 0:
            break
        if text.startswith("<!--", lt):
            pos = _skip_to(text, lt + 4, "-->")
            continue
        if text.startswith("<![CDATA[", lt):
            pos = _skip_to(text, lt + 9, "]]>")
            continue
        if text.startswith("<!", lt) or text.startswith("<?", lt):
            pos = _skip_to(text, lt + 2, ">")
            continue
        m = tag_re.match(text, lt)
        if not m:
            pos = lt + 1
            continue
        tag_end = get_slide.tag_end(text, lt)
        if tag_end < 0:
            break
        name = m.group(2).lower()
        if m.group(1):
            for i in range(len(stack) - 1, -1, -1):
                if stack[i]["tag"] == name:
                    for el in stack[i:]:
                        el["close"] = lt
                        el["end"] = tag_end
                    del stack[i:]
                    break
            pos = tag_end
            continue
        raw = text[lt:tag_end]
        attrs = {}
        body_start = m.end()
        body_end = tag_end - (2 if raw.endswith("/>") else 1)
        for am in ATTR_RE.finditer(text, body_start, body_end):
            key = am.group(1).lower()
            for g in (2, 3, 4):
                if am.group(g) is not None:
                    attrs.setdefault(key, (am.group(g), am.start(g)))
                    break
            else:
                attrs.setdefault(key, ("", am.end()))
        el = {
            "tag": name, "attrs": attrs, "start": lt, "open_end": tag_end,
            "close": tag_end, "end": tag_end, "parent": stack[-1] if stack else None,
        }
        elements.append(el)
        self_closing = raw.endswith("/>") or name in VOID
        if not self_closing:
            stack.append(el)
            if name in RAW_TEXT:
                close = re.compile(rf"</{name}\s*>", re.IGNORECASE).search(text, tag_end)
                close_at = close.start() if close else end
                pos = close_at
                continue
        pos = tag_end
    for el in stack:
        el["close"] = end
        el["end"] = end
    return elements


def element_label(text: str, el: dict, prefix: str = "") -> str:
    raw_inner = text[el["open_end"]:el["close"]]
    inner = re.sub(r"<!--.*?-->", " ", raw_inner, flags=re.DOTALL)
    inner = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", inner, flags=re.DOTALL | re.IGNORECASE)
    if prefix:  # SVG: separate <text>/<tspan> pieces
        inner = re.sub(r"<(?:text|tspan)\b", r" \g<0>", inner)
    inner = html_lib.unescape(get_slide.strip_html_tags(inner))
    inner = re.sub(r"\s+", " ", inner).strip()
    if inner:
        label = inner if len(inner) <= 32 else inner[:30].rstrip() + "…"
        return f'{prefix}"{label}"'
    attrs = el["attrs"]
    desc = el["tag"]
    if attrs.get("id"):
        desc += "#" + attrs["id"][0]
    elif attrs.get("class"):
        desc += "." + ".".join(attrs["class"][0].split()[:2])
    elif attrs.get("src"):
        desc += " " + Path(attrs["src"][0]).name
    else:
        shapes = re.findall(r"<(circle|ellipse|rect|line|path|polygon|polyline)\b", raw_inner)
        if shapes:
            top = max(set(shapes), key=shapes.count)
            desc += f": {shapes.count(top)} {top}"
    return f"{prefix}<{desc}>"


def label_of(item: dict) -> str:
    if item["label"] is None:
        item["label"] = element_label(item["source"].text, item["el"], item["prefix"])
    return item["label"]


# ---------------------------------------------------------------- slide model
class Source:
    """One file holding reveal attributes (the deck, an SVG or a table)."""

    def __init__(self, path: Path, text: str, kind: str, rel: str):
        self.path = Path(path)
        self.text = text
        self.kind = kind  # "deck" | "svg" | "table"
        self.rel = rel    # display name

    def __repr__(self):
        return f"Source({self.rel})"


def _asset_path(deck_dir: Path, src: str):
    src = html_lib.unescape(src).split("#")[0].split("?")[0].strip()
    if not src or re.match(r"^[a-z]+:", src, re.IGNORECASE):
        return None
    p = (deck_dir / src).resolve()
    return p if p.is_file() else None


def build_model(deck_index: Path, deck_text: str, slide: dict, asset_texts: dict | None = None):
    """Return the slide's reveal model: {'fields', 'elements', 'sources', 'max', ...}.

    ``asset_texts`` maps asset paths to replacement text (used to model a
    renumbered result before writing it).
    """
    deck_dir = Path(deck_index).parent
    asset_texts = asset_texts or {}
    deck_src = Source(deck_index, deck_text, "deck", Path(deck_index).name)
    sources = [deck_src]
    ordered = []  # (source, element) in document order, assets spliced in

    def add_asset(path: Path, kind: str):
        for s in sources:
            if s.path == path:
                return s
        text = asset_texts.get(path)
        if text is None:
            text = path.read_bytes().decode("utf-8", errors="surrogateescape")
        try:
            rel = path.relative_to(deck_dir).as_posix()
        except ValueError:
            rel = str(path)
        s = Source(path, text, kind, rel)
        sources.append(s)
        return s

    for el in scan_elements(deck_text, slide["start"], slide["end"]):
        ordered.append((deck_src, el))
        attrs = el["attrs"]
        if el["tag"] == "img" and "data-inline" in attrs and attrs.get("src"):
            path = _asset_path(deck_dir, attrs["src"][0])
            if path is not None and path.suffix.lower() == ".svg":
                src = add_asset(path, "svg")
                ordered.extend((src, sub) for sub in scan_elements(src.text))
        table = attrs.get("data-table-src")
        if table:
            path = _asset_path(deck_dir, table[0])
            if path is not None:
                src = add_asset(path, "table")
                ordered.extend((src, sub) for sub in scan_elements(src.text))

    fields = []
    max_start = 0
    prev_start = None
    reveal_counters = {}  # id(list element) -> next auto step
    elements = []

    def ancestors(el):
        p = el["parent"]
        while p is not None:
            yield p
            p = p["parent"]

    def elem_max(e):
        m = 0
        for f in e["fields"]:
            for v in (f["start"], f["end"]):
                if isinstance(v, int):
                    m = max(m, v)
        return m

    def relative_start(sym):
        """"+" is one past the latest start so far; "=" repeats the previous start (else 1)."""
        if sym == "+":
            return max_start + 1
        return prev_start if prev_start is not None else 1

    for order, (src, el) in enumerate(ordered):
        attrs = el["attrs"]
        prefix = "" if src.kind == "deck" else f"{Path(src.rel).name}:"
        item = {"source": src, "el": el, "order": order, "fields": [], "label": None, "prefix": prefix}

        def field(attr, raw, offset, kind, start, end, form, entry=None):
            f = {
                "item": item, "source": src, "attr": attr, "raw": raw, "offset": offset,
                "kind": kind, "start": start, "end": end, "form": form, "entry": entry,
            }
            item["fields"].append(f)
            fields.append(f)
            return f

        def range_field(attr, raw, offset, entry=None):
            """An absolute range field; relative values are not resolved here."""
            parsed = parse_range(raw)
            if parsed is None or parsed[0] == "rel":
                return field(attr, raw, offset, "bad" if parsed is None else "rel", None, None, "bad", entry=entry)
            return field(attr, raw, offset, "range", parsed[1], parsed[2], parsed[0], entry=entry)

        # data-step (with + / = resolution)
        if "data-step" in attrs:
            raw, off = attrs["data-step"]
            parsed = parse_range(raw)
            if parsed is None:
                field("data-step", raw, off, "bad", None, None, "bad")
            elif parsed[0] == "rel":
                sym, length = parsed[1], parsed[2]
                start = relative_start(sym)
                end = start + length - 1 if length else None
                field("data-step", raw, off, "rel", start, end, "rel")
                max_start = max(max_start, start)
                prev_start = start
            else:
                _form, start, end = parsed
                field("data-step", raw, off, "range", start, end, parsed[0])
                s = start if start is not None else 0
                max_start = max(max_start, s)
                prev_start = s
        elif el["tag"] == "li":
            owner = None
            for anc in ancestors(el):
                if anc["tag"] in ("ul", "ol") and anc["attrs"].get("data-reveal", ("",))[0] == "each" \
                        and id(anc) in reveal_counters:
                    owner = anc
            if owner is not None:
                step = reveal_counters[id(owner)]
                reveal_counters[id(owner)] = step + 1
                field("data-step", "", el["open_end"], "auto", step, None, "auto")
                max_start = max(max_start, step)
                prev_start = step
        for attr in ("data-highlight", "data-dim"):
            if attr in attrs:
                raw, off = attrs[attr]
                range_field(attr, raw, off)
        if "data-class" in attrs:
            raw, off = attrs["data-class"]
            for m in CLASS_ENTRY_RE.finditer(raw):
                range_field("data-class", m.group(2), off + m.start(2), entry=m.group(1))
        if "data-fade-after" in attrs:
            raw, off = attrs["data-fade-after"]
            v = raw.strip()
            lead = len(raw) - len(raw.lstrip())
            if v in ("+", "="):
                field("data-fade-after", raw, off, "rel", relative_start(v), None, "rel")
            elif v.isdigit():
                field("data-fade-after", v, off + lead, "point", int(v), None, "n")
            else:
                field("data-fade-after", raw, off, "bad", None, None, "bad")
        if "data-focus" in attrs:
            raw, off = attrs["data-focus"]
            pos = 0
            for part in raw.split(";"):
                colon = part.find(":")
                if colon > 0:
                    r = part[:colon]
                    lead = len(r) - len(r.lstrip())
                    range_field("data-focus", r.strip(), off + pos + lead, entry=part[colon + 1:].strip())
                pos += len(part) + 1
        if el["tag"] in ("ul", "ol") and attrs.get("data-reveal", ("",))[0] == "each":
            nested = any(a["tag"] in ("ul", "ol") and id(a) in reveal_counters for a in ancestors(el))
            if not nested:
                rs = attrs.get("data-reveal-start")
                if rs and rs[0].strip().isdigit():
                    v = rs[0].strip()
                    lead = len(rs[0]) - len(rs[0].lstrip())
                    field("data-reveal-start", v, rs[1] + lead, "point", int(v), None, "n")
                    first = int(v)
                else:
                    anc_ids = {id(a) for a in ancestors(el)}
                    first = 1 + max([elem_max(e) for e in elements if id(e["el"]) not in anc_ids] + [0])
                reveal_counters[id(el)] = first
        if item["fields"]:
            elements.append(item)

    max_step = 0
    for f in fields:
        if f["kind"] == "bad":
            continue
        for v in (f["start"], f["end"]):
            if isinstance(v, int):
                max_step = max(max_step, v)
    section = ordered[0][1] if ordered else None
    declared = section["attrs"].get("data-steps") if section else None
    return {
        "slide": slide, "fields": fields, "elements": elements, "sources": sources,
        "max": max_step, "declared_steps": declared,  # (raw value, offset) or None
    }


# ---------------------------------------------------------------- describing
def step_reveals(model) -> dict:
    """Map step -> list of short change descriptions ("+label", "-label", "fade label", ...)."""
    out = {}

    def add(step, text):
        if isinstance(step, int):
            out.setdefault(step, [])
            if text not in out[step]:
                out[step].append(text)

    for item in model["elements"]:
        label = label_of(item)
        for f in item["fields"]:
            if f["kind"] == "bad" or f["start"] is None and f["end"] is None:
                continue
            s, e = f["start"], f["end"]
            attr = f["attr"]
            if attr == "data-step":
                if s:
                    add(s, "+" + label)
                if e is not None:
                    add(e + 1, "-" + label)
            elif attr == "data-class":
                if s:
                    add(s, f"{f['entry']} on {label}")
                if e is not None:
                    add(e + 1, f"{f['entry']} off {label}")
            elif attr == "data-fade-after":
                add(s, "fade " + label)
            elif attr == "data-focus":
                if s:
                    add(s, f"focus {f['entry']} in {label}")
                if e is not None:
                    add(e + 1, f"unfocus {label}")
            elif attr == "data-highlight":
                if s:
                    add(s, "highlight " + label)
                if e is not None:
                    add(e + 1, "unhighlight " + label)
            elif attr == "data-dim":
                if s:
                    add(s, "dim " + label)
                if e is not None:
                    add(e + 1, "undim " + label)
    return out


def summarise(changes, width=110) -> str:
    if not changes:
        return "-"
    out = ""
    for n, c in enumerate(changes):
        piece = (" " if out else "") + c
        if len(out) + len(piece) > width:
            return out + f" ... and {len(changes) - n} more"
        out += piece
    return out


# ---------------------------------------------------------------- renumbering
def transition_map(op: str, a: int, b: int | None = None, merge: str = "next"):
    if op == "insert-after":
        return lambda t: t if t <= a else t + 1
    if op == "delete":
        if merge == "prev":
            return lambda t: t if t < a else t - 1
        return lambda t: t if t <= a else t - 1
    if op == "swap":
        return lambda t: b if t == a else (a if t == b else t)
    raise StepsError(f"unknown operation {op!r}")


def expected_max(op, old_max, a, b=None):
    if op == "insert-after":
        return old_max + 1
    if op == "delete":
        return old_max - 1
    return old_max


def validate_op(op, old_max, a, b=None):
    if op == "insert-after":
        if not 0 <= a <= old_max:
            raise StepsError(f"insert-after {a}: the slide has steps 0-{old_max}")
    elif op == "delete":
        if not 1 <= a <= old_max:
            raise StepsError(f"delete {a}: the slide has steps 0-{old_max} (step 0 cannot be deleted)")
    elif op == "swap":
        if a == b or not (1 <= a <= old_max and 1 <= b <= old_max):
            raise StepsError(f"swap {a} {b}: give two different steps in 1-{old_max}")


def map_range(tau, s, e):
    """(start, end) moved through the transition map: shown from ``s``, hidden at ``e + 1``."""
    ns = tau(s) if isinstance(s, int) and s > 0 else s
    ne = tau(e + 1) - 1 if isinstance(e, int) else e
    return ns, ne


def new_range(f, tau, op, a, b, merge="next"):
    """Return (start, end, note) for a range field under the transition map."""
    s, e = f["start"], f["end"]
    ns, ne = map_range(tau, s, e)
    if isinstance(ns, int) and isinstance(ne, int) and ne < ns:
        old = f"{s}" + (f"-{e}" if e != s else "")
        if op == "swap":
            # Transitions would cross: keep the steps it is shown at, permuted.
            shown = sorted(tau(t) for t in range(s, e + 1))
            if shown == list(range(shown[0], shown[-1] + 1)):
                new = f"{shown[0]}" + (f"-{shown[-1]}" if shown[-1] != shown[0] else "")
                return shown[0], shown[-1], f"was shown only at step {old}, now only at {new}"
        hint = ""
        if op == "delete" and merge != "prev" and s == a and s >= 1:
            hint = f" (--merge prev would show it at step {s - 1})"
        return None, None, f"would never be shown (it was shown only at step {old}){hint}"
    return ns, ne, None


def plan_renumber(model, op, a, b=None, merge="next", include_assets=False):
    """Return a plan: edits per source, the step map, warnings and errors."""
    old_max = model["max"]
    validate_op(op, old_max, a, b)
    tau = transition_map(op, a, b, merge)
    edits = {}
    warnings, errors = [], []
    skipped_assets = {}
    expected = {}  # id(field) -> (start, end) expected after renumbering

    for f in model["fields"]:
        src = f["source"]
        if f["kind"] == "bad":
            warnings.append(f"{src.rel}: {f['attr']}={f['raw']!r} on {label_of(f['item'])} is not a valid "
                            "step value; left as is")
            continue
        if f["kind"] in ("rel", "auto"):
            if f["kind"] == "rel":
                expected[id(f)] = map_range(tau, f["start"], f["end"])
            else:
                expected[id(f)] = (tau(f["start"]), None)
            continue
        if f["kind"] == "point":
            ns, ne, note = tau(f["start"]), None, None
        else:
            ns, ne, note = new_range(f, tau, op, a, b, merge)
            if ns is None and ne is None and note:
                errors.append(f"{src.rel}: {label_of(f['item'])} {f['attr']}={f['raw']!r} {note}; "
                              "change or remove it first")
                continue
            if note:
                warnings.append(f"{src.rel}: {label_of(f['item'])} {f['attr']}={f['raw']!r} {note}")
        expected[id(f)] = (ns, ne)
        if f["kind"] == "point":
            new_raw = str(ns)
        else:
            new_raw = format_range(f["raw"], ns, ne)
        if new_raw == f["raw"]:
            continue
        if src.kind != "deck" and not include_assets:
            skipped_assets.setdefault(src.rel, []).append(f)
            continue
        edits.setdefault(src.path, []).append((f["offset"], f["offset"] + len(f["raw"]), new_raw, f))

    # Section data-steps: keep it right if it was right.
    declared = model["declared_steps"]
    want = expected_max(op, old_max, a, b)
    deck_src = model["sources"][0]
    if declared is not None and declared[0].strip() == str(old_max):
        raw, off = declared
        off += len(raw) - len(raw.lstrip())
        edits.setdefault(deck_src.path, []).append((off, off + len(raw.strip()), str(want), None))
    return {
        "op": op, "a": a, "b": b, "merge": merge, "tau": tau, "old_max": old_max, "want_max": want,
        "edits": edits, "warnings": warnings, "errors": errors, "skipped_assets": skipped_assets,
        "expected": expected,
    }


def apply_edits(text: str, edits) -> str:
    out = text
    for start, end, new, _f in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:start] + new + out[end:]
    return out


def step_map_line(plan) -> str:
    """Old step -> new step ("x": that state no longer exists)."""
    tau, op, a, b = plan["tau"], plan["op"], plan["a"], plan["b"]
    old_max, want = plan["old_max"], plan["want_max"]
    if op == "swap":
        return f"{a}<>{b} (the changes at steps {a} and {b} trade places; other steps keep their numbers)"
    parts = []
    for t in range(old_max + 1):
        new = tau(t) if t else 0
        merged = t < old_max and tau(t + 1) == new
        parts.append(f"{t}>x" if merged or new > want else f"{t}>{new}")
    note = ""
    if op == "insert-after":
        note = f" (new step {a + 1} starts as a copy of step {a})"
    elif op == "delete":
        if plan["merge"] == "prev":
            note = f" (step {a}'s changes now happen at step {a - 1})"
        else:
            note = f" (step {a}'s changes now happen with the next step)"
    return " ".join(parts) + note


# ---------------------------------------------------------------- generated assets
GENERATOR_COMMENT_RE = re.compile(r"<!--[^>]*?(generated|generator|created by|made by|\.py\b)[^>]*?-->",
                                  re.IGNORECASE)


def generator_hint(asset: Path, deck_dir: Path):
    """Return a short note when ``asset`` looks script-generated, naming the script if found."""
    text = asset.read_text(encoding="utf-8", errors="replace")
    reasons = []
    m = GENERATOR_COMMENT_RE.search(text[:4000])
    if m:
        reasons.append("its comment says " + repr(re.sub(r"\s+", " ", m.group(0))[:80]))
    scripts = []
    seen = set()
    for code_dir in (deck_dir / "code", asset.parent.parent / "code", asset.parent / "code", deck_dir.parent / "code"):
        code_dir = code_dir.resolve()
        if code_dir in seen or not code_dir.is_dir():
            continue
        seen.add(code_dir)
        for script in sorted(code_dir.rglob("*")):
            if script.suffix.lower() not in (".py", ".r", ".jl", ".js", ".m", ".do") or not script.is_file():
                continue
            try:
                body = script.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if asset.name in body or re.search(rf"""["']{re.escape(asset.stem)}["']""", body):
                scripts.append(script)
        if not scripts:
            reasons.append(f"there is a {code_dir.name}/ folder of scripts beside it")
    if not reasons and not scripts:
        return None
    # Scripts that only check or test the figure are not its generator.
    makers = [s for s in scripts if not re.match(r"(check|test|verify|validate)_?", s.stem, re.IGNORECASE)]
    scripts = makers or scripts
    if scripts:
        names = ", ".join(_short(s, deck_dir) for s in scripts[:3])
        return f"generated by {names}: regenerating overwrites this edit, so change the step numbers there too"
    return "looks generated (" + "; ".join(reasons) + "): regenerating would overwrite this edit"


def _short(path: Path, deck_dir: Path) -> str:
    try:
        return path.relative_to(deck_dir).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- driver
def renumber(index: Path, deck_text: str, slide: dict, op: str, a: int, b: int | None = None,
             merge: str = "next", include_assets: bool = False, all_slides=None, html_only: bool = False):
    """Plan a renumbering, check its result, and return (report dict, {path: new_text})."""
    model = build_model(index, deck_text, slide)
    plan = plan_renumber(model, op, a, b, merge=merge, include_assets=include_assets)
    texts = {}
    for src in model["sources"]:
        if src.path in plan["edits"]:
            texts[src.path] = apply_edits(src.text, plan["edits"][src.path])
    deck_dir = Path(index).parent
    new_deck_text = texts.get(Path(index), deck_text)
    new_slide = dict(slide)
    delta = len(new_deck_text) - len(deck_text)
    new_slide["end"] = slide["end"] + delta
    after = build_model(index, new_deck_text, new_slide,
                        asset_texts={p: t for p, t in texts.items() if p != Path(index)})
    errors = list(plan["errors"])
    warnings = list(plan["warnings"])
    # Relative (+ / =) and auto-numbered (data-reveal="each") values are never
    # rewritten: say where they no longer land where the map would put them.
    by_order = {}
    for f in after["fields"]:
        by_order.setdefault((f["item"]["order"], f["attr"], f["entry"]), f)
    drifted = False
    for f in model["fields"]:
        if f["kind"] not in ("rel", "auto") or id(f) not in plan["expected"]:
            continue
        g = by_order.get((f["item"]["order"], f["attr"], f["entry"]))
        exp = plan["expected"][id(f)]
        if g is not None and (g["start"], g["end"]) != exp:
            what = f"{f['attr']}={f['raw']!r}" if f["kind"] == "rel" else "its data-reveal=\"each\" step"
            drifted = True
            warnings.append(
                f"{label_of(f['item'])} ({what}) now resolves to step {g['start']}, the map expects "
                f"{exp[0]}: set it explicitly if that is wrong")
    if not errors and after["max"] != plan["want_max"]:
        hint = ""
        if op == "delete" and merge != "prev" and a == plan["old_max"]:
            starts = [label_of(f["item"]) for f in model["fields"] if f["start"] == a and f["kind"] != "bad"]
            hint = (f"; step {a} is the last step and still reveals {summarise(starts, 80)}: delete those "
                    "first, or pass --merge prev to show them one step earlier")
        elif not include_assets and plan["skipped_assets"]:
            hint = "; the rest of the steps live in inlined files (rerun with --include-svg)"
        elif drifted:
            hint = ("; the later steps are relative (data-step=\"+\"/\"=\" or data-reveal=\"each\") and "
                    "renumber themselves: to add a step there, give its element data-step=\"+\" where it "
                    "belongs; to drop one, delete or merge its element")
        message = f"the slide would have steps 0-{after['max']}, not 0-{plan['want_max']}{hint}"
        (warnings if html_only and plan["skipped_assets"] else errors).append(message)
    asset_notes = []
    for src in model["sources"][1:]:
        changed = src.path in plan["edits"]
        if changed:
            n = len(plan["edits"][src.path])
            note = f"{src.rel}: {n} value(s) changed"
        elif src.rel in plan["skipped_assets"]:
            n = len(plan["skipped_assets"][src.rel])
            note = f"{src.rel}: NOT changed, {n} step value(s) inside need renumbering"
            if not html_only:
                errors.append(f"{src.rel} holds {n} step value(s) this change moves: rerun with --include-svg "
                              "to renumber them too, or --html-only to leave them out of sync")
        else:
            continue
        hint = generator_hint(src.path, deck_dir) if src.kind == "svg" else None
        if hint:
            note += f"; {hint}"
        if all_slides is not None:
            users = [s["number"] for s in all_slides
                     if s["index"] != slide["index"] and src.path.name in s["html"]]
            if users:
                note += f"; also used on slide(s) {', '.join(map(str, users))}, whose steps change too"
        asset_notes.append(note)
    deck_edits = plan["edits"].get(Path(index), [])
    lines = sorted({deck_text.count("\n", 0, e[0]) + 1 for e in deck_edits})
    report = {
        "plan": plan, "before": model, "after": after, "errors": errors, "warnings": warnings,
        "asset_notes": asset_notes, "deck_changes": len(deck_edits), "deck_lines": lines,
    }
    return report, texts


def format_report(report, slide, dry_run=False) -> list[str]:
    plan = report["plan"]
    op, a, b = plan["op"], plan["a"], plan["b"]
    what = f"{op} {a}" + (f" {b}" if b is not None else "")
    if op == "delete" and plan["merge"] == "prev":
        what += " --merge prev"
    head = (f"slide {slide['number']} #{slide['key']}: {what}, steps 0-{plan['old_max']} -> "
            f"0-{report['after']['max']}" + (" (dry run, nothing written)" if dry_run else ""))
    out = [head, "  map " + step_map_line(plan)]
    after = step_reveals(report["after"])
    tau = plan["tau"]
    origin = {}
    if op != "swap":
        for t in range(1, plan["old_max"] + 1):
            origin[tau(t)] = t  # the later old state holds every change up to it
    for s in range(1, report["after"]["max"] + 1):
        if op == "swap":
            src = tau(s)  # a swap is its own inverse
        else:
            src = origin.get(s)
        tag = f"step {s}" + ("" if src == s else (" (new)" if src is None else f" (was {src})"))
        changes = after.get(s, [])
        if changes or src != s:
            out.append(f"  {tag}: {summarise(changes)}")
    if report["deck_changes"]:
        lines = report["deck_lines"]
        span = f"line {lines[0]}" if len(lines) == 1 else f"lines {lines[0]}-{lines[-1]}"
        deck_name = report["before"]["sources"][0].path.name
        out.append(f"  {deck_name}: {report['deck_changes']} value(s) changed ({span})")
    out.extend("  " + n for n in report["asset_notes"])
    out.extend("  warning: " + w for w in report["warnings"])
    out.extend("  error: " + e for e in report["errors"])
    return out

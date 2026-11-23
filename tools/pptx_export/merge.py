"""Merge per-step captures of one slide into objects with reveal timelines.

Each capture lists what is visible at one step. An object keeps one entry per
distinct appearance: if its look or position changes at a step, the new look is
a second object that replaces the first, while an opacity change stays on the
same object as a PowerPoint transparency effect.
"""

from __future__ import annotations

import copy
import hashlib
import json

OP_EPS = 0.04


def freeform_points(obj):
    """Every (x, y) in a freeform's drawing commands, curve control points included."""
    return [point for cmd in obj["cmds"] for point in zip(cmd[1::2], cmd[2::2])]


def freeform_bounds(obj):
    """(left, top, right, bottom) of a freeform's points."""
    xs, ys = zip(*freeform_points(obj))
    return min(xs), min(ys), max(xs), max(ys)


def _digest(data, length):
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:length]


def _plain_color(c):
    return c["hex"] if isinstance(c, dict) and "hex" in c else c


def _para_sig(p):
    runs = [{k: (_plain_color(v) if k in ("c", "hl") else v) for k, v in r.items()} for r in p["runs"]]
    return {"runs": runs, "align": p.get("align"), "bullet": bool(p.get("bullet")), "level": p.get("level", 0)}


def _sig(obj):
    rect = obj.get("rect") or {}
    kind = obj["kind"]
    if kind == "table":
        return obj["key"]
    data = {"kind": kind}
    if rect:
        data["rect"] = [round(rect.get("x", 0)), round(rect.get("y", 0)), round(rect.get("w", 0))]
        if not obj.get("isList"):
            data["rect"].append(round(rect.get("h", 0)))
    for name in ("fill", "line", "stroke", "pts", "src", "grad"):
        if obj.get(name) is not None:
            value = obj[name]
            if name == "fill":
                data[name] = _plain_color(value)
            elif name == "grad":
                data[name] = [[_plain_color(s["c"]), s["pos"]] for s in value["stops"]]
            else:
                data[name] = {**value, "c": _plain_color(value.get("c"))} if isinstance(value, dict) else value
    if kind == "freeform":
        data["cmds"] = obj["cmds"]
    return _digest(data, 10)


def _scale_alpha(node, factor):
    if isinstance(node, dict):
        if "hex" in node and "a" in node:
            node["a"] = round(min(1.0, node["a"] * factor), 3)
            return
        for value in node.values():
            _scale_alpha(value, factor)
    elif isinstance(node, list):
        for value in node:
            _scale_alpha(value, factor)


def _insert(order, key, after):
    if key in order:
        return
    if after in order:
        order.insert(order.index(after) + 1, key)
    else:
        order.insert(0, key)


def _merge_paras(item, obj, step):
    previous = None
    for para in obj.get("paras", []):
        key = para["pkey"] + "#" + _digest(_para_sig(para), 8)
        slot = item["paras"].get(key)
        if slot is None:
            slot = item["paras"][key] = {"para": copy.deepcopy(para), "ops": {}}
            _insert(item["para_order"], key, previous)
        slot["ops"][step] = para.get("op", obj.get("op", 1))
        previous = key


def merge_frames(frames):
    """frames: list (one per step) of capture['objects']. Returns merged items in z-order."""
    items, order = {}, []
    for step, objects in enumerate(frames):
        previous = None
        for obj in objects:
            key = obj["key"] + "@" + _sig(obj)
            item = items.get(key)
            if item is None:
                item = items[key] = {"obj": copy.deepcopy(obj), "ops": {}, "paras": {}, "para_order": [], "rows": {}}
                _insert(order, key, previous)
            item["ops"][step] = obj.get("op", 1)
            if obj["kind"] == "table":
                hidden_by_row = {}
                for cell in obj["cells"]:
                    hidden_by_row.setdefault(cell["r"], []).append(cell["hidden"])
                for row, flags in hidden_by_row.items():
                    if not all(flags):
                        item["rows"].setdefault(row, set()).add(step)
                if _text_len(obj) > _text_len(item["obj"]):
                    item["obj"] = copy.deepcopy(obj)
            _merge_paras(item, obj, step)
            previous = key
    n_steps = len(frames)
    merged = []
    for key in order:
        merged.extend(_finish(items[key], n_steps))
    return merged


def _text_len(table):
    return sum(len(r.get("t", "")) for c in table["cells"] for p in c["paras"] for r in p["runs"])


def _timeline(ops, n_steps):
    """Visibility and opacity by step, relative to the opacity when first seen."""
    steps = sorted(ops)
    base = ops[steps[0]]
    vis = [s in ops for s in range(n_steps)]
    rel = [round(min(1.0, ops[s] / base), 2) if s in ops else None for s in range(n_steps)]
    return base, vis, rel


def _split_on_brighten(ops):
    """PowerPoint can only dim from the baked-in opacity; a brighter state is a new object."""
    parts, current, base = [], {}, None
    for step in sorted(ops):
        if base is not None and ops[step] > base + OP_EPS:
            parts.append(current)
            current, base = {}, None
        if base is None:
            base = ops[step]
        current[step] = ops[step]
    parts.append(current)
    return parts


def _finish(item, n_steps):
    out = []
    obj = item["obj"]
    for index, ops in enumerate(_split_on_brighten(item["ops"])):
        one = copy.deepcopy(obj)
        captured_op = obj.get("op", 1) or 1
        base, vis, rel = _timeline(ops, n_steps)
        _scale_alpha({k: v for k, v in one.items() if k in ("fill", "grad", "line", "stroke")}, base / captured_op)
        one["base_op"] = base
        one["vis"], one["rel"] = vis, rel
        one["uid"] = f'{one["key"]}~{index}'
        if item["para_order"]:
            paras = []
            for key in item["para_order"]:
                slot = item["paras"][key]
                para = copy.deepcopy(slot["para"])
                p_ops = {s: v for s, v in slot["ops"].items() if s in ops} or slot["ops"]
                p_base, p_vis, p_rel = _timeline(p_ops, n_steps)
                _scale_alpha(para["runs"], p_base / (para.get("op") or 1))
                para["vis"], para["rel"] = p_vis, p_rel
                if paras and para.get("top") is not None and paras[-1].get("bottom") is not None:
                    gap = para["top"] - paras[-1]["bottom"]
                    para["before"] = round(gap, 2) if gap > 0.5 else 0
                paras.append(para)
            one["paras"] = paras
            one["para_anim"] = any(p["vis"] != vis or p["rel"] != rel for p in paras)
            if one["para_anim"]:
                one["rel"] = [1.0 if v else None for v in vis]
        if one["kind"] == "table":
            one["row_vis"] = {r: [s in steps for s in range(n_steps)] for r, steps in item["rows"].items()}
        out.append(one)
    return out


def _area_box(obj):
    if obj["kind"] == "line":
        return None
    if obj["kind"] == "freeform":
        return freeform_bounds(obj)
    r = obj.get("rect")
    return (r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]) if r else None


def _overlaps(a, b):
    box_a, box_b = _area_box(a), _area_box(b)
    if not box_a or not box_b:
        return False
    w = min(box_a[2], box_b[2]) - max(box_a[0], box_b[0])
    h = min(box_a[3], box_b[3]) - max(box_a[1], box_b[1])
    if w <= 0 or h <= 0:
        return False
    area = lambda q: max((q[2] - q[0]) * (q[3] - q[1]), 1)
    return w * h > 0.35 * min(area(box_a), area(box_b)) and w * h > 100


def swap_steps(merged, slide_area):
    """Steps where new content takes the place of a large block that is leaving: these start a new
    slide, because big stacked blocks cannot be edited comfortably. A small swapped card stays on
    its slide, so that charts around it are not duplicated across slides."""
    n_steps = len(merged[0]["vis"]) if merged else 1
    cuts = []
    for step in range(1, n_steps):
        leaving = [o for o in merged if o["vis"][step - 1] and not o["vis"][step]]
        entering = [o for o in merged if o["vis"][step] and not o["vis"][step - 1]]
        swapped = [a for a in leaving if any(_overlaps(a, b) for b in entering)]
        boxes = [_area_box(a) for a in swapped]
        if sum((q[2] - q[0]) * (q[3] - q[1]) for q in boxes) >= 0.10 * slide_area:
            cuts.append(step)
    return cuts


def anim_signature(obj):
    return json.dumps([obj["vis"], obj["rel"], bool(obj.get("para_anim")) and obj["uid"]])

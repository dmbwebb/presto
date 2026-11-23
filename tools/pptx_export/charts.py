"""Turn drawn result plots into real PowerPoint charts with an editable data sheet.

Recognises two plot forms from their drawn marks: dot-and-whisker rows (a marker on a
horizontal line with a numeric label to its right) and horizontal bars with numeric labels.
Marks that reveal together become one chart. Anything else stays as grouped native shapes.
"""

from __future__ import annotations

import io
import re
from statistics import median
from xml.sax.saxutils import escape

import xlsxwriter

from .merge import freeform_bounds, freeform_points

C_NS = ('xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:c15="http://schemas.microsoft.com/office/drawing/2012/chart"')
NUMBER = re.compile(r"[-+−]?\d+(?:[.,]\d+)?")
HEADERS = {
    "fr": {"arm": "Groupe", "est": "Estimation", "lo": "IC bas", "hi": "IC haut", "pos": "Position (ne pas modifier)",
           "minus": "Moins (calculé)", "plus": "Plus (calculé)", "value": "Valeur", "series": "Série", "label": "étiquette"},
    "en": {"arm": "Group", "est": "Estimate", "lo": "CI low", "hi": "CI high", "pos": "Position (do not edit)",
           "minus": "Minus (calculated)", "plus": "Plus (calculated)", "value": "Value", "series": "Series", "label": "label"},
}


def _words(lang):
    return HEADERS["fr" if lang.startswith("fr") else "en"]


def text_of(obj):
    return "".join(r.get("t", "") for p in obj.get("paras", []) for r in p["runs"]).strip()


def number_in(text):
    m = NUMBER.search(text)
    return float(m.group().replace("−", "-").replace(",", ".").replace("+", "")) if m else None


def _center(r):
    return r["x"] + r["w"] / 2, r["y"] + r["h"] / 2


def _run(obj):
    return next(r for p in obj["paras"] for r in p["runs"] if "t" in r)


def _hex(c):
    return c["hex"] if c else None


def _is_marker(o):
    if o["kind"] == "box" and o.get("ellipse") and o.get("fill"):
        return o["rect"]["w"] < 44
    if o["kind"] == "freeform" and o.get("fill"):
        pts = freeform_points(o)
        return len(pts) <= 5 and max(p[0] for p in pts) - min(p[0] for p in pts) < 44
    return False


def _marker_box(o):
    if o["kind"] == "box":
        return o["rect"]
    x0, y0, x1, y1 = freeform_bounds(o)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _numeric_texts(objects):
    return [o for o in objects if o["kind"] == "text" and len(o["paras"]) == 1 and len(text_of(o)) <= 16 and number_in(text_of(o)) is not None]


def _key(o):
    groups = o.get("groups") or []
    return (tuple(o["vis"]), groups[0]["id"] if groups else None)


def _fit(pairs):
    n = len(pairs)
    mean_x, mean_y = sum(p[0] for p in pairs) / n, sum(p[1] for p in pairs) / n
    var = sum((p[0] - mean_x) ** 2 for p in pairs)
    if var < 1:
        return None
    b = sum((p[0] - mean_x) * (p[1] - mean_y) for p in pairs) / var
    return (mean_y - b * mean_x, b) if abs(b) > 1e-9 else None


def _tick_scale(texts, used, left, right, bottom):
    """Value-per-pixel from the numeric tick labels on the nearest axis below the marks."""
    rows = {}
    for t in texts:
        if id(t) in used:
            continue
        cx, cy = _center(t["rect"])
        if bottom - 12 < cy < bottom + 170 and left - 220 < cx < right + 220:
            rows.setdefault(round(cy / 8), []).append(t)
    for key in sorted(rows):
        row = rows[key]
        fit = _fit([(_center(t["rect"])[0], number_in(text_of(t))) for t in row]) if len(row) >= 2 else None
        if fit:
            return fit
    return None


def _legend_names(objects):
    """Arm names by colour, from a legend written as coloured words or marker + word pairs."""
    names = {}
    for o in objects:
        if o["kind"] != "text":
            continue
        for p in o["paras"]:
            for r in p["runs"]:
                if "t" in r and "●" in r["t"] and r.get("c"):
                    word = r["t"].replace("●", "").strip()
                    if word:
                        names.setdefault(r["c"]["hex"], word)
    for m in objects:
        if not _is_marker(m):
            continue
        box = _marker_box(m)
        for t in objects:
            if t["kind"] != "text":
                continue
            r = t["rect"]
            if 0 <= r["x"] - (box["x"] + box["w"]) < 26 and abs(_center(r)[1] - _center(box)[1]) < 12 and number_in(text_of(t)) is None:
                names.setdefault(m["fill"]["hex"], text_of(t))
    return names


def _dot_marks(objects, texts):
    lines = [o for o in objects if o["kind"] == "line" and abs(o["pts"][1] - o["pts"][3]) < 1 and abs(o["pts"][2] - o["pts"][0]) > 6]
    marks = []
    for m in filter(_is_marker, objects):
        box = _marker_box(m)
        cx, cy = _center(box)
        line = next((l for l in lines if _hex(l["stroke"]["c"]) == m["fill"]["hex"] and l["vis"] == m["vis"] and abs(l["pts"][1] - cy) < 2.5
                     and min(l["pts"][0], l["pts"][2]) - 2 <= cx <= max(l["pts"][0], l["pts"][2]) + 2), None)
        if not line:
            continue
        x1, x2 = sorted((line["pts"][0], line["pts"][2]))
        label = next((t for t in texts if t["vis"] == m["vis"] and _hex(_run(t).get("c")) == m["fill"]["hex"]
                      and abs(_center(t["rect"])[1] - cy) < 13 and x2 - 8 <= t["rect"]["x"] <= x2 + 70), None)
        if label:
            marks.append({"marker": m, "line": line, "label": label, "cx": cx, "cy": cy, "x1": x1, "x2": x2, "box": box})
    return marks


def _bar_marks(objects, texts):
    marks = []
    for b in objects:
        if b["kind"] != "box" or b.get("ellipse") or not b.get("fill") or b.get("line") or b["rect"]["h"] > 60 or b["rect"]["h"] < 8:
            continue
        r = b["rect"]
        right, cy = r["x"] + r["w"], r["y"] + r["h"] / 2
        label = next((t for t in texts if t["vis"] == b["vis"] and abs(_center(t["rect"])[1] - cy) < 10 and right - 2 <= t["rect"]["x"] <= right + 40), None)
        if label:
            marks.append({"bar": b, "label": label, "rect": r})
    return marks


def _labels_right(marks):
    return max(m["label"]["rect"]["x"] + m["label"]["rect"]["w"] for m in marks) + 46


def _chart_object(first, kind, spec, rect):
    return {"kind": "chart", "key": first["key"] + ":chart", "uid": first["uid"] + ":chart", "rect": rect, "chart": dict(spec, type=kind),
            "vis": first["vis"], "rel": first["rel"], "groups": [], "op": 1, "base_op": 1}


def detect_charts(objects, lang):
    """Replace recognised plots in a merged object list with chart objects."""
    series_word = _words(lang)["series"]
    texts = _numeric_texts(objects)
    names = _legend_names(objects)
    remove, inserts = set(), {}

    clusters = {}
    for mark in _dot_marks(objects, texts):
        clusters.setdefault(_key(mark["marker"]), []).append(mark)
    label_ids = {id(m["label"]) for ms in clusters.values() for m in ms}
    for marks in clusters.values():
        if len(marks) < 2:
            continue
        left, right = min(m["x1"] for m in marks), _labels_right(marks)
        top, bottom = min(m["box"]["y"] for m in marks) - 10, max(m["box"]["y"] + m["box"]["h"] for m in marks) + 10
        fit = _tick_scale(texts, label_ids, left, right, bottom)
        if not fit:
            fit = _fit([(m["cx"], number_in(text_of(m["label"]))) for m in marks])
        if not fit:
            continue
        a, b = fit
        value = lambda x: a + b * x
        rows = []
        for m in marks:
            shown = number_in(text_of(m["label"]))
            est = shown if abs(shown - value(m["cx"])) < abs(b) * 16 else round(value(m["cx"]), 3)
            run = _run(m["label"])
            rows.append({"arm": names.get(m["marker"]["fill"]["hex"], f"{series_word} {len(rows) + 1}"), "est": est, "lo": round(value(m["x1"]), 3), "hi": round(value(m["x2"]), 3),
                         "label": text_of(m["label"]), "pos": round(1 - (m["cy"] - top) / (bottom - top), 4), "color": m["marker"]["fill"],
                         "size": m["box"]["w"], "width": m["line"]["stroke"]["w"], "font": run, "shape": "circle" if m["marker"]["kind"] == "box" else "triangle"})
        spec = {"rows": rows, "xmin": round(value(left), 4), "xmax": round(value(right), 4), "lang": lang}
        rect = {"x": left, "y": top, "w": right - left, "h": bottom - top}
        inserts[id(marks[0]["marker"])] = _chart_object(marks[0]["marker"], "dot", spec, rect)
        for m in marks:
            remove.update({id(m["marker"]), id(m["line"]), id(m["label"])})

    clusters = {}
    for mark in _bar_marks([o for o in objects if id(o) not in remove], [t for t in texts if id(t) not in remove]):
        clusters.setdefault(_key(mark["bar"]), []).append(mark)
    scales = [number_in(text_of(m["label"])) / m["rect"]["w"] for ms in clusters.values() for m in ms if m["rect"]["w"] > 8 and number_in(text_of(m["label"]))]
    for marks in clusters.values():
        if len(marks) < 2 or not scales or len({round(m["rect"]["x"]) for m in marks}) != 1:
            continue
        marks.sort(key=lambda m: m["rect"]["y"])
        per_px = median(scales)
        left = marks[0]["rect"]["x"]
        right = _labels_right(marks)
        bar_h = marks[0]["rect"]["h"]
        gap = (marks[1]["rect"]["y"] - marks[0]["rect"]["y"]) - bar_h
        top, bottom = marks[0]["rect"]["y"] - gap / 2, marks[-1]["rect"]["y"] + bar_h + gap / 2
        rows = []
        for m in marks:
            shown = number_in(text_of(m["label"]))
            rows.append({"arm": names.get(m["bar"]["fill"]["hex"], f"{series_word} {len(rows) + 1}"), "value": shown, "label": text_of(m["label"]),
                         "color": m["bar"]["fill"], "font": _run(m["label"])})
        spec = {"rows": rows, "xmax": round((right - left) * per_px, 3), "gap": round(gap / bar_h * 100), "lang": lang}
        rect = {"x": left, "y": top, "w": right - left, "h": bottom - top}
        inserts[id(marks[0]["bar"])] = _chart_object(marks[0]["bar"], "bar", spec, rect)
        for m in marks:
            remove.update({id(m["bar"]), id(m["label"])})

    out = []
    for o in objects:
        if id(o) in inserts:
            out.append(inserts[id(o)])
        if id(o) not in remove:
            out.append(o)
    return out


# ---------------------------------------------------------------- chart parts
def font_size_hundredths(size, pt):
    """A font size in hundredths of a point, rounded to the nearest half point."""
    return int(round(size * pt * 2) * 50)


def _fill(c, theme):
    return f"<a:solidFill>{theme.color(c)}</a:solidFill>"


def _tx_pr(run, theme, pt, lang):
    size = font_size_hundredths(run["sz"], pt)
    return (f'<c:txPr><a:bodyPr wrap="none"/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="{size}" b="{int(run["b"])}">{_fill(run["c"], theme)}'
            f'<a:latin typeface="+mn-lt"/></a:defRPr></a:pPr><a:endParaRPr lang="{lang}"/></a:p></c:txPr>')


def _str_ref(cell, text):
    return f'<c:strRef><c:f>Sheet1!{cell}</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>{escape(text)}</c:v></c:pt></c:strCache></c:strRef>'


def _num_ref(cell, value):
    return f'<c:numRef><c:f>Sheet1!{cell}</c:f><c:numCache><c:formatCode>General</c:formatCode><c:ptCount val="1"/><c:pt idx="0"><c:v>{value}</c:v></c:pt></c:numCache></c:numRef>'


LABEL_FLAGS = '<c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/><c:showSerName val="0"/><c:showPercent val="0"/><c:showBubbleSize val="0"/>'
NO_PAINT = "<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>"


def _typed_label(index, text, run, theme, pt, position, lang):
    """A data label holding its own text (estimate plus stars), edited by clicking on it."""
    size = font_size_hundredths(run["sz"], pt)
    props = f'<a:rPr lang="{lang}" sz="{size}" b="{int(run["b"])}">{_fill(run["c"], theme)}<a:latin typeface="+mn-lt"/></a:rPr>'
    return (f'<c:dLbl><c:idx val="{index}"/><c:tx><c:rich><a:bodyPr wrap="none"/><a:lstStyle/><a:p><a:r>{props}<a:t>   {escape(text)}</a:t></a:r></a:p></c:rich></c:tx>'
            f'{NO_PAINT}<c:dLblPos val="{position}"/>{LABEL_FLAGS}</c:dLbl>')


def _typed_labels(rows, theme, pt, position, lang):
    points = "".join(_typed_label(i, row["label"], row["font"], theme, pt, position, lang) for i, row in enumerate(rows))
    return (f'<c:dLbls>{points}{NO_PAINT}{_tx_pr(rows[0]["font"], theme, pt, lang)}'
            f'<c:dLblPos val="{position}"/>{LABEL_FLAGS}</c:dLbls>')


AXIS_COMMON = '<c:delete val="1"/><c:axPos val="{pos}"/><c:numFmt formatCode="General" sourceLinked="0"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>'
LAYOUT = ('<c:layout><c:manualLayout><c:layoutTarget val="inner"/><c:xMode val="edge"/><c:yMode val="edge"/><c:x val="0"/><c:y val="0"/>'
          '<c:w val="1"/><c:h val="1"/></c:manualLayout></c:layout>')


def _wrap(plot, rid):
    return (f'<c:chartSpace {C_NS}><c:date1904 val="0"/><c:roundedCorners val="0"/><c:chart><c:autoTitleDeleted val="1"/><c:plotArea>{LAYOUT}{plot}'
            f'{NO_PAINT}</c:plotArea><c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/></c:chart>{NO_PAINT}'
            f'<c:externalData r:id="{rid}"><c:autoUpdate val="0"/></c:externalData></c:chartSpace>')


def dot_chart_xml(spec, theme, pt, rid):
    series = ""
    index = 0
    for i, row in enumerate(spec["rows"]):
        r = i + 2
        line = f'<a:ln w="{int(row["width"] * 12700 * pt)}" cap="rnd">{_fill(row["color"], theme)}</a:ln>'
        size = max(2, min(72, int(round(row["size"] * pt))))
        series += (f'<c:ser><c:idx val="{index}"/><c:order val="{index}"/><c:tx>{_str_ref(f"$A${r}", row["arm"])}</c:tx><c:spPr><a:ln w="19050"><a:noFill/></a:ln></c:spPr>'
                   f'<c:marker><c:symbol val="{row["shape"]}"/><c:size val="{size}"/><c:spPr>{_fill(row["color"], theme)}<a:ln><a:noFill/></a:ln></c:spPr></c:marker>'
                   f'<c:errBars><c:errDir val="x"/><c:errBarType val="both"/><c:errValType val="cust"/><c:noEndCap val="1"/>'
                   f'<c:plus>{_num_ref(f"$F${r}", round(row["hi"] - row["est"], 4))}</c:plus><c:minus>{_num_ref(f"$E${r}", round(row["est"] - row["lo"], 4))}</c:minus>'
                   f'<c:spPr>{line}</c:spPr></c:errBars><c:xVal>{_num_ref(f"$B${r}", row["est"])}</c:xVal><c:yVal>{_num_ref(f"$G${r}", row["pos"])}</c:yVal><c:smooth val="0"/></c:ser>')
        index += 1
        series += (f'<c:ser><c:idx val="{index}"/><c:order val="{index}"/><c:tx><c:v>{escape(row["arm"])} ({_words(spec["lang"])["label"]})</c:v></c:tx><c:spPr><a:ln w="19050"><a:noFill/></a:ln></c:spPr>'
                   f'<c:marker><c:symbol val="none"/></c:marker>{_typed_labels([row], theme, pt, "r", spec["lang"])}<c:xVal>{_num_ref(f"$D${r}", row["hi"])}</c:xVal>'
                   f'<c:yVal>{_num_ref(f"$G${r}", row["pos"])}</c:yVal><c:smooth val="0"/></c:ser>')
        index += 1
    plot = (f'<c:scatterChart><c:scatterStyle val="lineMarker"/><c:varyColors val="0"/>{series}<c:axId val="50010"/><c:axId val="50020"/></c:scatterChart>'
            f'<c:valAx><c:axId val="50010"/><c:scaling><c:orientation val="minMax"/><c:max val="{spec["xmax"]}"/><c:min val="{spec["xmin"]}"/></c:scaling>{AXIS_COMMON.format(pos="b")}'
            '<c:crossAx val="50020"/><c:crosses val="min"/><c:crossBetween val="midCat"/></c:valAx>'
            f'<c:valAx><c:axId val="50020"/><c:scaling><c:orientation val="minMax"/><c:max val="1"/><c:min val="0"/></c:scaling>{AXIS_COMMON.format(pos="l")}'
            '<c:crossAx val="50010"/><c:crosses val="min"/><c:crossBetween val="midCat"/></c:valAx>')
    return _wrap(plot, rid)


def bar_chart_xml(spec, theme, pt, rid):
    rows = spec["rows"]
    n = len(rows)
    points = "".join(f'<c:dPt><c:idx val="{i}"/><c:invertIfNegative val="0"/><c:bubble3D val="0"/><c:spPr>{_fill(r["color"], theme)}</c:spPr></c:dPt>' for i, r in enumerate(rows))
    cats = "".join(f'<c:pt idx="{i}"><c:v>{escape(r["arm"])}</c:v></c:pt>' for i, r in enumerate(rows))
    vals = "".join(f'<c:pt idx="{i}"><c:v>{r["value"]}</c:v></c:pt>' for i, r in enumerate(rows))
    d_lbls = _typed_labels(rows, theme, pt, "outEnd", spec["lang"])
    ser = (f'<c:ser><c:idx val="0"/><c:order val="0"/><c:tx>{_str_ref("$B$1", _words(spec["lang"])["value"])}</c:tx>'
           f'<c:invertIfNegative val="0"/>{points}{d_lbls}<c:cat><c:strRef><c:f>Sheet1!$A$2:$A${n + 1}</c:f><c:strCache><c:ptCount val="{n}"/>{cats}</c:strCache></c:strRef></c:cat>'
           f'<c:val><c:numRef><c:f>Sheet1!$B$2:$B${n + 1}</c:f><c:numCache><c:formatCode>General</c:formatCode><c:ptCount val="{n}"/>{vals}</c:numCache></c:numRef></c:val></c:ser>')
    plot = (f'<c:barChart><c:barDir val="bar"/><c:grouping val="clustered"/><c:varyColors val="0"/>{ser}<c:gapWidth val="{spec["gap"]}"/><c:axId val="50030"/><c:axId val="50040"/></c:barChart>'
            f'<c:catAx><c:axId val="50030"/><c:scaling><c:orientation val="maxMin"/></c:scaling>{AXIS_COMMON.format(pos="l")}<c:crossAx val="50040"/><c:crosses val="autoZero"/>'
            '<c:auto val="1"/><c:lblAlgn val="ctr"/><c:lblOffset val="100"/><c:noMultiLvlLbl val="0"/></c:catAx>'
            f'<c:valAx><c:axId val="50040"/><c:scaling><c:orientation val="minMax"/><c:max val="{spec["xmax"]}"/><c:min val="0"/></c:scaling>{AXIS_COMMON.format(pos="t")}'
            '<c:crossAx val="50030"/><c:crosses val="autoZero"/><c:crossBetween val="between"/></c:valAx>')
    return _wrap(plot, rid)


def workbook(spec, lang):
    h = _words(lang)
    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = book.add_worksheet("Sheet1")
    bold = book.add_format({"bold": True})
    grey = book.add_format({"font_color": "#888888"})
    if spec["type"] == "dot":
        for c, name in enumerate([h["arm"], h["est"], h["lo"], h["hi"], h["minus"], h["plus"], h["pos"]]):
            sheet.write(0, c, name, bold)
        for i, row in enumerate(spec["rows"]):
            r = i + 1
            sheet.write(r, 0, row["arm"])
            sheet.write_number(r, 1, row["est"])
            sheet.write_number(r, 2, row["lo"])
            sheet.write_number(r, 3, row["hi"])
            sheet.write_formula(r, 4, f"=B{r + 1}-C{r + 1}", grey, round(row["est"] - row["lo"], 4))
            sheet.write_formula(r, 5, f"=D{r + 1}-B{r + 1}", grey, round(row["hi"] - row["est"], 4))
            sheet.write_number(r, 6, row["pos"], grey)
        sheet.set_column(0, 6, 15)
    else:
        for c, name in enumerate([h["arm"], h["value"]]):
            sheet.write(0, c, name, bold)
        for i, row in enumerate(spec["rows"]):
            sheet.write(i + 1, 0, row["arm"])
            sheet.write_number(i + 1, 1, row["value"])
        sheet.set_column(0, 1, 16)
    book.close()
    return buffer.getvalue()

"""Write merged slide objects as native, editable PowerPoint shapes."""

from __future__ import annotations

import colorsys
import io
import re
from collections import Counter
from xml.sax.saxutils import escape, quoteattr

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml
from pptx.util import Emu

from . import charts, fonts
from .merge import anim_signature, freeform_bounds
from .timing import build_timing

SLIDE_W, SLIDE_H = 12192000, 6858000
SYSTEM_FONTS = {"Arial", "Helvetica", "Helvetica Neue", "Georgia", "Times New Roman", "Courier New", "Calibri", "Cambria", "Verdana", "Tahoma",
                "serif", "sans-serif", "monospace", "system-ui", "-apple-system", "KaTeX_Main", "KaTeX_Math"}
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = f'xmlns:a="{A}" xmlns:p="{P}" xmlns:r="{R}"'
Q = {"a": A, "p": P}


def attr(value):
    return quoteattr(str(value))


def _is_single_line(obj):
    return obj.get("lines", 1) <= 1 and len(obj.get("paras", [])) <= 1


def _empty_cell(flags=""):
    return f"<a:tc{flags}><a:txBody><a:bodyPr/><a:lstStyle/><a:p/></a:txBody><a:tcPr/></a:tc>"


def all_runs(slides):
    for slide in slides:
        for obj in slide["objects"]:
            for para in obj.get("paras", []):
                for run in para["runs"]:
                    yield obj, run
            for cell in obj.get("cells", []):
                for para in cell["paras"]:
                    for run in para["runs"]:
                        yield obj, run


class Theme:
    """Deck-wide fonts and colours, so one change in PowerPoint's theme reaches every slide."""

    def __init__(self, slides):
        body, heads, text, paint, bgs = Counter(), Counter(), Counter(), Counter(), Counter()
        for obj, run in all_runs(slides):
            if "t" not in run:
                continue
            (heads if obj.get("role") == "title" else body)[run["f"]] += len(run["t"])
            if run.get("c"):
                text[run["c"]["hex"]] += len(run["t"])
                paint[run["c"]["hex"]] += len(run["t"])
        for slide in slides:
            if slide.get("bg"):
                bgs[slide["bg"]["hex"]] += 1
            for obj in slide["objects"]:
                if obj.get("fill"):
                    paint[obj["fill"]["hex"]] += 40
                stroke = obj.get("stroke") or obj.get("line")
                if stroke and stroke.get("c"):
                    paint[stroke["c"]["hex"]] += 10
        self.minor = (body or heads or Counter({"Calibri": 1})).most_common(1)[0][0]
        self.major = (heads or body or Counter({"Calibri": 1})).most_common(1)[0][0]
        self.lt1 = bgs.most_common(1)[0][0] if bgs else "FFFFFF"
        darks = [c for c, _ in text.most_common() if self._lum(c) < 0.45]
        self.dk1 = darks[0] if darks else "000000"
        accents = [c for c, _ in paint.most_common() if self._sat(c) > 0.25 and c not in (self.dk1, self.lt1)]
        self.accents = (accents + ["4472C4", "ED7D31", "A5A5A5", "FFC000", "5B9BD5", "70AD47"])[:6]
        self.scheme = {self.dk1: "tx1", self.lt1: "bg1"}
        for i, c in enumerate(self.accents, 1):
            self.scheme.setdefault(c, f"accent{i}")

    @staticmethod
    def _rgb(c):
        return [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def _lum(self, c):
        return colorsys.rgb_to_hls(*self._rgb(c))[1]

    def _sat(self, c):
        return colorsys.rgb_to_hsv(*self._rgb(c))[1]

    def color(self, c):
        alpha = f'<a:alpha val="{int(round(c["a"] * 100000))}"/>' if c.get("a", 1) < 0.995 else ""
        scheme = self.scheme.get(c["hex"])
        if scheme:
            return f'<a:schemeClr val="{scheme}">{alpha}</a:schemeClr>'
        return f'<a:srgbClr val="{c["hex"]}">{alpha}</a:srgbClr>'

    def fill(self, c):
        return f"<a:solidFill>{self.color(c)}</a:solidFill>" if c else "<a:noFill/>"

    def apply(self, prs):
        part = prs.slide_master.part.part_related_by(RT.THEME)
        root = etree.fromstring(part.blob)
        scheme = root.find(".//a:clrScheme", Q)
        scheme.set("name", "Deck colours")
        values = {"dk1": self.dk1, "lt1": self.lt1, "dk2": self.dk1, "lt2": self.lt1}
        values.update({f"accent{i}": c for i, c in enumerate(self.accents, 1)})
        for name, hex_value in values.items():
            node = scheme.find(f"a:{name}", Q)
            for child in list(node):
                node.remove(child)
            etree.SubElement(node, f"{{{A}}}srgbClr").set("val", hex_value)
        font_scheme = root.find(".//a:fontScheme", Q)
        font_scheme.set("name", "Deck fonts")
        font_scheme.find("a:majorFont/a:latin", Q).set("typeface", self.major)
        font_scheme.find("a:minorFont/a:latin", Q).set("typeface", self.minor)
        part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


class Node:
    def __init__(self, obj=None, children=None, name=""):
        self.obj, self.children, self.name = obj, children, name

    @property
    def lead(self):
        return self.obj if self.obj is not None else self.children[0].lead

    def leaves(self):
        if self.obj is not None:
            return [self]
        return [leaf for child in self.children for leaf in child.leaves()]


def _ungroupable(obj):
    return obj["kind"] in ("table", "chart") or obj.get("role") in ("title", "sldNum", "ftr") or obj.get("para_anim")


def _cluster(nodes, name):
    buckets = {}
    for node in nodes:
        key = anim_signature(node.lead) if not _ungroupable(node.lead) else id(node)
        buckets.setdefault(key, []).append(node)
    out = []
    for members in buckets.values():
        if len(members) == 1:
            out.append(members[0])
        else:
            out.append(Node(children=[leaf for m in members for leaf in m.leaves()], name=name))
    return out


def arrange(items, depth=0):
    """Group related objects, but only those that appear and disappear together:
    PowerPoint cannot animate the members of a group separately."""
    nodes, i = [], 0
    while i < len(items):
        obj = items[i]
        groups = obj.get("groups") or []
        if len(groups) <= depth or _ungroupable(obj):
            nodes.append(Node(obj=obj))
            i += 1
            continue
        gid = groups[depth]["id"]
        j = i
        while j < len(items):
            g = items[j].get("groups") or []
            if len(g) <= depth or g[depth]["id"] != gid or _ungroupable(items[j]):
                break
            j += 1
        bucket = items[i:j]
        name = groups[depth]["name"]
        if len(bucket) == 1:
            nodes.append(Node(obj=obj))
        elif len({anim_signature(o) for o in bucket}) == 1:
            nodes.append(Node(children=[Node(obj=o) for o in bucket], name=name))
        else:
            nodes.extend(_cluster(arrange(bucket, depth + 1), name))
        i = j
    return nodes


class SlideWriter:
    def __init__(self, deck, slide, spec, index):
        self.deck, self.slide, self.spec, self.index = deck, slide, spec, index
        self.E = deck.emu
        self.pt = deck.pt
        self.next_id = 1
        self.targets = []
        self.theme = deck.theme

    def sid(self):
        self.next_id += 1
        return self.next_id

    def emu(self, value):
        return int(round(value * self.E))

    # ------------------------------------------------------------ links
    def link_xml(self, href):
        if not href:
            return ""
        target = self.deck.resolve_link(href)
        if target == "back":
            return '<a:hlinkClick r:id="" action="ppaction://hlinkshowjump?jump=lastslideviewed"/>'
        if isinstance(target, int):
            rid = self.slide.part.relate_to(self.deck.pptx_slides[target].part, RT.SLIDE)
            return f'<a:hlinkClick r:id="{rid}" action="ppaction://hlinksldjump"/>'
        if isinstance(target, str):
            rid = self.slide.part.relate_to(target, RT.HYPERLINK, is_external=True)
            return f'<a:hlinkClick r:id="{rid}"/>'
        return ""

    # ------------------------------------------------------------ text
    def rpr(self, run):
        size = charts.font_size_hundredths(run["sz"], self.pt)
        attrs = [f'lang="{self.deck.lang}"', f'sz="{max(size, 100)}"', f'b="{int(run["b"])}"', f'i="{int(run["i"])}"']
        if run.get("u"):
            attrs.append('u="sng"')
        if run.get("s"):
            attrs.append('strike="sngStrike"')
        if run.get("base"):
            attrs.append(f'baseline="{run["base"]}"')
        if run.get("spc"):
            attrs.append(f'spc="{int(round(run["spc"] * self.pt * 100))}"')
        attrs.append('dirty="0"')
        inner = self.theme.fill(run["c"]) if run.get("c") else ""
        if run.get("hl"):
            inner += f'<a:highlight><a:srgbClr val="{run["hl"]["hex"]}"/></a:highlight>'
        font = run["f"]
        if font == self.theme.major and font != self.theme.minor:
            inner += '<a:latin typeface="+mj-lt"/>'
        elif font != self.theme.minor:
            inner += f"<a:latin typeface={attr(font)}/><a:cs typeface={attr(font)}/>"
        inner += self.link_xml(run.get("link"))
        return f'<a:rPr {" ".join(attrs)}>{inner}</a:rPr>'

    def para_xml(self, para, field=False):
        attrs = [f'algn="{para.get("align", "l")}"']
        bullet = para.get("bullet")
        if para.get("level"):
            attrs.append(f'lvl="{min(para["level"], 8)}"')
        if "marL" in para:
            attrs.append(f'marL="{self.emu(para["marL"])}"')
            attrs.append(f'indent="{self.emu(para.get("indent", 0))}"')
        inner = ""
        if para.get("ln"):
            inner += f'<a:lnSpc><a:spcPct val="{int(max(70, min(200, para["ln"] * 100)) * 1000)}"/></a:lnSpc>'
        inner += f'<a:spcBef><a:spcPts val="{int(round(para.get("before", 0) * self.pt * 100))}"/></a:spcBef>'
        inner += '<a:spcAft><a:spcPts val="0"/></a:spcAft>'
        if bullet:
            if bullet.get("c"):
                inner += f'<a:buClr>{self.theme.color({**bullet["c"], "a": 1})}</a:buClr>'
            if bullet.get("auto"):
                inner += f'<a:buFont typeface="+mj-lt"/><a:buAutoNum type="{bullet["auto"]}"/>'
            else:
                inner += f'<a:buFont typeface="Arial"/><a:buChar char={attr(bullet["ch"])}/>'
        else:
            inner += "<a:buNone/>"
        body = ""
        last = None
        for run in para["runs"]:
            if run.get("br"):
                body += f'<a:br>{self.rpr(last) if last else ""}</a:br>'
                continue
            last = run
            if field:
                body += (f'<a:fld id="{{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}}" type="slidenum">{self.rpr(run)}'
                         f"<a:t>{self.index + 1}</a:t></a:fld>")
            else:
                body += f'<a:r>{self.rpr(run)}<a:t>{escape(run["t"])}</a:t></a:r>'
        return f'<a:p><a:pPr {" ".join(attrs)}>{inner}</a:pPr>{body}</a:p>'

    def body_pr(self, wrap, ins=None, anchor="t", autofit=True):
        i = ins or {"l": 0, "r": 0, "t": 0, "b": 0}
        fit = "<a:spAutoFit/>" if autofit else "<a:noAutofit/>"
        return (f'<a:bodyPr wrap="{wrap}" lIns="{self.emu(i["l"])}" rIns="{self.emu(i["r"])}" tIns="{self.emu(i["t"])}" '
                f'bIns="{self.emu(i["b"])}" anchor="{anchor}" rtlCol="0">{fit}</a:bodyPr>')

    def name_of(self, obj):
        for para in obj.get("paras", []):
            text = "".join(r.get("t", "") for r in para["runs"]).strip()
            if text:
                return text[:40]
        return {"line": "Line", "box": "Shape", "freeform": "Shape", "image": obj.get("alt") or "Picture",
                "snapshot": "Picture", "table": "Table"}.get(obj["kind"], "Shape")

    def nv(self, obj, spid, name=None):
        return f"<p:cNvPr id=\"{spid}\" name={attr(name or self.name_of(obj))}>{self.link_xml(obj.get('link'))}</p:cNvPr>"

    def xfrm(self, r, rot=0, tag="a:xfrm", extra=""):
        rot_attr = f' rot="{int(round(rot * 60000)) % 21600000}"' if rot else ""
        return (f'<{tag}{rot_attr}{extra}><a:off x="{self.emu(r["x"])}" y="{self.emu(r["y"])}"/>'
                f'<a:ext cx="{max(self.emu(r["w"]), 0)}" cy="{max(self.emu(r["h"]), 0)}"/></{tag}>')

    def line_xml(self, stroke):
        if not stroke or not stroke.get("c"):
            return "<a:ln><a:noFill/></a:ln>"
        cap = {"round": ' cap="rnd"', "square": ' cap="sq"'}.get(stroke.get("cap"), "")
        dash = ""
        if stroke.get("dash"):
            a = stroke["dash"][0]
            w = max(stroke.get("w", 1), 0.1)
            if a <= w * 1.5:
                preset = "sysDot"
            elif a <= w * 3.5:
                preset = "sysDash"
            else:
                preset = "dash"
            dash = f'<a:prstDash val="{preset}"/>'
        return f'<a:ln w="{self.emu(stroke.get("w", 1))}"{cap}>{self.theme.fill(stroke["c"])}{dash}</a:ln>'

    def geometry(self, obj):
        r = obj["rect"]
        if obj.get("ellipse"):
            return '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'
        radius = obj.get("radius") or 0
        side = min(r["w"], r["h"]) or 1
        if radius > 0.5:
            adj = int(min(50000, radius / side * 100000))
            return f'<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val {adj}"/></a:avLst></a:prstGeom>'
        return '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'

    def text_rect(self, obj):
        r = dict(obj["rect"])
        align = (obj["paras"][0].get("align") if obj.get("paras") else "l") or "l"
        wrap = "none" if obj.get("nowrap") or (_is_single_line(obj) and r["w"] < (obj.get("tw") or r["w"]) * 1.08) else "square"
        if wrap == "square":
            grow = r["w"] * 0.02 + 2
            if align == "ctr":
                r["x"] -= grow / 2
            elif align == "r":
                r["x"] -= grow
            r["w"] += grow
        return r, wrap

    def text_shape(self, obj, spid):
        role = obj.get("role")
        paras = "".join(self.para_xml(p, field=role == "sldNum") for p in obj["paras"])
        if role == "title":
            r = dict(obj["rect"])
            r["w"] = max(r["w"], self.deck.design_w - r["x"] - 40)
            sp_pr = f"<p:spPr>{self.xfrm(r)}</p:spPr>"
            nv = f'<p:nvSpPr>{self.nv(obj, spid, "Title")}<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
            return f'<p:sp>{nv}{sp_pr}<p:txBody>{self.body_pr("square", autofit=False)}<a:lstStyle/>{paras}</p:txBody></p:sp>'
        r, wrap = self.text_rect(obj)
        rot = obj.get("rot") or 0
        ph = {"sldNum": '<p:ph type="sldNum" sz="quarter" idx="12"/>', "ftr": '<p:ph type="ftr" sz="quarter" idx="11"/>'}.get(role, "")
        locks = '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>' if ph else '<p:cNvSpPr txBox="1"/>'
        name = {"sldNum": "Slide number", "ftr": "Footer"}.get(role)
        nv = f"<p:nvSpPr>{self.nv(obj, spid, name)}{locks}<p:nvPr>{ph}</p:nvPr></p:nvSpPr>"
        sp_pr = f'<p:spPr>{self.xfrm(r, rot)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        return f'<p:sp>{nv}{sp_pr}<p:txBody>{self.body_pr(wrap)}<a:lstStyle/>{paras}</p:txBody></p:sp>'

    def paint(self, obj):
        grad = obj.get("grad")
        if not grad:
            return self.theme.fill(obj.get("fill"))
        stops = "".join(f'<a:gs pos="{int(min(100, s["pos"]) * 1000)}">{self.theme.color(s["c"])}</a:gs>' for s in grad["stops"])
        angle = int(((grad["angle"] - 90) % 360) * 60000)
        return f'<a:gradFill rotWithShape="1"><a:gsLst>{stops}</a:gsLst><a:lin ang="{angle}" scaled="0"/></a:gradFill>'

    def box_shape(self, obj, spid):
        r = obj["rect"]
        fill = self.paint(obj)
        line = self.line_xml(obj.get("line"))
        sp_pr = f"<p:spPr>{self.xfrm(r)}{self.geometry(obj)}{fill}{line}</p:spPr>"
        nv = f"<p:nvSpPr>{self.nv(obj, spid)}<p:cNvSpPr/><p:nvPr/></p:nvSpPr>"
        if obj["kind"] == "textbox":
            paras = "".join(self.para_xml(p) for p in obj["paras"])
            wrap = "none" if obj.get("nowrap") or _is_single_line(obj) else "square"
            body = self.body_pr(wrap, obj.get("ins"), obj.get("anchor", "t"), autofit=False)
        else:
            paras = f'<a:p><a:endParaRPr lang="{self.deck.lang}"/></a:p>'
            body = '<a:bodyPr rtlCol="0" anchor="ctr"/>'
        return f"<p:sp>{nv}{sp_pr}<p:txBody>{body}<a:lstStyle/>{paras}</p:txBody></p:sp>"

    def connector(self, obj, spid):
        x1, y1, x2, y2 = obj["pts"]
        r = {"x": min(x1, x2), "y": min(y1, y2), "w": abs(x2 - x1), "h": abs(y2 - y1)}
        flip = (' flipH="1"' if x2 < x1 else "") + (' flipV="1"' if y2 < y1 else "")
        sp_pr = f'<p:spPr>{self.xfrm(r, extra=flip)}<a:prstGeom prst="line"><a:avLst/></a:prstGeom>{self.line_xml(obj["stroke"])}</p:spPr>'
        return f"<p:cxnSp><p:nvCxnSpPr>{self.nv(obj, spid)}<p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr>{sp_pr}</p:cxnSp>"

    def freeform(self, obj, spid):
        x0, y0, x1, y1 = freeform_bounds(obj)
        w, h = max(x1 - x0, 0.01), max(y1 - y0, 0.01)
        pt = lambda x, y: f'<a:pt x="{self.emu(x - x0)}" y="{self.emu(y - y0)}"/>'
        path = ""
        for cmd in obj["cmds"]:
            if cmd[0] == "M":
                path += f"<a:moveTo>{pt(cmd[1], cmd[2])}</a:moveTo>"
            elif cmd[0] == "L":
                path += f"<a:lnTo>{pt(cmd[1], cmd[2])}</a:lnTo>"
            elif cmd[0] == "C":
                path += f"<a:cubicBezTo>{pt(cmd[1], cmd[2])}{pt(cmd[3], cmd[4])}{pt(cmd[5], cmd[6])}</a:cubicBezTo>"
            else:
                path += "<a:close/>"
        no_fill = ' fill="none"' if not obj.get("fill") else ""
        geom = (f'<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/><a:pathLst>'
                f'<a:path w="{self.emu(w)}" h="{self.emu(h)}"{no_fill}>{path}</a:path></a:pathLst></a:custGeom>')
        rect = {"x": x0, "y": y0, "w": w, "h": h}
        sp_pr = f'<p:spPr>{self.xfrm(rect)}{geom}{self.theme.fill(obj.get("fill"))}{self.line_xml(obj.get("stroke"))}</p:spPr>'
        return (f'<p:sp><p:nvSpPr>{self.nv(obj, spid)}<p:cNvSpPr/><p:nvPr/></p:nvSpPr>{sp_pr}<p:txBody><a:bodyPr rtlCol="0"/>'
                f'<a:lstStyle/><a:p><a:endParaRPr lang="{self.deck.lang}"/></a:p></p:txBody></p:sp>')

    def picture(self, obj, spid):
        source = obj.get("src") or obj.get("url")
        data = obj.get("png") or self.deck.fetch_image(source)
        if data is None:
            self.deck.warn(self.index, f"image not found: {source}")
            return self.box_shape({**obj, "kind": "box", "fill": {"hex": "DDDDDD", "a": 1}}, spid)
        _, rid = self.slide.part.get_or_add_image_part(io.BytesIO(data))
        crop = obj.get("crop")
        src = ""
        if crop:
            src = "".join(f' {k}="{int(crop[k] * 100000)}"' for k in "ltrb")
            src = f"<a:srcRect{src}/>"
        alpha = f'<a:alphaModFix amt="{int(obj.get("base_op", 1) * 100000)}"/>' if obj.get("base_op", 1) < 0.99 else ""
        descr = f" descr={attr(obj['alt'])}" if obj.get("alt") else ""
        name = obj.get("alt") or "Picture"
        return (f'<p:pic><p:nvPicPr><p:cNvPr id="{spid}" name={attr(name[:40])}{descr}>{self.link_xml(obj.get("link"))}</p:cNvPr>'
                f'<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
                f'<p:blipFill><a:blip r:embed="{rid}">{alpha}</a:blip>{src}<a:stretch><a:fillRect/></a:stretch></p:blipFill>'
                f'<p:spPr>{self.xfrm(obj["rect"])}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')

    # ------------------------------------------------------------ tables
    def cell_border(self, tag, side):
        if side and side.get("w", 0) > 0 and side.get("c"):
            return (f'<a:{tag} w="{self.emu(side["w"])}" cap="flat" cmpd="sng" algn="ctr">'
                    f'{self.theme.fill(side["c"])}<a:prstDash val="solid"/></a:{tag}>')
        return f'<a:{tag} w="0"><a:noFill/></a:{tag}>'

    def table_part(self, obj, rows, spid):
        cols = obj["cols"]
        heights = obj["rows"]
        by_pos = {(c["r"], c["c"]): c for c in obj["cells"]}
        covered = {}
        for c in obj["cells"]:
            for a in range(c["rspan"]):
                for b in range(c["cspan"]):
                    if a or b:
                        covered[(c["r"] + a, c["c"] + b)] = (a > 0, b > 0)
        grid = "".join(f'<a:gridCol w="{self.emu(w)}"/>' for w in cols)
        body = ""
        for r in rows:
            tr = ""
            for col in range(len(cols)):
                if (r, col) in covered:
                    v, h = covered[(r, col)]
                    flags = (' vMerge="1"' if v else "") + (' hMerge="1"' if h else "")
                    tr += _empty_cell(flags)
                    continue
                cell = by_pos.get((r, col))
                if cell is None:
                    tr += _empty_cell()
                    continue
                span = (f' gridSpan="{cell["cspan"]}"' if cell["cspan"] > 1 else "") + (f' rowSpan="{cell["rspan"]}"' if cell["rspan"] > 1 else "")
                paras = "".join(self.para_xml(p) for p in cell["paras"]) or f'<a:p><a:endParaRPr lang="{self.deck.lang}" sz="1000"/></a:p>'
                pad = cell["pad"]
                b = cell["borders"]
                # Slim side margins: PowerPoint breaks words that only just fit the browser's column.
                pr = (f'<a:tcPr marL="{self.emu(pad[0] * 0.25)}" marR="{self.emu(pad[1] * 0.25)}" marT="{self.emu(min(pad[2], 2))}" marB="{self.emu(min(pad[3], 2))}" anchor="{cell["valign"]}">'
                      f'{self.cell_border("lnL", b["l"])}{self.cell_border("lnR", b["r"])}{self.cell_border("lnT", b["t"])}{self.cell_border("lnB", b["b"])}'
                      f'{self.theme.fill(cell.get("fill"))}</a:tcPr>')
                tr += f"<a:tc{span}><a:txBody><a:bodyPr/><a:lstStyle/>{paras}</a:txBody>{pr}</a:tc>"
            body += f'<a:tr h="{self.emu(heights[r])}">{tr}</a:tr>'
        rect = dict(obj["rect"])
        rect["y"] += sum(heights[:rows[0]])
        rect["h"] = sum(heights[r] for r in rows)
        return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{spid}" name="Table"/><p:cNvGraphicFramePr>'
                f'<a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>{self.xfrm(rect, tag="p:xfrm")}'
                f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tblPr/>'
                f"<a:tblGrid>{grid}</a:tblGrid>{body}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>")

    def table_shapes(self, obj):
        """One native table, or one per run of rows that are revealed together."""
        n = len(obj["rows"])
        always = [True] * len(obj["vis"])
        vectors = [tuple(obj.get("row_vis", {}).get(r, always)) for r in range(n)]
        parts, start = [], 0
        for r in range(1, n + 1):
            if r == n or vectors[r] != vectors[start]:
                parts.append((list(range(start, r)), vectors[start]))
                start = r
        xml = ""
        for rows, vector in parts:
            spid = self.sid()
            xml += self.table_part(obj, rows, spid)
            vis = [a and b for a, b in zip(obj["vis"], vector)]
            self.targets.append({"spid": spid, "para": None, "vis": vis, "rel": [1.0 if v else None for v in vis], "text": False})
        return xml

    # ------------------------------------------------------------ assembly
    def chart_frame(self, obj, spid):
        """A real chart: right-click > Edit Data opens its numbers in a sheet."""
        spec = obj["chart"]
        r = obj["rect"]
        placeholder = CategoryChartData()
        placeholder.categories = ["a"]
        placeholder.add_series("s", (1,))
        frame = self.slide.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, self.emu(r["x"]), self.emu(r["y"]), self.emu(r["w"]), self.emu(r["h"]), placeholder)
        part = frame.chart.part
        rid = part._element.xpath("c:externalData/@r:id")[0]
        build = charts.dot_chart_xml if spec["type"] == "dot" else charts.bar_chart_xml
        part._element = parse_xml(build(spec, self.theme, self.pt, rid))
        part.chart_workbook.xlsx_part.blob = charts.workbook(spec, self.deck.lang)
        element = frame._element
        element.getparent().remove(element)
        pr = element.xpath("./p:nvGraphicFramePr/p:cNvPr")[0]
        pr.set("id", str(spid))
        pr.set("name", "Graphique" if self.deck.lang.startswith("fr") else "Chart")
        return etree.tostring(element, encoding="unicode")

    @staticmethod
    def lift_link(obj):
        """A fully linked text (a button) links the whole shape, which keeps its own colours."""
        runs = [r for p in obj.get("paras", []) for r in p["runs"] if "t" in r]
        links = {r.get("link") for r in runs}
        if runs and len(links) == 1 and None not in links:
            obj["link"] = links.pop()
            for run in runs:
                run.pop("link", None)

    def leaf(self, obj, register=True):
        self.lift_link(obj)
        kind = obj["kind"]
        if kind == "table":
            return self.table_shapes(obj)
        spid = self.sid()
        if kind == "chart":
            self.targets.append({"spid": spid, "para": None, "vis": obj["vis"], "rel": obj["rel"], "text": True, "mode": "chart"})
            return self.chart_frame(obj, spid)
        if kind == "text":
            xml = self.text_shape(obj, spid)
        elif kind in ("box", "textbox"):
            if obj.get("url"):
                xml = self.picture(obj, spid)
            else:
                xml = self.box_shape(obj, spid)
        elif kind == "line":
            xml = self.connector(obj, spid)
        elif kind == "freeform":
            xml = self.freeform(obj, spid)
        else:
            xml = self.picture(obj, spid)
        if register:
            is_text = kind in ("text", "textbox")
            if obj.get("para_anim"):
                for i, para in enumerate(obj["paras"]):
                    self.targets.append({"spid": spid, "para": i, "vis": para["vis"], "rel": para["rel"], "text": True})
            else:
                self.targets.append({"spid": spid, "para": None, "vis": obj["vis"], "rel": obj["rel"], "text": is_text})
        return xml

    def bounds(self, obj):
        if obj["kind"] == "line":
            x1, y1, x2, y2 = obj["pts"]
            return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
        if obj["kind"] == "freeform":
            return freeform_bounds(obj)
        r = self.text_rect(obj)[0] if obj["kind"] == "text" else obj["rect"]
        return r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"]

    def group(self, node):
        spid = self.sid()
        leaves = [leaf.obj for leaf in node.leaves()]
        boxes = [self.bounds(o) for o in leaves]
        r = {"x": min(b[0] for b in boxes), "y": min(b[1] for b in boxes)}
        r["w"], r["h"] = max(b[2] for b in boxes) - r["x"], max(b[3] for b in boxes) - r["y"]
        lead = leaves[0]
        first = lead["vis"].index(True) if True in lead["vis"] else 0
        label = (node.name or "Group").strip() or "Group"
        name = f"Step {first}: {label}" if first else label
        inner = "".join(self.leaf(o, register=False) for o in leaves)
        ext = f'<a:off x="{self.emu(r["x"])}" y="{self.emu(r["y"])}"/><a:ext cx="{self.emu(r["w"])}" cy="{self.emu(r["h"])}"/>'
        ch = f'<a:chOff x="{self.emu(r["x"])}" y="{self.emu(r["y"])}"/><a:chExt cx="{self.emu(r["w"])}" cy="{self.emu(r["h"])}"/>'
        self.targets.append({"spid": spid, "para": None, "vis": lead["vis"], "rel": lead["rel"], "text": False})
        return (f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="{spid}" name={attr(name[:60])}/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                f"<p:grpSpPr><a:xfrm>{ext}{ch}</a:xfrm></p:grpSpPr>{inner}</p:grpSp>")

    def write(self):
        tree = self.slide.shapes._spTree
        for child in list(tree)[2:]:
            tree.remove(child)
        xml = "".join(self.group(n) if n.children else self.leaf(n.obj) for n in arrange(self.spec["objects"]))
        fragment = parse_xml(f"<p:spTree {NS}>{xml}</p:spTree>")
        for child in list(fragment):
            tree.append(child)
        if self.spec.get("bg") and self.spec["bg"]["hex"] != self.theme.lt1:
            bg = parse_xml(f'<p:bg {NS}><p:bgPr>{self.theme.fill(self.spec["bg"])}<a:effectLst/></p:bgPr></p:bg>')
            self.slide._element.find("p:cSld", Q).insert(0, bg)
        timing = build_timing(self.targets)
        if timing:
            self.slide._element.append(parse_xml(timing))
        if self.spec.get("notes"):
            self.slide.notes_slide.notes_text_frame.text = self.spec["notes"]


class DeckWriter:
    def __init__(self, data, fetch_image):
        self.data = data
        self.design_w = data["design_w"]
        self.emu = SLIDE_W / self.design_w
        self.pt = 960 / self.design_w
        self.lang = data.get("lang") or "en-GB"
        self.fetch_image = fetch_image
        self.warnings = []
        self.theme = Theme(data["slides"])
        self.pptx_slides = []
        self.anchors = {}
        for i, slide in enumerate(data["slides"]):
            for anchor in [slide.get("id")] + list(slide.get("inner_ids") or []):
                if anchor:
                    self.anchors.setdefault(anchor, i)

    def warn(self, index, message):
        self.warnings.append(f"slide {index + 1}: {message}")

    def resolve_link(self, href):
        if href in ("#", "back", "javascript:history.back()"):
            return "back"
        m = re.search(r"[#?&]slide[-=](\d+)", href)
        first_of = self.data["first_of"]
        if m and 1 <= int(m.group(1)) <= len(first_of):
            return first_of[int(m.group(1)) - 1]
        if "#" in href and href.split("#", 1)[1] in self.anchors:
            return self.anchors[href.split("#", 1)[1]]
        return href if href.startswith(("http://", "https://", "mailto:")) else None

    def tune_master(self, prs):
        master = prs.slide_master
        titles = [o for s in self.data["slides"] for o in s["objects"] if o.get("role") == "title"]
        size, color = 3200, None
        rect = {"x": 60, "y": 40, "w": self.design_w - 120, "h": 90}
        if titles:
            sizes = Counter(r["sz"] for o in titles for p in o["paras"] for r in p["runs"] if "t" in r)
            size = charts.font_size_hundredths(sizes.most_common(1)[0][0], self.pt)
            color = titles[0]["paras"][0]["runs"][0].get("c")
            xs = Counter(round(o["rect"]["x"]) for o in titles).most_common(1)[0][0]
            ys = Counter(round(o["rect"]["y"]) for o in titles).most_common(1)[0][0]
            rect = {"x": xs, "y": ys, "w": self.design_w - 2 * xs, "h": max(o["rect"]["h"] for o in titles)}
        def place(xfrm, r):
            offset, extent = xfrm.find("a:off", Q), xfrm.find("a:ext", Q)
            offset.set("x", str(int(r["x"] * self.emu)))
            offset.set("y", str(int(r["y"] * self.emu)))
            extent.set("cx", str(int(r["w"] * self.emu)))
            extent.set("cy", str(int(r["h"] * self.emu)))

        h = self.data["design_h"]
        boxes = {"title": rect, "body": {"x": rect["x"], "y": rect["y"] + rect["h"] + 20, "w": rect["w"], "h": h - rect["y"] - rect["h"] - 90},
                 "dt": {"x": rect["x"], "y": h - 50, "w": 200, "h": 34}, "ftr": {"x": self.design_w / 2 - 200, "y": h - 50, "w": 400, "h": 34},
                 "sldNum": {"x": self.design_w - rect["x"] - 200, "y": h - 50, "w": 200, "h": 34}}
        for sp in master._element.findall(".//p:sp", Q):
            ph = sp.find(".//p:ph", Q)
            xf = sp.find("p:spPr/a:xfrm", Q)
            if ph is not None and xf is not None and ph.get("type") in boxes:
                place(xf, boxes[ph.get("type")])
        style = master._element.find("p:txStyles/p:titleStyle/a:lvl1pPr", Q)
        style.set("algn", "l")
        rpr = style.find("a:defRPr", Q)
        rpr.set("sz", str(size))
        rpr.set("b", "1")
        if color:
            for child in rpr.findall("a:solidFill", Q):
                rpr.remove(child)
            rpr.insert(0, parse_xml(f'<a:solidFill {NS}>{self.theme.color({**color, "a": 1})}</a:solidFill>'))

    def build(self, output):
        prs = Presentation()
        prs.slide_width, prs.slide_height = Emu(SLIDE_W), Emu(SLIDE_H)
        layouts = {l.name: l for l in prs.slide_layouts}
        for layout in list(prs.slide_layouts):
            if layout.name not in ("Title Only", "Blank"):
                prs.slide_layouts.remove(layout)
        self.theme.apply(prs)
        self.tune_master(prs)
        specs = self.data["slides"]
        for spec in specs:
            has_title = any(o.get("role") == "title" for o in spec["objects"])
            self.pptx_slides.append(prs.slides.add_slide(layouts["Title Only" if has_title else "Blank"]))
        for i, spec in enumerate(specs):
            SlideWriter(self, self.pptx_slides[i], spec, i).write()
        prs.core_properties.title = self.data.get("title") or ""
        prs.core_properties.author = ""
        used = sorted({run["f"] for _, run in all_runs(self.data["slides"]) if "t" in run}, key=lambda f: (f != self.theme.minor, f != self.theme.major, f))
        fonts.embed(prs, [f for f in used if f not in SYSTEM_FONTS], self.warnings.append)
        prs.save(output)
        return self.warnings

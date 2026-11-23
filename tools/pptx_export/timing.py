"""Native PowerPoint click animations (p:timing) from per-step reveal timelines.

Every WebSlides step is one click. Effects used: Appear (entrance), Disappear
(exit) and Transparency (emphasis), the plainest ones in the Animation Pane.
"""

from __future__ import annotations

from itertools import count
from xml.sax.saxutils import quoteattr

NS = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
ACTION_ORDER = {"out": 0, "op": 1, "in": 2}


def _target(spid, para):
    if para is None:
        return f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl>'
    return f'<p:tgtEl><p:spTgt spid="{spid}"><p:txEl><p:pRg st="{para}" end="{para}"/></p:txEl></p:spTgt></p:tgtEl>'


def _effect(ids, action, node_type):
    kind, spid, para, value, grp = action
    target = _target(spid, para)
    grp_attr = f' grpId="{grp}"' if grp is not None else ""
    if kind in ("in", "out"):
        preset, state = ("entr", "visible") if kind == "in" else ("exit", "hidden")
        outer, inner = next(ids), next(ids)
        return (f'<p:par><p:cTn id="{outer}" presetID="1" presetClass="{preset}" presetSubtype="0" fill="hold"{grp_attr} '
                f'nodeType="{node_type}"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
                f'<p:set><p:cBhvr><p:cTn id="{inner}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>'
                f'{target}<p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst></p:cBhvr>'
                f'<p:to><p:strVal val="{state}"/></p:to></p:set></p:childTnLst></p:cTn></p:par>')
    outer, first, second = next(ids), next(ids), next(ids)
    return (f'<p:par><p:cTn id="{outer}" presetID="9" presetClass="emph" presetSubtype="0" fill="hold"{grp_attr} '
            f'nodeType="{node_type}"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
            f'<p:set><p:cBhvr><p:cTn id="{first}" dur="indefinite"/>{target}<p:attrNameLst><p:attrName>style.opacity</p:attrName>'
            f'</p:attrNameLst></p:cBhvr><p:to><p:strVal val="{value:g}"/></p:to></p:set>'
            f'<p:animEffect filter="image" prLst={quoteattr(f"opacity: {value:g}")}><p:cBhvr rctx="IE">'
            f'<p:cTn id="{second}" dur="indefinite"/>{target}</p:cBhvr></p:animEffect></p:childTnLst></p:cTn></p:par>')


def transitions(vis, rel):
    """(step, kind, value) for each change between consecutive reveal steps."""
    changes = []
    last = rel[0] if vis[0] else None
    for step in range(1, len(vis)):
        if vis[step] and not vis[step - 1]:
            changes.append((step, "in", None))
        elif vis[step - 1] and not vis[step]:
            changes.append((step, "out", None))
        if vis[step]:
            if last is not None and rel[step] is not None and abs(rel[step] - last) > 0.03:
                changes.append((step, "op", rel[step]))
            last = rel[step]
    return changes


def _build_item(spid, grp, mode):
    if mode == "chart":
        return f'<p:bldGraphic spid="{spid}" grpId="{grp}"><p:bldAsOne/></p:bldGraphic>'
    build = ' build="p"' if mode == "p" else ""
    return f'<p:bldP spid="{spid}" grpId="{grp}"{build}/>'


def build_timing(targets):
    """targets: dicts with spid, para (index or None), vis, rel, text (True for text shapes)."""
    by_step = {}
    groups = {}
    builds = {}
    for t in targets:
        for step, kind, value in transitions(t["vis"], t["rel"]):
            grp = None
            if t["text"]:
                grp = groups.setdefault((t["spid"], kind), len({k for k in groups if k[0] == t["spid"]}))
                builds[(t["spid"], grp)] = "p" if t["para"] is not None else t.get("mode", "whole")
            by_step.setdefault(step, []).append((kind, t["spid"], t["para"], value, grp))
    if not by_step:
        return ""
    ids = count(1)
    root, seq = next(ids), next(ids)
    clicks = []
    for step in sorted(by_step):
        actions = sorted(by_step[step], key=lambda a: ACTION_ORDER[a[0]])
        click, inner = next(ids), next(ids)
        effects = "".join(_effect(ids, a, "clickEffect" if i == 0 else "withEffect") for i, a in enumerate(actions))
        clicks.append(f'<p:par><p:cTn id="{click}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst><p:childTnLst>'
                      f'<p:par><p:cTn id="{inner}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>'
                      f'{effects}</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>')
    bld = ""
    if builds:
        items = "".join(_build_item(spid, grp, mode) for (spid, grp), mode in sorted(builds.items()))
        bld = f"<p:bldLst>{items}</p:bldLst>"
    return (f'<p:timing {NS}><p:tnLst><p:par><p:cTn id="{root}" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>'
            f'<p:seq concurrent="1" nextAc="seek"><p:cTn id="{seq}" dur="indefinite" nodeType="mainSeq"><p:childTnLst>'
            f'{"".join(clicks)}</p:childTnLst></p:cTn><p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond>'
            f'</p:prevCondLst><p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst></p:seq>'
            f'</p:childTnLst></p:cTn></p:par></p:tnLst>{bld}</p:timing>')

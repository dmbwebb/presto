"""Embed the deck's fonts so that collaborators see and can type in them without installing anything.

Fonts come from the user's font folders or, failing that, the open-licensed Google Fonts
repository (cached in ~/.cache/webslides-fonts). Each is cut to Latin characters and stored in the
Embedded OpenType form PowerPoint reads. A font that cannot be found is reported, not fatal.
"""

from __future__ import annotations

import io
import struct
import urllib.parse
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from pptx.opc.package import Part
from pptx.opc.packuri import PackURI
from pptx.oxml import parse_xml

CACHE = Path.home() / ".cache" / "webslides-fonts"
FONT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/font"
P_NS = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REPO = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
LATIN = list(range(0x20, 0x250)) + list(range(0x2000, 0x2070)) + [0x20AC, 0x2122, 0x2190, 0x2192, 0x2212, 0x2248, 0x2260, 0x2264, 0x2265, 0x25CF]
VARIABLE_AXES = {"Nunito Sans": "[YTLC,opsz,wdth,wght]", "Domine": "[wght]", "Inter": "[opsz,wght]", "Source Sans 3": "[wght]"}


def _style(bold):
    return "Bold" if bold else "Regular"


def _postscript_name(family, bold):
    return f"{family.replace(' ', '')}-{_style(bold)}"


def _local_static(family, bold):
    wanted = _postscript_name(family, bold)
    for folder in (Path.home() / "Library/Fonts", Path("/Library/Fonts")):
        for suffix in (".ttf", ".otf"):
            path = folder / (wanted + suffix)
            if path.exists():
                return path.read_bytes()
    return None


def _download(url):
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / urllib.parse.unquote(url.rsplit("/", 1)[1])
    if not target.exists():
        with urllib.request.urlopen(url, timeout=40) as response:
            target.write_bytes(response.read())
    return target.read_bytes()


def _source(family, bold):
    local = _local_static(family, bold)
    if local:
        return local
    compact = family.replace(" ", "")
    folder = compact.lower() + "/"
    axes = VARIABLE_AXES.get(family, "[wght]")
    for url in (REPO + folder + urllib.parse.quote(f"{compact}{axes}.ttf"), REPO + folder + f"{_postscript_name(family, bold)}.ttf"):
        try:
            return _download(url)
        except OSError:
            continue
    return None


def _static(data, family, bold):
    font = TTFont(io.BytesIO(data))
    if "fvar" in font:
        limits = {a.axisTag: a.defaultValue for a in font["fvar"].axes}
        limits["wght"] = 700 if bold else 400
        font = instancer.instantiateVariableFont(font, limits)
    options = subset.Options(layout_features=["*"], name_IDs=["*"], notdef_outline=True, glyph_names=False, hinting=False)
    cutter = subset.Subsetter(options)
    cutter.populate(unicodes=LATIN)
    cutter.subset(font)
    style = _style(bold)
    names = font["name"]
    for platform, encoding, language in ((3, 1, 0x409), (1, 0, 0)):
        names.setName(family, 1, platform, encoding, language)
        names.setName(style, 2, platform, encoding, language)
        names.setName(f"{family} {style}".strip(), 4, platform, encoding, language)
        names.setName(_postscript_name(family, bold), 6, platform, encoding, language)
    os2 = font["OS/2"]
    os2.fsType = 0
    os2.usWeightClass = 700 if bold else 400
    os2.fsSelection = (os2.fsSelection & ~0b1100001) | (0b100000 if bold else 0b1000000)
    font["head"].macStyle = 1 if bold else 0
    buffer = io.BytesIO()
    font.save(buffer)
    return buffer.getvalue()


def to_eot(ttf):
    """Wrap a TrueType font as uncompressed Embedded OpenType."""
    font = TTFont(io.BytesIO(ttf))
    os2, head, names = font["OS/2"], font["head"], font["name"]
    text = lambda n: (names.getDebugName(n) or "").encode("utf-16-le")
    panose = os2.panose
    panose_bytes = bytes(getattr(panose, f) for f in ("bFamilyType", "bSerifStyle", "bWeight", "bProportion", "bContrast", "bStrokeVariation",
                                                      "bArmStyle", "bLetterForm", "bMidline", "bXHeight"))
    head_part = struct.pack("<II", 0x00020002, 0) + panose_bytes + struct.pack("<BBIHH", 1, 0, os2.usWeightClass, os2.fsType, 0x504C)
    head_part += struct.pack("<6I", os2.ulUnicodeRange1, os2.ulUnicodeRange2, os2.ulUnicodeRange3, os2.ulUnicodeRange4,
                             getattr(os2, "ulCodePageRange1", 1), getattr(os2, "ulCodePageRange2", 0))
    head_part += struct.pack("<I", head.checkSumAdjustment) + b"\0" * 16 + b"\0\0"
    for name_id in (1, 2, 5, 4):
        value = text(name_id)
        head_part += struct.pack("<H", len(value)) + value + b"\0\0"
    head_part += struct.pack("<H", 0)                      # RootStringSize
    head_part += struct.pack("<II", 0, 0) + b"\0\0" + struct.pack("<H", 0) + struct.pack("<II", 0, 0)
    total = 8 + len(head_part) + len(ttf)
    return struct.pack("<II", total, len(ttf)) + head_part + ttf


def embed(prs, families, warn):
    """Add regular and bold faces of each family to the presentation."""
    entries, number = "", 0
    for family in families:
        faces = ""
        for bold, tag in ((False, "regular"), (True, "bold")):
            try:
                data = _source(family, bold)
                if data is None:
                    raise OSError("font file not found")
                blob = to_eot(_static(data, family, bold))
            except Exception as exc:  # the deck still works with a substitute font
                warn(f"font {family} ({tag}) was not embedded: {exc}")
                continue
            number += 1
            part = Part(PackURI(f"/ppt/fonts/font{number}.fntdata"), "application/x-fontdata", prs.part.package, blob)
            faces += f'<p:{tag} r:id="{prs.part.relate_to(part, FONT_REL)}"/>'
        if faces:
            entries += f'<p:embeddedFont><p:font typeface="{family}"/>{faces}</p:embeddedFont>'
    if not entries:
        return
    root = prs.part._element
    root.set("embedTrueTypeFonts", "1")
    root.set("saveSubsetFonts", "1")
    notes = root.find("{http://schemas.openxmlformats.org/presentationml/2006/main}notesSz")
    notes.addnext(parse_xml(f"<p:embeddedFontLst {P_NS}>{entries}</p:embeddedFontLst>"))

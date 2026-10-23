# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""CDFX -- ASAM Calibration Data Format (CDF 2.x, XML), read and written.

``MSRSW / SW-SYSTEMS / SW-SYSTEM / SW-INSTANCE-SPEC / SW-INSTANCE-TREE``
holds one ``SW-INSTANCE`` per parameter: ``SHORT-NAME``, ``CATEGORY``
(``VALUE``, ``BOOLEAN``, ``ASCII``, ``VAL_BLK``, ``CURVE``, ``MAP``,
``COM_AXIS``, ``CURVE_AXIS``, ``RES_AXIS``, ``CUBOID``), the values in
``SW-VALUE-CONT / SW-VALUES-PHYS`` (``V`` numbers, ``VT`` texts, ``VG``
groups) and the axes in ``SW-AXIS-CONTS`` -- one ``SW-AXIS-CONT`` per axis,
X first, each with its own values or an ``SW-INSTANCE-REF`` to a shared one.

A map's values come in one ``VG`` per Y point (its ``LABEL`` is that
point), each holding that row's values across X (:data:`MAP_GROUPS`).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List, Optional
from xml.sax.saxutils import escape

from .calvalues import Param, ValueFileError, ValueSet, ident

#: which axis a map's outer VG groups run along. Measured, not assumed: the
#: SIMOS 22 SC4800 CDFX files against the S19 images of the same datasets
#: through SC4800_A_OEM.a2l -- all 16 maps of 49AEfF agree with "y", 9 of
#: them disagree with "x".
MAP_GROUPS = "y"

_CAT = {"VALUE": "value", "DEPENDENT_VALUE": "value", "BOOLEAN": "value",
        "ASCII": "text", "VAL_BLK": "block", "CURVE": "curve", "MAP": "map",
        "COM_AXIS": "axis", "CURVE_AXIS": "axis", "RES_AXIS": "axis",
        "CUBOID": "cuboid", "CUBE_4": "cuboid", "CUBE_5": "cuboid"}
_AXCAT = {"STD_AXIS": "std", "FIX_AXIS": "fix", "COM_AXIS": "com",
          "CURVE_AXIS": "com", "RES_AXIS": "com"}


def is_cdfx(data: bytes) -> bool:
    head = bytes(data[:4096])
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    head = head.lstrip()
    if not head.startswith(b"<"):
        return False
    return b"<MSRSW" in head and (b"CDF" in head or b"SW-SYSTEMS" in head)


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _txt(el: Optional[ET.Element], path: str) -> str:
    if el is None:
        return ""
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""


def _val(el: ET.Element):
    t = (el.text or "").strip()
    if el.tag == "VT":
        return el.text or ""
    try:
        return float(t)
    except ValueError:
        return t


def _flat(phys: Optional[ET.Element]) -> list:
    """Every V/VT under ``phys``, in document order (groups flattened)."""
    if phys is None:
        return []
    return [_val(e) for e in phys.iter() if e.tag in ("V", "VT")]


def _groups(phys: Optional[ET.Element]):
    """``[(label, [values])]`` for the VG children of ``phys``."""
    out = []
    if phys is None:
        return out
    for vg in phys.findall("VG"):
        lab = vg.find("LABEL")
        label = None
        if lab is not None and lab.text is not None:
            label = _val(ET.Element("V", {}))  # placeholder
            t = lab.text.strip()
            try:
                label = float(t)
            except ValueError:
                label = t
        vals = [_val(e) for e in vg.iter() if e.tag in ("V", "VT")]
        out.append((label, vals))
    return out


def loads(data: bytes) -> ValueSet:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueFileError("not readable XML: %s" % exc)
    _strip_ns(root)
    if root.tag != "MSRSW":
        raise ValueFileError("not a CDFX file (root %s)" % root.tag)
    vs = ValueSet(title=_txt(root, "SHORT-NAME"))
    org = root.find(".//SW-INSTANCE-TREE-ORIGIN")
    if org is not None:
        if _txt(org, "SYMBOLIC-FILE"):
            vs.origin["a2l"] = _txt(org, "SYMBOLIC-FILE")
        if _txt(org, "DATA-FILE"):
            vs.origin["image"] = _txt(org, "DATA-FILE")
    for col in root.iter("SW-CS-COLLECTION"):
        ref = _txt(col, "SW-FEATURE-REF")
        if ref:
            vs.functions.append((ref, _txt(col, "REVISION"), ""))
    for inst in root.iter("SW-INSTANCE"):
        p = _instance(inst)
        if p is not None:
            vs.params.append(p)
    # a curve or map on a shared axis names it; its points are that axis'
    axes = {p.name: p for p in vs.params if p.kind == "axis"}
    for p in vs.params:
        for tag in ("x", "y"):
            ref = getattr(p, tag + "_ref")
            if ref and not getattr(p, tag) and ref in axes:
                setattr(p, tag, list(axes[ref].z))
    return vs


def _instance(inst: ET.Element) -> Optional[Param]:
    name = _txt(inst, "SHORT-NAME")
    cat = _txt(inst, "CATEGORY").upper()
    kind = _CAT.get(cat)
    if not name:
        return None
    p = Param(name=name, kind=kind or "value",
              description=_txt(inst, "LONG-NAME"),
              display=_txt(inst, "DISPLAY-NAME"),
              function=_txt(inst, "SW-FEATURE-REF"))
    if kind is None:
        p.notes.append("category %s not read" % cat)
    cont = inst.find("SW-VALUE-CONT")
    p.unit = (cont.findtext("UNIT-DISPLAY-NAME") or "") if cont is not None else ""
    phys = cont.find("SW-VALUES-PHYS") if cont is not None else None
    size = [int(float(_val(v))) for v in cont.findall("SW-ARRAYSIZE/V")] \
        if cont is not None else []
    axes = []
    for ac in inst.findall("SW-AXIS-CONTS/SW-AXIS-CONT"):
        acat = _txt(ac, "CATEGORY").upper()
        aphys = ac.find("SW-VALUES-PHYS")
        axes.append({"kind": _AXCAT.get(acat, "std"),
                     "ref": _txt(ac, "SW-INSTANCE-REF"),
                     "unit": ac.findtext("UNIT-DISPLAY-NAME") or "",
                     "vals": _flat(aphys) if aphys is not None else None})
    if p.kind == "text":
        vals = _flat(phys)
        p.z = [str(vals[0]) if vals else ""]
        return p
    if p.kind == "axis":
        p.z = _flat(phys)
        p.nx = len(p.z)
        return p
    if p.kind == "value":
        p.z = _flat(phys)[:1]
        return p
    if p.kind == "block":
        p.z = _flat(phys)
        if len(size) >= 2:
            p.nx, p.ny = size[0], size[1]
        else:
            p.nx, p.ny = (size[0] if size else len(p.z)), 1
        if p.nx * p.ny != len(p.z):
            p.notes.append("%d values, %s declared" % (len(p.z), "x".join(map(str, size))))
        return p
    if p.kind == "cuboid":
        p.z = _flat(phys)
        p.nx = len(p.z)
        return p
    ax = axes[0] if axes else {"kind": "", "ref": "", "unit": "", "vals": None}
    p.x_kind, p.x_ref, p.unit_x = ax["kind"], ax["ref"], ax["unit"]
    if p.kind == "curve":
        p.z = _flat(phys)
        p.nx = len(p.z)
        p.x = ax["vals"] if ax["vals"] is not None else None
        return p
    ay = axes[1] if len(axes) > 1 else {"kind": "", "ref": "", "unit": "", "vals": None}
    p.y_kind, p.y_ref, p.unit_y = ay["kind"], ay["ref"], ay["unit"]
    groups = _groups(phys)
    if not groups:
        p.notes.append("map values not in groups -- not read")
        p.nx = p.ny = 0
        return p
    outer = [g[1] for g in groups]
    labels = [g[0] for g in groups]
    if MAP_GROUPS == "x":
        p.nx, p.ny = len(outer), len(outer[0])
        p.z = [outer[x][y] if y < len(outer[x]) else 0.0
               for y in range(p.ny) for x in range(p.nx)]
        p.x = ax["vals"] if ax["vals"] is not None else (
            labels if all(v is not None for v in labels) else None)
        p.y = ay["vals"]
    else:
        p.ny, p.nx = len(outer), len(outer[0])
        p.z = [v for row in outer for v in row]
        p.y = ay["vals"] if ay["vals"] is not None else (
            labels if all(v is not None for v in labels) else None)
        p.x = ax["vals"]
    if any(len(g) != len(outer[0]) for g in outer):
        p.notes.append("map rows of different lengths")
    return p


# ------------------------------------------------------------------ writing
def _num(v) -> str:
    from .fmt_dcm import _fmt
    return _fmt(v)


def _v(v) -> str:
    if isinstance(v, str):
        return '<VT xml:space="preserve">%s</VT>' % escape(v)
    return "<V>%s</V>" % _num(v)


def _axis(out: List[str], kind: str, ref: str, unit: str, vals) -> None:
    cat = {"std": "STD_AXIS", "fix": "FIX_AXIS", "com": "COM_AXIS"}.get(kind, "STD_AXIS")
    out.append("<SW-AXIS-CONT>")
    out.append("<CATEGORY>%s</CATEGORY>" % cat)
    out.append('<UNIT-DISPLAY-NAME xml:space="preserve">%s</UNIT-DISPLAY-NAME>' % escape(unit or ""))
    if cat == "COM_AXIS" and ref:
        out.append("<SW-INSTANCE-REF>%s</SW-INSTANCE-REF>" % escape(ident(ref)))
    else:
        out.append("<SW-VALUES-PHYS>")
        out.extend(_v(v) for v in vals or [])
        out.append("</SW-VALUES-PHYS>")
    out.append("</SW-AXIS-CONT>")


def dumps(vs: ValueSet, source: str = "") -> bytes:
    from .. import branding
    title = vs.title or "values"
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<!DOCTYPE MSRSW PUBLIC "-//ASAM//DTD CALIBRATION DATA FORMAT:V2.0.0:LAI:IAI:XML:'
           'CDF200.XSD//EN" "cdf_v2.0.0.sl.dtd">',
           '<MSRSW CREATOR="%s" CREATOR-VERSION="%s">' % (
               escape(branding.APP_NAME), escape(branding.__version__)),
           "<SHORT-NAME>%s</SHORT-NAME>" % escape(re.sub(r"\s+", "_", title)),
           "<CATEGORY>CDF20</CATEGORY>",
           "<SW-SYSTEMS>", "<SW-SYSTEM>",
           "<SHORT-NAME>%s</SHORT-NAME>" % escape(re.sub(r"\s+", "_", title)),
           "<SW-INSTANCE-SPEC>", "<SW-INSTANCE-TREE>",
           "<SHORT-NAME>%s</SHORT-NAME>" % escape(re.sub(r"\s+", "_", title)),
           "<CATEGORY>NO_VCD</CATEGORY>"]
    a2l = vs.origin.get("a2l") or source
    if a2l or vs.origin.get("image"):
        out.append("<SW-INSTANCE-TREE-ORIGIN>")
        if a2l:
            out.append("<SYMBOLIC-FILE>%s</SYMBOLIC-FILE>" % escape(a2l))
        if vs.origin.get("image"):
            out.append("<DATA-FILE>%s</DATA-FILE>" % escape(vs.origin["image"]))
        out.append("</SW-INSTANCE-TREE-ORIGIN>")
    for p in vs.params:
        cat = {"value": "VALUE", "text": "ASCII", "block": "VAL_BLK", "curve": "CURVE",
               "map": "MAP", "axis": "COM_AXIS", "cuboid": "CUBOID"}[p.kind]
        if p.kind == "value" and p.z and isinstance(p.z[0], str):
            cat = "VALUE"
        out.append("<SW-INSTANCE>")
        short = p.name if re.match(r"^[A-Za-z_][A-Za-z0-9_.\[\]]*$", p.name) else ident(p.name)
        out.append("<SHORT-NAME>%s</SHORT-NAME>" % escape(short))
        if p.description:
            out.append("<LONG-NAME>%s</LONG-NAME>" % escape(p.description))
        display = p.display or (p.name if short != p.name else "")
        if display:
            out.append("<DISPLAY-NAME>%s</DISPLAY-NAME>" % escape(display))
        out.append("<CATEGORY>%s</CATEGORY>" % cat)
        if p.function:
            out.append("<SW-FEATURE-REF>%s</SW-FEATURE-REF>" % escape(p.function))
        out.append("<SW-VALUE-CONT>")
        out.append('<UNIT-DISPLAY-NAME xml:space="preserve">%s</UNIT-DISPLAY-NAME>'
                   % escape(p.unit or ""))
        if p.kind in ("block", "axis"):
            out.append("<SW-ARRAYSIZE>")
            out.append("<V>%d</V>" % p.nx)
            if p.kind == "block" and p.ny > 1:
                out.append("<V>%d</V>" % p.ny)
            out.append("</SW-ARRAYSIZE>")
        out.append("<SW-VALUES-PHYS>")
        if p.kind == "map":
            xs, ys = p.x or [], p.y or []
            if MAP_GROUPS == "x":
                for x in range(p.nx):
                    out.append("<VG>")
                    if x < len(xs):
                        out.append("<LABEL>%s</LABEL>" % escape(
                            xs[x] if isinstance(xs[x], str) else _num(xs[x])))
                    out.extend(_v(p.z[y * p.nx + x]) for y in range(p.ny))
                    out.append("</VG>")
            else:
                for y in range(p.ny):
                    out.append("<VG>")
                    if y < len(ys):
                        out.append("<LABEL>%s</LABEL>" % escape(
                            ys[y] if isinstance(ys[y], str) else _num(ys[y])))
                    out.extend(_v(v) for v in p.z[y * p.nx:(y + 1) * p.nx])
                    out.append("</VG>")
        elif p.kind == "block" and p.ny > 1:
            for y in range(p.ny):
                out.append("<VG>")
                out.extend(_v(v) for v in p.z[y * p.nx:(y + 1) * p.nx])
                out.append("</VG>")
        elif p.kind == "text":
            out.append(_v(str(p.z[0]) if p.z else ""))
        else:
            out.extend(_v(v) for v in (p.z[:1] if p.kind == "value" else p.z))
        out.append("</SW-VALUES-PHYS>")
        out.append("</SW-VALUE-CONT>")
        if p.kind in ("curve", "map"):
            out.append("<SW-AXIS-CONTS>")
            _axis(out, p.x_kind, p.x_ref, p.unit_x, p.x)
            if p.kind == "map":
                _axis(out, p.y_kind, p.y_ref, p.unit_y, p.y)
            out.append("</SW-AXIS-CONTS>")
        out.append("</SW-INSTANCE>")
    out += ["</SW-INSTANCE-TREE>", "</SW-INSTANCE-SPEC>", "</SW-SYSTEM>",
            "</SW-SYSTEMS>", "</MSRSW>"]
    return ("\n".join(out) + "\n").encode("utf-8")

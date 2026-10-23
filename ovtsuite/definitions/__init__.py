# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Third-party calibration definitions, read in the base app.

``load(data)`` turns a TunerPro XDF, an ASAP2 file (A2L), a WinOLS project
(OLS) or map pack (KP), or a Bosch DAMOS file (DAM) into one
:class:`~.model.Definition`; :mod:`ovtsuite.xdf_session` gives a session its
parameters from it. The type is decided by the file's own structure, never
by its name.

No module imports and no Qt here: this is part of the base app.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Optional

from .model import (AxisDef, Definition, DefinitionError, Storage, TableDef,
                    linear_equation)

#: definition types the base app reads (file extension = type)
TYPES = ("xdf", "a2l", "ols", "kp", "dam")
#: file-dialog filter for every definition type
FILTER = ("Definitions (*.xdf *.a2l *.kp *.ols *.dam);;TunerPro XDF (*.xdf);;"
          "ASAP2 (*.a2l);;WinOLS map pack (*.kp);;WinOLS project (*.ols);;"
          "DAMOS (*.dam);;All files (*)")
#: types that are definitions only (an OLS also carries its binary)
DEFINITION_ONLY = ("xdf", "a2l", "kp", "dam")


def detect(data: bytes) -> Optional[str]:
    """The definition type of ``data`` from its structure, or None."""
    from . import a2l, dam, winols
    head = bytes(data[:4096])
    if winols.is_winols(head):
        try:
            return winols.parse(data).kind
        except DefinitionError:
            return None
    text = head.decode("latin-1", "replace")
    if "<XDFFORMAT" in text or "<XDFHEADER" in text:
        return "xdf"
    if a2l.is_a2l(data):
        return "a2l"
    if dam.is_dam(data):
        return "dam"
    return None


def from_xdf(xdef) -> Definition:
    """A parsed TunerPro XDF (:mod:`ovtsuite.xdf`) as a Definition."""
    defn = Definition(type="xdf", title=xdef.title or "XDF",
                      base=-int(xdef.base_offset or 0))
    if getattr(xdef, "region_size", 0):
        defn.info["image_size"] = str(xdef.region_size)
    if getattr(xdef, "description", ""):
        defn.info["description"] = xdef.description
    defn.checksums = [dict(c) for c in getattr(xdef, "checksums", [])]
    defn.patches = [dict(p, entries=[dict(e) for e in p["entries"]])
                    for p in getattr(xdef, "patches", [])]

    def fixed(ax, n: int) -> Optional[AxisDef]:
        """An axis the XDF gives as LABELs (no address): its points, or
        its texts when they are not numbers -- only when there is one label
        per row / column (a lone 0.0 is a writer's placeholder)."""
        labels = [str(v) for v in (ax.labels or [])]
        if n < 2 or len(labels) != n:
            return None
        try:
            vals = [float(v) for v in labels]
        except ValueError:
            return AxisDef(len(labels), labels=labels,
                           values=Storage(addr=None, units=ax.units))
        return AxisDef(len(vals), fixed=vals,
                       values=Storage(addr=None, units=ax.units))

    def axis(ax, real, n) -> Optional[AxisDef]:
        if ax is None:
            return None
        if ax.addr is None:
            return fixed(ax, n)
        if not real:
            return None
        st = Storage(addr=ax.addr, bits=ax.bits, equation=ax.equation or "X",
                     units=ax.units, endian="",
                     float=bool(getattr(ax, "is_float", False)))
        st.signed = None                # as before: decided from the values
        return AxisDef(ax.count, values=st, labels=list(ax.labels) or None)

    for t in xdef.tables:
        order = "<" if t.z_lsb_first else ">"
        flt = bool(getattr(t, "z_float", False))
        z = Storage(addr=t.z_addr, bits=t.z_bits, signed=t.z_signed and not flt,
                    float=flt, endian=order if t.z_bits > 8 else "",
                    equation=t.z_equation or "X", units=t.z_units)
        kind = {"map": "map", "curve": "curve"}.get(t.kind, "curve")
        x = axis(t.x, t._real(t.x), t.cols)
        y = axis(t.y, t._real(t.y), t.rows) if kind == "map" else None
        for a in (x, y):
            if a is not None and a.values is not None and a.values.bits > 8:
                a.values.endian = order
        if t.kind == "indexed" and t.rows * t.cols == 1:
            kind = "constant"
        # column-major: the 0x04 flag, or (as this app writes it) a major
        # stride of one element
        colmajor = t.z_colmajor or (
            getattr(t, "z_major_stride", 0) == t.z_bits
            and t.rows > 1 and t.cols > 1)
        defn.tables.append(TableDef(
            name=t.name or "table @0x%X" % t.z_addr, kind=kind, z=z,
            rows=max(1, t.rows), cols=max(1, t.cols),
            description=t.description, categories=list(t.categories),
            order="col" if colmajor else "row", x=x, y=y,
            decimals=getattr(t, "z_decimals", None)))
    for c in xdef.constants:
        flt = bool(getattr(c, "is_float", False))
        defn.tables.append(TableDef(
            name=c.name or "value @0x%X" % c.addr, kind="constant",
            z=Storage(addr=c.addr, bits=c.bits, signed=c.signed and not flt,
                      float=flt,
                      endian=("<" if c.lsb_first else ">") if c.bits > 8 else "",
                      equation=c.equation or "X", units=c.units),
            description=c.description, categories=list(c.categories),
            decimals=getattr(c, "decimals", None)))
    return defn


_CACHE: "OrderedDict[tuple, Definition]" = OrderedDict()
_LOCK = threading.Lock()


def load(data: bytes, type_: Optional[str] = None, title: str = "") -> Definition:
    """The definition in ``data`` (``type_`` = its file extension, or None to
    tell from its structure). Parsed once per content and kept."""
    data = bytes(data)
    t = (type_ or "").lower().lstrip(".") or detect(data) or ""
    if t in ("ols", "kp"):
        t = "winols"
    key = (hashlib.sha1(data).hexdigest(), t)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    if t == "xdf":
        from ..xdf import parse_xdf_bytes
        defn = from_xdf(parse_xdf_bytes(data))
    elif t == "a2l":
        from . import a2l
        defn = a2l.load(data, title)
        defn.base_for = lambda size, _d=defn: a2l.resolve_base(_d, size)
    elif t == "winols":
        from . import winols
        defn = winols.load(data, title)
    elif t == "dam":
        from . import dam
        defn = dam.load(data, title)
    else:
        raise DefinitionError("not a definition this version reads "
                              "(XDF, A2L, KP, OLS or DAM)")
    with _LOCK:
        _CACHE[key] = defn
        while len(_CACHE) > 4:
            _CACHE.popitem(last=False)
    return defn


__all__ = ["TYPES", "FILTER", "DEFINITION_ONLY", "detect", "load", "from_xdf",
           "Definition", "TableDef", "AxisDef", "Storage", "DefinitionError",
           "linear_equation"]

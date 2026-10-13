# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""TunerPro XDF 1.80 parsing, full fidelity for WinOLS- and A2L-derived files.

Produces plain dataclasses so the knowledge-base builder, the detector and
(later) the GUI all share one representation.

Element kinds:
  ``XdfTable``   -- an <XDFTABLE>. May be a *map* (two embedded axes), a
                    *curve* (one embedded axis) or an *indexed table* (no
                    embedded axis; the axis is a list of labels).
  ``XdfConstant``-- an <XDFCONSTANT> scalar.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET

from .equation import Equation


@dataclass
class XdfAxis:
    addr: Optional[int] = None      # None => label axis (not stored in binary)
    count: int = 1
    bits: int = 16
    signed: bool = False
    equation: str = "X"
    units: str = ""
    labels: List[str] = field(default_factory=list)


@dataclass
class XdfTable:
    name: str
    description: str = ""
    categories: List[str] = field(default_factory=list)
    x: Optional[XdfAxis] = None     # column axis
    y: Optional[XdfAxis] = None     # row axis
    rows: int = 1
    cols: int = 1
    z_addr: int = 0
    z_bits: int = 16
    z_signed: bool = False
    z_equation: str = "X"
    z_units: str = ""
    z_min: Optional[float] = None
    z_max: Optional[float] = None
    #: EMBEDDEDDATA type flags of the Z data: 0x02 LSB first (little-endian),
    #: 0x04 column-major (each column stored contiguously)
    z_lsb_first: bool = False
    z_colmajor: bool = False

    def _real(self, ax: Optional[XdfAxis]) -> bool:
        """Is ``ax`` a stored axis, or a placeholder?

        An axis STORED AT THE Z ADDRESS is a placeholder some XDFs carry, not a breakpoint list -- it cannot be a stored axis,
        because it would overlap the data it indexes. Counting one as real
        turns the table into a "map", which then takes the map header offset
        (first axis - 4) instead of the curve one (axis - 2), so its structure
        address is wrong, its fingerprint is cut from the wrong bytes, and it
        never anchors.

        Two shapes of it, which is why the test is the address alone:

        * a 1-D curve, written as a 1xN table whose second axis is one element
          at the Z address -- 888 tables across the p447 and p447_906016
          references;
        * a label-indexed array, written with BOTH axes at the Z address and a
          count above one -- 96 float arrays in the ZF 6HP26A reference, which
          an additional "count <= 1" condition would have let through as maps.
        """
        return (ax is not None and ax.addr is not None
                and ax.addr != self.z_addr)

    @property
    def kind(self) -> str:
        """'map' | 'curve' | 'indexed'."""
        ex, ey = self._real(self.x), self._real(self.y)
        if ex and ey:
            return "map"
        if ex or ey:
            return "curve"
        return "indexed"

    @property
    def input_axis(self) -> Optional[XdfAxis]:
        """The single embedded axis of a curve."""
        if self._real(self.x):
            return self.x
        if self._real(self.y):
            return self.y
        return None

    @property
    def struct_addr(self) -> Optional[int]:
        """Address of the storage structure's count header, if any.

        Curves: ``[u16 N][axis][data]`` -> input-axis address minus 2.
        Maps:   ``[u16 Nx][u16 Ny][X][Y][Z]`` -> first axis address minus 4.
        """
        k = self.kind
        if k == "curve":
            return self.input_axis.addr - 2
        if k == "map":
            first = min(a.addr for a in (self.x, self.y) if self._real(a))
            return first - 4
        return None


def _as_row(tab: XdfTable) -> None:
    """Normalise a 1-D table written as a COLUMN into the row form.

    TunerPro will render a curve either way: ``1xN`` with the breakpoints on
    the x axis, or ``Nx1`` with them on y. Both describe the same bytes at the
    same address, and which one a converter emits is a display choice. The
    storage structure -- ``[u16 N][N axis values][N data values]`` -- does not
    care either, which is why ``kind`` and ``struct_addr`` were always right
    for both.

    ``rows`` and ``cols`` were not. A column-form curve arrived as ``rows=N,
    cols=1``, so its declared span, its fingerprint length and every
    dimension check downstream used 1 where they needed N. It is not a ZF
    quirk: 398, 418, 431 and 548 tables in four of the EDC16 references are in
    this form, and the ZF definitions regenerated in September 2026 put every
    curve in it. Normalising here means the rest of the codebase sees one
    representation instead of guarding for two.

    Deliberately narrow: only when the y axis is a real stored axis, the x
    axis is not, and the Z block really is a single column. A table with two
    real axes is a map and is left exactly as it is.
    """
    if (tab.cols == 1 and tab.rows > 1
            and tab._real(tab.y) and not tab._real(tab.x)):
        tab.x, tab.y = tab.y, tab.x
        tab.rows, tab.cols = tab.cols, tab.rows


@dataclass
class XdfConstant:
    name: str
    description: str = ""
    categories: List[str] = field(default_factory=list)
    addr: int = 0
    bits: int = 16
    signed: bool = False
    equation: str = "X"
    units: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    lsb_first: bool = False


@dataclass
class XdfDef:
    title: str = ""
    description: str = ""
    tables: List[XdfTable] = field(default_factory=list)
    constants: List[XdfConstant] = field(default_factory=list)
    categories: Dict[int, str] = field(default_factory=dict)
    #: XDFHEADER/BASEOFFSET: added to (or, with subtract, taken from) every
    #: address in the file -- 0 for the definitions this project generates
    base_offset: int = 0


def _hexint(s, default=None):
    if s is None:
        return default
    return int(s, 16)


def _axis_of(el) -> XdfAxis:
    ax = XdfAxis()
    emb = el.find("EMBEDDEDDATA")
    if emb is not None:
        ax.addr = _hexint(emb.get("mmedaddress"))
        ax.bits = int(emb.get("mmedelementsizebits", "16"))
        flags = _hexint(emb.get("mmedtypeflags"), 0)
        ax.signed = bool(flags & 0x01)
    ic = el.find("indexcount")
    if ic is not None and ic.text:
        ax.count = int(ic.text)
    m = el.find("MATH")
    if m is not None:
        ax.equation = m.get("equation", "X")
    u = el.find("units")
    if u is not None and u.text:
        ax.units = u.text.strip()
    ax.labels = [lab.get("value", "") for lab in el.findall("LABEL")]
    return ax


def _read_xml(path: str):
    with open(path, "rb") as fh:
        return _xml_of(fh.read())


def _xml_of(raw: bytes):
    """Parse a definition that may not be well-formed UTF-8.

    Some converter output carries raw cp1252 bytes -- the 6HP28AF definition
    has 0x93/0x94 smart quotes inside a category name -- which ``ET.parse``
    rejects outright at the first one. Decoding as cp1252 (falling back to
    latin-1, which cannot fail) and handing ElementTree text rather than a
    path keeps those readable, and changes nothing for a clean file.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    return ET.fromstring(text)


def parse_xdf(path: str) -> XdfDef:
    return _parse(_read_xml(path))


def parse_xdf_bytes(data: bytes) -> XdfDef:
    """:func:`parse_xdf` for a definition already in memory (one packaged in
    an .ovt project)."""
    return _parse(_xml_of(bytes(data)))


def _parse(root) -> XdfDef:
    d = XdfDef()

    hdr = root.find("XDFHEADER")
    cat_names = {}
    if hdr is not None:
        t = hdr.find("deftitle")
        if t is not None and t.text:
            d.title = t.text.strip()
        de = hdr.find("description")
        if de is not None and de.text:
            d.description = de.text.strip()
        for c in hdr.findall("CATEGORY"):
            cat_names[_hexint(c.get("index"), 0)] = c.get("name", "")
        bo = hdr.find("BASEOFFSET")
        if bo is not None:
            try:
                off = int(bo.get("offset", "0"), 0)
            except ValueError:
                off = 0
            d.base_offset = -off if bo.get("subtract", "0") not in ("0", "") \
                else off
    d.categories = cat_names

    def cats_of(el):
        out = []
        for cm in el.findall("CATEGORYMEM"):
            # CATEGORYMEM category attribute is 1-based over header index
            idx = int(cm.get("category", "0")) - 1
            name = cat_names.get(idx)
            if name:
                out.append(name)
        return out

    for el in root.findall("XDFTABLE"):
        title = el.findtext("title", "").strip()
        tab = XdfTable(name=title,
                       description=(el.findtext("description") or "").strip(),
                       categories=cats_of(el))
        for axel in el.findall("XDFAXIS"):
            aid = axel.get("id")
            if aid == "z":
                emb = axel.find("EMBEDDEDDATA")
                if emb is None:
                    break
                tab.z_addr = _hexint(emb.get("mmedaddress"), 0)
                tab.z_bits = int(emb.get("mmedelementsizebits", "16"))
                flags = _hexint(emb.get("mmedtypeflags"), 0)
                tab.z_signed = bool(flags & 0x01)
                tab.z_lsb_first = bool(flags & 0x02)
                tab.z_colmajor = bool(flags & 0x04)
                tab.rows = int(emb.get("mmedrowcount", "1") or 1)
                tab.cols = int(emb.get("mmedcolcount", "1") or 1)
                m = axel.find("MATH")
                if m is not None:
                    tab.z_equation = m.get("equation", "X")
                u = axel.findtext("units")
                if u:
                    tab.z_units = u.strip()
                mn, mx = axel.findtext("min"), axel.findtext("max")
                tab.z_min = float(mn) if mn else None
                tab.z_max = float(mx) if mx else None
            else:
                ax = _axis_of(axel)
                if aid == "x":
                    tab.x = ax
                elif aid == "y":
                    tab.y = ax
        if tab.z_addr:
            _as_row(tab)
            d.tables.append(tab)

    for el in root.findall("XDFCONSTANT"):
        emb = el.find("EMBEDDEDDATA")
        if emb is None:
            continue
        flags = _hexint(emb.get("mmedtypeflags"), 0)
        m = el.find("MATH")
        mn, mx = el.findtext("min"), el.findtext("max")
        d.constants.append(XdfConstant(
            name=el.findtext("title", "").strip(),
            description=(el.findtext("description") or "").strip(),
            categories=cats_of(el),
            addr=_hexint(emb.get("mmedaddress"), 0),
            bits=int(emb.get("mmedelementsizebits", "16")),
            signed=bool(flags & 0x01),
            equation=m.get("equation", "X") if m is not None else "X",
            units=(el.findtext("units") or "").strip(),
            min=float(mn) if mn else None,
            max=float(mx) if mx else None,
            lsb_first=bool(flags & 0x02),
        ))
    return d

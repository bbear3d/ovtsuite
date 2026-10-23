# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A Definition as the entities the writers take -- with or without an image.

The XDF, A2L and KP writers were written for a session's entities. A
conversion has a Definition and, usually, no binary; this builds the same
entities from the definition alone, so there is one writer per format and the
in-app export and the converter cannot drift apart.

Addresses become offsets in the image: definition address - ``base``. Every
table that cannot be placed that way (outside the image, a non-IEEE width, a
size stored in an image that is not there) is reported in ``loss`` and left
out -- nothing is guessed.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from ..definitions.model import AxisDef, Definition, TableDef
from ..groups import DEFINITION
from ..model import Axis, Entity


class NoImage:
    """The ``binfile`` of an entity built without a binary: it states a byte
    order and has no data, which the writers read as "decide from what the
    definition says"."""

    data = None
    name = ""

    def __init__(self, endian: str = ">"):
        self.endian = endian


class Image:
    """A minimal ``binfile`` over image bytes (what the writers read)."""

    def __init__(self, data: bytes, endian: str = ">"):
        self.data = bytes(data)
        self.endian = endian
        self.name = ""

    def read_many(self, addr: int, count: int, bits: int = 16,
                  signed: bool = False) -> List[int]:
        n = max(1, bits // 8)
        order = "little" if self.endian == "<" else "big"
        out = []
        for i in range(count):
            a = addr + i * n
            if a < 0 or a + n > len(self.data):
                break
            out.append(int.from_bytes(self.data[a:a + n], order, signed=signed))
        return out


def _axis(ad: Optional[AxisDef], base: int, count: int) -> Optional[Axis]:
    if ad is None:
        return None
    st = ad.values
    if st is not None and st.addr is not None:
        if st.bits > 32 or (st.float and st.bits != 32):
            return None
        return Axis(addr=st.addr - base, count=count, bits=st.bits,
                    is_float=st.float, equation=st.equation_text(),
                    units=st.units,
                    signed=None if st.float or st.signed is None else bool(st.signed),
                    labels=ad.labels)
    if ad.fixed or ad.labels:
        vals = list(ad.fixed[:count]) if ad.fixed else None
        return Axis(addr=None, count=count, values=vals,
                    units=st.units if st else "", labels=ad.labels)
    return None


def reader(data: Optional[bytes], base: int) -> Callable:
    """``read(addr, n, signed, endian)`` at definition addresses, for a
    table whose sizes the image stores (``TableDef.dynamic``)."""
    def read(addr, n, signed, endian):
        if data is None:
            return None
        a = addr - base
        if a < 0 or a + n > len(data):
            return None
        return int.from_bytes(bytes(data[a:a + n]),
                              "little" if endian == "<" else "big", signed=signed)
    return read


def resolve(defn: Definition, base: int, size: int,
            data: Optional[bytes] = None, dynamic: str = "max"
            ) -> Tuple[List[TableDef], Dict[str, List[str]]]:
    """The definition's tables as laid out (sizes stored in ``data`` read from
    it), with ``{table name: [why it was changed or left out]}``.

    ``dynamic``: what to do with a table whose sizes are stored in an image
    when there is none -- ``"max"`` keeps its declared maximum, ``"skip"``
    leaves it out."""
    read = reader(data, base)
    out: List[TableDef] = []
    loss: Dict[str, List[str]] = {}
    for t in defn.tables:
        if t.dynamic is not None:
            if data is not None:
                try:
                    lt = t.dynamic(read)
                except Exception:
                    lt = None
                if lt is None:
                    loss.setdefault(t.name, []).append(
                        "its stored sizes are not in this image -- left out")
                    continue
                t = lt
            elif dynamic == "skip":
                loss.setdefault(t.name, []).append(
                    "its sizes are stored in the image and no binary was "
                    "given -- left out")
                continue
            else:
                loss.setdefault(t.name, []).append(
                    "its sizes are stored in the image and no binary was "
                    "given -- written at the declared maximum")
        out.append(t)
    return out, loss


def entities(defn: Definition, base: int, size: int,
             data: Optional[bytes] = None, endian: Optional[str] = None,
             dynamic: str = "max",
             loss: Optional[Dict[str, List[str]]] = None) -> List[Entity]:
    """Entities for every table of ``defn`` that has a place in an image of
    ``size`` bytes starting at definition address ``base``."""
    endian = endian or defn.byte_order()
    binfile = Image(data, endian) if data is not None else NoImage(endian)
    tables, why = resolve(defn, base, size, data, dynamic)
    if loss is not None:
        for k, v in why.items():
            loss.setdefault(k, []).extend(v)

    def lose(t, msg):
        if loss is not None:
            loss.setdefault(t.name, []).append(msg)

    out: List[Entity] = []
    for t in tables:
        z = t.z
        if z.addr is None:
            lose(t, "no stored values -- left out")
            continue
        if z.bits > 32 or (z.float and z.bits != 32):
            lose(t, "%d-bit %s values have no form in the writers -- left out"
                 % (z.bits, "float" if z.float else "integer"))
            continue
        if z.bits > 8 and z.endian and z.endian != endian:
            lose(t, "stored in the other byte order than the rest -- left out")
            continue
        x = _axis(t.x, base, t.cols) if t.kind != "constant" else None
        y = _axis(t.y, base, t.rows) if t.kind == "map" else None
        kind = t.kind
        if kind == "curve" and x is None and not t.text_kind:
            kind = "indexed"
        rows = t.rows if kind in ("map", "indexed") else 1
        name = t.symbol or t.name
        e = Entity(
            name=name, kind=kind, tier=DEFINITION,
            categories=list(t.categories), description=t.description or "",
            units=z.units or "", addr=z.addr - base,
            rows=max(1, rows), cols=max(1, t.cols), bits=z.bits,
            signed=bool(z.signed) and not z.float,
            equation=z.equation_text(), source="definition", x=x, y=y,
            binfile=binfile, declared_rows=max(1, rows),
            declared_cols=max(1, t.cols),
            order="col" if t.order == "col" else "row",
            text_kind=t.text_kind)
        if t.symbol and t.name != t.symbol:
            e.label = t.name
        e.is_float = z.float
        if t.kind == "constant":
            e.rows = e.cols = 1
        e.category_path = list(t.categories[:1])
        if t.note:
            e.note = t.note
        if e.addr < 0 or (size and e.end > size):
            lose(t, "at 0x%X, outside the image (0x%X bytes from 0x%X) -- "
                 "left out" % (z.addr, size, base))
            continue
        for tag, ax in (("X", x), ("Y", y)):
            if ax is not None and ax.addr is not None and (
                    ax.addr < 0 or (size and ax.addr + ax.count * max(1, ax.bits // 8) > size)):
                # written, it would be an address no image has (0x-10)
                lose(t, "%s axis outside the image -- left out (the table "
                     "keeps index numbers)" % tag)
                if tag == "X":
                    e.x = None
                else:
                    e.y = None
        e._tabledef = t
        out.append(e)
    return out

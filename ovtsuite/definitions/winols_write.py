# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Write a WinOLS map pack (``.kp``) -- EXPERIMENTAL.

The layout is the one :mod:`ovtsuite.definitions.winols` reads (format 292,
see docs/WINOLS_FORMAT.md), so this app reads every pack it writes back
exactly. Whether WinOLS itself opens it is not known: the reader skips parts
of the format it does not understand, and those are written here as the
values two real map packs hold in them (the image size and the data address
in the 32 bytes after the end address, the counts and the default display
settings in the record tail, the value mask and the signed flag in the
block before the counts) -- or, for the header's window settings and version
list, left out. Exporting an A2L and importing that into WinOLS is the
reliable way.

What goes in: values (type 2), curves and curves without an axis (type 3)
and maps (type 4); a stored axis as an axis in the image (source 1), an
axis given as values as values in the record (source 4, IEEE float). A map
stored column by column -- EDC16's, Y varies fastest -- goes out with its
axes swapped (WinOLS stores X fastest), so it reads the same bytes and shows
turned. Only a linear conversion (``raw * factor + offset``) fits a record;
a parameter with another conversion, and text, is left out and listed in
the warnings.
"""

from __future__ import annotations

import io
import struct
import time
import zipfile
from typing import Dict, List, Optional

from .winols import FILE_MAGIC, FOLDERS_MAGIC, RECORDS_MAGIC

#: The format written (the layout the reader names "292").
VERSION = 292

#: Before the counts (61 bytes in format 292): as the value records of a real
#: pack hold it -- a flag, the value mask; the signed flag 3 from the end.
_PRE = bytearray(61)
_PRE[18] = 1
_PRE[49:53] = b"\xff\xff\xff\xff"
_SIGNED_AT = -3

#: The record tail (77 bytes, then 01 01 01): the counts again at 6 and 10,
#: then the display defaults every value record of the real packs holds.
_TAIL = bytes.fromhex(
    "000000000000010000000100000001000000010100000000"
    "0000f03f000000000000f03fffffffff010000000000c072"
    "40000000000000f03f0000000000000000ffffffff000000"
    "0000000000")
assert len(_TAIL) == 77


class _W:
    def __init__(self):
        self.out = bytearray()

    def raw(self, b):
        self.out += b

    def u8(self, v):
        self.out += struct.pack("<B", v & 0xFF)

    def u32(self, v):
        self.out += struct.pack("<i" if v < 0 else "<I", v)

    def f64(self, v):
        self.out += struct.pack("<d", float(v))

    def str(self, t):
        b = str(t or "").encode("latin-1", "replace")
        self.u32(len(b))
        self.out += b
        if b:
            self.out += b"\0"                  # format 292: a NUL after text


def _org(bits: int, little: bool, flt: bool) -> Optional[int]:
    if flt:
        return 7 if little else 6
    return {8: 1, 16: 3 if little else 2, 32: 5 if little else 4}.get(bits)


def _linear(equation: str):
    """``(factor, offset)`` of a linear equation, else None."""
    from ..equation import Equation
    try:
        eq = Equation(equation or "X")
    except Exception:
        return None
    if not eq.linear:
        return None
    return eq.factor, eq.offset


def _axis(w: _W, e, ax, count: int, little: bool, warnings: List[str]):
    from ..xdf_export import _axis_signed
    if ax is not None and ax.addr is not None:
        lin = _linear(ax.equation) or (1.0, 0.0)
        if _linear(ax.equation) is None:
            warnings.append("%s: axis conversion %s is not linear -- raw axis"
                            % (e.name, ax.equation))
        signed = ax.signed if ax.signed is not None else _axis_signed(e, ax)
        org = _org(ax.bits, little, ax.is_float) or 2
        w.str("")                               # label
        w.str(ax.units)
        w.f64(lin[0])
        w.f64(lin[1])
        for v in (1, ax.addr, org, max(1, ax.bits // 8), 10):
            w.u32(v)
        w.raw(b"\0" * 14)
        w.u32(2)                                # precision
        w.u8(1 if signed else 0)
        w.u32(0)
        w.u32(0)
        w.u32(-1)
        return
    vals = list(getattr(ax, "values", None) or [])[:count] if ax is not None else []
    w.str("")
    w.str(getattr(ax, "units", "") if ax is not None else "")
    w.f64(1.0)
    w.f64(0.0)
    if len(vals) == count and count:
        data = b"".join(struct.pack(">f", float(v)) for v in vals)
        for v in (4, 0, 6, 4, 10):
            w.u32(v)
    else:
        data = b""
        for v in (0, 0, 2, 2, 10):
            w.u32(v)
    w.raw(b"\0" * 14)
    w.u32(2)
    w.u8(0)
    w.u32(len(data))
    w.raw(data)
    w.u32(0)
    w.u32(-1)


def _record(w: _W, e, folder: int, image_size: int, little: bool,
            warnings: List[str]) -> bool:
    lin = _linear(e.equation)
    org = _org(e.bits, little, bool(getattr(e, "is_float", False)))
    if lin is None or org is None or getattr(e, "text_kind", ""):
        warnings.append("%s: left out (%s)" % (
            e.name, "text" if getattr(e, "text_kind", "") else
            "conversion %s is not linear" % e.equation if lin is None
            else "%d-bit data" % e.bits))
        return False
    size = max(1, e.bits // 8)
    x, y = e.x, e.y
    cols, rows = e.cols, e.rows
    if e.kind == "constant":
        typ, cols, rows, x, y = 2, 1, 1, None, None
    elif e.kind in ("curve", "indexed"):
        typ, cols, rows, y = 3, e.cols * max(1, e.rows), 1, None
        if e.kind == "indexed":
            x = None
    else:
        typ = 4
        if getattr(e, "order", "col") == "col":
            # Y varies fastest in the image; WinOLS reads X fastest
            cols, rows, x, y = e.rows, e.cols, e.y, e.x
    w.u8(0)
    w.u32(-1)
    w.str("")                                   # bit texts
    w.u8(0)
    w.str(e.title if e.title != e.name else (e.description or ""))
    for v in (typ, 2, org, size, 10, folder):
        w.u32(v)
    w.str(e.name)
    pre = bytearray(_PRE)
    pre[_SIGNED_AT] = 1 if e.signed else 0
    w.raw(bytes(pre))
    prec = 0 if float(lin[0]).is_integer() and float(lin[1]).is_integer() else 2
    for v in (cols, rows, 0, 0, prec):
        w.u32(v)
    w.str("")                                   # label
    w.str(e.units)
    w.f64(lin[0])
    w.f64(lin[1])
    w.u32(e.addr)
    w.u32(e.addr + cols * rows * size)
    w.raw(struct.pack("<8I", image_size, 0, 0, e.addr, 0, 0, image_size, 0))
    _axis(w, e, x, cols, little, warnings)
    _axis(w, e, y, rows, little, warnings)
    tail = bytearray(_TAIL)
    struct.pack_into("<II", tail, 6, cols, rows)
    w.raw(bytes(tail))
    w.raw(b"\x01\x01\x01")
    return True


def write_kp(entities, image_size: int, title: str = "",
             little_endian: Optional[bool] = None,
             warnings: Optional[List[str]] = None,
             tiers: bool = True) -> bytes:
    """The map pack for ``entities`` (addresses: offsets in the image)."""
    from ..tiers import tier_name
    warnings = warnings if warnings is not None else []
    if little_endian is None:
        little_endian = any(getattr(e.binfile, "endian", ">") == "<"
                            for e in list(entities)[:1])
    folders: Dict[str, int] = {}
    rec = _W()
    rec.u8(0)
    rec.u32(0)
    n = 0
    for e in entities:
        if e.kind not in ("map", "curve", "constant", "indexed"):
            continue
        path = ([tier_name(e.tier)] if tiers else []) + list(
            getattr(e, "category_path", None) or e.categories[:1] or [])
        label = " / ".join(p for p in path if p) or "Maps"
        fid = folders.setdefault(label, len(folders) + 1)
        if _record(rec, e, fid, image_size, little_endian, warnings):
            n += 1
    struct.pack_into("<I", rec.out, 1, n)
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("intern", bytes(rec.out))
    w = _W()
    w.raw(FILE_MAGIC)
    w.u32(VERSION)
    w.u32(0)                                    # file size, set below
    for _ in range(16):                         # vehicle, control unit
        w.str("")
    now = int(time.time())
    w.raw(struct.pack("<II", now, now))
    w.str((title or "export") + ".kp")
    from .. import branding
    w.str("%s %s" % (branding.APP_NAME, branding.__version__))
    w.str("")
    w.str("1")
    w.raw(RECORDS_MAGIC + b"\0")
    w.u32(len(z.getvalue()))
    w.raw(z.getvalue())
    w.raw(FOLDERS_MAGIC + b"\0\0\0\0" + FOLDERS_MAGIC)
    w.u32(len(folders))
    for label, fid in folders.items():
        w.u32(fid)
        w.u32(0)
        w.str(label)
        w.u8(1)
        w.u32(1)
        w.u8(1)
        w.raw(b"\0" * 15)
    struct.pack_into("<I", w.out, 20, len(w.out))
    return bytes(w.out)

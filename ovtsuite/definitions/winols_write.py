# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Write a WinOLS map pack (``.kp``) -- EXPERIMENTAL.

The layout is the one :mod:`ovtsuite.definitions.winols` reads (format 292,
see docs/WINOLS_FORMAT.md), so this app reads every pack it writes back
exactly. The file is laid out as the three format-292 map packs WinOLS 5.0
wrote that are in the reference set (golf5_bxe, 4B0927156FB_RS6_5HP24, the
Audi A4 map pack): the same header with its 555-byte settings block (the map
count in it), the two-entry version list, the record archive as WinOLS zips
it, the folder list opening with *Hexdump* and *My maps*, and the fixed
8298-byte block that closes the file. Where those three files differ from
one another (window and font settings, memory left over in buffers WinOLS
saves uninitialised) zeros are written. The blocks the reader skips inside
a record carry the values the samples' records hold by default. Whether
WinOLS opens the result has not been tried; exporting an A2L and importing
that into WinOLS is the reliable way.

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

import struct
import zlib
from typing import Dict, List, Optional

from .winols import (FILE_MAGIC, FOLDERS_MAGIC, RECORDS_MAGIC, VERSION_SIG,
                     VERSIONS_MAGIC)

#: The format written (the layout the reader names "292").
VERSION = 292

#: The program the samples of format 292 name as their writer.
WRITER = "OLS 5.0 (Windows)"

#: Marks the start of the version images, the last four bytes of a map pack
#: (which holds no image).
IMAGES_MAGIC = b"\x33\x88\x72\x98"


def _sparse(size: int, runs: Dict[int, str]) -> bytearray:
    out = bytearray(size)
    for at, hx in runs.items():
        b = bytes.fromhex(hx)
        out[at:at + len(b)] = b
    return out


#: The header after the current-version string, up to the version list:
#: 555 bytes in all three samples, identical but for the map count at 0xB0
#: and 128 bytes at 0x30 (window and font settings, or leftover memory --
#: different in each, zeros here). It names the vehicle type "Passenger car",
#: the component "Engine" and the import format "Binary data", as the
#: samples do.
_HEAD_COUNT_AT = 0xB0
_HEAD = _sparse(555, {
    0x10: "02000000ffffffff",
    0x20: "01",
    0xBB: "ffffffff0005",
    0xD8: "0d00000050617373656e676572206361720006000000456e67696e65",
    0x11D: "ffffffff",
    0x152: "0b00000042696e6172792064617461",
    0x16E: "01",
    0x17E: "9dffffff",
    # two column orders: 0..19 and 0..15 as u16
    0x18B: "".join(struct.pack("<H", i).hex() for i in range(20))
           + "".join(struct.pack("<H", i).hex() for i in range(16)),
    0x21B: "ffffffff01",
    0x227: "01",
})

#: After the folder list, closing the file: 8298 bytes in all three samples,
#: ending with the image marker. The section markers and settings below are
#: what all three hold; the rest is zeros in the RS6 pack and leftover
#: memory in the other two (zeros here).
_TAIL_BLOCK = _sparse(8298, {
    0x1: "".join(struct.pack("<H", i).hex() for i in range(20))
         + "".join(struct.pack("<H", i).hex() for i in range(16)),
    0x91: "ffffffff",
    0xB7: "32427689f7fcffffffffffff",
    0xE3: "c336ab8301908aab88",
    0xF0: "01",
    0xF5: "020000000163ab6783",
    0x10A: "01",
    0x10F: "0200000024313608",
    0x11B: "0200000024313608",
    0x127: "45727e01bc1c84b3",
    0x137: "ab7c8809",
    0x13F: "0100000002000000ab785634",
    0x14F: "8a0123cd20",
    0x16F: "bc321692",
    0x17B: "aabc8877",
    0x183: "89217766",
    0x18F: "a921753201",
    0x19B: "83122788",
    0x1B8: "0101",
    0x1D7: "840c000064",
    0x5D2: "02",
    0xE63: "ffffffffffffffffffffffff0144bc3a120101",
    8298 - 4: IMAGES_MAGIC.hex(),
})

#: Before the counts (61 bytes in format 292), as most records of the
#: samples hold it: the value masks for 8 bit (0xFF at 17), 16 bit (at 33)
#: and 32 bit (at 49), no range given (byte 18 clear), the signed flag 3
#: bytes from the end.
_PRE = _sparse(61, {17: "ff", 33: "ffff", 49: "ffffffff"})
_SIGNED_AT = -3

#: An axis the record does not have, as the samples write it.
_NO_AXIS = ("-", "-", 1, 1)                     # label, units, org, size

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
    # the equation is probed (f(1) - f(0)): 0.1 comes back 0.10000000000002
    return float("%.12g" % eq.factor), float("%.12g" % eq.offset)


def _precision(lin) -> int:
    """Decimals shown: none for an integer conversion, else 2."""
    return 0 if float(lin[0]).is_integer() and float(lin[1]).is_integer() else 2


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
        w.u32(_precision(lin))
        w.u8(1 if signed else 0)
        w.u32(0)
        w.u32(0)
        w.u32(-1)
        return
    vals = list(getattr(ax, "values", None) or [])[:count] if ax is not None else []
    if len(vals) == count and count:
        w.str("")
        w.str(getattr(ax, "units", ""))
        data = b"".join(struct.pack(">f", float(v)) for v in vals)
        prec = 2
        fields = (4, 0, 6, 4, 10)               # in the record, float BE
    else:
        # no axis: as the samples write one ("-", "-", 8 bit, precision 0)
        label, units, org, size = _NO_AXIS
        w.str(label)
        w.str(units)
        data = b""
        prec = 0
        fields = (0, 0, org, size, 10)
    w.f64(1.0)
    w.f64(0.0)
    for v in fields:
        w.u32(v)
    w.raw(b"\0" * 14)
    w.u32(prec)
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
    # the description WinOLS lists the map by: never empty in the samples
    w.str(e.title if e.title != e.name else (e.description or e.name))
    for v in (typ, 2, org, size, 10, folder):
        w.u32(v)
    w.str(e.name)
    pre = bytearray(_PRE)
    pre[_SIGNED_AT] = 1 if e.signed else 0
    w.raw(bytes(pre))
    for v in (cols, rows, 0, 0, _precision(lin)):
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
    # folder ids 0 and 1 are WinOLS' own My maps and Hexdump; a map without
    # a category goes in My maps
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
        label = " / ".join(p for p in path if p)
        if not label or label.lower() == "my maps":
            fid = 0
        else:
            fid = folders.setdefault(label, len(folders) + 2)
        if _record(rec, e, fid, image_size, little_endian, warnings):
            n += 1
    struct.pack_into("<I", rec.out, 1, n)
    z = _zip_intern(bytes(rec.out))
    w = _W()
    w.raw(FILE_MAGIC)
    w.u32(VERSION)
    w.u32(0)                                    # file size, set below
    for _ in range(16):                         # vehicle, control unit
        w.str("")
    w.raw(b"\0" * 8)                            # created / changed: 0 in packs
    w.str((title or "export") + ".kp")
    w.str(WRITER)
    w.str("")
    w.str("1")
    head = bytearray(_HEAD)
    struct.pack_into("<I", head, _HEAD_COUNT_AT, n)
    w.raw(bytes(head))
    images_at = len(w.out)
    w.u32(0)                                    # where the images start, below
    w.u32(0)                                    # one version's image: none
    w.raw(VERSIONS_MAGIC)
    w.u32(2)
    for name in ("Original", "Version 1"):
        w.str(name)
        w.str("")                               # comment
        w.raw(VERSION_SIG + b"\0" * 12)         # two times, u32
    w.raw(RECORDS_MAGIC + b"\x01")
    w.u32(len(z))
    w.raw(z)
    w.raw(FOLDERS_MAGIC + b"\0\0\0\0" + FOLDERS_MAGIC)
    w.u32(len(folders) + 2)
    listed = [(1, 9001, "Hexdump"), (0, 9000, "My maps")] + sorted(
        ((fid, 0, label) for label, fid in folders.items()),
        key=lambda f: f[2].lower())
    for fid, res, label in listed:              # as WinOLS: by name
        w.u32(fid)
        w.u32(res)
        w.str(label)
        w.u8(1)
        w.u32(1)                                # one version byte
        w.u8(1)
        w.raw(b"\0" * 15)
    w.raw(bytes(_TAIL_BLOCK))
    struct.pack_into("<I", w.out, 20, len(w.out))
    struct.pack_into("<I", w.out, images_at, len(w.out) - len(IMAGES_MAGIC))
    return bytes(w.out)


def _zip_intern(data: bytes) -> bytes:
    """``data`` as the ZIP entry ``intern``, laid out as WinOLS writes it:
    deflated at the highest level (flag 2), no time stamp, the archive
    attribute, no data descriptor."""
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = c.compress(data) + c.flush()
    crc = zlib.crc32(data) & 0xFFFFFFFF
    name = b"intern"
    common = struct.pack("<HHHHHIIIH", 20, 2, 8, 0, 0, crc, len(packed),
                         len(data), len(name))
    local = b"PK\x03\x04" + common + struct.pack("<H", 0) + name
    central = (b"PK\x01\x02" + struct.pack("<H", 20) + common
               + struct.pack("<HHHHII", 0, 0, 0, 0, 0x20, 0) + name)
    end = b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, 1, 1, len(central),
                                      len(local) + len(packed), 0)
    return local + packed + central + end

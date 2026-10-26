# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Write a WinOLS map pack (``.kp``).

Two layouts, both as WinOLS itself writes them (see docs/WINOLS_FORMAT.md):

* **format 292** (the default): the layout of the three map packs WinOLS 5.0
  wrote that are in the reference set (golf5_bxe, 4B0927156FB_RS6_5HP24, the
  Audi A4 map pack). Every section is plain: the header with its 555-byte
  settings block, the two-entry version list, the record archive, the folder
  list and the fixed 8298-byte block that closes the file. Where those files
  differ from one another (window settings, memory left over in buffers
  WinOLS saves uninitialised) zeros are written.
* **format 503**: the layout WinOLS 4.x writes (045906019K, a 038906012FD
  pack): multilingual strings, 117-byte value blocks, the folder list with
  *Potential maps*, and the values of every map stored after the image
  marker, checked by a CRC-32 and a byte sum in the header. Its closing block
  holds a block WinOLS encrypts, which cannot be made here; it is copied from
  a genuine format-503 pack given as ``template`` (one the user's WinOLS
  wrote), so only that pack's own bytes go in. A placeholder for future
  work: WinOLS 5.87 refuses it, and nothing in the app offers it (keep it
  out of the GUI, CLI and converter until a pack it writes is imported).

WinOLS 5.87 (Demo) imports a format-292 pack written here (2026-10-24: a
038906012FD export, and golf5_bxe passed through the writer). It refuses a
format-503 one made with a genuine pack's closing block, so 292 stays the
default; exporting an A2L remains the fallback.

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

#: The format written by default (the layout the reader names "292").
VERSION = 292

#: The formats :func:`write_kp` writes.
VERSIONS = (292, 503)

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

#: Two f64 display settings at 41 and 49 of the tail: 300 and 1 in the
#: records of maps and values, 0 and 0.01 in those of curves -- in every
#: record of the 038906012FD pack (WinOLS 4.x), and in most of the 292 ones.
_TAIL_VIEW_AT = 41
_CURVE_VIEW = (0.0, 0.01)

# ----------------------------------------------------------------- format 503
#: The header after the current-version string, up to the version list, as
#: WinOLS 4.x writes it in English (626 bytes; the 045906019K pack holds the
#: same block with the German texts): ``u32 2``, the CRC-32 of the map data
#: after the image marker (inverted: -1 for none) at 0x14, their byte sum
#: (16 bit) at 0x1C, the map count at 0xB8, then the vehicle type
#: "Passenger car", the component "Engine", the import format "Complete
#: binary file", the memory "Eprom", the two column orders and the language.
_HEAD503_CRC_AT, _HEAD503_SUM_AT, _HEAD503_COUNT_AT = 0x14, 0x1C, 0xB8
_HEAD503 = _sparse(626, {
    0x10: "02",
    0x20: "01",
    0xC3: "ffffffff0005",
    0xE0: "0d00000050617373656e6765722063617206000000456e67696e65",
    0x123: "ffffffff",
    0x158: "14000000436f6d706c6574652062696e6172792066696c65",
    0x17C: "01",
    0x184: "ffff0700050000004570726f6d9affffff",
    0x19E: "".join(struct.pack("<H", i).hex() for i in range(20))
           + "".join(struct.pack("<H", i).hex() for i in range(16)),
    0x22E: "ffffffff01",
    0x246: "0107000000456e676c697368",
    0x262: "73cc3983",
    0x26E: "01",
})

#: One version-list entry's bytes after the name, the comment and the
#: signature: the times and the checksum state, all 0 (as 045906019K).
_VERSION503_REST = 77

#: Before the counts (117 bytes): the largest values of 8, 16 and 32 bit
#: as f64 (256 at 25, 65536 at 41, 2^32 at 57) and the signed flag 3 bytes
#: from the end -- every record of the 038906012FD pack.
_PRE503 = _sparse(117, {
    4: "10",
    25: struct.pack("<d", 256.0).hex(),
    41: struct.pack("<d", 65536.0).hex(),
    57: struct.pack("<d", 4294967296.0).hex(),
})

#: After the 01 01 01 that ends a record's tail: ``i32 -1`` and 60 zeros.
_TAIL503 = b"\xff\xff\xff\xff" + b"\0" * 60

#: An absent axis' 14 bytes after the number base (the first set).
_NO_AXIS_PRE503 = b"\x01" + b"\0" * 13

#: The folder-entry tail of format 503 (25 bytes): the parent at 17 (-1:
#: at the top).
_FOLDER_TAIL503 = b"\0" * 17 + b"\xff\xff\xff\xff" + b"\0" * 4


class _W:
    def __init__(self, nul: bool = True, ml: bool = False):
        self.out = bytearray()
        self.nul = nul                          # format 292: NUL after text
        self.ml = ml                            # format 503: mlstr

    def raw(self, b):
        self.out += b

    def u8(self, v):
        self.out += struct.pack("<B", v & 0xFF)

    def u32(self, v):
        self.out += struct.pack("<i" if v < 0 else "<I", v)

    def f64(self, v):
        self.out += struct.pack("<d", float(v))

    def str(self, t):
        """A string; None is "no string" (length -1)."""
        if t is None:
            self.u32(-1)
            return
        b = str(t).encode("latin-1", "replace")
        self.u32(len(b))
        self.out += b
        if b and self.nul:
            self.out += b"\0"

    def mlstr(self, t):
        """A text with its translations (none): in format 503 the string,
        8 zero bytes and a count of 0; before, the string alone."""
        self.str(t)
        if self.ml:
            self.out += b"\0" * 12


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


def _axis_end(w: _W, fmt: int):
    w.u32(0)
    w.u32(-1)
    if fmt >= 503:
        w.str("")                               # axis id
        w.u32(0)                                # point texts
        w.raw(b"\0" * 4)


def _axis(w: _W, e, ax, count: int, little: bool, warnings: List[str],
          fmt: int, image: Optional[bytes], blob: bytearray):
    from ..xdf_export import _axis_signed
    new = fmt >= 503
    if ax is not None and ax.addr is not None:
        lin = _linear(ax.equation) or (1.0, 0.0)
        if _linear(ax.equation) is None:
            warnings.append("%s: axis conversion %s is not linear -- raw axis"
                            % (e.name, ax.equation))
        signed = ax.signed if ax.signed is not None else _axis_signed(e, ax)
        org = _org(ax.bits, little, ax.is_float) or 2
        size = max(1, ax.bits // 8)
        w.mlstr("")                             # label
        w.str(ax.units or (None if new else ""))
        w.f64(lin[0])
        w.f64(lin[1])
        for v in (1, ax.addr, org, size, 10):
            w.u32(v)
        w.raw(b"\0" * 14)
        w.u32(_precision(lin))
        w.u8(1 if signed else 0)
        w.u32(0)
        _axis_end(w, fmt)
        if image is not None:
            blob += _span(image, ax.addr, count * size)
        return
    vals = list(getattr(ax, "values", None) or [])[:count] if ax is not None else []
    if len(vals) == count and count:
        w.mlstr("")
        w.str(getattr(ax, "units", "") or (None if new else ""))
        data = b"".join(struct.pack(">f", float(v)) for v in vals)
        prec = 2
        fields = (4, 0, 6, 4, 10)               # in the record, float BE
        pre = b"\0" * 14
    elif new:
        # no axis: as WinOLS 4.x writes one (no texts, 8 bit, hexadecimal,
        # no precision)
        w.mlstr(None)
        w.str(None)
        data = b""
        prec = -1
        fields = (0, 0, 1, 1, 16)
        pre = _NO_AXIS_PRE503
    else:
        # no axis: as the samples write one ("-", "-", 8 bit, precision 0)
        label, units, org, size = _NO_AXIS
        w.str(label)
        w.str(units)
        data = b""
        prec = 0
        fields = (0, 0, org, size, 10)
        pre = b"\0" * 14
    w.f64(1.0)
    w.f64(0.0)
    for v in fields:
        w.u32(v)
    w.raw(pre)
    w.u32(prec)
    w.u8(0)
    w.u32(len(data))
    w.raw(data)
    _axis_end(w, fmt)


def _span(image: bytes, addr: int, n: int) -> bytes:
    """``n`` bytes of the image at ``addr`` (zeros past its end)."""
    got = bytes(image[addr:addr + n]) if 0 <= addr else b""
    return got + b"\0" * (n - len(got))


def _record(w: _W, e, folder: int, image_size: int, little: bool,
            warnings: List[str], fmt: int = VERSION,
            image: Optional[bytes] = None,
            blob: Optional[bytearray] = None) -> bool:
    lin = _linear(e.equation)
    org = _org(e.bits, little, bool(getattr(e, "is_float", False)))
    if lin is None or org is None or getattr(e, "text_kind", ""):
        warnings.append("%s: left out (%s)" % (
            e.name, "text" if getattr(e, "text_kind", "") else
            "conversion %s is not linear" % e.equation if lin is None
            else "%d-bit data" % e.bits))
        return False
    new = fmt >= 503
    blob = blob if blob is not None else bytearray()
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
    n = cols * rows * size
    w.u8(0)
    w.u32(-1)
    w.mlstr("")                                 # bit texts
    w.u8(0)
    # the description WinOLS lists the map by: never empty in the samples
    w.mlstr(e.title if e.title != e.name else (e.description or e.name))
    for v in (typ, 2, org, size, 10, folder):
        w.u32(v)
    w.str(e.name)
    pre = bytearray(_PRE503 if new else _PRE)
    pre[_SIGNED_AT] = 1 if e.signed else 0
    w.raw(bytes(pre))
    for v in (cols, rows, 0, 0, _precision(lin)):
        w.u32(v)
    w.str("")                                   # label
    w.str(e.units or (None if new else ""))
    w.f64(lin[0])
    w.f64(lin[1])
    w.u32(e.addr)
    w.u32(e.addr + n)
    if new:
        # the 038906012FD pack: the image size and the data's size
        w.raw(struct.pack("<8I", image_size, 0, 0, 0, 0, 0, n, 0))
    else:
        w.raw(struct.pack("<8I", image_size, 0, 0, e.addr, 0, 0, image_size, 0))
    if image is not None:
        blob += _span(image, e.addr, n)         # the values, then the axes
    _axis(w, e, x, cols, little, warnings, fmt, image, blob)
    _axis(w, e, y, rows, little, warnings, fmt, image, blob)
    tail = bytearray(_TAIL)
    struct.pack_into("<II", tail, 6, cols, rows)
    if typ == 3:
        struct.pack_into("<dd", tail, _TAIL_VIEW_AT, *_CURVE_VIEW)
    w.raw(bytes(tail))
    w.raw(b"\x01\x01\x01")
    if new:
        w.raw(_TAIL503)
    return True


def write_kp(entities, image_size: int, title: str = "",
             little_endian: Optional[bool] = None,
             warnings: Optional[List[str]] = None,
             tiers: bool = True, version: int = VERSION,
             template: Optional[bytes] = None,
             image: Optional[bytes] = None) -> bytes:
    """The map pack for ``entities`` (addresses: offsets in the image).

    ``version`` 292 (default) or 503. Format 503 needs ``template``, a map
    pack of format 503 WinOLS wrote (its closing block is copied), and
    stores the maps' values: from ``image``, else from the entities' binary
    (none when they have no binary)."""
    from ..tiers import tier_name
    if version not in VERSIONS:
        raise ValueError("format %r: map packs are written as %s"
                         % (version, " or ".join(map(str, VERSIONS))))
    closing = None
    if version >= 503:
        if template is None:
            raise ValueError("a format-503 map pack copies its closing block "
                             "from a genuine one: give a WinOLS map pack of "
                             "format 503 as the template")
        closing = template_closing(template)
    warnings = warnings if warnings is not None else []
    entities = list(entities)
    if little_endian is None:
        little_endian = any(getattr(e.binfile, "endian", ">") == "<"
                            for e in entities[:1])
    if version >= 503 and image is None:
        for e in entities[:1]:
            image = getattr(getattr(e, "binfile", None), "data", None)
    new = version >= 503
    # folder ids 0 and 1 are WinOLS' own My maps and Hexdump (2 Potential
    # maps in format 503); a map without a category goes in My maps
    first = 3 if new else 2
    folders: Dict[str, int] = {}
    rec = _W(nul=not new, ml=new)
    rec.u8(0)
    rec.u32(0)
    blob = bytearray()
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
            fid = folders.setdefault(label, len(folders) + first)
        if _record(rec, e, fid, image_size, little_endian, warnings, version,
                   image if new else None, blob):
            n += 1
    builtin = [(1, 9001, "Hexdump"), (0, 9000, "My maps")]
    rest = [(fid, 0, label) for label, fid in folders.items()]
    if new:
        rest.append((2, 9002, "Potential maps"))
    listed = builtin + sorted(rest, key=lambda f: f[2].lower())
    return _assemble(rec, n, listed, image_size, title, version, blob, closing)


def _assemble(rec: "_W", n: int, listed, image_size: int, title: str,
              version: int = VERSION, blob: Optional[bytearray] = None,
              closing: Optional[bytes] = None) -> bytes:
    """The map pack around the records in ``rec`` (``n`` of them, after
    its ``u8 0, u32 count``): the header, the version list, the record
    archive, the folder list ``listed`` (``[(id, resource, name)]`` in the
    order written) and the closing block."""
    new = version >= 503
    blob = blob if blob is not None else bytearray()
    struct.pack_into("<I", rec.out, 1, n)
    z = _zip_intern(bytes(rec.out))
    w = _W(nul=not new, ml=new)
    w.raw(FILE_MAGIC)
    w.u32(version)
    w.u32(0)                                    # file size, set below
    for _ in range(16):                         # vehicle, control unit
        w.str("")
    w.raw(b"\0" * 8)                            # created / changed: 0 in packs
    w.str((title or "export") + ".kp")
    w.str(WRITER)
    w.str("")
    w.str("1")
    if new:
        head = bytearray(_HEAD503)
        struct.pack_into("<I", head, _HEAD503_CRC_AT,
                         zlib.crc32(bytes(blob)) ^ 0xFFFFFFFF)
        struct.pack_into("<I", head, _HEAD503_SUM_AT, sum(blob) & 0xFFFF)
        struct.pack_into("<I", head, _HEAD503_COUNT_AT, n)
    else:
        head = bytearray(_HEAD)
        struct.pack_into("<I", head, _HEAD_COUNT_AT, n)
    w.raw(bytes(head))
    images_at = len(w.out)
    w.u32(0)                                    # where the images start, below
    w.u32(len(blob))                            # the map data after it
    w.raw(VERSIONS_MAGIC)
    w.u32(2)
    for name in ("Original", "Version 1"):
        w.str(name)
        w.str("")                               # comment
        w.raw(VERSION_SIG + b"\0" * (_VERSION503_REST if new else 12))
    w.raw(RECORDS_MAGIC + b"\x01")
    w.u32(len(z))
    w.raw(z)
    w.raw(FOLDERS_MAGIC + b"\0\0\0\0" + FOLDERS_MAGIC)
    w.u32(len(listed))
    for fid, res, label in listed:
        w.u32(fid)
        w.u32(res)
        w.mlstr(label)
        if new:
            # 038906012FD: open in the list (01) Hexdump and My maps
            w.u8(0)
            w.u32(1)
            w.u8(1 if res in (9000, 9001) else 0)
            w.raw(_FOLDER_TAIL503)
        else:
            w.u8(1)
            w.u32(1)                            # one version byte
            w.u8(1)
            w.raw(b"\0" * 15)
    if new:
        w.raw(closing)
        marker = len(w.out)
        w.raw(IMAGES_MAGIC)
        w.raw(bytes(blob))
    else:
        w.raw(bytes(_TAIL_BLOCK))
        marker = len(w.out) - len(IMAGES_MAGIC)
    struct.pack_into("<I", w.out, 20, len(w.out))
    struct.pack_into("<I", w.out, images_at, marker)
    return bytes(w.out)


# ------------------------------------------------------------ pass-through
#: The profiles whose record blocks are format 292's own, field for field
#: (a record read with one of them is written back byte for byte).
_SAME_292 = ("292",)
#: ... and those whose end-address block is (303: the 292 layout with the
#: 117-byte block before the counts).
_ZTAIL_292 = ("292", "303")


def _passthrough_axis(w: "_W", a, same: bool) -> None:
    def text(field, value):
        return None if same and field in a.none else value
    w.str(text("label", a.label))
    w.str(text("units", a.units))
    w.f64(a.factor)
    w.f64(a.offset)
    for v in (a.source, a.addr, a.org, a.size, a.numbase):
        w.u32(v)
    w.raw(a.pre if same and len(a.pre) == 14 else b"\0" * 14)
    w.u32(a.precision)
    w.u8(1 if a.signed else 0)
    w.u32(len(a.data))
    w.raw(bytes(a.data))
    w.raw(a.after if len(a.after) == 8 else b"\0\0\0\0\xff\xff\xff\xff")


def _passthrough_record(w: "_W", r, image_size: int) -> None:
    """Record ``r`` (a :class:`~.winols.Record`) in format 292. One read
    with the 292 profile goes out as it was read; one read with another
    has its fields written in the 292 layout (the blocks that layout does
    not share made as :func:`write_kp` makes them)."""
    same = r.profile in _SAME_292

    def text(field, value):
        return None if same and field in r.none else value
    w.u8(r.lead)
    w.u32(-1)
    w.str(r.bit_texts if same else (r.bit_texts or ""))
    w.u8(r.bit_u8)
    w.str(text("description", r.description))
    for v in (r.type, r.h1, r.org, r.size, r.numbase, r.folder):
        w.u32(v)
    w.str(text("name", r.name))
    if same and len(r.pre) == len(_PRE):
        w.raw(r.pre)
    else:
        pre = bytearray(_PRE)
        pre[_SIGNED_AT] = 1 if r.signed else 0
        w.raw(bytes(pre))
    for v in (r.cols, r.rows, r.c2, r.c3, r.precision):
        w.u32(v)
    w.str(text("label", r.label))
    w.str(text("units", r.units))
    w.f64(r.factor)
    w.f64(r.offset)
    w.u32(r.addr)
    w.u32(r.end)
    if r.profile in _ZTAIL_292 and len(r.ztail) == 32:
        w.raw(r.ztail)
    else:
        w.raw(struct.pack("<8I", image_size, 0, 0, r.addr, 0, 0, image_size, 0))
    _passthrough_axis(w, r.x, same)
    _passthrough_axis(w, r.y, same)
    w.raw(r.tail if len(r.tail) == len(_TAIL) else _TAIL)
    w.raw(r.flags if len(r.flags) == 3 else b"\x01\x01\x01")


def write_kp_records(records, folders, image_size: int, title: str = "",
                     warnings: Optional[List[str]] = None) -> bytes:
    """A map pack (format 292) holding WinOLS ``records`` as they are --
    the pass-through of an OLS or KP: every record with its own type,
    texts, scaling, axes and folder id, and the folder list ``folders``
    (``[(id, resource, name)]``) in the order given. Nothing is regrouped,
    renamed or left out but the hexdump view (type 1), which no map pack
    WinOLS writes holds, and *Potential maps* when no record is in it (no
    format-292 pack lists it; WinOLS shows it all the same). Texts in
    other languages a record carries (format 397 on) and axis point texts
    have no place in format 292 and are not written."""
    warnings = warnings if warnings is not None else []
    rec = _W(nul=True, ml=False)
    rec.u8(0)
    rec.u32(0)
    n = 0
    used = set()
    texts = 0
    for r in records:
        if r.type == 1:
            continue
        _passthrough_record(rec, r, image_size)
        used.add(r.folder)
        if any(a.texts for a in (r.x, r.y)):
            texts += 1
        n += 1
    if texts:
        warnings.append("%d maps have axis point texts, which a map pack of "
                        "format 292 cannot hold -- not written" % texts)
    listed = []
    have = set()
    for fid, res, name in folders:
        if fid in have:
            continue
        if res == 9002 and fid not in used:
            continue
        have.add(fid)
        listed.append((fid, res, name))
    for fid, res, name in ((1, 9001, "Hexdump"), (0, 9000, "My maps")):
        if fid not in have:
            listed.insert(0 if fid == 1 else 1, (fid, res, name))
            have.add(fid)
    lost = sorted(used - have)
    if lost:
        warnings.append("%d maps name a folder the file does not list (%s) -- "
                        "WinOLS shows them in My maps"
                        % (sum(1 for r in records if r.type != 1 and r.folder in lost),
                           ", ".join(map(str, lost[:10]))))
    return _assemble(rec, n, listed, image_size, title, VERSION)


def template_closing(data: bytes) -> bytes:
    """The closing block of a genuine format-503 map pack: the bytes from
    the end of its folder list to its image marker (settings, the WinOLS
    version that wrote it and a block WinOLS encrypts). ValueError when
    ``data`` is not such a pack."""
    import io
    import zipfile
    from .winols import DefinitionError, _folder_entry, format_version, \
        read_header
    d = bytes(data)
    try:
        ver = format_version(d)
        h = read_header(d)
    except (DefinitionError, struct.error) as ex:
        raise ValueError("the template is not a WinOLS file (%s)" % ex)
    if ver != 503:
        raise ValueError("the template is of format %d, not 503" % ver)
    v = d.find(VERSIONS_MAGIC, h.end)
    m = d.find(RECORDS_MAGIC, h.end)
    if v < 8 or m < 0:
        raise ValueError("the template has no version or map list")
    marker = struct.unpack_from("<I", d, v - 8)[0]
    zlen = struct.unpack_from("<I", d, m + 5)[0]
    try:
        zipfile.ZipFile(io.BytesIO(d[m + 9:m + 9 + zlen])).read("intern")
    except (zipfile.BadZipFile, KeyError) as ex:
        raise ValueError("the template is not a map pack (%s)" % ex)
    p = m + 9 + zlen
    if d[p:p + 4] != FOLDERS_MAGIC or d[p + 8:p + 12] != FOLDERS_MAGIC:
        raise ValueError("the template's folder list is not where expected")
    count = struct.unpack_from("<I", d, p + 12)[0]
    p += 16
    try:
        for _ in range(count):
            p = _folder_entry(d, p, False, True)[3] + len(_FOLDER_TAIL503)
    except (ValueError, struct.error) as ex:
        raise ValueError("the template's folder list does not read (%s)" % ex)
    if d[marker:marker + 4] != IMAGES_MAGIC or not p < marker:
        raise ValueError("the template's image marker is not where its "
                         "header says")
    return d[p:marker]


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

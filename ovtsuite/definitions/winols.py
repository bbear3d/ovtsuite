# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""WinOLS projects (``.ols``) and map packs (``.kp``), read structurally.

Both start with the same header -- ``u32 11, "WinOLS File\\0"``, ``u32``
format version -- and keep their maps as one list of records, written field
after field with no padding. The record layout was reconstructed from
projects of format versions 114, 117, 250, 292, 397, 479, 503 and 597 (OLS 4 ... 5.x);
(and 303, 373 - 375 and 440 from a larger corpus); every record of every
sample parses in sequence and the list ends exactly
where the next section starts, so nothing here searches or guesses: a record
is read field by field, and a list that does not parse to its declared count
is reported, not patched over.

Layout (``str`` = ``u32 length`` + bytes, plus a NUL in formats before 400
when not empty; a negative length means "no string"; ``mlstr`` = ``str`` +
8 bytes + ``u32 n`` + n x (``str``, ``u32`` language) -- a text with its
translations, from format 397)::

    record   u8, [i32 -1, mlstr bit texts, u8]       (format >= 200)
             mlstr description
             u32 type (1 hexdump, 2 value, 3 curve, 4/5 map), u32,
             u32 data organisation, u32 element size, u32 number base,
             u32 folder, str name
             pre-count block (5 / 9 / 117 bytes; its last 52 hold the value
             masks and, 3 bytes before the counts, the signed flag)
             u32 columns, u32 rows, u32, u32, u32 precision
             str label, str units, f64 factor, f64 offset,
             u32 address, u32 end, 24 / 32 bytes
             axis X, axis Y
             record tail (format >= 397: + u32, 36 / 48 / 60 / 64 bytes)
    axis     mlstr label, str units, f64 factor, f64 offset,
             u32 source (0 none, 1/3 in the binary, 4 values here,
             8 texts), u32 address, u32 data organisation, u32 size,
             u32 number base, 2 / 14 bytes, i32 precision, u8 signed,
             u32 n + n bytes of values, i32, i32,
             [str id, u32 n + n x mlstr point texts]        (format >= 373)
             [4 bytes]                                       (format >= 479)

Values are ``raw * factor + offset``; data organisation 1 = 8 bit,
2/3 = 16 bit big/little endian, 4/5 = 32 bit, 6/7 = IEEE float. Map data is
stored row by row (X varies fastest). Addresses are offsets in the image.

An ``.ols`` also carries the image itself (after ``33 88 72 98``), the folder
names, and the vehicle and control-unit details entered in WinOLS.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .model import AxisDef, Definition, DefinitionError, Storage, TableDef

FILE_MAGIC = b"\x0b\x00\x00\x00WinOLS File\x00"
RECORDS_MAGIC = b"\x77\x33\x88\x11"
FOLDERS_MAGIC = b"\x11\x88\x63\x98"
IMAGE_SIG = b"\x33\x88\x72\x98"
NO_PARENT = b"\xff\xff\xff\xff"

#: data organisation -> (bits, endian, float)
ORG = {1: (8, "", False), 2: (16, ">", False), 3: (16, "<", False),
       4: (32, ">", False), 5: (32, "<", False),
       6: (32, ">", True), 7: (32, "<", True)}

#: the vehicle and control-unit fields at the start of every header
VEHICLE_FIELDS = ("make", "model", "engine", "year", "fuel", "displacement",
                  "power", "gearbox")
ECU_FIELDS = ("use", "producer", "build", "part", "part2", "software",
              "software2", "extra")


def is_winols(data: bytes) -> bool:
    return bytes(data[:16]) == FILE_MAGIC


def format_version(data: bytes) -> int:
    return struct.unpack_from("<I", data, 16)[0]


# ------------------------------------------------------------------ profiles
@dataclass(frozen=True)
class _Profile:
    name: str
    nul: bool           # non-empty strings end in a NUL
    prefix: bool        # records start with i32 -1 + bit texts (>= 200)
    ml: bool            # descriptions / axis labels carry translations
    precx: int          # bytes between the name and the counts
    ztail: int          # bytes after the data's end address
    axpre: int          # bytes between an axis' number base and its precision
    axid: bool          # axes carry an id and point texts
    axextra: int        # bytes at the end of an axis
    tailn: int          # record tail after the 01 01 01 marker (-1: none)
    folder_ml: bool
    folder_tail: int


# Ordered newest first; a list is read with the profile its format version
# names, and if that does not parse to the declared count, with the others
# (an unseen version is usually one of these layouts).
PROFILES = (
    _Profile("597", False, True, True, 117, 32, 14, True, 4, 64, True, 25),
    _Profile("503", False, True, True, 117, 32, 14, True, 4, 60, True, 25),
    _Profile("479", False, True, True, 117, 32, 14, True, 4, 48, True, 25),
    # 440: the 479 layout (no NULs, 4 bytes after each axis) with the
    # 36-byte record tail of 397. 35 projects of it parse to their count and
    # end exactly at the folder section.
    _Profile("440", False, True, True, 117, 32, 14, True, 4, 36, True, 25),
    _Profile("397", True, True, True, 117, 32, 14, True, 0, 36, False, 25),
    # 373 - 375: the 397 layout with a 12-byte record tail (BMW EDC17C41 /
    # MED17.2.3, EDC17CP46, Delco E39 projects: each parses to its count and
    # ends at the folder section).
    _Profile("373", True, True, True, 117, 32, 14, True, 0, 12, True, 25),
    # 303: the 292 layout with the 117-byte block before the counts (18
    # projects, from 27 to 47,596 records). An axis carries no id or texts:
    # the alternative that reads one 12 bytes later breaks on projects whose
    # axes are stored, which this reads with sane precisions and signs.
    _Profile("303", True, True, False, 117, 32, 14, False, 0, -1, False, 15),
    _Profile("292", True, True, False, 61, 32, 14, False, 0, -1, False, 15),
    # Format 250 (OLS 5.0) is a hybrid: records carry NO i32 -1 prefix (like
    # 117) but use the 292-era field sizes (precx 61), with its own ztail 24
    # and axpre 6. Reconstructed from a real 163-record project (RS6 5HP24);
    # all records parse in sequence and the list ends exactly at the folder
    # section. Format 100 is yet another layout and is not covered here.
    _Profile("250", True, False, False, 61, 24, 6, False, 0, -1, False, 0),
    _Profile("117", True, False, False, 57, 24, 2, False, 0, -1, False, 0),
)


def _profile_for(ver: int) -> _Profile:
    for p in PROFILES:
        if ver >= int(p.name):
            return p
    return PROFILES[-1]


# -------------------------------------------------------------------- reader
class _R:
    __slots__ = ("d", "p", "P")

    def __init__(self, d: bytes, p: int, prof: _Profile):
        self.d, self.p, self.P = d, p, prof

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def f64(self):
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v

    def raw(self, n):
        if n < 0 or self.p + n > len(self.d):
            raise ValueError("block past the end at 0x%X" % self.p)
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def str(self) -> Optional[str]:
        n = self.i32()
        if n < 0:
            return None
        if n > 0x100000 or self.p + n > len(self.d):
            raise ValueError("string of %d bytes at 0x%X" % (n, self.p - 4))
        t = self.d[self.p:self.p + n].decode("latin-1")
        self.p += n
        if self.P.nul and n:
            if self.d[self.p] != 0:
                raise ValueError("unterminated string at 0x%X" % self.p)
            self.p += 1
        return t

    def mlstr(self) -> Optional[str]:
        t = self.str()
        if self.P.ml:
            self.p += 8
            n = self.u32()
            if n > 1000:
                raise ValueError("%d translations at 0x%X" % (n, self.p - 4))
            for _ in range(n):
                self.str()
                self.p += 4
        return t


# -------------------------------------------------------------------- records
@dataclass
class Axis:
    label: str
    units: str
    factor: float
    offset: float
    source: int
    addr: int
    org: int
    size: int
    signed: bool
    precision: int
    data: bytes
    texts: List[str] = field(default_factory=list)


@dataclass
class Record:
    type: int
    org: int
    size: int
    numbase: int
    folder: int
    name: str
    description: str
    cols: int
    rows: int
    signed: bool
    precision: int
    label: str
    units: str
    factor: float
    offset: float
    addr: int
    x: Axis
    y: Axis


def _axis(r: _R) -> Axis:
    P = r.P
    label = r.mlstr() or ""
    units = r.str() or ""
    factor, offset = r.f64(), r.f64()
    source, addr, org, size = r.u32(), r.u32(), r.u32(), r.u32()
    r.u32()                                     # number base
    r.p += P.axpre
    prec = r.i32()
    signed = bool(r.u8())
    n = r.u32()
    if n > 0x100000:
        raise ValueError("axis data of %d bytes at 0x%X" % (n, r.p - 4))
    data = r.raw(n)
    r.p += 8
    texts: List[str] = []
    if P.axid:
        r.str()                                 # axis id
        n = r.u32()
        if n > 100000:
            raise ValueError("%d axis texts at 0x%X" % (n, r.p - 4))
        for _ in range(n):
            # a point text with its translations (as every multilingual
            # string; with none, the 12 bytes read past it before)
            texts.append(r.mlstr() or "")
        r.p += P.axextra
    return Axis(label, units, factor, offset, source, addr, org, size, signed,
                prec, data, texts)


def _record(r: _R) -> Record:
    P = r.P
    r.u8()
    if P.prefix:
        if r.i32() != -1:
            raise ValueError("no record marker at 0x%X" % (r.p - 4))
        r.mlstr()                               # bit texts
        r.u8()
    desc = r.mlstr() or ""
    typ, _h1, org, size, numbase, folder = (r.u32() for _ in range(6))
    if not 1 <= typ <= 16:
        raise ValueError("record type %d at 0x%X" % (typ, r.p - 24))
    name = r.str() or ""
    pre = r.raw(P.precx)
    signed = bool(pre[-3])
    cols, rows, _c2, _c3, prec = (r.u32() for _ in range(5))
    label = r.str() or ""
    units = r.str() or ""
    factor, offset = r.f64(), r.f64()
    addr = r.u32()
    r.u32()                                     # end address
    r.p += P.ztail
    x = _axis(r)
    y = _axis(r)
    # record tail: counts again, display settings, the 01 01 01 marker
    r.p += 6 + 4 + 4 + 4 + 1 + 1 + 8 + 8 + 4 + 1 + 8 + 8 + 8 + 4 + 8
    # three on/off display settings (usually 01 01 01; a Simos 18.1 and an
    # MG1 project carry 01 01 00): anything else means the record was read
    # out of step
    flags = r.raw(3)
    if any(b > 1 for b in flags) or not flags[0]:
        raise ValueError("record tail out of step at 0x%X" % (r.p - 3))
    if P.tailn >= 0:
        r.p += 4 + P.tailn
    return Record(typ, org, size, numbase, folder, name, desc, cols, rows,
                  signed, prec, label, units, factor, offset, addr, x, y)


def _walk(d: bytes, start: int, count: int, prof: _Profile,
          end: Optional[int] = None) -> Tuple[List[Record], int, Optional[str]]:
    r = _R(d, start, prof)
    out: List[Record] = []
    for i in range(count):
        try:
            out.append(_record(r))
        except (ValueError, struct.error, IndexError) as ex:
            return out, r.p, "record %d of %d: %s" % (i + 1, count, ex)
    if end is not None and r.p != end and d[r.p:r.p + 4] != FOLDERS_MAGIC:
        return out, r.p, "the map list ends at 0x%X, not at the next section" % r.p
    return out, r.p, None


def read_records(d: bytes, start: int, count: int, ver: int,
                 kp: bool = False) -> Tuple[List[Record], int, _Profile, List[str]]:
    """Every record of a list of ``count`` starting at ``start``: with the
    profile of format ``ver`` first, then the others. Returns ``(records,
    end, profile, problems)`` -- the longest parse when none reaches the
    end."""
    first = _profile_for(ver)
    order = [first] + [p for p in PROFILES if p is not first]
    best = None
    for prof in order:
        recs, end, err = _walk(d, start, count, prof,
                               end=len(d) if kp else None)
        if err is None:
            return recs, end, prof, []
        if best is None or len(recs) > len(best[0]):
            best = (recs, end, prof, [err])
    return best


# --------------------------------------------------------------------- header
def _hstr(d: bytes, p: int, nul: bool) -> Tuple[Optional[str], int]:
    n = struct.unpack_from("<i", d, p)[0]
    p += 4
    if n < 0:
        return None, p
    if n > 4096:
        raise DefinitionError("WinOLS header: bad string at 0x%X" % (p - 4))
    t = d[p:p + n].decode("latin-1")
    p += n
    if nul and n and p < len(d) and d[p] == 0:
        p += 1
    return t, p


@dataclass
class Header:
    version: int
    vehicle: Dict[str, str]
    ecu: Dict[str, str]
    filename: str
    current_version: str
    end: int


def read_header(d: bytes) -> Header:
    if not is_winols(d):
        raise DefinitionError("not a WinOLS file")
    ver = format_version(d)
    nul = ver < 400
    p = 24 if ver >= 200 else 20
    vals = []
    for _ in range(16):
        t, p = _hstr(d, p, nul)
        vals.append((t or "").strip())
    p += 8                                      # created / changed (unix time)
    fname, p = _hstr(d, p, nul)
    _olsver, p = _hstr(d, p, nul)
    _x, p = _hstr(d, p, nul)
    cur, p = _hstr(d, p, nul)
    m = re.match(r"^\d+ \((.*)\)$", (cur or "").strip())
    return Header(ver, dict(zip(VEHICLE_FIELDS, vals[:8])),
                  dict(zip(ECU_FIELDS, vals[8:])), fname or "",
                  m.group(1) if m else "", p)


def project_info(h: Header) -> Dict[str, str]:
    """The header's details as project information (``Project.info`` keys)."""
    v, e = h.vehicle, h.ecu
    info = {k: v[k] for k in ("make", "model", "year", "gearbox") if v.get(k)}
    if v.get("engine"):
        info["engine"] = v["engine"]
    if v.get("power"):
        info["power"] = v["power"]
    if v.get("fuel"):
        info["Fuel"] = v["fuel"]
    if v.get("displacement"):
        info["Displacement"] = v["displacement"]
    unit = " ".join(x for x in (e.get("producer"), e.get("build")) if x)
    if unit:
        info["ecu"] = unit
    parts = []
    for key, label in (("part", "part number"), ("part2", "part number"),
                       ("software", "software")):
        val = e.get(key)
        if val and "%s %s" % (label, val) not in parts:
            parts.append("%s %s" % (label, val))
    if parts:
        info["notes"] = "Control unit " + ", ".join(parts) + "."
    return info


# -------------------------------------------------------------------- folders
def _folder_entry(d: bytes, p: int, nul: bool, ml: bool):
    """``(id, resource, name, end)`` of the folder entry at ``p`` (up to its
    version bytes), or ValueError."""
    fid, res = struct.unpack_from("<II", d, p)
    if fid > 1000000 or res > 100000:
        raise ValueError("not a folder entry")
    r = _R(d, p + 8, _Profile("f", nul, False, ml, 0, 0, 0, False, 0, 0, ml, 0))
    name = r.mlstr() if ml else r.str()
    if name is None or any(ord(c) < 0x20 and c not in "\t\r\n" for c in name):
        raise ValueError("not a folder name")
    if not name and res not in (9000, 9001, 9002):
        raise ValueError("unnamed folder")
    r.u8()
    nv = r.u32()
    if nv > 1000:
        raise ValueError("folder versions")
    return fid, res, name, r.p + nv


def _folder_list(d: bytes, p: int, nul: bool, ml: bool, tail: int, n: int):
    """Every entry of a folder list read with one layout: ``[(id, name,
    parent)]`` -- ValueError when one does not parse."""
    out = []
    for _ in range(n):
        fid, _res, name, q = _folder_entry(d, p, nul, ml)
        p = q + tail
        if p > len(d):
            raise ValueError("folder list past the end")
        out.append((fid, (name or "").strip(), q))
    return out


def read_folder_tree(d: bytes, p: int, prof: _Profile, ver: int = 0
                     ) -> Tuple[Dict[int, str], Dict[int, int]]:
    """The folder section after the map list: ``({id: name}, {id: parent
    id})``. The section is ``11 88 63 98``, ``u32 k`` and k map templates
    (usually none), then ``11 88 63 98``, ``u32 n`` and n entries --
    ``u32 id``, ``u32`` resource id (9000 *My maps*, 9001 *Hexdump*, 9002
    *Potential maps*), the name (a ``str``; an ``mlstr`` in some formats),
    ``u8``, ``u32`` version count + that many bytes, then a tail whose
    length differs between formats (0 / 15 / 25 / 29 bytes; from 25 bytes
    its ``i32`` 8 bytes before the next entry is the parent folder, -1 at
    the top). The layout is the one that reads every entry in sequence:
    the profile's first, then the others."""
    if d[p:p + 4] != FOLDERS_MAGIC:
        return {}, {}
    k = struct.unpack_from("<I", d, p + 4)[0]
    starts = []
    if d[p + 8:p + 12] == FOLDERS_MAGIC:
        starts.append(p + 8)
    if k:
        # map templates come first: the folder list is at a later marker
        q = p + 8
        for _ in range(16):
            q = d.find(FOLDERS_MAGIC, q)
            if q < 0:
                break
            if q not in starts:
                starts.append(q)
            q += 4
    nul = ver < 400 if ver else prof.nul
    combos = []
    for ml in (prof.folder_ml, not prof.folder_ml):
        for t in (prof.folder_tail, 0, 15, 25, 29) + tuple(range(48)):
            if (ml, t) not in combos:
                combos.append((ml, t))
    for q in starts:
        n = struct.unpack_from("<I", d, q + 4)[0]
        if not 0 < n <= 200000:
            continue
        for ml, tail in combos:
            try:
                ents = _folder_list(d, q + 8, nul, ml, tail, n)
            except (ValueError, struct.error, IndexError):
                continue
            names = {fid: name for fid, name, _q in ents}
            parents = {}
            # a folder inside another names it in its tail, where a folder
            # at the top (the first, Hexdump) has -1; formats without it
            # have no -1 there
            o = d.find(NO_PARENT, ents[0][2], ents[0][2] + tail)
            if tail >= 4 and o >= 0 and o + 4 <= ents[0][2] + tail:
                o -= ents[0][2]
                for fid, _n, q2 in ents:
                    par = struct.unpack_from("<i", d, q2 + o)[0]
                    if par >= 0 and par != fid and par in names:
                        parents[fid] = par
            return names, parents
    return {}, {}


def read_folders(d: bytes, p: int, prof: _Profile) -> Dict[int, str]:
    return read_folder_tree(d, p, prof)[0]


# ------------------------------------------------------------------ versions
VERSIONS_MAGIC = b"\x99\x78\x00\x42"
VERSION_SIG = b"\x01\x92\xbc\x93"


@dataclass
class Version:
    """One version of the binary a project holds (the first is the one
    WinOLS imported, normally named Original)."""
    name: str
    comment: str
    data: bytes = field(repr=False, default=b"")


def _vstr(d: bytes, p: int, nul: bool) -> Tuple[str, int]:
    n = struct.unpack_from("<i", d, p)[0]
    p += 4
    if n < 0:
        return "", p
    if n > 0x10000 or p + n > len(d):
        raise ValueError("version text")
    t = d[p:p + n].decode("latin-1")
    p += n
    if nul and p < len(d) and d[p] == 0:
        p += 1
    return t, p


def read_versions(d: bytes, start: int, limit: int, ver: int
                  ) -> Tuple[int, List[Tuple[str, str]]]:
    """The version list just before the map list: ``(size of one version's
    image, [(name, comment)])``, or ``(0, [])``. It is ``u32`` image size,
    ``99 78 00 42``, ``u32 n``, then n entries -- ``str`` name, ``str``
    comment, ``01 92 BC 93``, two times and (in later formats) the checksum
    state. Every version's image is stored in full, one after the other,
    after the image marker at the end of the file."""
    q = d.rfind(VERSIONS_MAGIC, start, limit)
    if q < 4:
        return 0, []
    size, = struct.unpack_from("<I", d, q - 4)
    n, = struct.unpack_from("<I", d, q + 4)
    if not 0 < n <= 1000 or not size:
        return 0, []
    nul = ver < 400
    out: List[Tuple[str, str]] = []
    p = q + 8
    for i in range(n):
        got = None
        for skip in range(0, 64 if i else 1):
            try:
                name, r = _vstr(d, p + skip, nul)
                comment, r = _vstr(d, r, nul)
            except (ValueError, struct.error):
                continue
            if d[r:r + 4] == VERSION_SIG and name.isprintable():
                got = (name, comment, r + 12)
                break
        if got is None:
            out += [("Original" if k == 0 else "Version %d" % k, "")
                    for k in range(len(out), n)]
            break
        out.append((got[0].strip(), got[1].strip()))
        p = got[2]
    return size, out


# ------------------------------------------------------------------ the files
@dataclass
class WinOlsFile:
    kind: str                       # "ols" / "kp"
    header: Header
    records: List[Record]
    folders: Dict[int, str]
    image: bytes = b""              # .ols: the embedded binary (the original)
    segments: List[Tuple[int, int]] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    declared: int = 0
    #: .ols: every version of the binary, the original first
    versions: List[Version] = field(default_factory=list)
    #: folder id -> the folder it is in (nested folders only)
    parents: Dict[int, int] = field(default_factory=dict)
    #: the memory areas the image was put together from (``#1``, ``#2``
    #: ...: ``(name, start, end)`` as image offsets), when there are several
    areas: List[Tuple[str, int, int]] = field(default_factory=list)


def _segments(d: bytes, limit: int, image_len: int, nul: bool) -> List[Tuple[int, int]]:
    """The image's segments as WinOLS imported them (``#1``, ``#2`` ... with
    start and end addresses), when they tile the image exactly."""
    def label(k):
        t = ("#%d" % k).encode()
        return struct.pack("<I", len(t)) + t + (b"\0" if nul else b"")
    p = d.find(label(1), 0, limit)
    if p < 12:
        return []
    n = struct.unpack_from("<I", d, p - 12)[0]
    if not 2 <= n <= 16:
        return []
    segs, pos = [], p
    for k in range(1, n + 1):
        if k > 1:
            pos = d.find(label(k), pos, limit)
            if pos < 8:
                return []
        start, end = struct.unpack_from("<II", d, pos - 8)
        if end < start or (segs and start != segs[-1][1] + 1):
            return []
        segs.append((start, end))
    if segs[0][0] != 0 or segs[-1][1] + 1 != image_len:
        return []
    return segs


def _areas(d: bytes, limit: int, image_len: int) -> List[Tuple[str, int, int]]:
    """The memory areas ``#1``, ``#2`` ... in the header that tile the
    image, in order (areas outside the file, such as RAM, are passed
    over): ``[(name, start, end)]``, or [] with fewer than two."""
    out: List[Tuple[str, int, int]] = []
    pos = 0
    for k in range(1, 17):
        t = ("#%d" % k).encode()
        hit = d.find(struct.pack("<I", len(t)) + t, pos, limit)
        if hit < 8:
            break
        start, end = struct.unpack_from("<II", d, hit - 8)
        if end < start or start != (out[-1][2] + 1 if out else 0):
            return []
        out.append(("#%d" % k, start, end))
        pos = hit + 4 + len(t)
        if end + 1 >= image_len:
            break
    if len(out) < 2 or out[-1][2] + 1 != image_len:
        return []
    return out


def parse(data: bytes) -> WinOlsFile:
    """Read an ``.ols`` project or ``.kp`` map pack."""
    d = bytes(data)
    h = read_header(d)
    p = d.find(RECORDS_MAGIC, h.end)
    if p < 0:
        raise DefinitionError("WinOLS file without a map list")
    best = None
    for _ in range(8):                  # the marker could also occur by chance
        wf = _parse_at(d, h, p)
        if not wf.problems:
            return wf
        if best is None or len(wf.records) > len(best.records):
            best = wf
        p = d.find(RECORDS_MAGIC, p + 4)
        if p < 0:
            break
    return best


def _parse_at(d: bytes, h: Header, p: int) -> WinOlsFile:
    ver = h.version
    start = p + 4
    if ver >= 200:
        start += 1
    count = struct.unpack_from("<I", d, start)[0]
    if d[start + 4:start + 8] == b"PK\x03\x04":
        # a map pack: the list is a ZIP ("intern": u8, u32 count, records)
        try:
            intern = zipfile.ZipFile(io.BytesIO(
                d[start + 4:start + 4 + count])).read("intern")
        except (zipfile.BadZipFile, KeyError) as ex:
            raise DefinitionError("map pack without its record archive (%s)" % ex)
        n = struct.unpack_from("<I", intern, 1)[0]
        recs, _end, prof, probs = read_records(intern, 5, n, ver, kp=True)
        folders, parents = read_folder_tree(d, start + 4 + count, prof, ver)
        return WinOlsFile("kp", h, recs, folders, problems=probs, declared=n,
                          parents=parents)
    recs, end, prof, probs = read_records(d, start + 4, count, ver)
    folders, parents = read_folder_tree(d, end, prof, ver)
    versions = _versions(d, h, p, end)
    image = versions[0].data if versions else b""
    segs = _segments(d, p, len(image), prof.nul) if image else []
    areas = _areas(d, p, len(image)) if image else []
    return WinOlsFile("ols", h, recs, folders, image, segs, probs, count,
                      versions=versions, parents=parents, areas=areas)


def _versions(d: bytes, h: Header, limit: int, end: int = 0) -> List[Version]:
    """Every version of the binary in a project, the original first. The
    images follow the image marker to the end of the file, one whole image
    per version; a project whose version list does not account for what is
    there keeps it as one image."""
    size, names = read_versions(d, h.end, limit, h.version)
    if size and names:
        at = len(d) - size * len(names) - 4
        if at > limit and d[at:at + 4] == IMAGE_SIG:
            return [Version(n, c, d[at + 4 + i * size:at + 4 + (i + 1) * size])
                    for i, (n, c) in enumerate(names)]
    sig = d.find(IMAGE_SIG, end or limit)
    if sig < 0:
        return []
    region = d[sig + 4:]
    if not region:
        return []
    if size and names and len(region) > size and len(region) % size == 0:
        k = len(region) // size
        names = (names + [("Version %d" % i, "") for i in range(len(names), k)])[:k]
        return [Version(n, c, region[i * size:(i + 1) * size])
                for i, (n, c) in enumerate(names)]
    name, comment = names[0] if names else ("Original", "")
    return [Version(name or "Original", comment, region)]


def scan_images(data: bytes) -> Optional[WinOlsFile]:
    """A project's header, version list, embedded images and declared map
    count, without reading the maps (fast, for a summary). None when the
    images cannot be found that way (:func:`parse` then)."""
    d = bytes(data)
    h = read_header(d)
    p = d.find(RECORDS_MAGIC, h.end)
    if p < 0:
        return None
    start = p + 4 + (1 if h.version >= 200 else 0)
    count = struct.unpack_from("<I", d, start)[0]
    if d[start + 4:start + 8] == b"PK\x03\x04":
        return None
    size, names = read_versions(d, h.end, p, h.version)
    if not (size and names):
        return None
    at = len(d) - size * len(names) - 4
    if at <= p or d[at:at + 4] != IMAGE_SIG:
        return None
    vs = _versions(d, h, p)
    image = vs[0].data
    prof = _profile_for(h.version)
    return WinOlsFile("ols", h, [], {}, image, _segments(d, p, len(image), prof.nul),
                      declared=count, versions=vs, areas=_areas(d, p, len(image)))


# --------------------------------------------------------------- definition
def _storage(org: int, signed: bool, addr: Optional[int], factor: float,
             offset: float, units: str, size: int = 0) -> Optional[Storage]:
    spec = ORG.get(org)
    if spec is None and size == 1:
        # an organisation this reader has no entry for (9: Valeo projects'
        # on/off parameters), whose record says each element is one byte --
        # which has no byte order to get wrong
        spec = (8, "", False)
    if spec is None:
        return None
    bits, endian, flt = spec
    return Storage(addr=addr, bits=bits, signed=signed and not flt, float=flt,
                   endian=endian, factor=factor if factor else 1.0,
                   offset=offset, units=units)


def _axis_def(a: Axis, count: int) -> Optional[AxisDef]:
    if count <= 1:
        return None
    name = a.label if a.label not in ("-", "?") else ""
    units = a.units if a.units not in ("-", "?") else ""
    if a.source in (1, 3) and a.addr:
        st = _storage(a.org, a.signed, a.addr, a.factor, a.offset, units, a.size)
        if st is not None:
            # A set "signed" is WinOLS's own; an unset one is not proof of
            # unsigned -- projects built from a DAMOS leave it unset on
            # axes whose points are negative -- so it is left to the values.
            if not a.signed:
                st.signed = None
            return AxisDef(count, values=st, name=name,
                           labels=a.texts[:count] if any(a.texts) else None)
    if a.source in (4, 8) and a.data:
        st = _storage(a.org, a.signed, None, a.factor, a.offset, units, a.size)
        if st is not None and len(a.data) >= count * st.size:
            fmt = (st.endian or "<") + {(8, False): "B", (16, False): "H",
                                        (32, False): "I", (32, True): "f"}[
                (st.bits, st.float)]
            if st.signed:
                fmt = fmt.lower()
            vals = [struct.unpack_from(fmt, a.data, i * st.size)[0]
                    * st.factor + st.offset for i in range(count)]
            return AxisDef(count, fixed=vals, name=name,
                           labels=a.texts[:count] if any(a.texts) else None)
    if a.texts and any(a.texts):
        return AxisDef(count, fixed=[float(i) for i in range(count)],
                       labels=a.texts[:count], name=name)
    return None


def codeblocks(records: List[Record]) -> List[int]:
    """The code block of each record (all 0 when the project has one).

    A project WinOLS made for an ECU that keeps its data more than once
    (an EDC15's three code blocks) lists every map once per block, the
    copies a fixed distance apart, the blocks one after the other. The
    number of blocks and that distance are the ones most names repeat with;
    a block ends half-way between the last record of one copy and the first
    of the next."""
    by: Dict[str, List[int]] = {}
    for i, r in enumerate(records):
        if r.type != 1 and r.addr and r.name:
            by.setdefault(r.name, []).append(i)

    def addrs(idx):
        out = []
        for i in idx:
            a = records[i].addr
            if a not in out:
                out.append(a)
        return out
    cands: Dict[Tuple[int, int], int] = {}
    for idx in by.values():
        a = addrs(idx)
        if len(a) < 2:
            continue
        deltas = {b - c for b, c in zip(a[1:], a)}
        if len(deltas) == 1:
            dl = deltas.pop()
            if dl > 0:
                cands[(len(a), dl)] = cands.get((len(a), dl), 0) + 1
    zero = [0] * len(records)
    if not cands:
        return zero
    (n, delta), votes = max(cands.items(), key=lambda kv: kv[1])
    if votes < max(2, len(by) * 0.2):
        return zero
    bounds = []
    refs = []
    for idx in by.values():
        a = addrs(idx)
        if len(a) == n and {b - c for b, c in zip(a[1:], a)} == {delta}:
            refs.append((idx, a))
    for b in range(n - 1):
        ends = [max(i for i in idx if records[i].addr == a[b]) for idx, a in refs]
        starts = [min(i for i in idx if records[i].addr == a[b + 1]) for idx, a in refs]
        bounds.append((max(ends) + min(starts)) / 2.0)
    out = []
    for i in range(len(records)):
        k = 0
        while k < len(bounds) and i > bounds[k]:
            k += 1
        out.append(k)
    return out


def folder_path(wf: WinOlsFile, fid: int) -> List[str]:
    """The names from the top folder down to folder ``fid``."""
    path, seen = [], set()
    while fid in wf.folders and fid not in seen:
        seen.add(fid)
        if wf.folders[fid]:
            path.append(wf.folders[fid])
        fid = wf.parents.get(fid, -1)
    return path[::-1]


#: the category of a map's code block (a project with several)
CODEBLOCK = "Codeblock "
#: WinOLS's folder for maps the user filed nowhere (resource 9000)
MY_MAPS = "My maps"


def category_order(names) -> List[str]:
    """Categories in the order an XDF numbers them: the code
    blocks first, then the folders by name (case-blind)."""
    names = list(dict.fromkeys(names))

    def block(n):
        rest = n[len(CODEBLOCK):]
        return n.startswith(CODEBLOCK) and rest.isdigit()
    blocks = sorted((n for n in names if block(n)), key=lambda n: int(n[len(CODEBLOCK):]))
    rest = sorted((n for n in names if not block(n)), key=lambda n: n.lower())
    return blocks + rest


def to_definition(wf: WinOlsFile, title: str = "") -> Definition:
    """The maps of a parsed WinOLS file as a :class:`Definition`.

    Categories: in a project that lists every map once per code block, the
    map's block first (``Codeblock 0`` ...), then its folder (after the
    folders it is inside, top first). A map listed twice in the same block,
    folder and place stays listed twice: every map the project lists is
    exported, as WinOLS shows it. ``info["category_order"]`` holds the
    categories in the order an XDF numbers them."""
    kind = wf.kind
    defn = Definition(type=kind, title=title or wf.header.filename or
                      "WinOLS %s" % kind.upper(),
                      info=project_info(wf.header))
    defn.warnings.extend(wf.problems)
    skipped = 0
    blocks = codeblocks(wf.records)
    multi = max(blocks, default=0) > 0
    for rec, cb in zip(wf.records, blocks):
        if rec.type == 1 or not rec.addr:
            continue                            # hexdump view / no data
        z = _storage(rec.org, rec.signed, rec.addr, rec.factor, rec.offset,
                     rec.units if rec.units not in ("-", "?") else "", rec.size)
        if z is None:
            skipped += 1
            continue
        cols, rows = max(1, rec.cols), max(1, rec.rows)
        if rec.type == 2 and cols * rows == 1:
            k = "constant"
        elif rows == 1 or rec.type == 3:
            k = "curve"
            cols, rows = cols * rows, 1
        else:
            k = "map"
        name = rec.name.strip() or rec.description.strip() or \
            "map @0x%X" % rec.addr
        desc = rec.description.strip() if rec.name.strip() else ""
        if rec.label and rec.label not in ("-", "?") and rec.label != desc:
            # the value's own label, as the "Z:" part the panel shows
            desc = (desc + " | " if desc else "") + "Z: " + rec.label
        # a map in no folder is in WinOLS's own "My maps" (as WinOLS shows it)
        cats = (["%s%d" % (CODEBLOCK, cb)] if multi else []) + \
            (folder_path(wf, rec.folder) or [MY_MAPS])
        t = TableDef(name=name, kind=k, z=z, rows=rows, cols=cols,
                     description=desc,
                     categories=cats,
                     x=_axis_def(rec.x, cols) if k != "constant" else None,
                     y=_axis_def(rec.y, rows) if k == "map" else None,
                     # WinOLS's own decimal places, as WinOLS's map view shows them
                     decimals=rec.precision if 0 < rec.precision < 16 else None)
        defn.tables.append(t)
    if skipped:
        defn.warnings.append("%d maps use a data organisation this version "
                             "does not read" % skipped)
    order = category_order(c for t in defn.tables for c in t.categories)
    if order:
        defn.info["category_order"] = "\n".join(order)
    return defn


def load(data: bytes, title: str = "") -> Definition:
    wf = parse(data)
    defn = to_definition(wf, title)
    if wf.image:
        # an .ols carries the image its maps were made on (file offsets),
        # and every other version of it
        defn.reference = (bytes(wf.image), 0)
        defn.images = [(v.name or "Original", bytes(v.data), 0) for v in wf.versions]
    return defn


__all__ = ["is_winols", "parse", "load", "to_definition", "read_header",
           "project_info", "scan_images", "codeblocks", "category_order",
           "WinOlsFile", "Version", "Record", "Axis", "Header", "ORG"]

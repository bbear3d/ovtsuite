# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""WinOLS projects (``.ols``) and map packs (``.kp``), read structurally.

Both start with the same header -- ``u32 11, "WinOLS File\\0"``, ``u32``
format version -- and keep their maps as one list of records, written field
after field with no padding. The record layout was reconstructed from
projects of format versions 114, 117, 292, 397, 479 and 597 (OLS 4 ... 5.x);
every record of every sample parses in sequence and the list ends exactly
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
             record tail (format >= 397: + u32, 36 / 48 / 64 bytes)
    axis     mlstr label, str units, f64 factor, f64 offset,
             u32 source (0 none, 1/3 in the binary, 4 values here,
             8 texts), u32 address, u32 data organisation, u32 size,
             u32 number base, 2 / 14 bytes, i32 precision, u8 signed,
             u32 n + n bytes of values, i32, i32,
             [str id, u32 n + n x (str, 12 bytes) texts]   (format >= 397)
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
    _Profile("479", False, True, True, 117, 32, 14, True, 4, 48, True, 25),
    _Profile("397", True, True, True, 117, 32, 14, True, 0, 36, False, 25),
    _Profile("292", True, True, False, 61, 32, 14, False, 0, -1, False, 15),
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
            texts.append(r.str() or "")
            r.p += 12
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
    if r.raw(3) != b"\x01\x01\x01":
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
def read_folders(d: bytes, p: int, prof: _Profile) -> Dict[int, str]:
    if d[p:p + 4] != FOLDERS_MAGIC or d[p + 8:p + 12] != FOLDERS_MAGIC:
        return {}
    r = _R(d, p + 12, prof)
    out: Dict[int, str] = {}
    try:
        n = r.u32()
        for _ in range(min(n, 100000)):
            fid, _res = r.u32(), r.u32()
            name = r.mlstr() if prof.folder_ml else r.str()
            r.u8()
            nv = r.u32()
            if nv > 1000:
                break
            r.p += nv + prof.folder_tail
            out[fid] = (name or "").strip()
    except (ValueError, struct.error, IndexError):
        pass
    return out


# ------------------------------------------------------------------ the files
@dataclass
class WinOlsFile:
    kind: str                       # "ols" / "kp"
    header: Header
    records: List[Record]
    folders: Dict[int, str]
    image: bytes = b""              # .ols: the embedded binary
    segments: List[Tuple[int, int]] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    declared: int = 0


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
        folders = read_folders(d, start + 4 + count, prof)
        return WinOlsFile("kp", h, recs, folders, problems=probs, declared=n)
    recs, end, prof, probs = read_records(d, start + 4, count, ver)
    folders = read_folders(d, end, prof)
    sig = d.find(IMAGE_SIG, end)
    image = d[sig + 4:] if sig >= 0 else b""
    segs = _segments(d, p, len(image), prof.nul) if image else []
    return WinOlsFile("ols", h, recs, folders, image, segs, probs, count)


# --------------------------------------------------------------- definition
def _storage(org: int, signed: bool, addr: Optional[int], factor: float,
             offset: float, units: str) -> Optional[Storage]:
    spec = ORG.get(org)
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
        st = _storage(a.org, a.signed, a.addr, a.factor, a.offset, units)
        if st is not None:
            # A set "signed" is WinOLS's own; an unset one is not proof of
            # unsigned -- projects built from a DAMOS leave it unset on
            # axes whose points are negative -- so it is left to the values.
            if not a.signed:
                st.signed = None
            return AxisDef(count, values=st, name=name,
                           labels=a.texts[:count] if any(a.texts) else None)
    if a.source in (4, 8) and a.data:
        st = _storage(a.org, a.signed, None, a.factor, a.offset, units)
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


def to_definition(wf: WinOlsFile, title: str = "") -> Definition:
    """The maps of a parsed WinOLS file as a :class:`Definition`."""
    kind = wf.kind
    defn = Definition(type=kind, title=title or wf.header.filename or
                      "WinOLS %s" % kind.upper(),
                      info=project_info(wf.header))
    defn.warnings.extend(wf.problems)
    skipped = 0
    for rec in wf.records:
        if rec.type == 1 or not rec.addr:
            continue                            # hexdump view / no data
        z = _storage(rec.org, rec.signed, rec.addr, rec.factor, rec.offset,
                     rec.units if rec.units not in ("-", "?") else "")
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
        folder = wf.folders.get(rec.folder, "")
        t = TableDef(name=name, kind=k, z=z, rows=rows, cols=cols,
                     description=desc,
                     categories=[folder] if folder else [],
                     x=_axis_def(rec.x, cols) if k != "constant" else None,
                     y=_axis_def(rec.y, rows) if k == "map" else None,
                     decimals=max(0, rec.precision - 1)
                     if 0 < rec.precision < 12 else None)
        defn.tables.append(t)
    if skipped:
        defn.warnings.append("%d maps use a data organisation this version "
                             "does not read" % skipped)
    return defn


def load(data: bytes, title: str = "") -> Definition:
    wf = parse(data)
    defn = to_definition(wf, title)
    if wf.image:
        # an .ols carries the image its maps were made on (file offsets)
        defn.reference = (bytes(wf.image), 0)
    return defn


__all__ = ["is_winols", "parse", "load", "to_definition", "read_header",
           "project_info", "WinOlsFile", "Record", "Axis", "Header", "ORG"]

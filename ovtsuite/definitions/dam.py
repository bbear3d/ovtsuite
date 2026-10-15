# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch DAMOS (``.dam``) descriptions.

A DAMOS file is text. Records end with ``;`` (outside ``{...}``, which may
hold anything), fields are separated by ``,`` or a line break (long records
wrap), and a record is a run of ``/TAG`` groups:

* the **header** (everything up to the first ``;``): ``/PBA`` the program
  base address, ``/UP`` the processor (byte order), ``/RFG``, and ``/SND``
  the memory segments (``CODE1 $80000 $BFFFF`` ...);
* one record per **parameter**::

      n,/SPZ,name,{description},kind
        $pointer,$address
      /SPW,unit,type,min,max                 values
      /SPX,unit,type,min,max,input           X axis
      /SPY,unit,type,min,max,input           Y axis
      /FKX,max points,0,0   /FKY,...
      /ABL,n;

  ``kind`` is 1 a value, 2 a curve, 3 a map, 6 an axis shared by group
  maps, 7 / 8 a group curve / map (values only; ``input`` names the shared
  axes by their record number; no group curve was in the DAMOS this was
  checked against) and 12 text. ``type`` is the storage:
  1 / 2 / 4 bytes, negative when signed (the text length for kind 12).
  ``unit`` numbers a ``/REG`` record, ``input`` (kinds 2 and 3) a ``/SRC``
  one. Addresses are relative to the program base address; ``$pointer`` is
  where the program finds the parameter (a pointer table), ``$address``
  where it is stored;
* ``n,/REG,name,{description},6,0,{units},integer digits,digits,min,max``
  ``/REP,p1,p2,p3,p4,p5,p6;`` -- a conversion: the stored value is
  ``(p1*x + p2) / (p3*x + p4) + p5``;
* ``n,/SRC,name,{description},$address,type,unit,...;`` -- a measured
  variable, the input of an axis;
* ``/FKT,name,{description}`` and two lists of names -- a function, which
  becomes a category.

A curve, a map or a shared axis keeps its point counts in the image, in
front of its points: per axis the address of its input variable, the number
of points and the points; the values follow (``/FKX`` and ``/FKY`` are only
the most points there may be). The counts and the addresses that follow from
them are read from the image when the description is applied to one
(``TableDef.dynamic``).
"""

from __future__ import annotations

import copy
import re
from typing import Dict, List, Optional, Tuple

from .model import AxisDef, Definition, DefinitionError, Storage, TableDef

#: processors that store values least significant byte first
_LSB_CPUS = {"s80166", "s80c166", "s80c167", "s80c196", "st10", "st10f",
             "c167", "c165", "c166", "c16x"}
#: records never hold more points than this (a guard against a misread)
_MAX_POINTS = 1024


def is_dam(data: bytes) -> bool:
    head = bytes(data[:16384]).decode("latin-1", "replace")
    return "/SPZ," in head or ("/PBA," in head and "/UP," in head)


# ------------------------------------------------------------------ reading
_TAG = re.compile(r"^/([A-Za-z]{2,3})(?![A-Za-z0-9_])")


def _records(text: str) -> List[List[List[str]]]:
    """The file as records, each a list of ``/TAG`` groups (the fields in
    front of the first tag -- a record number -- form a group of their own,
    tagged ``""``)."""
    out: List[List[List[str]]] = []
    fields: List[str] = []
    cur: List[str] = []
    depth = 0

    def end_field():
        f = "".join(cur).strip()
        cur.clear()
        if f:
            fields.append(f)

    def end_record():
        end_field()
        if fields:
            groups: List[List[str]] = [[""]]
            for f in fields:
                m = _TAG.match(f)
                if m:
                    groups.append([m.group(1).upper()])
                    rest = f[m.end():].strip()
                    if rest:
                        groups[-1].append(rest)
                else:
                    groups[-1].append(f)
            if len(groups[0]) == 1:
                groups.pop(0)
            out.append(groups)
            fields.clear()

    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        elif depth == 0:
            if ch in ",\n":
                end_field()
                continue
            if ch == ";":
                end_record()
                continue
            if ch == "\r":
                continue
        cur.append(ch)
    end_record()
    return out


def _hex(s: str) -> Optional[int]:
    m = re.match(r"^\s*\$([0-9A-Fa-f]+)h?\s*$", s or "")
    return int(m.group(1), 16) if m else None


def _num(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _int(s: str) -> int:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def _text(s: str) -> str:
    s = (s or "").strip()
    return s[1:-1].strip() if s.startswith("{") and s.endswith("}") else s


def _n(v: float) -> str:
    return str(int(v)) if v == int(v) and abs(v) < 1e15 else repr(float(v))


def _signed(v: float) -> str:
    return ("+" if v >= 0 else "-") + _n(abs(v))


class _Unit:
    __slots__ = ("name", "desc", "units", "factor", "offset", "equation",
                 "decimals", "note")

    def __init__(self, name="", desc="", units=""):
        self.name, self.desc, self.units = name, desc, units
        self.factor, self.offset, self.equation = 1.0, 0.0, ""
        self.decimals: Optional[int] = None
        self.note = ""

    def set_rep(self, p: List[float]):
        """``stored = (p1*x + p2) / (p3*x + p4) + p5``, solved for x. (A
        sixth coefficient is not part of it: it only appears with
        conversions whose values read correctly without it, and WinOLS
        ignores it as well.)"""
        p1, p2, p3, p4, p5 = (list(p) + [0.0] * 5)[:5]
        if p3 == 0 and p1 != 0:
            self.factor = p4 / p1
            self.offset = -(p2 + p5 * p4) / p1
            return
        if p3 == 0:
            self.note = "conversion %s has no physical value: values shown raw" \
                % self.name
            return
        x = "X" if not p5 else "(X%s)" % _signed(-p5)
        if p1 == 0:
            # x = p2 / (p3 * (X - p5)) - p4 / p3
            eq = "%s/%s" % (_n(p2 / p3), x)
            if p4:
                eq += _signed(-p4 / p3)
        else:
            eq = "(%s*%s%s)/(%s%s*%s)" % (_n(p4), x, _signed(-p2), _n(p1),
                                          _signed(-p3), x)
        self.equation = eq

    def storage(self, **kw) -> Storage:
        return Storage(factor=self.factor, offset=self.offset,
                       equation=self.equation, units=self.units, **kw)


_NO_UNIT = _Unit()


class _Param:
    __slots__ = ("no", "name", "desc", "kind", "pointer", "addr", "sp", "fk")

    def __init__(self):
        self.no = 0
        self.name = self.desc = ""
        self.kind = 0
        self.pointer = self.addr = None
        self.sp: Dict[str, List[str]] = {}
        self.fk: Dict[str, int] = {}

    def spec(self, axis: str) -> Tuple[int, int, int]:
        """``(unit, type, input)`` of ``/SPW`` / ``/SPX`` / ``/SPY``."""
        f = self.sp.get(axis, [])
        return (_int(f[0]) if f else 0, _int(f[1]) if len(f) > 1 else 0,
                _int(f[4]) if len(f) > 4 else 0)


# ------------------------------------------------------------- the tables
class _Layout:
    """Reads a table's point counts from the image (see the module doc) and
    gives the table as stored there."""

    def __init__(self, table: TableDef, kind: int, addr: int,
                 xhead: Optional[int] = None, yhead: Optional[int] = None,
                 transpose: bool = False, xsrc: Optional[int] = None,
                 ysrc: Optional[int] = None):
        self.table, self.kind, self.addr = table, kind, addr
        self.xhead, self.yhead, self.transpose = xhead, yhead, transpose
        #: RAM addresses of the axes' inputs (``/SRC``), stored at the start
        #: of each axis block
        self.xsrc, self.ysrc = xsrc, ysrc

    def evidence(self, read):
        """``(checked, passed)``: the facts the DAMOS states about this
        table's blocks in the image -- each block starts with its input's
        RAM address, then a point count of at most ``/FKX`` / ``/FKY``."""
        t = self.table
        e = t.z.endian or "<"
        heads = []
        if self.kind in (2, 3, 6):
            heads.append((self.addr, self.xsrc, t.cols if self.kind != 6 else t.cols))
            if self.kind == 3:
                n = self._count(read, self.addr, e)
                if n is None:
                    return (2 + (self.xsrc is not None), 0)
                heads.append((self.addr + 4 + n * t.x.values.size, self.ysrc, t.rows))
        else:
            if self.xhead is not None:
                heads.append((self.xhead, self.xsrc, t.cols))
            if self.yhead is not None:
                heads.append((self.yhead, self.ysrc, t.rows))
        checked = passed = 0
        for head, src, mx in heads:
            checked += 1
            n = read(head + 2, 2, False, e)
            if n is not None and 0 < n <= max(1, mx):
                passed += 1
            if src is not None:
                checked += 1
                if read(head, 2, False, e) == src:
                    passed += 1
        return checked, passed

    @staticmethod
    def _count(read, head, endian):
        n = read(head + 2, 2, False, endian)
        return n if n and 0 < n <= _MAX_POINTS else None

    def __call__(self, read):
        t = self.table
        e = t.z.endian or "<"
        new = copy.copy(t)
        new.z = copy.copy(t.z)
        new.dynamic = None
        if self.kind in (2, 3, 6):
            # input address, count, points [, input, count, points], values
            first = self._count(read, self.addr, e)
            if first is None:
                return None
            ax = [(self.addr + 4, first)]
            nxt = self.addr + 4 + first * t.x.values.size
            if self.kind == 3:
                second = self._count(read, nxt, e)
                if second is None:
                    return None
                ax.append((nxt + 4, second))
                nxt += 4 + second * t.y.values.size
            if self.kind == 6:          # the points themselves are the table
                new.z.addr, new.cols, new.rows = ax[0][0], first, 1
                new.x = None
                return new
            new.z.addr = nxt
        else:
            # group curve / map: the counts are the shared axes'
            ax = []
            for head in (self.xhead, self.yhead):
                if head is None:
                    continue
                n = self._count(read, head, e)
                if n is None:
                    return None
                ax.append((head + 4, n))
            if not ax:
                return None
            new.z.addr = self.addr
        axes = [new.x, new.y][:len(ax)]
        out = []
        for a, (addr, n) in zip(axes, ax):
            a = copy.copy(a)
            a.values = copy.copy(a.values)
            a.values.addr, a.count = addr, n
            out.append(a)
        if len(out) == 1:
            new.x, new.cols, new.rows = out[0], out[0].count, 1
        elif self.transpose:
            # stored with the second axis varying fastest: shown with it
            # across, as WinOLS does
            new.y, new.x = out
            new.rows, new.cols = out[0].count, out[1].count
        else:
            new.x, new.y = out
            new.cols, new.rows = out[0].count, out[1].count
        return new


def _locate(checks, data, preferred: int) -> Optional[int]:
    """The base at which (nearly) every block's header holds its input's RAM
    address and a point count within the maximum, or None. An image with
    several datasets (EDC15) fits at each of them: the base the memory
    segments give wins when it is one, else the highest dataset."""
    data = bytes(data)
    n = len(data)

    def ok(base, chk):
        a, src, mx = chk
        o = a - base
        if o < 0 or o + 4 > n or o & 1:
            return False
        return (int.from_bytes(data[o:o + 2], "little") == src
                and 0 < int.from_bytes(data[o + 2:o + 4], "little") <= mx)

    def score(base):
        return sum(ok(base, c) for c in checks)

    need = max(3, int(len(checks) * 0.9))
    if score(preferred) >= need:
        return preferred
    votes: Dict[int, int] = {}
    for a, src, mx in checks[:24]:
        pat = src.to_bytes(2, "little")
        i = data.find(pat)
        while i != -1:
            if not i & 1 and i + 4 <= n and \
                    0 < int.from_bytes(data[i + 2:i + 4], "little") <= mx:
                votes[a - i] = votes.get(a - i, 0) + 1
            i = data.find(pat, i + 1)
    good = [b for b, _v in sorted(votes.items(), key=lambda kv: -kv[1])[:8]
            if score(b) >= need]
    return min(good) if good else None


def load(data: bytes, title: str = "") -> Definition:
    text = bytes(data).decode("latin-1")
    if "/SPZ," not in text:
        raise DefinitionError("not a DAMOS file")
    recs = _records(text)
    header: Dict[str, List[List[str]]] = {}
    params: List[_Param] = []
    units: Dict[int, _Unit] = {}
    sources: Dict[int, Tuple[str, str]] = {}
    src_addr: Dict[int, int] = {}
    functions: List[Tuple[str, List[str]]] = []
    seen_body = False
    for i, rec in enumerate(recs):
        tags = [g[0] for g in rec]
        if i == 0 and "SPZ" not in tags:
            # the header: everything up to the first ";"
            for g in rec:
                if g[0]:
                    header.setdefault(g[0], []).append(g[1:])
            continue
        no = _int(rec[0][1]) if rec[0][0] == "" and len(rec[0]) > 1 else 0
        if "SPZ" in tags:
            seen_body = True
            p = _Param()
            p.no = no
            for g in rec:
                f = g[1:]
                if g[0] == "SPZ":
                    p.name = f[0] if f else ""
                    p.desc = _text(f[1]) if len(f) > 1 else ""
                    p.kind = _int(f[2]) if len(f) > 2 else 0
                    if len(f) > 4:
                        p.pointer, p.addr = _hex(f[3]), _hex(f[4])
                    elif len(f) > 3:
                        p.pointer = p.addr = _hex(f[3])
                elif g[0] in ("SPW", "SPX", "SPY"):
                    p.sp[g[0][2]] = f
                elif g[0] in ("FKX", "FKY"):
                    p.fk[g[0][2]] = _int(f[0]) if f else 0
            params.append(p)
        elif "REG" in tags:
            seen_body = True
            u = _Unit()
            for g in rec:
                f = g[1:]
                if g[0] == "REG":
                    u.name = f[0] if f else ""
                    u.desc = _text(f[1]) if len(f) > 1 else ""
                    u.units = _text(f[4]) if len(f) > 4 else ""
                    if len(f) > 6:
                        u.decimals = max(0, _int(f[6]) - _int(f[5]))
                elif g[0] == "REP":
                    u.set_rep([_num(x) for x in f])
            units[no] = u
        elif "SRC" in tags:
            seen_body = True
            for g in rec:
                if g[0] == "SRC" and len(g) > 2:
                    sources[no] = (g[1], _text(g[2]))
                    if len(g) > 3 and _hex(g[3]) is not None:
                        src_addr[no] = _hex(g[3])
        elif "FKT" in tags:
            seen_body = True
            for g in rec:
                if g[0] == "FKT":
                    f = g[1:]
                    name = f[0] if f else ""
                    desc = _text(f[1]) if len(f) > 1 else ""
                    members = [x for x in f[2:] if not x.lstrip("-").isdigit()]
                    functions.append(("%s: %s" % (name, desc) if desc else name,
                                      members))
        elif not seen_body:
            for g in rec:
                if g[0]:
                    header.setdefault(g[0], []).append(g[1:])

    def first(tag: str) -> str:
        v = header.get(tag)
        return v[0][0].strip() if v and v[0] else ""

    pba = _hex(first("PBA"))
    cpu = _text(first("UP")).lower().lstrip("_")
    endian = "<" if (cpu in _LSB_CPUS or not cpu) else ">"
    tops = []
    for f in header.get("SND", []):
        m = re.match(r"^\s*(\w+)\s+\$([0-9A-Fa-f]+)\s+\$([0-9A-Fa-f]+)\s*$",
                     f[0] if f else "")
        if m and m.group(1).upper().startswith(("CODE", "DATA")):
            tops.append(int(m.group(3), 16) + 1)
    transpose = first("RFG") != "1"

    defn = Definition(type="dam", title=title or _text(first("PRO")) or "DAMOS")
    defn.info.update({k: v for k, v in (
        ("processor", _text(first("UP"))), ("project", _text(first("PRO"))),
        ("program", _text(first("YNR")))) if v})
    if pba is None:
        defn.warnings.append("no /PBA program base address: addresses are "
                             "taken as image offsets")
        pba = 0
    defn.info["pba"] = "0x%X" % pba
    # Addresses are kept as processor addresses (PBA + address). The image
    # ends where the highest code / data segment does.
    if tops:
        top = max(tops)
        defn.base_for = lambda size, _t=top: _t - size
        defn.info["top"] = "0x%X" % top
    elif pba:
        # no segments: the image is taken to end at twice its size (a 512 KB
        # image at 0x80000-0xFFFFF)
        defn.base_for = lambda size: size

    cats: Dict[str, str] = {}
    for label, members in functions:
        for m in members:
            cats.setdefault(m, label)
    by_no = {p.no: p for p in params}
    notes = set()

    def unit(no: int) -> _Unit:
        u = units.get(no, _NO_UNIT) if no else _NO_UNIT
        if u.note:
            notes.add(u.note)
        return u

    def storage(no_unit: int, typ: int, addr: Optional[int]) -> Optional[Storage]:
        if abs(typ) not in (1, 2, 4):
            return None
        st = unit(no_unit).storage(addr=addr, bits=abs(typ) * 8, signed=typ < 0)
        st.endian = endian if abs(typ) > 1 else ""
        return st

    def axis_name(src: int) -> str:
        s = sources.get(src)
        return ("%s (%s)" % (s[0], s[1]) if s[1] else s[0]) if s else ""

    skipped = 0
    for p in params:
        if p.addr is None:
            skipped += 1
            continue
        addr = pba + p.addr
        wu, wt, _ = p.spec("W")
        xu, xt, xin = p.spec("X")
        yu, yt, yin = p.spec("Y")
        cat = [cats[p.name]] if p.name in cats else []
        if p.kind == 1:
            z = storage(wu, wt, addr)
            if z is None:
                skipped += 1
                continue
            defn.tables.append(TableDef(
                name=p.name, kind="constant", z=z, description=p.desc,
                categories=cat, decimals=unit(wu).decimals))
            continue
        if p.kind == 6:
            # a shared axis: its points, shown as a table of their own
            z = storage(xu, xt, None)
            if z is None:
                skipped += 1
                continue
            t = TableDef(name=p.name, kind="curve", z=z, description=p.desc,
                         categories=cat, cols=max(1, p.fk.get("X", 1)),
                         decimals=unit(xu).decimals, record=addr)
            t.x = AxisDef(1, values=copy.copy(z))
            t.dynamic = _Layout(t, 6, addr, xsrc=src_addr.get(p.spec("X")[2]))
            defn.tables.append(t)
            continue
        if p.kind == 12 and 0 < wt <= 256:
            # text: as many characters as the type says
            defn.tables.append(TableDef(
                name=p.name, kind="curve", cols=wt, description=p.desc,
                categories=cat, text_kind="ascii",
                z=Storage(addr=addr, bits=8)))
            continue
        if p.kind not in (2, 3, 7, 8):
            skipped += 1
            continue
        z = storage(wu, wt, None)
        if z is None:
            skipped += 1
            continue
        two = p.kind in (3, 8)
        group = p.kind in (7, 8)
        if group:
            xa, ya = by_no.get(xin), by_no.get(yin) if two else None
            if xa is None or xa.addr is None or (two and (ya is None or ya.addr is None)):
                skipped += 1
                continue
            # a shared axis' storage and input are its own
            xu, xt, xsrc = xa.spec("X")
            if two:
                yu, yt, ysrc = ya.spec("X")
        else:
            xsrc, ysrc = xin, yin
        xs = storage(xu, xt, None)
        ys = storage(yu, yt, None) if two else None
        if xs is None or (two and ys is None):
            skipped += 1
            continue
        t = TableDef(name=p.name, kind="map" if two else "curve", z=z,
                     description=p.desc, categories=cat, order="row",
                     decimals=unit(wu).decimals,
                     cols=max(1, p.fk.get("X", 1)),
                     rows=max(1, p.fk.get("Y", 1)) if two else 1)
        t.x = AxisDef(t.cols, values=xs, name=axis_name(xsrc))
        if two:
            t.y = AxisDef(t.rows, values=ys, name=axis_name(ysrc))
        if group:
            t.dynamic = _Layout(t, p.kind, addr, xhead=pba + xa.addr,
                                yhead=pba + ya.addr if two else None,
                                transpose=two and transpose,
                                xsrc=src_addr.get(xsrc),
                                ysrc=src_addr.get(ysrc) if two else None)
        else:
            t.dynamic = _Layout(t, p.kind, addr, transpose=two and transpose,
                                xsrc=src_addr.get(xsrc),
                                ysrc=src_addr.get(ysrc) if two else None)
            t.record = addr
        defn.tables.append(t)
    # Where the description sits in an image, found from the image itself:
    # a curve's, map's or shared axis' block starts with the RAM address of
    # its input variable, which the DAMOS states (/SRC), then its point count
    # (at most /FKX). Every such block must check out at one base.
    checks = []
    for p in params:
        if p.kind in (2, 3, 6) and p.addr is not None:
            _u, _t, xin = p.spec("X")
            if xin in src_addr:
                checks.append((pba + p.addr, src_addr[xin],
                               max(1, p.fk.get("X", 1))))
    if len(checks) >= 3:
        defn.locate = lambda data, _c=checks, _d=defn: _locate(
            _c, data, _d.base_of(len(data)))
    if skipped:
        defn.info["not tables"] = str(skipped)
    defn.warnings.extend(sorted(notes))
    return defn


__all__ = ["load", "is_dam"]

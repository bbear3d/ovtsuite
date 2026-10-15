# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where a definition's tables are in an image -- with evidence for each.

A definition (A2L, DAMOS, WinOLS, XDF) gives addresses for the software it
was made for. On that software one offset fits every table; on another
software of the same project (another dataset, a newer program version)
parts of the calibration have moved. WinOLS applies one offset and shows
whatever is there. This module places every table on its own evidence and
says which evidence it had, so a table that moved is shown where it went
and a table nothing confirms is not shown as if it were known.

Evidence for a table at a candidate address, strongest first:

* **reference** -- the image the definition was made for is at hand (a
  WinOLS project carries it; an A2L's HEX can be given): the table's stored
  header and axis bytes there are found, once, in this image;
* **header** -- what the format itself states about the stored record
  holds: a DAMOS block starts with its input's RAM address and a point
  count within ``/FKX`` / ``/FKY``; an A2L layout with ``NO_AXIS_PTS`` stores
  counts between 1 and the declared maximum (``evidence`` on the table's
  dynamic layout). Stored axes must also rise or fall throughout.

and the grades a placement gets:

=============  ===============================================================
``exact``      confirmed at the definition's own address (the global offset)
``moved``      confirmed somewhere else, by evidence found only there
``region``     confirmed by its header at the offset its neighbours moved by
``bracketed``  no evidence of its own (a single value, a table with fixed
               axes), but the confirmed tables on both sides moved alike
``unverified`` none of the above: placed by the offset of the nearest
               confirmed table only; not shown unless asked for
=============  ===============================================================

The global offset is the candidate that confirms the most tables: the
definition's own (``Definition.base_in``), those its memory segments give,
and every offset a header or a reference fingerprint occurs at. A context
match (the reference bytes around a single value, found once) places a
value that moved.

Measured on definitions of one software applied to another of the same
project, with that software's own definition as the truth -- see
``tests/test_definition_placement.py``.
"""

from __future__ import annotations

import bisect
import copy
import os
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .model import Definition, TableDef

GRADES = ("exact", "moved", "region", "bracketed", "stated", "unverified")
#: grades shown without asking
SHOWN = ("exact", "moved", "region", "bracketed", "stated")
#: how far a moved table is looked for around its expected place
WINDOW = 0x20000
#: a reference fingerprint must be at least this long to count
MIN_FP = 8
#: bytes of reference context either side of a single value
CONTEXT = 12
#: records whose gap is shorter than this are laid out in sequence (aligned)
PAD = 4
#: how far (bytes, either way) the stored headers are scanned for the offsets
#: parts of the calibration moved by, when one offset does not fit
SCAN = 0x2000
#: below this share of confirmed tables, the scan runs
GOOD_FIT = 0.8
#: how many of the commonest offsets a table's header is tried at
VOTED = 8
#: placed tables either side whose offsets a single value is tried at
NEAR = 4
#: a single value's context must be the only one this close to count
LOCAL = 0x400
#: how far (definition bytes) a sequence is followed with nothing in it
#: confirmed before it is given up
RUN = 0x200
#: offsets (from the global one) the chance rate of headers is measured at
NOISE_AT = (0x1002, -0x1002, 0x2006, -0x2006)
#: headers must hold at this many times their chance rate, and at this share
#: of tables at least, for the global offset to stand on them
NOISE_X = 4
MIN_FIT = 0.08
#: with fewer tables to check than this, chance cannot be told apart
FEW = 12
#: stored axis points a table needs for its axes to count as evidence
AXIS_POINTS = 6
#: of a format that states only its axes, the points checked (a definition
#: may declare more points than the image stores)
PREFIX = 5
#: the definition's own offset is kept while it confirms this share of what
#: the best one does
OWN = 0.9


@dataclass
class Placed:
    index: int                  #: index in Definition.tables
    shift: int                  #: image bytes moved, on top of the global offset
    grade: str
    why: str = ""

    @property
    def shown(self) -> bool:
        return self.grade in SHOWN


@dataclass
class Region:
    start: int                  #: definition addresses [start, end)
    end: int
    shift: int
    tables: int


@dataclass
class Placement:
    base: int                   #: image offset = definition address - base (+ shift)
    #: how well the base fits: tables a header or the reference confirms there,
    #: of the tables that can be checked at all
    confirmed: int = 0
    checkable: int = 0
    items: List[Placed] = field(default_factory=list)
    regions: List[Region] = field(default_factory=list)
    reference: bool = False     #: a reference image was used
    override: bool = False      #: the base was given, not found
    candidates: List[Tuple[int, int]] = field(default_factory=list)  #: (base, confirmed)
    #: share of the tables with a stated header whose header holds at the
    #: global offset, and at offsets nothing sits at (chance)
    header_rate: float = 0.0
    noise: float = 0.0
    #: False: the headers hold here no more often than by chance, so nothing
    #: is placed by a header alone
    established: bool = True

    def counts(self) -> Dict[str, int]:
        c = Counter(p.grade for p in self.items)
        return {g: c.get(g, 0) for g in GRADES}

    @property
    def fit(self) -> float:
        return self.confirmed / self.checkable if self.checkable else 0.0

    def by_index(self) -> Dict[int, Placed]:
        return {p.index: p for p in self.items}

    def summary(self) -> str:
        """``"8456 where the definition puts them, 12 moved, 3 unverified"``."""
        c = self.counts()
        bits = []
        for g, word in (("exact", "where the definition puts them"),
                        ("moved", "found moved"),
                        ("region", "moved with their region"),
                        ("bracketed", "between confirmed tables"),
                        ("stated", "as the definition states"),
                        ("unverified", "unverified")):
            if c[g]:
                bits.append("%d %s" % (c[g], word))
        return ", ".join(bits) or "nothing placed"


# ------------------------------------------------------------------ reading
def _reader(data: bytes, base: int, shift: int = 0):
    n = len(data)

    def read(addr, size, signed, endian):
        a = addr - base + shift
        if a < 0 or a + size > n:
            return None
        return int.from_bytes(data[a:a + size], "little" if endian == "<" else "big",
                              signed=bool(signed))
    return read


def _resolved(t: TableDef, read) -> Optional[TableDef]:
    if t.dynamic is None:
        return t
    try:
        return t.dynamic(read)
    except Exception:
        return None


def _stores(t: TableDef):
    """The stored runs of a resolved table: [(addr, size)]."""
    out = []
    for a in (t.x, t.y):
        if a is not None and a.values is not None and a.values.addr is not None:
            out.append((a.values.addr, a.values.size * max(1, a.count)))
    if t.z.addr is not None:
        n = max(1, t.rows) * max(1, t.cols) if t.kind != "constant" else 1
        out.append((t.z.addr, t.z.size * n))
    return out


def _anchor(t: TableDef) -> Optional[int]:
    """The definition address a table is known by (its record start)."""
    if t.record is not None:
        return t.record
    addrs = [t.z.addr] + [a.values.addr for a in (t.x, t.y)
                          if a is not None and a.values is not None]
    addrs = [a for a in addrs if a is not None]
    return min(addrs) if addrs else None


def _monotonic(t: TableDef, read, order: str = ">", prefix: int = 0) -> Optional[bool]:
    """Stored axes rise or fall throughout -- or over their first ``prefix``
    points (a definition may declare more points than are stored) -- (None:
    no axis of 3+ points)."""
    seen = False
    for a in (t.x, t.y):
        if a is None or a.values is None or a.values.addr is None or a.count < 3:
            continue
        st = a.values
        vals = []
        for i in range(min(a.count, prefix) if prefix else a.count):
            v = read(st.addr + i * st.size, st.size, bool(st.signed), st.endian or order)
            if v is None:
                return False
            vals.append(v)
        seen = True
        up = all(x < y for x, y in zip(vals, vals[1:]))
        down = all(x > y for x, y in zip(vals, vals[1:]))
        if not (up or down):
            return False
    return True if seen else None


@dataclass
class _Probe:
    """What can be checked of one table."""
    index: int
    anchor: int
    header: bool            # the format states facts about its stored record
    axes: bool              # it has stored axes of 3+ points
    fp: bytes = b""         # reference bytes: header + axes
    fp_at: int = 0          # definition address the fingerprint starts at
    ctx: Optional[Tuple[bytes, int, bytes]] = None   # (left, value size, right)
    whole: bytes = b""      # the whole record, when ``fp`` is not unique there


def _strong(t: TableDef, read, order: str = ">") -> Optional[bool]:
    """True: the format's own facts (and stored axes) hold here; False: they
    fail; None: nothing the format states to check."""
    ev = getattr(t.dynamic, "evidence", None)
    if ev is None:
        return None
    try:
        checked, passed = ev(read)
    except Exception:
        return False
    if not checked:
        return None
    if passed != checked:
        return False
    r = _resolved(t, read)
    if r is None:
        return False
    return _monotonic(r, read, order) is not False


_ORDERS: "OrderedDict[int, Tuple[Definition, str]]" = OrderedDict()


def _order(defn: Definition) -> str:
    """``defn.byte_order()``, worked out once per definition."""
    hit = _ORDERS.get(id(defn))
    if hit is not None and hit[0] is defn:
        return hit[1]
    o = defn.byte_order()
    _ORDERS[id(defn)] = (defn, o)
    while len(_ORDERS) > 8:
        _ORDERS.popitem(last=False)
    return o


def _holds(t: TableDef, read, order: str) -> bool:
    """What the definition states about a table's stored record holds: its
    format's header facts (with stored axes rising or falling), or -- for a
    format that states none -- its stored axes rising or falling."""
    if getattr(t.dynamic, "evidence", None) is not None:
        return bool(_strong(t, read, order))
    return _monotonic(t, read, order, PREFIX) is True


def _axis_points(t: TableDef) -> int:
    return sum(a.count for a in (t.x, t.y)
               if a is not None and a.values is not None and a.values.addr is not None)


# ------------------------------------------------------------------ the work
class _Ref:
    """The image the definition was made for, read at definition addresses."""

    def __init__(self, data: bytes, base: int):
        self.data, self.base = bytes(data), base
        self.read = _reader(self.data, base)

    def unique(self, b: bytes, addr: int) -> bool:
        """``b`` (found at definition address ``addr``) occurs nowhere else
        within the distance a table is looked for."""
        a = addr - self.base
        lo, hi = max(0, a - WINDOW), min(len(self.data), a + WINDOW + len(b))
        first = self.data.find(b, lo, hi)
        return first == a and self.data.find(b, first + 1, hi) == -1

    def bytes_at(self, addr: int, n: int) -> Optional[bytes]:
        a = addr - self.base
        if a < 0 or a + n > len(self.data):
            return None
        return self.data[a:a + n]


def _probes(defn: Definition, ref: Optional[_Ref]) -> List[_Probe]:
    out = []
    for i, t in enumerate(defn.tables):
        a = _anchor(t)
        if a is None:
            continue
        p = _Probe(index=i, anchor=a,
                   header=getattr(t.dynamic, "evidence", None) is not None,
                   axes=any(x is not None and x.values is not None and x.count >= 3
                            for x in (t.x, t.y)))
        if ref is not None:
            r = _resolved(t, ref.read)
            if r is not None:
                if t.kind == "constant" or not (p.header or p.axes):
                    za, zs = r.z.addr, r.z.size
                    left = ref.bytes_at(za - CONTEXT, CONTEXT) if za is not None else None
                    right = ref.bytes_at(za + zs, CONTEXT) if za is not None else None
                    if left is not None and right is not None:
                        p.ctx = (left, zs, right)
                else:
                    # header + axes: everything stored ahead of the values
                    end = r.z.addr if r.z.addr is not None else a
                    start = min([a] + [s for s, _n in _stores(r)])
                    if end - start >= MIN_FP:
                        b = ref.bytes_at(start, min(end - start, 256))
                        if b is not None and len(set(b)) > 2:
                            p.fp, p.fp_at = b, start
                            if not ref.unique(b, start):
                                # another table there has the same header and
                                # axes: the whole record (values too) tells
                                # them apart while the values are unchanged
                                whole = _span(r, a)[1]
                                w = ref.bytes_at(start, min(whole - start, 1024))
                                if w is not None and ref.unique(w, start):
                                    p.whole = w
        out.append(p)
    return out


def _confirm(defn, probes_by_i, i, data, base, shift, ref) -> str:
    """The evidence for table ``i`` at ``base``/``shift``: "reference",
    "header", "axes" (stored axes rise or fall, nothing else), or ""."""
    p = probes_by_i[i]
    t = defn.tables[i]
    read = _reader(data, base, shift)
    if p.fp:
        a = p.fp_at - base + shift
        if 0 <= a and data[a:a + len(p.fp)] == p.fp:
            return "reference"
    order = _order(defn)
    if p.header and t.dynamic is None:
        # a format without header facts: its stored axes are what it states
        return "header" if _monotonic(t, read, order, PREFIX) is True else ""
    s = _strong(t, read, order)
    if s:
        return "header"
    if s is False:
        return ""
    if p.axes:
        r = _resolved(t, read)
        if r is not None and _monotonic(r, read, order):
            return "axes"
    return ""


def _base_candidates(defn: Definition, data: bytes, probes, ref) -> List[int]:
    cands = Counter()
    try:
        cands[defn.base_in(data)] += 1000
    except Exception:
        pass
    try:
        cands[defn.base_of(len(data))] += 500
    except Exception:
        pass
    cands[defn.base] += 1
    for start, _n in defn.segments:
        cands[start] += 1
    if ref is not None:
        cands[ref.base] += 1
    # every offset a reference fingerprint occurs at
    for p in probes[:4000]:
        if p.fp:
            pat = p.fp[:24]
            j = data.find(pat)
            k = 0
            while j != -1 and k < 4:
                cands[p.fp_at - j] += 1
                j = data.find(pat, j + 1)
                k += 1
    # every offset a DAMOS block header occurs at
    srcs = Counter()
    for p in probes:
        t = defn.tables[p.index]
        src = getattr(t.dynamic, "xsrc", None)
        if src is not None and t.record is not None:
            srcs[(src, t.record)] += 1
    seen = 0
    for (src, rec), _c in srcs.items():
        if seen > 64:
            break
        seen += 1
        pat = src.to_bytes(2, "little")
        j = data.find(pat)
        k = 0
        while j != -1 and k < 400:
            if not j & 1:
                cands[rec - j] += 1
            j = data.find(pat, j + 1)
            k += 1
    return [b for b, _c in cands.most_common(24)]


def _score(defn, probes, data, base, ref, sample=None) -> int:
    by_i = {p.index: p for p in probes}
    n = 0
    for p in (sample if sample is not None else probes):
        if (p.header or p.fp) and _confirm(defn, by_i, p.index, data, base, 0, ref) \
                in ("reference", "header"):
            n += 1
    return n


def _span(t: TableDef, anchor: int) -> Tuple[int, int]:
    """Definition addresses ``[start, end)`` a (resolved) table's record covers."""
    runs = _stores(t)
    start = min([anchor] + [a for a, _n in runs])
    end = max([anchor + 1] + [a + n for a, n in runs])
    return start, end


def _align(t: TableDef) -> int:
    """The alignment a record of ``t`` starts on: 2 when it stores words."""
    for st in (t.z, t.x.values if t.x else None, t.y.values if t.y else None):
        if st is not None and st.addr is not None and st.size > 1:
            return 2
    return 1


class _Work:
    """One placement run: the definition, the image and what is known."""

    def __init__(self, defn, data, base, ref, probes):
        self.defn, self.data, self.base, self.ref = defn, data, base, ref
        self.probes = sorted(probes, key=lambda p: (p.anchor, p.index))
        self.pos = {p.index: k for k, p in enumerate(self.probes)}
        self.by_i = {p.index: p for p in probes}
        self.grade: Dict[int, Placed] = {}
        self.votes: Counter = Counter()
        self._spans: Dict[Tuple[int, int], Optional[Tuple[int, int]]] = {}
        self._ctx: Dict[Tuple[int, int], bool] = {}
        self.walked = set()
        self._ev: Dict[Tuple[int, int], str] = {}
        self.static = {}            # index -> (start, end) at the declared counts
        self.align = {}
        for p in self.probes:
            t = defn.tables[p.index]
            self.static[p.index] = _span(t, p.anchor)
            self.align[p.index] = _align(t)

    # --- evidence, remembered
    def ev(self, i, s) -> str:
        k = (i, s)
        if k not in self._ev:
            self._ev[k] = _confirm(self.defn, self.by_i, i, self.data, self.base, s, self.ref)
        return self._ev[k]

    def context(self, i, s) -> bool:
        """A single value's reference context holds at shift ``s`` -- and
        nowhere close by (in repetitive data it would hold at every period)."""
        p = self.by_i[i]
        if p.ctx is None:
            return False
        k = (i, s)
        if k not in self._ctx:
            t = self.defn.tables[i]
            ok = _context(self.data, p, self.base, [s], t) == s
            if ok:
                ok = _found_once(self.data, p, self.base - s, t, LOCAL) == 0
            self._ctx[k] = ok
        return self._ctx[k]

    def neighbour_agrees(self, i, s) -> bool:
        """Placed at ``s``, table ``i`` puts the tables laid out after (or
        before) it somewhere: the first of them with anything to check
        (within ``RUN`` bytes) holds there."""
        for step in (1, -1):
            k, cur, sc = self.pos[i], i, s
            while True:
                k += step
                if not 0 <= k < len(self.probes):
                    break
                j = self.probes[k].index
                a, b = (cur, j) if step > 0 else (j, cur)
                if not self.adjacent(a, b) or \
                        abs(self.by_i[j].anchor - self.by_i[i].anchor) > RUN:
                    break
                sj = self._next_shift(cur, sc, j) if step > 0 else self._prev_shift(cur, sc, j)
                if sj is None:
                    break
                if self.checkable(j):
                    if self.ev(j, sj) in ("reference", "header"):
                        return True
                    break
                if self.context(j, sj):
                    return True
                cur, sc = j, sj
        return False

    def checkable(self, i) -> bool:
        p = self.by_i[i]
        return bool(p.header or p.fp)

    def resolved(self, i, s) -> Optional[TableDef]:
        return _resolved(self.defn.tables[i], _reader(self.data, self.base, s))

    def span_at(self, i, s) -> Optional[Tuple[int, int]]:
        """Image offsets ``[start, end)`` of table ``i`` placed with shift ``s``."""
        t = self.defn.tables[i]
        if t.dynamic is None:
            a, b = self.static[i]
            return a - self.base + s, b - self.base + s
        k = (i, s)
        if k not in self._spans:
            r = self.resolved(i, s)
            if r is None:
                self._spans[k] = None
            else:
                a, b = _span(r, self.by_i[i].anchor)
                self._spans[k] = (a - self.base, b - self.base)
        sp = self._spans[k]
        return None if sp is None else (sp[0] + s, sp[1] + s)

    def source_end(self, i) -> Optional[int]:
        """Definition address where table ``i`` ends in the image it was made
        for: exact for a table of fixed size or with the reference at hand,
        else None."""
        t = self.defn.tables[i]
        if t.dynamic is None:
            return self.static[i][1]
        if self.ref is not None:
            r = _resolved(t, self.ref.read)
            if r is not None:
                return _span(r, self.by_i[i].anchor)[1]
        return None

    def adjacent(self, i, j) -> bool:
        """Table ``j`` starts where ``i`` ends in the definition (no gap but
        alignment): records laid out in sequence."""
        end = self.source_end(i)
        aj = self.by_i[j].anchor
        if end is not None:
            return 0 <= aj - end < PAD
        lo = self.by_i[i].anchor
        return lo < aj < self.static[i][1] + PAD

    # --- the walk
    def _next_shift(self, cur, scur, nxt) -> Optional[int]:
        sp = self.span_at(cur, scur)
        if sp is None:
            return None
        end = self.source_end(cur)
        an = self.by_i[nxt].anchor
        if end is not None:
            q = sp[1] + (an - end)
        else:
            q = sp[1]
            al = self.align[nxt]
            q += (-q) % al
        return q - (an - self.base)

    def _prev_shift(self, cur, scur, prv) -> Optional[int]:
        sp = self.span_at(cur, scur)
        if sp is None:
            return None
        start = sp[0]
        t = self.defn.tables[prv]
        if t.dynamic is None:
            s0, e0 = self.static[prv]
            # the same gap as in the definition
            return start - (self.by_i[cur].anchor - e0) - (e0 - self.base) + (s0 - s0)
        end = self.source_end(prv)
        if end is not None and self.ref is not None:
            gap = self.by_i[cur].anchor - end
        else:
            gap = None
        # its size here depends on the counts stored here: every start whose
        # record (as stored there) ends where ``cur`` starts
        hits = []
        lo_size = 1
        hi_size = self.static[prv][1] - self.static[prv][0] + 2
        al = self.align[prv]
        evidence = getattr(t.dynamic, "evidence", None)
        for size in range(lo_size, hi_size + 1):
            st = start - size
            s = st - (self.static[prv][0] - self.base)
            if al > 1 and (st % al):
                continue
            if evidence is not None:
                try:
                    c, ok = evidence(_reader(self.data, self.base, s))
                except Exception:
                    continue
                if c and ok != c:
                    continue
            sp2 = self.span_at(prv, s)
            if sp2 is None:
                continue
            g = start - sp2[1]
            if (gap is not None and g == gap) or (gap is None and 0 <= g < max(2, self.align[cur])):
                if self.ev(prv, s) in ("reference", "header"):
                    hits.append(s)
                    if len(hits) > 1:
                        return None
        return hits[0] if hits else None

    def walk(self, i, step):
        """From placed table ``i``, follow the tables laid out after it
        (``step`` +1) or before it (-1) for as long as each one checked holds
        at the place the one before puts it."""
        pending = []
        cur, scur = i, self.grade[i].shift
        k = self.pos[i]
        while True:
            k += step
            if not 0 <= k < len(self.probes):
                return
            nxt = self.probes[k].index
            a, b = (cur, nxt) if step > 0 else (nxt, cur)
            if not pending and nxt in self.grade:
                return              # placed already, nothing between to settle
            if not self.adjacent(a, b):
                return
            sn = (self._next_shift(cur, scur, nxt) if step > 0
                  else self._prev_shift(cur, scur, nxt))
            if sn is None:
                return
            g = self.grade.get(nxt)
            if g is not None:
                if g.shift == sn and g.grade not in ("unverified", "bracketed"):
                    self._flush(pending)
                return
            if self.checkable(nxt):
                e = self.ev(nxt, sn)
                if e not in ("reference", "header"):
                    return
                if e == "header" and pending and abs(
                        self.by_i[nxt].anchor - self.by_i[pending[0][0]].anchor) > RUN:
                    # a long way with nothing confirmed: a header alone (it
                    # holds by chance now and then) does not close it
                    return
                self.grade[nxt] = Placed(
                    nxt, sn, "exact" if sn == 0 else "moved",
                    "%s, %s" % ("reference bytes" if e == "reference" else "its header",
                                "right after the table before it" if step > 0
                                else "right before the table after it"))
                self._flush(pending)
                pending = []
            else:
                p = self.by_i[nxt]
                if p.axes:
                    r = self.resolved(nxt, sn)
                    if r is None or _monotonic(r, _reader(self.data, self.base, sn),
                                               _order(self.defn)) is False:
                        return
                if self.context(nxt, sn):
                    self.grade[nxt] = Placed(
                        nxt, sn, "exact" if sn == 0 else "moved",
                        "the reference bytes around it, in sequence")
                    self._flush(pending)
                    pending = []
                else:
                    if p.ctx is not None:
                        # its reference context is somewhere else close by:
                        # the sequence broke here
                        alt = _found_once(self.data, p, self.base - sn,
                                          self.defn.tables[nxt], LOCAL)
                        if alt is not None and alt != 0:
                            return
                    pending.append((nxt, sn))
            cur, scur = nxt, sn

    def _flush(self, pending):
        for j, s in pending:
            if j not in self.grade:
                self.grade[j] = Placed(j, s, "bracketed",
                                       "laid out in sequence between confirmed tables")

    def walk_all(self):
        done = self.walked
        while True:
            todo = [(i, d) for i, g in list(self.grade.items())
                    if g.grade in ("exact", "moved", "region") for d in (1, -1)
                    if (i, d) not in done]
            if not todo:
                return
            for i, d in todo:
                done.add((i, d))
                self.walk(i, d)


def _scan(defn, data, base, probes, span=SCAN, tables=40, progress=None) -> Counter:
    """Offsets (from ``base``) at which the stored headers of a sample of
    tables hold, counted: the offsets parts of the calibration moved by."""
    order = _order(defn)
    chk = [p for p in probes if p.header]
    # the tables with the most to check first: a long stored axis
    chk.sort(key=lambda p: -max([a.count for a in (defn.tables[p.index].x,
                                                     defn.tables[p.index].y)
                                 if a is not None and a.values is not None] or [0]))
    chk = chk[:max(tables * 4, 1)]
    sample = sorted(chk, key=lambda p: p.anchor)[::max(1, len(chk) // tables)]
    votes = Counter()
    for n, p in enumerate(sample):
        if progress:
            progress("Looking for tables that moved (%d of %d)" % (n + 1, len(sample)))
        t = defn.tables[p.index]
        step = _align(t)
        ev = getattr(t.dynamic, "evidence", None)
        for s in range(-span, span + 1, step):
            rd = _reader(data, base, s)
            if ev is not None:
                try:
                    c, ok = ev(rd)
                except Exception:
                    continue
                if not c or ok != c:
                    continue
            if _holds(t, rd, order):
                votes[s] += 1
    return votes


def place(defn: Definition, data: bytes, reference: Optional[Tuple[bytes, int]] = None,
          base: Optional[int] = None, relocate: bool = True,
          progress: Optional[Callable[[str], None]] = None) -> Placement:
    """Place every table of ``defn`` in ``data``.

    ``reference``: ``(image, base)`` of the image the definition was made for
    (default: the one the definition carries); ``base``: use this global
    offset instead of finding one; ``relocate``: False keeps every table at
    the global offset (graded, never moved)."""
    data = bytes(data)
    if reference is None:
        reference = defn.reference
    ref = _Ref(*reference) if reference is not None else None
    probes = _probes(defn, ref)
    if not any(p.header for p in probes):
        # a format that states nothing about its records (XDF, a map pack)
        # (with the image it was made for, its bytes are the evidence; the
        # axes rising is too weak beside them)
        if ref is None:
            return _as_stated(defn, data, probes, base, res_ref=False)
    checkable = [p for p in probes if p.header or p.fp]
    res = Placement(base=0, reference=ref is not None)

    # ---- the global offset
    if base is not None:
        res.base, res.override = base, True
    else:
        cands = _base_candidates(defn, data, probes, ref)
        step = max(1, len(checkable) // 600)
        sample = checkable[::step]
        scored = [(_score(defn, probes, data, b, ref, sample), b) for b in cands]
        scored.sort(key=lambda x: (-x[0], abs(x[1] - cands[0]) if cands else 0))

        def pick():
            # the definition's own offset, unless another fits clearly better
            # (its axes, shifted by a word, can rise just as well)
            if not scored or scored[0][0] <= 0:
                return cands[0] if cands else 0
            own = next((sc for sc, b in scored if b == cands[0]), 0)
            return cands[0] if own >= OWN * scored[0][0] else scored[0][1]
        best = pick()
        votes = Counter()
        if relocate and sample and (scored[0][0] if scored else 0) < GOOD_FIT * len(sample):
            # one offset does not fit: find the ones parts of it moved by
            votes = _scan(defn, data, best, probes, progress=progress)
            known = {b for _s, b in scored}
            more = [best - s for s, c in votes.most_common(8) if c > 1 and best - s not in known]
            scored += [(_score(defn, probes, data, b, ref, sample), b) for b in more]
            scored.sort(key=lambda x: (-x[0], abs(x[1] - cands[0]) if cands else 0))
            if scored and scored[0][0] > 0:
                nb = pick()
                votes = Counter({s + (nb - best): c for s, c in votes.items()})
                best = nb
        res.candidates = [(b, s) for s, b in scored[:8]]
        res.base = best
    if progress:
        progress("Checking %d tables at offset 0x%X" % (len(probes), res.base))

    w = _Work(defn, data, res.base, ref, probes)
    w.votes = votes if base is None else Counter()
    # ---- every table at the global offset
    later = []
    for p in probes:
        if p.header or p.fp:
            res.checkable += 1
            e = w.ev(p.index, 0)
            if e == "reference" or (e == "header" and not p.fp):
                w.grade[p.index] = Placed(p.index, 0, "exact", e)
            elif e == "header":
                # its header holds, but its reference bytes are not here:
                # they may be elsewhere (the table moved, something else
                # sits here); exact only if they are found nowhere
                later.append(p.index)
    res.confirmed = len(w.grade) + len(later)

    # how often a header holds by chance here: at offsets nothing sits at
    hdr = [p for p in probes if p.header]
    if hdr:
        samp = hdr[::max(1, len(hdr) // 400)]
        order = _order(defn)

        def rate(s):
            return sum(1 for p in samp if _holds(defn.tables[p.index],
                                                 _reader(data, res.base, s), order)) / len(samp)
        res.noise = sum(rate(k) for k in NOISE_AT) / len(NOISE_AT)
        res.header_rate = rate(0)
        if not _established(res.header_rate, res.noise, len(hdr)):
            # the headers hold here no more often than they do anywhere: the
            # offset is not established by them, and none is placed by a
            # header alone
            res.established = False
            for i in [i for i, g in w.grade.items() if g.why == "header"]:
                del w.grade[i]
            later = []

    if relocate and res.fit < GOOD_FIT and w.votes:
        # where one offset fits only part of it, a header that also holds at
        # another offset many tables moved by (records of one shape in a
        # row) does not say which of them it is at
        common = [s for s, c in w.votes.most_common(VOTED) if c > 1 and s != 0]
        for i in [i for i, g in w.grade.items() if g.why == "header"]:
            alt = [s for s in common if w.ev(i, s) == "header"]
            if not alt:
                continue
            # the tables laid out next to it decide, when they hold at one
            # of those offsets only
            agree = [s for s in [0] + alt if w.neighbour_agrees(i, s)]
            del w.grade[i]
            if agree == [0]:
                w.grade[i] = Placed(i, 0, "exact", "its header and the table next to it")
            elif len(agree) == 1:
                w.grade[i] = Placed(i, agree[0], "region",
                                    "its header and the table next to it hold at an "
                                    "offset many tables moved by")
    if relocate:
        if progress:
            progress("Following the tables that moved")
        _relocate(w, later)
    for i in later:
        if i not in w.grade:
            w.grade[i] = Placed(i, 0, "exact", "header")
    if relocate:
        w.walk_all()
    if relocate:
        _overlaps(w)
    _fill(w, res, relocate)
    if relocate:
        _overlaps(w)
    res.items = [w.grade[i] for i in sorted(w.grade)]
    res.regions = _regions(probes, w.grade)
    return res


def _established(rate: float, noise: float, n: int) -> bool:
    """Whether the tables' stated facts holding at ``rate`` (of ``n``) at an
    offset, against ``noise`` at offsets nothing sits at, establish it: well
    above chance -- or nearly all of them (in smooth data they hold anywhere)
    -- and too few to tell anything count as established."""
    if n < FEW:
        return True
    return rate >= MIN_FIT and (rate >= NOISE_X * noise or rate >= 0.9)


def _as_stated(defn, data, probes, base, res_ref=False) -> Placement:
    """A definition with nothing to check but its stored axes (an XDF, a
    WinOLS map pack) and no image of its own: every table where the
    definition puts it -- at its own offset, or the one given -- unless its
    axes show the definition is not for this image at all.

    (The axes alone cannot move a table: shifted by the word ahead of them
    -- often a point count -- they rise just as well.)"""
    res = Placement(base=defn.base_in(data) if base is None else base,
                    override=base is not None, reference=res_ref)
    order = _order(defn)
    read = _reader(data, res.base)
    checked = {}
    for p in probes:
        t = defn.tables[p.index]
        if t.dynamic is None and _axis_points(t) >= AXIS_POINTS:
            checked[p.index] = _monotonic(t, read, order, PREFIX) is True
    res.checkable = len(checked)
    res.confirmed = sum(checked.values())
    if checked:
        samp = list(checked)[::max(1, len(checked) // 400)]

        def rate(s):
            rd = _reader(data, res.base, s)
            return sum(1 for i in samp if _monotonic(defn.tables[i], rd, order, PREFIX)
                       is True) / len(samp)
        res.noise = sum(rate(k) for k in NOISE_AT) / len(NOISE_AT)
        res.header_rate = res.fit
        res.established = _established(res.fit, res.noise, len(checked))
    for p in probes:
        if checked.get(p.index) and res.established:
            res.items.append(Placed(p.index, 0, "exact", "its stored axes rise here"))
        elif res.established:
            res.items.append(Placed(p.index, 0, "stated",
                                    "where the definition puts it (nothing to check)"))
        else:
            res.items.append(Placed(p.index, 0, "unverified",
                                    "the definition does not fit this image here"))
    res.items.sort(key=lambda p: p.index)
    res.regions = _regions(probes, {p.index: p for p in res.items})
    return res


def _relocate(w: _Work, later=()):
    """Look for each unconfirmed table where evidence of its own puts it
    (``later``: tables whose header holds where they are, looked for by
    their reference bytes first)."""
    data, base, n = w.data, w.base, len(w.data)
    todo = [p for p in w.probes if p.index not in w.grade and (p.header or p.fp)]
    held = set(later)
    # (1) a reference fingerprint found once near its expected place
    def once(b, exp):
        lo, hi = max(0, exp - WINDOW), min(n, exp + WINDOW + len(b))
        j = data.find(b, lo, hi)
        if j == -1 or data.find(b, j + 1, hi) != -1:
            return None
        return j

    for p in todo:
        if p.index in w.grade or not p.fp:
            continue
        exp = p.fp_at - base
        j = once(p.whole, exp) if p.whole else None
        if j is None:
            j = once(p.fp, exp)
        if j is not None and j != exp:
            s = j - exp
            if w.ev(p.index, s) == "reference":
                w.grade[p.index] = Placed(p.index, s, "moved", "reference bytes found once")
    # tables laid out in sequence after those, before the rest are taken
    # where their header holds
    w.walk_all()
    for i in held:
        if i not in w.grade:
            w.grade[i] = Placed(i, 0, "exact", "header")
    todo = [p for p in todo if p.index not in held]
    w.walk_all()
    # (2) a DAMOS header signature found once in the window
    for p in todo:
        if p.index in w.grade or not p.header:
            continue
        t = w.defn.tables[p.index]
        src = getattr(t.dynamic, "xsrc", None)
        if src is None or t.record is None:
            continue
        exp = t.record - base
        lo, hi = max(0, exp - WINDOW), min(n, exp + WINDOW)
        pat = src.to_bytes(2, "little")
        hits = []
        j = data.find(pat, lo, hi)
        while j != -1:
            if not j & 1 and w.ev(p.index, j - exp) == "header":
                hits.append(j - exp)
                if len(hits) > 1:
                    break
            j = data.find(pat, j + 1, hi)
        if len(hits) == 1:
            w.grade[p.index] = Placed(p.index, hits[0], "moved",
                                      "its header found once nearby")
    w.walk_all()
    # (3) a single value whose reference context is found once nearby, and
    # whose neighbour in the definition agrees
    for p in w.probes:
        if p.index in w.grade or p.ctx is None:
            continue
        s = _found_once(data, p, base, w.defn.tables[p.index])
        if s is not None and s != 0 and w.neighbour_agrees(p.index, s):
            w.grade[p.index] = Placed(p.index, s, "moved",
                                      "the reference bytes around it found once, "
                                      "and the table next to it agrees")
            w.walk_all()
    # (4) a header the format states, at the offset its confirmed neighbours
    # (on both sides, or the only side there is) moved by
    keyed = None
    for p in todo:
        if p.index in w.grade or not p.header:
            continue
        if keyed is None:
            keyed = sorted((w.by_i[i].anchor, g.shift) for i, g in w.grade.items()
                           if g.grade in ("exact", "moved"))
            keys = [a for a, _s in keyed]
        j = bisect.bisect_right(keys, p.anchor)
        prev = keyed[j - 1][1] if j > 0 else None
        nxt = keyed[j][1] if j < len(keyed) else None
        tried = [s for s in (prev, nxt) if s is not None and s != 0]
        ok = [s for s in dict.fromkeys(tried) if w.ev(p.index, s) == "header"]
        if len(ok) == 1:
            w.grade[p.index] = Placed(p.index, ok[0], "region",
                                      "its header holds at its neighbours' offset")
            continue
        if ok or p.fp:
            # (a table whose reference bytes are nowhere has changed: its
            # header alone at a common offset is not enough)
            continue
        # the offsets the scan found many tables moved by
        common = [s for s, c in w.votes.most_common(VOTED) if c > 1 and s != 0]
        ok = [s for s in common if w.ev(p.index, s) == "header"]
        if len(ok) == 1 and w.neighbour_agrees(p.index, ok[0]):
            w.grade[p.index] = Placed(p.index, ok[0], "region",
                                      "its header holds at an offset many tables moved by")
    w.walk_all()


_RANK = {"exact": 4, "moved": 3, "region": 2, "bracketed": 1, "stated": 1,
         "unverified": 0}


def _overlaps(w: _Work):
    """Two tables placed on the same bytes (that the definition keeps apart)
    cannot both be right: the one with the weaker evidence -- or both, when
    neither has more -- is shown as unverified."""
    rows = []
    for i, g in w.grade.items():
        if g.grade not in SHOWN:
            continue
        sp = w.span_at(i, g.shift)
        if sp is not None:
            rows.append((sp[0], sp[1], i))
    rows.sort()
    bad = set()
    for k in range(1, len(rows)):
        a0, a1, i = rows[k]
        # the nearest earlier ones that reach into this one
        for m in range(k - 1, max(-1, k - 6), -1):
            b0, b1, j = rows[m]
            if b1 <= a0:
                continue
            si, sj = w.static[i], w.static[j]
            if si[0] < sj[1] and sj[0] < si[1]:
                continue            # they overlap in the definition too
            ri, rj = _RANK[w.grade[i].grade], _RANK[w.grade[j].grade]
            if ri >= rj:
                bad.add(j)
            if rj >= ri:
                bad.add(i)
    for i in bad:
        g = w.grade[i]
        w.grade[i] = Placed(i, g.shift, "unverified",
                            "placed on bytes another table was placed on")


def _fill(w: _Work, res: Placement, relocate: bool):
    """Grade every table nothing placed yet from what is around it."""
    # in definition order: every table placed by evidence, and every table
    # that was checked and placed nowhere (a break between its neighbours)
    marks = []
    for p in w.probes:
        g = w.grade.get(p.index)
        if g is not None and g.grade in SHOWN:
            marks.append((p.anchor, g.shift))
        elif g is not None or p.header or p.fp:
            marks.append((p.anchor, None))
    keys = [a for a, _s in marks]
    shifts = {s for _a, s in marks if s is not None}
    uniform = (len(shifts) == 1 and res.checkable and res.fit >= GOOD_FIT)
    for p in w.probes:
        if p.index in w.grade:
            continue
        t = w.defn.tables[p.index]
        j = bisect.bisect_left(keys, p.anchor)
        # (a table placed nowhere is its own mark: step over it)
        # neighbours strictly before / after (a failed checkable table is its
        # own mark: skip it)
        jl = j - 1
        while jl >= 0 and keys[jl] == p.anchor:
            jl -= 1
        jr = j
        while jr < len(keys) and keys[jr] == p.anchor and marks[jr][1] is None:
            jr += 1
        prev = marks[jl][1] if jl >= 0 else "edge"
        nxt = marks[jr][1] if jr < len(marks) else "edge"
        if p.ctx is not None:
            near = [0]
            if relocate:
                # the offsets the nearest placed tables either side moved by
                near = []
                for rng in (range(jl, max(-1, jl - 40), -1),
                            range(jr, min(len(marks), jr + 40))):
                    got = 0
                    for m in rng:
                        x = marks[m][1]
                        if x is not None:
                            if x not in near:
                                near.append(x)
                            got += 1
                            if got >= NEAR:
                                break
            hit = _context(w.data, p, w.base, near, t)
            if hit is not None:
                w.grade[p.index] = Placed(p.index, hit, "exact" if hit == 0 else "region",
                                          "the reference bytes around it hold here")
                continue
        s, g = None, "unverified"
        if isinstance(prev, int) and prev == nxt and keys[jr] - keys[jl] <= RUN:
            s, g = prev, "bracketed"
        elif uniform:
            # one offset fits (nearly) everything checked, and nothing was
            # found anywhere else: the definition is for this software
            s, g = next(iter(shifts)), "bracketed"
        if s is None:
            s = prev if isinstance(prev, int) else (nxt if isinstance(nxt, int) else 0)
            if not relocate:
                s = 0
        if g == "bracketed" and (p.header or p.fp):
            g = "unverified"            # checked, and it did not hold anywhere
        elif g == "bracketed" and p.axes:
            r = w.resolved(p.index, s)
            if r is None or _monotonic(r, _reader(w.data, w.base, s), _order(w.defn)) is False:
                g = "unverified"
        w.grade[p.index] = Placed(p.index, s, g,
                                  "between confirmed tables" if g == "bracketed"
                                  else "nothing confirms it here")


def _context(data, p, base, shifts, t) -> Optional[int]:
    """The one of ``shifts`` (the offsets the tables either side moved by) at
    which a single value's reference context -- the bytes on both sides of
    it in the image the definition was made for -- is found, or None."""
    left, size, right = p.ctx
    if len(set(left + right)) < 4:
        return None
    za = t.z.addr
    if za is None:
        return None
    hits = []
    for s in dict.fromkeys(shifts):
        a = za - base + s
        if a - CONTEXT < 0 or a + size + CONTEXT > len(data):
            continue
        if data[a - CONTEXT:a] == left and data[a + size:a + size + CONTEXT] == right:
            hits.append(s)
    return hits[0] if len(hits) == 1 else None


def _found_once(data, p, base, t, window=WINDOW) -> Optional[int]:
    """The shift at which a single value's reference context occurs once
    within ``window`` bytes of its expected place, or None."""
    left, size, right = p.ctx
    if len(set(left + right)) < 4 or t.z.addr is None:
        return None
    exp = t.z.addr - base
    lo, hi = max(0, exp - window), min(len(data), exp + window)
    pat = re.compile(re.escape(left) + b"(?s:.{%d})" % size + re.escape(right))
    hits = []
    for m in pat.finditer(data, lo, hi):
        hits.append(m.start() + CONTEXT - exp)
        if len(hits) > 1:
            return None
    return hits[0] if hits else None


def _regions(probes, grade) -> List[Region]:
    rows = sorted((p.anchor, grade[p.index].shift) for p in probes
                  if p.index in grade and grade[p.index].grade in SHOWN)
    out: List[Region] = []
    for a, s in rows:
        if out and out[-1].shift == s:
            out[-1].end = a + 1
            out[-1].tables += 1
        else:
            out.append(Region(a, a + 1, s, 1))
    return out


def relocated(defn: Definition, pl: Placement, show_unverified: bool = False) -> Definition:
    """A copy of ``defn`` whose tables sit where ``pl`` put them (base =
    ``pl.base``); tables not shown are left out. Each kept table's note says
    how it was placed."""
    out = copy.copy(defn)
    out.tables = []
    out.base = pl.base
    out.base_for = None
    out.locate = None
    by = pl.by_index()
    for i, t in enumerate(defn.tables):
        p = by.get(i)
        if p is None or not (p.shown or show_unverified):
            continue
        out.tables.append(_shifted(t, p))
    return out


def _shifted(t: TableDef, p: Placed) -> TableDef:
    """A copy of ``t`` moved by ``p.shift`` image bytes, its note saying how
    it was placed (``placement``: the grade)."""
    d = p.shift
    new = _move(t, d)
    new.note = _note(t.note, p)
    new.placement = p.grade
    if t.record is not None:
        new.record = t.record + d
    if t.dynamic is not None:
        new.dynamic = _ShiftedLayout(t.dynamic, d, new.note, p.grade)
    return new


class _ShiftedLayout:
    """A dynamic layout read ``delta`` bytes further on, its result moved
    to match (and carrying the placement's note)."""

    def __init__(self, inner, delta, note="", grade=""):
        self.inner, self.delta, self.note, self.grade = inner, delta, note, grade

    def __call__(self, read):
        r = self.inner(lambda a, n, sg, e: read(a + self.delta, n, sg, e))
        if r is None:
            return None
        r = _move(r, self.delta)
        r.dynamic = None
        r.note = self.note
        r.placement = self.grade
        return r

    def evidence(self, read):
        ev = getattr(self.inner, "evidence", None)
        if ev is None:
            return (0, 0)
        return ev(lambda a, n, sg, e: read(a + self.delta, n, sg, e))


def _move(t: TableDef, delta: int) -> TableDef:
    new = copy.copy(t)
    if not delta:
        return new

    def mv(st):
        if st is None or st.addr is None:
            return st
        st = copy.copy(st)
        st.addr += delta
        return st
    new.z = mv(t.z)
    for attr in ("x", "y"):
        a = getattr(t, attr)
        if a is not None and a.values is not None:
            a = copy.copy(a)
            a.values = mv(a.values)
            setattr(new, attr, a)
    return new


def _note(note: str, p: Placed) -> str:
    text = {
        "exact": "",
        "moved": "Moved %+d bytes from the definition's address (%s)." % (p.shift, p.why),
        "region": "Moved %+d bytes with the tables around it (%s)." % (p.shift, p.why),
        "bracketed": ("Placed between confirmed tables%s; nothing of its own "
                      "was checked." % (" (moved %+d bytes)" % p.shift if p.shift else "")),
        "stated": "",
        "unverified": ("UNVERIFIED: nothing confirms this table here (%+d bytes "
                       "from the definition's address). Check before writing."
                       % p.shift),
    }[p.grade]
    if not text:
        return note
    return (note + "\n" if note else "") + text


def reference_from_file(data: bytes, defn: Definition) -> Optional[Tuple[bytes, int]]:
    """``(image, base)`` of a reference image file the user gives for
    ``defn``: an Intel HEX (at its own addresses -- an A2L's), or a plain
    binary placed where the definition places itself in it."""
    data = bytes(data)
    if data[:1] == b":":
        img = _intel_hex(data)
        if img is not None:
            return img
    try:
        return data, defn.base_in(data)
    except Exception:
        return data, 0


def _intel_hex(data: bytes) -> Optional[Tuple[bytes, int]]:
    mem = {}
    ext = 0
    try:
        for line in data.decode("ascii", "replace").splitlines():
            line = line.strip()
            if not line.startswith(":"):
                continue
            b = bytes.fromhex(line[1:])
            n, a, t = b[0], (b[1] << 8) | b[2], b[3]
            body = b[4:4 + n]
            if t == 0:
                for i, x in enumerate(body):
                    mem[ext + a + i] = x
            elif t == 2:
                ext = ((body[0] << 8) | body[1]) << 4
            elif t == 4:
                ext = ((body[0] << 8) | body[1]) << 16
            elif t == 1:
                break
    except (ValueError, IndexError):
        return None
    if not mem:
        return None
    lo, hi = min(mem), max(mem)
    if hi - lo > 0x2000000:
        return None
    img = bytearray(b"\xff" * (hi - lo + 1))
    for k, v in mem.items():
        img[k - lo] = v
    return bytes(img), lo


# ------------------------------------------------------------------ keeping
def to_dict(pl: Placement) -> dict:
    """``pl`` as plain data (JSON)."""
    whys: List[str] = []
    at: Dict[str, int] = {}
    rows = []
    for p in pl.items:
        if p.why not in at:
            at[p.why] = len(whys)
            whys.append(p.why)
        rows.append([p.index, p.shift, GRADES.index(p.grade), at[p.why]])
    return {"v": 1, "base": pl.base, "confirmed": pl.confirmed,
            "checkable": pl.checkable, "reference": pl.reference,
            "override": pl.override, "candidates": [list(c) for c in pl.candidates],
            "header_rate": pl.header_rate, "noise": pl.noise,
            "established": pl.established, "whys": whys, "items": rows}


def from_dict(d: dict) -> Placement:
    pl = Placement(base=int(d["base"]), confirmed=int(d.get("confirmed", 0)),
                   checkable=int(d.get("checkable", 0)),
                   reference=bool(d.get("reference")), override=bool(d.get("override")),
                   candidates=[tuple(c) for c in d.get("candidates", [])],
                   header_rate=float(d.get("header_rate", 0.0)),
                   noise=float(d.get("noise", 0.0)),
                   established=bool(d.get("established", True)))
    whys = d.get("whys", [])
    pl.items = [Placed(int(i), int(s), GRADES[g], whys[w] if w < len(whys) else "")
                for i, s, g, w in d.get("items", [])]
    return pl


def fits(defn: Definition, data: bytes, pl: Placement,
         reference: Optional[Tuple[bytes, int]] = None, sample: int = 80) -> bool:
    """A placement worked out before still holds on ``data`` (the same
    software, values edited since): most of a sample of the tables it
    confirmed are still confirmed where it put them."""
    data = bytes(data)
    if reference is None:
        reference = defn.reference
    ref = _Ref(*reference) if reference is not None else None
    if len(pl.items) != len([t for t in defn.tables if _anchor(t) is not None]):
        return False
    probes = _probes(defn, ref)
    if not any(p.header for p in probes) and ref is None:
        return False                # (worked out again: it is quick)
    by_i = {p.index: p for p in probes}
    strong = [p for p in pl.items if p.grade in ("exact", "moved", "region")
              and p.index in by_i and (by_i[p.index].header or by_i[p.index].fp)]
    if not strong:
        return not any(p.grade in ("exact", "moved", "region") for p in pl.items)
    pick = strong[::max(1, len(strong) // sample)]
    ok = sum(1 for p in pick if _confirm(defn, by_i, p.index, data, pl.base, p.shift, ref)
             in ("reference", "header"))
    return ok >= 0.8 * len(pick)


_MEMO: "OrderedDict[str, Tuple[str, Placement]]" = OrderedDict()


def _cache_dir() -> Optional[str]:
    """Where placements are remembered between runs (``OVT_PLACEMENT_CACHE``
    overrides; empty: not remembered on disk)."""
    env = os.environ.get("OVT_PLACEMENT_CACHE")
    if env is not None:
        return env or None
    try:
        from ..recent import _config_dir
        return os.path.join(_config_dir(), "placements")
    except Exception:
        return None


def placed(defn: Definition, data: bytes, key: str,
           reference: Optional[Tuple[bytes, int]] = None, base: Optional[int] = None,
           relocate: bool = True, progress=None) -> Placement:
    """:func:`place`, remembered: ``key`` names the definition (and the
    options); a placement worked out for it before is used again while it
    still :func:`fits` the image (an edited value does not move a table),
    in this run of the app or a later one."""
    import hashlib
    import json
    data = bytes(data)
    k = hashlib.sha1(("%s|%s|%s|%d" % (key, "-" if base is None else base,
                                       hashlib.sha1(reference[0]).hexdigest()
                                       if reference else "", relocate)).encode()).hexdigest()
    digest = hashlib.sha1(data).hexdigest()
    hit = _MEMO.get(k)
    folder = _cache_dir()
    path = os.path.join(folder, k + ".json") if folder else None
    if hit is None and path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            hit = (d.get("image", ""), from_dict(d["placement"]))
        except Exception:
            hit = None
    if hit is not None and (hit[0] == digest or fits(defn, data, hit[1], reference)):
        _MEMO[k] = (digest, hit[1])
        _MEMO.move_to_end(k)
        return hit[1]
    pl = place(defn, data, reference=reference, base=base, relocate=relocate,
               progress=progress)
    _MEMO[k] = (digest, pl)
    while len(_MEMO) > 16:
        _MEMO.popitem(last=False)
    if path:
        try:
            os.makedirs(folder, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"image": digest, "placement": to_dict(pl)}, fh)
            os.replace(tmp, path)
        except OSError:
            pass                    # remembering is a convenience
    return pl


__all__ = ["place", "placed", "relocated", "reference_from_file", "fits",
           "to_dict", "from_dict", "Placement", "Placed", "Region", "GRADES", "SHOWN"]

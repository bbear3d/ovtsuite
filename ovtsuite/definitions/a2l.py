# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ASAP2 (``.a2l``) calibration descriptions.

A real ASAP2 reader rather than a pattern match: the file is tokenised
(comments, quoted strings with escapes), its ``/begin ... /end`` blocks are
nested into a tree, and each object is read by the standard's own grammar --
the positional parameters every block type has, then its optional keywords.

From that, every ``CHARACTERISTIC`` becomes a table whose addresses follow
its ``RECORD_LAYOUT`` exactly: the layout's elements are placed in their
position order, each aligned as ``ALIGNMENT_*`` says, so the values of a
map are where the layout puts them, after whatever axis counts and axis
points it stores first. Axes come from that layout (``STD_AXIS``), from a
shared ``AXIS_PTS`` (``COM_AXIS``), from another curve (``CURVE_AXIS``), or
are given by the description itself (``FIX_AXIS``). Conversions are the
``COMPU_METHOD``'s: identity, linear, rational (inverted to raw -> physical),
formula, or a value table (whose texts are listed in the description).

Addresses are ECU addresses; :func:`resolve_base` finds the offset that
maps them onto a given image from the description's memory segments (or,
when it has none, from where its parameters lie).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .model import AxisDef, Definition, DefinitionError, Storage, TableDef

# ------------------------------------------------------------------ datatypes
#: ASAP2 datatype -> (bytes, signed, float)
DATATYPES = {
    "UBYTE": (1, False, False), "SBYTE": (1, True, False),
    "UWORD": (2, False, False), "SWORD": (2, True, False),
    "ULONG": (4, False, False), "SLONG": (4, True, False),
    "A_UINT64": (8, False, False), "A_INT64": (8, True, False),
    "FLOAT16_IEEE": (2, True, True), "FLOAT32_IEEE": (4, True, True),
    "FLOAT64_IEEE": (8, True, True),
}
_ALIGN_KEY = {(1, False): "ALIGNMENT_BYTE", (2, False): "ALIGNMENT_WORD",
              (4, False): "ALIGNMENT_LONG", (8, False): "ALIGNMENT_INT64",
              (2, True): "ALIGNMENT_FLOAT16_IEEE",
              (4, True): "ALIGNMENT_FLOAT32_IEEE",
              (8, True): "ALIGNMENT_FLOAT64_IEEE"}
_RESERVED_SIZE = {"BYTE": 1, "WORD": 2, "LONG": 4}


# ------------------------------------------------------------------ tokenising
class Q(str):
    """A quoted string token (its text, unescaped)."""


_TOKEN = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:[^"\\]|\\.|"")*"|[^\s"]+', re.S)
_SKIP_BLOCKS = {"IF_DATA", "A2ML"}


@dataclass
class Block:
    kind: str
    items: list = field(default_factory=list)

    def args(self) -> list:
        return [i for i in self.items if not isinstance(i, Block)]

    def blocks(self, kind: Optional[str] = None) -> List["Block"]:
        return [i for i in self.items if isinstance(i, Block)
                and (kind is None or i.kind == kind)]

    def keyword(self, key: str, n: int = 1, start: int = 0):
        """The ``n`` tokens after keyword ``key`` among this block's own
        tokens (from position ``start``), or None."""
        toks = self.args()
        for i in range(start, len(toks)):
            t = toks[i]
            if t == key and not isinstance(t, Q):
                return toks[i + 1:i + 1 + n] if n else []
        return None


def _unescape(t: str) -> str:
    body = t[1:-1]
    if "\\" in body or '""' in body:
        body = body.replace('""', '"')
        body = re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(
            m.group(1), m.group(1)), body)
    return body


def parse_tree(text: str) -> Block:
    """The block tree of an ASAP2 file."""
    root = Block("ROOT")
    stack = [root]
    skipping = 0
    toks = _TOKEN.findall(text)
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        i += 1
        if t.startswith("/*") or t.startswith("//"):
            continue
        if t == "/begin":
            name = toks[i] if i < n else ""
            i += 1
            if skipping or name in _SKIP_BLOCKS:
                skipping += 1
                continue
            b = Block(name)
            stack[-1].items.append(b)
            stack.append(b)
            continue
        if t == "/end":
            i += 1
            if skipping:
                skipping -= 1
            elif len(stack) > 1:
                stack.pop()
            continue
        if skipping:
            continue
        stack[-1].items.append(Q(_unescape(t)) if t.startswith('"') else t)
    return root


def _num(t) -> float:
    t = str(t)
    if t.lower().startswith(("0x", "-0x", "+0x")):
        return float(int(t, 16))
    return float(t)


def _int(t) -> int:
    t = str(t)
    if t.lower().startswith(("0x", "-0x", "+0x")):
        return int(t, 16)
    return int(float(t))


# ------------------------------------------------------------------ objects
@dataclass
class Compu:
    kind: str = "IDENTICAL"
    unit: str = ""
    fmt: str = ""
    factor: float = 1.0
    offset: float = 0.0
    equation: str = ""
    texts: Dict[int, str] = field(default_factory=dict)
    note: str = ""


@dataclass
class LayoutItem:
    pos: float
    kind: str
    dtype: str = ""
    mode: str = ""            # FNC_VALUES index mode
    size: int = 0             # RESERVED


@dataclass
class Layout:
    items: List[LayoutItem]
    align: Dict[str, int]
    fix_no: Dict[str, int]
    static: bool


def _dsize(dtype: str) -> Tuple[int, bool, bool]:
    return DATATYPES.get(dtype.upper(), (2, False, False))


class _Module:
    def __init__(self, mod: Block):
        self.mod = mod
        self.name = str(mod.args()[0]) if mod.args() else ""
        self.byte_order = ">"
        self.align: Dict[str, int] = {}
        for mc in mod.blocks("MOD_COMMON"):
            bo = mc.keyword("BYTE_ORDER")
            if bo:
                self.byte_order = _order(bo[0], ">")
            for key in _ALIGN_KEY.values():
                v = mc.keyword(key)
                if v:
                    self.align[key] = max(1, _int(v[0]))
        self.segments: List[Tuple[int, int, str]] = []
        for mp in mod.blocks("MOD_PAR"):
            for ms in mp.blocks("MEMORY_SEGMENT"):
                a = ms.args()
                try:
                    self.segments.append((_int(a[5]), _int(a[6]),
                                          "%s %s" % (a[2], a[3])))
                except (IndexError, ValueError):
                    pass
        self.compu = {str(b.args()[0]): b for b in mod.blocks("COMPU_METHOD") if b.args()}
        self.tabs = {}
        for kind in ("COMPU_TAB", "COMPU_VTAB", "COMPU_VTAB_RANGE"):
            for b in mod.blocks(kind):
                if b.args():
                    self.tabs[str(b.args()[0])] = b
        self.layouts = {str(b.args()[0]): b for b in mod.blocks("RECORD_LAYOUT") if b.args()}
        self.axis_pts = {str(b.args()[0]): b for b in mod.blocks("AXIS_PTS") if b.args()}
        self.chars = [b for b in mod.blocks("CHARACTERISTIC") if b.args()]
        self.char_by_name = {str(b.args()[0]): b for b in self.chars}
        self._compu_cache: Dict[str, Compu] = {}
        self._layout_cache: Dict[str, Layout] = {}

    # -- conversions -----------------------------------------------------
    def compu_of(self, name: str) -> Compu:
        name = str(name)
        if name in self._compu_cache:
            return self._compu_cache[name]
        c = Compu()
        b = self.compu.get(name)
        if b is not None:
            a = b.args()
            kind = str(a[2]).upper() if len(a) > 2 else "IDENTICAL"
            c.kind = kind
            c.fmt = str(a[3]) if len(a) > 3 else ""
            c.unit = str(a[4]) if len(a) > 4 else ""
            if kind == "LINEAR":
                v = b.keyword("COEFFS_LINEAR", 2)
                if v:
                    c.factor, c.offset = _num(v[0]), _num(v[1])
            elif kind == "RAT_FUNC":
                v = b.keyword("COEFFS", 6)
                if v:
                    _rat(c, [_num(x) for x in v])
            elif kind == "FORM":
                f = None
                for fb in b.blocks("FORMULA"):
                    if fb.args():
                        f = str(fb.args()[0])
                if f:
                    eq = re.sub(r"\b[Xx]1?\b", "X", f)
                    if re.match(r"^[0-9X\s\.\+\-\*\/\(\)Ee]+$", eq):
                        c.equation = eq
                    else:
                        c.note = "conversion formula %s (shown raw)" % f
            elif kind in ("TAB_VERB", "TAB_INTP", "TAB_NOINTP"):
                ref = b.keyword("COMPU_TAB_REF")
                tab = self.tabs.get(str(ref[0])) if ref else None
                if tab is not None:
                    _table(c, tab)
        self._compu_cache[name] = c
        return c

    # -- record layouts --------------------------------------------------
    def layout_of(self, name: str) -> Optional[Layout]:
        name = str(name)
        if name in self._layout_cache:
            return self._layout_cache[name]
        b = self.layouts.get(name)
        if b is None:
            return None
        toks = b.args()[1:]
        items: List[LayoutItem] = []
        align: Dict[str, int] = {}
        fix: Dict[str, int] = {}
        static = False
        i = 0
        while i < len(toks):
            t = str(toks[i]).upper()
            if t == "FNC_VALUES" and i + 4 < len(toks):
                items.append(LayoutItem(_num(toks[i + 1]), t, str(toks[i + 2]),
                                        str(toks[i + 3]).upper()))
                i += 5
            elif t in ("AXIS_PTS_X", "AXIS_PTS_Y", "AXIS_PTS_Z") and i + 4 < len(toks):
                items.append(LayoutItem(_num(toks[i + 1]), t, str(toks[i + 2])))
                i += 5
            elif t.startswith("AXIS_RESCALE_") and i + 5 < len(toks):
                items.append(LayoutItem(_num(toks[i + 1]), t, str(toks[i + 2]),
                                        size=_int(toks[i + 3])))
                i += 6
            elif t in ("NO_AXIS_PTS_X", "NO_AXIS_PTS_Y", "NO_AXIS_PTS_Z",
                       "SRC_ADDR_X", "SRC_ADDR_Y", "SRC_ADDR_Z",
                       "RIP_ADDR_X", "RIP_ADDR_Y", "RIP_ADDR_Z", "RIP_ADDR_W",
                       "OFFSET_X", "OFFSET_Y", "OFFSET_Z",
                       "SHIFT_OP_X", "SHIFT_OP_Y", "SHIFT_OP_Z",
                       "DIST_OP_X", "DIST_OP_Y", "DIST_OP_Z",
                       "NO_RESCALE_X", "NO_RESCALE_Y", "IDENTIFICATION") \
                    and i + 2 < len(toks):
                items.append(LayoutItem(_num(toks[i + 1]), t, str(toks[i + 2])))
                i += 3
            elif t == "RESERVED" and i + 2 < len(toks):
                items.append(LayoutItem(_num(toks[i + 1]), t,
                                        size=_RESERVED_SIZE.get(str(toks[i + 2]).upper(), 1)))
                i += 3
            elif t.startswith("FIX_NO_AXIS_PTS_") and i + 1 < len(toks):
                fix[t[-1]] = _int(toks[i + 1])
                i += 2
            elif t.startswith("ALIGNMENT_") and i + 1 < len(toks):
                align[t] = max(1, _int(toks[i + 1]))
                i += 2
            elif t == "STATIC_RECORD_LAYOUT":
                static = True
                i += 1
            else:
                i += 1
        items.sort(key=lambda it: it.pos)
        lay = Layout(items, align, fix, static)
        self._layout_cache[name] = lay
        return lay

    def alignment(self, lay: Layout, size: int, flt: bool) -> int:
        key = _ALIGN_KEY.get((size, flt), "ALIGNMENT_BYTE")
        if key in lay.align:
            return lay.align[key]
        if key in self.align:
            return self.align[key]
        return size if size in (1, 2, 4, 8) else 1


def _order(tok, default: str) -> str:
    t = str(tok).upper()
    if t in ("MSB_FIRST", "BIG_ENDIAN", "MSB_FIRST_MSW_LAST"):
        return ">"
    if t in ("MSB_LAST", "LITTLE_ENDIAN", "MSB_LAST_MSW_FIRST"):
        return "<"
    return default


def _rat(c: Compu, k: List[float]) -> None:
    """RAT_FUNC gives raw = (a p^2 + b p + c) / (d p^2 + e p + f); invert
    the linear forms (a = d = 0) to physical = (f X - c) / (b - e X)."""
    a, b, cc, d, e, f = k
    if a == 0 and d == 0:
        if e == 0 and b != 0:
            c.factor, c.offset = f / b, -cc / b
            return
        if b != 0 or e != 0:
            c.equation = "(%r*X-%r)/(%r-%r*X)" % (f, cc, b, e)
            return
    c.note = "a quadratic conversion (shown raw)"


def _table(c: Compu, tab: Block) -> None:
    a = tab.args()
    first = 2 if tab.kind == "COMPU_VTAB_RANGE" else 3
    try:
        n = _int(a[first])
    except (IndexError, ValueError):
        return
    vals = a[first + 1:]
    pairs = []
    if tab.kind == "COMPU_VTAB":
        for i in range(n):
            try:
                c.texts[_int(vals[2 * i])] = str(vals[2 * i + 1])
            except (IndexError, ValueError):
                break
        return
    if tab.kind == "COMPU_VTAB_RANGE":
        for i in range(n):
            try:
                lo, hi, txt = _int(vals[3 * i]), _int(vals[3 * i + 1]), str(vals[3 * i + 2])
                c.texts[lo] = txt if lo == hi else "%s (to %d)" % (txt, hi)
            except (IndexError, ValueError):
                break
        return
    for i in range(n):
        try:
            pairs.append((_num(vals[2 * i]), _num(vals[2 * i + 1])))
        except (IndexError, ValueError):
            break
    if len(pairs) >= 2:
        (x0, y0), (x1, y1) = pairs[0], pairs[-1]
        if x1 != x0:
            f = (y1 - y0) / (x1 - x0)
            o = y0 - f * x0
            if all(abs(f * x + o - y) <= 1e-9 * max(1.0, abs(y)) for x, y in pairs):
                c.factor, c.offset = f, o
                return
    c.note = "a value table conversion (shown raw)"


# ------------------------------------------------------------- characteristics
def _storage(m: _Module, dtype: str, addr: Optional[int], compu: Compu,
             order: str, units: str = "") -> Storage:
    size, signed, flt = _dsize(dtype)
    return Storage(addr=addr, bits=size * 8, signed=signed and not flt,
                   float=flt, endian=order if size > 1 else "",
                   factor=compu.factor, offset=compu.offset,
                   equation=compu.equation, units=units or compu.unit,
                   texts=dict(compu.texts) if compu.texts else None)


def _place(m: _Module, lay: Layout, counts: Dict[str, int], cells: int):
    """Offsets of a layout's elements: ``{kind: (offset, dtype)}``."""
    off = 0
    out = {}
    for it in lay.items:
        if it.kind == "RESERVED":
            off += it.size
            continue
        size, _s, flt = _dsize(it.dtype)
        al = m.alignment(lay, size, flt)
        if al > 1 and off % al:
            off += al - off % al
        out[it.kind] = (off, it.dtype)
        if it.kind == "FNC_VALUES":
            off += size * cells
        elif it.kind.startswith("AXIS_PTS_"):
            off += size * counts.get(it.kind[-1], 1)
        elif it.kind.startswith("AXIS_RESCALE_"):
            off += size * 2 * max(1, it.size)
        else:
            off += size
    return out


def _axis_def(m: _Module, ad: Block, lay: Layout, placed, base_addr: int,
              order: str, axis: str, warnings: List[str], char: str) -> Tuple[Optional[AxisDef], int]:
    a = ad.args()
    attr = str(a[0]).upper() if a else "STD_AXIS"
    try:
        count = _int(a[3])
    except (IndexError, ValueError):
        count = 1
    compu = m.compu_of(a[2]) if len(a) > 2 else Compu()
    unit = ad.keyword("PHYS_UNIT")
    units = str(unit[0]) if unit else compu.unit
    name = str(a[1]) if len(a) > 1 and str(a[1]) != "NO_INPUT_QUANTITY" else ""
    bo = ad.keyword("BYTE_ORDER")
    aorder = _order(bo[0], order) if bo else order
    if attr == "STD_AXIS":
        key = "AXIS_PTS_" + axis
        if key in placed:
            off, dt = placed[key]
            return AxisDef(count, values=_storage(m, dt, base_addr + off, compu,
                                                  aorder, units), name=name), count
        return None, count
    if attr == "COM_AXIS":
        ref = ad.keyword("AXIS_PTS_REF")
        ap = m.axis_pts.get(str(ref[0])) if ref else None
        if ap is None:
            return None, count
        st = _axis_pts_storage(m, ap, order)
        if st is None:
            return None, count
        return AxisDef(count, values=st, name=name or str(ap.args()[0]),
                       ref=str(ap.args()[0])), count
    if attr == "CURVE_AXIS":
        ref = ad.keyword("CURVE_AXIS_REF")
        cb = m.char_by_name.get(str(ref[0])) if ref else None
        if cb is not None:
            t = _characteristic(m, cb, order, [], {})
            if t is not None and t.kind == "curve":
                return AxisDef(count, values=t.z, name=name or t.name,
                               ref=t.name), count
        return None, count
    if attr == "FIX_AXIS":
        vals = None
        p = ad.keyword("FIX_AXIS_PAR", 3)
        if p:
            o, sh, n = _num(p[0]), _int(p[1]), _int(p[2])
            vals = [o + i * (2 ** sh) for i in range(n)]
        p = ad.keyword("FIX_AXIS_PAR_DIST", 3)
        if p:
            o, dist, n = _num(p[0]), _num(p[1]), _int(p[2])
            vals = [o + i * dist for i in range(n)]
        for lb in ad.blocks("FIX_AXIS_PAR_LIST"):
            vals = [_num(x) for x in lb.args()]
        if vals:
            st = Storage(factor=compu.factor, offset=compu.offset,
                         equation=compu.equation)
            from ..equation import Equation
            eq = Equation(st.equation_text())
            phys = [eq.decode(v) for v in vals]
            labels = None
            if compu.texts and all(float(v).is_integer() and int(v) in compu.texts
                                   for v in vals):
                # an enumeration axis: each point's text from the verbal table
                labels = [compu.texts[int(v)] for v in vals]
            return AxisDef(len(vals), fixed=phys, labels=labels, name=name), len(vals)
        return None, count
    warnings.append("%s: %s axes are not read" % (char, attr))
    return None, count


def _axis_pts_storage(m: _Module, ap: Block, order: str) -> Optional[Storage]:
    a = ap.args()
    try:
        addr = _int(a[2])
    except (IndexError, ValueError):
        return None
    lay = m.layout_of(a[4]) if len(a) > 4 else None
    compu = m.compu_of(a[6]) if len(a) > 6 else Compu()
    try:
        count = _int(a[7])
    except (IndexError, ValueError):
        count = 1
    if lay is None:
        return None
    bo = ap.keyword("BYTE_ORDER")
    aorder = _order(bo[0], order) if bo else order
    placed = _place(m, lay, {"X": count}, 0)
    for key in ("AXIS_PTS_X", "AXIS_PTS_Y"):
        if key in placed:
            off, dt = placed[key]
            unit = ap.keyword("PHYS_UNIT")
            return _storage(m, dt, addr + off, compu, aorder,
                            str(unit[0]) if unit else "")
    return None


class _Dynamic:
    """Re-lays a characteristic out with the axis counts its image stores
    (``NO_AXIS_PTS_X/Y``, here or in a shared ``AXIS_PTS``): the counts a
    description gives are maxima, and a layout without
    ``STATIC_RECORD_LAYOUT`` packs its elements by the actual counts."""

    def __init__(self, m, lay, addr, table, std, com, order):
        self.m, self.lay, self.addr, self.table = m, lay, addr, table
        self.std, self.com, self.order = std, com, order

    def evidence(self, read):
        """``(checked, passed)``: each point count the layout stores
        (``NO_AXIS_PTS_X/Y``) is between 1 and the axis' declared maximum."""
        t = self.table
        maxc = {"X": t.cols, "Y": t.rows if t.kind == "map" else 1}
        placed = _place(self.m, self.lay, maxc, maxc["X"] * maxc["Y"])
        checked = passed = 0
        for ax in ("X", "Y"):
            if ax == "Y" and t.kind != "map":
                continue
            key = "NO_AXIS_PTS_" + ax
            n = None
            if self.std.get(ax) and key in placed:
                off, dt = placed[key]
                size, signed, _f = _dsize(dt)
                n = read(self.addr + off, size, signed, self.order)
            elif ax in self.com:
                ap_addr, ap_lay = self.com[ax]
                ap_placed = _place(self.m, ap_lay, {"X": maxc[ax]}, 0)
                if "NO_AXIS_PTS_X" not in ap_placed:
                    continue
                off, dt = ap_placed["NO_AXIS_PTS_X"]
                size, signed, _f = _dsize(dt)
                n = read(ap_addr + off, size, signed, self.order)
            else:
                continue
            checked += 1
            if n is not None and 1 <= n <= maxc[ax]:
                passed += 1
        return checked, passed

    def __call__(self, read):
        import copy
        t = self.table
        maxc = {"X": t.cols, "Y": t.rows if t.kind == "map" else 1}
        counts = dict(maxc)
        placed = _place(self.m, self.lay, counts, counts["X"] * counts["Y"])
        changed = False
        for ax in ("X", "Y"):
            if ax == "Y" and t.kind != "map":
                continue
            key = "NO_AXIS_PTS_" + ax
            n = None
            if self.std.get(ax) and key in placed:
                off, dt = placed[key]
                size, signed, _f = _dsize(dt)
                n = read(self.addr + off, size, signed, self.order)
            elif ax in self.com:
                ap_addr, ap_lay = self.com[ax]
                ap_placed = _place(self.m, ap_lay, {"X": maxc[ax]}, 0)
                if "NO_AXIS_PTS_X" in ap_placed:
                    off, dt = ap_placed["NO_AXIS_PTS_X"]
                    size, signed, _f = _dsize(dt)
                    n = read(ap_addr + off, size, signed, self.order)
            if n is not None and 1 <= n <= maxc[ax] and n != counts[ax]:
                counts[ax] = n
                changed = True
        if not changed:
            return t
        new = copy.copy(t)
        new.cols = counts["X"] if t.kind != "constant" else 1
        new.rows = counts["Y"] if t.kind == "map" else 1
        if not self.lay.static:
            placed = _place(self.m, self.lay, counts, counts["X"] * counts["Y"])
            new.z = copy.copy(t.z)
            new.z.addr = self.addr + placed["FNC_VALUES"][0]
        for ax, attr in (("X", "x"), ("Y", "y")):
            a = getattr(t, attr)
            if a is None:
                continue
            a2 = copy.copy(a)
            a2.count = counts[ax]
            if a.values is not None and self.std.get(ax) and not self.lay.static \
                    and "AXIS_PTS_" + ax in placed:
                a2.values = copy.copy(a.values)
                a2.values.addr = self.addr + placed["AXIS_PTS_" + ax][0]
            if a.fixed:
                a2.fixed = a.fixed[:counts[ax]]
            setattr(new, attr, a2)
        new.dynamic = None
        return new


def _characteristic(m: _Module, b: Block, order: str, warnings: List[str],
                    cats: Dict[str, str]) -> Optional[TableDef]:
    a = b.args()
    if len(a) < 7:
        return None
    name, desc = str(a[0]), str(a[1])
    ctype = str(a[2]).upper()
    try:
        addr = _int(a[3])
    except ValueError:
        return None
    lay = m.layout_of(a[4])
    if lay is None:
        warnings.append("%s: record layout %s is not defined" % (name, a[4]))
        return None
    fnc = next((it for it in lay.items if it.kind == "FNC_VALUES"), None)
    if fnc is None:
        return None
    compu = m.compu_of(a[6])
    bo = b.keyword("BYTE_ORDER")
    corder = _order(bo[0], order) if bo else order
    unit = b.keyword("PHYS_UNIT")
    units = str(unit[0]) if unit else compu.unit
    axes = b.blocks("AXIS_DESCR")
    if ctype == "VALUE":
        kind, nx, ny = "constant", 1, 1
    elif ctype in ("CURVE", "MAP"):
        kind = "curve" if ctype == "CURVE" else "map"
        need = 1 if ctype == "CURVE" else 2
        if len(axes) < need:
            return None
        nx = _ax_count(axes[0], lay, "X")
        ny = _ax_count(axes[1], lay, "Y") if ctype == "MAP" else 1
    elif ctype == "VAL_BLK":
        dims = b.keyword("MATRIX_DIM", 3) or b.keyword("MATRIX_DIM", 2)
        num = b.keyword("NUMBER")
        if dims:
            vals = [max(1, _int(x)) for x in dims if str(x).lstrip("-").isdigit()]
            nx = vals[0] if vals else 1
            ny = vals[1] if len(vals) > 1 else 1
        elif num:
            nx, ny = max(1, _int(num[0])), 1
        else:
            return None
        kind = "map" if ny > 1 else ("curve" if nx > 1 else "constant")
    elif ctype == "ASCII":
        # characters (a part number): NUMBER of them, or MATRIX_DIM
        num = b.keyword("NUMBER") or b.keyword("MATRIX_DIM", 1)
        if not num or not str(num[0]).isdigit():
            return None
        nx, ny, kind = max(1, _int(num[0])), 1, "curve"
    else:
        return None                             # CUBOID, CUBE_4 ...
    counts = {"X": nx, "Y": ny}
    placed = _place(m, lay, counts, nx * ny)
    off, dt = placed["FNC_VALUES"]
    z = _storage(m, dt, addr + off, compu, corder, units)
    if z.float and z.bits not in (32,):
        warnings.append("%s: %d-bit floating point values are not read" % (name, z.bits))
        return None
    order_rc = "col" if fnc.mode in ("COLUMN_DIR",) else "row"
    x = y = None
    if ctype in ("CURVE", "MAP"):
        x, _n = _axis_def(m, axes[0], lay, placed, addr, corder, "X", warnings, name)
        if ctype == "MAP":
            y, _n = _axis_def(m, axes[1], lay, placed, addr, corder, "Y", warnings, name)
    note_bits = []
    if compu.texts:
        note_bits.append("Values: " + ", ".join(
            "%d = %s" % (k, v) for k, v in sorted(compu.texts.items())[:24]))
    if compu.note:
        note_bits.append("The description uses %s." % compu.note)
    bm = b.keyword("BIT_MASK")
    if bm:
        note_bits.append("Bit mask %s." % bm[0])
    description = desc + ("\n" + "\n".join(note_bits) if note_bits else "")
    t = TableDef(name=name, kind=kind, z=z, rows=ny if kind == "map" else 1,
                 cols=nx if kind != "constant" else 1,
                 description=description,
                 categories=[cats[name]] if name in cats else [],
                 order=order_rc, x=x, y=y, decimals=_decimals(compu.fmt),
                 text_kind="ascii" if ctype == "ASCII" else "")
    if ctype in ("CURVE", "MAP"):
        std, com = {}, {}
        for ax, ad in zip(("X", "Y"), axes[:2]):
            attr = str(ad.args()[0]).upper() if ad.args() else ""
            std[ax] = attr == "STD_AXIS"
            if attr == "COM_AXIS":
                ref = ad.keyword("AXIS_PTS_REF")
                ap = m.axis_pts.get(str(ref[0])) if ref else None
                if ap is not None and len(ap.args()) > 4:
                    ap_lay = m.layout_of(ap.args()[4])
                    try:
                        if ap_lay is not None:
                            com[ax] = (_int(ap.args()[2]), ap_lay)
                    except ValueError:
                        pass
        has_counts = any(it.kind.startswith("NO_AXIS_PTS_") for it in lay.items)
        if has_counts or com:
            t.dynamic = _Dynamic(m, lay, addr, t, std, com, corder)
    t.record = addr
    return t


def _ax_count(ad: Block, lay: Layout, axis: str) -> int:
    a = ad.args()
    if axis in lay.fix_no:
        return max(1, lay.fix_no[axis])
    attr = str(a[0]).upper() if a else ""
    if attr == "FIX_AXIS":
        for key in ("FIX_AXIS_PAR", "FIX_AXIS_PAR_DIST"):
            p = ad.keyword(key, 3)
            if p:
                return max(1, _int(p[2]))
        for lb in ad.blocks("FIX_AXIS_PAR_LIST"):
            return max(1, len(lb.args()))
    try:
        return max(1, _int(a[3]))
    except (IndexError, ValueError):
        return 1


def _decimals(fmt: str) -> Optional[int]:
    mm = re.match(r'^%\d*\.(\d+)', fmt or "")
    return int(mm.group(1)) if mm else None


def _categories(m: _Module) -> Dict[str, str]:
    """Characteristic -> the function (else group) it belongs to."""
    out: Dict[str, str] = {}
    weak: Dict[str, str] = {}
    for kind in ("FUNCTION", "GROUP"):
        for fb in m.mod.blocks(kind):
            a = fb.args()
            if not a:
                continue
            label = str(a[0])
            if len(a) > 1 and str(a[1]).strip():
                label = "%s: %s" % (a[0], str(a[1]).strip())
            label = label[:90]
            for sub, target in (("DEF_CHARACTERISTIC", out),
                                ("REF_CHARACTERISTIC", weak)):
                for sb in fb.blocks(sub):
                    for nm in sb.args():
                        target.setdefault(str(nm), label)
    for k, v in weak.items():
        out.setdefault(k, v)
    return out


# ------------------------------------------------------------------ public
def is_a2l(data: bytes) -> bool:
    head = bytes(data[:65536]).decode("latin-1", "replace")
    return "ASAP2_VERSION" in head or ("/begin PROJECT" in head) or \
        ("/begin MODULE" in head)


def load(data: bytes, title: str = "") -> Definition:
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    else:
        text = data
    if "/begin" not in text:
        raise DefinitionError("not an ASAP2 file")
    root = parse_tree(text)
    mods = [b for p in root.blocks("PROJECT") for b in p.blocks("MODULE")] or \
        root.blocks("MODULE")
    if not mods:
        raise DefinitionError("ASAP2 file without a MODULE")
    m = _Module(mods[0])
    defn = Definition(type="a2l", title=title or m.name)
    defn.segments = [(s, n) for s, n, kind in m.segments
                     if not kind.upper().startswith(("VARIABLES", "RESERVED"))]
    cats = _categories(m)
    for cb in m.chars:
        try:
            t = _characteristic(m, cb, m.byte_order, defn.warnings, cats)
        except (ValueError, IndexError, KeyError) as ex:
            defn.warnings.append("%s: %s" % (cb.args()[0], ex))
            continue
        if t is not None:
            defn.tables.append(t)
    if len(mods) > 1:
        defn.warnings.append("only the first of %d modules is read" % len(mods))
    return defn


def resolve_base(defn: Definition, size: int) -> int:
    """The offset to subtract from the description's addresses to land in an
    image of ``size`` bytes: the start of the memory segment (or of the
    ``size``-aligned window) holding most of its parameters, checked by
    how many of them then fit the image."""
    addrs = [t.z.addr for t in defn.tables if t.z.addr is not None]
    if not addrs or size <= 0:
        return 0
    cands = {0}
    for start, _n in defn.segments:
        cands.add(start)
    for a in addrs[:2000]:
        cands.add(a - a % size)
        for blk in (0x10000, 0x40000, 0x80000, 0x100000):
            cands.add(a - a % blk)

    def fits(b):
        return sum(1 for a in addrs if 0 <= a - b < size)
    best = max(sorted(cands), key=lambda b: (fits(b), -abs(b)))
    return best


__all__ = ["load", "is_a2l", "parse_tree", "resolve_base", "DATATYPES"]

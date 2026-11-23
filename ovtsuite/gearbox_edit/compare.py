# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""One schedule set against another file's: the comparison binary.

The comparison's set is built by the same provider from the comparison
session; schedules pair by key (``SKL 11.0``). A pair is only compared cell
for cell when both maps have the same shape (pedal rows x lines) -- and the
same pedal axis, or a "row 4" would mean different pedal in the two files.
Pairs whose axes differ are still drawn (each on its own axis) but not
compared, shaded or copied -- unless the pair is *aligned*: the comparison's
values are then read at THIS file's pedal points (:func:`align_values`) and
compared, shaded and copied like any other.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .model import ScheduleSet, Scope
from .ops import SetValues


@dataclass
class Pairing:
    """How ``a`` (this file's set) lines up with ``b`` (the comparison's)."""
    a: Optional[ScheduleSet]
    b: Optional[ScheduleSet]
    #: primary schedule index -> comparison index, same shape
    pairs: Dict[int, int] = field(default_factory=dict)
    #: primary indices whose pair has a different pedal axis (drawn only)
    axis_differs: Set[int] = field(default_factory=set)
    #: of those, the ones whose comparison is read at this file's pedal points
    #: (:func:`align_values`), so they compare like the rest
    aligned: Set[int] = field(default_factory=set)
    #: primary line index -> comparison line index, when the two sets do not have
    #: the same lines (a lock-up map of 30 columns has three characteristics per
    #: gear, one of 40 has four): matched by name. Empty: the same lines.
    line_map: Dict[int, int] = field(default_factory=dict)
    #: primary keys with no counterpart in the comparison
    unmatched: List[str] = field(default_factory=list)
    #: why nothing pairs at all (None when something does)
    problem: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.problem is None and bool(self.pairs)

    def cmp_col(self, line: int) -> Optional[int]:
        """The comparison's column for primary line ``line`` (None: the comparison has
        no such line)."""
        return line if not self.line_map else self.line_map.get(line)

    def comparable(self, i: int) -> bool:
        """Cell-for-cell comparable: paired, same shape and pedal axis."""
        return i in self.pairs and (i not in self.axis_differs or i in self.aligned)

    def summary(self) -> str:
        if self.problem:
            return self.problem
        n, tot = len(self.pairs), len(self.a.schedules)
        out = "%d of %d %s paired" % (n, tot, "characteristics" if self.a.kind == "tcc"
                                       else "schedules")
        if self.axis_differs:
            n_al = len(self.axis_differs & self.aligned)
            out += ", %d with a different pedal axis (%s)" % (
                len(self.axis_differs),
                "read at this file's pedal points" if n_al == len(self.axis_differs)
                else "drawn, not compared")
        if self.line_map:
            gone = [ln.label for ln in self.a.lines if ln.index not in self.line_map]
            out += ", lines matched by name"
            if gone:
                out += " (%s not in the comparison)" % ", ".join(gone)
        if self.unmatched:
            out += "; not in the comparison: %s" % _short(self.unmatched)
        return out


def _short(keys: List[str], n: int = 4) -> str:
    return ", ".join(keys[:n]) + (" and %d more" % (len(keys) - n) if len(keys) > n else "")


def pair_sets(a: Optional[ScheduleSet], b: Optional[ScheduleSet]) -> Pairing:
    """Pair ``a``'s schedules with ``b``'s by key, and their lines by name.

    A schedule of another size is paired too: another number of pedal points is a pedal
    axis that differs (read at this file's points when aligned), another number of lines
    is a set with other lines (matched by name; the ones the comparison lacks are not
    compared)."""
    if a is None:
        return Pairing(a, b, problem="Nothing to compare")
    if b is None:
        return Pairing(a, b, problem="The comparison file has no %s to compare"
                       % ("lock-up characteristics" if a.kind == "tcc" else "shift schedules"))
    if b.kind != a.kind:
        return Pairing(a, b, problem="The comparison's set is of another kind")
    out = Pairing(a, b)
    if [(ln.index, ln.label) for ln in a.lines] != [(ln.index, ln.label) for ln in b.lines]:
        theirs = {ln.label: ln.index for ln in b.lines}
        out.line_map = {ln.index: theirs[ln.label] for ln in a.lines if ln.label in theirs}
        if not out.line_map:
            return Pairing(a, b, problem="The comparison's lines differ (%s against %s)" % (
                ", ".join(ln.label for ln in b.lines), ", ".join(ln.label for ln in a.lines)))
    keys = {s.key: j for j, s in enumerate(b.schedules)}
    for i, s in enumerate(a.schedules):
        j = keys.get(s.key)
        t = b.schedules[j] if j is not None else None
        if t is None:
            out.unmatched.append(s.key)
            continue
        out.pairs[i] = j
        if tuple(round(p, 3) for p in t.pedal) != tuple(round(p, 3) for p in s.pedal):
            out.axis_differs.add(i)
    if not out.pairs:
        out.problem = "No schedule of the comparison has this file's key"
    return out


def align_values(src_pedal: Sequence[float], values: Sequence[float], cols: int,
                 dst_pedal: Sequence[float], is_off=None,
                 columns: Optional[Sequence[Optional[int]]] = None) -> Optional[List[float]]:
    """``values`` (row-major, one row per ``src_pedal`` point, ``cols`` columns) read at
    ``dst_pedal``: each column interpolated along the pedal axis, the end values held beyond
    the ends. A stored "off" (``is_off(v)`` -- a speed that means "never") is no number to
    blend: a point between an "off" and a speed takes the nearer one's value. None when the
    source axis cannot be interpolated along (fewer than two points, not ascending).

    ``columns`` says which source column each result column is (``None``: the source has
    none, and the result holds None there) -- a lock-up map of three characteristics per
    gear laid over one of four; default: the same columns."""
    n = len(src_pedal)
    if n < 2 or len(values) != n * cols:
        return None
    if any(b < a for a, b in zip(src_pedal, src_pedal[1:])) or             any(p != p for p in list(src_pedal) + list(dst_pedal)):
        return None
    off = is_off or (lambda v: False)
    cmap = list(range(cols)) if columns is None else list(columns)
    out: List[float] = []
    for p in dst_pedal:
        if p <= src_pedal[0]:
            lo = hi = 0
        elif p >= src_pedal[-1]:
            lo = hi = n - 1
        else:
            hi = next(k for k in range(1, n) if src_pedal[k] >= p)
            lo = hi - 1
        span = src_pedal[hi] - src_pedal[lo]
        f = 0.0 if hi == lo or span == 0 else (p - src_pedal[lo]) / span
        for c in cmap:
            if c is None or not 0 <= c < cols:
                out.append(None)
                continue
            a, b = values[lo * cols + c], values[hi * cols + c]
            if hi == lo or f == 0.0:
                v = a
            elif f == 1.0:
                v = b
            elif off(a) or off(b):
                v = a if f < 0.5 else b
            else:
                v = a + f * (b - a)
            out.append(v)
    return out


def average_op(pairing: Pairing, cells: Iterable[Tuple[int, int, int]],
               values: Dict[int, List[float]], label: str, weight: float,
               current: Dict[int, List[float]]) -> Optional[SetValues]:
    """A step moving each ``(schedule, row, line)`` of ``cells`` (primary indices)
    ``weight`` percent of the way to the comparison's value: 0 keeps the cell, 50 averages
    the two, 100 is :func:`copy_op`. ``values`` is the comparison's row-major values by
    COMPARISON index, in the primary schedule's shape (``CompareView.cvalues``); ``current``
    the primary's. Cells that can't be compared, where the comparison has no value, where
    either side is "off", or that would not change are left out. None when nothing is left."""
    a = pairing.a
    vals: Dict[Tuple[int, int, int], float] = {}
    for i, r, c in cells:
        if not pairing.comparable(i):
            continue
        s = a.schedules[i]
        if r >= s.rows or c >= s.cols:
            continue
        k = r * s.cols + c
        other, mine = values[pairing.pairs[i]][k], current[i][k]
        if other is None or s.is_off(mine) or pairing.b.schedules[pairing.pairs[i]].is_off(other):
            continue
        v = mine + weight / 100.0 * (other - mine)
        if abs(v - mine) > 1e-9:
            vals[(i, r, c)] = v
    if not vals:
        return None
    scope = Scope(sorted({k[0] for k in vals}), sorted({k[2] for k in vals}))
    return SetValues(scope, vals, label)


def copy_op(pairing: Pairing, cells: Iterable[Tuple[int, int, int]],
            values: Dict[int, List[float]], label: str,
            current: Optional[Dict[int, List[float]]] = None) -> Optional[SetValues]:
    """A step writing the comparison's value into each ``(schedule, row,
    line)`` of ``cells`` (primary indices). ``values`` is the comparison's
    row-major values by COMPARISON index. Cells that can't be compared, and
    with ``current`` (primary values) those already equal, are left out.
    None when nothing is left."""
    a = pairing.a
    vals: Dict[Tuple[int, int, int], float] = {}
    for i, r, c in cells:
        if not pairing.comparable(i):
            continue
        s = a.schedules[i]
        if r >= s.rows or c >= s.cols:
            continue
        v = values[pairing.pairs[i]][r * s.cols + c]
        if v is None:                   # the comparison has no such line
            continue
        if current is not None and current[i][r * s.cols + c] == v:
            continue
        vals[(i, r, c)] = v
    if not vals:
        return None
    scope = Scope(sorted({k[0] for k in vals}), sorted({k[2] for k in vals}))
    return SetValues(scope, vals, label)

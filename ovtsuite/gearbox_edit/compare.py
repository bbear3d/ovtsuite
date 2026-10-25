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
compared, shaded or copied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

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
    #: primary keys with no counterpart of the same shape
    unmatched: List[str] = field(default_factory=list)
    #: why nothing pairs at all (None when something does)
    problem: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.problem is None and bool(self.pairs)

    def comparable(self, i: int) -> bool:
        """Cell-for-cell comparable: paired, same shape and pedal axis."""
        return i in self.pairs and i not in self.axis_differs

    def summary(self) -> str:
        if self.problem:
            return self.problem
        n, tot = len(self.pairs), len(self.a.schedules)
        out = "%d of %d %s paired" % (n, tot, "characteristics" if self.a.kind == "tcc"
                                       else "schedules")
        if self.axis_differs:
            out += ", %d with a different pedal axis (drawn, not compared)" \
                % len(self.axis_differs)
        if self.unmatched:
            out += "; not in the comparison (or another shape): %s" % _short(self.unmatched)
        return out


def _short(keys: List[str], n: int = 4) -> str:
    return ", ".join(keys[:n]) + (" and %d more" % (len(keys) - n) if len(keys) > n else "")


def pair_sets(a: Optional[ScheduleSet], b: Optional[ScheduleSet]) -> Pairing:
    """Pair ``a``'s schedules with ``b``'s by key, shape and lines."""
    if a is None:
        return Pairing(a, b, problem="Nothing to compare")
    if b is None:
        return Pairing(a, b, problem="The comparison file has no %s to compare"
                       % ("lock-up characteristics" if a.kind == "tcc" else "shift schedules"))
    if b.kind != a.kind:
        return Pairing(a, b, problem="The comparison's set is of another kind")
    if [(ln.index, ln.label) for ln in a.lines] != [(ln.index, ln.label) for ln in b.lines]:
        return Pairing(a, b, problem="The comparison's lines differ (%s against %s)" % (
            ", ".join(ln.label for ln in b.lines), ", ".join(ln.label for ln in a.lines)))
    keys = {s.key: j for j, s in enumerate(b.schedules)}
    out = Pairing(a, b)
    for i, s in enumerate(a.schedules):
        j = keys.get(s.key)
        t = b.schedules[j] if j is not None else None
        if t is None or (t.rows, t.cols) != (s.rows, s.cols):
            out.unmatched.append(s.key)
            continue
        out.pairs[i] = j
        if tuple(round(p, 3) for p in t.pedal) != tuple(round(p, 3) for p in s.pedal):
            out.axis_differs.add(i)
    if not out.pairs:
        out.problem = "No schedule of the comparison has this file's key and shape"
    return out


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
        if current is not None and current[i][r * s.cols + c] == v:
            continue
        vals[(i, r, c)] = v
    if not vals:
        return None
    scope = Scope(sorted({k[0] for k in vals}), sorted({k[2] for k in vals}))
    return SetValues(scope, vals, label)

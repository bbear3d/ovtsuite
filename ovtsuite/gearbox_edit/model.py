# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What an edit works on: schedules, their lines, and a scope.

A *schedule* is one stored map (a shift program, or a lock-up characteristic
table): pedal rows x lines, values in output-shaft rpm. A *line* is one column
meaning -- ``5>4``, or ``WK13 Regulated -> Closed`` -- and carries the gear
ratios that turn its values into engine rpm. The gearbox module builds these;
nothing here knows a gearbox.

Pedal is always handled as a percentage read from each schedule's OWN axis:
schedules on one image need not share an axis (on the 6HP19, SKL 31/32 carry
the 3.0 TDI one), so "rows 4..8" means different pedal on different maps and is
never how a scope is stated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Line:
    """One column meaning shared by every schedule in a set."""
    index: int                      # column in the stored map
    label: str                      # "5>4"
    kind: str = "up"                # "up" | "down" | "tcc"
    ratio_left: float = 1.0         # engine rpm = output rpm x this (gear left)
    ratio_entered: float = 1.0      # gear entered (post-shift engine rpm)
    pair: Optional[int] = None      # the opposing line (5>4 <-> 4>5)
    gear_left: int = 0
    gear_entered: int = 0


@dataclass
class Schedule:
    """One stored map, as the editor sees it."""
    key: str                        # "SKL 11.0"
    entity: object                  # model.Entity (values read/written through it)
    number: int = 0                 # SKL id / map number
    bank: int = 0                   # coding bank (0 = the first coding)
    group: str = ""                 # "D (economic) — adaptive grid"
    role: str = ""                  # "Sx1, gradient 2" / "program 28"
    states: Tuple[int, ...] = ()    # selection states (or programs) that use it
    pedal: Tuple[float, ...] = ()   # pedal % per row, from its own axis
    #: stored value(s) meaning "never" -- not a speed (8HP 10000; the 6HP
    #: lock-up maps' 10000 "disabled" and 8160 "never lock")
    off: object = None
    #: A schedule may be a SLICE of a stored map: a lock-up characteristic is
    #: three or four columns of a ten-characteristic map. ``col_offset`` is the
    #: first stored column and ``ncols`` how many belong to this schedule.
    col_offset: int = 0
    ncols: Optional[int] = None
    #: Engine rpm = output rpm x this, when it is the schedule's own (a lock-up
    #: characteristic serves one gear); None -> the line's ratio.
    ratio: Optional[float] = None
    #: Reference curves to compare with or follow, by name:
    #: ``{"SKL 12": (pedal %, values)}`` -- e.g. the upshift out of this
    #: characteristic's gear in each shift program.
    refs: Dict[str, Tuple[Tuple[float, ...], Tuple[float, ...]]] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return int(self.entity.rows)

    @property
    def cols(self) -> int:
        return int(self.ncols if self.ncols is not None else self.entity.cols)

    def is_off(self, v) -> bool:
        if self.off is None:
            return False
        if isinstance(self.off, (tuple, list, set, frozenset)):
            return v in self.off
        return v == self.off

    def values(self) -> List[float]:
        """Row-major cell values in stored units (output rpm)."""
        e, o = self.entity, self.col_offset
        return [float(e.cell(r, o + c)) for r in range(e.rows) for c in range(self.cols)]


@dataclass
class ScheduleSet:
    """Everything an editor shows for one image: schedules, lines, presets."""
    schedules: List[Schedule]
    lines: List[Line]
    presets: Dict[str, List[int]] = field(default_factory=dict)
    kind: str = "shift"             # "shift" | "tcc"
    notes: List[str] = field(default_factory=list)

    def read(self, data: bytes) -> Dict[int, Tuple[Tuple[float, ...], List[float]]]:
        """Every schedule as stored in ``data`` -- another version of the same
        image (a checkpoint): ``{index: (pedal axis, row-major values)}``.

        The entities are read through a copy pointed at ``data``, so the
        pedal axis comes from that version too (it moves when the pedal map
        is reshaped). A schedule that can't be read there is left out.
        """
        import copy
        out = {}
        swapped = {}
        for i, s in enumerate(self.schedules):
            e = s.entity
            bf = getattr(e, "binfile", None)
            if bf is None:
                continue
            key = id(bf)
            if key not in swapped:
                nb = copy.copy(bf)
                nb.data = bytearray(data)
                swapped[key] = nb
            e2 = copy.copy(e)
            e2.binfile = swapped[key]
            try:
                pedal = tuple(float(v) for v in e2.y_values())
                o = s.col_offset
                vals = [float(e2.cell(r, o + c)) for r in range(e2.rows) for c in range(s.cols)]
            except Exception:
                continue
            out[i] = (pedal, vals)
        return out

    def index(self, key: str) -> int:
        for i, s in enumerate(self.schedules):
            if s.key == key:
                return i
        raise KeyError(key)

    def line(self, label_or_index) -> Line:
        if isinstance(label_or_index, int):
            for ln in self.lines:
                if ln.index == label_or_index:
                    return ln
            raise KeyError(label_or_index)
        for ln in self.lines:
            if ln.label == label_or_index:
                return ln
        raise KeyError(label_or_index)

    def shared(self, i: int) -> Tuple[int, ...]:
        """States beyond the first that route to schedule ``i``."""
        return tuple(self.schedules[i].states[1:])

    def select(self, text: str) -> List[int]:
        """Schedules matching a preset name or a range (:func:`parse_range`)."""
        if text in self.presets:
            return list(self.presets[text])
        return parse_range(text, self)


_ITEM = re.compile(r"^\s*(\d+)(?:\.(\d))?\s*(?:-\s*(\d+)(?:\.(\d))?)?\s*$")


def parse_range(text: str, sset: ScheduleSet) -> List[int]:
    """``"10-24, 31, 40.0"`` -> schedule indices, in set order.

    A bare number means every coding bank of that schedule number; ``n.b``
    means that bank only. Ranges are inclusive. Unknown numbers are ignored
    rather than guessed at.
    """
    wanted = set()
    for part in (text or "").split(","):
        if not part.strip():
            continue
        m = _ITEM.match(part)
        if not m:
            raise ValueError("not a schedule range: %r" % part.strip())
        a, ab, b, bb = m.groups()
        lo, hi = int(a), int(b) if b is not None else int(a)
        if lo > hi:
            lo, hi = hi, lo
        bank = int(ab) if (ab is not None and b is None) else None
        for n in range(lo, hi + 1):
            wanted.add((n, bank))
    out = []
    for i, s in enumerate(sset.schedules):
        if (s.number, None) in wanted or (s.number, s.bank) in wanted:
            out.append(i)
    return out


@dataclass
class Scope:
    """Which cells an operation acts on, and how strongly.

    ``weight(p)`` is 1 inside ``[pedal_min, pedal_max]`` and 0 outside. With a
    taper, the full amount applies up to ``taper_start`` and fades linearly to
    nothing at ``taper_end`` -- the shape the 6HP19 downshift passes used
    (full to 80 %, none at 100 %, kickdown rows untouched).
    """
    schedules: Sequence[int]
    lines: Sequence[int]            # Line.index values
    pedal_min: float = 0.0
    pedal_max: float = 100.4
    taper_start: Optional[float] = None
    taper_end: Optional[float] = None
    direction: str = "any"          # "any" | "raise" | "lower"

    def weight(self, p: float) -> float:
        eps = 1e-9
        if p < self.pedal_min - eps or p > self.pedal_max + eps:
            return 0.0
        if self.taper_start is not None and self.taper_end is not None \
                and p > self.taper_start:
            span = self.taper_end - self.taper_start
            if span <= 0:
                return 0.0
            return max(0.0, min(1.0, (self.taper_end - p) / span))
        return 1.0


def interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    """Piecewise-linear, clamped at both ends (the TCU's own lookup rule)."""
    if not xs:
        return 0.0
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    for i in range(len(xs) - 1):
        a, b = xs[i], xs[i + 1]
        if a <= x <= b:
            if b == a:
                return float(ys[i + 1])
            t = (x - a) / (b - a)
            return float(ys[i]) + t * (float(ys[i + 1]) - float(ys[i]))
    return float(ys[-1])

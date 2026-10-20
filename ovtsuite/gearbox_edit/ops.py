# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The operations an edit is made of.

Every operation works on a *grid* -- ``{schedule index: row-major values}`` in
stored units (output rpm) -- inside a :class:`~.model.Scope`. Each reads the
values as they stood when it started, so its result does not depend on the
order it visits cells in, and then:

* scales its change by the scope's pedal weight (range and taper);
* applies the scope's direction (raise-only keeps ``max(before, new)``);
* rounds to the stored precision (half-to-even, as the passes it replaces did);
* skips a schedule's "off" value -- a stored "never" is not a speed.

Relative operations (offset, scale, slide, band) are the default way to change
many schedules because they keep each schedule's own character; an absolute
curve is there for when identical values are actually wanted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .model import Scope, ScheduleSet, interp

Grid = Dict[int, List[float]]


def _is_float(sched) -> bool:
    return bool(getattr(sched.entity, "is_float", False))


def _finish(sched, before: float, new: float, scope: Scope) -> float:
    if scope.direction == "raise":
        new = max(before, new)
    elif scope.direction == "lower":
        new = min(before, new)
    if not _is_float(sched):
        new = float(round(new))
    return new


def _cells(sset: ScheduleSet, scope: Scope):
    """``(i, sched, r, c, weight, pedal)`` for every cell the scope reaches."""
    for i in scope.schedules:
        s = sset.schedules[i]
        cols = s.cols
        for c in scope.lines:
            if c >= cols:
                continue
            for r, p in enumerate(s.pedal):
                w = scope.weight(p)
                if w > 0:
                    yield i, s, r, c, w, p


def ratio_of(sset: ScheduleSet, s, line: int) -> float:
    """Engine rpm per output rpm for ``line`` of schedule ``s``."""
    return (s.ratio if s is not None and s.ratio else sset.line(line).ratio_left) or 1.0


def _to_out(sset: ScheduleSet, line: int, amount: float, unit: str, s=None) -> float:
    """An amount in ``unit`` as output rpm for ``line`` (of schedule ``s``)."""
    if unit == "engine":
        return amount / ratio_of(sset, s, line)
    if unit in ("output", "raw"):
        return amount
    raise ValueError("unit must be 'engine' or 'output', not %r" % unit)


class Op:
    scope: Scope
    name = "op"

    def describe(self) -> str:
        return self.name

    def apply(self, grid: Grid, sset: ScheduleSet) -> Dict[Tuple[int, int, int], str]:
        before = {i: list(v) for i, v in grid.items()}
        changed = {}
        for i, s, r, c, w, p in _cells(sset, self.scope):
            k = r * s.cols + c
            old = before[i][k]
            if s.is_off(old):
                continue
            target = self.target(before, sset, i, s, r, c, p)
            if target is None:
                continue
            new = _finish(s, old, old + w * (target - old), self.scope)
            if new != old:
                grid[i][k] = new
                changed[(i, r, c)] = self.describe()
        return changed

    def target(self, before, sset, i, s, r, c, p) -> Optional[float]:
        raise NotImplementedError


@dataclass
class Offset(Op):
    """Add ``amount`` (engine or output rpm) to every reached cell."""
    scope: Scope
    amount: float
    unit: str = "engine"
    name = "offset"

    def describe(self):
        return "offset %+g %s rpm" % (self.amount, self.unit)

    def target(self, before, sset, i, s, r, c, p):
        return before[i][r * s.cols + c] + _to_out(sset, c, self.amount, self.unit, s)


@dataclass
class Scale(Op):
    """Multiply every reached cell by ``factor`` (0.92 = 8 % earlier)."""
    scope: Scope
    factor: float
    name = "scale"

    def describe(self):
        return "scale x%g" % self.factor

    def target(self, before, sset, i, s, r, c, p):
        return before[i][r * s.cols + c] * self.factor


@dataclass
class SlidePedal(Op):
    """Read each line ``points`` pedal-% further along its own pedal axis.

    Positive moves the line's shape to LOWER pedal (a downshift then needs
    less pedal): the new value at pedal p is the old curve at p + points.
    """
    scope: Scope
    points: float
    name = "slide"

    def describe(self):
        return "slide %+g pedal points" % self.points

    def apply(self, grid, sset):
        # Like Op.apply, but the taper scales the DISTANCE moved along the
        # axis, not the value change: at 90 % pedal with half weight the cell
        # takes the curve's value 0.5 x points further on.
        before = {i: list(v) for i, v in grid.items()}
        changed = {}
        for i, s, r, c, w, p in _cells(sset, self.scope):
            k = r * s.cols + c
            old = before[i][k]
            if s.is_off(old):
                continue
            col = [before[i][rr * s.cols + c] for rr in range(s.rows)]
            new = _finish(s, old, interp(s.pedal, col, p + w * self.points), self.scope)
            if new != old:
                grid[i][k] = new
                changed[(i, r, c)] = self.describe()
        return changed


@dataclass
class BandRemap(Op):
    """Map the selection's spread onto ``[lo, hi]`` (engine or output rpm).

    Per line and pedal row, the lowest and highest values across the selected
    schedules become ``lo`` and ``hi``; everything between moves linearly, so
    the ORDER of the schedules and their relative spacing survive.
    """
    scope: Scope
    lo: float
    hi: float
    unit: str = "engine"
    blend: float = 1.0              # how far toward the band (0.5 = half way)
    #: How the spread is measured: per pedal value (default -- schedules with
    #: their own axis are compared at the same pedal) or per row number, which
    #: is what the 6HP19 upshift retune script did.
    group_by: str = "pedal"
    name = "band"

    def describe(self):
        return "band %g..%g %s rpm%s" % (
            self.lo, self.hi, self.unit,
            "" if self.blend == 1.0 else " (%d%% of the way)" % round(self.blend * 100))

    def apply(self, grid, sset):
        spans: Dict[Tuple[int, float], List[float]] = {}
        for i, s, r, c, w, p in _cells(sset, self.scope):
            v = grid[i][r * s.cols + c]
            if s.is_off(v):
                continue
            spans.setdefault(self._key(c, r, p), []).append(v)
        self._spans = {k: (min(v), max(v)) for k, v in spans.items()}
        return Op.apply(self, grid, sset)

    def _key(self, c, r, p):
        return (c, r) if self.group_by == "row" else (c, round(p, 3))

    def target(self, before, sset, i, s, r, c, p):
        lo_v, hi_v = self._spans.get(self._key(c, r, p), (None, None))
        if lo_v is None:
            return None
        lo = _to_out(sset, c, self.lo, self.unit, s)
        hi = _to_out(sset, c, self.hi, self.unit, s)
        v = before[i][r * s.cols + c]
        if hi_v == lo_v:
            band = lo
        else:
            band = lo + (v - lo_v) * (hi - lo) / (hi_v - lo_v)
        if self.scope.direction == "raise":
            band = max(v, band)
        elif self.scope.direction == "lower":
            band = min(v, band)
        return v + self.blend * (band - v)


@dataclass
class SetCurve(Op):
    """Set the line to an absolute curve ``[(pedal %, value), ...]``.

    ``blend`` 1.0 writes the curve; 0.5 moves each cell half-way to it.
    """
    scope: Scope
    points: Sequence[Tuple[float, float]]
    unit: str = "engine"
    blend: float = 1.0
    name = "set"

    def describe(self):
        return "set curve (%d points)" % len(self.points)

    def target(self, before, sset, i, s, r, c, p):
        pts = sorted(self.points)
        val = interp([a for a, _ in pts], [b for _, b in pts], p)
        val = _to_out(sset, c, val, self.unit, s)
        old = before[i][r * s.cols + c]
        return old + self.blend * (val - old)


@dataclass
class CopyFrom(Op):
    """Move toward another schedule's values, by pedal (not by row).

    ``source`` is a schedule index in the same set, or explicit values via
    ``source_values`` (``{line index: [(pedal, value), ...]}``) for a
    reference file or an earlier checkpoint.
    """
    scope: Scope
    source: Optional[int] = None
    source_values: Optional[Dict[int, Sequence[Tuple[float, float]]]] = None
    fraction: float = 1.0
    name = "copy"

    def describe(self):
        return "copy %d%%" % round(self.fraction * 100)

    def target(self, before, sset, i, s, r, c, p):
        if self.source is not None:
            src = sset.schedules[self.source]
            if c >= src.cols:
                return None
            col = [before[self.source][rr * src.cols + c] for rr in range(src.rows)]
            val = interp(src.pedal, col, p)
        elif self.source_values and c in self.source_values:
            pts = sorted(self.source_values[c])
            val = interp([a for a, _ in pts], [b for _, b in pts], p)
        else:
            return None
        old = before[i][r * s.cols + c]
        return old + self.fraction * (val - old)


@dataclass
class Smooth(Op):
    """Pull each interior row toward the mean of its neighbours by ``amount``."""
    scope: Scope
    amount: float = 0.5
    name = "smooth"

    def describe(self):
        return "smooth %d%%" % round(self.amount * 100)

    def target(self, before, sset, i, s, r, c, p):
        if r == 0 or r >= s.rows - 1:
            return None
        k = s.cols
        a, b, m = before[i][(r - 1) * k + c], before[i][(r + 1) * k + c], before[i][r * k + c]
        return m + self.amount * ((a + b) / 2.0 - m)


@dataclass
class TrackReference(Op):
    """Set a line to ``factor`` x a reference curve ``+ offset``.

    The reference is one of the schedule's ``refs`` by name -- for a lock-up
    characteristic, the upshift out of its gear in a chosen shift program
    ("lock-complete at 85 % of SKL 12's 3>4"). ``within`` limits it to the
    cells whose current value sits less than that far below the reference
    (output rpm): the rows where the two are too close, and no others.
    """
    scope: Scope
    ref: str
    factor: float = 1.0
    offset: float = 0.0
    unit: str = "output"
    within: Optional[float] = None
    name = "track"

    def describe(self):
        parts = ["%g %% of %s" % (self.factor * 100, self.ref)]
        if self.offset:
            parts.append("%+g %s rpm" % (self.offset, self.unit))
        if self.within is not None:
            parts.append("where closer than %g" % self.within)
        return "follow " + " ".join(parts)

    def target(self, before, sset, i, s, r, c, p):
        got = s.refs.get(self.ref)
        if not got:
            return None
        ref = interp(got[0], got[1], p)
        old = before[i][r * s.cols + c]
        if self.within is not None and ref - old >= self.within:
            return None
        return self.factor * ref + _to_out(sset, c, self.offset, self.unit, s)


def _pairs(sset: ScheduleSet, lines: Sequence[int]) -> List[Tuple[int, int]]:
    """``(lower, upper)`` line pairs reached by ``lines``: a downshift (or RO)
    is the lower member, its upshift (or OR) the upper."""
    out = []
    for c in lines:
        ln = sset.line(c)
        if ln.pair is None:
            continue
        lo, hi = (c, ln.pair) if ln.kind == "down" else (ln.pair, c)
        if (lo, hi) not in out:
            out.append((lo, hi))
    return out


@dataclass
class SetGap(Op):
    """Set the gap between a line and its pair -- an upshift and the
    downshift that undoes it, or lock-up's RO and OR (the slip-control band).

    ``anchor`` says which member stays: ``"low"`` keeps the lower line (the
    downshift, RO) and moves the upper one to ``low + gap``; ``"high"`` keeps
    the upper and moves the lower to ``high - gap``; ``"mid"`` moves both
    about their middle. ``gap`` is in ``unit`` (engine rpm converts at each
    line's own ratio, so the two sides are compared in output rpm). The
    scope's pedal range, taper and direction apply to the line that moves.
    """
    scope: Scope
    gap: float
    unit: str = "output"
    anchor: str = "low"
    name = "gap"

    def describe(self):
        return "gap %g %s rpm (%s)" % (self.gap, self.unit, {
            "low": "lower line kept", "high": "upper line kept",
            "mid": "about the middle"}.get(self.anchor, self.anchor))

    def apply(self, grid, sset):
        before = {i: list(v) for i, v in grid.items()}
        changed = {}
        for i in self.scope.schedules:
            s = sset.schedules[i]
            for lo, hi in _pairs(sset, self.scope.lines):
                if max(lo, hi) >= s.cols:
                    continue
                gap = _to_out(sset, hi, self.gap, self.unit, s)
                for r, p in enumerate(s.pedal):
                    w = self.scope.weight(p)
                    if w <= 0:
                        continue
                    k_lo, k_hi = r * s.cols + lo, r * s.cols + hi
                    a, b = before[i][k_lo], before[i][k_hi]
                    if s.is_off(a) or s.is_off(b):
                        continue
                    if self.anchor == "high":
                        want = {k_lo: b - gap}
                    elif self.anchor == "mid":
                        m = (a + b) / 2.0
                        want = {k_lo: m - gap / 2.0, k_hi: m + gap / 2.0}
                    else:
                        want = {k_hi: a + gap}
                    for k, t in want.items():
                        old = before[i][k]
                        new = _finish(s, old, old + w * (t - old), self.scope)
                        if new != old:
                            grid[i][k] = new
                            changed[(i, r, k % s.cols)] = self.describe()
        return changed


def _storable(entity, value: float) -> bool:
    """Whether ``entity`` can hold ``value`` exactly enough to read it back."""
    if getattr(entity, "is_float", False):
        return True
    eq = getattr(entity, "eq", None)
    bits = int(getattr(entity, "bits", 16) or 16)
    try:
        raw = int(round(eq.encode(value))) if eq is not None else int(round(value))
        back = eq.decode(raw) if eq is not None else raw
    except Exception:
        return False
    hi = (1 << (bits - 1)) - 1 if getattr(entity, "signed", False) else (1 << bits) - 1
    return 0 <= raw <= hi and abs(back - value) < 0.5


@dataclass
class SetOff(Op):
    """Write the schedule's own "off" value -- for lock-up, ZF's "disabled"
    (10000 output rpm; 8160 where only 8 bits are stored) -- so the threshold
    is never reached: a characteristic whose OR is off never leaves "open",
    i.e. never locks. Taper and direction do not apply; any cell inside the
    pedal range is written."""
    scope: Scope
    name = "off"

    def describe(self):
        return "set to off (never)"

    def apply(self, grid, sset):
        changed = {}
        for i, s, r, c, w, p in _cells(sset, self.scope):
            offs = s.off if isinstance(s.off, (tuple, list)) else (s.off,)
            offs = [v for v in offs if v is not None]
            if not offs:
                continue
            k = r * s.cols + c
            if s.is_off(grid[i][k]):
                continue
            val = next((v for v in sorted(offs, reverse=True)
                        if _storable(s.entity, v)), None)
            if val is None:
                continue
            grid[i][k] = float(val)
            changed[(i, r, c)] = self.describe()
        return changed

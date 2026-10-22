# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Part-load shift strategy, and moving the gearbox's pedal axis to where the
engine asks for full torque (project doc ``claude/part_throttle_shift_strategy.md``).

**The strategy.** An upshift is set by the engine speed it leaves the next
gear at. That *post-shift speed* rises with pedal: from a floor at light
pedal (no lower: the engine must pull the next gear) to the full-load
optimum's own post-shift speed at full load, where it joins the full-load
step (:mod:`.fullload`)::

    post(p) = floor + (full-load post - floor) * s(p)
    s(p)    = 0 below p_start, ((p - p_start) / (100 - p_start)) ** curve above,
              1 from the pedal where the ECU already asks for full torque

The threshold written is ``post(p) / k`` (``k`` = ratio entered / ratio
left); the downshift that undoes it sits ``gap`` below the post-shift speed,
never under its own floor. Economy keeps the floor low and the rise late;
sport the opposite. The presets are the ones replayed on Bear's logs
(2026-10-22). A strategy is assigned by mode: D economy and S sport by
default.

**The pedal axis.** Where the ECU asks for full torque well before 100 %
pedal, the rows above that pedal are full load in engine terms while the
gearbox still treats them as part load. :class:`RemapPedalAxis` moves the
breakpoints instead of the values: rows between ``keep_below`` and the top
part-load row are squeezed onto ``keep_below .. full_torque`` so the top row
lands where full torque begins; 100 % and kickdown (and everything below
``keep_below``) stay put, as the 2026-10-13 remap kept 0, 254 and 255.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .model import Scope, ScheduleSet
from .ops import Op, _finish

FULL_ROW = 99.9           # rows at or above this are the full-load step's


@dataclass(frozen=True)
class Strategy:
    name: str
    floor: float              # post-shift engine rpm at light pedal
    p_start: float            # pedal % where the rise begins
    curve: float              # exponent of the rise (> 1: late, < 1: early)
    gap: float = 0.90         # downshift at this fraction of the post-shift speed
    down_floor: Optional[float] = None   # rpm in the gear being left; default 0.85 x floor

    @property
    def dn_floor(self) -> float:
        return self.down_floor if self.down_floor is not None else self.floor * 0.85

    def rise(self, p: float, full_torque: Optional[float] = None) -> float:
        if full_torque is not None and p >= full_torque - 1e-6:
            return 1.0
        if p <= self.p_start:
            return 0.0
        return min(1.0, ((min(p, 100.0) - self.p_start) / (100.0 - self.p_start)) ** self.curve)


ECONOMY = Strategy("Economy", 1250.0, 25.0, 1.6)
BALANCED = Strategy("Balanced", 1500.0, 15.0, 1.3)
SPORT = Strategy("Sport", 1850.0, 5.0, 1.0)
PRESETS = {s.name: s for s in (ECONOMY, BALANCED, SPORT)}


def mode_of(group: str) -> str:
    """"D", "S", "M" or "" from a schedule's group ("D (economic) — ...")."""
    g = (group or "").strip()
    for m in ("D", "S", "M"):
        if g == m or g.startswith(m + " ") or g.startswith(m + "("):
            return m
    return ""


def assign_by_mode(by_mode: Dict[str, Optional[Strategy]],
                   other: Optional[Strategy] = None) -> Callable[[object], Optional[Strategy]]:
    """``schedule -> strategy``: by the schedule's mode (D/S/M), ``other``
    for the rest (None leaves them alone)."""
    def pick(s):
        m = mode_of(getattr(s, "group", ""))
        if m in by_mode:
            return by_mode[m]
        return other
    return pick


@dataclass
class PartLoadTarget(Op):
    """Write a part-load :class:`Strategy` into each reached schedule's
    upshifts (and the downshifts that pair with them).

    ``anchors`` gives each upshift's full-load point (output rpm), e.g. the
    full-load step's targets; where a line has none, the schedule's own
    full-load row is the anchor. ``strategy_of(schedule)`` picks the
    strategy (None leaves the schedule alone). ``fraction`` moves each cell
    that far toward the strategy (1.0 = all the way). ``full_torque`` (pedal
    %) makes every row from there use the full-load point. Rows at or above
    :data:`FULL_ROW` are left to the full-load step unless
    ``full_load_rows`` is set.
    """
    scope: Scope
    strategy_of: Callable[[object], Optional[Strategy]]
    anchors: Dict[int, float] = None
    fraction: float = 1.0
    full_torque: Optional[float] = None
    downshifts: bool = True
    full_load_rows: bool = False
    name = "part-load"

    def describe(self):
        bits = ["part-load strategy"]
        if self.fraction < 1.0:
            bits.append("%d %% of the way" % round(self.fraction * 100))
        if self.full_torque is not None:
            bits.append("full load from %.1f %%" % self.full_torque)
        return ", ".join(bits)

    def _anchor(self, s, col, c) -> Optional[float]:
        """The full-load point the strategy reaches: the schedule's own
        full-load row as it is now. A planned point (``anchors``, the
        full-load step's) never takes it above that row -- the step may not
        have been applied, or skipped this line -- else the line would fall
        going into full load."""
        full = [r for r, p in enumerate(s.pedal) if p >= FULL_ROW and not s.is_off(col[r])]
        own = col[full[0]] if full else None
        if self.anchors and c in self.anchors:
            a = self.anchors[c]
            if own is None or self.full_load_rows:   # (it writes those rows too)
                return a
            return min(a, own)
        return own

    def targets(self, sset: ScheduleSet, s, col_up, c) -> Optional[Tuple[List[float], List[float]]]:
        """``(upshift, downshift)`` targets per row, output rpm, for line ``c``
        of schedule ``s`` (None when the schedule gets no strategy)."""
        st = self.strategy_of(s)
        ln = sset.line(c)
        if st is None or ln.kind != "up" or not ln.ratio_left or not ln.ratio_entered:
            return None
        a = self._anchor(s, col_up, c)
        if a is None:
            return None
        k = ln.ratio_entered / ln.ratio_left
        top_post = a * ln.ratio_left * k                  # rpm in the next gear
        floor = min(st.floor, top_post)
        dn_floor = min(st.dn_floor, top_post * st.gap)
        up, dn = [], []
        for p in s.pedal:
            f = st.rise(p, self.full_torque)
            post = floor + (top_post - floor) * f
            up.append(min(post / k, a * ln.ratio_left) / ln.ratio_left)
            d = min(post * st.gap, dn_floor + (top_post * st.gap - dn_floor) * f)
            dn.append(d / ln.ratio_entered)
        return up, dn

    def apply(self, grid, sset):
        before = {i: list(v) for i, v in grid.items()}
        changed = {}
        for i in self.scope.schedules:
            s = sset.schedules[i]
            K = s.cols
            for c in self.scope.lines:
                if c >= K or sset.line(c).kind != "up":
                    continue
                col = [before[i][r * K + c] for r in range(s.rows)]
                got = self.targets(sset, s, col, c)
                if got is None:
                    continue
                up, dn = got
                pair = sset.line(c).pair
                for r, p in enumerate(s.pedal):
                    w = self.scope.weight(p)
                    if w <= 0 or (p >= FULL_ROW and not self.full_load_rows):
                        continue
                    for cc, tgt in ((c, up[r]), (pair, dn[r])):
                        if cc is None or cc >= K or (cc == pair and not self.downshifts):
                            continue
                        k = r * K + cc
                        old = before[i][k]
                        if s.is_off(old):
                            continue
                        new = _finish(s, old, old + w * self.fraction * (tgt - old), self.scope)
                        if new != old:
                            grid[i][k] = new
                            changed[(i, r, cc)] = self.describe()
        return changed


# ------------------------------------------------------------- pedal axis
def remap_pedal(p: float, keep_below: float, top: float, full_torque: float) -> float:
    """Where breakpoint ``p`` moves: identity below ``keep_below`` and from
    :data:`FULL_ROW` up; ``keep_below .. top`` onto ``keep_below ..
    full_torque``; ``top .. 100`` onto ``full_torque .. 100``."""
    if p <= keep_below or p >= FULL_ROW:
        return p
    if p <= top:
        return keep_below + (p - keep_below) * (full_torque - keep_below) / (top - keep_below)
    return full_torque + (p - top) * (100.0 - full_torque) / (100.0 - top)


@dataclass
class RemapPedalAxis(Op):
    """Move the pedal axis of each reached schedule (and of ``extra``
    entities -- lock-up maps -- with the same mapping) so the gearbox's top
    part-load row lands where the ECU asks for full torque. No cell value
    changes; :meth:`axes` gives the new breakpoints, which
    :class:`~.edit.Edit` writes in the same undo step."""
    scope: Scope
    full_torque: float
    keep_below: float = 50.0
    top: Optional[float] = None
    extra: Sequence[object] = ()
    name = "pedal axis"

    def describe(self):
        return "pedal axis: top part-load row to %.1f %% (rows below %.0f %% kept)" % (
            self.full_torque, self.keep_below)

    def apply(self, grid, sset):
        return {}

    def _top(self, pedal) -> Optional[float]:
        if self.top is not None:
            return self.top
        below = [p for p in pedal if p < FULL_ROW]
        return max(below) if below else None

    def new_axis(self, pedal: Sequence[float]) -> Optional[List[float]]:
        return self._remap(pedal, self._top(pedal))

    def _remap(self, pedal: Sequence[float], top: Optional[float]) -> Optional[List[float]]:
        if top is None or top <= self.keep_below or self.full_torque <= self.keep_below \
                or self.full_torque >= FULL_ROW:
            return None
        return [remap_pedal(p, self.keep_below, top, self.full_torque) for p in pedal]

    def axes(self, sset: ScheduleSet) -> Dict[int, Tuple[object, List[float], List[float]]]:
        """``{id(entity): (entity, old pedal, new pedal)}`` for every axis this
        moves (each stored axis once, however many schedules share it)."""
        out = {}
        own = [sset.schedules[i].entity for i in self.scope.schedules]
        # the extra (lock-up) axes move with the shift schedules' mapping --
        # the same top -- so lock-up keeps meeting the shifts at the same
        # pedal, whatever their own top part-load row
        shift_top = None
        if self.scope.schedules and self.top is None:
            shift_top = self._top(sset.schedules[self.scope.schedules[0]].pedal)
        for e in own + list(self.extra):
            if id(e) in out or getattr(e, "y", None) is None:
                continue
            try:
                old = [float(v) for v in e.y_values()]
            except Exception:
                continue
            if e in own or shift_top is None:
                new = self.new_axis(old)
            else:
                new = self._remap(old, shift_top)
            if new is None or new == old:
                continue
            out[id(e)] = (e, old, new)
        return out


def encode_axis(entity, values: Sequence[float]) -> Optional[List[float]]:
    """``values`` as the axis can store them (decoded back), or None when
    two breakpoints would collapse onto one stored value or the order would
    break."""
    ax = entity.y
    eq = getattr(ax, "eq", None)
    out = []
    for v in values:
        if eq is not None and not getattr(ax, "is_float", False):
            raw = int(round(eq.encode(v)))
            out.append(eq.decode(raw))
        else:
            out.append(float(v))
    if any(b <= a for a, b in zip(out, out[1:])):
        return None
    return out


# ------------------------------------------------------------------ check
@dataclass
class ConverterCheck:
    """An upshift should land where the next gear's converter is locked --
    an economy shift into an open converter loses what it was for.

    For each upshift cell the edit changed, the output speed at the shift
    (it does not change across the shift) is held against the lock-up
    characteristics that serve the next gear in this schedule's program
    (``tcc``, the lock-up editor's set; without program references, every
    characteristic of that gear): below the lowest OR (open -> regulated) at
    that pedal the converter is still open. Judged only where the edit made
    it so, or lowered a shift that already was. Report only.
    """
    tcc: Optional[ScheduleSet] = None
    code = "converter"
    autofix = False

    def _threshold(self, s, gear: int, pedal: float) -> Optional[float]:
        from .model import interp
        t = self.tcc
        if t is None:
            return None
        lab = {ln.label: ln.index for ln in t.lines}
        col = lab.get("OR")
        if col is None:
            return None
        by_program = any(c.states for c in t.schedules)
        best = None
        for c in t.schedules:
            if c.group != "Gear %d" % gear:
                continue
            if by_program and not (c.bank == s.bank and s.number in c.states):
                continue
            vals = c.values()
            column = [vals[r * c.cols + col] for r in range(c.rows)]
            if any(c.is_off(v) for v in column):
                continue
            v = interp(list(c.pedal), column, pedal)
            best = v if best is None else min(best, v)
        return best

    def run(self, sset, origin, grid, changed):
        from .checks import Finding
        out = []
        by: Dict[tuple, List[int]] = {}
        for (i, r, c) in changed:
            ln = sset.line(c)
            if ln.kind != "up" or not ln.gear_entered:
                continue
            s = sset.schedules[i]
            k = r * s.cols + c
            new, old = grid[i][k], origin[i][k]
            if s.is_off(new):
                continue
            thr = self._threshold(s, ln.gear_entered, s.pedal[r])
            if thr is None or new >= thr:
                continue
            if old >= thr or new < old:
                by.setdefault((i, c), []).append(r)
        for (i, c), rows in sorted(by.items()):
            s = sset.schedules[i]
            ln = sset.line(c)
            out.append(Finding(
                "warning", self.code,
                "%s %s lands in %s with the converter open at %s (below the lock-up "
                "characteristic's OR)" % (
                    s.key, ln.label, _ordinal(ln.gear_entered),
                    ", ".join("%.1f %%" % s.pedal[r] for r in sorted(rows))),
                [(i, r, c) for r in rows]))
        return out


def _ordinal(n: int) -> str:
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th"))


__all__ = ["ConverterCheck", "Strategy", "ECONOMY", "BALANCED", "SPORT", "PRESETS", "mode_of",
           "assign_by_mode", "PartLoadTarget", "RemapPedalAxis", "remap_pedal",
           "encode_axis", "FULL_ROW"]

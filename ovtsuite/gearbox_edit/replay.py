# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Logged drives replayed against a set of shift lines.

Each logged sample keeps the speed, the pedal and the brake power the engine
gave. The lines being judged pick the gear -- one shift per sample at most,
up when the speed in the current gear reaches the upshift line at that pedal,
down when it falls to the downshift line -- and the engine then runs at that
gear's speed. It burns what the ECU's own fuel model says for that power at
that speed; where that gear can not give the power (the full-load envelope),
the shortfall is counted. The result is what the lines would have done with
that driving: fuel per 100 km, time and energy short of what was asked, how
busy the box is, and the mean engine speed.

Open loop: the driver's answer to a different gear is not modelled, so two
sets of lines are compared with each other, not with the log's own figure.
No Qt, no gearbox or ECU knowledge: the caller supplies the samples, the
ratios, the lines and the engine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from .model import ScheduleSet, interp


@dataclass
class Sample:
    dt: float                 # seconds this sample stands for
    v: float                  # km/h
    pedal: float              # %
    gear: int                 # gear the log shows
    out: float                # output-shaft rpm
    power: float              # brake kW the engine gave (<= 0: overrun)
    program: Optional[int] = None
    dsp: Optional[int] = None


@dataclass
class Metrics:
    l100: float               # l / 100 km
    short_s: float            # seconds the gear could not give the power
    short_kj: float           # energy short, kJ
    shifts_h: float
    mean_rpm: float
    agree: float              # share of samples in the logged gear
    km: float
    hours: float

    @property
    def short_pct(self) -> float:
        return 100.0 * self.short_s / (self.hours * 3600.0) if self.hours else 0.0


#: ``(gear, pedal, sample) -> turbine rpm`` of the upshift out of ``gear``
#: (None: no upshift), and of the downshift INTO ``gear`` from ``gear + 1``
#: (rpm in ``gear + 1``)
LineFn = Callable[[int, float, Sample], Optional[float]]


def replay(samples: Sequence[Sample], ratios: Dict[int, float], up: LineFn, down: LineFn,
           fuel_lh: Callable[[float, float], float], tmax: Callable[[float], float],
           idle: float = 760.0, points: Optional[list] = None) -> Metrics:
    """Replay ``samples`` with the lines ``up`` / ``down``; ``fuel_lh(brake
    torque, rpm)`` is l/h, ``tmax(rpm)`` the most brake torque there. With
    ``points`` (a list), each pulling sample's ``(rpm, brake torque, seconds)``
    is appended -- where the engine ran (the efficiency map draws them)."""
    if not ratios:
        raise ValueError("the gearbox's ratios are unknown, so the logs cannot "
                         "be replayed")
    top = max(ratios)
    gs = None
    fuel = short = short_e = km = rpm_t = 0.0
    shifts = agree = n = 0
    T = 0.0
    for s in samples:
        if s.gear < 1 or s.gear > top or s.out < 50:
            gs = None
            continue
        if gs is None:
            gs = s.gear
        u = up(gs, s.pedal, s) if gs < top else None
        if u is not None and s.out * ratios[gs] >= u:
            gs += 1
            shifts += 1
        elif gs > 1:
            d = down(gs - 1, s.pedal, s)
            if d is not None and s.out * ratios[gs] <= d:
                gs -= 1
                shifts += 1
        ns = max(s.out * ratios[gs], idle)
        km += s.v * s.dt / 3600.0
        T += s.dt
        rpm_t += ns * s.dt
        n += 1
        agree += gs == s.gear
        if s.power > 0:
            tb = s.power * 9549.2966 / ns
            tm = tmax(ns)
            if tb > tm + 1.0:
                short += s.dt
                short_e += (tb - tm) * ns / 9549.2966 * s.dt
                tb = tm
            fuel += fuel_lh(tb, ns) * s.dt / 3600.0
            if points is not None:
                points.append((ns, tb, s.dt))
    return Metrics(fuel / km * 100.0 if km else 0.0, short, short_e,
                   shifts / (T / 3600.0) if T else 0.0, rpm_t / T if T else 0.0,
                   agree / n if n else 0.0, km, T / 3600.0)


_GRID = [i * 0.25 for i in range(0, 403)]          # 0 .. 100.5 % pedal


def line_source(sset: ScheduleSet, grid: Dict[int, List[float]],
                pedal_of: Callable[[int], Sequence[float]],
                corners_for: Callable[[Sample], Sequence[tuple]]):
    """``(up, down)`` :data:`LineFn` s reading ``grid``: at each sample, the
    weighted corners ``corners_for(sample)`` -> ``[(schedule index, weight)]``
    (a blend for the logged program, the program's own schedule, or the
    ticked schedules), each read on its own pedal axis ``pedal_of(i)`` -- the
    axis as the edit leaves it. Each corner set's line is worked out once on
    a 0.25 % pedal grid."""
    lines = {ln.gear_left: ln for ln in sset.lines if ln.kind == "up"}
    downs = {ln.gear_entered: ln for ln in sset.lines if ln.kind == "down"}
    cache: Dict[tuple, Optional[List[float]]] = {}

    def table(corners, c, ratio):
        key = (tuple(corners), c)
        if key in cache:
            return cache[key]
        cols = []
        for i, w in corners:
            s = sset.schedules[i]
            if c >= s.cols:
                continue
            col = [grid[i][r * s.cols + c] for r in range(s.rows)]
            if any(s.is_off(v) for v in col):
                cache[key] = None
                return None
            cols.append((list(pedal_of(i)), col, w))
        wsum = sum(w for _p, _c, w in cols)
        if not wsum:
            cache[key] = None
            return None
        cache[key] = [sum(w * interp(px, col, p) for px, col, w in cols) / wsum * ratio
                      for p in _GRID]
        return cache[key]

    def read(c, ratio, p, smp):
        t = table(corners_for(smp), c, ratio)
        if t is None:
            return None
        x = max(0.0, min(_GRID[-1], p)) / 0.25
        k = min(int(x), len(t) - 2)
        f = x - k
        return t[k] + (t[k + 1] - t[k]) * f

    def up(g, p, smp):
        ln = lines.get(g)
        return None if ln is None else read(ln.index, ln.ratio_left, p, smp)

    def down(g, p, smp):
        ln = downs.get(g)
        return None if ln is None else read(ln.index, ln.ratio_left, p, smp)
    return up, down


_PROG = re.compile(r"programs? ([\d, ]+)")


def program_corners(sset: ScheduleSet, blends, fallback: Sequence[int], bank: int = 0):
    """``corners_for(sample)``: the blend whose programs include the logged
    program (and dsp), else the schedule of that coding whose role names the
    program ("program 36" -- cruise reads SKL 2 on the 6HP19), else
    ``fallback`` (the ticked schedules) weighted alike."""
    by_prog: Dict[int, List[tuple]] = {}
    for i, s in enumerate(sset.schedules):
        if s.bank != bank:
            continue
        m = _PROG.search(s.role or "")
        if m:
            for x in m.group(1).replace(" ", "").split(","):
                if x.isdigit():
                    by_prog.setdefault(int(x), [(i, 1.0)])
    usable = [b for b in (blends or ()) if b.programs and b.indices(sset)]
    fb = [(i, 1.0) for i in fallback]

    def corners_for(smp):
        for b in usable:
            if smp.program in b.programs and (b.dsp is None or smp.dsp == b.dsp):
                return b.indices(sset)
        got = by_prog.get(smp.program)
        return got if got else fb
    return corners_for


__all__ = ["Sample", "Metrics", "replay", "line_source", "program_corners"]

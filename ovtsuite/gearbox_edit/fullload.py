# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Full-load upshifts from the engine's torque curve (project doc
``claude/fullload_shift_optimiser.md``).

At full load the quickest car is the one whose engine makes the most power at
every road speed. At one road speed gear n turns the engine faster than gear
n+1, and both deliver (nearly) all the engine power they make to the wheels,
so n gives more thrust than n+1 exactly while P(N) > P(N * r(n+1) / r(n)).
The best upshift is the first speed where that stops -- the *crossover*. No
vehicle data is needed for it: at one road speed the thrust ratio IS the power
ratio.

Three things move the map value away from that speed:

* **lead** -- a shift-map value is where the gearbox STARTS the shift; the
  engine keeps climbing until the ratio changes. The value is the crossover
  less that climb, worked out from the vehicle (:func:`lead_physics`) or
  measured from logged full-load shifts (:func:`measured_leads`);
* **converter slip** -- the maps are turbine speed (output x ratio); in a gear
  whose converter is open at full load the engine runs ``(1 + slip)`` faster;
* **ceiling** -- never later than the engine's own cut-off allows.

:class:`FullLoadTarget` writes the result into the full-load rows of chosen
schedules and blends it into the part-load rows -- keeping the factory's
shape (default), easing in, or the full-load rows alone -- and spreads any
step it creates that is steeper than the line's own steepest factory step.

Nothing here knows an ECU or a gearbox: the curve comes in as numbers
(:class:`TorqueCurve`), the gears from the schedule set's lines.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .model import Scope, ScheduleSet, interp
from .ops import Op, _finish

#: shapes of the blend into part load
SHAPE_FACTORY, SHAPE_EASE, SHAPE_ROWS = "factory", "ease", "rows"
SHAPES = ((SHAPE_FACTORY, "Keep the factory shape"),
          (SHAPE_EASE, "Ease in from the blend pedal"),
          (SHAPE_ROWS, "Full-load rows only"))
#: which rows get the optimum
MODE_ALL, MODE_KICKDOWN = "all", "kickdown"
MODES = ((MODE_ALL, "100 % and kickdown to the optimum"),
         (MODE_KICKDOWN, "Kickdown to the optimum, 100 % keeps its offset"))

#: pedal above this is a kickdown row (the 6HP/8HP 100.4 %)
KICKDOWN_ABOVE = 100.05
#: intervals narrower than this (pedal %) are left out of the step rule:
#: the 100.0 -> 100.4 % kickdown step is meant to be a step
MIN_SPAN = 1.0


# ------------------------------------------------------------------ curve
@dataclass
class TorqueCurve:
    """Full-load crank torque by engine rpm (Nm), e.g. the calculated dyno
    curve of the project's ECU. Past the last point there is no torque."""
    rpm: Tuple[float, ...]
    torque: Tuple[float, ...]
    label: str = ""

    def __post_init__(self):
        pts = sorted(zip(self.rpm, self.torque))
        self.rpm = tuple(float(a) for a, _ in pts)
        self.torque = tuple(max(0.0, float(b)) for _, b in pts)

    def torque_at(self, n: float) -> float:
        if not self.rpm or n > self.rpm[-1]:
            return 0.0
        return interp(self.rpm, self.torque, n)

    def power_at(self, n: float) -> float:
        """kW at ``n`` rpm."""
        return self.torque_at(n) * n / 9549.2966

    @property
    def peak_torque_rpm(self) -> float:
        return self.rpm[max(range(len(self.rpm)), key=lambda i: self.torque[i])] if self.rpm else 0.0

    @property
    def peak_power_rpm(self) -> float:
        if not self.rpm:
            return 0.0
        return self.rpm[max(range(len(self.rpm)), key=lambda i: self.torque[i] * self.rpm[i])]

    @property
    def cutoff(self) -> float:
        """Where the engine stops pulling: the first speed past peak power at
        which torque has fallen below half its peak (the fuel cut-off ramp)."""
        if not self.rpm:
            return 0.0
        half = max(self.torque) / 2.0
        start = self.peak_power_rpm
        for i in range(len(self.rpm) - 1):
            a, b = self.rpm[i], self.rpm[i + 1]
            if b <= start:
                continue
            ta, tb = self.torque[i], self.torque[i + 1]
            if tb < half <= ta:
                return a + (ta - half) / (ta - tb) * (b - a) if ta != tb else b
        return self.rpm[-1]

    @classmethod
    def from_points(cls, points, label: str = "") -> "TorqueCurve":
        """From ``[(rpm, Nm), ...]`` or objects with ``rpm`` and ``torque``."""
        pts = []
        for p in points:
            if hasattr(p, "rpm"):
                pts.append((p.rpm, p.torque))
            else:
                pts.append((p[0], p[1]))
        return cls(tuple(a for a, _ in pts), tuple(b for _, b in pts), label)


def _turbine_power(curve: TorqueCurve, nt: float, slip: float) -> float:
    """Power reaching the gearbox at turbine speed ``nt`` with converter
    ``slip`` (engine = turbine x (1 + slip); torque ratio ~1 near coupling)."""
    return curve.torque_at(nt * (1.0 + slip)) * nt / 9549.2966


def crossover(curve_from: TorqueCurve, curve_to: TorqueCurve, r_from: float, r_to: float,
              slip_from: float = 0.0, slip_to: float = 0.0,
              ceiling: Optional[float] = None, step: float = 5.0) -> Tuple[float, str]:
    """``(turbine rpm in the gear left, reason)`` of the best full-load upshift.

    ``reason`` is ``"crossover"`` (the next gear gives more thrust from here)
    or ``"ceiling"`` (it never does before the ceiling -- engine rpm --
    which then sets the point)."""
    k = r_to / r_from
    top = ceiling if ceiling else curve_from.cutoff
    nt_max = top / (1.0 + slip_from)
    nt = max(curve_from.peak_torque_rpm, 1000.0) / (1.0 + slip_from)
    while nt <= nt_max:
        if _turbine_power(curve_from, nt, slip_from) <= _turbine_power(curve_to, nt * k, slip_to):
            return nt, "crossover"
        nt += step
    return nt_max, "ceiling"


def mean_power(curve: TorqueCurve, n_shift: float, k: float, slip: float = 0.0,
               n: int = 48) -> float:
    """Average power (kW) across the rpm the shift drops through, ``k x
    n_shift .. n_shift`` (turbine rpm): the band the next gear starts in. A
    later or earlier shift moves it; the better band has more power."""
    lo = n_shift * k
    return sum(_turbine_power(curve, lo + (n_shift - lo) * j / n, slip)
               for j in range(n + 1)) / (n + 1)


# ------------------------------------------------------------------- lead
@dataclass
class VehicleModel:
    """What the engine's rate of climb needs beyond the curve. Defaults are a
    mid-size saloon; the editor shows and keeps every one of them."""
    mass_kg: float = 1800.0
    axle_ratio: float = 4.16
    circumference_m: float = 2.0
    response_s: float = 0.30      # threshold -> ratio change, at full load
    cda: float = 0.65             # drag area, m^2
    crr: float = 0.012            # rolling resistance
    efficiency: float = 0.92      # driveline


def lead_physics(curve: TorqueCurve, ratio: float, nt: float, veh: VehicleModel,
                 slip: float = 0.0) -> float:
    """Turbine rpm the gear climbs during ``veh.response_s`` at turbine speed
    ``nt``: the wheel force from the engine's torque there, less drag and
    rolling resistance, over the mass and the gear's rotating inertia."""
    overall = ratio * veh.axle_ratio
    if overall <= 0 or veh.circumference_m <= 0 or veh.mass_kg <= 0:
        return 0.0
    rw = veh.circumference_m / (2.0 * math.pi)
    t = curve.torque_at(nt * (1.0 + slip))
    force = t * overall * veh.efficiency / rw
    v = nt / overall * veh.circumference_m / 60.0
    resist = 0.5 * 1.2 * veh.cda * v * v + veh.crr * veh.mass_kg * 9.81
    inertia = 1.04 + 0.0025 * overall * overall
    a = max(0.0, (force - resist) / (veh.mass_kg * inertia))
    rate = a * overall * 60.0 / veh.circumference_m       # turbine rpm / s
    return rate * veh.response_s


def measured_leads(events, threshold_of: Callable[[object], Optional[float]],
                   ratio_of_line: Callable[[int], float],
                   min_pedal: float = 95.0) -> Dict[int, Tuple[float, int]]:
    """``{line: (turbine rpm, events)}``: how far past the threshold logged
    full-load upshifts had climbed when the ratio began to change.

    ``events`` are :class:`~.logfit.Event` s (the shift's start = the last
    sample still in the old ratio); ``threshold_of(event)`` gives the
    threshold the car had (output rpm, for the version flashed), or None to
    leave the event out. Only steady upshifts at ``min_pedal`` or more count.
    """
    acc: Dict[int, List[float]] = {}
    for e in events:
        if getattr(e, "kind", "") != "up" or e.line is None or e.pedal < min_pedal \
                or not getattr(e, "steady", True):
            continue
        thr = threshold_of(e)
        if thr is None:
            continue
        lead = (e.out_rpm - thr) * ratio_of_line(e.line)
        if lead < 0:
            continue
        acc.setdefault(e.line, []).append(lead)
    return {ln: (sum(v) / len(v), len(v)) for ln, v in acc.items()}


# ------------------------------------------------------------------- plan
@dataclass
class ShiftTarget:
    """One upshift's full-load optimum, with the evidence for it."""
    line: int
    label: str
    gear_from: int
    gear_to: int
    ratio: float                  # of the gear left
    shift_turbine: float          # turbine rpm where the shift should happen
    reason: str                   # "crossover" | "ceiling"
    slip: float
    lead: float                   # turbine rpm climbed after the threshold
    lead_source: str              # "physics" | "logs (n)" | "manual"
    threshold: float              # turbine rpm = map value x ratio
    k: float                      # post-shift / pre-shift speed

    @property
    def output(self) -> float:
        """The map value, output rpm."""
        return self.threshold / self.ratio

    @property
    def shift_engine(self) -> float:
        return self.shift_turbine * (1.0 + self.slip)


@dataclass
class FullLoadPlan:
    targets: Dict[int, ShiftTarget]
    curve: TorqueCurve
    ceiling: float
    notes: List[str] = field(default_factory=list)
    #: a gear's own curve, where it differs from ``curve``
    curves: Dict[int, TorqueCurve] = field(default_factory=dict)

    def curve_for(self, gear: int) -> TorqueCurve:
        return self.curves.get(gear, self.curve)

    def outputs(self) -> Dict[int, float]:
        return {ln: t.output for ln, t in self.targets.items()}

    def gain(self, line: int, current_output: float) -> float:
        """Fractional gain in average power (see :func:`mean_power`) of the
        optimum over a line now at ``current_output`` (output rpm)."""
        t = self.targets.get(line)
        if t is None:
            return 0.0
        now = (current_output * t.ratio + t.lead)
        c = self.curve_for(t.gear_from)
        a = mean_power(c, now, t.k, t.slip)
        b = mean_power(c, t.shift_turbine, t.k, t.slip)
        return (b - a) / a if a > 0 else 0.0


def plan_upshifts(sset: ScheduleSet, curve: TorqueCurve, *,
                  curves: Optional[Dict[int, TorqueCurve]] = None,
                  slip: Optional[Dict[int, float]] = None,
                  ceiling: Optional[float] = None,
                  vehicle: Optional[VehicleModel] = None,
                  measured: Optional[Dict[int, Tuple[float, int]]] = None,
                  manual: Optional[Dict[int, float]] = None,
                  lines: Optional[Sequence[int]] = None) -> FullLoadPlan:
    """The full-load optimum of every upshift line of ``sset``.

    ``curves`` gives a gear its own curve (a per-gear driver's wish or torque
    limit), else ``curve``. ``slip`` is converter slip by gear (0.06 = 6 %).
    ``ceiling`` is the highest engine rpm to shift at (default: 200 below the
    curve's cut-off). The lead per line is ``manual`` (turbine rpm) when
    given, else ``measured`` (from logs), else :func:`lead_physics`.
    """
    curves = curves or {}
    slip = slip or {}
    veh = vehicle or VehicleModel()
    top = ceiling if ceiling else max(0.0, curve.cutoff - 200.0)
    out: Dict[int, ShiftTarget] = {}
    notes: List[str] = []
    # never shift at full load before the engine reaches peak torque: a
    # ceiling under it (a typing slip, a tool's guess) would put upshifts at
    # walking pace
    floor = max(curve.peak_torque_rpm, 1000.0) + 200.0
    if top < floor:
        notes.append("The ceiling (%.0f rpm) is under peak torque; %.0f rpm is used"
                     % (top, floor))
        top = floor
    for ln in sset.lines:
        if ln.kind != "up" or (lines is not None and ln.index not in lines):
            continue
        if not ln.ratio_left or not ln.ratio_entered:
            continue
        g0, g1 = ln.gear_left, ln.gear_entered
        c0, c1 = curves.get(g0, curve), curves.get(g1, curve)
        s0, s1 = slip.get(g0, 0.0), slip.get(g1, 0.0)
        nt, why = crossover(c0, c1, ln.ratio_left, ln.ratio_entered, s0, s1, top)
        if manual and ln.index in manual:
            lead, src = float(manual[ln.index]), "manual"
        elif measured and ln.index in measured:
            lead, src = measured[ln.index][0], "logs (%d)" % measured[ln.index][1]
        else:
            lead, src = lead_physics(c0, ln.ratio_left, nt, veh, s0), "physics"
        # the lead never takes the point below 60 % of the shift speed
        thr = max(nt - lead, 0.6 * nt)
        if thr > nt - lead + 1e-6:
            notes.append("%s: a lead of %.0f rpm is more than the shift allows; "
                         "%.0f is used" % (ln.label, lead, nt - thr))
            lead = nt - thr
        out[ln.index] = ShiftTarget(ln.index, ln.label, g0, g1, ln.ratio_left, nt, why,
                                    s0, lead, src, thr, ln.ratio_entered / ln.ratio_left)
        if why == "ceiling":
            notes.append("%s: gear %d still pulls harder than %d up to the ceiling "
                         "(%.0f rpm), which sets the point" % (ln.label, g0, g1, top))
    return FullLoadPlan(out, curve, top, notes, dict(curves))


def open_gears(sset: ScheduleSet, tcc: Optional[ScheduleSet],
               selected: Sequence[int]) -> List[int]:
    """Gears whose converter is open at full load in the selected shift
    programs, read from the lock-up set ``tcc``: no characteristic of that
    gear ("Gear n") serves any of the programs (``states`` are SKL ids), or
    its full lock (RC) at the top pedal row is "off". 1st when there is no
    lock-up set or nothing is selected."""
    sel = [sset.schedules[i] for i in selected]
    gears = sorted({ln.gear_left for ln in sset.lines if ln.kind == "up"})
    if tcc is None or not sel:
        return [1]
    rc = next((ln.index for ln in tcc.lines if ln.label == "RC"), None)
    progs = {(s.bank, s.number) for s in sel}
    # without program references (8HP Gen1/Gen2 until the SKL mapping is
    # solved) every characteristic of the gear counts
    by_program = any(t.states for t in tcc.schedules)
    out = []
    for g in gears:
        locks = False
        for t in tcc.schedules:
            if t.group != "Gear %d" % g:
                continue
            if by_program and not any((t.bank, n) in progs for n in t.states):
                continue
            if rc is None or not t.is_off(t.values()[(t.rows - 1) * t.cols + rc]):
                locks = True
                break
        if not locks:
            out.append(g)
    return out


def blend_threshold(sset: ScheduleSet, blends, lookup) -> Callable[[object], Optional[float]]:
    """``threshold_of(event)`` for :func:`measured_leads`: the threshold the
    car had where a logged shift happened -- the blend (``logfit``/``blend``
    :class:`~.blend.Blend`) whose programs include the event's program and
    dsp, its corners read with ``lookup`` (the version flashed then)."""
    usable = [b for b in blends if b.programs and b.indices(sset)]

    def threshold(e):
        b = next((b for b in usable if e.program in b.programs
                  and (b.dsp is None or e.dsp == b.dsp)), None)
        if b is None or lookup is None:
            return None
        tot = wsum = 0.0
        for i, w in b.indices(sset):
            v = lookup(i, e.line, e.pedal, e.version)
            if v is None:
                return None
            tot += w * v
            wsum += w
        return tot / wsum if wsum else None
    return threshold


# --------------------------------------------------------------- operation
def _factory_slope(pedal: Sequence[float], col: Sequence[float], skip) -> float:
    """The line's steepest rise per pedal % (output rpm) over intervals of at
    least :data:`MIN_SPAN`, "off" cells left out."""
    best = 0.0
    for r in range(len(pedal) - 1):
        span = pedal[r + 1] - pedal[r]
        if span < MIN_SPAN or skip(col[r]) or skip(col[r + 1]):
            continue
        best = max(best, (col[r + 1] - col[r]) / span)
    return best


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


@dataclass
class FullLoadTarget(Op):
    """Set the full-load rows of each reached upshift to ``targets`` (output
    rpm by line index) and blend the part-load rows into them.

    * ``mode`` :data:`MODE_ALL` puts every row at or above ``p_full`` on the
      target; :data:`MODE_KICKDOWN` puts the kickdown row there and keeps
      each lower full-load row its current distance below the kickdown row.
    * ``shape`` :data:`SHAPE_FACTORY` scales each line between ``p0`` and the
      full-load rows, so it keeps the factory's shape (a factory step stays,
      in proportion); :data:`SHAPE_EASE` moves the rows by the change at full
      load times a smoothstep from 0 at ``p0``; :data:`SHAPE_ROWS` leaves the
      part-load rows alone, except one above a lowered full-load point,
      which comes down to it (the line never falls into full load); nothing
      is spread over them. Rows below ``p0`` are never touched by the blend.
    * ``smooth`` (a factor, None = off): a rise the edit made steeper than
      ``smooth`` x the line's own steepest factory rise is spread over the
      rows below it, down to ``smooth_floor`` pedal at most.
    * ``kickdown_downshifts``: on kickdown rows the paired downshift moves by
      the same amount, so the kickdown hysteresis is kept.
    * ``pair_gap`` (output rpm, None = off): where a lowered upshift would
      come within this of its downshift (or the cell's own smaller gap), the
      downshift comes down with it -- a lowered full-load line must not leave
      the box hunting.
    * ``worth(i, line, old)`` -> False leaves that line of that schedule
      alone (a minimum gain).

    It may raise or lower: the scope's direction applies as for every op.
    """
    scope: Scope
    targets: Dict[int, float]
    shape: str = SHAPE_FACTORY
    mode: str = MODE_ALL
    p0: float = 80.0
    p_full: float = 99.9
    smooth: Optional[float] = 1.5
    smooth_floor: float = 50.0
    kickdown_downshifts: bool = True
    pair_gap: Optional[float] = 60.0
    worth: Optional[Callable[[int, int, float], bool]] = None
    name = "full-load"

    def describe(self):
        return "full-load optimum (%s%s)" % (
            dict(SHAPES).get(self.shape, self.shape).lower(),
            ", blend from %g %%" % self.p0 if self.shape != SHAPE_ROWS else "")

    def _column(self, s, before, i, c):
        return [before[i][r * s.cols + c] for r in range(s.rows)]

    def new_column(self, s, col: List[float], target: float) -> List[float]:
        """The line ``col`` (one schedule, output rpm) as this op leaves it,
        before rounding and direction."""
        P = list(s.pedal)
        new = list(col)
        full = [r for r, p in enumerate(P) if p >= self.p_full - 1e-9 and not s.is_off(col[r])]
        if not full:
            return new
        kd = [r for r in full if P[r] > KICKDOWN_ABOVE]
        if self.mode == MODE_KICKDOWN and kd and len(kd) < len(full):
            top = max(col[r] for r in kd)
            for r in full:
                new[r] = target if r in kd else target - max(0.0, top - col[r])
        else:
            for r in full:
                new[r] = target
        rf = full[0]
        old_top, new_top = col[rf], new[rf]
        blend = [r for r, p in enumerate(P) if self.p0 - 1e-9 <= p < P[rf] and r < rf
                 and not s.is_off(col[r])]
        if self.shape == SHAPE_FACTORY and blend:
            base = interp(P, col, self.p0)
            if old_top - base > 1e-6 and new_top > base:
                f = (new_top - base) / (old_top - base)
                for r in blend:
                    new[r] = base + (col[r] - base) * f
            else:
                for r in blend:
                    new[r] = min(col[r], new_top)
        elif self.shape == SHAPE_EASE and blend:
            d = new_top - old_top
            span = P[rf] - self.p0
            for r in blend:
                w = _smoothstep((P[r] - self.p0) / span) if span > 0 else 1.0
                v = col[r] + w * d
                new[r] = min(v, max(col[r], new_top))
        else:
            # full-load rows only -- but a part-load row above the new top
            # would make the line fall into full load
            for r in blend:
                new[r] = min(col[r], new_top) if new_top < col[r] and self.mode == MODE_ALL \
                    else col[r]
        # a lowered full-load point: rows below the blend that sit above it
        # come down to it (down to smooth_floor), so the line never falls
        # into full load
        for r in range(rf - 1, -1, -1):
            if P[r] < self.smooth_floor - 0.01 or s.is_off(col[r]):
                break
            if new[r] > new_top:
                new[r] = new_top
        if self.smooth and self.shape != SHAPE_ROWS:
            lim = self.smooth * _factory_slope(P, col, s.is_off)
            if lim > 0:
                for r in range(rf - 1, -1, -1):
                    if P[r] < self.smooth_floor - 0.01 or s.is_off(col[r]):
                        break
                    span = P[r + 1] - P[r]
                    if span < MIN_SPAN:
                        continue
                    if (new[r + 1] - new[r]) / span > lim + 1e-9:
                        new[r] = max(new[r], new[r + 1] - lim * span)
        return new

    def apply(self, grid, sset):
        before = {i: list(v) for i, v in grid.items()}
        changed = {}
        for i in self.scope.schedules:
            s = sset.schedules[i]
            K = s.cols
            for c in self.scope.lines:
                if c not in self.targets or c >= K:
                    continue
                col = self._column(s, before, i, c)
                full = [r for r, p in enumerate(s.pedal) if p >= self.p_full - 1e-9]
                if not full:
                    continue
                if self.worth is not None and not self.worth(i, c, col[full[0]]):
                    continue
                new = self.new_column(s, col, self.targets[c])
                pair = sset.line(c).pair
                for r in range(s.rows):
                    old = col[r]
                    if s.is_off(old):
                        continue
                    v = _finish(s, old, new[r], self.scope)
                    if v == old:
                        continue
                    grid[i][r * K + c] = v
                    changed[(i, r, c)] = self.describe()
                    if pair is None or pair >= K:
                        continue
                    k = r * K + pair
                    o = before[i][k]
                    if s.is_off(o):
                        continue
                    nd = grid[i][k]
                    if self.kickdown_downshifts and s.pedal[r] > KICKDOWN_ABOVE:
                        nd = _finish(s, o, o + (v - old), self.scope)
                    if self.pair_gap is not None and sset.line(c).kind == "up":
                        need = min(max(0.0, old - o), self.pair_gap)
                        if nd > v - need:
                            nd = max(0.0, v - need)
                            if not getattr(s.entity, "is_float", False):
                                nd = float(math.floor(nd))
                    if nd != o:
                        grid[i][k] = nd
                        changed[(i, r, pair)] = self.describe()
                # a downshift pulled down with its upshift: the rows below
                # it follow, so that line does not fall as pedal rises either
                if pair is not None and pair < K and self.pair_gap is not None:
                    for r in range(s.rows - 2, -1, -1):
                        if s.pedal[r] < self.smooth_floor - 0.01:
                            break
                        k, k1 = r * K + pair, (r + 1) * K + pair
                        lo = grid[i][k1]
                        if s.is_off(grid[i][k]) or s.is_off(lo) or lo == before[i][k1]:
                            continue
                        if grid[i][k] > lo:
                            grid[i][k] = lo
                            changed[(i, r, pair)] = self.describe()
        return changed


# ------------------------------------------------------------------ check
@dataclass
class StepCheck:
    """A line should not rise between two pedal rows much more steeply than
    it does anywhere in the factory calibration: such a step means a small
    pedal movement moves the shift point a long way. Judged only where the
    edit made the rise steeper; the kickdown interval (narrower than
    :data:`MIN_SPAN`) is a step by design and left out. Report only -- the
    full-load operation spreads its own steps (``FullLoadTarget.smooth``)."""
    factor: float = 1.5
    code = "step"
    autofix = False

    def run(self, sset, origin, grid, changed):
        from .checks import Finding
        out = []
        for i, c in sorted({(i, c) for (i, r, c) in changed}):
            s = sset.schedules[i]
            K = s.cols
            was = [origin[i][r * K + c] for r in range(s.rows)]
            now = [grid[i][r * K + c] for r in range(s.rows)]
            lim = self.factor * _factory_slope(s.pedal, was, s.is_off)
            if lim <= 0:
                continue
            bad = []
            for r in range(s.rows - 1):
                span = s.pedal[r + 1] - s.pedal[r]
                if span < MIN_SPAN or s.is_off(now[r]) or s.is_off(now[r + 1]):
                    continue
                a, b = (now[r + 1] - now[r]) / span, (was[r + 1] - was[r]) / span
                # a rounded cell next to a spread step may sit 1 rpm over
                if a > lim + 1.0 / span and a > b + 1e-9:
                    bad.append(r)
            if bad:
                out.append(Finding(
                    "warning", self.code,
                    "%s %s rises steeply between %s" % (
                        s.key, sset.line(c).label,
                        ", ".join("%.1f–%.1f %%" % (s.pedal[r], s.pedal[r + 1])
                                  for r in bad)),
                    [(i, r, c) for r in bad]))
        return out


__all__ = ["TorqueCurve", "VehicleModel", "ShiftTarget", "FullLoadPlan",
           "FullLoadTarget", "StepCheck", "crossover", "mean_power", "lead_physics",
           "measured_leads", "plan_upshifts", "open_gears", "blend_threshold",
           "SHAPES", "MODES",
           "SHAPE_FACTORY", "SHAPE_EASE", "SHAPE_ROWS", "MODE_ALL", "MODE_KICKDOWN"]

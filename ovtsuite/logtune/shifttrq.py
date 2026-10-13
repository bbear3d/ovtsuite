# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Torque intervention during shifts: what the TCU asked for vs what arrived.

Read-only. This measures and reports; it changes nothing.

The point of it. "Rpm dips when downshifting" has survived every revision of this
tune, and there are two completely different reasons it could: the TCU asks the
engine for too small a torque change, or it asks for the right change and the
engine does not deliver it. Those have opposite fixes, and nothing in a shift
log tells them apart by itself. This separates them.

That distinction has already bitten this car once. The EDC16 carried
``Gearbx_trqIncMax_C = 0`` -- the ceiling on the torque *increase* the gearbox
may request -- so every TCU request produced nothing at all, however the TCU's
own maps were calibrated (see the project note on the gearbox torque-intervention
gate). An auto-tuner that simply raised TCU maps would have "fixed" that by
writing numbers with no effect, and hidden the real cause. Hence: measure first.

What is asked. The intervention family is per-event and named, one map per shift
in both directions:

    KF_MEHAUPT<from><to>        e.g. KF_MEHAUPT43 for a 4>3
    KF_MEHAUPTSP<from><to>      the sport-program variant

Each is 10x6 in Nm (``raw*6 - 768``), indexed by input torque down the rows
(-78..300 Nm on the 4>3) and speed across the columns (512..5760 rpm). The
sport variant is chosen when the logged shift program is a sport state, taken
from the controller's own two tables (SKL_ZUSTAND_0 -> STDAT_kprt_Drv_0), not
from a guess about which programs are sporty.

What is delivered. Engine torque as the TCU sees it, from group 9. The baseline
is the median over a window ending just before the shift starts; the delivered
change is the largest signed deviation from that baseline across the shift. So a
shift taken while the driver is moving the pedal measures the driver, not the
intervention -- which is why the preset's instructions ask for a few steady
seconds between events, and why ``pedal_move`` is reported per event so a noisy
one can be discarded rather than silently averaged in.

Three things this does NOT assume, because they are not established:

* **The speed axis.** 512-5760 rpm fits engine speed and turbine speed about
  equally well. ``speed_role`` selects it, and every event reports whether the
  other choice would land in a different column (``speed_ambiguous``). Where it
  would, the asked figure is not trustworthy and the report says so.
* **The sign convention.** Every map in the family holds non-negative Nm, on
  upshifts as well as downshifts, so the value is presumably a magnitude whose
  direction the event implies. Rather than encode that, the report prints asked
  as a magnitude and delivered signed, and lets the pattern show it.
* **That the engine can deliver it.** ``gate_nm`` is the ECU's
  ``Gearbx_trqIncMax_C``. Anything asked above it is clipped before it reaches
  the engine, so the report flags those events and states the value the gate
  would need for the asked figures to pass unclipped.

Shift location is by ``turbine / output`` against the six ratios -- the measured
gear ratio regardless of converter slip -- taking the last sample still in the
old ratio as the start. "Engaged gear" reports the completed shift about 0.7 s
late and is useless for this.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

#: measured on the car, turbine / output, within 0.4 %
RATIOS = (4.171, 2.34, 1.521, 1.143, 0.867, 0.691)
RATIO_TOL = 0.06
#: ECU default once an automatic is declared; see the torque-intervention note
DEFAULT_GATE_NM = 100.0
BASELINE_S = 1.0                # window before the shift the baseline is taken over
TAIL_S = 0.6                    # how far past the shift end the response is tracked
#: a shift taken while the pedal moves this much measures the driver, not the
#: intervention, so those events are reported but kept out of the aggregates
STEADY_PCT = 3.0


@dataclass
class Event:
    t: float
    from_gear: int
    to_gear: int
    kind: str                   # "up" or "down"
    out: float                  # output shaft rpm at the shift start
    speed: float                # the map's speed input
    pedal: Optional[float]
    program: Optional[float]
    baseline_nm: Optional[float]
    delivered_nm: Optional[float]
    pedal_move: Optional[float]
    map_name: Optional[str] = None
    asked_nm: Optional[float] = None
    clipped: bool = False
    speed_ambiguous: bool = False

    @property
    def label(self) -> str:
        return "%d>%d" % (self.from_gear, self.to_gear)


@dataclass
class Report:
    events: List[Event] = field(default_factory=list)
    gate_nm: float = DEFAULT_GATE_NM
    needed_gate_nm: float = 0.0
    speed_role: str = "tcu_rpm"
    notes: List[str] = field(default_factory=list)

    def by_map(self) -> Dict[str, List[Event]]:
        out: Dict[str, List[Event]] = {}
        for e in self.events:
            if e.map_name:
                out.setdefault(e.map_name, []).append(e)
        return out


# ---- the controller's own view of which programs are sporty ---------------
#: the asked-torque map family the report reads, one map per shift
ASKED_PREFIX = "KF_MEHAUPT"


def available(session) -> bool:
    """True when the open calibration carries the per-shift intervention maps
    (KF_MEHAUPT<from><to>) -- a structural test on the parameter tree, not a
    guess from the file name or platform."""
    return session is not None and any(
        (e.name or "").startswith(ASKED_PREFIX)
        for e in getattr(session, "entities", ()) or ())


def sport_programs(session) -> Set[int]:
    """Program numbers whose mode is S, from SKL_ZUSTAND_0 -> STDAT_kprt_Drv_0.

    Confirmed on the car: D reads 10, S reads 12, M reads 27, each predicted
    from these two tables before being measured.
    """
    def flat(name):
        for e in session.entities:
            if e.name == name:
                out = []
                for row in e.values():
                    out.extend(row) if isinstance(row, (list, tuple)) else out.append(row)
                return [int(v) for v in out]
        return None
    skl, mode = flat("SKL_ZUSTAND_0"), flat("STDAT_kprt_Drv_0")
    if not skl or not mode:
        return set()
    return {st for st, s in enumerate(skl) if s < len(mode) and mode[s] == 1}


# ---- map lookup ------------------------------------------------------------
def find_map(session, from_gear: int, to_gear: int, sport: bool):
    name = "KF_MEHAUPT%s%d%d" % ("SP" if sport else "", from_gear, to_gear)
    for e in session.entities:
        if e.name == name:
            return e, name
    if sport:                                    # no SP variant for this event
        return find_map(session, from_gear, to_gear, False)
    return None, name


def _axis_cell(axis: Sequence[float], v: float) -> Tuple[int, int, float]:
    """(lo index, hi index, fraction) for bilinear interpolation, clamped."""
    if v <= axis[0]:
        return 0, 0, 0.0
    if v >= axis[-1]:
        n = len(axis) - 1
        return n, n, 0.0
    for i in range(1, len(axis)):
        if v <= axis[i]:
            span = axis[i] - axis[i - 1]
            return i - 1, i, (v - axis[i - 1]) / span if span else 0.0
    n = len(axis) - 1
    return n, n, 0.0


def lookup(entity, torque: float, speed: float) -> float:
    """Bilinear read of a 10x6 intervention map: rows torque, columns speed."""
    ys, xs = entity.y_values(), entity.x_values()
    vals = entity.values()
    r0, r1, fr = _axis_cell(ys, torque)
    c0, c1, fc = _axis_cell(xs, speed)
    v00, v01 = vals[r0][c0], vals[r0][c1]
    v10, v11 = vals[r1][c0], vals[r1][c1]
    top = v00 + (v01 - v00) * fc
    bot = v10 + (v11 - v10) * fc
    return top + (bot - top) * fr


def _column(entity, speed: float) -> int:
    xs = entity.x_values()
    c0, c1, fc = _axis_cell(xs, speed)
    return c1 if fc >= 0.5 else c0


# ---- shift detection -------------------------------------------------------
def _gear_at(run, i: int) -> Optional[int]:
    turb, out = run.get("tcu_nturb", i), run.get("tcu_nout", i)
    if not turb or not out or out < 80 or turb < 200:
        return None
    ratio = turb / out
    best = min(range(6), key=lambda k: abs(ratio - RATIOS[k]))
    return best + 1 if abs(ratio - RATIOS[best]) / RATIOS[best] < RATIO_TOL else None


def _median(xs: Sequence[float]) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[len(xs) // 2]


def _shifts(run) -> List[Tuple[int, int, int, int]]:
    """``(i_start, i_end, from_gear, to_gear)`` -- start is the last sample
    still in the old ratio."""
    out = []
    cur = last = None
    for i in range(len(run)):
        g = _gear_at(run, i)
        if g is None:
            continue
        if cur is not None and g != cur and last is not None \
                and run.t[i] - run.t[last] < 2.5 and abs(g - cur) == 1:
            out.append((last, i, cur, g))
        if g != cur:
            cur = g
        last = i
    return out


# ---- the analysis ----------------------------------------------------------
def analyse(session, runs, gate_nm: float = DEFAULT_GATE_NM,
            speed_role: str = "tcu_rpm") -> Report:
    rep = Report(gate_nm=gate_nm, speed_role=speed_role)
    sporty = sport_programs(session)
    if not sporty:
        rep.notes.append("Could not read the state tables, so every event is "
                         "measured against the NORMAL map even in a sport program.")
    for run in runs:
        for role in ("tcu_trq", "tcu_nturb", "tcu_nout"):
            if not run.has(role):
                rep.notes.append("%s: no %s column -- use the 'Torque intervention "
                                 "during shifts' preset (groups 1 + 9)." % (run.name, role))
                break
        else:
            if run.rate() < 6.0:
                rep.notes.append("%s: %.1f Hz. A shift lasts 0.25-0.5 s, so below "
                                 "~6 Hz the intervention is one sample and the "
                                 "delivered figure is not meaningful."
                                 % (run.name, run.rate()))
            _one_run(session, run, rep, sporty)
    asked = [e.asked_nm for e in rep.events if e.asked_nm is not None]
    rep.needed_gate_nm = max(asked) if asked else 0.0
    return rep


def _one_run(session, run, rep: Report, sporty: Set[int]):
    for i0, i1, g_from, g_to in _shifts(run):
        t0 = run.t[i0]
        base = _median([run.get("tcu_trq", j) for j in range(len(run))
                        if t0 - BASELINE_S <= run.t[j] < t0])
        window = [j for j in range(len(run)) if t0 <= run.t[j] <= run.t[i1] + TAIL_S]
        trq = [run.get("tcu_trq", j) for j in window]
        trq = [v for v in trq if v is not None]
        peds = [run.get("tcu_pedal", j) for j in window]
        peds = [v for v in peds if v is not None]
        delivered = None
        if base is not None and trq:
            hi, lo = max(trq) - base, min(trq) - base
            delivered = hi if abs(hi) >= abs(lo) else lo
        speed = run.get(rep.speed_role, i0) or 0.0
        prog = run.get("tcu_prog", i0)
        ev = Event(t=t0, from_gear=g_from, to_gear=g_to,
                   kind="up" if g_to > g_from else "down",
                   out=run.get("tcu_nout", i0) or 0.0, speed=speed,
                   pedal=run.get("tcu_pedal", i0), program=prog,
                   baseline_nm=base, delivered_nm=delivered,
                   pedal_move=(max(peds) - min(peds)) if len(peds) > 1 else None)
        sport = prog is not None and int(prog) in sporty
        ent, name = find_map(session, g_from, g_to, sport)
        ev.map_name = name if ent is not None else None
        if ent is not None and base is not None:
            ev.asked_nm = lookup(ent, base, speed)
            ev.clipped = ev.asked_nm > rep.gate_nm
            other = run.get("tcu_nturb" if rep.speed_role == "tcu_rpm" else "tcu_rpm", i0)
            if other:
                ev.speed_ambiguous = _column(ent, speed) != _column(ent, other)
        rep.events.append(ev)


# ---- report ----------------------------------------------------------------
def to_text(rep: Report) -> str:
    L = ["Torque intervention during shifts -- asked vs delivered",
         "speed axis read from %s;  ECU gate %.0f Nm" % (rep.speed_role, rep.gate_nm), ""]
    for n in rep.notes:
        L.append("  note: " + n)
    if rep.notes:
        L.append("")
    if not rep.events:
        L.append("  no shifts found.")
        return "\n".join(L)
    L.append("  %-5s %-5s %7s %7s %9s %9s %9s %7s  %s"
             % ("evt", "kind", "pedal", "speed", "baseline", "asked", "delivered",
                "ped mv", "map"))
    for e in sorted(rep.events, key=lambda x: (x.from_gear, x.to_gear, x.t)):
        flags = []
        if e.clipped:
            flags.append("CLIPPED by the gate")
        if e.speed_ambiguous:
            flags.append("speed axis ambiguous")
        if e.pedal_move is not None and e.pedal_move > 5:
            flags.append("pedal moved %.0f%%" % e.pedal_move)
        L.append("  %-5s %-5s %6s%% %7.0f %9s %9s %9s %7s  %s%s"
                 % (e.label, e.kind,
                    "%.1f" % e.pedal if e.pedal is not None else "-",
                    e.speed,
                    "%.0f" % e.baseline_nm if e.baseline_nm is not None else "-",
                    "%.0f" % e.asked_nm if e.asked_nm is not None else "-",
                    "%+.0f" % e.delivered_nm if e.delivered_nm is not None else "-",
                    "%.0f%%" % e.pedal_move if e.pedal_move is not None else "-",
                    e.map_name or "(no map)",
                    ("   " + "; ".join(flags)) if flags else ""))
    L.append("")
    # Signed throughout. An earlier version averaged abs(delivered), which made
    # a -51 Nm collapse look like a +51 Nm blip and reported "56% of asked" for
    # the one event family that in fact delivered nothing. Sign IS the finding
    # here, so only clean events (pedal essentially still) are aggregated and
    # the count of discarded ones is shown rather than hidden.
    L.append("  per map, CLEAN events only (pedal moved <= %d%% across the shift):" % STEADY_PCT)
    for name, evs in sorted(rep.by_map().items()):
        clean = [e for e in evs
                 if e.asked_nm is not None and e.delivered_nm is not None
                 and (e.pedal_move is None or e.pedal_move <= STEADY_PCT)]
        drop = len(evs) - len(clean)
        if not clean:
            L.append("  %-18s %d event(s), none clean -- all had a moving pedal"
                     % (name, len(evs)))
            continue
        a = sum(e.asked_nm for e in clean) / len(clean)
        d = sum(e.delivered_nm for e in clean) / len(clean)
        verdict = ""
        if abs(a) >= 10:
            if (a > 0 and d < 0.25 * a) or (a < 0 and d > 0.25 * a):
                verdict = "   <== asked for %+.0f, got %+.0f: NOT delivered" % (a, d)
        L.append("  %-18s %d clean%s: asked %+.0f Nm mean, delivered %+.0f Nm mean%s"
                 % (name, len(clean), " (%d discarded)" % drop if drop else "",
                    a, d, verdict))
    if rep.needed_gate_nm > rep.gate_nm:
        L += ["", "  The gate is the binding constraint on at least one event: the "
                  "largest asked figure is %.0f Nm against a %.0f Nm ceiling."
                  % (rep.needed_gate_nm, rep.gate_nm),
              "  Raising a TCU map above the gate changes nothing until "
              "Gearbx_trqIncMax_C (and Gearbx_trqASTM2_C, which moves with it) "
              "is raised to at least that figure."]
    return "\n".join(L)

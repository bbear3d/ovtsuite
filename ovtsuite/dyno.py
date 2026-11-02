# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A calculated dyno chart: the torque and power a calibration asks for at
full load, rpm by rpm (docs/DYNO_PLAN.md).

What it is. At each engine speed the ECU fuels the engine to the LOWEST of
its full-load limits -- the driver's wish at 100 % pedal, the torque
limiter(s), the smoke limiter(s) at the air the engine has. That quantity
(mg/stroke) is what the file delivers; the platform's module supplies the
chain of limits as a :class:`DynoModel` (``dyno`` hook), everything here is
generic. No Qt: the GUI (``gui_dyno``) and tests use it alike.

Torque from quantity -- the reference. An EDC16 converts the torque it is
asked for into a quantity through its own torque-to-fuel map; raising that
map is ordinary tuning, after which the file's own Nm figures no longer mean
real Nm. So the delivered quantity is turned back into torque through the
REFERENCE file's conversion -- by default the original (the file as first
opened / the project set as read), or the comparison, or a version from the
history, as the user picks. A platform with no conversion map (EDC15) uses
the fuel-energy model ``T = q * cylinders * LHV * efficiency / 4 pi`` for
every curve, so the curves stay comparable.

Quantity through the duration maps. On a unit-injector (PD) engine the
quantity is only a label until the duration maps turn it into crank degrees
of delivery -- and a tune that rescales those maps (or raises a limit to a
quantity the injector cannot pass) changes what that label is worth. So,
where both files carry the injection tables of the End of injection view
(:mod:`ovtsuite.eoi`), the delivered quantity is first carried across:
this file's start of injection and duration at its quantity, then the
quantity the REFERENCE's duration maps need for that same duration at that
start (:class:`FuelMap`). That quantity goes into the reference's conversion.

What it is not. A dyno measures what the engine makes; this shows what the
calibration asks for. The air the smoke limits read comes from the target
boost (the turbo is assumed to reach it), and transient, temperature and
fault paths are left out. The GUI says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .eoi import Table, is_rpm_axis  # noqa: F401  (re-exported for modules)

# ---------------------------------------------------------------- units
#: (key, label, factor from kW)
POWER_UNITS = (("kW", "kW", 1.0),
               ("PS", "PS", 1.0 / 0.73549875),
               ("hp", "hp", 1.0 / 0.745699872))
#: (key, label, factor from Nm)
TORQUE_UNITS = (("Nm", "Nm", 1.0),
                ("lbft", "lb·ft", 1.0 / 1.3558179483),
                ("kgfm", "kgf·m", 1.0 / 9.80665))
DEFAULT_POWER, DEFAULT_TORQUE = "kW", "Nm"


def _unit(table, key):
    for k, lbl, f in table:
        if k == key:
            return lbl, f
    return table[0][1], table[0][2]


def power_label(key: str) -> str:
    return _unit(POWER_UNITS, key)[0]


def torque_label(key: str) -> str:
    return _unit(TORQUE_UNITS, key)[0]


def power_in(kw: float, key: str) -> float:
    return kw * _unit(POWER_UNITS, key)[1]


def torque_in(nm: float, key: str) -> float:
    return nm * _unit(TORQUE_UNITS, key)[1]


def power_kw(nm: float, rpm: float) -> float:
    """Power (kW) of torque ``nm`` at ``rpm``."""
    return nm * rpm * 2.0 * math.pi / 60.0 / 1000.0


# ---------------------------------------------------------------- physics
#: lower heating value of diesel fuel, J/kg
LHV_DIESEL = 42.8e6
#: specific gas constant of air, J/(kg K)
R_AIR = 287.05
#: stoichiometric air/fuel ratio of diesel (lambda 1)
AFR_STOICH = 14.5


def fuel_torque(q_mg: float, cylinders: int, efficiency: float) -> float:
    """Brake torque (Nm) of ``q_mg`` mg/stroke burnt at ``efficiency``: each
    cylinder fires once in two revolutions, so the work of one cycle spreads
    over 4 pi radians."""
    return q_mg * 1e-6 * cylinders * LHV_DIESEL * efficiency / (4.0 * math.pi)


def fuel_q(torque: float, cylinders: int, efficiency: float) -> float:
    """The inverse of :func:`fuel_torque`."""
    if cylinders <= 0 or efficiency <= 0:
        return 0.0
    return torque * 4.0 * math.pi / (1e-6 * cylinders * LHV_DIESEL * efficiency)


def air_mass(p_hpa: float, displacement: float, cylinders: int, ve: float,
             charge_c: float) -> float:
    """Air per stroke (mg) at manifold pressure ``p_hpa`` (absolute), engine
    ``displacement`` litres over ``cylinders``, volumetric efficiency ``ve``
    and charge temperature ``charge_c`` deg C (ideal gas)."""
    if cylinders <= 0:
        return 0.0
    v_cyl = displacement / 1000.0 / cylinders                # m^3
    kg = p_hpa * 100.0 * v_cyl * ve / (R_AIR * (charge_c + 273.15))
    return kg * 1e6


# ------------------------------------------------------------ conditions
@dataclass
class Conditions:
    """What the full-load run is worked out at. The defaults are a warm
    engine at sea level; the GUI shows every one of them."""
    baro: float = 1013.0            # atmospheric pressure, hPa
    charge_temp: float = 50.0       # intake charge temperature, deg C
    coolant: float = 90.0           # deg C
    ve: float = 0.90                # volumetric efficiency (air from boost)
    efficiency: float = 0.40        # brake efficiency, fuel-energy model
    #: which driver's-wish map ("" = the highest of them, any gear) --
    #: the keys of :attr:`DynoModel.wish_choices`
    wish: str = ""
    #: the smoke limits applied (keys of :attr:`DynoModel.smoke_choices`);
    #: None = the ones the file's own switches select
    smoke: Optional[Tuple[str, ...]] = None
    displacement: Optional[float] = None   # litres; None = the model's
    cylinders: Optional[int] = None        # None = the model's
    loss: float = 0.0               # drivetrain loss %, 0 = at the crank
    #: "duration": carry the quantity across through the injection-duration
    #: maps when both files have them (:class:`FuelMap`); "quantity": take
    #: the ECU's quantity as it is
    fuel_basis: str = "duration"
    step: float = 50.0              # rpm between points
    #: the most boost the turbo makes, hPa absolute (None = it reaches the
    #: target): caps the air the smoke limits read
    boost_cap: Optional[float] = None
    #: the time-limited overboost torque limit in full, where the model has
    #: one (:attr:`DynoModel.has_overboost`); off = the steady limit
    overboost: bool = False
    #: one gear of the drive (1 = 1st): a model with per-gear limits (the
    #: EDC16 ``Gearbx_trqMaxGear<n>_CUR``) applies that gear's instead of the
    #: highest. None = any gear
    gear: Optional[int] = None
    #: road speed per engine speed in that gear, (km/h)/rpm: where the ECU
    #: picks its driver's-wish map by v/n (EDC16 ``AccPed_stGearSel_CUR``),
    #: the map(s) it would pick there instead of the highest. None = any
    v_per_n: Optional[float] = None


# ---------------------------------------------------------------- results
@dataclass
class Limiter:
    """One full-load limit a model applies, for the legend and "go to"."""
    key: str
    label: str
    entities: Tuple[object, ...] = ()
    #: one line on what it is (tooltip)
    tip: str = ""


@dataclass
class FullLoad:
    """What a model works out at one engine speed."""
    q: float                         # delivered quantity, mg/stroke
    limiter: str                     # key of the binding limit
    limits: Dict[str, float]         # every limit, as a quantity (mg/stroke)
    boost: Optional[float] = None    # target boost used for the air, hPa
    air: Optional[float] = None      # air per stroke, mg
    asked: Optional[float] = None    # torque the ECU was asked for, Nm (EDC16)


@dataclass
class Point:
    rpm: float
    q: float
    torque: float                    # Nm at the crank (or wheel, with loss)
    power: float                     # kW
    limiter: str
    limits: Dict[str, float]
    boost: Optional[float] = None
    air: Optional[float] = None
    asked: Optional[float] = None
    #: the reference conversion had to be extended past its last breakpoint
    extrapolated: bool = False
    #: the quantity in the reference's terms (through the duration maps);
    #: None when it was taken as it is
    q_ref: Optional[float] = None
    #: the start of injection (deg BTDC) and duration (crank deg) used there
    soi: Optional[float] = None
    duration: Optional[float] = None


@dataclass
class Curve:
    label: str
    points: List[Point]
    limiters: Dict[str, Limiter]
    #: how quantity became torque, in words
    torque_source: str = ""
    notes: List[str] = field(default_factory=list)

    def peak_power(self) -> Optional[Point]:
        return max(self.points, key=lambda p: p.power) if self.points else None

    def peak_torque(self) -> Optional[Point]:
        return max(self.points, key=lambda p: p.torque) if self.points else None

    def runs(self) -> List[Tuple[str, float, float]]:
        """``[(limiter, rpm from, rpm to)]``: the bands of engine speed one
        limit governs, in order."""
        out: List[Tuple[str, float, float]] = []
        for p in self.points:
            if out and out[-1][0] == p.limiter:
                out[-1] = (p.limiter, out[-1][1], p.rpm)
            else:
                out.append((p.limiter, p.rpm, p.rpm))
        return out


# ------------------------------------------------------------------ model
class DynoModel:
    """The full-load chain of one calibration (one dataset of it). A
    platform's module subclasses it; see ``edc16/dyno16.py``."""

    platform = ""
    #: the dataset (EDC15 'Manual 1' ...); "" when the file has one
    group = ""
    #: the platform keeps a torque-to-fuel conversion (EDC16) -- the
    #: reference file's then turns quantity into torque
    has_conversion = False
    #: what turns quantity into torque, in words ("torque-to-fuel map")
    conversion_name = "torque-to-fuel map"

    def conversion_begin(self) -> None:
        """Called before a curve's torque conversions (a model that notes
        what its conversion limited starts counting here)."""

    def conversion_notes(self) -> List[str]:
        """Notes on what the conversion limited in the curve just made."""
        return []
    #: the calibration has a time-limited overboost limit
    #: (:attr:`Conditions.overboost` applies it)
    has_overboost = False

    def __init__(self):
        self.limiters: Dict[str, Limiter] = {}
        self.notes: List[str] = []
        #: what the definition lacks for the calculation (never guessed)
        self.missing: List[str] = []
        #: [(key, label)] -- driver's-wish maps to run with ("" = highest)
        self.wish_choices: List[Tuple[str, str]] = []
        #: [(key, label, on by default)]
        self.smoke_choices: List[Tuple[str, str, bool]] = []
        self.displacement: Optional[float] = None
        self.cylinders: Optional[int] = None
        #: where those two came from, in words
        self.engine_source = ""

    @property
    def label(self) -> str:
        return self.group or self.platform.upper()

    # -- what a subclass gives ------------------------------------------
    def rpm_range(self, cond: Conditions) -> Tuple[float, float]:
        raise NotImplementedError

    def full_load(self, rpm: float, cond: Conditions) -> FullLoad:
        raise NotImplementedError

    def demand(self, rpm: float, pedal: float, cond: Conditions) -> Optional[float]:
        """The quantity (mg/stroke) the driver's wish asks for at ``pedal`` %
        -- before any limit -- in the units of :attr:`FullLoad.q`; None when
        the model has no driver's wish. ``cond.gear`` / ``v_per_n`` pick the
        map as at full load."""
        return None

    def torque_of(self, rpm: float, q: float, cond: Conditions
                  ) -> Optional[Tuple[float, bool]]:
        """``(Nm, extrapolated)`` this file's own conversion gives quantity
        ``q`` at ``rpm``; None when the platform keeps no conversion."""
        return None

    # -- helpers ----------------------------------------------------------
    def engine(self, cond: Conditions) -> Tuple[float, int]:
        d = cond.displacement or self.displacement or 0.0
        c = cond.cylinders or self.cylinders or 0
        return float(d), int(c)

    def smoke_on(self, cond: Conditions) -> Tuple[str, ...]:
        if cond.smoke is not None:
            return tuple(cond.smoke)
        return tuple(k for k, _l, on in self.smoke_choices if on)

    def air(self, p_hpa: float, cond: Conditions) -> float:
        d, c = self.engine(cond)
        return air_mass(p_hpa, d, c, cond.ve, cond.charge_temp)

    def blockers(self, cond: Conditions) -> List[str]:
        """What stops the calculation: tables the definition lacks, and the
        engine's size when neither the file nor ``cond`` gives it."""
        out = list(self.missing)
        d, c = self.engine(cond)
        if not c:
            out.append("the cylinder count (enter it under Engine)")
        if not d and (self.smoke_choices or not self.has_conversion):
            out.append("the engine's displacement (enter it under Engine)")
        return out


def binding(limits: Dict[str, float]) -> Tuple[str, float]:
    """The lowest limit, ``(key, q)``; the first wins a tie, so give the
    limits in the order they should be named."""
    key, val = None, None
    for k, v in limits.items():
        if v is None:
            continue
        if val is None or v < val - 1e-9:
            key, val = k, v
    return key or "", max(0.0, val or 0.0)


# ------------------------------------------------------------- fuel map
class FuelMap:
    """This file's quantity as the reference would have to inject it for the
    same delivery: the duration this file gives (its own SOI, its own
    duration maps) read back through the reference's duration maps at that
    same SOI. Two :class:`ovtsuite.eoi.Calculator` objects, one per file;
    steady running, fuel at its reference temperature."""

    def __init__(self, calc, ref_calc, coolant: float = 90.0):
        self.calc, self.ref, self.coolant = calc, ref_calc, coolant
        tabs = [t for t in ref_calc.dur_t if t is not None]
        self.q_top = max((t.q_axis()[-1] for t in tabs), default=0.0)
        self._span: Dict[float, Tuple[float, float]] = {}

    def ref_soi_span(self, rpm: float) -> Tuple[float, float]:
        """The starts of injection (deg BTDC) the reference itself uses at
        ``rpm`` over its quantity range: its duration maps are calibrated
        along those, so a start outside them is held to the nearest end."""
        got = self._span.get(rpm)
        if got is None:
            qs = sorted({q for t in self.ref.soi_t if t is not None
                         for q in t.q_axis()} | {0.0, self.q_top})
            vals = [self.ref.btdc(self.ref.soi(self.coolant, rpm, q)[0])
                    for q in qs if 0.0 <= q <= self.q_top]
            got = (min(vals), max(vals)) if vals else (-1e9, 1e9)
            self._span[rpm] = got
        return got

    def own(self, rpm: float, q: float) -> Tuple[float, float]:
        """``(soi, duration)`` this file uses at (rpm, q), both own units."""
        soi, _lim = self.calc.soi(self.coolant, rpm, q)
        d, _i, _m = self.calc.duration(soi, rpm, q)
        return soi, d

    def ref_duration(self, soi: float, rpm: float, q: float) -> float:
        return self.ref.duration(soi, rpm, q)[0]

    def delivered(self, rpm: float, q: float
                  ) -> Tuple[float, bool, float, float]:
        """``(quantity in the reference's terms, extrapolated, soi BTDC,
        duration)``."""
        soi, d = self.own(rpm, q)
        btdc = self.calc.btdc(soi)
        a, b = self.ref_soi_span(rpm)
        held = min(max(btdc, a), b)
        ext = abs(held - btdc) > 0.05
        # the reference's SOI sign may differ (EDC15 stores BTDC negative)
        r_soi = self.ref.native(held)
        lo, hi = 0.0, self.q_top
        d_hi = self.ref_duration(r_soi, rpm, hi)
        if d >= d_hi:
            d_lo = self.ref_duration(r_soi, rpm, 0.0)
            qr = hi if d_hi <= d_lo else hi * (d - d_lo) / (d_hi - d_lo)
            ext = ext or d > d_hi + 1e-6
        else:
            for _i in range(40):                  # bisection: monotone in q
                mid = 0.5 * (lo + hi)
                if self.ref_duration(r_soi, rpm, mid) < d:
                    lo = mid
                else:
                    hi = mid
            qr = 0.5 * (lo + hi)
        return qr, ext, btdc, d


def fuel_map(session, ref_session, group: str = "",
             coolant: float = 90.0) -> Optional[FuelMap]:
    """A :class:`FuelMap` from ``session`` to ``ref_session`` when both carry
    complete injection tables (the End of injection view's models), else
    None."""
    if session is None or ref_session is None:
        return None
    from . import eoi as _eoi

    def pick(ms):
        ms = [m for m in ms if not m.missing]
        for m in ms:
            if getattr(m, "group", "") == group:
                return m
        return ms[0] if ms else None
    a, b = pick(_eoi.models(session)), pick(_eoi.models(ref_session))
    if a is None or b is None:
        return None
    try:
        return FuelMap(_eoi.Calculator(a), _eoi.Calculator(b), coolant)
    except Exception:
        return None


# ------------------------------------------------------------ calculation
def rpm_grid(lo: float, hi: float, step: float) -> List[float]:
    step = max(10.0, float(step or 50.0))
    lo = math.ceil(lo / step) * step
    out, r = [], lo
    while r <= hi + 1e-6:
        out.append(r)
        r += step
    return out


def calculate(model: DynoModel, cond: Optional[Conditions] = None,
              reference: Optional[DynoModel] = None,
              label: str = "", fuel: Optional[FuelMap] = None) -> Curve:
    """The full-load curve of ``model``, quantity turned into torque through
    ``reference``'s conversion (its own when None; the fuel-energy model when
    neither keeps one). With ``fuel`` (and ``cond.fuel_basis`` "duration")
    the quantity is first carried into the reference's terms through the
    duration maps."""
    cond = cond or Conditions()
    if cond.fuel_basis != "duration":
        fuel = None
    why = model.blockers(cond)
    if why:
        return Curve(label or model.label, [], dict(model.limiters),
                     "", ["Not calculated -- this needs " + "; ".join(why) + "."])
    conv = reference if (reference is not None and reference.has_conversion) \
        else (model if model.has_conversion else None)
    d, cyl = model.engine(cond)
    if conv is not None:
        src = ("the reference's %s" % conv.conversion_name
               if conv is not model else "this file's own %s" % conv.conversion_name)
    else:
        src = "fuel energy at %.0f %% efficiency, %d cylinders" % (
            cond.efficiency * 100, cyl)
    keep = 1.0 - max(0.0, min(cond.loss, 90.0)) / 100.0
    lo, hi = model.rpm_range(cond)
    pts: List[Point] = []
    notes = list(model.notes)
    if not rpm_grid(lo, hi, cond.step):
        return Curve(label or model.label, [], dict(model.limiters), "",
                     notes + ["Not calculated -- no engine-speed range: the "
                              "file's maps give %.0f to %.0f rpm." % (lo, hi)])
    if conv is not None:
        conv.conversion_begin()
    for rpm in rpm_grid(lo, hi, cond.step):
        fl = model.full_load(rpm, cond)
        ext = False
        t = None
        q, q_ref, soi, dur = fl.q, None, None, None
        if fuel is not None and fl.q > 0:
            try:
                q_ref, e1, soi, dur = fuel.delivered(rpm, fl.q)
                q, ext = q_ref, e1
            except Exception:
                q_ref = None
        if conv is not None:
            r = conv.torque_of(rpm, q, cond)
            if r is not None:
                t, e2 = r
                ext = ext or e2
        if t is None:
            t = fuel_torque(q, cyl, cond.efficiency) if cyl else 0.0
        t = max(0.0, t) * keep
        pts.append(Point(rpm, fl.q, t, power_kw(t, rpm), fl.limiter,
                         dict(fl.limits), fl.boost, fl.air, fl.asked, ext,
                         q_ref, soi, dur))
    # the curve ends where the ECU stops fuelling (the cut-off)
    while len(pts) > 2 and pts[-1].q <= 0.05 and pts[-2].q <= 0.05:
        pts.pop()
    if pts and max(p.q for p in pts) <= 0.05:
        lim = max(pts, key=lambda p: 0).limiter
        return Curve(label or model.label, [], dict(model.limiters), "",
                     notes + ["Not calculated -- every limit reads no fuel; %s "
                              "is zero across the range." % (
                                  model.limiters[lim].label if lim in model.limiters
                                  else lim)])
    if fuel is not None:
        src = "duration maps, then " + src
    if conv is not None:
        notes.extend(conv.conversion_notes())
    if any(p.extrapolated for p in pts):
        notes.append("Past the reference's last breakpoint (duration or "
                     "torque-to-fuel map) the value is extended in "
                     "proportion.")
    if cond.loss:
        src += "; less %.0f %% drivetrain loss (at the wheels)" % cond.loss
    return Curve(label or model.label, pts, dict(model.limiters), src, notes)


def demand_fraction(model: DynoModel, rpm: float, pedal: float,
                    cond: Optional[Conditions] = None) -> Optional[float]:
    """How much of what the engine delivers at full load the driver's wish
    asks for at ``pedal`` % and ``rpm`` (1.0 = full load: no more pedal adds
    torque there); None when the model has no driver's wish."""
    cond = cond or Conditions()
    d = model.demand(rpm, pedal, cond)
    if d is None:
        return None
    full = model.full_load(rpm, cond).q
    if full <= 0:
        return None
    return max(0.0, min(1.0, d / full))


def matching(models: Sequence[DynoModel], group: str) -> Optional[DynoModel]:
    """The model of ``group`` among ``models`` (a reference file's), else the
    first; None when there are none."""
    for m in models:
        if m.group == group:
            return m
    return models[0] if models else None


# ----------------------------------------------------------------- lookup
def models(session) -> List[DynoModel]:
    """The dyno models the installed module gives for ``session`` (one per
    dataset), or [] when it has none."""
    if session is None:
        return []
    from .modules import registry as _registry
    m = _registry.for_platform(getattr(session, "platform", "") or "")
    if m is None or not getattr(m, "dyno", None) or \
            getattr(m, "variant", "full") != "full":
        return []
    try:
        out = list(m.call("dyno", session) or [])
    except Exception as ex:             # (the chart says why, not "no module")
        out, why = [], str(ex) or type(ex).__name__
    else:
        why = ""
    try:
        session.__dict__["_dyno_error"] = why
    except AttributeError:
        pass
    return out


def models_error(session) -> str:
    """Why :func:`models` gave nothing for ``session`` when the module
    failed on it ("" when it did not)."""
    return getattr(session, "__dict__", {}).get("_dyno_error", "")


# ------------------------------------------------------------- references
#: reference choices (``ReferenceChoice.kind``)
REF_ORIGINAL, REF_COMPARISON, REF_VERSION, REF_NONE = (
    "original", "comparison", "version", "none")
#: a run the dyno window added (``ovtsuite.dyno_sources``): another tab,
#: a project's branch or version, a file, a pinned curve
REF_SOURCE = "source"


def original_bytes(session) -> Optional[bytes]:
    """The binary as first opened -- in a project tab the set as read --
    reconstructed from the version history; None when it can't be."""
    from . import history as _history
    try:
        return _history.bytes_at_version(session, _history.ORIGINAL)
    except Exception:
        return None


def version_bytes(session, version_id: str) -> Optional[bytes]:
    from . import history as _history
    try:
        return _history.bytes_at_version(session, version_id)
    except Exception:
        return None


def session_of(data: bytes, like):
    """``data`` as a session sharing ``like``'s definition (a version of the
    same file), else loaded in full."""
    from .patch import _session_from_bytes
    return _session_from_bytes(bytes(data), like)


# ----------------------------------------------------------------- export
def to_csv(curves: Sequence[Curve], power_unit: str = DEFAULT_POWER,
           torque_unit: str = DEFAULT_TORQUE) -> str:
    """The curves as CSV, one row per engine speed of the first curve."""
    import csv
    import io
    pl, tl = power_label(power_unit), torque_label(torque_unit)
    head = ["rpm"]
    for c in curves:
        head += ["%s power (%s)" % (c.label, pl), "%s torque (%s)" % (c.label, tl),
                 "%s quantity (mg/stroke)" % c.label, "%s limited by" % c.label]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    rpms = sorted({p.rpm for c in curves for p in c.points})
    idx = [{p.rpm: p for p in c.points} for c in curves]
    for r in rpms:
        row = ["%g" % r]
        for c, ix in zip(curves, idx):
            p = ix.get(r)
            if p is None:
                row += ["", "", "", ""]
                continue
            lim = c.limiters.get(p.limiter)
            row += ["%.1f" % power_in(p.power, power_unit),
                    "%.1f" % torque_in(p.torque, torque_unit),
                    "%.2f" % p.q, lim.label if lim else p.limiter]
        w.writerow(row)
    return buf.getvalue()


__all__ = ["POWER_UNITS", "TORQUE_UNITS", "DEFAULT_POWER", "DEFAULT_TORQUE",
           "power_label", "torque_label", "power_in", "torque_in", "power_kw",
           "fuel_torque", "fuel_q", "air_mass", "LHV_DIESEL", "AFR_STOICH",
           "Conditions", "Limiter", "FullLoad", "Point", "Curve", "DynoModel",
           "binding", "rpm_grid", "calculate", "demand_fraction", "FuelMap", "fuel_map", "matching", "models",
           "original_bytes", "version_bytes", "session_of", "to_csv",
           "REF_ORIGINAL", "REF_COMPARISON", "REF_VERSION", "REF_NONE", "REF_SOURCE",
           "Table", "is_rpm_axis"]

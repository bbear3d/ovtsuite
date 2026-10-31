# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""End of injection (EOI) on unit-injector (PD) diesels.

A PD ECU does not store an end of injection. It works one out from two sets of
tables, each a *table set* (Bosch "Kennraum"): several maps on the same inputs,
one of which -- or a blend of two -- is picked by a selector curve whose output
is a table number with a fraction (8.8 fixed point; ``Map_Select`` = raw/256).
The integer part n picks table n, the fraction f blends table n+1 in:
``value = T[n] + (T[n+1] - T[n]) * f``; with f = 0 only table n is read.

* **Start of injection** -- the SOI set, picked by coolant temperature.
* **Duration** -- the duration set (crank degrees of delivery), picked by the
  SOI itself, because the cam's plunger speed differs with the angle the
  delivery starts at.

  EOI (deg ATDC) = duration - SOI (deg BTDC); negative = before TDC. That is
  the convention Tune from logs uses.

Both platforms were checked in disassembly (EDC15P 038906019GQ, EDC16
03G906016KN); docs/EOI.md has the evidence. Which tables form the sets is
platform knowledge, so the platform's module supplies it through the
``injection`` hook as :class:`InjectionModel` objects; everything here is
generic. No Qt: the GUI (``gui_eoi``) and tests use it alike.

SOI corrections that are table sets of their own over another operating
condition -- intake air temperature, atmospheric pressure -- are added to the
base set when the platform supplies them (:class:`Correction`), at the
conditions set on the :class:`Calculator` (default: 20 °C air, sea level);
the limiter applies after them, as the ECU does. A set the ECU uses *instead
of* the base set in some running state (EDC16 dynamic SOI while smoke-limited,
gear-dependent base maps) is a :class:`SoiSet` the view can switch to. Every
set is blended the same way -- table n plus the fraction of n+1, the
fraction being the low byte of the selector's 8.8 output (038906019GQ
0x3961A-0x39692 with the internal-ROM MUL/DIVL helper 10F2H; 03G906016KN set
routine 0x5E6FC) -- not an equal average of two tables. What is *not*
modelled: the start-phase terms, the fuel-temperature quantity correction, and
the camshaft-offset term of the selector input (zero on every reference
file). The numbers are those of steady running with fuel at its reference
temperature, and the GUI says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .logtune.grid import position

#: axis units that mean engine speed (definitions use either spelling)
_RPM_UNITS = ("rpm", "1/min", "u/min", "min-1", "min^-1")


def is_rpm_axis(ax) -> bool:
    u = (getattr(ax, "units", "") or "").strip().lower()
    return any(k in u for k in _RPM_UNITS)


# ------------------------------------------------------------------ tables
class Table:
    """One map or curve, its axes and cells read once (refresh to re-read).

    Lookups clamp at the axis ends and interpolate linearly in between, as the
    ECU does (:func:`ovtsuite.logtune.grid.position`). A map's engine-speed
    axis is found by its units, so a table stored rpm x mg or mg x rpm reads
    the same way through :meth:`at`.
    """

    def __init__(self, entity):
        self.e = entity
        self.refresh()

    def refresh(self) -> None:
        e = self.e
        self.xs = e.x_values() if e.x is not None else [0.0]
        self.ys = e.y_values() if (e.kind == "map" and e.y is not None) else []
        self.vals = e.values()
        self.rpm_on_x = e.kind != "map" or is_rpm_axis(e.x) or not is_rpm_axis(e.y)

    # -- geometry -------------------------------------------------------
    @property
    def is_curve(self) -> bool:
        return not self.ys

    def rpm_axis(self) -> List[float]:
        return list(self.xs if self.rpm_on_x else self.ys)

    def q_axis(self) -> List[float]:
        return list(self.ys if self.rpm_on_x else self.xs)

    def point(self, r: int, c: int) -> Tuple[float, float]:
        """``(rpm, q)`` of cell (row, col)."""
        x, y = self.xs[c], (self.ys[r] if self.ys else 0.0)
        return (x, y) if self.rpm_on_x else (y, x)

    def cell_of(self, i_rpm: int, i_q: int) -> Tuple[int, int]:
        """(row, col) of breakpoint (rpm index, quantity index)."""
        return (i_q, i_rpm) if self.rpm_on_x else (i_rpm, i_q)

    # -- lookups ----------------------------------------------------------
    def weights(self, rpm: float, q: float = 0.0,
                ) -> List[Tuple[int, int, float]]:
        """``[(row, col, weight)]`` the lookup at (rpm, q) blends."""
        x, y = (rpm, q) if self.rpm_on_x else (q, rpm)
        c0, c1, fx = position(self.xs, x)
        if self.is_curve:
            out = [(0, c0, 1.0 - fx)] + ([(0, c1, fx)] if c1 != c0 else [])
            return [w for w in out if w[2] > 1e-12]
        r0, r1, fy = position(self.ys, y)
        acc: Dict[Tuple[int, int], float] = {}
        for r, wy in ((r0, 1.0 - fy), (r1, fy)):
            for c, wx in ((c0, 1.0 - fx), (c1, fx)):
                acc[(r, c)] = acc.get((r, c), 0.0) + wy * wx
        return [(r, c, w) for (r, c), w in acc.items() if w > 1e-12]

    def at(self, rpm: float, q: float = 0.0, vals=None) -> float:
        v = vals if vals is not None else self.vals
        return sum(v[r][c] * w for r, c, w in self.weights(rpm, q))

    def curve(self, x: float) -> float:
        """A curve's value at ``x`` (its only input)."""
        c0, c1, f = position(self.xs, x)
        row = self.vals[0]
        return row[c0] + (row[c1] - row[c0]) * f


@dataclass
class TableSet:
    """Tables picked by a selector curve whose output is a table number.

    ``tables`` is in table-number order; ``max_index`` is the highest number
    the ECU reads (default: the last table). A selector value above it is
    clamped here for the arithmetic -- :func:`selector_problems` reports it,
    because on EDC15 the ECU does NOT clamp and reads past its pointer list.
    """
    tables: List[object]
    selector: Optional[object] = None
    label: str = ""
    max_index: Optional[float] = None

    @property
    def last(self) -> float:
        return float(self.max_index if self.max_index is not None
                     else len(self.tables) - 1)


def members(idx: float, count: int) -> List[Tuple[int, float]]:
    """``[(table number, weight)]`` for a selector output ``idx``."""
    idx = max(0.0, float(idx))
    n = int(math.floor(idx + 1e-9))
    f = idx - n
    if n >= count - 1:
        return [(count - 1, 1.0)]
    if f <= 1e-9:
        return [(n, 1.0)]
    return [(n, 1.0 - f), (n + 1, f)]


#: Operating conditions a :class:`Correction` can depend on, besides coolant:
#: key -> (label, units, default, low, high). The defaults are steady running
#: at sea level on a mild day.
CONDITIONS: Dict[str, Tuple[str, str, Optional[float], float, float]] = {
    "air": ("Intake air", "°C", 20.0, -40.0, 120.0),
    "baro": ("Air pressure", "hPa", 1013.0, 500.0, 1100.0),
    "q_start": ("Start quantity", "mg/stroke", 40.0, 0.0, 150.0),
    #: None: at the ECU's own reference temperature (no correction)
    "fuel": ("Fuel temperature", "°C", None, -40.0, 120.0),
}


@dataclass
class FuelCorrection:
    """The fuel-temperature correction of the quantity the duration maps
    read. The pump maps were measured at a reference fuel temperature
    (``ref``: engine speed x quantity -> deg C); away from it the quantity is
    corrected by ``rate`` (mg per 100 K, same inputs) times ``factor`` (a
    curve over fuel - reference, in K) / 100. EDC15 (038906019GQ 0x46C58):
    zmwMKBT_KF / zmwMKOR_KF / zmwMKOR_KL, the result held to 0..zmwMKOR_Mx;
    EDC16 (03G906016KN 0x62A70): InjUn_tFlRef_MAP / InjUn_QntyPerTemp_MAP /
    InjUn_tFlDiffCor_CUR. While starting the ECU uses coolant instead of fuel
    temperature when ``start_coolant`` (cowFUN_MEK / InjUn_swtTempSel_C)."""
    ref: object
    rate: object
    factor: object
    q_max: Optional[float] = None
    start_coolant: bool = False


@dataclass
class Correction:
    """An SOI term the ECU adds to the base set before the limiter: a table
    set on the SOI tables' inputs, in the SOI tables' own units and sign,
    picked by a selector curve over the operating condition ``cond`` (a
    :data:`CONDITIONS` key) -- EDC15 ``fnwSWAD*KR`` by atmospheric pressure,
    ``fnwSWLT*KR`` by air temperature."""
    cond: str
    set: TableSet


@dataclass
class SoiSet:
    """Another start-of-injection table set the ECU uses *instead of* the
    base set in some running state -- EDC16 ``InjCrv_phiDyn0..4_GMAP`` while
    the smoke limit is the binding torque limit, or the gear-dependent base
    maps ``InjCrv_phiBasGear12/34/56_MAP`` when warm. The corrections and the
    limiter apply to it as to the base set. ``when`` says, in plain words,
    when the ECU reads it."""
    key: str
    label: str
    set: TableSet
    when: str = ""
    #: what picks the table number: "" = coolant, else a CONDITIONS key
    #: (the start sets: atmospheric pressure)
    selector_cond: str = ""
    #: the tables' second input: "q" (quantity) or "coolant" (the start
    #: sets are engine speed x coolant); with "coolant" the grid runs over
    #: coolant and the quantity comes from ``quantity`` or the
    #: ``q_start`` condition
    second: str = "q"
    #: whether the corrections are added (not while starting)
    corrections: bool = True
    #: a map of the quantity on (engine speed, coolant) for a "coolant" set
    #: (EDC15 start-quantity base map); None: the ``q_start`` condition
    quantity: Optional[object] = None


@dataclass
class InjectionModel:
    """What a platform module tells the base app about one dataset.

    ``soi.tables`` and ``duration.tables`` hold the definition's entities.
    The duration selector's input is the SOI in the SOI tables' own units.
    ``soi_btdc`` is +1 when those units are degrees BTDC (EDC16) and -1 when
    the table stores degrees with negative = BTDC (EDC15's FB). ``limiter``,
    when given, is a map of the latest-allowed advance, BTDC-signed like the
    SOI, on engine speed x coolant (EDC16 ``InjCrv_phiMIMax_MAP``).
    """
    platform: str
    soi: TableSet
    duration: TableSet
    soi_btdc: float = 1.0
    group: str = ""
    limiter: Optional[object] = None
    coolant_warm: float = 90.0
    #: plain-English notes the view shows under the grid (what is assumed)
    notes: Tuple[str, ...] = ()
    #: measuring-block / RAM channels a log needs for EOI (for the logger)
    log_channels: Tuple[str, ...] = ()
    #: tables the platform needs but this definition does not place: the view
    #: shows them instead of a result (never a guess)
    missing: Tuple[str, ...] = ()
    #: SOI terms added before the limiter (see :class:`Correction`)
    corrections: Tuple[Correction, ...] = ()
    #: sets that replace the base SOI set in some running state (see
    #: :class:`SoiSet`); the base set is ``soi``
    soi_sets: Tuple[SoiSet, ...] = ()
    #: the fuel-temperature correction of the duration maps' quantity
    fuel: Optional[FuelCorrection] = None

    def soi_set(self, key: Optional[str]) -> TableSet:
        """The SOI table set ``key`` names (None / "base": the base set)."""
        if not key or key == "base":
            return self.soi
        for s in self.soi_sets:
            if s.key == key:
                return s.set
        raise KeyError("no SOI set %r" % key)

    @property
    def label(self) -> str:
        return self.group or self.platform.upper()


# ------------------------------------------------------------ calculation
@dataclass
class Cell:
    rpm: float
    q: float
    soi: float                         # in the SOI tables' own units
    soi_members: List[Tuple[int, float]]
    soi_limited: bool
    index: float                       # duration selector output
    dur_members: List[Tuple[int, float]]
    duration: float                    # crank degrees
    eoi: float                         # deg ATDC (negative = BTDC)
    soi_btdc: float = 0.0              # the SOI as degrees BTDC
    soi_cor: float = 0.0               # of the SOI: the corrections, deg (own units)
    q_dur: Optional[float] = None      # quantity the duration maps read (fuel-corrected)


@dataclass
class Grid:
    """EOI over the SOI table's own breakpoints: ``cells[i_rpm][i_q]``."""
    coolant: float
    rpm: List[float]
    q: List[float]
    cells: List[List[Cell]]
    ref: Table                         # the SOI table the axes came from


class Calculator:
    """Works out SOI, duration and EOI for one :class:`InjectionModel`.

    ``conditions`` holds the operating conditions the model's corrections are
    read at (:data:`CONDITIONS` keys; defaults filled in). ``soi_set`` picks
    the SOI table set (:attr:`InjectionModel.soi_sets` key; default the base
    set): everything below -- grid, plans, solver -- works on that set."""

    def __init__(self, model: InjectionModel,
                 conditions: Optional[Dict[str, float]] = None,
                 soi_set: Optional[str] = None):
        self.model = model
        self.soi_key = soi_set or "base"
        self.soi_ts = model.soi_set(soi_set)
        self.sset = next((x for x in model.soi_sets if x.key == self.soi_key), None)
        self.q_map = (Table(self.sset.quantity)
                      if self.sset is not None and self.sset.quantity is not None else None)
        self.soi_t = [Table(e) if e is not None else None for e in self.soi_ts.tables]
        self.dur_t = [Table(e) if e is not None else None for e in model.duration.tables]
        self.soi_sel = Table(self.soi_ts.selector) if self.soi_ts.selector is not None else None
        self.dur_sel = Table(model.duration.selector)
        self.lim = Table(model.limiter) if model.limiter is not None else None
        self.cor = [(c, [Table(e) for e in c.set.tables],
                     Table(c.set.selector) if c.set.selector is not None else None)
                    for c in model.corrections]
        self.conditions = {k: v[2] for k, v in CONDITIONS.items()}
        self.conditions.update(conditions or {})
        fc = model.fuel
        self.fuel_t = None if fc is None else \
            (Table(fc.ref), Table(fc.rate), Table(fc.factor))

    def refresh(self) -> None:
        """Re-read every table (after an edit or an undo)."""
        extra = [t for _c, ts, sel in self.cor for t in ts + [sel]] + \
            list(self.fuel_t or ())
        for t in self.soi_t + self.dur_t + [self.soi_sel, self.dur_sel, self.lim] + extra:
            if t is not None:
                t.refresh()

    @property
    def conds(self) -> List[str]:
        """The conditions this model's corrections depend on, in order."""
        out: List[str] = []
        for c, _ts, _sel in self.cor:
            if c.cond not in out:
                out.append(c.cond)
        return out

    # -- what the grid's second axis means ----------------------------------
    @property
    def by_coolant(self) -> bool:
        """The SOI set's second input is coolant (a start set): the grid runs
        over engine speed x coolant at the start quantity."""
        return self.sset is not None and self.sset.second == "coolant"

    def clt(self, coolant: float, x2: float) -> float:
        """The coolant at grid point (., x2)."""
        return x2 if self.by_coolant else coolant

    def fuel_q(self, rpm: float, x2: float) -> float:
        """The injected quantity at grid point (rpm, x2)."""
        if not self.by_coolant:
            return x2
        if self.q_map is not None:
            return self.q_map.at(rpm, x2)
        return self.conditions.get("q_start", CONDITIONS["q_start"][2])

    def fuel_correction(self, coolant: float, rpm: float, x2: float) -> float:
        """mg the ECU adds to the quantity before the duration maps, at grid
        point (rpm, x2): 0 without a model or at the reference temperature."""
        if self.fuel_t is None:
            return 0.0
        fc = self.model.fuel
        if self.by_coolant and fc.start_coolant:
            temp = self.clt(coolant, x2)             # starting: coolant
        else:
            temp = self.conditions.get("fuel")
        if temp is None:
            return 0.0
        q = self.fuel_q(rpm, x2)
        ref, rate, fac = self.fuel_t
        return rate.at(rpm, q) * fac.curve(temp - ref.at(rpm, q)) / 100.0

    def dur_q(self, coolant: float, rpm: float, x2: float) -> float:
        """The quantity the duration maps read at grid point (rpm, x2): the
        injected quantity plus the fuel-temperature correction, held to
        0..q_max as the ECU does."""
        q = self.fuel_q(rpm, x2) + self.fuel_correction(coolant, rpm, x2)
        hi = self.model.fuel.q_max if self.model.fuel is not None else None
        q = max(q, 0.0)
        return min(q, hi) if hi is not None else q

    @property
    def uses_corrections(self) -> bool:
        return bool(self.cor) and (self.sset is None or self.sset.corrections)

    @property
    def conditions_used(self) -> List[str]:
        """The CONDITIONS keys this calculation reads."""
        out = list(self.conds) if self.uses_corrections else []
        if self.sset is not None and self.sset.selector_cond:
            out.append(self.sset.selector_cond)
        if self.by_coolant and self.q_map is None:
            out.append("q_start")
        if self.fuel_t is not None and not (self.by_coolant and self.model.fuel.start_coolant):
            out.append("fuel")
        return [k for i, k in enumerate(out) if k not in out[:i]]

    # -- sign -------------------------------------------------------------
    def btdc(self, soi: float) -> float:
        return self.model.soi_btdc * soi

    def native(self, btdc: float) -> float:
        return self.model.soi_btdc * btdc

    # -- sets -------------------------------------------------------------
    def soi_members(self, coolant: float) -> List[Tuple[int, float]]:
        n = len(self.soi_t)
        if self.soi_sel is None:
            return [(n - 1, 1.0)]
        x = coolant
        if self.sset is not None and self.sset.selector_cond:
            k = self.sset.selector_cond
            x = self.conditions.get(k, CONDITIONS[k][2])
        idx = min(self.soi_sel.curve(x), self.soi_ts.last)
        out = members(idx, n)
        if any(self.soi_t[i] is None for i, _ in out):
            raise ValueError("SOI table %d is not in this definition"
                             % next(i for i, _ in out if self.soi_t[i] is None))
        return out

    def correction_members(self, k: int) -> List[Tuple[int, float]]:
        """Tables of correction ``k`` at the current conditions."""
        c, ts, sel = self.cor[k]
        if sel is None:
            return [(len(ts) - 1, 1.0)]
        x = self.conditions.get(c.cond, CONDITIONS.get(c.cond, ("", "", 0.0))[2])
        return members(min(sel.curve(x), c.set.last), len(ts))

    def correction(self, rpm: float, q: float) -> float:
        """Sum of the corrections at (rpm, q) and the current conditions
        (own units)."""
        tot = 0.0
        for k, (_c, ts, _sel) in enumerate(self.cor):
            tot += sum(ts[i].at(rpm, q) * w for i, w in self.correction_members(k))
        return tot

    def soi(self, coolant: float, rpm: float, q: float,
            mem=None, limit: bool = True) -> Tuple[float, bool]:
        """SOI (own units) at a point: the base set, plus the corrections,
        then the limiter. ``(value, limited)``."""
        mem = mem if mem is not None else self.soi_members(coolant)
        s = sum(self.soi_t[i].at(rpm, q) * w for i, w in mem)
        if self.uses_corrections:
            s += self.correction(rpm, self.fuel_q(rpm, q))
        if limit and self.lim is not None:
            cap = self.cap(coolant, rpm, q)
            if self.btdc(s) > self.btdc(cap):
                return cap, True
        return s, False

    def cap(self, coolant: float, rpm: float, x2: float) -> float:
        """The limiter's SOI (own units) at grid point (rpm, x2)."""
        return self.lim.at(rpm, self.clt(coolant, x2))   # rpm x coolant

    def index(self, soi: float) -> float:
        return min(max(self.dur_sel.curve(soi), 0.0), self.model.duration.last)

    def duration(self, soi: float, rpm: float, q: float,
                 overrides: Optional[Dict[int, list]] = None):
        """``(duration, selector output, members)`` at SOI ``soi``."""
        idx = self.index(soi)
        mem = members(idx, len(self.dur_t))
        d = 0.0
        for i, w in mem:
            t = self.dur_t[i]
            if t is None:
                raise ValueError("duration table %d is not in this definition" % i)
            d += w * t.at(rpm, q, (overrides or {}).get(i))
        return d, idx, mem

    def eoi(self, soi: float, duration: float) -> float:
        return duration - self.btdc(soi)

    def cell(self, coolant: float, rpm: float, q: float, mem=None) -> Cell:
        sm = mem if mem is not None else self.soi_members(coolant)
        s, lim = self.soi(coolant, rpm, q, sm)
        fq = self.fuel_q(rpm, q)
        qd = self.dur_q(coolant, rpm, q)
        d, idx, dm = self.duration(s, rpm, qd)
        return Cell(rpm, q, s, sm, lim, idx, dm, d, self.eoi(s, d),
                    self.btdc(s), self.correction(rpm, fq) if self.uses_corrections else 0.0,
                    qd)

    def reference(self, coolant: float) -> Table:
        """The SOI table whose breakpoints the grid uses: the one with most
        weight at ``coolant`` (the set's tables share their axes)."""
        mem = self.soi_members(coolant)
        return self.soi_t[max(mem, key=lambda m: m[1])[0]]

    def grid(self, coolant: float) -> Grid:
        ref = self.reference(coolant)
        mem = self.soi_members(coolant)
        rpm, q = ref.rpm_axis(), ref.q_axis()
        cells = [[self.cell(coolant, n, m, mem) for m in q] for n in rpm]
        return Grid(coolant, rpm, q, cells, ref)

    # -- one duration table on its own axes --------------------------------
    def usage(self, k: int, coolant: float) -> List[List[float]]:
        """Weight with which the ECU reads duration table ``k`` at each of its
        own cells, at ``coolant`` (0 = never used there). Same shape as the
        table's cells."""
        t = self.dur_t[k]
        mem = self.soi_members(coolant)
        out = [[0.0] * len(t.vals[0]) for _ in t.vals]
        for r in range(len(t.vals)):
            for c in range(len(t.vals[0])):
                rpm, q = t.point(r, c)
                if self.by_coolant:
                    # a start set: only the cells around the start quantity
                    if not any((rr, cc) == (r, c) for rr, cc, _w in
                               t.weights(rpm, self.dur_q(coolant, rpm, coolant))):
                        continue
                    s, _ = self.soi(coolant, rpm, coolant, mem)
                else:
                    s, _ = self.soi(coolant, rpm, q, mem)
                for i, w in members(self.index(s), len(self.dur_t)):
                    if i == k:
                        out[r][c] = w
        return out

    # -- solving ------------------------------------------------------------
    def solve_soi(self, coolant: float, rpm: float, q: float,
                  target_eoi: float, lo_btdc: float, hi_btdc: float,
                  ) -> Optional[float]:
        """The SOI (own units) that gives ``target_eoi`` at (rpm, q), keeping
        the duration selector's logic, or None if no SOI in
        [lo_btdc, hi_btdc] reaches it. EOI falls as SOI advances (the
        duration's change with SOI is far below one degree per degree), so
        bisection on the BTDC value is enough."""
        fq = self.dur_q(coolant, rpm, q)

        def f(b):
            s = self.native(b)
            d, _, _ = self.duration(s, rpm, fq)
            return d - b - target_eoi
        flo, fhi = f(lo_btdc), f(hi_btdc)
        if flo == 0:
            return self.native(lo_btdc)
        if fhi == 0:
            return self.native(hi_btdc)
        if (flo > 0) == (fhi > 0):
            return None
        a, b = lo_btdc, hi_btdc
        for _ in range(60):
            m = 0.5 * (a + b)
            fm = f(m)
            if (fm > 0) == (flo > 0):
                a, flo = m, fm
            else:
                b = m
            if abs(b - a) < 1e-4:
                break
        return self.native(0.5 * (a + b))


def advance_for_target(calc: "Calculator", rpm: float, q: float,
                       soi_btdc: float, duration: float, target: float,
                       span: float = 20.0, coolant: float = 90.0) -> Optional[float]:
    """How many degrees to advance a LOGGED start of injection so the end of
    injection reaches ``target`` (deg ATDC), with the ECU's own choice of
    duration table at the new SOI.

    The logged duration already holds what the model leaves out (fuel
    temperature, pilot split), so only the model's CHANGE of duration with
    SOI is applied to it: d(s) = duration + D(s) - D(soi). Returns the
    advance in degrees (negative = retard), or None if no SOI within
    ``span`` degrees reaches the target. The duration maps are read at the
    quantity the ECU corrects for the fuel temperature in ``calc``'s
    conditions (none set: the reference temperature)."""
    q = calc.dur_q(coolant, rpm, q)
    d0, _, _ = calc.duration(calc.native(soi_btdc), rpm, q)

    def eoi_at(b):
        d, _, _ = calc.duration(calc.native(b), rpm, q)
        return duration + (d - d0) - b

    lo, hi = soi_btdc - span, soi_btdc + span
    flo, fhi = eoi_at(lo) - target, eoi_at(hi) - target
    if (flo > 0) == (fhi > 0):
        return None
    for _ in range(50):
        m = 0.5 * (lo + hi)
        fm = eoi_at(m) - target
        if (fm > 0) == (flo > 0):
            lo, flo = m, fm
        else:
            hi = m
        if hi - lo < 1e-3:
            break
    return 0.5 * (lo + hi) - soi_btdc


# ----------------------------------------------------------------- plans
@dataclass
class Plan:
    """Cell writes for one action, per entity: ``(row, col) -> value``.
    (Entities are compared by value, so they are keyed by identity.)"""
    label: str
    limited: List[Tuple[float, float]] = field(default_factory=list)
    unreached: List[Tuple[float, float]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    _w: Dict[int, Tuple[object, Dict[Tuple[int, int], float]]] = field(
        default_factory=dict, repr=False)

    def put(self, e, r: int, c: int, v: float) -> None:
        self._w.setdefault(id(e), (e, {}))[1][(r, c)] = v

    def items(self) -> List[Tuple[object, Dict[Tuple[int, int], float]]]:
        """``[(entity, {(row, col): value})]`` in the order first written."""
        return list(self._w.values())

    def cells_of(self, e) -> Dict[Tuple[int, int], float]:
        hit = self._w.get(id(e))
        return hit[1] if hit else {}

    @property
    def entities(self) -> List[object]:
        return [e for e, _ in self._w.values()]

    @property
    def n_cells(self) -> int:
        return sum(len(v) for _, v in self._w.values())


def plan_target_eoi(calc: Calculator, coolant: float,
                    target: Callable[[float, float], Optional[float]],
                    tables: Optional[Sequence[int]] = None,
                    span_btdc: float = 30.0) -> Plan:
    """Move SOI so EOI meets ``target(rpm, q)`` (deg ATDC; None = leave the
    cell) at ``coolant``.

    Every SOI-grid point gets the SOI that gives the target with the ECU's own
    duration selection, so the fuelling stays calibrated. The change is added
    to the same cell of each table in ``tables`` (default: those the coolant
    selects), which moves the blended SOI by exactly that much. A point the
    limiter caps is set to the limit and listed in ``limited``."""
    mem = calc.soi_members(coolant)
    use = list(tables) if tables else [i for i, w in mem if w > 0]
    ref = calc.reference(coolant)
    plan = Plan("End of injection: SOI for target EOI at %.0f °C" % coolant)
    lo_t, hi_t = ref.e.value_range
    lo_b, hi_b = sorted((calc.btdc(lo_t), calc.btdc(hi_t)))
    for i_n, rpm in enumerate(ref.rpm_axis()):
        for i_q, q in enumerate(ref.q_axis()):
            t = target(rpm, q)
            if t is None:
                continue
            cur, _ = calc.soi(coolant, rpm, q, mem, limit=False)
            b0 = calc.btdc(cur)
            want = calc.solve_soi(coolant, rpm, q, t,
                                  max(lo_b, b0 - span_btdc), min(hi_b, b0 + span_btdc))
            if want is None:
                plan.unreached.append((rpm, q))
                continue
            if calc.lim is not None:
                cap = calc.cap(coolant, rpm, q)
                if calc.btdc(want) > calc.btdc(cap):
                    want = cap
                    plan.limited.append((rpm, q))
            delta = want - cur
            r, c = ref.cell_of(i_n, i_q)
            for i in use:
                tb = calc.soi_t[i]
                if tb is None:
                    continue
                plan.put(tb.e, r, c, tb.vals[r][c] + delta)
    return plan


def plan_soi(calc: Calculator, coolant: float,
             points: Iterable[Tuple[float, float, float]],
             tables: Optional[Sequence[int]] = None) -> Plan:
    """Set the SOI at SOI-grid points ``(rpm, q, degrees BTDC)`` at
    ``coolant``: the change is added to the same cell of each table in
    ``tables`` (default: those the coolant selects), so the blended SOI
    moves by exactly that much. Points off the grid are ignored."""
    mem = calc.soi_members(coolant)
    use = list(tables) if tables else [i for i, w in mem if w > 0]
    ref = calc.reference(coolant)
    rpms, qs = ref.rpm_axis(), ref.q_axis()
    plan = Plan("End of injection: SOI at %.0f °C" % coolant)
    for rpm, q, btdc in points:
        try:
            i_n, i_q = rpms.index(rpm), qs.index(q)
        except ValueError:
            continue
        cur, _ = calc.soi(coolant, rpm, q, mem, limit=False)
        delta = calc.native(btdc) - cur
        r, c = ref.cell_of(i_n, i_q)
        for i in use:
            tb = calc.soi_t[i]
            if tb is not None:
                plan.put(tb.e, r, c, tb.vals[r][c] + delta)
    return plan


def plan_table(e, cells: Iterable[Tuple[int, int, float]],
               label: str = "") -> Plan:
    """Plain cell writes to one table, as a :class:`Plan` (so they go through
    :func:`apply` with its coding-variant copies)."""
    plan = Plan(label or "End of injection: %s" % e.name)
    for r, c, v in cells:
        plan.put(e, r, c, v)
    return plan


def plan_duration(calc: Calculator, coolant: float,
                  points: Iterable[Tuple[float, float, float]]) -> Plan:
    """Make the duration the ECU uses at each ``(rpm, q)`` equal the given
    value (crank degrees), at ``coolant``.

    The change goes into every cell the ECU reads there -- in each duration
    table the selector blends, through the same bilinear weights -- so the
    ECU's duration moves by exactly the change. Where several edited points
    read one cell, that cell takes their weighted mean change."""
    mem = calc.soi_members(coolant)
    acc: Dict[int, Dict[Tuple[int, int], List[float]]] = {}
    for rpm, q, new in points:
        s, _ = calc.soi(coolant, rpm, q, mem)
        fq = calc.dur_q(coolant, rpm, q)
        cur, _, dm = calc.duration(s, rpm, fq)
        delta = new - cur
        for i, _w in dm:
            for r, c, w in calc.dur_t[i].weights(rpm, fq):
                a = acc.setdefault(i, {}).setdefault((r, c), [0.0, 0.0])
                a[0] += w * delta
                a[1] += w
    plan = Plan("End of injection: duration at %.0f °C" % coolant)
    for i, cells in acc.items():
        t = calc.dur_t[i]
        for (r, c), (sw, w) in cells.items():
            if w > 0:
                plan.put(t.e, r, c, t.vals[r][c] + sw / w)
    plan.notes.append("Duration is the fuel quantity on a unit-injector "
                      "engine: this changes how much is injected for the "
                      "same quantity request.")
    return plan


def plan_collapse(calc: Calculator, keep: int, coolant: float) -> Plan:
    """Write into duration table ``keep`` the duration the ECU uses today at
    each of its breakpoints at ``coolant``, and point the selector at
    ``keep`` everywhere. The other tables are left as they are (unused)."""
    if calc.by_coolant:
        raise ValueError("collapse works from a running SOI set, not the start set")
    t = calc.dur_t[keep]
    mem = calc.soi_members(coolant)
    plan = Plan("End of injection: one duration table (%s)" % t.e.name)
    for r in range(len(t.vals)):
        for c in range(len(t.vals[0])):
            rpm, q = t.point(r, c)
            s, _ = calc.soi(coolant, rpm, q, mem)
            d, _, _ = calc.duration(s, rpm, q)
            plan.put(t.e, r, c, d)
    sel = calc.dur_sel
    for c in range(len(sel.vals[0])):
        plan.put(sel.e, 0, c, float(keep))
    plan.notes.append("The duration tables correct the quantity for the "
                      "cam's speed at each start angle. With one table, any "
                      "SOI away from %.0f °C running (cold, altitude, a later "
                      "SOI edit) meters a different quantity for the same "
                      "request." % coolant)
    return plan


def collapse_error(calc: Calculator, plan: Plan, keep: int,
                   coolant: float) -> List[float]:
    """|duration today - duration after ``plan``| over the SOI grid at another
    ``coolant`` (crank degrees), for the preview."""
    t = calc.dur_t[keep]
    new = [row[:] for row in t.vals]
    for (r, c), v in plan.cells_of(t.e).items():
        new[r][c] = v
    ref = calc.reference(coolant)
    mem = calc.soi_members(coolant)
    out = []
    for rpm in ref.rpm_axis():
        for q in ref.q_axis():
            s, _ = calc.soi(coolant, rpm, q, mem)
            d, _, _ = calc.duration(s, rpm, q)
            out.append(abs(d - t.at(rpm, q, new)))
    return out


def selector_problems(model: InjectionModel) -> List[str]:
    """Selector values the ECU cannot use: above the last table it reads.
    (EDC15 does not clamp the duration selector and reads past its pointer
    list -- the result is garbage -- so this must be fixed, not tolerated.)"""
    out = []
    for ts in ((model.duration, model.soi) + tuple(s.set for s in model.soi_sets)
               + tuple(c.set for c in model.corrections)):
        sel = ts.selector
        if sel is None:
            continue
        top = ts.last
        bad = [v for v in sel.values()[0] if v > top + 1e-9 or v < -1e-9]
        if bad:
            out.append("%s holds %s; the ECU reads tables 0 to %g only."
                       % (sel.name, ", ".join("%g" % v for v in bad), top))
    return out


# ----------------------------------------------------------------- apply
def apply(session, plan: Plan, with_copies: bool = True) -> int:
    """Write ``plan`` as ONE undo step; returns the cells written.

    With ``with_copies``, an EDC16 coding-variant copy of a written table
    (the same table in the secondary calibration block) gets the same cells,
    so the edit holds whichever coding the ECU runs."""
    if not plan.n_cells:
        return 0
    variants = []
    if with_copies:
        try:
            variants = list(session.variant_entities())
        except Exception:
            variants = []
    n = 0
    with session.transaction(plan.label):
        for e, cells in plan.items():
            rows = [(r, c, v) for (r, c), v in sorted(cells.items())]
            pre = e.name + " (variant @"
            copies = [v for v in variants if v.name.startswith(pre)
                      and v.rows == e.rows and v.cols == e.cols]
            for target in [e] + copies:
                session.edit_cells(target, rows)
            n += len(rows)
    return n


# ----------------------------------------------------------------- lookup
def models(session) -> List[InjectionModel]:
    """The injection models the installed module gives for ``session``
    (one per dataset), or [] when it has none."""
    from .modules import registry as _registry
    m = _registry.for_platform(getattr(session, "platform", "") or "")
    if m is None or not getattr(m, "injection", None) or \
            getattr(m, "variant", "full") != "full":
        return []
    try:
        return list(m.call("injection", session) or [])
    except Exception:
        return []


def deg_to_us(deg: float, rpm: float) -> Optional[float]:
    """Crank degrees as microseconds at ``rpm`` (one revolution is 360° in
    60e6/rpm µs). None at a speed of 0 or below: no time is defined there."""
    if rpm is None or rpm <= 0:
        return None
    return deg * 1e6 / (6.0 * rpm)


def us_to_deg(us: float, rpm: float) -> Optional[float]:
    """Microseconds as crank degrees at ``rpm`` (inverse of :func:`deg_to_us`)."""
    if rpm is None or rpm <= 0:
        return None
    return us * 6.0 * rpm / 1e6


def fmt_angle(v: float, nd: int = 1) -> str:
    """``-3.5`` -> "3.5° BTDC", ``4`` -> "4.0° ATDC" (EOI is ATDC-signed)."""
    if abs(v) < 0.5 * 10 ** -nd:
        return "TDC"
    return "%.*f° %s" % (nd, abs(v), "ATDC" if v > 0 else "BTDC")


__all__ = ["Table", "TableSet", "SoiSet", "Correction", "FuelCorrection", "CONDITIONS",
           "InjectionModel", "Calculator", "Cell", "Grid",
           "Plan", "members", "plan_target_eoi", "plan_duration", "plan_soi",
           "plan_table", "advance_for_target",
           "plan_collapse", "collapse_error", "selector_problems", "apply",
           "models", "fmt_angle", "is_rpm_axis", "deg_to_us", "us_to_deg"]

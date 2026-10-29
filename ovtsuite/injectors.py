# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit-injector (PD) hardware: what a calibration is set up for, and fitting
other injectors or nozzles to it.

A PD ECU has no injector flow constant. The cam drives the plunger; the
solenoid closing starts delivery and its opening ends it, so the quantity is
turned into crank degrees of delivery by the duration set -- several maps on
(engine speed, quantity), one of them (or a blend of two) picked by a
selector curve over the start of injection, because the plunger speed depends
on where on the cam flank delivery starts (:mod:`ovtsuite.eoi`). That set,
with its selector, is the calibrated *characteristic* of one hardware set:
cam lobe + injector body + nozzle together. The injector body's solenoid has
its own electrical calibration (the BIP detection and the solenoid drive),
which the platform module names (:class:`Platform`).

What this module does, Qt-free (the window is :mod:`ovtsuite.gui_injectors`):

* :func:`characteristic` reads a characteristic out of an
  :class:`~ovtsuite.eoi.InjectionModel` -- the tables and the selector as the
  ECU reads them, in degrees BTDC whatever the platform's own sign;
* :func:`identify` ranks the presets by how closely the file matches them;
* :func:`plan_fit` works out the writes that make the file's requested
  quantity what the new hardware delivers (the duration set, the selector
  when the layouts match, the start of injection by the chosen timing
  policy, and the solenoid's calibration for a body change), with findings;
  :func:`apply_fit` writes them as one undo step.

Presets are the module's data (``Platform.presets``); an aftermarket preset
is a *derived* one -- its base nozzle's OEM characteristic with the quantity
scaled by the vendor's flow factor (``flow``). Read as an upper bound on the
flow increase (a displacement-driven element passes at most the extra hole
area: Q = Cd·A·sqrt(2Δp/ρ), and Δp falls as A grows), that asks for less fuel
than the engine gets: lean by construction, and Tune from logs brings it up.
The design and its evidence: ``claude/injector_setup_tool.md``.
"""

from __future__ import annotations

import gzip
import json
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import eoi as _eoi
from .logtune.grid import position

#: preset file schema
SCHEMA = 1

# preset kinds and confidence badges
OEM = "oem"
AFTERMARKET = "aftermarket"
USER = "user"
EXACT = "exact"
ESTIMATED = "estimated"
START = "start"

KIND_LABELS = {OEM: "Factory injectors", AFTERMARKET: "Aftermarket nozzles",
               USER: "Your presets"}
CONFIDENCE_LABELS = {EXACT: "exact (from a stock file)",
                     ESTIMATED: "estimated: refine from logs",
                     START: "starting point: refine from logs"}

#: timing policies (see :func:`plan_fit`)
KEEP_CENTRE = "centre"
KEEP_EOI = "eoi"
KEEP_SOI = "soi"
TIMING_LABELS = {
    KEEP_CENTRE: "Keep the centre of injection (recommended)",
    KEEP_EOI: "Keep the end of injection",
    KEEP_SOI: "Keep the start of injection",
}
TIMING_TIPS = {
    KEEP_CENTRE: "Moves the start of injection by half the change in duration, so "
                 "the middle of the injection stays where it was. Bosch's own "
                 "recalibrations for bigger injectors on the same engine (ATD to "
                 "ASZ, AVB to AVF) follow this most closely.",
    KEEP_EOI: "Moves the start of injection by the whole change in duration, so "
              "injection ends where it did.",
    KEEP_SOI: "Leaves the start of injection alone; with bigger injectors, "
              "injection then ends earlier.",
}

#: where :func:`compare` samples two characteristics
CMP_SOI = tuple(float(s) for s in range(0, 25, 2))
CMP_RPM = tuple(float(n) for n in range(1000, 4001, 500))
CMP_Q = tuple(float(q) for q in range(5, 51, 5))
#: identical within this (crank degrees, worst point): the same calibration
EXACT_TOL = 0.05
#: within this on average (relative): the same hardware set, calibrated a
#: little differently in places (ASZ vs ARL: 0.08 %, up to 1 deg locally)
SAME_TOL = 0.01


# ---------------------------------------------------------------- tables
@dataclass
class RawTable:
    """One duration map, normalised: ``z[i_rpm][i_q]`` in crank degrees."""
    rpm: List[float]
    q: List[float]
    z: List[List[float]]

    def at(self, rpm: float, q: float) -> float:
        i0, i1, fr = position(self.rpm, rpm)
        j0, j1, fq = position(self.q, q)
        a = self.z[i0][j0] + (self.z[i0][j1] - self.z[i0][j0]) * fq
        b = self.z[i1][j0] + (self.z[i1][j1] - self.z[i1][j0]) * fq
        return a + (b - a) * fr

    @property
    def q_max(self) -> float:
        return float(self.q[-1]) if self.q else 0.0

    def to_json(self) -> dict:
        return {"rpm": self.rpm, "q": self.q, "z": self.z}

    @classmethod
    def from_json(cls, d: dict) -> "RawTable":
        return cls([float(v) for v in d["rpm"]], [float(v) for v in d["q"]],
                   [[float(v) for v in row] for row in d["z"]])


@dataclass
class Characteristic:
    """A duration set as the ECU reads it, platform-neutral: the selector maps
    the start of injection (degrees BTDC, ascending) to a table number, and
    table n is blended with n+1 by the fraction (:func:`ovtsuite.eoi.members`).
    ``flow`` scales the quantity a derived (aftermarket) characteristic reads:
    ``duration(q) = base(q / flow)``."""
    sel_x: List[float]
    sel_y: List[float]
    tables: List[RawTable]
    max_index: Optional[float] = None
    flow: float = 1.0

    @property
    def last(self) -> float:
        return float(self.max_index if self.max_index is not None
                     else len(self.tables) - 1)

    def index(self, btdc: float) -> float:
        i0, i1, f = position(self.sel_x, btdc)
        v = self.sel_y[i0] + (self.sel_y[i1] - self.sel_y[i0]) * f
        return min(max(v, 0.0), self.last)

    def duration(self, btdc: float, rpm: float, q: float) -> float:
        qq = q / self.flow
        return sum(self.tables[i].at(rpm, qq) * w
                   for i, w in _eoi.members(self.index(btdc), len(self.tables)))

    def table_value(self, k: int, rpm: float, q: float) -> float:
        """Table ``k`` alone at (rpm, q) -- the quantity scaled by ``flow``."""
        return self.tables[k].at(rpm, q / self.flow)

    def scaled(self, flow: float) -> "Characteristic":
        return Characteristic(self.sel_x, self.sel_y, self.tables,
                              self.max_index, self.flow * flow)

    def soi_span(self) -> Tuple[float, float]:
        return (self.sel_x[0], self.sel_x[-1]) if self.sel_x else (0.0, 0.0)

    @property
    def q_max(self) -> float:
        """The highest quantity every table covers (before ``flow``)."""
        return min((t.q_max for t in self.tables), default=0.0)

    def mg_per_degree(self, btdc: float, rpm: float, q: float) -> float:
        d = self.duration(btdc, rpm, q)
        return q / d if d > 1e-6 else 0.0

    def to_json(self) -> dict:
        return {"sel_x": self.sel_x, "sel_y": self.sel_y,
                "max_index": self.max_index,
                "tables": [t.to_json() for t in self.tables]}

    @classmethod
    def from_json(cls, d: dict) -> "Characteristic":
        return cls([float(v) for v in d["sel_x"]], [float(v) for v in d["sel_y"]],
                   [RawTable.from_json(t) for t in d["tables"]],
                   d.get("max_index"))


def _table_raw(e) -> RawTable:
    t = _eoi.Table(e)
    rp, qa = t.rpm_axis(), t.q_axis()
    z = []
    for i in range(len(rp)):
        row = []
        for j in range(len(qa)):
            r, c = t.cell_of(i, j)
            row.append(float(t.vals[r][c]))
        z.append(row)
    return RawTable([float(v) for v in rp], [float(v) for v in qa], z)


def _selector(model) -> Tuple[List[float], List[float]]:
    sel = _eoi.Table(model.duration.selector)
    xs = [model.soi_btdc * float(x) for x in sel.xs]
    ys = [float(v) for v in sel.vals[0]]
    if len(xs) > 1 and xs[0] > xs[-1]:
        xs.reverse()
        ys.reverse()
    return xs, ys


def characteristic(model) -> Characteristic:
    """The characteristic ``model`` (an :class:`~ovtsuite.eoi.InjectionModel`)
    is calibrated for. Raises ValueError when a table is missing."""
    if model.duration.selector is None:
        raise ValueError("the duration selector is not in this definition")
    tabs = []
    for i, e in enumerate(model.duration.tables):
        if e is None:
            raise ValueError("duration table %d is not in this definition" % i)
        tabs.append(_table_raw(e))
    xs, ys = _selector(model)
    return Characteristic(xs, ys, tabs, model.duration.max_index)


# ---------------------------------------------------------------- compare
def compare(a: Characteristic, b: Characteristic) -> Tuple[float, float]:
    """``(mean relative, worst absolute degrees)`` difference of ``a`` against
    ``b`` over the operating range both cover (SOI 0-24° BTDC, 1000-4000 rpm,
    5-50 mg). The relative mean leaves out points under 10 mg, where a
    duration of a degree or two makes any difference look large."""
    lo = max(a.sel_x[0], b.sel_x[0]) if a.sel_x and b.sel_x else 0.0
    hi = min(a.sel_x[-1], b.sel_x[-1]) if a.sel_x and b.sel_x else 0.0
    qtop = min(a.q_max * a.flow, b.q_max * b.flow)
    rel, worst = [], 0.0
    for s in CMP_SOI:
        if s < lo - 1e-9 or s > hi + 1e-9:
            continue
        for n in CMP_RPM:
            for q in CMP_Q:
                if q > qtop + 1e-9:
                    continue
                da, db = a.duration(s, n, q), b.duration(s, n, q)
                worst = max(worst, abs(da - db))
                if q >= 10.0:
                    rel.append(abs(da - db) / max(abs(db), 1.0))
    if not rel:
        return float("inf"), float("inf")
    return sum(rel) / len(rel), worst


# ---------------------------------------------------------------- presets
@dataclass
class Snap:
    """One parameter's values as a preset carries them (raw display units):
    ``z[row][col]`` on ``x`` (columns) and ``y`` (rows)."""
    kind: str
    x: List[float] = field(default_factory=list)
    y: List[float] = field(default_factory=list)
    z: List[List[float]] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"kind": self.kind, "x": self.x, "y": self.y, "z": self.z}

    @classmethod
    def from_json(cls, d: dict) -> "Snap":
        return cls(d["kind"], [float(v) for v in d.get("x", [])],
                   [float(v) for v in d.get("y", [])],
                   [[float(v) for v in r] for r in d.get("z", [])])


def snapshot(e) -> Snap:
    xs = [float(v) for v in e.x_values()] if getattr(e, "x", None) is not None else []
    ys = [float(v) for v in e.y_values()] \
        if (e.kind == "map" and getattr(e, "y", None) is not None) else []
    return Snap(e.kind, xs, ys, [[float(v) for v in r] for r in e.values()])


@dataclass
class Preset:
    """A hardware set (cam family + injector body + nozzle).

    An OEM or user preset carries its own ``char``; a derived one names its
    ``base`` preset and a ``flow`` factor (basis ``"area"``: the vendor's
    extra hole area, an upper bound on the flow increase; ``"flow"``: a
    measured flow figure). ``electrical`` is the body's solenoid calibration
    by parameter name -- written only when the injector bodies are changed.
    ``extras`` are other hydraulic parameters of the set (pilot / post
    duration sets) written with the characteristic where both files have
    them with the same layout."""
    id: str
    name: str
    kind: str = OEM
    confidence: str = EXACT
    platform: str = ""
    cam: str = ""
    body: str = ""
    nozzle: str = ""
    listed_for: List[str] = field(default_factory=list)
    #: the injector body's OEM part numbers: VAG (038 130 073 AL ...) and
    #: Bosch (0 414 720 013 ...), the one the preset is read from first
    vag: List[str] = field(default_factory=list)
    bosch: List[str] = field(default_factory=list)
    source: str = ""
    notes: str = ""
    char: Optional[Characteristic] = None
    base: str = ""
    flow: float = 1.0
    basis: str = ""
    electrical: Dict[str, Snap] = field(default_factory=dict)
    extras: Dict[str, Snap] = field(default_factory=dict)

    @property
    def derived(self) -> bool:
        return self.char is None and bool(self.base)

    def part_numbers(self) -> str:
        """The OEM part numbers in one line ("" when none are recorded)."""
        bits = []
        if self.vag:
            bits.append("VAG " + ", ".join(self.vag))
        if self.bosch:
            bits.append("Bosch " + ", ".join(self.bosch))
        return "; ".join(bits)

    def to_json(self) -> dict:
        d = {"id": self.id, "name": self.name, "kind": self.kind,
             "confidence": self.confidence, "platform": self.platform,
             "cam": self.cam, "body": self.body, "nozzle": self.nozzle,
             "listed_for": list(self.listed_for), "vag": list(self.vag),
             "bosch": list(self.bosch), "source": self.source,
             "notes": self.notes, "base": self.base, "flow": self.flow,
             "basis": self.basis}
        if self.char is not None:
            d["char"] = self.char.to_json()
        if self.electrical:
            d["electrical"] = {k: v.to_json() for k, v in self.electrical.items()}
        if self.extras:
            d["extras"] = {k: v.to_json() for k, v in self.extras.items()}
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Preset":
        return cls(
            id=d["id"], name=d.get("name", d["id"]), kind=d.get("kind", OEM),
            confidence=d.get("confidence", EXACT), platform=d.get("platform", ""),
            cam=d.get("cam", ""), body=d.get("body", ""), nozzle=d.get("nozzle", ""),
            listed_for=list(d.get("listed_for", [])), vag=list(d.get("vag", [])),
            bosch=list(d.get("bosch", [])), source=d.get("source", ""),
            notes=d.get("notes", ""),
            char=Characteristic.from_json(d["char"]) if d.get("char") else None,
            base=d.get("base", ""), flow=float(d.get("flow", 1.0)),
            basis=d.get("basis", ""),
            electrical={k: Snap.from_json(v) for k, v in (d.get("electrical") or {}).items()},
            extras={k: Snap.from_json(v) for k, v in (d.get("extras") or {}).items()})


def load_presets(path: str) -> List[Preset]:
    """Presets from a module data file (JSON, optionally gzip)."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    return [Preset.from_json(p) for p in doc.get("presets", [])]


def save_presets(path: str, presets: Iterable[Preset]) -> None:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as fh:
        json.dump({"schema": SCHEMA, "presets": [p.to_json() for p in presets]},
                  fh, separators=(",", ":"))


class Catalogue:
    """Presets by id, derived ones resolved against their base."""

    def __init__(self, presets: Iterable[Preset] = ()):
        self.by_id: Dict[str, Preset] = {}
        for p in presets:
            self.add(p)

    def add(self, p: Preset) -> None:
        self.by_id[p.id] = p

    def __iter__(self):
        return iter(self.by_id.values())

    def __len__(self):
        return len(self.by_id)

    def get(self, pid: str) -> Optional[Preset]:
        return self.by_id.get(pid)

    def base_of(self, p: Preset) -> Optional[Preset]:
        seen = set()
        while p is not None and p.derived and p.id not in seen:
            seen.add(p.id)
            p = self.by_id.get(p.base)
        return p if p is not None and p.char is not None else None

    def char_of(self, p: Preset) -> Optional[Characteristic]:
        """``p``'s characteristic, a derived preset's scaled from its base."""
        if p.char is not None:
            return p.char.scaled(p.flow) if p.flow != 1.0 else p.char
        b = self.base_of(p)
        if b is None:
            return None
        flow = p.flow
        cur = self.by_id.get(p.base)
        while cur is not None and cur.derived:
            flow *= cur.flow
            cur = self.by_id.get(cur.base)
        return b.char.scaled(flow)

    def electrical_of(self, p: Preset) -> Dict[str, Snap]:
        """The body calibration a preset brings (a derived one: its base's)."""
        if p.electrical:
            return p.electrical
        b = self.base_of(p)
        return dict(b.electrical) if b is not None else {}

    def extras_of(self, p: Preset) -> Dict[str, Snap]:
        if p.extras:
            return p.extras
        b = self.base_of(p)
        return dict(b.extras) if b is not None else {}


@dataclass
class Match:
    preset: Preset
    mean: float
    worst: float

    @property
    def exact(self) -> bool:
        """The same calibration (every point within EXACT_TOL degrees)."""
        return self.worst <= EXACT_TOL

    @property
    def same(self) -> bool:
        """The same hardware set (within SAME_TOL on average)."""
        return self.mean <= SAME_TOL


def identify(char: Characteristic, catalogue: Catalogue,
             limit: int = 5) -> List[Match]:
    """The presets that carry their own characteristic, closest first."""
    out = []
    for p in catalogue:
        if p.char is None or p.flow != 1.0:
            continue
        mean, worst = compare(char, p.char)
        if math.isfinite(mean):
            out.append(Match(p, mean, worst))
    out.sort(key=lambda m: (m.mean, m.worst))
    return out[:limit]


@dataclass
class DatasetMatch:
    """:func:`identify` for one dataset of a file (EDC15 files can carry
    several, each with its own duration set)."""
    label: str
    model: object
    char: Optional[Characteristic] = None
    matches: List[Match] = field(default_factory=list)
    error: str = ""

    @property
    def top(self) -> Optional[Match]:
        return self.matches[0] if self.matches else None

    def verdict(self) -> Tuple[str, str]:
        """What the dataset is set up for, as two datasets are compared:
        ``("same", preset id)``, ``("closest", preset id)`` or ``("", "")``."""
        t = self.top
        if t is None:
            return ("", "")
        return ("same" if t.same else "closest", t.preset.id)


def identify_datasets(models: Sequence, catalogue: Catalogue,
                      limit: int = 5) -> List[DatasetMatch]:
    """:func:`identify` for every dataset in ``models`` (eoi models), in
    their order; a dataset that cannot be read carries its ``error``."""
    out = []
    for m in models:
        label = getattr(m, "label", "") or getattr(m, "group", "") or "Main"
        try:
            char = characteristic(m)
            out.append(DatasetMatch(label, m, char, identify(char, catalogue, limit)))
        except Exception as ex:                               # noqa: BLE001
            out.append(DatasetMatch(label, m, error=str(ex) or type(ex).__name__))
    return out


def datasets_disagree(results: Sequence[DatasetMatch]) -> Optional[str]:
    """None when every dataset is set up for the same hardware (or there is
    only one), else a sentence that says which is set up for what."""
    ok = [r for r in results if not r.error]
    if len(ok) < 2 or len({r.verdict() for r in ok}) == 1:
        return None
    parts = []
    for r in ok:
        t = r.top
        if t is None:
            parts.append("%s: no preset to compare with" % r.label)
        elif t.same:
            parts.append("%s: %s" % (r.label, t.preset.name))
        else:
            parts.append("%s: closest %s (%.0f %% apart)" % (r.label, t.preset.name,
                                                            t.mean * 100))
    return ("The datasets are not set up for the same hardware (%s). Fit each "
            "dataset on its own, or check which one the car runs." % "; ".join(parts))


# ---------------------------------------------------------------- platform
@dataclass
class Platform:
    """What a platform module tells the base app (the ``injectors`` hook).

    ``presets()`` -- the module's shipped presets; ``bundle(session, model,
    which)`` -- ``{name: entity}`` of the file's ``"electrical"`` or
    ``"extras"`` parameters for the model's dataset; ``piezo(session)`` -- a
    reason the file is not a solenoid PD (None when it is)."""
    platform: str
    presets: object
    bundle: object
    electrical_names: Tuple[str, ...] = ()
    extras_names: Tuple[str, ...] = ()
    piezo: Optional[object] = None
    #: ``tuned(session) -> [str]``: signs in the file's own structure that it
    #: is not stock (a function switched off with its fault paths silenced,
    #: which Bosch never ships) -- [] when none is found, which is not proof
    #: of stock. Optional.
    tuned: Optional[object] = None


def platforms() -> List[Platform]:
    """Every installed module's injector platform (presets are shared across
    platforms: the same hardware set on EDC15 and EDC16)."""
    from .modules import registry as _registry
    out = []
    for spec in _registry.installed().values():
        if not getattr(spec, "injectors", None) or \
                getattr(spec, "variant", "full") != "full":
            continue
        try:
            p = spec.call("injectors")
        except Exception:
            p = None
        if p is not None:
            out.append(p)
    return out


def platform_for(session) -> Optional[Platform]:
    name = getattr(session, "platform", "") or ""
    for p in platforms():
        if p.platform == name:
            return p
    return None


def tuned_signs(session) -> List[str]:
    """What in ``session``'s structure says it is not a stock file (the
    platform module's ``tuned`` check); [] when nothing does."""
    plat = platform_for(session)
    if plat is None or plat.tuned is None:
        return []
    try:
        return list(plat.tuned(session) or [])
    except Exception:
        return []


def catalogue(user: Iterable[Preset] = ()) -> Catalogue:
    """Every installed module's presets, plus ``user`` presets."""
    cat = Catalogue()
    for p in platforms():
        try:
            for pr in p.presets() or ():
                cat.add(pr)
        except Exception:
            continue
    for pr in user:
        cat.add(pr)
    return cat


def availability(session) -> Optional[str]:
    """None when the tool can work on ``session``, else why not."""
    if session is None:
        return "No file open"
    if getattr(session, "support", "full") != "full":
        return "Needs the platform's module and a definition for this file"
    plat = platform_for(session)
    if plat is None:
        return "Only unit-injector (PD) EDC15P and EDC16 files have an injector setup"
    if plat.piezo is not None:
        try:
            why = plat.piezo(session)
        except Exception:
            why = None
        if why:
            return why
    ms = _eoi.models(session)
    if not ms:
        return ("Only unit-injector (PD) EDC15P and EDC16 software calculates an "
                "injection duration")
    if not any(not m.missing or not _missing_duration(m) for m in ms):
        return "The duration maps are not all in this file's definition"
    return None


def _missing_duration(m) -> List[str]:
    return [x for x in m.missing if "phiInjMI1" in x or "zmwP_K" in x]


# ---------------------------------------------------------------- fitting
@dataclass
class Finding:
    level: str          # "error" (blocks Apply), "warning", "note"
    text: str


@dataclass
class FitOptions:
    #: injector bodies changed too (complete injectors), not only the nozzles
    body: bool = True
    timing: str = KEEP_CENTRE
    #: adopt the preset's selector curve when the layouts match
    selector: bool = True
    #: also the EDC16 coding-variant copies of every written table
    copies: bool = True
    #: SOI sets to retime (keys of InjectionModel.soi_sets, "base" for the base set)
    soi_sets: Optional[Sequence[str]] = None
    #: also retime the start (cranking) sets -- engine speed x coolant -- at
    #: the start quantity (:func:`plan_fit`, step 2)
    start: bool = True


@dataclass
class TableChange:
    entity: object
    cells: int
    max_delta: float
    label: str = ""


@dataclass
class FitPlan:
    """Writes for one dataset, and what was found working them out."""
    label: str
    model: object
    cells: Dict[int, Tuple[object, Dict[Tuple[int, int], float]]] = field(default_factory=dict)
    axes: List[Tuple[object, object, int, float]] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    changes: List[TableChange] = field(default_factory=list)
    #: (rpm, q) -> (old, new) mg per degree at the reference SOI, for the chart
    curve_old: Dict[float, List[Tuple[float, float]]] = field(default_factory=dict)
    curve_new: Dict[float, List[Tuple[float, float]]] = field(default_factory=dict)

    def put(self, e, r: int, c: int, v: float) -> None:
        self.cells.setdefault(id(e), (e, {}))[1][(r, c)] = v

    def note(self, level: str, text: str) -> None:
        self.findings.append(Finding(level, text))

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def n_cells(self) -> int:
        return sum(len(v) for _e, v in self.cells.values()) + len(self.axes)


def _sel_native(model, char: Characteristic) -> Tuple[List[float], List[float]]:
    """``char``'s selector in the model's own SOI units and axis order."""
    xs = [model.soi_btdc * x for x in char.sel_x]
    ys = list(char.sel_y)
    if len(xs) > 1 and xs[0] > xs[-1]:
        xs.reverse()
        ys.reverse()
    return xs, ys


def _invert(xs: Sequence[float], ys: Sequence[float], k: float) -> Optional[float]:
    """The first x where the piecewise-linear curve (xs, ys) equals ``k``."""
    for i in range(len(xs) - 1):
        y0, y1 = ys[i], ys[i + 1]
        if (y0 - k) * (y1 - k) <= 0 and y0 != y1:
            return xs[i] + (xs[i + 1] - xs[i]) * (k - y0) / (y1 - y0)
        if y0 == k:
            return xs[i]
    if ys and ys[-1] == k:
        return xs[-1]
    return None


def _nearest_end(xs, ys, k) -> float:
    return xs[0] if abs(ys[0] - k) <= abs(ys[-1] - k) else xs[-1]


class _NewDuration:
    """The file's duration lookup with planned tables and selector."""

    def __init__(self, model, sel_xs, sel_ys, vals, tables):
        self.model, self.sxs, self.sys_ = model, sel_xs, sel_ys
        self.vals, self.tabs = vals, tables

    def __call__(self, soi_native: float, rpm: float, q: float) -> float:
        i0, i1, f = position(self.sxs, soi_native)
        idx = self.sys_[i0] + (self.sys_[i1] - self.sys_[i0]) * f
        idx = min(max(idx, 0.0), self.model.duration.last)
        return sum(self.tabs[i].at(rpm, q, self.vals[i]) * w
                   for i, w in _eoi.members(idx, len(self.tabs)))


def _differs(e, old: float, v: float, tol: float) -> bool:
    """Writing ``v`` over ``old`` changes ``e``'s cell: more than ``tol``
    apart and not the same stored value (a write rounds to the raw step, so
    a plan made again after Apply finds nothing left under one step)."""
    if abs(v - old) <= tol:
        return False
    try:
        if not getattr(e, "is_float", False) and getattr(e, "cell_writes", None) is None:
            enc = e.eq.encode
            return int(round(enc(v))) != int(round(enc(old)))
    except Exception:                                         # noqa: BLE001
        pass
    return True


def _clamp(e, v: float) -> Tuple[float, bool]:
    try:
        lo, hi = e.value_range
    except Exception:
        return v, False
    if v < lo:
        return lo, True
    if v > hi:
        return hi, True
    return v, False


def plan_fit(session, model, char: Characteristic, opts: FitOptions,
             electrical: Optional[Dict[str, Snap]] = None,
             extras: Optional[Dict[str, Snap]] = None,
             platform: Optional[Platform] = None,
             label: str = "Injector setup") -> FitPlan:
    """The writes that make ``model``'s dataset of ``session`` deliver its
    requested quantity with hardware ``char``.

    1. **Duration set.** When the table counts and selector point counts
       match and ``opts.selector``, the preset's selector curve is adopted and
       table k is the preset's table k on this file's breakpoints -- exact.
       Otherwise this file's selector is kept and table k is the preset's
       duration at the SOI where this file's selector reads exactly k.
    2. **Start of injection** (``opts.timing``). Each SOI-table cell is moved
       so that, with the new duration at that point, the centre (or the end)
       of injection stays where it was: SOI_new = SOI_old - (d_old - d_new)/2
       (centre) or - (d_old - d_new) (end), solved with the new duration read
       at the new SOI. The start (cranking) sets, on engine speed x coolant,
       are moved the same way (``opts.start``) at the quantity the ECU starts
       with: the set's start-quantity map where the platform names one, else
       the ``q_start`` condition (:data:`ovtsuite.eoi.CONDITIONS`). Starting
       reads the same duration maps, so without this the start of injection
       stays and its end moves with the new duration.
    3. **Body calibration** (``opts.body``): ``electrical`` resampled onto this
       file's parameters of the same name.
    """
    plan = FitPlan(label, model)
    calc = _eoi.Calculator(model)
    tabs = [_eoi.Table(e) for e in model.duration.tables]
    n = len(tabs)
    cur_xs, cur_ys = calc.dur_sel.xs, list(calc.dur_sel.vals[0])

    # -- 1. the duration set ---------------------------------------------
    same_layout = (len(char.tables) == n and len(char.sel_x) == len(cur_xs))
    use_sel = bool(opts.selector and same_layout)
    if use_sel:
        new_xs, new_ys = _sel_native(model, char)
    else:
        new_xs, new_ys = list(cur_xs), cur_ys
        if not same_layout:
            plan.note("note", "This file has %d duration tables and a %d-point "
                      "selector, the preset %d and %d: the file's own selector is "
                      "kept and each table is read from the preset where the "
                      "selector picks it." % (n, len(cur_xs), len(char.tables),
                                              len(char.sel_x)))
    clamped_q = clamped_v = 0
    qcap_seen = 0.0
    new_vals = []
    for k, t in enumerate(tabs):
        e = model.duration.tables[k]
        vals = [list(r) for r in t.vals]
        if not use_sel:
            s_nat = _invert(new_xs, new_ys, float(k))
            if s_nat is None:
                s_nat = _nearest_end(new_xs, new_ys, float(k))
                plan.note("note", "%s: the selector never picks this table; it is "
                          "filled from the preset at the nearest end." % e.name)
            s_b = model.soi_btdc * s_nat
        if use_sel:
            qcap = char.tables[k].q_max * char.flow
        else:
            qcap = min(char.tables[i].q_max for i, w in
                       _eoi.members(char.index(s_b), len(char.tables)) if w > 0) * char.flow
        changed, mx = 0, 0.0
        for i_n, rpm in enumerate(t.rpm_axis()):
            for i_q, q in enumerate(t.q_axis()):
                r, c = t.cell_of(i_n, i_q)
                if q > qcap + 1e-6:
                    clamped_q += 1
                    qcap_seen = max(qcap_seen, qcap)
                v = char.table_value(k, rpm, q) if use_sel else char.duration(s_b, rpm, q)
                v, cl = _clamp(e, v)
                clamped_v += cl
                old = vals[r][c]
                if _differs(e, old, v, 1e-6):
                    plan.put(e, r, c, v)
                    vals[r][c] = v
                    changed += 1
                    mx = max(mx, abs(v - old))
        new_vals.append(vals)
        if changed:
            plan.changes.append(TableChange(e, changed, mx, "duration"))
        # monotonic in quantity along every speed
        for i_n in range(len(t.rpm_axis())):
            row = [vals[t.cell_of(i_n, j)[0]][t.cell_of(i_n, j)[1]]
                   for j in range(len(t.q_axis()))]
            if any(b < a - 0.05 for a, b in zip(row, row[1:])):
                plan.note("error", "%s would fall with quantity at %g rpm: the "
                          "preset does not cover this table's layout." %
                          (e.name, t.rpm_axis()[i_n]))
                break
    if use_sel:
        se = model.duration.selector
        for i, (x, y) in enumerate(zip(new_xs, new_ys)):
            if abs(x - cur_xs[i]) > 1e-6:
                plan.axes.append((se, se.x, i, x))
            if _differs(se, cur_ys[i], y, 1e-6):
                plan.put(se, 0, i, y)
        if plan.axes or plan.cells.get(id(se)):
            plan.changes.append(TableChange(se, len(new_xs), 0.0, "selector"))
    if clamped_q:
        plan.note("note", "%d duration cell(s) lie past the quantity the preset's "
                  "maps reach for that start of injection (up to %.0f mg there): "
                  "they hold its last column, as the ECU would." %
                  (clamped_q, qcap_seen))
    if clamped_v:
        plan.note("warning", "%d duration cell(s) were held to the map's value range."
                  % clamped_v)

    newdur = _NewDuration(model, new_xs, new_ys, new_vals, tabs)

    # -- 2. start of injection ---------------------------------------------
    if opts.timing in (KEEP_CENTRE, KEEP_EOI):
        frac = 0.5 if opts.timing == KEEP_CENTRE else 1.0
        sets = [("base", model.soi, True)]
        start_sets = []
        for ss in model.soi_sets:
            if ss.second == "q":
                sets.append((ss.key, ss.set, ss.corrections))
            elif opts.start:
                start_sets.append(ss)
            else:
                plan.note("note", "%s (engine speed x coolant) left as it is, as chosen: "
                          "the new duration moves its end of injection. Check starting "
                          "after the change." % ss.label)
        want = set(opts.soi_sets) if opts.soi_sets is not None else None
        seen = set()
        for key, ts, cors in sets:
            if want is not None and key not in want:
                continue
            for e in ts.tables:
                if e is None or id(e) in seen:
                    continue
                seen.add(id(e))
                st = _eoi.Table(e)
                changed, mx, lim = 0, 0.0, 0
                for i_n, rpm in enumerate(st.rpm_axis()):
                    for i_q, q in enumerate(st.q_axis()):
                        r, c = st.cell_of(i_n, i_q)
                        cell = st.vals[r][c]
                        s_op = cell + (calc.correction(rpm, q)
                                       if (cors and calc.uses_corrections) else 0.0)
                        d_old = calc.duration(s_op, rpm, q)[0]
                        b_old = calc.btdc(s_op)
                        b = b_old
                        for _ in range(8):
                            d_new = newdur(calc.native(b), rpm, q)
                            nb = b_old - frac * (d_old - d_new)
                            if abs(nb - b) < 1e-4:
                                b = nb
                                break
                            b = nb
                        v = cell + (calc.native(b) - s_op)
                        v, cl = _clamp(e, v)
                        lim += cl
                        if _differs(e, cell, v, 0.005):
                            plan.put(e, r, c, v)
                            changed += 1
                            mx = max(mx, abs(v - cell))
                if changed:
                    plan.changes.append(TableChange(e, changed, mx, "timing"))
                if lim:
                    plan.note("warning", "%s: %d cell(s) held to the map's range."
                              % (e.name, lim))
        for ss in start_sets:
            if want is not None and ss.key not in want:
                continue
            _retime_start(plan, model, ss, newdur, frac, seen)
        if calc.lim is not None:
            plan.note("note", "The start-of-injection limiter (%s) still applies "
                      "after the change." % model.limiter.name)

    # -- 3. body calibration and extras --------------------------------------
    def _write_bundle(snaps, which, title):
        if not snaps:
            return
        have = {}
        if platform is not None:
            try:
                have = platform.bundle(session, model, which) or {}
            except Exception:
                have = {}
        miss, done = [], set()
        own = set(getattr(platform, "electrical_names", ()) or ()) | \
            set(getattr(platform, "extras_names", ()) or ())
        # this platform's own names first; another platform's name for the
        # same quantity (an alias) only where the own one is not in the preset
        for name, snap in sorted(snaps.items(), key=lambda kv: kv[0] not in own):
            e = have.get(name)
            if e is None:
                if name in own:
                    miss.append(name)
                continue
            if id(e) in done:
                continue
            done.add(id(e))
            try:
                vals = _resample_snap(snap, e)
            except (ValueError, IndexError) as ex:     # IndexError: an empty snapshot
                plan.note("warning", "%s: %s; left as it is." % (name, str(ex) or "no values"))
                continue
            cur = e.values()
            changed, mx = 0, 0.0
            for r, row in enumerate(vals):
                for c, v in enumerate(row):
                    v, _cl = _clamp(e, v)
                    if _differs(e, cur[r][c], v, 1e-6):
                        plan.put(e, r, c, v)
                        changed += 1
                        mx = max(mx, abs(v - cur[r][c]))
            if changed:
                plan.changes.append(TableChange(e, changed, mx, title))
        if miss:
            plan.note("note", "%s not in this file's definition, so not written: %s."
                      % (title.capitalize(), ", ".join(sorted(miss))))

    if opts.body:
        _write_bundle(electrical or {}, "electrical", "solenoid calibration")
    elif electrical:
        plan.note("note", "Nozzles only: this file's own solenoid (BIP) calibration "
                  "is kept, as the injector bodies stay.")
    _write_bundle(extras or {}, "extras", "pilot / post duration")

    # -- what the change means: fuel per crank degree at 10 deg BTDC, where
    # the delivery lasts long enough to mean something (>= 3 deg: below it
    # the valve's own closing time dominates and the ratio says nothing)
    old_char = characteristic(model)

    def _qtop(c, flow=1.0):
        return min(c.tables[i].q_max for i, w in
                   _eoi.members(c.index(10.0), len(c.tables)) if w > 0) * flow
    qtop = min(_qtop(old_char), _qtop(char, char.flow))
    for q in (20.0, 30.0, 45.0):
        if q > qtop + 1e-6:
            continue
        o, nw = [], []
        for n_ in range(1000, 4001, 250):
            d0 = old_char.duration(10.0, n_, q)
            d1 = newdur(model.soi_btdc * 10.0, n_, q)
            if d0 >= 3.0:
                o.append((float(n_), q / d0))
            if d1 >= 3.0:
                nw.append((float(n_), q / d1))
        plan.curve_old[q], plan.curve_new[q] = o, nw
    _check_quantity_axis(session, model, tabs, plan)
    return plan


def _retime_start(plan: FitPlan, model, ss, newdur: "_NewDuration", frac: float,
                  seen: set) -> None:
    """Step 2 of :func:`plan_fit` for a start (cranking) set ``ss`` (an
    :class:`~ovtsuite.eoi.SoiSet` on engine speed x coolant): every cell moved
    by ``frac`` of the duration change at the quantity the ECU starts with
    there. No corrections are added while starting, so the cell is the SOI."""
    try:
        cs = _eoi.Calculator(model, soi_set=ss.key)
    except Exception as ex:                                   # noqa: BLE001
        plan.note("note", "%s left as it is: %s." % (ss.label, ex))
        return
    if cs.q_map is not None:
        how = "at the start quantity of %s" % ss.quantity.name
    else:
        how = ("at %g mg per stroke (no start-quantity map in this definition: "
               "End of injection's start quantity)"
               % cs.conditions.get("q_start", _eoi.CONDITIONS["q_start"][2]))
    total, skipped = 0, 0
    for e in ss.set.tables:
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        st = _eoi.Table(e)
        changed, mx, lim = 0, 0.0, 0
        for i_n, rpm in enumerate(st.rpm_axis()):
            for i_c, clt in enumerate(st.q_axis()):
                r, c = st.cell_of(i_n, i_c)
                cell = st.vals[r][c]
                q = cs.dur_q(clt, rpm, clt)
                if q < 0.5:                     # nothing injected there
                    skipped += 1
                    continue
                d_old = cs.duration(cell, rpm, q)[0]
                b_old = cs.btdc(cell)
                b = b_old
                for _ in range(8):
                    nb = b_old - frac * (d_old - newdur(cs.native(b), rpm, q))
                    if abs(nb - b) < 1e-4:
                        b = nb
                        break
                    b = nb
                v, cl = _clamp(e, cs.native(b))
                lim += cl
                if _differs(e, cell, v, 0.005):
                    plan.put(e, r, c, v)
                    changed += 1
                    mx = max(mx, abs(v - cell))
        if changed:
            plan.changes.append(TableChange(e, changed, mx, "start timing"))
            total += changed
        if lim:
            plan.note("warning", "%s: %d cell(s) held to the map's range." % (e.name, lim))
    if total:
        plan.note("note", "%s, on engine speed x coolant: retimed like the running maps, "
                  "%s%s. Bosch's own start maps for other injectors differ from that by a "
                  "degree or two: check starting after the change." % (
                      ss.label, how,
                      "; cells where nothing is injected left as they are" if skipped else ""))


def _resample_snap(snap: Snap, e) -> List[List[float]]:
    from .resize import resample
    if snap.kind != e.kind:
        raise ValueError("a %s in the preset but a %s here" % (snap.kind, e.kind))
    if e.kind == "constant" or (not snap.x and not snap.y):
        return [[snap.z[0][0]]]
    nx = [float(v) for v in e.x_values()] if getattr(e, "x", None) is not None else []
    ny = [float(v) for v in e.y_values()] if e.kind == "map" else []
    if len(snap.x) < 1 or not nx:
        raise ValueError("no axis to resample on")
    if any(b < a for a, b in zip(snap.x, snap.x[1:])) or \
            any(b < a for a, b in zip(nx, nx[1:])):
        raise ValueError("an axis is not ascending")
    return resample(snap.x, snap.y, snap.z, nx, ny)


def _check_quantity_axis(session, model, tabs, plan: FitPlan) -> None:
    """Warn when the full-load quantity the ECU asks for runs past the last
    quantity breakpoint of the duration tables it reads there (warm, at the
    base start of injection): the lookup holds the last column, so no more
    fuel is delivered."""
    try:
        from . import dyno as _dyno
        ms = _dyno.models(session)
        m = _dyno.matching(ms, getattr(model, "group", ""))
        if m is None:
            return
        cond = _dyno.Conditions(fuel_basis="quantity")
        lo, hi = m.rpm_range(cond)
        calc = _eoi.Calculator(model)
    except Exception:
        return
    worst = None
    n_ = float(lo)
    while n_ <= hi + 1e-6:
        try:
            q = float(m.full_load(n_, cond).q)
            soi, _ = calc.soi(calc.model.coolant_warm, n_, q)
            mem = _eoi.members(calc.index(soi), len(tabs))
            top = min(tabs[i].q_axis()[-1] for i, w in mem if w > 0)
            if q > top + 0.5 and (worst is None or q - top > worst[0] - worst[2]):
                worst = (q, n_, top)
        except Exception:
            pass
        n_ += 100.0
    if worst is not None:
        q, rpm, top = worst
        plan.note("warning", "At full load the ECU asks for %.1f mg at %.0f rpm, past "
                  "the last quantity breakpoint (%.0f mg) of the duration table it "
                  "reads there: no more fuel is delivered above it. Extend the "
                  "quantity axis (Resize axes) if the new injectors are meant to "
                  "give more." % (q, rpm, top))


def apply_fit(session, plans: Sequence[FitPlan], label: str,
              copies: bool = True) -> int:
    """Write ``plans`` as ONE undo step; the number of values written.

    With ``copies``, an EDC16 coding-variant copy of a written table gets the
    same values (as the End of injection view does)."""
    if any(p.errors for p in plans):
        raise ValueError("the plan has errors: " + "; ".join(
            f.text for p in plans for f in p.errors))
    variants = []
    if copies:
        try:
            variants = list(session.variant_entities())
        except Exception:
            variants = []
    n = 0
    with session.transaction(label):
        for plan in plans:
            for e, ax, i, v in plan.axes:
                targets = [ax]
                pre = e.name + " (variant @"
                targets += [getattr(x, "x", None) for x in variants
                            if x.name.startswith(pre) and getattr(x, "x", None) is not None
                            and x.cols == e.cols]
                for a in targets:
                    session.edit_axis(a, i, v)
                n += 1
            for e, cells in plan.cells.values():
                rows = [(r, c, v) for (r, c), v in sorted(cells.items())]
                pre = e.name + " (variant @"
                copies_ = [x for x in variants if x.name.startswith(pre)
                           and x.rows == e.rows and x.cols == e.cols]
                for target in [e] + copies_:
                    session.edit_cells(target, rows)
                n += len(rows)
    return n


def preset_from_session(session, model, platform: Optional[Platform], pid: str,
                        name: str, **kw) -> Preset:
    """A user preset of the hardware ``model``'s dataset is set up for."""
    elec, extras = {}, {}
    if platform is not None:
        for which, out in (("electrical", elec), ("extras", extras)):
            try:
                for nm, e in (platform.bundle(session, model, which) or {}).items():
                    out[nm] = snapshot(e)
            except Exception:
                pass
    return Preset(id=pid, name=name, kind=kw.pop("kind", USER),
                  confidence=kw.pop("confidence", EXACT),
                  platform=getattr(session, "platform", ""),
                  char=characteristic(model), electrical=elec, extras=extras, **kw)


# ---------------------------------------------------------------- project record
#: ``ovtsuite.project.hardware`` kind of a unit-injector record
HW_KIND = "pd_injectors"


def hardware_record(preset: "Preset", body: bool) -> Tuple[str, dict]:
    """``(summary, parts)`` for the project's fitted-hardware record
    (:mod:`ovtsuite.project.hardware`) of fitting ``preset``: complete
    injectors (``body``) or its nozzles in the old bodies. ``parts`` is what
    two records are compared by."""
    what = "complete injectors" if body else "nozzles (injector bodies kept)"
    return ("%s, %s" % (preset.name, what),
            {"preset": preset.id, "complete": bool(body)})


__all__ = ["HW_KIND", "hardware_record", "RawTable", "Characteristic", "characteristic", "compare", "Snap",
           "snapshot", "Preset", "load_presets", "save_presets", "Catalogue",
           "Match", "identify", "DatasetMatch", "identify_datasets", "datasets_disagree",
           "Platform", "platforms", "platform_for",
           "catalogue", "tuned_signs", "availability", "Finding", "FitOptions", "FitPlan",
           "TableChange", "plan_fit", "apply_fit", "preset_from_session",
           "KEEP_CENTRE", "KEEP_EOI", "KEEP_SOI", "TIMING_LABELS", "TIMING_TIPS",
           "OEM", "AFTERMARKET", "USER", "EXACT", "ESTIMATED", "START",
           "KIND_LABELS", "CONFIDENCE_LABELS"]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The calculated dyno chart (:mod:`ovtsuite.dyno`): a panel in the tab and
a full window.

**Dyno** on the edit bar (Ctrl+4) shows the panel above the table: power and
torque against engine speed for the open file, the reference drawn dashed
beside it, and a strip under the curves coloured by the limit that governs
at each engine speed -- click a band to open that map. It follows every
edit and undo. **Details...** on the panel, or **Tools > Dyno chart...**,
opens the full window: the reference and the conditions, the per-rpm
figures, and export.

The reference -- the file whose conversion turns quantity into torque, and
whose curve is drawn beside -- is by default the ORIGINAL (the file as first
opened; a project set as read), and can be the comparison or any version in
the history, or any run added in the window. Panel and window share one
state per tab, so a change in either shows in both.

The window adds other runs (:mod:`ovtsuite.dyno_sources`): another tab, a
branch or version of any open project, a file, this file's curve pinned at a
moment, a data log (:mod:`ovtsuite.dyno_log`); each is read through the
reference at the same conditions and drawn in a colour of its own. It also
shows what changed between two runs (:mod:`ovtsuite.dyno_diff`), road speed
in a gear and the condition presets (:mod:`ovtsuite.dyno_speed`), and exports
with the conditions (:mod:`ovtsuite.gui_dyno_export`). In a project tab the
conditions and the runs are kept with the set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import dyno as _dy
from . import dyno_sources as _src

#: Settings keys
SHOWN_KEY = "dyno/shown"
SIZES_KEY = "dyno/sizes"
POWER_KEY = "dyno/power_unit"
TORQUE_KEY = "dyno/torque_unit"
REF_KEY = "dyno/reference"
COND_KEY = "dyno/cond/%s"
#: conditions remembered between runs (the rest are per tab)
_REMEMBER = ("baro", "charge_temp", "coolant", "ve", "efficiency", "loss",
             "fuel_basis")

#: one colour per limiter key (the strip and the legend)
LIMITER_COLOURS = {
    "wish": "#1D9E75", "trq_p": "#7F77DD", "trq_p_boost": "#AFA9EC",
    "trq_n": "#534AB7",
    "gearbox": "#888780", "conv_end": "#BA7517", "qlim_p": "#993C1D",
    "smk_air": "#D85A30", "smk_boost": "#D4537E", "smk_lambda": "#E24B4A",
}

_POLL_MS = 400


def _settings():
    from .appsettings import settings
    return settings()


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def limiter_colour(key: str) -> QtGui.QColor:
    return QtGui.QColor(LIMITER_COLOURS.get(key, "#B4B2A9"))


def _num(v: float, nd: int = 1) -> str:
    t = "%.*f" % (nd, v)
    return "0" if t in ("-0", "-0.0") else t


# =================================================================== state
@dataclass
class Run:
    """One curve on the chart: the tab's own, its reference, or a source."""
    key: str
    label: str
    detail: str
    slot: int                        # its colour (RUN_COLOURS)
    curve: Optional[_dy.Curve]
    why: str = ""                    # why there is no curve
    dots: bool = False               # a measured run: points, not a line
    source: Optional[_src.Source] = None
    session: object = None


#: Conditions fields a project keeps for its set
_PROJECT_COND = ("baro", "charge_temp", "coolant", "ve", "efficiency", "loss",
                 "fuel_basis", "wish", "smoke", "displacement", "cylinders",
                 "boost_cap", "overboost")


class DynoState(QtCore.QObject):
    """What the tab's dyno shows: the conditions, the reference, the units,
    the other runs, and the curves worked out from them. One per tab, shared
    by the panel and the window; :meth:`poll` recalculates when a file it
    reads changed.

    In a project tab the conditions, the runs and the axis are the set's
    (kept in the project, ``BinarySet.extra["dyno"]``); a plain file's
    conditions are the app's."""

    changed = QtCore.Signal()

    def __init__(self, ws):
        super().__init__(ws)
        self.ws = ws
        s = _settings()
        self.cond = _dy.Conditions()
        for k in _REMEMBER:
            v = s.value(COND_KEY % k, None)
            if v is None or v == "":
                continue
            try:
                cur = getattr(self.cond, k)
                setattr(self.cond, k, type(cur)(v) if not isinstance(cur, str) else str(v))
            except (TypeError, ValueError):
                pass
        self.power_unit = str(s.value(POWER_KEY, _dy.DEFAULT_POWER))
        self.torque_unit = str(s.value(TORQUE_KEY, _dy.DEFAULT_TORQUE))
        kind = str(s.value(REF_KEY, _dy.REF_ORIGINAL))
        #: the reference: (kind, version id) -- kind REF_* of ovtsuite.dyno,
        #: or REF_SOURCE with the source's key
        self.ref_kind = kind if kind in (_dy.REF_ORIGINAL, _dy.REF_COMPARISON,
                                         _dy.REF_NONE) else _dy.REF_ORIGINAL
        self.ref_version: Optional[str] = None
        self.group: Optional[str] = None
        #: the runs beside this file's and its reference's
        self.sources: List[_src.Source] = []
        self.resolver = _src.Resolver(ws)
        #: the x axis: engine speed, or road speed in a gear
        #: (``k``: km/h per 1000 rpm the user set, per gear)
        from . import dyno_speed as _sp
        self.xaxis = {"mode": "rpm", "gear": 0, "k": {}, "unit": "kmh",
                      "gearbox": "", "tyre": _sp.DEFAULT_TYRE}
        # results
        self.models: List[_dy.DynoModel] = []
        self.model: Optional[_dy.DynoModel] = None
        #: the reference's model, when it has one (its conversion is used)
        self.ref_model: Optional[_dy.DynoModel] = None
        self.ref_session = None
        self.curve: Optional[_dy.Curve] = None
        self.ref_curve: Optional[_dy.Curve] = None
        self.ref_label = ""
        self.runs: List[Run] = []
        self.message = ""
        # change tracking
        self._seen = None
        self._ref_cache: Dict[tuple, object] = {}
        self._dirty = True
        self._loading = False
        self._gears: Optional[tuple] = None
        #: the session worked out last (held, so a new file in the tab is
        #: told apart by identity, never by a reused id())
        self._file = None

    # -- where its settings live ---------------------------------------------
    def _set_extra(self):
        """The project set this tab shows, or None (a plain file)."""
        ref = getattr(self.ws, "project_ref", None)
        if ref is None:
            return None
        try:
            return ref.set
        except Exception:
            return None

    def _load_project(self):
        st = self._set_extra()
        d = (st.extra.get("dyno") if st is not None else None) or {}
        if not d:
            return
        self._loading = True
        try:
            for k in _PROJECT_COND:
                if k in d:
                    v = d[k]
                    setattr(self.cond, k, tuple(v) if k == "smoke" and v is not None else v)
            self.group = d.get("group") or None
            self.sources = [x for x in (_src.Source.from_dict(i)
                                        for i in d.get("sources") or []) if x]
            ref = d.get("ref") or {}
            kind = ref.get("kind", self.ref_kind)
            if kind == _dy.REF_SOURCE:
                i = ref.get("index", -1)
                if 0 <= i < len(self.sources):
                    self.ref_kind, self.ref_version = kind, self.sources[i].key
            elif kind in (_dy.REF_ORIGINAL, _dy.REF_COMPARISON, _dy.REF_NONE,
                          _dy.REF_VERSION):
                self.ref_kind, self.ref_version = kind, ref.get("vid")
            xa = d.get("xaxis")
            if isinstance(xa, dict):
                self.xaxis.update(xa)
                self.xaxis["k"] = {int(g): float(v) for g, v in
                                   (xa.get("k") or {}).items()}
        finally:
            self._loading = False

    def _save_project(self):
        """Keep the set's dyno settings in the project (a change to it, as
        an edit is: saved with the project)."""
        if self._loading:
            return
        st = self._set_extra()
        if st is None:
            return
        keep = [x for x in self.sources if x.to_dict() is not None]
        d = {k: getattr(self.cond, k) for k in _PROJECT_COND}
        if d.get("smoke") is not None:
            d["smoke"] = list(d["smoke"])
        ref = {"kind": self.ref_kind}
        if self.ref_kind == _dy.REF_VERSION:
            ref["vid"] = self.ref_version
        elif self.ref_kind == _dy.REF_SOURCE:
            src = self.source(self.ref_version)
            if src in keep:
                ref["index"] = keep.index(src)
            else:
                ref = {"kind": _dy.REF_ORIGINAL}
        d.update(group=self.group, ref=ref, sources=[x.to_dict() for x in keep],
                 xaxis={"mode": self.xaxis["mode"], "gear": self.xaxis["gear"],
                        "unit": self.xaxis["unit"],
                        "gearbox": self.xaxis.get("gearbox", ""),
                        "tyre": self.xaxis.get("tyre", ""),
                        "k": {str(g): v for g, v in self.xaxis["k"].items()}})
        if st.extra.get("dyno") == d:
            return
        st.extra["dyno"] = d
        try:
            self.ws.project_ref.doc.project._touch()
            win = getattr(self.ws, "win", None)
            if win is not None:
                win._workspace_changed(self.ws)      # its "*"
        except Exception:
            pass

    # -- settings ------------------------------------------------------------
    def set_units(self, power: Optional[str] = None, torque: Optional[str] = None):
        s = _settings()
        if power:
            self.power_unit = power
            s.setValue(POWER_KEY, power)
        if torque:
            self.torque_unit = torque
            s.setValue(TORQUE_KEY, torque)
        s.sync()
        self.changed.emit()                 # display only: nothing to work out

    def set_reference(self, kind: str, version: Optional[str] = None):
        self.ref_kind, self.ref_version = kind, version
        if kind not in (_dy.REF_VERSION, _dy.REF_SOURCE) and self._set_extra() is None:
            s = _settings()
            s.setValue(REF_KEY, kind)
            s.sync()
        self._save_project()
        self.invalidate()

    def set_condition(self, **kw):
        project = self._set_extra() is not None
        s = _settings()
        for k, v in kw.items():
            setattr(self.cond, k, v)
            if k in _REMEMBER and not project:
                s.setValue(COND_KEY % k, v)
        s.sync()
        self._save_project()
        self.invalidate()

    def set_group(self, group: Optional[str]):
        self.group = group
        self._gears = None
        self._save_project()
        self.invalidate()

    def invalidate(self):
        self._dirty = True
        self.poll()

    # -- the other runs ------------------------------------------------------------
    def source(self, key) -> Optional[_src.Source]:
        return next((x for x in self.sources if x.key == key), None)

    def add_source(self, src: _src.Source) -> Optional[_src.Source]:
        """Add a run (None when the chart has no colour left for it)."""
        slot = _src.free_slot(self.sources)
        if slot is None or len(self.sources) >= _src.MAX_SOURCES:
            return None
        src.slot = slot
        self.sources.append(src)
        self._save_project()
        self.invalidate()
        return src

    def remove_source(self, key: str):
        src = self.source(key)
        if src is None:
            return
        self.sources.remove(src)
        self.resolver.forget(key)
        if self.ref_kind == _dy.REF_SOURCE and self.ref_version == key:
            self.ref_kind, self.ref_version = _dy.REF_ORIGINAL, None
        self._save_project()
        self.invalidate()

    def show_source(self, key: str, on: bool):
        src = self.source(key)
        if src is not None and src.shown != bool(on):
            src.shown = bool(on)
            self._save_project()
            self.invalidate()

    def pin(self) -> Optional[_src.Source]:
        """This file's curve as it is now, kept as a run to edit against."""
        p = getattr(self.ws, "primary", None)
        if p is None:
            return None
        return self.add_source(_src.make_pin(p))

    # -- the x axis ------------------------------------------------------------------
    def gear_speeds(self) -> Tuple[Dict[int, float], str]:
        """``({gear: km/h per 1000 rpm}, where from)`` of the gearbox chosen
        on the tyre given -- or, with none chosen on EDC16, of the ECU's own
        gear model (said to be the ECU's); before the user's own figures."""
        from . import dyno_speed as _sp
        p = getattr(self.ws, "primary", None)
        gid = self.xaxis.get("gearbox") or ""
        tyre = self.xaxis.get("tyre") or ""
        key = (id(p), gid, tyre)
        if self._gears is not None and self._gears[0] == key:
            return self._gears[1], self._gears[2]
        if gid and gid != _sp.ECU:
            gb = _sp.gearbox(gid)
            circ = _sp.tyre_circ(tyre)
            if gb is None:
                got = ({}, "the gearbox chosen is no longer listed")
            elif circ is None:
                got = ({}, "enter the tyre size (e.g. 225/45 R17) or its rolling "
                           "circumference in metres")
            else:
                got = (_sp.gearbox_speeds(gb, circ),
                       "%s on %s (%.3f m)" % (gb["name"], tyre.strip(), circ))
        else:
            got = _sp.ecu_gears(p)
            if got[0] and not gid:
                got = (got[0], got[1] + " -- the ECU's, often not your gearbox: "
                       "choose yours")
            elif not got[0]:
                got = ({}, "choose the gearbox (%s)" % got[1])
        self._gears = (key,) + got
        return got

    def file_gears(self) -> Tuple[Dict[int, float], str]:
        """(kept for the first version's callers: :meth:`gear_speeds`)"""
        return self.gear_speeds()

    def gear_factor(self, gear: int) -> Optional[float]:
        """km/h per 1000 rpm in ``gear``: the user's figure, else the
        gearbox's."""
        k = self.xaxis["k"].get(int(gear))
        if k:
            return k
        return self.gear_speeds()[0].get(int(gear))

    def speed_factor(self) -> Optional[float]:
        """km/h per rpm when the x axis is road speed, else None."""
        if self.xaxis.get("mode") != "speed":
            return None
        k = self.gear_factor(self.xaxis.get("gear") or 0)
        return (k / 1000.0) if k else None

    def set_xaxis(self, **kw):
        self.xaxis.update(kw)
        if "gearbox" in kw or "tyre" in kw:
            self._gears = None
        self._save_project()
        self.changed.emit()                 # display only

    def set_gear_factor(self, gear: int, k: Optional[float]):
        if k:
            self.xaxis["k"][int(gear)] = float(k)
        else:
            self.xaxis["k"].pop(int(gear), None)
        self._save_project()
        self.changed.emit()

    # -- reference ---------------------------------------------------------------
    def versions(self) -> List[dict]:
        p = getattr(self.ws, "primary", None)
        if p is None:
            return []
        from . import history as _history
        try:
            return [v for v in _history.list_versions(p) if not v.get("pending")]
        except Exception:
            return []

    def original_label(self) -> str:
        return ("Original (as read)" if getattr(self.ws, "project_ref", None)
                is not None else "Original (as first opened)")

    def _reference(self):
        """``(session or None, label)`` of the chosen reference."""
        p = self.ws.primary
        kind = self.ref_kind
        if kind == _dy.REF_NONE:
            return None, "none"
        if kind == _dy.REF_COMPARISON:
            c = getattr(self.ws, "comparison", None)
            if c is None:
                return None, "no comparison loaded"
            return c, "Comparison - %s" % c.binfile.name
        if kind == _dy.REF_SOURCE:
            src = self.source(self.ref_version)
            if src is None or src.kind == _src.LOG:
                return None, "no such run"
            sess, why = self.resolver.session(src, p, getattr(self.ws, "win", None))
            if sess is None:
                return None, "%s (%s)" % (src.label, why or "could not be read")
            return sess, src.label
        if kind == _dy.REF_VERSION and self.ref_version:
            key = ("v", id(p), self.ref_version)
            label = "Version %s" % self._version_label(self.ref_version)
            getter = lambda: _dy.version_bytes(p, self.ref_version)  # noqa: E731
        else:
            key = ("o", id(p), getattr(p, "path", ""), len(self.versions()))
            label = self.original_label()
            getter = lambda: _dy.original_bytes(p)                    # noqa: E731
        if key not in self._ref_cache:
            data = getter()
            sess = None
            if data is not None:
                try:
                    sess = _dy.session_of(data, p)
                except Exception:
                    sess = None
            self._ref_cache = {k: v for k, v in self._ref_cache.items()
                               if k[1] == id(p)}
            self._ref_cache[key] = (sess, None if data is None else bytes(data))
        sess, data = self._ref_cache[key]
        if sess is None:
            return None, label + " (could not be rebuilt)"
        same = data is not None and data == bytes(p.binfile.data)
        return sess, label + (" - same as current" if same else "")

    def _version_label(self, vid: str) -> str:
        from . import history as _history
        for v in self.versions():
            if v.get("id") == vid:
                return "%s %s" % (v.get("ts", ""), _history.display_label(v))
        return vid

    # -- recalculation -------------------------------------------------------------
    def _signature(self):
        p = getattr(self.ws, "primary", None)
        c = getattr(self.ws, "comparison", None)
        cmp_ = self.ref_kind == _dy.REF_COMPARISON
        return (id(p), getattr(p, "revision", None),
                id(c) if cmp_ else None,
                getattr(c, "revision", None) if cmp_ else None,
                tuple((x.key, x.shown, self.resolver.stamp(x)) for x in self.sources))

    def _new_file(self):
        """Another file in the tab: what was picked for the old one -- its
        engine, smoke limits, wish map, dataset, version, runs -- goes with
        it (a project set brings its own back)."""
        for k in ("wish", "smoke", "displacement", "cylinders"):
            setattr(self.cond, k, getattr(_dy.Conditions(), k))
        self.group = None
        self.ref_version = None
        if self.ref_kind in (_dy.REF_VERSION, _dy.REF_SOURCE):
            self.ref_kind = _dy.REF_ORIGINAL
        self.sources = []
        self.xaxis = {"mode": "rpm", "gear": 0, "k": {}, "unit": self.xaxis["unit"],
                      "gearbox": self.xaxis.get("gearbox", ""),
                      "tyre": self.xaxis.get("tyre", "")}
        self._ref_cache = {}
        self._gears = None
        self.resolver.clear()

    def poll(self) -> bool:
        """Recalculate when something changed; True when it did."""
        p = getattr(self.ws, "primary", None)
        if p is not self._file:
            if self._file is not None:
                self._new_file()
            self._file = p
            self._load_project()
            self._dirty = True
        sig = self._signature()
        if not self._dirty and sig == self._seen:
            return False
        self._seen, self._dirty = sig, False
        self._calculate()
        self.changed.emit()
        return True

    def _calculate(self):
        self.curve = self.ref_curve = None
        self.model = self.ref_model = self.ref_session = None
        self.runs = []
        self.message = ""
        p = getattr(self.ws, "primary", None)
        if p is None:
            self.models = []
            self.message = "No file open."
            return
        self.models = _dy.models(p)
        if not self.models:
            err = _dy.models_error(p)
            self.message = ("The module could not read this file's maps for "
                            "the dyno: %s" % err) if err else (
                "The dyno chart needs an EDC15 or EDC16 file with its module "
                "installed and a definition.")
            return
        m = next((x for x in self.models if x.group == self.group), self.models[0])
        self.model = m
        try:
            ref_s, self.ref_label = self._reference()
            rm = None
            if ref_s is not None:
                rm = _dy.matching(_dy.models(ref_s), m.group)
            self.ref_model, self.ref_session = rm, ref_s
            fuel = _dy.fuel_map(p, ref_s, m.group, self.cond.coolant) \
                if (ref_s is not None and self.cond.fuel_basis == "duration") \
                else None
            self.curve = _dy.calculate(m, self.cond, rm, label="This file", fuel=fuel)
            if rm is not None:
                self.ref_curve = _dy.calculate(rm, self.cond, rm, label="Reference")
        except Exception as ex:          # a definition the model can't read
            self.message = "Could not work the curve out: %s" % ex
            self.curve = self.ref_curve = None
            return
        self.runs.append(Run("this", "This file", p.binfile.name, 0, self.curve,
                             session=p))
        if self.ref_curve is not None:
            self.runs.append(Run("ref", "Reference", self.ref_label, 1,
                                 self.ref_curve, session=ref_s))
        for src in self.sources:
            if not src.shown or (self.ref_kind == _dy.REF_SOURCE
                                 and self.ref_version == src.key):
                continue
            self.runs.append(self._run_of(src, p, m, rm, ref_s))

    def _run_of(self, src, p, m, rm, ref_s) -> Run:
        """``src`` worked out as this file is: through the reference's
        conversion (and its duration maps), at the same conditions."""
        win = getattr(self.ws, "win", None)
        run = Run(src.key, src.label, src.detail, src.slot, None, source=src)
        try:
            if src.kind == _src.LOG:
                from . import dyno_log as _dl
                runs, why = self.resolver.runs(src, p, win)
                pts, note = _dl.full_load(runs) if runs else ([], why)
                run.dots = True
                if pts:
                    run.curve = _dl.curve(pts, m, rm, self.cond, src.label, note)
                else:
                    run.why = note or why or "nothing to show"
                return run
            sess, why = self.resolver.session(src, p, win)
            run.session = sess
            if sess is None:
                run.why = why or "could not be read"
                return run
            mo = _dy.matching(_dy.models(sess), m.group)
            if mo is None:
                run.why = _dy.models_error(sess) or "not a file the dyno reads"
                return run
            fuel = _dy.fuel_map(sess, ref_s, mo.group, self.cond.coolant) \
                if (ref_s is not None and self.cond.fuel_basis == "duration") \
                else None
            run.curve = _dy.calculate(mo, self.cond, rm, label=src.label, fuel=fuel)
        except Exception as ex:
            run.why = "could not be worked out: %s" % (str(ex) or type(ex).__name__)
        return run

    # -- summary -----------------------------------------------------------------
    def summary(self, compact: bool = False) -> str:
        """Peak power and torque, and how they compare with the reference;
        ``compact`` (the panel's one line) leaves out the reference's own
        figures."""
        c = self.curve
        if c is None or not c.points:
            return self.message or (c.notes[-1] if c is not None and c.notes else "")
        pu, tu = self.power_unit, self.torque_unit
        pp, pt = c.peak_power(), c.peak_torque()
        txt = "%s %s @ %d   %s %s @ %d" % (
            _num(_dy.power_in(pp.power, pu)), _dy.power_label(pu), pp.rpm,
            _num(_dy.torque_in(pt.torque, tu), 0), _dy.torque_label(tu), pt.rpm)
        r = self.ref_curve
        if r is not None and r.points:
            rp, rt = r.peak_power(), r.peak_torque()
            dp = _dy.power_in(pp.power - rp.power, pu)
            dt = _dy.torque_in(pt.torque - rt.torque, tu)
            if compact:
                return txt + "   (%+.1f %s, %+.0f %s)" % (
                    dp, _dy.power_label(pu), dt, _dy.torque_label(tu))
            txt += "    (reference %s %s, %s %s:  %+.1f %s, %+.0f %s)" % (
                _num(_dy.power_in(rp.power, pu)), _dy.power_label(pu),
                _num(_dy.torque_in(rt.torque, tu), 0), _dy.torque_label(tu),
                dp, _dy.power_label(pu), dt, _dy.torque_label(tu))
        return txt


def state_for(ws) -> DynoState:
    st = getattr(ws, "_dyno_state", None)
    if st is None:
        st = DynoState(ws)
        ws._dyno_state = st
    return st


def availability(ws) -> Optional[str]:
    """None when the chart can open for tab ``ws``, else why not."""
    p = getattr(ws, "primary", None)
    if p is None:
        return "No file open"
    if getattr(p, "support", "full") != "full":
        return "Needs the platform's module and a definition for this file"
    if not _dy.models(p):
        return "The dyno chart works out EDC15 and EDC16 calibrations"
    return None


# =================================================================== chart
#: one colour per run, in a fixed order (this file, its reference, then the
#: runs added -- each keeps the colour it was given): the validated
#: categorical palette, its light- and dark-surface steps
RUN_COLOURS = {
    "light": ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300",
              "#6250d6", "#e34948"),
    "dark": ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300",
             "#9085e9", "#e66767"),
}
#: kept for the modules and the tests that name them: this file's colour
#: and the reference's
POWER_COLOUR = QtGui.QColor(RUN_COLOURS["light"][1])
TORQUE_COLOUR = QtGui.QColor(RUN_COLOURS["light"][0])


def run_colour(slot: int, dark: bool = False) -> QtGui.QColor:
    pal = RUN_COLOURS["dark" if dark else "light"]
    return QtGui.QColor(pal[max(0, min(len(pal) - 1, int(slot)))])


def _is_dark(widget) -> bool:
    return widget.palette().color(QtGui.QPalette.Base).lightness() < 128


class DynoChart(QtWidgets.QWidget):
    """Power and torque against engine speed (or road speed in a gear),
    painted (no chart library): power on the left axis, torque on the
    right; each run in its own colour, power solid and torque dashed, a
    logged run as points; this file's governing-limit strip under the plot."""

    limiterClicked = QtCore.Signal(str, float)      # (limiter key, rpm)

    STRIP_H = 12

    def __init__(self, state: Optional[DynoState] = None, parent=None):
        super().__init__(parent)
        self.state = state
        self.setMouseTracking(True)
        self.setMinimumSize(320, 180)
        self._hover: Optional[float] = None
        self._plot = QtCore.QRectF()
        self._strip = QtCore.QRectF()
        self._xr = (1000.0, 5000.0)
        self._k: Optional[float] = None     # x units per rpm (road speed)
        self.show_power = True
        self.show_torque = True
        # without a state (tests, export): set directly
        self.curve: Optional[_dy.Curve] = None
        self.ref_curve: Optional[_dy.Curve] = None
        self.power_unit, self.torque_unit = _dy.DEFAULT_POWER, _dy.DEFAULT_TORQUE
        if state is not None:
            state.changed.connect(self.update)

    # -- data ---------------------------------------------------------------
    def _runs(self) -> List[Run]:
        st = self.state
        if st is not None:
            return [r for r in st.runs if r.curve is not None and r.curve.points]
        out = []
        for i, c in enumerate((self.curve, self.ref_curve)):
            if c is not None and c.points:
                out.append(Run("this" if i == 0 else "ref", c.label, "", i, c))
        return out

    def _units(self):
        st = self.state
        if st is not None:
            return st.power_unit, st.torque_unit
        return self.power_unit, self.torque_unit

    def _xaxis(self) -> Tuple[Optional[float], str]:
        """``(x units per rpm or None, the x axis's title)``."""
        st = self.state
        if st is None:
            return None, "rpm"
        k = st.speed_factor()
        if k is None:
            return None, "rpm"
        from . import dyno_speed as _sp
        unit = st.xaxis.get("unit", "kmh")
        return _sp.speed_in(k, unit), "%s in gear %d" % (
            _sp.speed_label(unit), st.xaxis.get("gear") or 0)

    def xv(self, rpm: float) -> float:
        return rpm * self._k if self._k else rpm

    @staticmethod
    def _nice(v: float) -> float:
        if v <= 0:
            return 1.0
        import math
        e = 10 ** math.floor(math.log10(v))
        for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
            if m * e >= v:
                return m * e
        return 10 * e

    # -- geometry ---------------------------------------------------------------
    def _x(self, rpm):
        a, b = self._xr
        r = self._plot
        return r.left() + (self.xv(rpm) - a) / max(1e-9, b - a) * r.width()

    def _rpm_at(self, x):
        a, b = self._xr
        r = self._plot
        v = a + (x - r.left()) / max(1.0, r.width()) * (b - a)
        return v / self._k if self._k else v

    # -- painting -----------------------------------------------------------------
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p):
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pal = self.palette()
        dark = _is_dark(self)
        ink = pal.color(QtGui.QPalette.WindowText)
        muted = QtGui.QColor(ink)
        muted.setAlpha(150)
        grid = QtGui.QColor(ink)
        grid.setAlpha(28)
        base = pal.color(QtGui.QPalette.Base)
        p.fillRect(self.rect(), base)
        runs = self._runs()
        pu, tu = self._units()
        fm = p.fontMetrics()
        if not runs:
            p.setPen(muted)
            msg = self.state.summary() if self.state is not None else ""
            p.drawText(self.rect().adjusted(12, 12, -12, -12),
                       QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap,
                       msg or "Nothing to show")
            return
        self._k, xtitle = self._xaxis()
        cur = next((r.curve for r in runs if r.key == "this"), None)
        left = fm.horizontalAdvance("0000") + 18
        right = fm.horizontalAdvance("0000") + 18
        # the legend: names at their own width, wrapping onto more rows
        avail = max(60.0, self.width() - left - right)
        items, row, x = [], 0, 0.0
        for run in runs:
            name = fm.elidedText(run.label, QtCore.Qt.ElideMiddle, int(avail - 22))
            w = fm.horizontalAdvance(name) + 30
            if x and x + w > avail:
                row, x = row + 1, 0.0
            items.append((run, name, x, row))
            x += w
        legend_h = (row + 1) * fm.height() + 4
        top = fm.height() + legend_h + 8
        bottom = fm.height() * 2 + self.STRIP_H + 12
        self._plot = QtCore.QRectF(left, top, max(10, self.width() - left - right),
                                   max(10, self.height() - top - bottom))
        r = self._plot
        self._strip = QtCore.QRectF(r.left(), r.bottom() + 4, r.width(), self.STRIP_H - 2)
        lo = min(self.xv(c.curve.points[0].rpm) for c in runs)
        hi = max(self.xv(c.curve.points[-1].rpm) for c in runs)
        self._xr = (lo, hi if hi > lo else lo + 1)
        pts = [pt for c in runs for pt in c.curve.points]
        pmax = self._nice(max(_dy.power_in(pt.power, pu) for pt in pts) * 1.08)
        tmax = self._nice(max(_dy.torque_in(pt.torque, tu) for pt in pts) * 1.08)
        # grid + axes (text in ink: the marks carry the colour)
        n = 5
        for i in range(n + 1):
            y = r.bottom() - i * r.height() / n
            p.setPen(QtGui.QPen(grid, 1))
            p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
            p.setPen(muted)
            if self.show_power:
                p.drawText(QtCore.QRectF(0, y - fm.height() / 2, left - 6, fm.height()),
                           QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                           "%g" % round(pmax * i / n, 1))
            if self.show_torque:
                p.drawText(QtCore.QRectF(r.right() + 6, y - fm.height() / 2, right - 6,
                                         fm.height()),
                           QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                           "%g" % round(tmax * i / n, 1))
        # x ticks
        import math
        span = hi - lo
        step = self._nice(span / 8.0) if span > 0 else 1.0
        p.setPen(muted)
        t = math.ceil(lo / step) * step
        while t <= hi + 1e-6:
            x = r.left() + (t - lo) / max(1e-9, hi - lo) * r.width()
            p.drawText(QtCore.QRectF(x - 30, self._strip.bottom() + 2, 60, fm.height()),
                       QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop, "%g" % round(t, 1))
            t += step
        p.drawText(QtCore.QRectF(r.left(), self._strip.bottom() + 2 + fm.height(),
                                 r.width(), fm.height()), QtCore.Qt.AlignHCenter, xtitle)
        # axis titles, with the line style each one is drawn in
        def sample(x, y, dashed):
            pen = QtGui.QPen(ink, 1.6)
            if dashed:
                pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(x + 18, y))
        yt = fm.height() / 2
        if self.show_power:
            sample(2, yt, False)
            p.setPen(ink)
            p.drawText(QtCore.QPointF(24, fm.ascent()), "Power (%s)" % _dy.power_label(pu))
        if self.show_torque:
            txt = "Torque (%s)" % _dy.torque_label(tu)
            w = fm.horizontalAdvance(txt)
            p.setPen(ink)
            p.drawText(QtCore.QPointF(self.width() - w - 2, fm.ascent()), txt)
            sample(self.width() - w - 24, yt, True)
        # legend: a swatch and a name per run
        for run, name, x, row in items:
            x += left
            ly = fm.height() + 4 + row * fm.height()
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(run_colour(run.slot, dark))
            if run.dots:
                p.drawEllipse(QtCore.QPointF(x + 6, ly + fm.height() / 2), 4, 4)
            else:
                p.drawRoundedRect(QtCore.QRectF(x, ly + fm.height() / 2 - 4, 12, 8), 2, 2)
            p.setPen(ink)
            p.drawText(QtCore.QPointF(x + 16, ly + fm.ascent()), name)

        def path_of(c, val, vmax):
            path = QtGui.QPainterPath()
            for i, pt in enumerate(c.points):
                xy = QtCore.QPointF(self._x(pt.rpm), r.bottom() - val(pt) / vmax * r.height())
                if i == 0:
                    path.moveTo(xy)
                else:
                    path.lineTo(xy)
            return path

        def line(c, val, vmax, colour, dashed, width):
            pen = QtGui.QPen(colour, width)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            if dashed:
                pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawPath(path_of(c, val, vmax))

        def dots(c, val, vmax, colour, hollow):
            for pt in c.points:
                xy = QtCore.QPointF(self._x(pt.rpm), r.bottom() - val(pt) / vmax * r.height())
                p.setPen(QtGui.QPen(colour if hollow else base, 1.6 if hollow else 1.5))
                p.setBrush(base if hollow else colour)
                p.drawEllipse(xy, 4, 4)

        pv = lambda pt: _dy.power_in(pt.power, pu)      # noqa: E731
        tv = lambda pt: _dy.torque_in(pt.torque, tu)    # noqa: E731
        # this file last, on top
        for run in sorted(runs, key=lambda x: x.key == "this"):
            c, col = run.curve, run_colour(run.slot, dark)
            width = 2.6 if run.key == "this" else 2.0
            if run.dots:
                if self.show_torque:
                    dots(c, tv, tmax, col, True)
                if self.show_power:
                    dots(c, pv, pmax, col, False)
                continue
            if self.show_torque:
                line(c, tv, tmax, col, True, width)
            if self.show_power:
                line(c, pv, pmax, col, False, width)
        # peaks of this file
        if cur is not None and cur.points:
            col = run_colour(0, dark)
            for show, peak, val, vmax, unit in (
                    (self.show_power, cur.peak_power(), pv, pmax, _dy.power_label(pu)),
                    (self.show_torque, cur.peak_torque(), tv, tmax, _dy.torque_label(tu))):
                if not show:
                    continue
                xy = QtCore.QPointF(self._x(peak.rpm), r.bottom() - val(peak) / vmax * r.height())
                p.setPen(QtGui.QPen(base, 2))
                p.setBrush(col)
                p.drawEllipse(xy, 4, 4)
                p.setPen(ink)
                txt = "%s %s" % (_num(val(peak), 1 if unit != "Nm" else 0), unit)
                w = fm.horizontalAdvance(txt)
                x0 = min(max(xy.x() - w / 2, r.left()), r.right() - w)
                p.drawText(QtCore.QPointF(x0, max(r.top() + fm.ascent(), xy.y() - 8)), txt)
            # the governing-limit strip
            half = (cur.points[1].rpm - cur.points[0].rpm) / 2 if len(cur.points) > 1 else 0
            first, last = cur.points[0].rpm, cur.points[-1].rpm
            for key, a, b in cur.runs():
                x0 = self._x(a - half) if a > first else self._x(first)
                x1 = self._x(b + half) if b < last else self._x(last)
                p.fillRect(QtCore.QRectF(x0, self._strip.top(), max(1.0, x1 - x0),
                                         self._strip.height()), limiter_colour(key))
        # hover
        if self._hover is not None and r.left() <= self._hover <= r.right():
            p.setPen(QtGui.QPen(muted, 1, QtCore.Qt.DotLine))
            p.drawLine(QtCore.QPointF(self._hover, r.top()),
                       QtCore.QPointF(self._hover, r.bottom()))
            lines = self._hover_lines(self._rpm_at(self._hover))
            if lines:
                w = max(fm.horizontalAdvance(s_) for s_ in lines) + 12
                h = fm.height() * len(lines) + 8
                x = self._hover + 10 if self._hover + 10 + w < r.right() else self._hover - 10 - w
                box = QtCore.QRectF(max(0.0, x), r.top() + 4, w, h)
                p.setPen(QtGui.QPen(grid, 1))
                p.setBrush(pal.color(QtGui.QPalette.ToolTipBase))
                p.drawRoundedRect(box, 4, 4)
                p.setPen(pal.color(QtGui.QPalette.ToolTipText))
                for i, s_ in enumerate(lines):
                    p.drawText(QtCore.QPointF(box.left() + 6,
                                              box.top() + 4 + fm.ascent() + i * fm.height()), s_)

    def _hover_lines(self, rpm) -> List[str]:
        pu, tu = self._units()
        out = []
        k, _t = self._xaxis()
        if k:
            from . import dyno_speed as _sp
            unit = self.state.xaxis.get("unit", "kmh")
            out.append("%d rpm  (%s %s)" % (rpm, _num(rpm * k, 0), _sp.speed_label(unit)))
        for run in self._runs():
            c = run.curve
            pt = min(c.points, key=lambda q: abs(q.rpm - rpm))
            if run.dots and abs(pt.rpm - rpm) > 150:
                continue
            name = run.label if len(run.label) <= 28 else run.label[:27] + "…"
            out.append("%s @ %d rpm: %s %s, %s %s" % (
                name, pt.rpm, _num(_dy.power_in(pt.power, pu)), _dy.power_label(pu),
                _num(_dy.torque_in(pt.torque, tu), 0), _dy.torque_label(tu)))
            if run.key == "this":
                lim = c.limiters.get(pt.limiter)
                out.append("   %s mg/stroke, limited by %s" % (
                    _num(pt.q), lim.label if lim else pt.limiter))
        return out

    # -- mouse ------------------------------------------------------------------
    def _this(self):
        return next((r.curve for r in self._runs() if r.key == "this"), None)

    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        self._hover = x if self._plot.left() <= x <= self._plot.right() else None
        cur = self._this()
        if self._strip.contains(ev.position()) and cur is not None and cur.points:
            key = self._strip_key(x)
            lim = cur.limiters.get(key)
            if lim is not None:
                QtWidgets.QToolTip.showText(
                    ev.globalPosition().toPoint(),
                    "%s -- click to open it%s" % (lim.label, (": " + lim.tip) if lim.tip else ""),
                    self)
        self.update()

    def leaveEvent(self, _ev):
        self._hover = None
        self.update()

    def _strip_key(self, x) -> Optional[str]:
        cur = self._this()
        if cur is None or not cur.points:
            return None
        rpm = self._rpm_at(x)
        pt = min(cur.points, key=lambda q: abs(q.rpm - rpm))
        return pt.limiter

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton and self._strip.contains(ev.position()):
            key = self._strip_key(ev.position().x())
            if key:
                self.limiterClicked.emit(key, self._rpm_at(ev.position().x()))
                return
        super().mousePressEvent(ev)


# ============================================================== controls
def _unit_combo(table, current) -> QtWidgets.QComboBox:
    cb = QtWidgets.QComboBox()
    for k, lbl, _f in table:
        cb.addItem(lbl, k)
    i = cb.findData(current)
    cb.setCurrentIndex(max(0, i))
    return cb


def _ref_combo(state: DynoState, with_versions: bool) -> QtWidgets.QComboBox:
    cb = QtWidgets.QComboBox()
    _fill_ref_combo(cb, state, with_versions)
    return cb


def ref_key(kind: str, version: Optional[str] = None) -> str:
    """A reference choice as combo-box data (a plain string: Qt matches
    strings, not Python tuples)."""
    return "%s:%s" % (kind, version) if kind in (_dy.REF_VERSION, _dy.REF_SOURCE) \
        else kind


def ref_of(key) -> Tuple[str, Optional[str]]:
    key = str(key or _dy.REF_ORIGINAL)
    kind, _sep, vid = key.partition(":")
    return kind, (vid or None) if kind in (_dy.REF_VERSION, _dy.REF_SOURCE) else None


def _cap_combo(cb: QtWidgets.QComboBox, chars: int = 22) -> None:
    """A combo no wider than ``chars`` characters, whatever its items say (a
    long version or file name shows in full in the list and the tooltip)."""
    cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
    cb.setMinimumContentsLength(chars)
    view = cb.view()
    view.setTextElideMode(QtCore.Qt.ElideMiddle)
    scr = cb.screen() or QtGui.QGuiApplication.primaryScreen()
    if scr is not None:                 # the list, too, stays on the screen
        view.setMaximumWidth(int(scr.availableGeometry().width() * 0.6))
    cb.currentIndexChanged.connect(lambda _i: cb.setToolTip(cb.currentText()))


def _fill_ref_combo(cb, state, with_versions):
    cb.blockSignals(True)
    cb.clear()
    cb.addItem(state.original_label(), ref_key(_dy.REF_ORIGINAL))
    cb.addItem("Comparison", ref_key(_dy.REF_COMPARISON))
    if with_versions:
        from . import history as _history
        for v in state.versions():
            cb.addItem("Version: %s %s" % (v.get("ts", ""), _history.display_label(v)),
                       ref_key(_dy.REF_VERSION, v.get("id")))
    elif state.ref_kind == _dy.REF_VERSION:
        cb.addItem("Version: %s" % state._version_label(state.ref_version),
                   ref_key(_dy.REF_VERSION, state.ref_version))
    for src in state.sources:           # the runs added (a log converts nothing)
        if src.kind != _src.LOG:
            cb.addItem(src.label, ref_key(_dy.REF_SOURCE, src.key))
    cb.addItem("None (this file only)", ref_key(_dy.REF_NONE))
    i = cb.findData(ref_key(state.ref_kind, state.ref_version))
    cb.setCurrentIndex(max(0, i))
    for j in range(cb.count()):
        cb.setItemData(j, cb.itemText(j), QtCore.Qt.ToolTipRole)
    cb.setToolTip(cb.currentText())
    cb.blockSignals(False)


def go_to_limiter(ws, state: DynoState, key: str):
    """Open the map behind limiter ``key`` in the tab's table."""
    m = state.model
    lim = m.limiters.get(key) if m is not None else None
    if lim is None or not lim.entities:
        return
    try:
        ws.show_entity(lim.entities[0])
    except Exception:
        pass


# =================================================================== panel
class DynoPanel(QtWidgets.QFrame):
    """The compact chart above the tab's table."""

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.state = state_for(ws)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(4, 2, 4, 4)
        v.setSpacing(2)
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)
        title = QtWidgets.QLabel("Dyno")
        title.setStyleSheet("font-weight: bold;")
        bar.addWidget(title)
        self.summary = QtWidgets.QLabel()
        self.summary.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                   QtWidgets.QSizePolicy.Preferred)
        self.summary.setMinimumWidth(0)
        bar.addWidget(self.summary, 1)
        bar.addWidget(QtWidgets.QLabel("Reference:"))
        self.ref_box = _ref_combo(self.state, with_versions=False)
        _cap_combo(self.ref_box, 16)    # a long version name never widens the tab
        bar.addWidget(self.ref_box)
        self.power_box = _unit_combo(_dy.POWER_UNITS, self.state.power_unit)
        self.torque_box = _unit_combo(_dy.TORQUE_UNITS, self.state.torque_unit)
        bar.addWidget(self.power_box)
        bar.addWidget(self.torque_box)
        more = QtWidgets.QToolButton()
        more.setText("Details...")
        more.setToolTip("The conditions, the reference, every engine speed, export")
        bar.addWidget(more)
        close = QtWidgets.QToolButton()
        close.setText("×")
        close.setAutoRaise(True)
        close.setToolTip("Hide the dyno chart (Ctrl+4)")
        bar.addWidget(close)
        v.addLayout(bar)
        self.chart = DynoChart(self.state, self)
        v.addWidget(self.chart, 1)
        self.power_box.currentIndexChanged.connect(
            lambda _i: self.state.set_units(power=self.power_box.currentData()))
        self.torque_box.currentIndexChanged.connect(
            lambda _i: self.state.set_units(torque=self.torque_box.currentData()))
        self.ref_box.currentIndexChanged.connect(self._ref_changed)
        more.clicked.connect(lambda: open_for(ws))
        close.clicked.connect(lambda: set_shown(ws, False))
        self.chart.limiterClicked.connect(
            lambda key, _rpm: go_to_limiter(ws, self.state, key))
        self.state.changed.connect(self._sync)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._tick)

    def _ref_changed(self, _i):
        kind, vid = ref_of(self.ref_box.currentData())
        self.state.set_reference(kind, vid)

    def _sync(self):
        st = self.state
        self.summary.setText(st.summary(compact=True))
        self.summary.setToolTip("%s\nReference: %s\n%s" % (st.summary(), st.ref_label, "\n".join(
            (st.curve.notes if st.curve is not None else []))))
        for box, val in ((self.power_box, st.power_unit), (self.torque_box, st.torque_unit)):
            i = box.findData(val)
            if i >= 0 and i != box.currentIndex():
                box.blockSignals(True)
                box.setCurrentIndex(i)
                box.blockSignals(False)
        _fill_ref_combo(self.ref_box, st, with_versions=False)

    def _tick(self):
        if self.isVisible():
            self.state.poll()

    def showEvent(self, ev):
        super().showEvent(ev)
        self._timer.start()
        self.state.poll()
        self._sync()

    def hideEvent(self, ev):
        self._timer.stop()
        super().hideEvent(ev)


def attach(ws, below: QtWidgets.QWidget) -> QtWidgets.QSplitter:
    """Put a dyno panel above ``below`` (the 3-D surface/table splitter);
    returns the splitter that holds both. Makes the edit bar's **Dyno**
    button (``ws.dyno_btn``) the panel's toggle."""
    split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
    split.setChildrenCollapsible(False)
    panel = DynoPanel(ws)
    panel.hide()
    split.addWidget(panel)
    split.addWidget(below)
    split.setStretchFactor(0, 0)
    split.setStretchFactor(1, 1)
    ws.dyno_panel = panel
    ws.dyno_split = split
    split.splitterMoved.connect(lambda *_a: _save_sizes(ws))
    btn = getattr(ws, "dyno_btn", None)
    if btn is not None:
        btn.setCheckable(True)
        btn.setToolTip("Show or hide the calculated dyno chart of this file "
                       "above the table  Ctrl+4")
        btn.toggled.connect(lambda on: set_shown(ws, on))
    if _truthy(_settings().value(SHOWN_KEY, False)) and availability(ws) is None:
        set_shown(ws, True, remember=False)
    return split


def is_shown(ws) -> bool:
    p = getattr(ws, "dyno_panel", None)
    return p is not None and not p.isHidden()


def set_shown(ws, on: bool, remember: bool = True):
    """Show or hide ``ws``'s dyno panel -- the toggle's one way in."""
    panel = getattr(ws, "dyno_panel", None)
    if panel is None:
        return None
    on = bool(on)
    if on != is_shown(ws):
        if on:
            panel.show()
            _restore_sizes(ws)
        else:
            panel.hide()
    btn = getattr(ws, "dyno_btn", None)
    if btn is not None and btn.isChecked() != on:
        btn.blockSignals(True)
        btn.setChecked(on)
        btn.blockSignals(False)
    if remember:
        s = _settings()
        s.setValue(SHOWN_KEY, on)
        s.sync()
    return panel


def _save_sizes(ws):
    if not is_shown(ws):
        return
    sizes = ws.dyno_split.sizes()
    if len(sizes) == 2 and all(x > 0 for x in sizes):
        s = _settings()
        s.setValue(SIZES_KEY, ",".join(str(int(x)) for x in sizes))
        s.sync()


def _restore_sizes(ws):
    raw = _settings().value(SIZES_KEY, "")
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(x) for x in raw)
    try:
        sizes = [int(x) for x in str(raw).split(",") if x.strip()]
    except ValueError:
        sizes = []
    split = ws.dyno_split
    total = sum(split.sizes()) or split.height()
    if len(sizes) != 2 or min(sizes) <= 0:
        sizes = [max(200, int(total * 0.4)), max(100, int(total * 0.6))]
    split.setSizes(sizes)


# ================================================================== window
_ROWS = (("rpm", "rpm"), ("power", "Power"), ("torque", "Torque"),
         ("q", "Quantity (mg)"), ("q_ref", "In the reference (mg)"),
         ("limiter", "Limited by"), ("boost", "Boost (hPa)"), ("air", "Air (mg)"))


def open_for(ws):
    """Open (or raise) tab ``ws``'s dyno window."""
    why = availability(ws)
    if why:
        QtWidgets.QMessageBox.information(ws, "Dyno chart", why)
        return None
    return ws.open_tool("dyno", lambda: DynoDialog(ws))


class DynoDialog(QtWidgets.QDialog):
    """The full dyno window: the chart, the reference and the other runs,
    the conditions, every engine speed, what changed, export."""

    def __init__(self, ws, parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.state = state_for(ws)
        self._filling = 0                   # > 0 while _sync fills the controls
        self._runs_sig = None
        self.setWindowTitle("Dyno chart")
        try:
            from . import gui
            self.setWindowIcon(gui.app_icon())
        except Exception:
            pass
        self._fit_to_screen(1180, 760)
        outer = QtWidgets.QHBoxLayout(self)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer.addWidget(split)
        # -- left: chart, summary, the tables ----------------------------------
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        top = QtWidgets.QHBoxLayout()
        self.power_box = _unit_combo(_dy.POWER_UNITS, self.state.power_unit)
        self.torque_box = _unit_combo(_dy.TORQUE_UNITS, self.state.torque_unit)
        self.show_p = QtWidgets.QCheckBox("Power")
        self.show_t = QtWidgets.QCheckBox("Torque")
        self.show_p.setChecked(True)
        self.show_t.setChecked(True)
        self.x_box = QtWidgets.QComboBox()
        self.x_box.addItem("Engine speed", "rpm")
        self.x_box.addItem("Road speed", "speed")
        self.x_box.setToolTip("Draw the runs against engine speed, or against road "
                              "speed in the gear chosen under Road speed")
        for w in (QtWidgets.QLabel("Units:"), self.power_box, self.torque_box,
                  self.show_p, self.show_t):
            top.addWidget(w)
        top.addStretch(1)
        lv.addLayout(top)
        self.chart = DynoChart(self.state, self)
        self.chart.setMinimumHeight(220)
        lv.addWidget(self.chart, 3)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.summary.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                   QtWidgets.QSizePolicy.Preferred)
        lv.addWidget(self.summary)
        self.legend = QtWidgets.QLabel()
        self.legend.setWordWrap(True)
        self.legend.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                  QtWidgets.QSizePolicy.Preferred)
        lv.addWidget(self.legend)
        self.table = QtWidgets.QTableWidget(0, len(_ROWS))
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setToolTip("Double-click a 'Limited by' cell to open that map")
        self.table.verticalHeader().setDefaultSectionSize(
            self.table.fontMetrics().height() + 6)
        # what changed against another run
        diffw = QtWidgets.QWidget()
        dv = QtWidgets.QVBoxLayout(diffw)
        dv.setContentsMargins(4, 4, 4, 0)
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(QtWidgets.QLabel("This file against:"))
        self.diff_box = QtWidgets.QComboBox()
        self.diff_box.setToolTip("The run to compare this file with")
        drow.addWidget(self.diff_box)
        drow.addStretch(1)
        dv.addLayout(drow)
        self.diff_note = QtWidgets.QLabel()
        self.diff_note.setWordWrap(True)
        self.diff_note.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                     QtWidgets.QSizePolicy.Preferred)
        dv.addWidget(self.diff_note)
        self.diff_table = QtWidgets.QTableWidget(0, len(_DIFF_COLS))
        self.diff_table.setHorizontalHeaderLabels([h for h in _DIFF_COLS])
        self.diff_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.diff_table.verticalHeader().setVisible(False)
        self.diff_table.setWordWrap(True)
        self.diff_table.horizontalHeader().setStretchLastSection(True)
        self.diff_table.setToolTip("Double-click a band to open the first map that "
                                   "differs there")
        dv.addWidget(self.diff_table, 1)
        # how the curve was worked out: a tab beside the table, where it has
        # room to be read (not squeezed under the settings)
        self.notes = QtWidgets.QLabel()
        self.notes.setWordWrap(True)
        self.notes.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.notes.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.notes.setContentsMargins(6, 6, 6, 6)
        sa = QtWidgets.QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(self.notes)
        sa.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.low = QtWidgets.QTabWidget()
        self.low.addTab(self.table, "Engine speeds")
        self.low.addTab(diffw, "What changed")
        self.low.addTab(sa, "How it is worked out")
        lv.addWidget(self.low, 2)
        split.addWidget(left)
        # -- right: reference, runs, conditions, engine, road speed --------------
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        refg = QtWidgets.QGroupBox("Reference")
        rf = QtWidgets.QFormLayout(refg)
        self.ref_box = _ref_combo(self.state, with_versions=True)
        self.ref_box.setToolTip("Turns every run's quantity into torque (its "
                                "torque-to-fuel and duration maps) and is drawn "
                                "beside them. Default: the original.")
        rf.addRow("Compare with:", self.ref_box)
        self.ref_note = _note_label()
        rf.addRow(self.ref_note)
        self.fuel_chk = QtWidgets.QCheckBox("Carry the quantity through the duration maps")
        self.fuel_chk.setToolTip(
            "Unit-injector engines: read this file's duration back through the "
            "reference's duration maps, so a rescaled quantity is not taken at face "
            "value. Off: the ECU's quantity is used as it is.")
        rf.addRow(self.fuel_chk)
        rv.addWidget(refg)
        # the other runs
        runsg = QtWidgets.QGroupBox("Other runs")
        rg = QtWidgets.QVBoxLayout(runsg)
        self.runs_list = QtWidgets.QListWidget()
        self.runs_list.setToolTip("Untick a run to hide it; right-click for more")
        self.runs_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.runs_list.setTextElideMode(QtCore.Qt.ElideMiddle)
        self.runs_list.setMaximumHeight(self.runs_list.fontMetrics().height() * 5 + 8)
        rg.addWidget(self.runs_list)
        rrow = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QToolButton()
        self.add_btn.setText("Add")
        self.add_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.add_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.add_btn.setToolTip("Another tab, a project's branch or version, a "
                                "version of this file, a file, or a data log")
        self.add_menu = QtWidgets.QMenu(self.add_btn)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        self.add_btn.setMenu(self.add_menu)
        self.pin_btn = QtWidgets.QPushButton("Pin this curve")
        self.pin_btn.setToolTip("Keep this file's curve as it is now, to edit "
                                "against (lasts until the tab closes)")
        self.rm_btn = QtWidgets.QPushButton("Remove")
        for w in (self.add_btn, self.pin_btn, self.rm_btn):
            rrow.addWidget(w)
        rrow.addStretch(1)
        rg.addLayout(rrow)
        rv.addWidget(runsg)
        run = QtWidgets.QGroupBox("Run")
        form = QtWidgets.QFormLayout(run)
        prow = QtWidgets.QHBoxLayout()
        self.preset_box = QtWidgets.QComboBox()
        self.preset_box.setToolTip("Set the conditions from a preset")
        self.preset_save = QtWidgets.QToolButton()
        self.preset_save.setText("Save...")
        self.preset_save.setToolTip("Keep these conditions as a preset of your own")
        self.preset_del = QtWidgets.QToolButton()
        self.preset_del.setText("Delete")
        self.preset_del.setToolTip("Delete the preset chosen (your own only)")
        prow.addWidget(self.preset_box, 1)
        prow.addWidget(self.preset_save)
        prow.addWidget(self.preset_del)
        form.addRow("Preset:", prow)
        self.group_box = QtWidgets.QComboBox()
        self.group_box.setToolTip("The dataset (the coding picks which the ECU uses)")
        form.addRow("Dataset:", self.group_box)
        self.wish_box = QtWidgets.QComboBox()
        self.wish_box.setToolTip("Which driver's-wish map asks for torque (EDC16 has "
                                 "one per gear)")
        form.addRow("Driver's wish:", self.wish_box)
        self.smoke_w = QtWidgets.QWidget()
        self.smoke_l = QtWidgets.QVBoxLayout(self.smoke_w)
        self.smoke_l.setContentsMargins(0, 0, 0, 0)
        form.addRow("Smoke limit:", self.smoke_w)
        self.smoke_chk: Dict[str, QtWidgets.QAbstractButton] = {}
        self.smoke_grp = QtWidgets.QButtonGroup(self)
        self.smoke_grp.setExclusive(True)

        def spin(lo, hi, step, dec, suffix, tip):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setSingleStep(step)
            sb.setDecimals(dec)
            sb.setSuffix(suffix)
            sb.setToolTip(tip)
            sb.setKeyboardTracking(False)
            return sb
        self.f_baro = spin(500, 1100, 10, 0, " hPa", "Atmospheric pressure")
        self.f_charge = spin(-20, 120, 5, 0, " °C", "Intake charge temperature")
        self.f_coolant = spin(-20, 120, 5, 0, " °C", "Coolant temperature")
        self.f_ve = spin(0.5, 1.2, 0.01, 2, "", "Volumetric efficiency: how much of "
                         "the cylinder fills at the boost pressure")
        self.f_cap = spin(0, 4000, 50, 0, " hPa", "The most boost the turbo makes "
                          "(absolute). 0: it reaches the target")
        self.f_eff = spin(0.2, 0.55, 0.01, 2, "", "Brake efficiency for the "
                          "fuel-energy model (EDC15, or no conversion map)")
        self.f_loss = spin(0, 40, 1, 0, " %", "Drivetrain loss: 0 shows the crank, "
                           "more shows the wheels")
        self.f_disp = spin(0, 10, 0.1, 2, " l", "Engine displacement; 0 = the file's")
        self.f_cyl = QtWidgets.QSpinBox()
        self.f_cyl.setRange(0, 12)
        self.f_cyl.setToolTip("Cylinders; 0 = the file's")
        self.f_ob = QtWidgets.QCheckBox("Overboost")
        self.f_ob.setToolTip("Apply the overboost torque limit in full. The ECU "
                             "allows it only for a limited time; off shows the "
                             "steady limit. Only where the file calibrates one.")
        form.addRow("", self.f_ob)
        for lbl, w in (("Air pressure:", self.f_baro), ("Charge temperature:", self.f_charge),
                       ("Coolant:", self.f_coolant), ("Volumetric efficiency:", self.f_ve),
                       ("Boost the turbo makes:", self.f_cap),
                       ("Efficiency (fuel energy):", self.f_eff),
                       ("Drivetrain loss:", self.f_loss)):
            form.addRow(lbl, w)
        rv.addWidget(run)
        eng = QtWidgets.QGroupBox("Engine")
        ef = QtWidgets.QFormLayout(eng)
        ef.addRow("Displacement:", self.f_disp)
        ef.addRow("Cylinders:", self.f_cyl)
        self.eng_note = _note_label()
        ef.addRow(self.eng_note)
        rv.addWidget(eng)
        spd = QtWidgets.QGroupBox("Road speed")
        sf = QtWidgets.QFormLayout(spd)
        sf.addRow("Draw across:", self.x_box)
        grow = QtWidgets.QHBoxLayout()
        self.gearbox_box = QtWidgets.QComboBox()
        _cap_combo(self.gearbox_box, 16)
        self.gb_new = QtWidgets.QToolButton()
        self.gb_new.setText("New...")
        self.gb_new.setToolTip("A gearbox of your own: its gear ratios and final "
                               "drive (starting from the one chosen)")
        self.gb_edit = QtWidgets.QToolButton()
        self.gb_edit.setText("Edit...")
        self.gb_edit.setToolTip("Change or delete the gearbox of your own chosen")
        grow.addWidget(self.gearbox_box, 1)
        grow.addWidget(self.gb_new)
        grow.addWidget(self.gb_edit)
        sf.addRow("Gearbox:", grow)
        self.tyre_edit = QtWidgets.QLineEdit()
        self.tyre_edit.setPlaceholderText("225/45 R17, or 1.93 (m)")
        sf.addRow("Tyre:", self.tyre_edit)
        self.gear_box = QtWidgets.QComboBox()
        self.gear_box.setToolTip("The gear the road speed is worked out in")
        sf.addRow("Gear:", self.gear_box)
        self.f_k = spin(0, 200, 0.5, 1, "", "Road speed per 1000 rpm in this gear "
                        "(km/h): your own figure. 0: the gearbox's")
        self.f_k.setSpecialValueText("the gearbox's")
        sf.addRow("km/h per 1000 rpm:", self.f_k)
        self.speed_unit = QtWidgets.QComboBox()
        from . import dyno_speed as _sp
        for k, lbl, _f in _sp.SPEED_UNITS:
            self.speed_unit.addItem(lbl, k)
        sf.addRow("Show in:", self.speed_unit)
        self.speed_note = _note_label()
        sf.addRow(self.speed_note)
        rv.addWidget(spd)
        rv.addStretch(1)
        # the settings scroll when the window is short: their height is not
        # the window's minimum
        rscroll = QtWidgets.QScrollArea()
        rscroll.setWidgetResizable(True)
        rscroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        # (sideways only on a screen too narrow for the column)
        rscroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        rscroll.setWidget(right)
        self._side = rscroll
        side = QtWidgets.QWidget()
        sv = QtWidgets.QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(rscroll, 1)
        btns = QtWidgets.QHBoxLayout()
        csv_b = QtWidgets.QPushButton("Export CSV...")
        png_b = QtWidgets.QPushButton("Save image...")
        copy_b = QtWidgets.QPushButton("Copy image")
        for b in (csv_b, png_b, copy_b):
            btns.addWidget(b)
        sv.addLayout(btns)
        split.addWidget(side)
        for cb in (self.ref_box, self.group_box, self.wish_box, self.diff_box):
            _cap_combo(cb)
        _cap_combo(self.preset_box, 14)
        self._fit_side()
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        # -- wiring ----------------------------------------------------------------
        self.power_box.currentIndexChanged.connect(
            lambda _i: self._filling or self.state.set_units(
                power=self.power_box.currentData()))
        self.torque_box.currentIndexChanged.connect(
            lambda _i: self._filling or self.state.set_units(
                torque=self.torque_box.currentData()))
        self.show_p.toggled.connect(self._show_toggled)
        self.show_t.toggled.connect(self._show_toggled)
        self.x_box.currentIndexChanged.connect(self._x_changed)
        self.ref_box.currentIndexChanged.connect(self._ref_changed)
        self.fuel_chk.toggled.connect(lambda on: self._set(
            fuel_basis="duration" if on else "quantity"))
        self.group_box.currentIndexChanged.connect(self._group_changed)
        self.wish_box.currentIndexChanged.connect(
            lambda _i: self._set(wish=self.wish_box.currentData() or ""))
        for sb, key in ((self.f_baro, "baro"), (self.f_charge, "charge_temp"),
                        (self.f_coolant, "coolant"), (self.f_ve, "ve"),
                        (self.f_eff, "efficiency"), (self.f_loss, "loss")):
            sb.valueChanged.connect(lambda v, k=key: self._set(**{k: float(v)}))
        self.f_cap.valueChanged.connect(lambda v: self._set(boost_cap=float(v) or None))
        self.f_ob.toggled.connect(lambda on: self._set(overboost=bool(on)))
        self.f_disp.valueChanged.connect(lambda v: self._set(displacement=float(v) or None))
        self.f_cyl.valueChanged.connect(lambda v: self._set(cylinders=int(v) or None))
        self.preset_box.activated.connect(self._preset_chosen)
        self.preset_box.currentIndexChanged.connect(lambda _i: self._sync_preset_buttons())
        self.preset_save.clicked.connect(self._save_preset)
        self.preset_del.clicked.connect(self._delete_preset)
        self.runs_list.itemChanged.connect(self._run_ticked)
        self.runs_list.customContextMenuRequested.connect(self._runs_menu)
        self.runs_list.currentRowChanged.connect(lambda _r: self._sync_run_buttons())
        self.pin_btn.clicked.connect(self._pin)
        self.rm_btn.clicked.connect(self._remove_run)
        self.diff_box.currentIndexChanged.connect(lambda _i: self._filling or self._fill_diff())
        self.diff_table.cellDoubleClicked.connect(self._diff_clicked)
        self.gear_box.currentIndexChanged.connect(self._gear_changed)
        self.gearbox_box.currentIndexChanged.connect(self._gearbox_changed)
        self.gb_new.clicked.connect(lambda: self._edit_gearbox(new=True))
        self.gb_edit.clicked.connect(lambda: self._edit_gearbox())
        self.tyre_edit.editingFinished.connect(self._tyre_changed)
        self.f_k.valueChanged.connect(self._k_changed)
        self.speed_unit.currentIndexChanged.connect(
            lambda _i: self._filling or self.state.set_xaxis(
                unit=self.speed_unit.currentData()))
        self.table.cellDoubleClicked.connect(self._cell_clicked)
        self.chart.limiterClicked.connect(
            lambda key, _rpm: go_to_limiter(ws, self.state, key))
        csv_b.clicked.connect(self._export_csv)
        png_b.clicked.connect(self._save_png)
        copy_b.clicked.connect(self._copy_image)
        self.state.changed.connect(self._sync)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(lambda: self.isVisible() and self.state.poll())
        self._timer.start()
        self._fill_presets()
        self.state.poll()
        self._sync()

    def _set(self, **kw):
        if not self._filling:
            self.state.set_condition(**kw)

    def _show_toggled(self, _on=False):
        self.chart.show_power = self.show_p.isChecked()
        self.chart.show_torque = self.show_t.isChecked()
        self.chart.update()

    def _ref_changed(self, _i):
        if self._filling:
            return
        kind, vid = ref_of(self.ref_box.currentData())
        self._busy(lambda: self.state.set_reference(kind, vid))

    def _group_changed(self, _i):
        if not self._filling:
            self.state.set_group(self.group_box.currentData())

    def refresh_from_session(self):
        """An edit or undo elsewhere (the tab calls this)."""
        self.state.poll()

    def _busy(self, fn):
        """``fn`` with the wait cursor (a run read from a file loads it)."""
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            return fn()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    # -- the other runs -------------------------------------------------------------
    def _add(self, src):
        if self._busy(lambda: self.state.add_source(src)) is None:
            QtWidgets.QMessageBox.information(
                self, "Dyno chart", "The chart shows at most %d other runs; remove "
                "one first." % _src.MAX_SOURCES)

    def _fill_add_menu(self):
        m = self.add_menu
        m.clear()
        st = self.state
        full = len(st.sources) >= _src.MAX_SOURCES
        win = getattr(self.ws, "win", None)
        own = m.addMenu("This file")
        from . import history as _history
        own.addAction(st.original_label(), lambda: self._add(_src.Source(
            _src.VERSION, st.original_label(), "This file's original",
            vid=_history.ORIGINAL)))
        vs = st.versions()
        if vs:
            vm = own.addMenu("Version")
            for v in vs[:40]:
                lbl = "%s %s" % (v.get("ts", ""), _history.display_label(v))
                vm.addAction(_menu_text(lbl), lambda v=v, lbl=lbl: self._add(
                    _src.Source(_src.VERSION, "Version " + lbl, "This file, version "
                                + lbl, vid=v.get("id"))))
        tm = m.addMenu("Open tab")
        tabs = _src.tab_choices(win, self.ws)
        for lbl, t in tabs:
            tm.addAction(_menu_text(lbl), lambda t=t: self._add(_src.make_tab(t)))
        tm.setEnabled(bool(tabs))
        pm = m.addMenu("Open project")
        projs = _src.project_choices(win)
        for title, path, proj in projs:
            sub = pm.addMenu(_menu_text(title))
            for s in proj.sets:
                ssub = sub.addMenu(_menu_text(s.name))
                for b in s.branches:
                    bsub = ssub.addMenu(_menu_text(b.name))
                    lbl = "%s ▸ %s · %s" % (title, s.name, b.name)
                    bsub.addAction("Its newest version", lambda path=path, s=s, b=b, lbl=lbl:
                                   self._add(_src.Source(_src.PROJECT, lbl,
                                                         "Project %s (saved)" % lbl,
                                                         path=path, sid=s.id, bid=b.id)))
                    bsub.addSeparator()
                    for c in _src.branch_versions(proj, s.id, b.id)[:30]:
                        cl = _src.checkpoint_label(c)
                        bsub.addAction(_menu_text(cl), lambda path=path, s=s, b=b, c=c,
                                       lbl=lbl, cl=cl: self._add(_src.Source(
                                           _src.PROJECT, "%s (%s)" % (lbl, cl),
                                           "Project %s, version %s" % (lbl, cl),
                                           path=path, sid=s.id, bid=b.id, cid=c.id)))
        pm.setEnabled(bool(projs))
        m.addAction("File...", self._add_file)
        m.addSeparator()
        lm = m.addMenu("Data log")
        logs = _src.log_choices(win)
        for lbl, path, lid in logs[:40]:
            lm.addAction(_menu_text(lbl), lambda lbl=lbl, path=path, lid=lid: self._add(
                _src.Source(_src.LOG, "Log " + lbl.split(": ", 1)[-1], "Data log " + lbl,
                            path=path, log_id=lid)))
        if logs:
            lm.addSeparator()
        lm.addAction("Log file...", self._add_log_file)
        for a in m.actions():
            a.setEnabled(a.isEnabled() and not full)

    def _add_file(self):
        from .recent import get_last_dir, set_last_dir
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add a run from a file", get_last_dir(), "All files (*)")
        if path:
            set_last_dir(path)
            import os
            name = os.path.basename(path)
            self._add(_src.Source(_src.FILE, name, "File " + path, path=path))

    def _add_log_file(self):
        from .recent import get_last_dir
        path, _f = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add a data log", get_last_dir("log"),
            "Logs (*.csv *.log *.txt);;All files (*)")
        if path:
            import os
            self._add(_src.Source(_src.LOG, "Log " + os.path.basename(path),
                                  "Data log " + path, path=path))

    def _pin(self):
        if self.state.pin() is None:
            QtWidgets.QMessageBox.information(
                self, "Dyno chart", "The chart shows at most %d other runs; remove "
                "one first." % _src.MAX_SOURCES)

    def _current_source(self):
        it = self.runs_list.currentItem()
        return self.state.source(it.data(QtCore.Qt.UserRole)) if it is not None else None

    def _remove_run(self):
        src = self._current_source()
        if src is not None:
            self.state.remove_source(src.key)

    def _run_ticked(self, it):
        if self._filling:
            return
        self.state.show_source(it.data(QtCore.Qt.UserRole),
                               it.checkState() == QtCore.Qt.Checked)

    def _runs_menu(self, pos):
        it = self.runs_list.itemAt(pos)
        src = self.state.source(it.data(QtCore.Qt.UserRole)) if it is not None else None
        if src is None:
            return
        m = QtWidgets.QMenu(self)
        a = m.addAction("Use as the reference", lambda: self._busy(
            lambda: self.state.set_reference(_dy.REF_SOURCE, src.key)))
        a.setEnabled(src.kind != _src.LOG)
        m.addAction("Compare with this file (What changed)", lambda: self._diff_with(src.key))
        if src.kind == _src.PINNED:
            m.addAction("Rename...", lambda: self._rename(src))
        m.addSeparator()
        m.addAction("Remove", lambda: self.state.remove_source(src.key))
        m.exec(self.runs_list.viewport().mapToGlobal(pos))

    def _rename(self, src):
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename run", "Name:",
                                                  text=src.label)
        if ok and name.strip():
            src.label = name.strip()
            self.state.invalidate()

    def _diff_with(self, key):
        i = self.diff_box.findData(key)
        if i >= 0:
            self.diff_box.setCurrentIndex(i)
        self.low.setCurrentIndex(1)

    def _sync_run_buttons(self):
        self.rm_btn.setEnabled(self._current_source() is not None)
        self.pin_btn.setEnabled(len(self.state.sources) < _src.MAX_SOURCES)

    def _fill_runs(self):
        st = self.state
        whys = {r.key: r.why for r in st.runs}
        sig = tuple((x.key, x.label, x.shown, x.slot, whys.get(x.key, ""),
                     st.ref_kind == _dy.REF_SOURCE and st.ref_version == x.key)
                    for x in st.sources)
        if sig == self._runs_sig:
            return
        self._runs_sig = sig
        cur = self._current_source()
        self.runs_list.clear()
        dark = _is_dark(self)
        for x in st.sources:
            is_ref = st.ref_kind == _dy.REF_SOURCE and st.ref_version == x.key
            why = whys.get(x.key, "")
            it = QtWidgets.QListWidgetItem(x.label + ("  (the reference)" if is_ref else "")
                                           + (" -- " + why if why else ""))
            it.setData(QtCore.Qt.UserRole, x.key)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if x.shown else QtCore.Qt.Unchecked)
            it.setIcon(_swatch(run_colour(1 if is_ref else x.slot, dark)))
            it.setToolTip(x.detail + ("\n" + why if why else ""))
            self.runs_list.addItem(it)
            if cur is not None and cur.key == x.key:
                self.runs_list.setCurrentItem(it)
        self._sync_run_buttons()

    # -- presets ---------------------------------------------------------------
    def _fill_presets(self):
        from . import dyno_speed as _sp
        self.preset_box.blockSignals(True)
        self.preset_box.clear()
        self.preset_box.addItem("Choose...", None)
        for name, fields in _sp.PRESETS:
            self.preset_box.addItem(name, ("builtin", name))
        users = _sp.user_presets()
        if users:
            self.preset_box.insertSeparator(self.preset_box.count())
            for name, fields in users:
                self.preset_box.addItem(name, ("user", name))
        self.preset_box.setCurrentIndex(0)
        self.preset_box.blockSignals(False)
        self._sync_preset_buttons()

    def _sync_preset_buttons(self):
        d = self.preset_box.currentData()
        self.preset_del.setEnabled(bool(d) and d[0] == "user")

    def _preset_chosen(self, i):
        from . import dyno_speed as _sp
        d = self.preset_box.itemData(i)
        if not d:
            return
        items = _sp.PRESETS if d[0] == "builtin" else _sp.user_presets()
        fields = next((f for n, f in items if n == d[1]), None)
        if fields:
            self.state.set_condition(**dict(fields))

    def _save_preset(self):
        from . import dyno_speed as _sp
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Save preset", "Name for these conditions:")
        name = name.strip()
        if not ok or not name:
            return
        items = [(n, f) for n, f in _sp.user_presets() if n != name]
        items.append((name, _sp.preset_of(self.state.cond)))
        _sp.save_user_presets(items)
        self._fill_presets()
        self.preset_box.setCurrentIndex(self.preset_box.findText(name))

    def _delete_preset(self):
        from . import dyno_speed as _sp
        d = self.preset_box.currentData()
        if not d or d[0] != "user":
            return
        _sp.save_user_presets([(n, f) for n, f in _sp.user_presets() if n != d[1]])
        self._fill_presets()

    # -- road speed --------------------------------------------------------------
    def _x_changed(self, _i):
        if self._filling:
            return
        mode = self.x_box.currentData()
        st = self.state
        kw = {"mode": mode}
        if mode == "speed" and not st.xaxis.get("gear"):
            gears = st.gear_speeds()[0]
            kw["gear"] = 4 if 4 in gears else (max(gears) if gears else 4)
        st.set_xaxis(**kw)

    def _gear_changed(self, _i):
        if not self._filling and self.gear_box.currentData():
            self.state.set_xaxis(gear=int(self.gear_box.currentData()))

    def _k_changed(self, v):
        if self._filling:
            return
        g = self.state.xaxis.get("gear") or 0
        if g:
            base = self.state.gear_speeds()[0].get(g)
            same = base is not None and abs(float(v) - base) < 0.05
            self.state.set_gear_factor(g, None if (not v or same) else float(v))

    def _gearbox_changed(self, _i):
        if not self._filling:
            self.state.set_xaxis(gearbox=self.gearbox_box.currentData() or "")

    def _tyre_changed(self):
        if not self._filling:
            self.state.set_xaxis(tyre=self.tyre_edit.text().strip())

    def _fill_gearboxes(self):
        from . import dyno_speed as _sp
        st = self.state
        cb = self.gearbox_box
        cb.blockSignals(True)
        cb.clear()
        cb.addItem("Choose...", "")
        if not getattr(st.ws.primary, "is_edc15", False):
            cb.addItem("The ECU's gear model (EDC16)", _sp.ECU)
        users = _sp.user_gearboxes()
        if users:
            cb.insertSeparator(cb.count())
            for g in users:
                cb.addItem(g["name"], g["id"])
        cb.insertSeparator(cb.count())
        for g in _sp.catalogue():
            cb.addItem(g["name"], g["id"])
            tip = "%s\nGears %s, final drive %s%s" % (
                g["name"], " / ".join("%g" % x for x in g["gears"]),
                " / ".join("%g" % x for x in g["finals"]),
                ("\n" + g["note"]) if g.get("note") else "")
            if g.get("source"):
                tip += "\nSource: %s" % g["source"]
            cb.setItemData(cb.count() - 1, tip, QtCore.Qt.ToolTipRole)
        i = cb.findData(st.xaxis.get("gearbox") or "")
        cb.setCurrentIndex(max(0, i))
        cb.blockSignals(False)
        cb.setToolTip(cb.itemData(cb.currentIndex(), QtCore.Qt.ToolTipRole)
                      or "The car's gearbox: its gear ratios and final drive")
        gb = _sp.gearbox(cb.currentData() or "")
        self.gb_edit.setEnabled(bool(gb and gb.get("user")))

    def _edit_gearbox(self, new=False):
        from . import dyno_speed as _sp
        gid = self.gearbox_box.currentData() or ""
        cur = _sp.gearbox(gid) if gid else None
        mine = cur if (cur and cur.get("user") and not new) else None
        dlg = GearboxDialog(self, mine, template=cur)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        items = [g for g in _sp.user_gearboxes() if g["id"] != dlg.result_gb["id"]]
        if dlg.deleted:
            _sp.save_user_gearboxes(items)
            self.state.set_xaxis(gearbox="")
        else:
            items.append(dlg.result_gb)
            _sp.save_user_gearboxes(items)
            self.state.set_xaxis(gearbox=dlg.result_gb["id"])

    def _sync_speed(self):
        from . import dyno_speed as _sp
        st = self.state
        self._fill_gearboxes()
        if not self.tyre_edit.hasFocus():
            self.tyre_edit.setText(st.xaxis.get("tyre", ""))
        circ = _sp.tyre_circ(st.xaxis.get("tyre", ""))
        self.tyre_edit.setToolTip("Tyre size (225/45 R17) or rolling circumference "
                                  "in metres%s" % (
                                      ": %.3f m rolling" % circ if circ else ""))
        gears, where = st.gear_speeds()
        g = st.xaxis.get("gear") or 0
        top = max([6] + list(gears) + list(st.xaxis["k"]))
        self.gear_box.clear()
        for n in range(1, top + 1):
            k = st.gear_factor(n)
            self.gear_box.addItem("%d  (%s)" % (n, "%.1f km/h" % k if k else "?"), n)
        if g:
            self.gear_box.setCurrentIndex(max(0, self.gear_box.findData(g)))
        user = st.xaxis["k"].get(g)
        self.f_k.setValue(user or (gears.get(g) or 0.0))
        self.speed_unit.setCurrentIndex(max(0, self.speed_unit.findData(
            st.xaxis.get("unit", "kmh"))))
        self.x_box.setCurrentIndex(max(0, self.x_box.findData(st.xaxis.get("mode", "rpm"))))
        if st.xaxis.get("mode") == "speed" and st.speed_factor() is None:
            note = "No road speed for gear %d: %s." % (g, where)
        elif user:
            note = "Your figure for gear %d (%s gives %s)." % (
                g, where, ("%.1f" % gears[g]) if g in gears else "none")
        elif gears:
            note = "From %s." % where
        else:
            note = where[:1].upper() + where[1:] + "."
        _set_note(self.speed_note, note)


    # -- what changed ------------------------------------------------------------
    def _fill_diff_box(self):
        st = self.state
        keep = self.diff_box.currentData()
        self.diff_box.blockSignals(True)
        self.diff_box.clear()
        for r in st.runs:
            if r.key != "this" and r.curve is not None and r.curve.points:
                lbl = st.ref_label if r.key == "ref" else r.label
                self.diff_box.addItem(_menu_text(("Reference: " if r.key == "ref" else "")
                                                 + lbl), r.key)
        i = self.diff_box.findData(keep)
        self.diff_box.setCurrentIndex(i if i >= 0 else 0)
        self.diff_box.blockSignals(False)

    def _fill_diff(self):
        from . import dyno_diff as _dd
        st = self.state
        key = self.diff_box.currentData()
        a = next((r for r in st.runs if r.key == "this"), None)
        b = next((r for r in st.runs if r.key == key), None)
        self.diff_table.setRowCount(0)
        self._bands = []
        if a is None or b is None or a.curve is None or b.curve is None:
            _set_note(self.diff_note, "Add a run, or choose a reference, to compare "
                      "this file with.")
            return
        try:
            bands = _dd.bands(a.curve, b.curve, a.session, b.session)
        except Exception as ex:
            _set_note(self.diff_note, "Could not compare them: %s" % ex)
            return
        self._bands = bands
        pu, tu = st.power_unit, st.torque_unit
        _set_note(self.diff_note, _dd.summary(bands, a.curve, b.curve))
        self.diff_table.setRowCount(len(bands))
        for r, bd in enumerate(bands):
            la = a.curve.limiters.get(bd.limit_a)
            lb = b.curve.limiters.get(bd.limit_b)
            vals = ["%d-%d" % (bd.rpm_from, bd.rpm_to) if bd.rpm_to > bd.rpm_from
                    else "%d" % bd.rpm_from,
                    la.label if la else bd.limit_a,
                    (lb.label if lb else bd.limit_b) or "-",
                    "%+.1f" % _dy.power_in(bd.d_power, pu),
                    "%+.0f" % _dy.torque_in(bd.d_torque, tu),
                    "%+.1f" % bd.d_q,
                    "; ".join(ch.text() for ch in bd.maps) or "-"]
            for c, txt in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(txt)
                if 3 <= c <= 5:
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if c == 6 and bd.maps:
                    it.setToolTip("\n".join(ch.text() for ch in bd.maps))
                self.diff_table.setItem(r, c, it)
        self.diff_table.setHorizontalHeaderLabels([
            "rpm", "This file limited by", "Other limited by",
            "Δ power (%s)" % _dy.power_label(pu), "Δ torque (%s)" % _dy.torque_label(tu),
            "Δ quantity (mg)", "Maps behind those limits that differ"])
        self.diff_table.resizeColumnsToContents()
        self.diff_table.resizeRowsToContents()

    def _diff_clicked(self, r, _c):
        bands = getattr(self, "_bands", [])
        if 0 <= r < len(bands) and bands[r].maps:
            try:
                self.ws.show_entity(bands[r].maps[0].entity)
            except Exception:
                pass

    # -- display ------------------------------------------------------------------
    def _sync(self):
        st = self.state
        self._filling += 1                  # a count: _sync can nest
        try:
            for box, val in ((self.power_box, st.power_unit),
                             (self.torque_box, st.torque_unit)):
                i = box.findData(val)
                if i >= 0:
                    box.setCurrentIndex(i)
            _fill_ref_combo(self.ref_box, st, with_versions=True)
            self.fuel_chk.setChecked(st.cond.fuel_basis == "duration")
            m = st.model
            self.group_box.clear()
            for x in st.models:
                self.group_box.addItem(x.label, x.group)
            if m is not None:
                self.group_box.setCurrentIndex(max(0, self.group_box.findData(m.group)))
            self.group_box.setEnabled(len(st.models) > 1)
            self.wish_box.clear()
            for k, lbl in (m.wish_choices if m is not None else []):
                self.wish_box.addItem(lbl, k)
            self.wish_box.setCurrentIndex(max(0, self.wish_box.findData(st.cond.wish)))
            self.wish_box.setEnabled(self.wish_box.count() > 1)
            self._sync_smoke(m)
            c = st.cond
            for sb, val in ((self.f_baro, c.baro), (self.f_charge, c.charge_temp),
                            (self.f_coolant, c.coolant), (self.f_ve, c.ve),
                            (self.f_eff, c.efficiency), (self.f_loss, c.loss),
                            (self.f_cap, c.boost_cap or 0.0),
                            (self.f_disp, c.displacement or 0.0)):
                sb.setValue(val)
            self.f_cyl.setValue(c.cylinders or 0)
            self.f_ob.setChecked(bool(getattr(c, "overboost", False)))
            self.f_ob.setEnabled(bool(m is not None and m.has_overboost))
            if m is not None:
                _set_note(self.eng_note, "This file: %s l, %s cylinders (%s)." % (
                    "%g" % m.displacement if m.displacement else "?",
                    m.cylinders or "?", m.engine_source or "the definition"))
            # only the fuel-energy model reads it: neither file converts
            rm = st.ref_model
            self.f_eff.setEnabled(m is None or not (
                m.has_conversion or (rm is not None and rm.has_conversion)))
            self._fill_runs()
            self._sync_speed()
            self._fill_diff_box()
        finally:
            self._filling -= 1
        self._fit_side()
        _set_note(self.ref_note, "Using: %s" % st.ref_label if st.ref_label else "")
        self.summary.setText("<b>%s</b>" % st.summary() if st.curve else st.summary())
        self._fill_legend()
        self._fill_table()
        self._fill_diff()
        notes = []
        if st.curve is not None:
            notes.append("Torque from: %s." % st.curve.torque_source
                         if st.curve.torque_source else "")
            notes += st.curve.notes
        for r in st.runs:
            if r.key not in ("this", "ref") and r.curve is not None:
                notes.append("%s: %s%s." % (r.label, r.curve.torque_source,
                                             ("; " + "; ".join(r.curve.notes))
                                             if r.dots and r.curve.notes else ""))
            elif r.why:
                notes.append("%s: not shown, %s." % (r.label, r.why))
        if len(st.runs) > 1:
            notes.append("Every run is read through the reference's conversion, at "
                         "these conditions, so they compare like for like.")
        notes.append("This is what the calibration asks for at full load, not a "
                     "measurement: the engine makes it only if the turbo, injectors "
                     "and fuelling deliver it. A data log shows the quantity the ECU "
                     "delivered, converted the same way.")
        self.notes.setText("<br>".join("• " + _html(n) for n in notes if n))

    def _sync_smoke(self, m):
        """One radio button per smoke limiter the file has: the ECU reads
        exactly one, as its switches select; the default is that one."""
        for cb in self.smoke_chk.values():
            self.smoke_grp.removeButton(cb)
            cb.setParent(None)
        self.smoke_chk = {}
        if m is None:
            return
        on = set(m.smoke_on(self.state.cond))
        defaults = {k for k, _l, d in m.smoke_choices if d}
        for k, lbl, _d in m.smoke_choices:
            rb = QtWidgets.QRadioButton(lbl + ("  (the file's switch)" if k in defaults else ""))
            rb.setChecked(k in on)
            rb.toggled.connect(self._smoke_changed)
            self.smoke_grp.addButton(rb)
            self.smoke_l.addWidget(rb)
            self.smoke_chk[k] = rb
        if not m.smoke_choices:
            lbl = QtWidgets.QRadioButton("none calibrated")
            lbl.setEnabled(False)
            self.smoke_l.addWidget(lbl)
            self.smoke_chk["_none"] = lbl

    def _smoke_changed(self, on=False):
        if self._filling or not on:
            return
        self._set(smoke=tuple(k for k, cb in self.smoke_chk.items()
                              if k != "_none" and cb.isChecked()))

    def _fill_legend(self):
        c = self.state.curve
        if c is None or not c.points:
            self.legend.setText("")
            return
        used = []
        for key, _a, _b in c.runs():
            if key not in used:
                used.append(key)
        parts = []
        for key in used:
            lim = c.limiters.get(key)
            parts.append('<span style="color:%s">&#9632;</span> %s' % (
                LIMITER_COLOURS.get(key, "#B4B2A9"), _html(lim.label if lim else key)))
        self.legend.setText("Limited by: " + "&nbsp;&nbsp; ".join(parts)
                            + "  <i>(click the strip to open a map)</i>")

    def _fill_table(self):
        st = self.state
        c = st.curve
        pu, tu = st.power_unit, st.torque_unit
        heads = [h for _k, h in _ROWS]
        heads[1] = "Power (%s)" % _dy.power_label(pu)
        heads[2] = "Torque (%s)" % _dy.torque_label(tu)
        self.table.setHorizontalHeaderLabels(heads)
        pts = c.points if c is not None else []
        self.table.setRowCount(len(pts))
        for r, p in enumerate(pts):
            lim = c.limiters.get(p.limiter)
            vals = ["%d" % p.rpm, _num(_dy.power_in(p.power, pu)),
                    _num(_dy.torque_in(p.torque, tu), 0), _num(p.q),
                    "" if p.q_ref is None else _num(p.q_ref) + (" *" if p.extrapolated else ""),
                    lim.label if lim else p.limiter,
                    "" if p.boost is None else "%d" % round(p.boost),
                    "" if p.air is None else "%d" % round(p.air)]
            for col, txt in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(txt)
                it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
                                    if col != 5 else QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                if col == 5:
                    it.setData(QtCore.Qt.UserRole, p.limiter)
                    it.setForeground(limiter_colour(p.limiter))
                self.table.setItem(r, col, it)
        self.table.resizeColumnsToContents()

    def _cell_clicked(self, r, c):
        it = self.table.item(r, 5)
        if it is not None:
            go_to_limiter(self.ws, self.state, it.data(QtCore.Qt.UserRole))

    # -- export ------------------------------------------------------------------
    def _base_name(self):
        p = self.ws.primary
        import os
        return os.path.splitext(p.binfile.name if p is not None else "dyno")[0] + " dyno"

    def _export_csv(self):
        from . import gui_dyno_export as _ex
        st = self.state
        if st.curve is None or not st.curve.points:
            return
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export dyno curves", self._base_name() + ".csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(_ex.csv_text(st))

    def _image(self):
        from . import gui_dyno_export as _ex
        return _ex.image(self.state, show_power=self.chart.show_power,
                         show_torque=self.chart.show_torque)

    def _save_png(self):
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save dyno chart", self._base_name() + ".png", "PNG image (*.png)")
        if path:
            self._image().save(path, "PNG")

    def _copy_image(self):
        QtWidgets.QApplication.clipboard().setImage(self._image())

    def _fit_side(self):
        """The settings column as wide as its controls, never a horizontal
        scroll (notes wrap or elide, so a long name never widens it)."""
        sc = self._side
        w = (sc.widget().sizeHint().width()
             + sc.verticalScrollBar().sizeHint().width())
        scr = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if scr is not None:
            w = min(w, int(scr.availableGeometry().width() * 0.45))
        if w > sc.minimumWidth():
            sc.setMinimumWidth(w)

    def _fit_to_screen(self, w: int, h: int) -> None:
        """Open at ``w`` x ``h``, or smaller where the screen is."""
        scr = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            w = min(w, int(g.width() * 0.92))
            h = min(h, int(g.height() * 0.9))
        self.resize(w, h)

    def showEvent(self, ev):
        super().showEvent(ev)
        # the content settled: the whole window on the screen, centred
        if not getattr(self, "_fitted", False):
            self._fitted = True
            self._fit_side()                # measured again, now it is styled
            scr = self.screen() or QtGui.QGuiApplication.primaryScreen()
            if scr is not None:
                g = scr.availableGeometry()
                self.resize(min(self.width(), max(self.minimumWidth(), g.width())),
                            min(self.height(), max(self.minimumHeight(), g.height())))
                fr = self.frameGeometry()
                fr.moveCenter(g.center())
                x = max(g.left(), min(fr.left(), g.right() - fr.width() + 1))
                y = max(g.top(), min(fr.top(), g.bottom() - fr.height() + 1))
                self.move(x, y)

    def closeEvent(self, ev):
        self._timer.stop()
        super().closeEvent(ev)


class GearboxDialog(QtWidgets.QDialog):
    """A gearbox of the user's own for the road speed: its name, gear
    ratios, final drive(s) and which gears use which (a twin-shaft box,
    DSG, has two). ``mine`` is one being edited (it can be deleted);
    ``template`` (any gearbox) fills a new one."""

    def __init__(self, parent, mine=None, template=None):
        super().__init__(parent)
        import uuid
        self.setWindowTitle("Gearbox")
        self.deleted = False
        self.result_gb = None
        src = mine or template or {}
        self._id = mine["id"] if mine else "user-" + uuid.uuid4().hex[:8]
        form = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit(
            src.get("name", "") if mine else
            ((src.get("name", "") + " (mine)") if src else ""))
        self.name.setPlaceholderText("e.g. 02J, my car")
        form.addRow("Name:", self.name)
        self.gears = QtWidgets.QLineEdit(" ".join("%g" % x for x in src.get("gears", [])))
        self.gears.setPlaceholderText("1st 2nd 3rd ...  e.g. 3.78 2.12 1.36 0.97 0.76")
        form.addRow("Gear ratios:", self.gears)
        self.finals = QtWidgets.QLineEdit(" ".join("%g" % x for x in src.get("finals", [])))
        self.finals.setPlaceholderText("e.g. 3.389 (two for a DSG: 4.118 3.087)")
        form.addRow("Final drive:", self.finals)
        self.final_of = QtWidgets.QLineEdit(" ".join(
            str(int(i) + 1) for i in src.get("final_of", [])))
        self.final_of.setPlaceholderText("which final each gear uses, e.g. 1 1 1 1 2 2 "
                                         "(empty: the first for all)")
        form.addRow("Gears use final:", self.final_of)
        self.msg = QtWidgets.QLabel()
        self.msg.setWordWrap(True)
        form.addRow(self.msg)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save
                                        | QtWidgets.QDialogButtonBox.Cancel)
        if mine:
            dl = bb.addButton("Delete", QtWidgets.QDialogButtonBox.DestructiveRole)
            dl.clicked.connect(self._delete)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _delete(self):
        self.deleted = True
        self.result_gb = {"id": self._id}
        self.accept()

    def _save(self):
        from . import dyno_speed as _sp
        try:
            gears = _sp.parse_ratios(self.gears.text())
            finals = _sp.parse_ratios(self.finals.text())
            of = [int(round(x)) - 1 for x in _sp.parse_ratios(self.final_of.text())]
        except ValueError as ex:
            self.msg.setText(str(ex) or "Numbers only, separated by spaces.")
            return
        name = self.name.text().strip()
        if not name or not gears or not finals:
            self.msg.setText("Give the gearbox a name, its gear ratios and its "
                             "final drive.")
            return
        if any(i < 0 or i >= len(finals) for i in of):
            self.msg.setText("Each gear's final drive is 1 to %d." % len(finals))
            return
        self.result_gb = {"id": self._id, "name": name, "gears": gears,
                          "finals": finals, "final_of": of, "note": "your own"}
        self.accept()


#: the What changed table's columns
_DIFF_COLS = ("rpm", "This file limited by", "Other limited by", "Δ power",
              "Δ torque", "Δ quantity (mg)", "Maps behind those limits that differ")


def _html(text: str) -> str:
    import html
    return html.escape(text or "", quote=False)


def _menu_text(text: str, n: int = 60) -> str:
    """A menu or list entry: long names shortened in the middle, ``&`` kept."""
    t = (text or "").replace("&", "&&")
    return t if len(t) <= n else t[:n // 2 - 1] + "…" + t[-(n // 2 - 1):]


def _swatch(colour: QtGui.QColor) -> QtGui.QIcon:
    pm = QtGui.QPixmap(14, 10)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(colour)
    p.drawRoundedRect(QtCore.QRectF(0, 1, 14, 8), 2, 2)
    p.end()
    return QtGui.QIcon(pm)


def _note_label() -> QtWidgets.QLabel:
    """A note under a control: wraps, and never sets the column's width (a
    long version or file name is elided in it rather than widening it)."""
    lbl = QtWidgets.QLabel()
    lbl.setWordWrap(True)
    lbl.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
    lbl.setMinimumWidth(0)
    return lbl


def _set_note(lbl: QtWidgets.QLabel, text: str, longest: int = 48) -> None:
    """``text`` in a note, any word too long to wrap shortened (the whole
    text in the tooltip)."""
    words = []
    for w in (text or "").split(" "):
        words.append(w if len(w) <= longest else w[:longest // 2] + "…" + w[-(longest // 2 - 1):])
    lbl.setText(" ".join(words))
    lbl.setToolTip(text if text and words != (text or "").split(" ") else "")


__all__ = ["DynoState", "DynoChart", "DynoPanel", "DynoDialog", "attach",
           "set_shown", "is_shown", "open_for", "availability", "state_for",
           "LIMITER_COLOURS", "RUN_COLOURS", "run_colour", "Run", "GearboxDialog"]

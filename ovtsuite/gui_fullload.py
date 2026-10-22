# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The shift editor's "Full-load optimum" step: its controls, the targets it
works out, and a small power-through-the-gears chart.

The torque comes from an ECU kept in the same project
(:mod:`ovtsuite.fullload_ecu`); the optimum, lead and blend from
:mod:`ovtsuite.gearbox_edit.fullload`. The panel builds the operation; the
editor previews, checks and applies it like any other step.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import dyno as _dy
from . import gearbox_edit as GE
from .gearbox_edit import fullload as FL

#: QSettings keys (the vehicle mass sits with the Vehicle dialog's settings)
MASS_KEY = "zf6hp/mass"
RESPONSE_KEY = "fullload/response"
LEADS = (("logs", "From logs, else physics"), ("physics", "Physics"),
         ("manual", "Manual"))
_OPEN_TAB = "__tab__"


def _settings():
    from .appsettings import settings
    return settings()


def _conditions() -> _dy.Conditions:
    """The dyno chart's remembered conditions, so both agree."""
    cond = _dy.Conditions()
    try:
        from .gui_dyno import COND_KEY, _REMEMBER
        s = _settings()
        for k in _REMEMBER:
            v = s.value(COND_KEY % k, None)
            if v is None or v == "":
                continue
            cur = getattr(cond, k)
            setattr(cond, k, type(cur)(v) if not isinstance(cur, str) else str(v))
    except Exception:
        pass
    cond.step = 25.0
    return cond


def _is_manual(s) -> bool:
    g = (s.group or "").strip()
    return g.startswith("M ") or g.startswith("M(") or g == "M"


# ----------------------------------------------------------------- chart
class PowerChart(QtWidgets.QWidget):
    """Engine power against road speed (output rpm) at full load, through the
    gears: today's shift points dashed, the optimum solid."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.traces: List[tuple] = []      # (label, [(x, kW)], dashed, [(x, kW)] dots)
        self.title = ""

    def set_traces(self, traces, title: str = ""):
        self.traces, self.title = traces, title
        self.update()

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QtGui.QPalette.Base))
        fg = pal.color(QtGui.QPalette.Text)
        m_l, m_r, m_t, m_b = 40, 8, 18, 22
        W, H = self.width() - m_l - m_r, self.height() - m_t - m_b
        pts = [pt for _l, line, _d, _o in self.traces for pt in line]
        if W < 20 or H < 20 or not pts:
            p.setPen(fg)
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, self.title or "No curve yet")
            p.end()
            return
        x0, x1 = 0.0, max(x for x, _ in pts) * 1.02
        y0, y1 = min(y for _, y in pts) * 0.9, max(y for _, y in pts) * 1.05

        def X(v):
            return m_l + (v - x0) / (x1 - x0) * W

        def Y(v):
            return m_t + H - (v - y0) / ((y1 - y0) or 1) * H
        grid = QtGui.QColor(fg)
        grid.setAlpha(40)
        p.setPen(fg)
        p.drawText(QtCore.QRectF(m_l, 0, W, m_t), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                   self.title)
        from .gui_gearbox_edit import _nice_ticks
        for v in _nice_ticks(y0, y1, 4):
            p.setPen(QtGui.QPen(grid, 1))
            p.drawLine(QtCore.QPointF(m_l, Y(v)), QtCore.QPointF(m_l + W, Y(v)))
            p.setPen(fg)
            p.drawText(QtCore.QRectF(0, Y(v) - 7, m_l - 4, 14),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, "%.0f" % v)
        for v in _nice_ticks(x0, x1, 5):
            p.setPen(fg)
            p.drawText(QtCore.QRectF(X(v) - 24, m_t + H + 2, 48, 14), QtCore.Qt.AlignCenter,
                       "%.0f" % v)
        p.drawText(QtCore.QRectF(m_l, m_t + H + 2, W, 18), QtCore.Qt.AlignRight
                   | QtCore.Qt.AlignBottom, "kW / output rpm")
        cols = {False: QtGui.QColor("#2A78D6"), True: QtGui.QColor(fg)}
        for _lab, line, dashed, dots in self.traces:
            c = QtGui.QColor(cols[dashed])
            if dashed:
                c.setAlpha(140)
            p.setPen(QtGui.QPen(c, 1.4 if dashed else 2.0,
                                QtCore.Qt.DashLine if dashed else QtCore.Qt.SolidLine))
            p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(X(a), Y(b)) for a, b in line]))
            p.setBrush(c)
            for a, b in dots:
                p.drawEllipse(QtCore.QPointF(X(a), Y(b)), 2.8, 2.8)
            p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(QtGui.QPen(fg, 1))
        p.drawRect(QtCore.QRectF(m_l, m_t, W, H))
        p.end()


def sawtooth(plan: FL.FullLoadPlan, shifts: Dict[int, float], lines, slip) -> tuple:
    """``(trace, dots)``: power by output rpm through the gears with the
    upshifts at ``shifts`` (turbine rpm where each shift happens, by line)."""
    ups = sorted((ln for ln in lines if ln.kind == "up" and ln.index in shifts),
                 key=lambda ln: ln.gear_left)
    if not ups:
        return [], []
    trace, dots = [], []
    start = plan.curve.peak_torque_rpm * 0.6 / ups[0].ratio_left
    gears = [(ln.gear_left, ln.ratio_left, shifts[ln.index] / ln.ratio_left) for ln in ups]
    last = ups[-1]
    gears.append((last.gear_entered, last.ratio_entered,
                  plan.ceiling / (1 + slip.get(last.gear_entered, 0.0)) / last.ratio_entered))
    out = start
    for g, r, end in gears:
        s = slip.get(g, 0.0)
        curve = plan.curve_for(g)
        n = 24
        if end <= out:
            continue
        for j in range(n + 1):
            o = out + (end - out) * j / n
            trace.append((o, curve.torque_at(o * r * (1 + s)) * o * r * (1 + s) / 9549.2966))
        dots.append(trace[-1])
        out = end
    return trace, dots[:-1]


# ----------------------------------------------------------------- panel
class FullLoadPanel(QtWidgets.QWidget):
    """The step's controls. ``changed`` fires when the operation it would
    build changed."""

    changed = QtCore.Signal()

    def __init__(self, editor):
        super().__init__(editor)
        self.ed = editor
        self.ws = editor.ws
        ref = getattr(self.ws, "project_ref", None)
        self.project = ref.doc.project if ref is not None else None
        self._source = None
        self._curves: Dict[tuple, tuple] = {}
        self._measured = None
        self.plan: Optional[FL.FullLoadPlan] = None
        self.curve: Optional[FL.TorqueCurve] = None
        self._active = False
        st = _settings()

        f = QtWidgets.QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.set_box = QtWidgets.QComboBox()
        self.set_box.setToolTip("The engine ECU in this project whose full-load torque "
                                "sets the shift points")
        self.ver_box = QtWidgets.QComboBox()
        self.ver_box.setToolTip("Which version of it: by default the one recorded as "
                                "flashed (what is in the car)")
        self.ver_box.setMinimumContentsLength(18)
        self.ver_box.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        f.addRow("Engine ECU", self.set_box)
        f.addRow("Version", self.ver_box)
        self.curve_lbl = QtWidgets.QLabel()
        self.curve_lbl.setWordWrap(True)
        self.curve_lbl.setStyleSheet("color: palette(mid);")
        f.addRow(self.curve_lbl)

        self.shape = QtWidgets.QComboBox()
        for k, t in FL.SHAPES:
            self.shape.addItem(t, k)
        self.shape.setToolTip(
            "How the rows below full load follow: keep the factory shape scales each line "
            "between the blend pedal and full load (a factory step stays, in proportion); "
            "ease in moves them by a smooth fraction of the change; full-load rows only "
            "leaves them alone, except a row above a lowered full-load point, which "
            "comes down to it so the line never falls into full load")
        self.p0 = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=99, value=80,
                                           suffix=" %")
        self.p0.setToolTip("No row below this pedal is changed by the blend")
        sh = QtWidgets.QHBoxLayout()
        sh.addWidget(self.shape, 1)
        sh.addWidget(QtWidgets.QLabel("from"))
        sh.addWidget(self.p0)
        f.addRow("Blend", sh)
        self.mode = QtWidgets.QComboBox()
        for k, t in FL.MODES:
            self.mode.addItem(t, k)
        f.addRow("Rows", self.mode)
        self.ft_chk = QtWidgets.QCheckBox("Full load from where the ECU asks for full torque")
        self.ft_chk.setToolTip(
            "Where the engine's driver's wish already asks for full torque before 100 % "
            "pedal (in every gear, 1500-3500 rpm), the gearbox rows from that pedal up are "
            "full load in engine terms: give them the full-load points too. The other way "
            "to line the two up is the step 'Pedal axis to the ECU's full torque', which "
            "moves the gearbox's pedal axis instead of the values.")
        self.ft_chk.toggled.connect(lambda *_: self.changed.emit())
        f.addRow("", self.ft_chk)
        self.skip_m = QtWidgets.QCheckBox("Leave manual (M) programs alone")
        self.skip_m.setChecked(True)
        f.addRow("", self.skip_m)
        self.kd_down = QtWidgets.QCheckBox("Kickdown downshifts move with their upshift")
        self.kd_down.setChecked(True)
        self.kd_down.setToolTip("On the kickdown row the downshift moves by the same amount, "
                                "so the kickdown hysteresis is kept")
        f.addRow("", self.kd_down)
        sm = QtWidgets.QHBoxLayout()
        self.smooth_chk = QtWidgets.QCheckBox("Spread steps steeper than")
        self.smooth_chk.setChecked(True)
        self.smooth = QtWidgets.QDoubleSpinBox(decimals=1, minimum=1.0, maximum=10, value=1.5,
                                               suffix=" × factory")
        self.smooth_chk.setToolTip(
            "Where the new full-load point makes a line rise between two pedal rows more "
            "steeply than this times the line's own steepest factory rise, the rows below "
            "are lifted to spread it (down to 50 % pedal at most)")
        sm.addWidget(self.smooth_chk)
        sm.addWidget(self.smooth)
        f.addRow("", sm)
        self.min_gain = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=50, value=1.0,
                                                 suffix=" %")
        self.min_gain.setToolTip(
            "Leave a line alone where moving it gains less average power than this -- "
            "the upper gears are often already within a percent")
        f.addRow("Only if it gains ≥", self.min_gain)

        self.lead_box = QtWidgets.QComboBox()
        for k, t in LEADS:
            self.lead_box.addItem(t, k)
        self.lead_box.setToolTip(
            "A map value is where the gearbox STARTS the shift; the engine keeps climbing "
            "until the ratio changes. Physics works the climb out from the curve and the "
            "vehicle; logs measure it from full-load upshifts in this project's logs")
        f.addRow("Lead", self.lead_box)
        vr = QtWidgets.QHBoxLayout()
        self.mass = QtWidgets.QSpinBox(minimum=500, maximum=5000, singleStep=10,
                                       value=int(float(st.value(MASS_KEY, 1800) or 1800)),
                                       suffix=" kg")
        self.mass.setToolTip("Vehicle mass with driver -- kept with the Vehicle settings "
                             "(final drive, tyre) of the table view")
        self.resp = QtWidgets.QDoubleSpinBox(decimals=2, minimum=0.05, maximum=2.0,
                                             singleStep=0.05, suffix=" s",
                                             value=float(st.value(RESPONSE_KEY, 0.30) or 0.30))
        self.resp.setToolTip("Time from the threshold to the ratio starting to change "
                             "at full load")
        vr.addWidget(self.mass)
        vr.addWidget(self.resp)
        f.addRow("Mass, response", vr)
        self.manual = QtWidgets.QLineEdit()
        self.manual.setPlaceholderText("rpm per upshift, e.g. 500, 220, 100, 60, 30")
        f.addRow("Manual lead", self.manual)
        sl = QtWidgets.QHBoxLayout()
        self.slip = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=30, value=6,
                                             suffix=" %")
        self.slip.setToolTip("Converter slip at full load in the gears whose converter is "
                             "open there: engine = turbine × (1 + slip)")
        self.open_lbl = QtWidgets.QLabel()
        self.open_lbl.setStyleSheet("color: palette(mid);")
        sl.addWidget(self.slip)
        sl.addWidget(self.open_lbl, 1)
        f.addRow("Slip, open gears", sl)
        self.ceiling = QtWidgets.QSpinBox(minimum=0, maximum=9000, singleStep=50,
                                          suffix=" rpm")
        self.ceiling.setSpecialValueText("Auto")
        self.ceiling.setToolTip("Highest engine rpm to shift at. Auto: 200 rpm below the "
                                "engine's cut-off (where torque halves past peak power)")
        f.addRow("Ceiling", self.ceiling)

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Line", "Shift at", "Lead", "Map value", "Why"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.setMaximumHeight(150)
        self.table.setToolTip("Engine rpm (turbine rpm x slip where the converter is "
                              "open). Map value is the threshold as the editor shows it: "
                              "output rpm x the gear's ratio")
        f.addRow(self.table)
        self.chart = PowerChart()
        f.addRow(self.chart)

        for w in (self.set_box,):
            w.currentIndexChanged.connect(self._set_changed)
        self.ver_box.currentIndexChanged.connect(lambda *_: self._recalc(True))
        for w in (self.shape, self.mode):
            w.currentIndexChanged.connect(lambda *_: self.changed.emit())
        for w in (self.p0, self.smooth, self.min_gain):
            w.valueChanged.connect(lambda *_: self.changed.emit())
        for w in (self.skip_m, self.kd_down, self.smooth_chk):
            w.toggled.connect(lambda *_: self.changed.emit())
        self.lead_box.currentIndexChanged.connect(lambda *_: self._recalc())
        for w in (self.mass, self.resp, self.slip, self.ceiling):
            w.valueChanged.connect(lambda *_: self._recalc())
        self.manual.editingFinished.connect(self._recalc)
        self.shape.currentIndexChanged.connect(
            lambda *_: self.p0.setEnabled(self.shape.currentData() != FL.SHAPE_ROWS))

    # -------------------------------------------------------- sources
    def activate(self):
        """Fill the ECU lists and work the targets out -- done the first time
        the step is chosen, not when the editor opens."""
        if self._active:
            return
        self._active = True
        if self.project is None:
            self.curve_lbl.setText("Open this gearbox from a project that also holds the "
                                   "engine's ECU: the torque comes from it.")
            for w in (self.set_box, self.ver_box):
                w.setEnabled(False)
            return
        from . import fullload_ecu as fe
        sets = fe.ecu_sets(self.project)
        if not sets:
            self.curve_lbl.setText("This project holds no engine ECU. Add the ECU's binary "
                                   "to the project to work from its torque.")
            return
        default = fe.default_set(self.project)
        self.set_box.blockSignals(True)
        for s in sets:
            self.set_box.addItem("%s (%s)" % (s.name, s.platform), s.id)
        self.set_box.setCurrentIndex(max(0, self.set_box.findData(default.id)))
        self.set_box.blockSignals(False)
        self._set_changed()

    def _open_tab(self, sid):
        ref = getattr(self.ws, "project_ref", None)
        try:
            for t in ref.doc.tabs(self.ws.win):
                if t.project_ref.sid == sid and t.primary is not None:
                    return t
        except Exception:
            pass
        return None

    def _set_changed(self, *_):
        from . import fullload_ecu as fe
        sid = self.set_box.currentData()
        self.ver_box.blockSignals(True)
        self.ver_box.clear()
        if sid:
            tab = self._open_tab(sid)
            vers = fe.versions(self.project, sid)
            for v in vers:
                self.ver_box.addItem(v.label, v.cid)
            if tab is not None:
                self.ver_box.insertItem(1 if vers and vers[0].kind == fe.FLASHED else 0,
                                        "Open tab, with unsaved edits", _OPEN_TAB)
            self.ver_box.setCurrentIndex(0)
        self.ver_box.blockSignals(False)
        self._recalc(True)

    def _vn_by_gear(self) -> Dict[int, float]:
        """(km/h)/rpm in each gear, from the final drive and tyre: what the
        ECU picks its per-gear driver's wish by."""
        veh = self.ed._vehicle()
        try:
            circ, axle = veh.circumference_m, veh.axle_ratio
        except Exception:
            return {}
        out = {}
        for ln in self.ed.sset.lines:
            for g, r in ((ln.gear_left, ln.ratio_left), (ln.gear_entered, ln.ratio_entered)):
                if g and r and axle:
                    out[g] = circ * 60.0 / 1000.0 / (r * axle)
        return out

    def _curve(self):
        """``(curve, {gear: curve}, note)`` of the chosen ECU version."""
        from . import fullload_ecu as fe
        sid, cid = self.set_box.currentData(), self.ver_box.currentData()
        if not sid or not cid:
            return None, {}, ""
        sess = None
        if cid == _OPEN_TAB:
            tab = self._open_tab(sid)
            if tab is None:
                return None, {}, "That tab is closed."
            sess = tab.primary
            cid = tab.project_ref.set.branch(tab.project_ref.bid).head
            key = (sid, _OPEN_TAB, getattr(sess, "revision", None))
        else:
            key = (sid, cid)
        vn = self._vn_by_gear()
        key = key + (tuple(sorted((g, round(v, 6)) for g, v in vn.items())),)
        if key not in self._curves:
            if self._source is None:
                self._source = fe.EcuTorque(self.project)
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            try:
                self._curves[key] = self._source.curves(sid, cid, vn, _conditions(),
                                                        session=sess)
            except Exception as ex:
                self._curves[key] = (None, {}, "The ECU could not be read: %s" % ex)
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
        return self._curves[key]

    def model(self):
        """The dyno model of the chosen ECU version (None without one)."""
        from . import fullload_ecu as fe
        sid, cid = self.set_box.currentData(), self.ver_box.currentData()
        if not sid or not cid:
            return None
        if cid == _OPEN_TAB:
            tab = self._open_tab(sid)
            sess = tab.primary if tab is not None else None
        else:
            if self._source is None:
                self._source = fe.EcuTorque(self.project)
            sess = self._source.session(sid, cid)
        ms = _dy.models(sess) if sess is not None else []
        return ms[0] if ms else None

    def full_torque(self, pedals=None):
        """The lowest pedal (of ``pedals``, default the first ticked
        schedule's rows) at which the ECU asks for full torque in every gear,
        or None (no ECU, no driver's wish, or never before 100 %)."""
        from . import fullload_ecu as fe
        if pedals is None:
            sel = self.ed._selected()
            if not sel:
                return None
            pedals = tuple(self.ed.sset.schedules[sel[0]].pedal)
        key = (self.set_box.currentData(), self.ver_box.currentData(), tuple(pedals),
               tuple(sorted(self._vn_by_gear().items())))
        cache = self.__dict__.setdefault("_ft_cache", {})
        if key not in cache:
            m = self.model() if self._active else None
            try:
                cache[key] = fe.full_torque_pedal(m, self._vn_by_gear(), pedals,
                                                  cond=_conditions())[0] if m else None
            except Exception:
                cache[key] = None
        return cache[key]

    # ---------------------------------------------------------- targets
    def _vehicle(self) -> FL.VehicleModel:
        veh = self.ed._vehicle()
        st = _settings()
        st.setValue(MASS_KEY, self.mass.value())
        st.setValue(RESPONSE_KEY, self.resp.value())
        try:
            circ = veh.circumference_m
            axle = veh.axle_ratio
        except Exception:
            circ, axle = 2.0, 4.16
        return FL.VehicleModel(mass_kg=float(self.mass.value()), axle_ratio=axle,
                               circumference_m=circ, response_s=self.resp.value())

    def _open_gears(self) -> List[int]:
        """Gears whose converter is open at full load in the selected
        programs, from the lock-up maps (:func:`fullload.open_gears`)."""
        if getattr(self, "_tcc", False) is False:
            try:
                from .gui_gearbox_edit import schedule_set_for
                self._tcc = schedule_set_for(self.ed.session, "tcc")
            except Exception:
                self._tcc = None
        return FL.open_gears(self.ed.sset, self._tcc, self.ed._selected())

    def _manual(self) -> Optional[Dict[int, float]]:
        if self.lead_box.currentData() != "manual":
            return None
        ups = [ln for ln in self.ed.sset.lines if ln.kind == "up"]
        vals = []
        for part in self.manual.text().replace(";", ",").split(","):
            try:
                vals.append(float(part))
            except ValueError:
                continue
        return {ln.index: v for ln, v in zip(ups, vals)}

    def _measured_leads(self):
        if self.lead_box.currentData() != "logs":
            return None
        if self._measured is not None:
            return self._measured
        pl = self.ed._project_logs() if getattr(self.ws, "project_ref", None) else None
        if pl is None:
            self._measured = {}
            return self._measured
        sset = self.ed.sset
        evs = [e for e in pl.shifts if e.kind == "up" and e.pedal >= 95.0]
        threshold = FL.blend_threshold(sset, self.ed._blends(),
                                       pl.lookup(evs) if evs else None)
        self._measured = FL.measured_leads(evs, threshold,
                                           lambda ln: sset.line(ln).ratio_left or 1.0)
        return self._measured

    def _recalc(self, new_curve: bool = False):
        if not self._active:
            return
        self.curve, by_gear, note = self._curve() if self.set_box.count() else (None, {}, "")
        if self.curve is None:
            self.plan = None
            if note:
                self.curve_lbl.setText(note)
            self._fill()
            self.changed.emit()
            return
        open_g = self._open_gears()
        slip = {g: self.slip.value() / 100.0 for g in open_g}
        self.open_lbl.setText("open in gear %s" % ", ".join(map(str, open_g))
                              if open_g else "locked in every gear")
        meas = self._measured_leads()
        self.plan = FL.plan_upshifts(self.ed.sset, self.curve, curves=by_gear, slip=slip,
                                     ceiling=float(self.ceiling.value()) or None,
                                     vehicle=self._vehicle(), measured=meas,
                                     manual=self._manual())
        self._slip = slip
        txt = "Full load: " + note
        if self.lead_box.currentData() == "logs" and not meas:
            txt += "\nNo full-load upshifts in this project's logs yet -- the lead is " \
                   "worked out from the vehicle."
        if self.plan.notes:
            txt += "\n" + "\n".join(self.plan.notes)
        self.curve_lbl.setText(txt)
        ft = self.full_torque()
        self.ft_chk.setText("Full load from where the ECU asks for full torque (%s)" % (
            "%.1f %% pedal" % ft if ft is not None else "not before 100 % here"))
        self.ft_chk.setEnabled(ft is not None)
        self._fill()
        self.changed.emit()

    def _fill(self):
        t = self.table
        t.setRowCount(0)
        if self.plan is None:
            self.chart.set_traces([], "")
            return
        for st in sorted(self.plan.targets.values(), key=lambda s: s.gear_from):
            r = t.rowCount()
            t.insertRow(r)
            vals = [st.label, "%.0f" % st.shift_engine, "%.0f %s" % (st.lead, st.lead_source),
                    "%.0f" % st.threshold, st.reason]
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if c in (1, 2, 3):
                    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        self._chart()

    def _chart(self):
        if self.plan is None:
            return
        sset = self.ed.sset
        sel = self.ed._selected()
        after = {ln: st.shift_turbine for ln, st in self.plan.targets.items()}
        traces = []
        if sel:
            i = sel[0]
            s = sset.schedules[i]
            r = next((r for r, p in enumerate(s.pedal) if p >= 99.9), None)
            if r is not None:
                vals = s.values()
                before = {ln: vals[r * s.cols + ln] * st.ratio + st.lead
                          for ln, st in self.plan.targets.items()
                          if ln < s.cols and not s.is_off(vals[r * s.cols + ln])}
                tr, dots = sawtooth(self.plan, before, sset.lines, self._slip)
                traces.append(("today", tr, True, dots))
            title = "Full load through the gears: %s today (dashed) vs optimum" % s.key
        else:
            title = "Full load through the gears: optimum"
        tr, dots = sawtooth(self.plan, after, sset.lines, self._slip)
        traces.append(("optimum", tr, False, dots))
        self.chart.set_traces(traces, title)

    def selection_changed(self):
        if self._active and self.plan is not None:
            self._recalc()

    # -------------------------------------------------------------- op
    def make_op(self, scope: GE.Scope) -> Optional[FL.FullLoadTarget]:
        if self.plan is None or not self.plan.targets:
            return None
        sset = self.ed.sset
        scheds = [i for i in scope.schedules
                  if not (self.skip_m.isChecked() and _is_manual(sset.schedules[i]))]
        lines = [c for c in scope.lines if c in self.plan.targets]
        if not scheds or not lines:
            return None
        sc = GE.Scope(scheds, lines, direction=scope.direction)
        plan = self.plan
        gain = self.min_gain.value() / 100.0
        worth = (lambda i, c, old: plan.gain(c, old) >= gain) if gain > 0 else None
        return FL.FullLoadTarget(
            sc, plan.outputs(), shape=self.shape.currentData(), mode=self.mode.currentData(),
            p0=self.p0.value(),
            p_full=(self.full_torque() or 99.9) if self.ft_chk.isChecked() else 99.9,
            smooth=self.smooth.value() if self.smooth_chk.isChecked() else None,
            kickdown_downshifts=self.kd_down.isChecked(), worth=worth)

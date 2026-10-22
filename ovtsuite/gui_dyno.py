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
the history. Panel and window share one state per tab, so a change in
either shows in both.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import dyno as _dy

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
POWER_COLOUR = QtGui.QColor("#D85A30")
TORQUE_COLOUR = QtGui.QColor("#2A78D6")

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
class DynoState(QtCore.QObject):
    """What the tab's dyno shows: the conditions, the reference, the units,
    and the curves worked out from them. One per tab, shared by the panel
    and the window; :meth:`poll` recalculates when either file changed."""

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
        #: the reference: (kind, version id) -- kind REF_* of ovtsuite.dyno
        self.ref_kind = kind if kind in (_dy.REF_ORIGINAL, _dy.REF_COMPARISON,
                                         _dy.REF_NONE) else _dy.REF_ORIGINAL
        self.ref_version: Optional[str] = None
        self.group: Optional[str] = None
        # results
        self.models: List[_dy.DynoModel] = []
        self.model: Optional[_dy.DynoModel] = None
        #: the reference's model, when it has one (its conversion is used)
        self.ref_model: Optional[_dy.DynoModel] = None
        self.curve: Optional[_dy.Curve] = None
        self.ref_curve: Optional[_dy.Curve] = None
        self.ref_label = ""
        self.message = ""
        # change tracking
        self._seen = None
        self._ref_cache: Dict[tuple, object] = {}
        self._dirty = True
        #: the session worked out last (held, so a new file in the tab is
        #: told apart by identity, never by a reused id())
        self._file = None

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
        if kind != _dy.REF_VERSION:
            s = _settings()
            s.setValue(REF_KEY, kind)
            s.sync()
        self.invalidate()

    def set_condition(self, **kw):
        s = _settings()
        for k, v in kw.items():
            setattr(self.cond, k, v)
            if k in _REMEMBER:
                s.setValue(COND_KEY % k, v)
        s.sync()
        self.invalidate()

    def set_group(self, group: Optional[str]):
        self.group = group
        self.invalidate()

    def invalidate(self):
        self._dirty = True
        self.poll()

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
        return (id(p), getattr(p, "revision", None),
                id(c) if self.ref_kind == _dy.REF_COMPARISON else None,
                getattr(c, "revision", None) if self.ref_kind == _dy.REF_COMPARISON else None)

    def _new_file(self):
        """Another file in the tab: what was picked for the old one -- its
        engine, smoke limits, wish map, dataset, version -- goes with it."""
        for k in ("wish", "smoke", "displacement", "cylinders"):
            setattr(self.cond, k, getattr(_dy.Conditions(), k))
        self.group = None
        self.ref_version = None
        if self.ref_kind == _dy.REF_VERSION:
            self.ref_kind = _dy.REF_ORIGINAL
        self._ref_cache = {}

    def poll(self) -> bool:
        """Recalculate when something changed; True when it did."""
        p = getattr(self.ws, "primary", None)
        if p is not self._file:
            if self._file is not None:
                self._new_file()
            self._file = p
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
        self.model = self.ref_model = None
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
            self.ref_model = rm
            fuel = _dy.fuel_map(p, ref_s, m.group, self.cond.coolant) \
                if (ref_s is not None and self.cond.fuel_basis == "duration") \
                else None
            self.curve = _dy.calculate(m, self.cond, rm, label="This file", fuel=fuel)
            if rm is not None:
                self.ref_curve = _dy.calculate(rm, self.cond, rm, label="Reference")
        except Exception as ex:          # a definition the model can't read
            self.message = "Could not work the curve out: %s" % ex
            self.curve = self.ref_curve = None

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
class DynoChart(QtWidgets.QWidget):
    """Power and torque against engine speed, painted (no chart library):
    power on the left axis, torque on the right, this file solid and the
    reference dashed, the governing-limit strip under the plot."""

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
        self.show_power = True
        self.show_torque = True
        # without a state (tests, export): set directly
        self.curve: Optional[_dy.Curve] = None
        self.ref_curve: Optional[_dy.Curve] = None
        self.power_unit, self.torque_unit = _dy.DEFAULT_POWER, _dy.DEFAULT_TORQUE
        if state is not None:
            state.changed.connect(self.update)

    # -- data ---------------------------------------------------------------
    def _data(self):
        st = self.state
        if st is not None:
            return st.curve, st.ref_curve, st.power_unit, st.torque_unit
        return self.curve, self.ref_curve, self.power_unit, self.torque_unit

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
        return r.left() + (rpm - a) / max(1.0, b - a) * r.width()

    def _rpm_at(self, x):
        a, b = self._xr
        r = self._plot
        return a + (x - r.left()) / max(1.0, r.width()) * (b - a)

    # -- painting -----------------------------------------------------------------
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pal = self.palette()
        ink = pal.color(QtGui.QPalette.WindowText)
        muted = QtGui.QColor(ink)
        muted.setAlpha(140)
        grid = QtGui.QColor(ink)
        grid.setAlpha(28)
        p.fillRect(self.rect(), pal.color(QtGui.QPalette.Base))
        cur, ref, pu, tu = self._data()
        fm = p.fontMetrics()
        curves = [c for c in (cur, ref) if c is not None and c.points]
        if not curves:
            p.setPen(muted)
            msg = ""
            if self.state is not None:
                msg = self.state.summary()
            p.drawText(self.rect().adjusted(12, 12, -12, -12),
                       QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap,
                       msg or "Nothing to show")
            return
        left = fm.horizontalAdvance("0000") + 18
        right = fm.horizontalAdvance("0000") + 18
        top = fm.height() + 6
        bottom = fm.height() + self.STRIP_H + 12
        self._plot = QtCore.QRectF(left, top, max(10, self.width() - left - right),
                                   max(10, self.height() - top - bottom))
        r = self._plot
        self._strip = QtCore.QRectF(r.left(), r.bottom() + 4, r.width(), self.STRIP_H - 2)
        lo = min(c.points[0].rpm for c in curves)
        hi = max(c.points[-1].rpm for c in curves)
        self._xr = (lo, hi if hi > lo else lo + 1)
        pmax = self._nice(max(_dy.power_in(pt.power, pu) for c in curves for pt in c.points) * 1.08)
        tmax = self._nice(max(_dy.torque_in(pt.torque, tu) for c in curves for pt in c.points) * 1.08)
        # grid + axes
        n = 5
        for i in range(n + 1):
            y = r.bottom() - i * r.height() / n
            p.setPen(QtGui.QPen(grid, 1))
            p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
            if self.show_power:
                p.setPen(POWER_COLOUR)
                p.drawText(QtCore.QRectF(0, y - fm.height() / 2, left - 6, fm.height()),
                           QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                           "%g" % round(pmax * i / n, 1))
            if self.show_torque:
                p.setPen(TORQUE_COLOUR)
                p.drawText(QtCore.QRectF(r.right() + 6, y - fm.height() / 2, right - 6,
                                         fm.height()),
                           QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                           "%g" % round(tmax * i / n, 1))
        # rpm ticks
        step = 500 if (hi - lo) <= 4000 else 1000
        import math
        t0 = math.ceil(lo / step) * step
        p.setPen(muted)
        t = t0
        while t <= hi + 1e-6:
            x = self._x(t)
            p.drawText(QtCore.QRectF(x - 30, self._strip.bottom() + 2, 60, fm.height()),
                       QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop, "%d" % t)
            t += step
        # axis titles
        p.setPen(POWER_COLOUR if self.show_power else muted)
        p.drawText(QtCore.QRectF(0, 0, left + 60, fm.height()), QtCore.Qt.AlignLeft,
                   _dy.power_label(pu) if self.show_power else "")
        p.setPen(TORQUE_COLOUR)
        p.drawText(QtCore.QRectF(self.width() - right - 60, 0, right + 60, fm.height()),
                   QtCore.Qt.AlignRight, _dy.torque_label(tu) if self.show_torque else "")
        # legend (centre top)
        p.setPen(muted)
        leg = "solid: this file    dashed: reference" if ref is not None and ref.points \
            else "this file"
        p.drawText(QtCore.QRectF(left, 0, r.width(), fm.height()),
                   QtCore.Qt.AlignHCenter, leg)

        def line(c, val, vmax, colour, dashed):
            path = QtGui.QPainterPath()
            for i, pt in enumerate(c.points):
                xy = QtCore.QPointF(self._x(pt.rpm), r.bottom() - val(pt) / vmax * r.height())
                if i == 0:
                    path.moveTo(xy)
                else:
                    path.lineTo(xy)
            pen = QtGui.QPen(colour, 1.8 if dashed else 2.4)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            if dashed:
                pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawPath(path)

        pv = lambda pt: _dy.power_in(pt.power, pu)      # noqa: E731
        tv = lambda pt: _dy.torque_in(pt.torque, tu)    # noqa: E731
        for c, dashed in ((ref, True), (cur, False)):
            if c is None or not c.points:
                continue
            if self.show_torque:
                line(c, tv, tmax, TORQUE_COLOUR, dashed)
            if self.show_power:
                line(c, pv, pmax, POWER_COLOUR, dashed)
        # peaks of this file
        if cur is not None and cur.points:
            for show, peak, val, vmax, col, unit in (
                    (self.show_power, cur.peak_power(), pv, pmax, POWER_COLOUR,
                     _dy.power_label(pu)),
                    (self.show_torque, cur.peak_torque(), tv, tmax, TORQUE_COLOUR,
                     _dy.torque_label(tu))):
                if not show:
                    continue
                xy = QtCore.QPointF(self._x(peak.rpm), r.bottom() - val(peak) / vmax * r.height())
                p.setPen(QtGui.QPen(pal.color(QtGui.QPalette.Base), 2))
                p.setBrush(col)
                p.drawEllipse(xy, 4, 4)
                p.setPen(col)
                txt = "%s %s" % (_num(val(peak), 1 if unit != "Nm" else 0), unit)
                w = fm.horizontalAdvance(txt)
                x0 = min(max(xy.x() - w / 2, r.left()), r.right() - w)
                p.drawText(QtCore.QPointF(x0, max(r.top() + fm.ascent(), xy.y() - 8)), txt)
            # the governing-limit strip
            for key, a, b in cur.runs():
                x0 = self._x(a) if a > lo else r.left()
                x1 = self._x(b) if b < hi else r.right()
                # bands meet half way between the points
                half = (cur.points[1].rpm - cur.points[0].rpm) / 2 if len(cur.points) > 1 else 0
                if a > lo:
                    x0 = self._x(a - half)
                if b < hi:
                    x1 = self._x(b + half)
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
                box = QtCore.QRectF(x, r.top() + 4, w, h)
                bg = pal.color(QtGui.QPalette.ToolTipBase)
                p.setPen(QtGui.QPen(grid, 1))
                p.setBrush(bg)
                p.drawRoundedRect(box, 4, 4)
                p.setPen(pal.color(QtGui.QPalette.ToolTipText))
                for i, s_ in enumerate(lines):
                    p.drawText(QtCore.QPointF(box.left() + 6,
                                              box.top() + 4 + fm.ascent() + i * fm.height()), s_)
        p.end()

    def _hover_lines(self, rpm) -> List[str]:
        cur, ref, pu, tu = self._data()
        out = []
        for c, name in ((cur, "This file"), (ref, "Reference")):
            if c is None or not c.points:
                continue
            pt = min(c.points, key=lambda q: abs(q.rpm - rpm))
            lim = c.limiters.get(pt.limiter)
            out.append("%s @ %d rpm: %s %s, %s %s" % (
                name, pt.rpm, _num(_dy.power_in(pt.power, pu)), _dy.power_label(pu),
                _num(_dy.torque_in(pt.torque, tu), 0), _dy.torque_label(tu)))
            if c is cur:
                out.append("   %s mg/stroke, limited by %s" % (
                    _num(pt.q), lim.label if lim else pt.limiter))
        return out

    # -- mouse ------------------------------------------------------------------
    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        self._hover = x if self._plot.left() <= x <= self._plot.right() else None
        cur = self._data()[0]
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
        cur = self._data()[0]
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
    return "%s:%s" % (kind, version) if kind == _dy.REF_VERSION else kind


def ref_of(key) -> Tuple[str, Optional[str]]:
    key = str(key or _dy.REF_ORIGINAL)
    kind, _sep, vid = key.partition(":")
    return kind, (vid or None) if kind == _dy.REF_VERSION else None


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
    cb.addItem("None (this file only)", ref_key(_dy.REF_NONE))
    i = cb.findData(ref_key(state.ref_kind, state.ref_version))
    cb.setCurrentIndex(max(0, i))
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
        self.ref_box.setToolTip("The file whose torque-to-fuel and duration maps turn "
                                "quantity into torque, drawn dashed beside this one")
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
    """The full dyno window: the chart, the reference and conditions, every
    engine speed, export."""

    def __init__(self, ws, parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.state = state_for(ws)
        self._filling = 0                   # > 0 while _sync fills the controls
        self.setWindowTitle("Dyno chart")
        try:
            from . import gui
            self.setWindowIcon(gui.app_icon())
        except Exception:
            pass
        self._fit_to_screen(1100, 720)
        outer = QtWidgets.QHBoxLayout(self)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        outer.addWidget(split)
        # -- left: chart, summary, table -----------------------------------
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
        lv.addWidget(self.summary)
        self.legend = QtWidgets.QLabel()
        self.legend.setWordWrap(True)
        lv.addWidget(self.legend)
        self.table = QtWidgets.QTableWidget(0, len(_ROWS))
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setToolTip("Double-click a 'Limited by' cell to open that map")
        self.table.verticalHeader().setDefaultSectionSize(
            self.table.fontMetrics().height() + 6)
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
        low = QtWidgets.QTabWidget()
        low.addTab(self.table, "Engine speeds")
        low.addTab(sa, "How it is worked out")
        lv.addWidget(low, 2)
        split.addWidget(left)
        # -- right: reference, conditions, notes, export ------------------------
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        refg = QtWidgets.QGroupBox("Reference")
        rf = QtWidgets.QFormLayout(refg)
        self.ref_box = _ref_combo(self.state, with_versions=True)
        self.ref_box.setToolTip("Turns this file's quantity into torque (its "
                                "torque-to-fuel and duration maps) and is drawn "
                                "dashed. Default: the original.")
        rf.addRow("Compare with:", self.ref_box)
        self.ref_note = QtWidgets.QLabel()
        self.ref_note.setWordWrap(True)
        rf.addRow(self.ref_note)
        self.fuel_chk = QtWidgets.QCheckBox("Carry the quantity through the duration maps")
        self.fuel_chk.setToolTip(
            "Unit-injector engines: read this file's duration back through the "
            "reference's duration maps, so a rescaled quantity is not taken at face "
            "value. Off: the ECU's quantity is used as it is.")
        rf.addRow(self.fuel_chk)
        rv.addWidget(refg)
        run = QtWidgets.QGroupBox("Run")
        form = QtWidgets.QFormLayout(run)
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
        self.eng_note = QtWidgets.QLabel()
        self.eng_note.setWordWrap(True)
        ef.addRow(self.eng_note)
        rv.addWidget(eng)
        rv.addStretch(1)
        # the settings scroll when the window is short: their height is not
        # the window's minimum
        rscroll = QtWidgets.QScrollArea()
        rscroll.setWidgetResizable(True)
        rscroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        rscroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        rscroll.setWidget(right)
        self._side = rscroll
        self._fit_side()
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
        for cb in (self.ref_box, self.group_box, self.wish_box):
            # a long version name scrolls in the list, not the column wider
            cb.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(22)
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
        self.table.cellDoubleClicked.connect(self._cell_clicked)
        self.chart.limiterClicked.connect(
            lambda key, _rpm: go_to_limiter(ws, self.state, key))
        csv_b.clicked.connect(self._export_csv)
        png_b.clicked.connect(self._save_png)
        copy_b.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setPixmap(self.chart.grab()))
        self.state.changed.connect(self._sync)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(lambda: self.isVisible() and self.state.poll())
        self._timer.start()
        self.state.poll()
        self._sync()

    # -- changes ----------------------------------------------------------------
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
        self.state.set_reference(kind, vid)

    def _group_changed(self, _i):
        if not self._filling:
            self.state.set_group(self.group_box.currentData())

    def refresh_from_session(self):
        """An edit or undo elsewhere (the tab calls this)."""
        self.state.poll()

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
            self._fit_side()
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
                self.eng_note.setText("This file: %s l, %s cylinders (%s)." % (
                    "%g" % m.displacement if m.displacement else "?",
                    m.cylinders or "?", m.engine_source or "the definition"))
            # only the fuel-energy model reads it: neither file converts
            rm = st.ref_model
            self.f_eff.setEnabled(m is None or not (
                m.has_conversion or (rm is not None and rm.has_conversion)))
        finally:
            self._filling -= 1
        self.ref_note.setText("Using: %s" % st.ref_label if st.ref_label else "")
        self.summary.setText("<b>%s</b>" % st.summary() if st.curve else st.summary())
        self._fill_legend()
        self._fill_table()
        notes = []
        if st.curve is not None:
            notes.append("Torque from: %s." % st.curve.torque_source
                         if st.curve.torque_source else "")
            notes += st.curve.notes
        notes.append("This is what the calibration asks for at full load, not a "
                     "measurement: the engine makes it only if the turbo, injectors "
                     "and fuelling deliver it.")
        self.notes.setText("<br>".join("• " + n for n in notes if n))

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
                LIMITER_COLOURS.get(key, "#B4B2A9"), lim.label if lim else key))
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
        st = self.state
        curves = [c for c in (st.curve, st.ref_curve) if c is not None and c.points]
        if not curves:
            return
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export dyno curves", self._base_name() + ".csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(_dy.to_csv(curves, st.power_unit, st.torque_unit))

    def _save_png(self):
        path, _f = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save dyno chart", self._base_name() + ".png", "PNG image (*.png)")
        if path:
            self.chart.grab().save(path, "PNG")

    def _fit_side(self):
        """The settings column as wide as its widest control (the smoke
        limits come and go with the file), never a horizontal scroll."""
        sc = self._side
        w = (sc.widget().sizeHint().width()
             + sc.verticalScrollBar().sizeHint().width())
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


__all__ = ["DynoState", "DynoChart", "DynoPanel", "DynoDialog", "attach",
           "set_shown", "is_shown", "open_for", "availability", "state_for",
           "LIMITER_COLOURS"]

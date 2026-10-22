# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The engine's efficiency map, from the shift editor's Replay box.

Fuel per kWh (g/kWh, brake specific fuel consumption) over engine speed and
brake torque, worked out from the ECU's own conversion
(:class:`ovtsuite.shift_replay.EngineFuel`), up to the full-load curve. Over
it, where the logged driving ran: today's lines and the edit's, as circles
sized by the time spent there. Shift points move those circles -- an
economy strategy pulls them left and up, toward the efficient middle of the
map; a sport one pushes them right.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

#: g/kWh band edges: one colour per band, so equal-efficiency regions read as
#: contours (a smooth ramp makes the efficient middle one flat colour)
BANDS = (205.0, 210.0, 215.0, 220.0, 230.0, 240.0, 255.0, 275.0, 300.0, 350.0)
RPM_STEP, TRQ_STEP = 100.0, 10.0


def bsfc(fuel, rpm: float, torque: float) -> Optional[float]:
    """g/kWh at ``rpm`` and brake ``torque`` (None below 1 kW)."""
    p = torque * rpm / 9549.2966
    if p < 1.0:
        return None
    from .shift_replay import DIESEL_KG_PER_L
    return fuel.litres_per_hour(torque, rpm) * DIESEL_KG_PER_L * 1000.0 / p


def _bins(points) -> Dict[Tuple[int, int], float]:
    out: Dict[Tuple[int, int], float] = {}
    for n, t, dt in points or ():
        k = (int(n // RPM_STEP), int(t // TRQ_STEP))
        out[k] = out.get(k, 0.0) + dt
    return out


def _mean_bsfc(fuel, points) -> Optional[float]:
    """Fuel over the logged work, as g/kWh."""
    g = e = 0.0
    for n, t, dt in points or ():
        p = t * n / 9549.2966
        if p <= 0:
            continue
        from .shift_replay import DIESEL_KG_PER_L
        g += fuel.litres_per_hour(t, n) * DIESEL_KG_PER_L * 1000.0 * dt
        e += p * dt
    return g / e if e else None


def _band(v: float) -> int:
    for k, edge in enumerate(BANDS):
        if v < edge:
            return k
    return len(BANDS)


def _colour(v: float, dark: bool) -> QtGui.QColor:
    """One hue in bands, deep (efficient) to light (poor)."""
    f = 1.0 - _band(v) / float(len(BANDS))
    if dark:
        lo, hi = (38, 42, 50), (64, 150, 240)
    else:
        lo, hi = (240, 243, 248), (12, 68, 124)
    return QtGui.QColor(*[int(a + (b - a) * f) for a, b in zip(lo, hi)])


class EfficiencyMap(QtWidgets.QWidget):
    def __init__(self, fuel, curve, before, after, parent=None):
        super().__init__(parent)
        self.setMinimumSize(560, 380)
        self.setMouseTracking(True)
        self.fuel, self.curve = fuel, curve
        self.before, self.after = _bins(before), _bins(after)
        self.show_before = self.show_after = True
        top = max(curve.torque) if curve is not None and curve.torque else 350.0
        self.rpm_lo, self.rpm_hi = 700.0, (curve.cutoff if curve is not None else 4500.0)
        self.trq_hi = top * 1.06
        self.cells = {}
        n = self.rpm_lo
        while n < self.rpm_hi:
            t = TRQ_STEP / 2
            cap = curve.torque_at(n + RPM_STEP / 2) if curve is not None else top
            while t < cap:
                v = bsfc(fuel, n + RPM_STEP / 2, t)
                if v is not None:
                    self.cells[(n, t)] = v
                t += TRQ_STEP
            n += RPM_STEP
        self._xf = None

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pal = self.palette()
        bg = pal.color(QtGui.QPalette.Base)
        p.fillRect(self.rect(), bg)
        fg = pal.color(QtGui.QPalette.Text)
        dark = bg.lightness() < 128
        m_l, m_r, m_t, m_b = 52, 96, 10, 34
        W, H = self.width() - m_l - m_r, self.height() - m_t - m_b

        def X(n):
            return m_l + (n - self.rpm_lo) / (self.rpm_hi - self.rpm_lo) * W

        def Y(t):
            return m_t + H - t / self.trq_hi * H
        self._xf = (X, Y)
        for (n, t), v in self.cells.items():
            r = QtCore.QRectF(X(n), Y(t + TRQ_STEP / 2), X(n + RPM_STEP) - X(n) + 0.6,
                              Y(t - TRQ_STEP / 2) - Y(t + TRQ_STEP / 2) + 0.6)
            p.fillRect(r, _colour(v, dark))
        # full-load curve
        if self.curve is not None:
            pts = []
            n = self.rpm_lo
            while n <= self.rpm_hi:
                pts.append(QtCore.QPointF(X(n), Y(self.curve.torque_at(n))))
                n += 25.0
            p.setPen(QtGui.QPen(fg, 1.6))
            p.drawPolyline(QtGui.QPolygonF(pts))
        # where the driving ran (inside the plot only)
        p.save()
        p.setClipRect(QtCore.QRectF(m_l, m_t, W, H))
        tot_b = sum(self.before.values()) or 1.0
        tot_a = sum(self.after.values()) or 1.0
        for bins, tot, on, col, filled in (
                (self.before, tot_b, self.show_before, QtGui.QColor("#898781"), False),
                (self.after, tot_a, self.show_after, QtGui.QColor("#D85A30"), True)):
            if not on:
                continue
            p.setPen(QtGui.QPen(col, 1.4))
            p.setBrush(col if filled else QtCore.Qt.NoBrush)
            for (bn, bt), dt in bins.items():
                share = dt / tot
                if share < 0.002:
                    continue
                rad = 2.0 + 14.0 * share ** 0.5
                c = QtCore.QPointF(X((bn + 0.5) * RPM_STEP), Y((bt + 0.5) * TRQ_STEP))
                if filled:
                    cc = QtGui.QColor(col)
                    cc.setAlpha(150)
                    p.setBrush(cc)
                p.drawEllipse(c, rad, rad)
            p.setBrush(QtCore.Qt.NoBrush)
        p.restore()
        # axes
        p.setPen(fg)
        for n in range(1000, int(self.rpm_hi) + 1, 500):
            p.drawText(QtCore.QRectF(X(n) - 24, m_t + H + 2, 48, 14), QtCore.Qt.AlignCenter,
                       str(n))
        t = 0
        while t <= self.trq_hi:
            p.drawText(QtCore.QRectF(0, Y(t) - 7, m_l - 6, 14),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, "%d" % t)
            t += 50
        p.drawText(QtCore.QRectF(m_l, m_t + H + 16, W, 16), QtCore.Qt.AlignCenter,
                   "engine rpm  /  brake torque, Nm")
        # legend: one swatch per band
        lx = m_l + W + 12
        p.setPen(fg)
        p.drawText(QtCore.QRectF(lx, m_t, 90, 14), QtCore.Qt.AlignLeft, "g/kWh")
        labels = ["< %d" % BANDS[0]] + ["%d-%d" % (a, b) for a, b in zip(BANDS, BANDS[1:])] + \
            ["%d +" % BANDS[-1]]
        h = min(18.0, (H - 20) / len(labels))
        for k, lab in enumerate(labels):
            y = m_t + 18 + k * h
            v = (BANDS[k] - 1) if k < len(BANDS) else BANDS[-1] + 1
            p.fillRect(QtCore.QRectF(lx, y + 2, 14, h - 4), _colour(v, dark))
            p.drawText(QtCore.QRectF(lx + 20, y, 70, h), QtCore.Qt.AlignLeft
                       | QtCore.Qt.AlignVCenter, lab)
        p.drawRect(QtCore.QRectF(m_l, m_t, W, H))
        p.end()

    def mouseMoveEvent(self, ev):
        if self._xf is None:
            return
        X, Y = self._xf
        pos = ev.position()
        # invert the axes
        W0, W1 = X(self.rpm_lo), X(self.rpm_hi)
        n = self.rpm_lo + (pos.x() - W0) / (W1 - W0) * (self.rpm_hi - self.rpm_lo)
        t0, t1 = Y(0), Y(self.trq_hi)
        t = (pos.y() - t0) / (t1 - t0) * self.trq_hi
        if not (self.rpm_lo <= n <= self.rpm_hi and 0 <= t <= self.trq_hi):
            QtWidgets.QToolTip.hideText()
            return
        v = bsfc(self.fuel, n, t)
        k = (int(n // RPM_STEP), int(t // TRQ_STEP))
        tb, ta = self.before.get(k, 0.0), self.after.get(k, 0.0)
        tip = "%.0f rpm, %.0f Nm" % (n, t)
        if v is not None and (self.curve is None or t <= self.curve.torque_at(n)):
            tip += "\n%.0f g/kWh" % v
        if tb or ta:
            tip += "\ntime here: today %.0f s, after the edit %.0f s" % (tb, ta)
        QtWidgets.QToolTip.showText(ev.globalPosition().toPoint(), tip, self)


class EfficiencyDialog(QtWidgets.QDialog):
    def __init__(self, parent, fuel, curve, before: Optional[List] = None,
                 after: Optional[List] = None, no_logs: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Engine efficiency map")
        self.resize(760, 520)
        v = QtWidgets.QVBoxLayout(self)
        self.map = EfficiencyMap(fuel, curve, before, after)
        row = QtWidgets.QHBoxLayout()
        self.b_chk = QtWidgets.QCheckBox("Where the driving ran today (hollow)")
        self.a_chk = QtWidgets.QCheckBox("After the edit (filled)")
        for c in (self.b_chk, self.a_chk):
            c.setChecked(True)
            c.setEnabled(bool(before))
            c.toggled.connect(self._toggle)
            row.addWidget(c)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(self.map, 1)
        lab = QtWidgets.QLabel()
        lab.setWordWrap(True)
        lab.setStyleSheet("color: palette(mid);")
        mb, ma = _mean_bsfc(fuel, before), _mean_bsfc(fuel, after)
        if no_logs:
            lab.setText("From the ECU's torque-to-fuel conversion, up to the full-load "
                        "curve. Run Replay first to see where the logged driving ran.")
        elif mb is not None:
            txt = "Over the logged work: %.0f g/kWh today" % mb
            if ma is not None:
                txt += ", %.0f g/kWh after the edit (%+.1f %%)" % (ma, 100.0 * (ma - mb) / mb)
            lab.setText(txt + ". From the ECU's torque-to-fuel conversion, up to the "
                        "full-load curve; circles are sized by time spent.")
        v.addWidget(lab)

    def _toggle(self, *_):
        self.map.show_before = self.b_chk.isChecked()
        self.map.show_after = self.a_chk.isChecked()
        self.map.update()


__all__ = ["EfficiencyDialog", "EfficiencyMap", "bsfc"]

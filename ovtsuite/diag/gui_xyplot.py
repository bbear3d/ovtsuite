# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Two live values against each other, with the specification's window.

The TDI timing graph is this: coolant temperature across, start of
injection up, the workshop manual's window drawn as a band, and every
reading of the live step a point -- the latest one large, in green inside
the window and red outside it, the earlier ones a fading trail. A procedure's
``live`` step asks for it with ``plot:`` (:mod:`.procedures`).
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .gui_widgets import tokens

#: how many earlier readings the trail keeps
TRAIL = 120


def _nice(lo: float, hi: float, n: int = 5) -> List[float]:
    """Round tick values spanning ``lo..hi``."""
    span = max(hi - lo, 1e-9)
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t = math.floor(lo / step) * step
    out = []
    while t <= hi + step * 1e-6:
        if t >= lo - step * 1e-6:
            out.append(round(t, 6))
        t += step
    return out


class XYPlot(QtWidgets.QWidget):
    """``set_spec(xlabel, ylabel, band)``, then ``add(x, y)`` per reading."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.xlabel = ""
        self.ylabel = ""
        self.band: Optional[Tuple[float, float, float, float]] = None
        self.points: List[Tuple[float, float]] = []

    def set_spec(self, xlabel: str = "", ylabel: str = "",
                 band: Optional[Sequence[float]] = None):
        self.xlabel, self.ylabel = xlabel, ylabel
        self.band = tuple(band) if band else None       # type: ignore[assignment]
        self.points = []
        self.update()

    def add(self, x: float, y: float):
        self.points.append((float(x), float(y)))
        del self.points[:-TRAIL]
        self.update()

    def inside(self, x: float, y: float) -> Optional[bool]:
        if self.band is None:
            return None
        x0, x1, y0, y1 = self.band
        return x0 <= x <= x1 and y0 <= y <= y1

    # ------------------------------------------------------------ drawing
    def _ranges(self):
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        if self.band:
            xs += [self.band[0], self.band[1]]
            ys += [self.band[2], self.band[3]]
        if not xs:
            return 0.0, 1.0, 0.0, 1.0
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        px = max((x1 - x0) * 0.25, 1.0)
        py = max((y1 - y0) * 0.25, 1.0)
        return x0 - px, x1 + px, y0 - py, y1 + py

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        fm = p.fontMetrics()
        left, right, top = fm.horizontalAdvance("-0000.0") + 14, 12, 10
        bottom = fm.height() * 2 + 10
        r = QtCore.QRectF(left, top, max(10, self.width() - left - right),
                          max(10, self.height() - top - bottom))
        x0, x1, y0, y1 = self._ranges()

        def X(v):
            return r.left() + (v - x0) / (x1 - x0) * r.width()

        def Y(v):
            return r.bottom() - (v - y0) / (y1 - y0) * r.height()

        grid = QtGui.QColor(t.muted)
        grid.setAlpha(60)
        p.setPen(QtGui.QPen(grid, 1))
        p.setFont(self.font())
        for v in _nice(x0, x1):
            p.drawLine(QtCore.QPointF(X(v), r.top()), QtCore.QPointF(X(v), r.bottom()))
            p.setPen(t.muted)
            s = ("%g" % v)
            p.drawText(QtCore.QPointF(X(v) - fm.horizontalAdvance(s) / 2, r.bottom() + fm.ascent() + 3), s)
            p.setPen(QtGui.QPen(grid, 1))
        for v in _nice(y0, y1):
            p.drawLine(QtCore.QPointF(r.left(), Y(v)), QtCore.QPointF(r.right(), Y(v)))
            p.setPen(t.muted)
            s = ("%g" % v)
            p.drawText(QtCore.QPointF(r.left() - fm.horizontalAdvance(s) - 6, Y(v) + fm.ascent() / 2 - 1), s)
            p.setPen(QtGui.QPen(grid, 1))
        if self.band:
            bx0, bx1, by0, by1 = self.band
            fill = QtGui.QColor("#1D9E75")
            fill.setAlpha(45)
            edge = QtGui.QColor("#1D9E75")
            p.setPen(QtGui.QPen(edge, 1.2))
            p.setBrush(fill)
            p.drawRect(QtCore.QRectF(QtCore.QPointF(X(bx0), Y(by1)), QtCore.QPointF(X(bx1), Y(by0))))
        p.setPen(t.text)
        p.drawText(QtCore.QRectF(r.left(), r.bottom() + fm.height() + 4, r.width(), fm.height()),
                   QtCore.Qt.AlignmentFlag.AlignHCenter, self.xlabel)
        if self.ylabel:                 # across the top-left, not rotated
            p.drawText(QtCore.QPointF(r.left() + 6, r.top() + fm.ascent()), self.ylabel)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(t.muted, 1))
        p.drawRect(r)
        n = len(self.points)
        for i, (x, y) in enumerate(self.points):
            last = i == n - 1
            ok = self.inside(x, y)
            c = QtGui.QColor(t.accent if ok is None else ("#1D9E75" if ok else "#E24B4A"))
            if not last:
                c.setAlpha(40 + int(140 * (i + 1) / n))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(c)
            rad = 6.0 if last else 2.5
            p.drawEllipse(QtCore.QPointF(X(x), Y(y)), rad, rad)
        p.end()


__all__ = ["XYPlot"]

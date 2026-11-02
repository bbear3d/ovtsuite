# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Live data tab of a connected module (docs/OVT Diag Interface
Options.pdf, page 1): on the left a filterable list of measuring groups, each
with a checkbox and the names of its cells; on the right the values of every
checked group as tiles, with the reading rate, a graph view and a log-to-file
button; underneath, the other modules that are live in the same session.

A view only: it reads nothing from the car. The window runs the reads on the
module's live session -- the checked groups in turn -- and hands every
reading to :meth:`set_readings`; the view draws, keeps the recent history for
the graph, and writes the CSV log. Qt only.

THE CONTRACT with :class:`ovtsuite.diag.gui_diag.DiagWindow` (keep it stable):

``GroupEntry(group, title, cells, served=True, note="")``
    ``cells``: the cell names (None for an unnamed cell), as the module's
    definitions or the factory catalogue name them.

signals
    ``selectionChanged(list)``      checked group numbers, in the order checked
    ``manualGroupRequested(int)``   the user typed a group number not in the list
    ``otherModuleActivated(str)``   a module of the "also live" strip was clicked (address)
    ``splitViewRequested()``        "Open in split view"

methods
    ``set_groups(groups, checked)``     fill the list; ``checked`` pre-ticks groups
    ``set_source_note(text)``           the line over the list ("Named from the loaded calibration")
    ``set_readings(group, cells)``      ``cells``: ``[Reading]``, one per cell, shown as tiles;
                                        values arrive repeatedly -- update in place, no flicker
    ``set_rate(frames_per_s)``          the "14.2 frames/s" readout
    ``set_running(bool)``               reads are flowing (dims the tiles when not)
    ``set_others(rows)``                ``[(addr, name, [(label, text)])]`` for the strip, or []
    ``checked_groups() -> list``
    ``start_log(path) / stop_log()`` and ``logging_active() -> bool``  CSV: one row per reading
                                        round, a column per cell (``addr group cell name``)
    ``clear()``                         forget readings and history (the module changed)

``Reading(name, text, unit, value, frac, state)``
    ``text``: the formatted value; ``value``: the number (None if not numeric);
    ``frac``: 0..1 where it sits in its range, or None for no bar;
    ``state``: "" | "warn" | "bad".

Additions to the contract (optional for the window; nothing above changes):

``loggingChanged(bool)``     signal: logging started / stopped (by the button, by
                             ``start_log`` / ``stop_log``, or by a write error)
``set_module_addr(addr)``    the module's address for the suggested log file name
                             (``live-01-20261027-101500.csv``)
``set_graph(bool)`` / ``graph_active()``    the Graph button, from code

Behaviour worth knowing:

* ``set_groups(groups, checked)``: ``checked`` is the whole new selection (an
  empty one clears it); it is applied without emitting ``selectionChanged``.
  Groups that are not in ``groups`` or are not served are not ticked.
* Tiles and CSV columns follow the order the groups were checked, then the
  cell order. A tile exists as soon as its group is checked (named from the
  group entry, value "-") and is updated in place by every ``set_readings``.
  A reading for a group that is not checked is ignored.
* ``clear()`` also ends a running log (the module changed).
* CSV (``start_log``): UTF-8, comma separated, CRLF line ends (``newline=""``),
  header ``time_s`` then one ``<group>:<cell>:<name>`` column per cell (cells
  numbered from 1), fixed when the header is written -- at ``start_log`` if
  groups are checked, otherwise at the first row. ROW RULE: readings are
  latched per cell; when a reading of the FIRST checked group arrives and
  readings came in since the last row, the latched values (the round that just
  ended) are written as one row stamped with the time of that round's last
  reading (seconds since ``start_log``) and the new reading begins the next
  round. Row times are at least 50 ms apart (a round ending sooner is merged into
  the next). ``stop_log`` writes the round in progress, then closes. A write or
  open error never raises: logging stops and a short message shows in the view.
"""
from __future__ import annotations

import csv
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from .gui_widgets import (Card, ClickableFrame, Dot, ValueTile, _Restyler, button_qss, mono_font,
                          muted_label, scaled_font, section_label, style_button, style_page,
                          style_panel, tokens)

Qt = QtCore.Qt

HISTORY_MAX = 600            # samples kept per cell
WINDOW_S = 60.0              # the graph shows the last minute
MIN_TILE_W = 190
TILE_GAP = 10
LOG_MIN_INTERVAL = 0.05      # seconds between CSV rows
AUTO_SERIES = 4              # series shown by default in the graph
_DASH = "–"
_DOT = "·"

_last_dir: str = ""          # the folder of the last log file, for the next save dialog


def _now() -> float:
    return time.monotonic()


@dataclass
class GroupEntry:
    group: int
    title: str = ""
    cells: List[Optional[str]] = field(default_factory=list)
    served: bool = True
    note: str = ""


@dataclass
class Reading:
    name: str = ""
    text: str = ""
    unit: str = ""
    value: Optional[float] = None
    frac: Optional[float] = None
    state: str = ""


def _cell_label(group: int, cell: int) -> str:
    """The tile name of an unnamed cell (``cell`` counts from 0)."""
    return "Group %03d cell %d" % (group, cell + 1)


def _why(err: Exception) -> str:
    return (getattr(err, "strerror", "") or str(err) or err.__class__.__name__).strip()


# ---------------------------------------------------------------- group list
class _GroupRow(QtWidgets.QWidget):
    """One row of the group list: checkbox, mono group number, bold title and
    a muted line of cell names. Checked rows get the accent-tinted fill."""
    toggled = QtCore.Signal(int, bool)

    def __init__(self, entry: GroupEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self._hover = False
        self._cb = QtWidgets.QCheckBox(self)
        self._cb.setAccessibleName("Group %03d %s" % (entry.group, entry.title))
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.addWidget(self._cb, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)
        self._cb.toggled.connect(self._on_toggled)
        self.setEnabled(entry.served)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(54)
        names = [n if n else "cell %d" % (i + 1) for i, n in enumerate(entry.cells)]
        self._sub = (entry.note if (not entry.served and entry.note) else (" %s " % _DOT).join(names))
        self.setToolTip("\n".join(filter(None, [entry.title, self._sub])))
        self.setCursor(Qt.CursorShape.PointingHandCursor if entry.served else Qt.CursorShape.ForbiddenCursor)

    def sizeHint(self):
        return QtCore.QSize(200, 54)

    def isChecked(self) -> bool:
        return self._cb.isChecked()

    def checkbox(self) -> QtWidgets.QCheckBox:
        return self._cb

    def set_checked(self, on: bool):
        """Programmatic: no ``toggled`` signal."""
        if self._cb.isChecked() != on:
            was = self._cb.blockSignals(True)
            self._cb.setChecked(on)
            self._cb.blockSignals(was)
            self.update()

    def _on_toggled(self, on: bool):
        self.update()
        self.toggled.emit(self.entry.group, on)

    def mouseReleaseEvent(self, ev):
        if (ev.button() == Qt.MouseButton.LeftButton and self.isEnabled()
                and self.rect().contains(ev.position().toPoint())):
            self._cb.toggle()
        super().mouseReleaseEvent(ev)

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def changeEvent(self, ev):
        if ev.type() in (QtCore.QEvent.Type.PaletteChange, QtCore.QEvent.Type.EnabledChange):
            self.update()
        super().changeEvent(ev)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        on = self.isEnabled()
        r = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if on and self._cb.isChecked():
            edge = QtGui.QColor(t.accent)
            edge.setAlpha(70)
            p.setBrush(t.accent_bg)
            p.setPen(QtGui.QPen(edge, 1))
            p.drawRoundedRect(r, 6, 6)
        elif on and self._hover:
            p.setBrush(t.card_hover)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, 6, 6)
        x = 12 + self._cb.sizeHint().width() + 10
        right = self.width() - 10
        base = self.font()
        nf, tf, sf = mono_font(base, 0.9), scaled_font(base, 1.0, True), scaled_font(base, 0.86)
        nfm, tfm, sfm = QtGui.QFontMetrics(nf), QtGui.QFontMetrics(tf), QtGui.QFontMetrics(sf)
        total = tfm.height() + 2 + sfm.height()
        y = (self.height() - total) / 2.0
        num = "%03d" % self.entry.group
        p.setFont(nf)
        p.setPen(t.muted if on else t.faint)
        p.drawText(QtCore.QPointF(x, y + (tfm.height() - nfm.height()) / 2.0 + nfm.ascent()), num)
        tx = x + nfm.horizontalAdvance(num) + 8
        p.setFont(tf)
        p.setPen(t.text if on else t.faint)
        p.drawText(QtCore.QPointF(tx, y + tfm.ascent()),
                   tfm.elidedText(self.entry.title, Qt.TextElideMode.ElideRight, max(10, int(right - tx))))
        p.setFont(sf)
        p.setPen(t.muted if on else t.faint)
        p.drawText(QtCore.QPointF(x, y + tfm.height() + 2 + sfm.ascent()),
                   sfm.elidedText(self._sub, Qt.TextElideMode.ElideRight, max(10, int(right - x))))


# ------------------------------------------------------------------ tile grid
class _TileGrid(QtWidgets.QWidget):
    """Tiles reflowed to as many columns as fit (``MIN_TILE_W`` each). The
    tiles are placed by hand (no layout), so a resize or a change of columns
    only moves them."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tiles: List[QtWidgets.QWidget] = []
        self._cols = 1
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFixedHeight(0)

    def columns(self) -> int:
        return self._cols

    def tiles(self) -> List[QtWidgets.QWidget]:
        return list(self._tiles)

    def set_tiles(self, tiles: Sequence[QtWidgets.QWidget]):
        self._tiles = list(tiles)
        for t in self._tiles:
            if t.parent() is not self:
                t.setParent(self)
            t.show()
        self._reflow()

    def _reflow(self):
        w = max(1, self.width())
        cols = max(1, (w + TILE_GAP) // (MIN_TILE_W + TILE_GAP))
        self._cols = cols
        n = len(self._tiles)
        if not n:
            self.setFixedHeight(0)
            return
        tw = (w - TILE_GAP * (cols - 1)) // cols
        th = self._tiles[0].sizeHint().height()
        for i, t in enumerate(self._tiles):
            r, c = divmod(i, cols)
            t.setGeometry(c * (tw + TILE_GAP), r * (th + TILE_GAP), tw, th)
        rows = (n + cols - 1) // cols
        h = rows * th + (rows - 1) * TILE_GAP
        if self.height() != h:
            self.setFixedHeight(h)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._reflow()


# ---------------------------------------------------------------- strip chart
_SERIES_DARK = ("#3ab3c1", "#e2a83a", "#4cbf91", "#e07269", "#9b8cf0", "#5aa0e8", "#e58fc0", "#c3d35a")
_SERIES_LIGHT = ("#0e7c89", "#a8650a", "#1f7a4d", "#b23b2a", "#6a4fd0", "#1f6fc0", "#b8407f", "#7a8a0a")


def _series_color(idx: int, dark: bool) -> QtGui.QColor:
    pal = _SERIES_DARK if dark else _SERIES_LIGHT
    return QtGui.QColor(pal[idx % len(pal)])


class _Series:
    __slots__ = ("key", "name", "unit", "text", "hist", "color_idx", "visible")

    def __init__(self, key, color_idx: int, visible: bool):
        self.key = key
        self.name = ""
        self.unit = ""
        self.text = ""
        self.hist: Deque[Tuple[float, float]] = deque(maxlen=HISTORY_MAX)
        self.color_idx = color_idx
        self.visible = visible


class _Canvas(Card):
    """The plot: the last minute of every visible series, each normalised to
    its own min..max over the window."""

    def __init__(self, chart: "StripChart"):
        super().__init__(chart)
        self._chart = chart
        self.setMinimumHeight(220)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        self._paint_card(p, t)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        small = scaled_font(self.font(), 0.8)
        fm = QtGui.QFontMetrics(small)
        plot = QtCore.QRectF(self.rect()).adjusted(40, 14, -14, -(fm.height() + 14))
        if plot.width() < 20 or plot.height() < 20:
            return
        p.setFont(small)
        for k in range(5):
            y = plot.top() + plot.height() * k / 4.0
            p.setPen(QtGui.QPen(t.border, 1))
            p.drawLine(QtCore.QPointF(plot.left(), y), QtCore.QPointF(plot.right(), y))
        p.setPen(t.faint)
        right_mid = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        p.drawText(QtCore.QRectF(0, plot.top() - 7, 36, 14), right_mid, "max")
        p.drawText(QtCore.QRectF(0, plot.bottom() - 7, 36, 14), right_mid, "min")
        for frac, label in ((0.0, "-60 s"), (0.5, "-30 s"), (1.0, "now")):
            x = plot.left() + plot.width() * frac
            w = fm.horizontalAdvance(label)
            xl = min(max(x - w / 2.0, plot.left() - 4), plot.right() + 14 - w)
            p.drawText(QtCore.QPointF(xl, plot.bottom() + 6 + fm.ascent()), label)
        shown = [s for s in self._chart.series if s.visible and s.hist]
        if not shown:
            p.setPen(t.muted)
            msg = ("Waiting for numeric readings." if not self._chart.series
                   else "No series shown. Click a name on the right to show it.")
            p.setFont(self.font())
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, msg)
            return
        start = self._chart.latest_time() - WINDOW_S
        p.setClipRect(plot.adjusted(-2, -2, 2, 2))
        for s in shown:
            pts = [(tt, v) for tt, v in s.hist if tt >= start]
            if not pts:
                continue
            lo = min(v for _, v in pts)
            hi = max(v for _, v in pts)
            span = hi - lo
            path = QtGui.QPainterPath()
            x = y = 0.0
            for j, (tt, v) in enumerate(pts):
                x = plot.left() + plot.width() * (tt - start) / WINDOW_S
                y = plot.bottom() - plot.height() * (0.5 if span <= 0 else (v - lo) / span)
                if j == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            if len(pts) == 1:
                path.lineTo(x + 0.1, y)
            pen = QtGui.QPen(_series_color(s.color_idx, t.dark), 1.7)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)


class _Legend(QtWidgets.QWidget):
    """Swatch, name and current value per series; a click hides / shows it."""
    ROW = 28

    def __init__(self, chart: "StripChart"):
        super().__init__(chart)
        self._chart = chart
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(220)

    def sizeHint(self):
        return QtCore.QSize(240, max(40, len(self._chart.series) * self.ROW + 8))

    def index_at(self, pos) -> int:
        i = int((pos.y() - 4) // self.ROW)
        return i if 0 <= i < len(self._chart.series) else -1

    def mouseMoveEvent(self, ev):
        i = self.index_at(ev.position().toPoint())
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, ev):
        self._hover = -1
        self.update()
        super().leaveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            i = self.index_at(ev.position().toPoint())
            if i >= 0:
                self._chart.toggle(self._chart.series[i].key)
        super().mouseReleaseEvent(ev)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        nf = scaled_font(self.font(), 0.92)
        vf = mono_font(self.font(), 0.9)
        nfm, vfm = QtGui.QFontMetrics(nf), QtGui.QFontMetrics(vf)
        for i, s in enumerate(self._chart.series):
            r = QtCore.QRectF(0, 4 + i * self.ROW, self.width(), self.ROW)
            if i == self._hover:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(t.card_hover)
                p.drawRoundedRect(r.adjusted(0.5, 1, -0.5, -1), 5, 5)
            col = _series_color(s.color_idx, t.dark)
            sw = QtCore.QRectF(r.left() + 8, r.center().y() - 5, 10, 10)
            if s.visible:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
            else:
                p.setPen(QtGui.QPen(t.faint, 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(sw, 3, 3)
            val = ("%s %s" % (s.text, s.unit)).strip()
            p.setFont(vf)
            vw = min(vfm.horizontalAdvance(val), int(r.width() * 0.5))
            p.setPen(t.text if s.visible else t.faint)
            p.drawText(QtCore.QPointF(r.right() - 8 - vw, r.center().y() + (vfm.ascent() - vfm.descent()) / 2.0),
                       vfm.elidedText(val, Qt.TextElideMode.ElideRight, vw))
            p.setFont(nf)
            nx = sw.right() + 8
            p.drawText(QtCore.QPointF(nx, r.center().y() + (nfm.ascent() - nfm.descent()) / 2.0),
                       nfm.elidedText(s.name, Qt.TextElideMode.ElideRight, max(10, int(r.right() - 8 - vw - 8 - nx))))


class StripChart(QtWidgets.QWidget):
    """The graph view: a painted strip chart plus its legend. History is kept
    here per cell whatever view is showing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.series: List[_Series] = []
        self._by_key: Dict[Tuple[int, int], _Series] = {}
        self._next_color = 0
        self._latest_t = 0.0
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.canvas = _Canvas(self)
        self.legend = _Legend(self)
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidget(self.legend)
        self.legend.setAutoFillBackground(False)
        self._scroll.viewport().setAutoFillBackground(False)
        self.legend.setAutoFillBackground(False)
        self._scroll.setFixedWidth(250)
        lay.addWidget(self.canvas, 1)
        lay.addWidget(self._scroll, 0)

    def latest_time(self) -> float:
        return self._latest_t

    def push(self, key, name: str, unit: str, text: str, value: Optional[float], t: float):
        s = self._by_key.get(key)
        if s is None:
            if value is None:
                return                      # a series starts with its first number
            s = _Series(key, self._next_color, len(self.series) < AUTO_SERIES)
            self._next_color += 1
            self._by_key[key] = s
            self.series.append(s)
            self.legend.updateGeometry()
        s.name, s.unit, s.text = name, unit, text
        if value is not None:
            s.hist.append((t, float(value)))
            if t > self._latest_t:
                self._latest_t = t

    def toggle(self, key):
        s = self._by_key.get(key)
        if s is not None:
            s.visible = not s.visible
            self.refresh()

    def drop_group(self, group: int):
        keep = [s for s in self.series if s.key[0] != group]
        if len(keep) != len(self.series):
            self.series = keep
            self._by_key = {s.key: s for s in keep}
            self.legend.updateGeometry()

    def clear(self):
        self.series = []
        self._by_key = {}
        self._next_color = 0
        self._latest_t = 0.0
        self.legend.updateGeometry()
        self.refresh()

    def refresh(self):
        self.canvas.update()
        self.legend.update()

    # for the tests and the window
    def keys(self) -> List[Tuple[int, int]]:
        return [s.key for s in self.series]

    def visible_keys(self) -> List[Tuple[int, int]]:
        return [s.key for s in self.series if s.visible]

    def history(self, key) -> List[Tuple[float, float]]:
        s = self._by_key.get(key)
        return list(s.hist) if s else []


# ------------------------------------------------------------ other modules
class _StripItems(QtWidgets.QWidget):
    """``Gear N   TCC slip 0 rpm ...``: a muted label and a bold value each,
    cut off with an ellipsis when the row is too narrow."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Tuple[str, str]] = []
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)

    def set_items(self, items):
        self._items = [(str(a), str(b)) for a, b in items]
        self.update()

    def minimumSizeHint(self):
        return QtCore.QSize(0, 20)

    def sizeHint(self):
        return QtCore.QSize(200, 20)

    def paintEvent(self, _ev):
        t = tokens(self.palette())
        p = QtGui.QPainter(self)
        lf = scaled_font(self.font(), 0.95)
        vf = scaled_font(self.font(), 0.95, True)
        lfm, vfm = QtGui.QFontMetrics(lf), QtGui.QFontMetrics(vf)
        base = self.height() / 2.0 + (lfm.ascent() - lfm.descent()) / 2.0
        x = 0.0
        for label, val in self._items:
            need = lfm.horizontalAdvance(label + " ") + vfm.horizontalAdvance(val)
            if x + need > self.width():
                p.setPen(t.muted)
                p.setFont(lf)
                p.drawText(QtCore.QPointF(x, base), "…")
                break
            p.setFont(lf)
            p.setPen(t.muted)
            p.drawText(QtCore.QPointF(x, base), label)
            x += lfm.horizontalAdvance(label + " ")
            p.setFont(vf)
            p.setPen(t.text)
            p.drawText(QtCore.QPointF(x, base), val)
            x += vfm.horizontalAdvance(val) + 20


class _OtherRow(ClickableFrame):
    """``02 Transmission  also live, same session  Gear N ...  Open in split view``."""

    def __init__(self, addr: str, name: str, items, with_link: bool, view: "LiveDataView"):
        super().__init__(view, radius=8)
        self.addr = addr
        self.setMinimumWidth(0)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(10)
        num = QtWidgets.QLabel(addr)
        num.setFont(mono_font(num.font(), 0.95))
        num.setStyleSheet("color: palette(placeholder-text);")
        nm = QtWidgets.QLabel(name)
        nm.setFont(scaled_font(nm.font(), 1.0, True))
        also = muted_label("also live, same session", scale=0.9)
        self.items = _StripItems()
        self.items.set_items(items)
        for w in (num, nm, also, self.items):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(num)
        lay.addWidget(nm)
        lay.addWidget(also)
        lay.addSpacing(6)
        lay.addWidget(self.items, 1)
        self.link: Optional[QtWidgets.QPushButton] = None
        if with_link:
            self.link = QtWidgets.QPushButton("Open in split view →")
            style_button(self.link, "ghost")
            self.link.clicked.connect(view.splitViewRequested)
            lay.addWidget(self.link)
        self.clicked.connect(lambda: view.otherModuleActivated.emit(self.addr))


class _LogButton(QtWidgets.QPushButton):
    """The primary ``Log to file`` button; a red dot sits inside its left edge
    while logging."""

    def __init__(self, parent=None):
        super().__init__("Log to file", parent)
        self.dot = Dot("bad", 9, self)
        self.dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.dot.hide()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.dot.move(11, (self.height() - self.dot.height()) // 2)


# ------------------------------------------------------------------- the view
class LiveDataView(QtWidgets.QWidget):
    selectionChanged = QtCore.Signal(list)
    manualGroupRequested = QtCore.Signal(int)
    otherModuleActivated = QtCore.Signal(str)
    splitViewRequested = QtCore.Signal()
    loggingChanged = QtCore.Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: Dict[int, GroupEntry] = {}
        self._rows: Dict[int, _GroupRow] = {}
        self._checked: List[int] = []
        self._tiles: Dict[int, List[ValueTile]] = {}
        self._latest: Dict[Tuple[int, int], Tuple[Optional[float], str]] = {}
        self._rate: Optional[float] = None
        self._running = True
        self._graph = False
        self._addr = ""
        # logging
        self._log_file = None
        self._log_writer = None
        self._log_t0 = 0.0
        self._log_cols: Optional[List[Tuple[int, int]]] = None
        self._log_pending = False
        self._log_pending_t = 0.0
        self._log_last_row: Optional[float] = None
        self._log_path = ""

        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_left())
        outer.addWidget(self._build_right(), 1)
        self._refresh_state()

    # ----------------------------------------------------------- construction
    def _build_left(self) -> QtWidgets.QWidget:
        left = QtWidgets.QWidget()
        style_panel(left, "liveLeft", "right")
        left.setFixedWidth(300)
        lay = QtWidgets.QVBoxLayout(left)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self._filter = QtWidgets.QLineEdit()
        self._filter.setPlaceholderText("Filter by name... e.g. boost")
        self._filter.setClearButtonEnabled(True)
        self._filter.setStyleSheet("QLineEdit{border:1px solid palette(mid); border-radius:6px;"
                                   " padding:6px 8px; background:palette(base);}"
                                   "QLineEdit:focus{border-color:palette(highlight);}")
        self._filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self._filter)
        self._note = section_label("")
        self._note.setContentsMargins(2, 4, 0, 0)
        self._note.hide()
        lay.addWidget(self._note)

        self._list_scroll = QtWidgets.QScrollArea()
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_scroll.viewport().setAutoFillBackground(False)
        inner = QtWidgets.QWidget()
        inner.setAutoFillBackground(False)
        self._list_lay = QtWidgets.QVBoxLayout(inner)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(4)
        self._list_empty = muted_label("")
        self._list_empty.setWordWrap(True)
        self._list_empty.setContentsMargins(6, 6, 6, 6)
        self._list_empty.hide()
        self._list_lay.addWidget(self._list_empty)
        self._list_lay.addStretch(1)
        self._list_scroll.setWidget(inner)
        inner.setAutoFillBackground(False)      # setWidget turns it on
        lay.addWidget(self._list_scroll, 1)

        add = QtWidgets.QHBoxLayout()
        add.setSpacing(8)
        add.addWidget(muted_label("Group number"))
        self._spin = QtWidgets.QSpinBox()
        self._spin.setRange(0, 255)
        self._spin.setValue(1)
        self._spin.setAccessibleName("Group number")
        self._add = QtWidgets.QPushButton("Add")
        style_button(self._add, "outline")
        self._add.clicked.connect(self._on_add)
        self._spin.lineEdit().returnPressed.connect(self._on_add)
        add.addWidget(self._spin)
        add.addWidget(self._add)
        add.addStretch(1)
        lay.addLayout(add)
        return left

    def _build_right(self) -> QtWidgets.QWidget:
        right = QtWidgets.QWidget()
        style_page(right, "liveRight")
        lay = QtWidgets.QVBoxLayout(right)
        lay.setContentsMargins(16, 14, 16, 10)
        lay.setSpacing(8)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)
        self._hdr = QtWidgets.QLabel()
        self._hdr.setTextFormat(Qt.TextFormat.RichText)
        self._stopped = muted_label("reads stopped")
        self._stopped.hide()
        head.addWidget(self._hdr)
        head.addWidget(self._stopped)
        head.addStretch(1)
        self._graph_btn = QtWidgets.QPushButton("Graph")
        self._graph_btn.setCheckable(True)
        _Restyler(self._graph_btn, self._graph_qss)
        self._graph_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._graph_btn.toggled.connect(self._on_graph_toggled)
        self._log_btn = _LogButton()
        _Restyler(self._log_btn, self._log_qss)
        self._log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._log_btn.clicked.connect(self._on_log_clicked)
        head.addWidget(self._graph_btn)
        head.addWidget(self._log_btn)
        lay.addLayout(head)

        self._notice = QtWidgets.QLabel()
        self._notice.setWordWrap(True)
        self._notice.hide()
        lay.addWidget(self._notice)
        self._notice_timer = QtCore.QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(10000)
        self._notice_timer.timeout.connect(self._notice.hide)

        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        content = QtWidgets.QWidget()
        content.setAutoFillBackground(False)
        cl = QtWidgets.QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(10)
        self._grid = _TileGrid()
        self._message = QtWidgets.QLabel()
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setMinimumHeight(200)
        self._message.setStyleSheet("color: palette(placeholder-text);")
        self._chart = StripChart()
        self._chart.hide()
        self._strip = QtWidgets.QWidget()
        self._strip_lay = QtWidgets.QVBoxLayout(self._strip)
        self._strip_lay.setContentsMargins(0, 0, 0, 0)
        self._strip_lay.setSpacing(6)
        self._strip.hide()
        cl.addWidget(self._grid)
        cl.addWidget(self._message)
        cl.addWidget(self._chart, 1000)
        cl.addWidget(self._strip)
        cl.addStretch(1)
        self._scroll.setWidget(content)
        content.setAutoFillBackground(False)    # setWidget turns it on
        lay.addWidget(self._scroll, 1)
        self._style_log_button()
        return right

    def _graph_qss(self) -> str:
        t = tokens()
        c = lambda col: col.name(QtGui.QColor.NameFormat.HexArgb)           # noqa: E731
        return (button_qss("outline", t)
                + "QPushButton:checked{color:%s; border-color:%s; background:%s;}"
                % (c(t.accent), c(t.accent), c(t.accent_bg)))

    def _log_qss(self) -> str:
        pad = "QPushButton{padding-left:26px;}" if self._log_file is not None else ""
        return button_qss("primary") + pad

    def _style_log_button(self):
        on = self._log_file is not None
        self._log_btn.setText("Stop logging" if on else "Log to file")
        self._log_btn.dot.setVisible(on)
        self._log_btn.setStyleSheet(self._log_qss())
        self._log_btn.setToolTip("Stop writing the CSV file" if on else
                                 "Write every reading round to a CSV file")

    # ------------------------------------------------------------------ groups
    def set_groups(self, groups: Sequence[GroupEntry], checked: Sequence[int] = ()) -> None:
        """Fill the list. ``checked`` is the whole new selection; no signal."""
        for g in list(self._tiles):
            self._drop_group_tiles(g)
        for row in self._rows.values():
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._rows, self._entries = {}, {}
        for e in groups:
            if e.group in self._entries:
                continue
            self._entries[e.group] = e
            row = _GroupRow(e)
            row.toggled.connect(self._on_row_toggled)
            self._rows[e.group] = row
            self._list_lay.insertWidget(self._list_lay.count() - 1, row)
        want: List[int] = []
        for g in checked:
            g = int(g)
            if g in self._entries and self._entries[g].served and g not in want:
                want.append(g)
        self._checked = want
        for g, row in self._rows.items():
            row.set_checked(g in want)
        self._apply_filter()
        self._sync_tiles()
        self._refresh_state()

    def set_source_note(self, text: str) -> None:
        self._note.setText((text or "").upper())
        self._note.setVisible(bool(text))

    def checked_groups(self) -> List[int]:
        return list(self._checked)

    def _apply_filter(self, *_):
        q = self._filter.text().strip().lower()
        shown = 0
        for g, row in self._rows.items():
            e = self._entries[g]
            hit = (not q or q in e.title.lower() or q in ("%03d" % g) or q in str(g)
                   or any(c and q in c.lower() for c in e.cells))
            row.setVisible(hit)
            shown += hit
        if not self._entries:
            self._list_empty.setText("No groups yet.")
            self._list_empty.show()
        elif not shown:
            self._list_empty.setText("No group or cell matches \"%s\"." % self._filter.text().strip())
            self._list_empty.show()
        else:
            self._list_empty.hide()

    def visible_groups(self) -> List[int]:
        return [g for g, r in self._rows.items() if not r.isHidden()]

    def row(self, group: int) -> Optional[_GroupRow]:
        return self._rows.get(group)

    def _on_row_toggled(self, group: int, on: bool):
        if on and group not in self._checked:
            self._checked.append(group)
        elif not on and group in self._checked:
            self._checked.remove(group)
        else:
            return
        self._sync_tiles()
        self._refresh_state()
        self.selectionChanged.emit(list(self._checked))

    def _on_add(self):
        n = self._spin.value()
        if n in self._rows:
            row = self._rows[n]
            if row.isEnabled():
                self._filter.clear()
                row.checkbox().setChecked(True)
            return
        self.manualGroupRequested.emit(n)

    # ------------------------------------------------------------------- tiles
    def _placeholder_name(self, group: int, i: int) -> str:
        e = self._entries.get(group)
        if e is not None and i < len(e.cells) and e.cells[i]:
            return e.cells[i]
        return _cell_label(group, i)

    def _make_tile(self, group: int, i: int) -> ValueTile:
        tile = ValueTile(self._grid)
        tile.set_value(self._placeholder_name(group, i), _DASH)
        return tile

    @staticmethod
    def _discard(tile: ValueTile):
        tile.hide()
        tile.setParent(None)
        tile.deleteLater()

    def _drop_group_tiles(self, group: int):
        for tile in self._tiles.pop(group, []):
            self._discard(tile)
        self._chart.drop_group(group)
        for k in [k for k in self._latest if k[0] == group]:
            del self._latest[k]

    def _sync_tiles(self):
        for g in [g for g in self._tiles if g not in self._checked]:
            self._drop_group_tiles(g)
        for g in self._checked:
            if g not in self._tiles:
                e = self._entries.get(g)
                self._tiles[g] = [self._make_tile(g, i) for i in range(len(e.cells) if e else 0)]
        self._relayout_tiles()

    def _relayout_tiles(self):
        flat: List[ValueTile] = []
        for g in self._checked:
            flat.extend(self._tiles.get(g, []))
        self._grid.set_tiles(flat)

    def tiles(self) -> List[ValueTile]:
        return self._grid.tiles()

    def tile_groups(self) -> List[Tuple[int, int]]:
        """``(group, cell)`` of every tile, in tile order."""
        return [(g, i) for g in self._checked for i in range(len(self._tiles.get(g, [])))]

    def column_count(self) -> int:
        return self._grid.columns()

    # ---------------------------------------------------------------- readings
    def set_readings(self, group: int, cells: Sequence[Reading]) -> None:
        tiles = self._tiles.get(group)
        if tiles is None:
            return
        n = len(cells)
        if n != len(tiles):
            while len(tiles) < n:
                tiles.append(self._make_tile(group, len(tiles)))
            while len(tiles) > n:
                self._discard(tiles.pop())
                self._latest.pop((group, len(tiles)), None)
            self._relayout_tiles()
            self._refresh_state()
        now = _now()
        logging_on = self._log_file is not None
        if logging_on and self._checked and group == self._checked[0]:
            self._log_round_boundary()
        latest = self._latest
        push = self._chart.push
        for i, r in enumerate(cells):
            name = r.name or _cell_label(group, i)
            tiles[i].set_value(name, r.text, r.unit, r.frac, r.state)
            key = (group, i)
            latest[key] = (r.value, r.text)
            push(key, name, r.unit, r.text, r.value, now)
        if logging_on:
            self._log_pending = True
            self._log_pending_t = now
        if self._graph:
            self._chart.refresh()

    def set_rate(self, frames_per_s: float) -> None:
        self._rate = None if frames_per_s is None else float(frames_per_s)
        self._update_header()

    def set_running(self, on: bool) -> None:
        on = bool(on)
        if on == self._running:
            return
        self._running = on
        self._stopped.setVisible(not on)
        for w in (self._grid, self._chart.canvas):
            if on:
                w.setGraphicsEffect(None)
            else:
                eff = QtWidgets.QGraphicsOpacityEffect(w)
                eff.setOpacity(0.45)
                w.setGraphicsEffect(eff)
        self._update_header()

    def is_running(self) -> bool:
        return self._running

    def _update_header(self):
        n_groups = len(self._checked)
        n_vals = sum(len(v) for v in self._tiles.values())
        parts = ["%d group%s" % (n_groups, "" if n_groups == 1 else "s")]
        if self._running and self._rate is not None:
            parts.append("<b>%.1f</b> frames/s" % self._rate)
        parts.append("%d value%s" % (n_vals, "" if n_vals == 1 else "s"))
        text = (" %s " % _DOT).join(parts)
        if text != self._hdr.text():
            self._hdr.setText(text)

    def header_text(self) -> str:
        """The header line as plain text (``3 groups . 14.2 frames/s . 12 values``)."""
        t = QtGui.QTextDocumentFragment.fromHtml(self._hdr.text()).toPlainText()
        return t + ((" %s reads stopped" % _DOT) if not self._running else "")

    def _refresh_state(self):
        self._update_header()
        if self._graph:
            self._message.hide()
            return
        if not self._entries:
            self._message.setText("No groups to read. Type a group number on the left.")
        elif not self._checked:
            self._message.setText("Tick one or more groups on the left.")
        else:
            self._message.setText("")
        self._message.setVisible(bool(self._message.text()))

    def message_text(self) -> str:
        return self._message.text() if not self._message.isHidden() else ""

    # ------------------------------------------------------------------- graph
    def _on_graph_toggled(self, on: bool):
        self._graph = on
        self._grid.setVisible(not on)
        self._chart.setVisible(on)
        self._refresh_state()
        if on:
            self._chart.refresh()

    def set_graph(self, on: bool) -> None:
        self._graph_btn.setChecked(bool(on))

    def graph_active(self) -> bool:
        return self._graph

    def chart(self) -> StripChart:
        return self._chart

    # ------------------------------------------------------------ other modules
    def set_others(self, rows: Sequence[Tuple[str, str, Sequence[Tuple[str, str]]]]) -> None:
        while self._strip_lay.count():
            w = self._strip_lay.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        for k, (addr, name, items) in enumerate(rows or []):
            self._strip_lay.addWidget(_OtherRow(addr, name, items, k == 0, self))
        self._strip.setVisible(bool(rows))

    def other_rows(self) -> List[_OtherRow]:
        return [self._strip_lay.itemAt(i).widget() for i in range(self._strip_lay.count())]

    # ------------------------------------------------------------------ logging
    def set_module_addr(self, addr: str) -> None:
        self._addr = str(addr or "")

    def default_log_name(self) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return "live-%s%s.csv" % (self._addr + "-" if self._addr else "", stamp)

    def logging_active(self) -> bool:
        return self._log_file is not None

    def _on_log_clicked(self):
        global _last_dir
        if self._log_file is not None:
            self.stop_log()
            return
        folder = _last_dir if _last_dir and os.path.isdir(_last_dir) else os.path.expanduser("~")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Log live data to file", os.path.join(folder, self.default_log_name()),
            "CSV files (*.csv);;All files (*)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv"
        _last_dir = os.path.dirname(os.path.abspath(path))
        self.start_log(path)

    def _tile_name(self, key) -> str:
        tiles = self._tiles.get(key[0], [])
        return tiles[key[1]]._name if key[1] < len(tiles) else _cell_label(*key)

    def _log_write_header(self):
        cols = self.tile_groups()
        if not cols:
            return                          # nothing to name yet: at the first row
        self._log_cols = cols
        self._log_writer.writerow(["time_s"] + ["%d:%d:%s" % (g, i + 1, self._tile_name((g, i)))
                                                 for g, i in cols])
        self._log_file.flush()

    def start_log(self, path: str) -> None:
        self.stop_log()
        try:
            f = open(path, "w", encoding="utf-8", newline="")
        except (OSError, ValueError) as e:
            self._show_notice("Cannot log to %s: %s" % (os.path.basename(str(path)) or path, _why(e)))
            return
        self._log_file, self._log_writer, self._log_path = f, csv.writer(f), str(path)
        self._log_t0 = _now()
        self._log_cols = None
        self._log_pending = False
        self._log_last_row = None
        self._notice.hide()
        try:
            self._log_write_header()
        except (OSError, ValueError, csv.Error) as e:
            self._log_failed(e)
            return
        self._style_log_button()
        self.loggingChanged.emit(True)

    def _log_round_boundary(self):
        """A reading of the first checked group arrived: the round that was in
        progress is complete, write it (row times are at least 50 ms apart)."""
        if not self._log_pending:
            return
        if (self._log_last_row is not None
                and self._log_pending_t - self._log_last_row < LOG_MIN_INTERVAL):
            return
        self._log_write_row(self._log_pending_t)

    def _log_write_row(self, t: float):
        try:
            if self._log_cols is None:
                self._log_write_header()
                if self._log_cols is None:
                    return
            row: List = ["%.3f" % max(0.0, t - self._log_t0)]
            for key in self._log_cols:
                v = self._latest.get(key)
                if v is None:
                    row.append("")
                else:
                    row.append(v[0] if v[0] is not None else v[1])
            self._log_writer.writerow(row)
            self._log_file.flush()
        except (OSError, ValueError, csv.Error) as e:
            self._log_failed(e)
            return
        self._log_pending = False
        self._log_last_row = t

    def stop_log(self) -> None:
        if self._log_file is None:
            return
        if self._log_pending:
            self._log_write_row(self._log_pending_t)
        if self._log_file is None:          # the final write failed
            return
        self._close_log()
        self.loggingChanged.emit(False)

    def _close_log(self):
        f = self._log_file
        self._log_file = self._log_writer = None
        self._log_cols = None
        self._log_pending = False
        try:
            if f is not None:
                f.close()
        except (OSError, ValueError):
            pass
        self._style_log_button()

    def _log_failed(self, err: Exception):
        name = os.path.basename(self._log_path) or self._log_path
        self._close_log()
        self._show_notice("Logging stopped, cannot write %s: %s" % (name, _why(err)))
        self.loggingChanged.emit(False)

    def _show_notice(self, text: str):
        self._notice.setText(text)
        self._notice.setStyleSheet("color:%s;" % tokens().bad.name())
        self._notice.show()
        self._notice_timer.start()

    def notice_text(self) -> str:
        return self._notice.text() if not self._notice.isHidden() else ""

    # ------------------------------------------------------------------- reset
    def clear(self) -> None:
        """Forget readings and history (the module changed); ends a log."""
        self.stop_log()
        self._latest.clear()
        self._chart.clear()
        self._rate = None
        for g, tiles in self._tiles.items():
            for i, tile in enumerate(tiles):
                tile.set_value(self._placeholder_name(g, i), _DASH)
        self._update_header()

    def closeEvent(self, ev):
        self.stop_log()
        super().closeEvent(ev)

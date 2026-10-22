# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The shift-map editor window (Tools > ZF 6HP > Shift map editor...).

Edits many schedules at once through :mod:`ovtsuite.gearbox_edit`. A gearbox
module supplies the schedules (``PROVIDERS``); this window knows no gearbox.

Layout, left to right:

* **Schedules** -- presets (a coding, a mode, a grid row or column, the
  byte-identical groups), a range field (``10-24, 31``), saved sets kept with
  the project, and the tree to tick schedules in by hand;
* **Chart + table** -- the chosen line over pedal % for every ticked
  schedule, in engine rpm by default: the line as it is (faint), as it would
  be (solid), the opposite shift dashed; the table lists the same values;
* **Edit** -- the step being set up (previewed live), the steps already
  added, the checks with their auto-fixes, what the checks found, and Apply:
  one undoable step, optionally saved as a project version with a label.

Nothing is locked and no check blocks Apply.
"""
from __future__ import annotations

import colorsys
import importlib
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import gearbox_edit as GE
from . import speedview as _sv

#: (platform, kind) -> (module, function) returning a ScheduleSet for a session.
PROVIDERS = {
    ("zf6hp", "shift"): ("ovtsuite.zf6hp.shiftedit6hp", "schedule_set"),
    ("zf6hp", "tcc"): ("ovtsuite.zf6hp.tccedit6hp", "schedule_set"),
    ("zf8hp", "shift"): ("ovtsuite.zf8hp.shiftedit8hp", "schedule_set"),
    ("zf8hp", "tcc"): ("ovtsuite.zf8hp.shiftedit8hp", "tcc_schedule_set"),
}
TITLES = {"shift": "Shift map editor", "tcc": "Lock-up (TCC) editor"}

UNITS = (("engine", "Engine rpm"), ("output", "Output rpm"), ("kmh", "km/h"))
DIRECTIONS = (("any", "Raise or lower"), ("raise", "Raise only"),
              ("lower", "Lower only"))
OPS = (("offset", "Offset"), ("scale", "Scale"), ("slide", "Slide along pedal"),
       ("band", "Band (keep order)"), ("set", "Set to value"),
       ("copy", "Copy from schedule"), ("smooth", "Smooth"),
       ("track", "Follow an upshift"), ("gap", "Set gap to the paired line"),
       ("off", "Never (write \u201coff\u201d)"),
       ("fullload", "Full-load optimum (engine torque)"),
       ("partload", "Part-load strategy (economy / sport)"),
       ("pedalaxis", "Pedal axis to the ECU's full torque"))
#: Operations only one kind of editor offers.
OPS_ONLY = {"track": "tcc", "off": "tcc", "fullload": "shift", "partload": "shift",
            "pedalaxis": "shift"}
_SETS_KEY = "schedule_sets"
_BLENDS_KEY = "blends"
#: platform -> module giving default blends and project log events.
LOG_PROVIDERS = {"zf6hp": "ovtsuite.zf6hp.logevents6hp"}


def _log_module(session):
    name = LOG_PROVIDERS.get(getattr(session, "platform", ""))
    if not name:
        return None
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def schedule_set_for(session, kind: str = "shift") -> Optional[GE.ScheduleSet]:
    """The editable set of ``kind`` for ``session``'s platform, or None."""
    spec = PROVIDERS.get((getattr(session, "platform", ""), kind))
    if spec is None:
        return None
    try:
        mod = importlib.import_module(spec[0])
    except ImportError:
        return None
    sset = getattr(mod, spec[1])(session)
    return sset if sset.schedules else None


def available(session, kind: str = "shift") -> bool:
    return (getattr(session, "platform", ""), kind) in PROVIDERS


def _hue(i: int, n: int, dark: bool) -> QtGui.QColor:
    h = (i * 0.618033988749895) % 1.0
    r, g, b = colorsys.hls_to_rgb(h, 0.62 if dark else 0.42, 0.65)
    return QtGui.QColor(int(r * 255), int(g * 255), int(b * 255))


def _nice_ticks(lo: float, hi: float, want: int = 6) -> List[float]:
    """Round-number ticks inside ``[lo, hi]``."""
    import math
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / want
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10)), key=lambda st: abs(st - raw))
    first = math.ceil(lo / step) * step
    out, v = [], first
    while v <= hi + 1e-9:
        out.append(v)
        v += step
    return out


class _Flow(QtWidgets.QLayout):
    """Lays its widgets out left to right and wraps onto the next row when
    the width runs out -- the line buttons (a 6-speed has eleven, and the
    lock-up labels are long), so they never set the window's width."""

    def __init__(self, parent=None, spacing: int = 4):
        super().__init__(parent)
        self._items: List[QtWidgets.QLayoutItem] = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QtCore.QRect(0, 0, width, 0), False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        # the widest item, whole: one per row at the narrowest, never clipped
        size = QtCore.QSize()
        for it in self._items:
            size = size.expandedTo(it.sizeHint())
        return size

    def _place(self, rect, apply: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        sp = self.spacing()
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + sp, 0
            if apply:
                it.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += hint.width() + sp
            line = max(line, hint.height())
        return y + line - rect.y()


def _is_dark() -> bool:
    pal = QtWidgets.QApplication.palette()
    return pal.color(QtGui.QPalette.Window).lightness() < 128


# --------------------------------------------------------------------- chart
class ScheduleChart(QtWidgets.QWidget):
    """One line of many schedules over pedal %: before faint, after solid,
    the paired line dashed. Hover names the nearest schedule; a click on it
    emits ``picked``."""

    picked = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 220)
        self.setMouseTracking(True)
        self.series: List[dict] = []
        self.unit = ""
        self.title = ""
        self.highlight: Optional[str] = None
        self._xform = None
        #: ``{"effective": [(label, before, after)], "points": [dict]}``
        self.overlay: dict = {}

    def set_series(self, series, unit: str, title: str, overlay: Optional[dict] = None):
        self.series, self.unit, self.title = series, unit, title
        self.overlay = overlay or {}
        self.update()

    def _bounds(self):
        ys = [v for s in self.series for k in ("before", "after", "pair_before", "pair_after")
              for _p, v in (s.get(k) or ())]
        ys += [v for s in self.series for ref in (s.get("refs") or ()) for _p, v in ref]
        for _lab, before, after in self.overlay.get("effective") or ():
            ys += [v for _p, v in list(before) + list(after)]
        ys += [pt["y"] for pt in self.overlay.get("points") or ()]
        if not ys:
            return 0.0, 1.0
        lo, hi = min(ys), max(ys)
        if hi == lo:
            hi = lo + 1
        pad = (hi - lo) * 0.06
        return lo - pad, hi + pad

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QtGui.QPalette.Base))
        fg = pal.color(QtGui.QPalette.Text)
        grid = QtGui.QColor(fg)
        grid.setAlpha(40)
        m_l, m_r, m_t, m_b = 64, 14, 26, 34
        W, H = self.width() - m_l - m_r, self.height() - m_t - m_b
        if W < 20 or H < 20:
            return
        lo, hi = self._bounds()
        x0, x1 = 0.0, 100.4

        def X(px):
            return m_l + (px - x0) / (x1 - x0) * W

        def Y(v):
            return m_t + H - (v - lo) / (hi - lo) * H
        self._xform = (X, Y, m_l, m_t, W, H, lo, hi)
        p.setPen(fg)
        p.drawText(QtCore.QRectF(m_l, 2, W, m_t - 4),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self.title)
        for t in (0, 20, 40, 60, 80, 100):
            p.setPen(QtGui.QPen(grid, 1))
            p.drawLine(QtCore.QPointF(X(t), m_t), QtCore.QPointF(X(t), m_t + H))
            p.setPen(fg)
            p.drawText(QtCore.QRectF(X(t) - 20, m_t + H + 4, 40, 14),
                       QtCore.Qt.AlignCenter, "%d %%" % t)
        for v in _nice_ticks(lo, hi):
            p.setPen(QtGui.QPen(grid, 1))
            p.drawLine(QtCore.QPointF(m_l, Y(v)), QtCore.QPointF(m_l + W, Y(v)))
            p.setPen(fg)
            p.drawText(QtCore.QRectF(0, Y(v) - 8, m_l - 6, 16),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, "%.0f" % v)
        p.save()
        p.translate(12, m_t + H / 2)
        p.rotate(-90)
        p.drawText(QtCore.QRectF(-H / 2, -10, H, 14), QtCore.Qt.AlignCenter, self.unit)
        p.restore()
        p.drawText(QtCore.QRectF(m_l, m_t + H + 18, W, 14), QtCore.Qt.AlignCenter,
                   "pedal")

        def poly(pts):
            return QtGui.QPolygonF([QtCore.QPointF(X(a), Y(b)) for a, b in pts])
        for s in self.series:
            for ref in s.get("refs") or ():
                c3 = QtGui.QColor(fg)
                c3.setAlpha(70)
                p.setPen(QtGui.QPen(c3, 1.0, QtCore.Qt.DotLine))
                p.drawPolyline(poly(ref))
        for s in self.series:
            hl = self.highlight == s["key"]
            col = QtGui.QColor(s["color"])
            ghost = QtGui.QColor(col)
            ghost.setAlpha(90 if hl else 60)
            if s.get("pair_after"):
                pen = QtGui.QPen(QtGui.QColor(col), 1.2, QtCore.Qt.DashLine)
                c2 = QtGui.QColor(col)
                c2.setAlpha(150)
                pen.setColor(c2)
                p.setPen(pen)
                p.drawPolyline(poly(s["pair_after"]))
            if s.get("changed"):
                p.setPen(QtGui.QPen(ghost, 1.2))
                p.drawPolyline(poly(s["before"]))
            p.setPen(QtGui.QPen(col, 3.0 if hl else 1.8))
            p.drawPolyline(poly(s["after"]))
        eff = QtGui.QColor(fg)
        for lab, before, after in self.overlay.get("effective") or ():
            if before != after:
                c4 = QtGui.QColor(eff)
                c4.setAlpha(110)
                p.setPen(QtGui.QPen(c4, 2.4, QtCore.Qt.DashLine))
                p.drawPolyline(poly(before))
            p.setPen(QtGui.QPen(eff, 3.6))
            p.drawPolyline(poly(after))
            if after:
                a, b = after[-1]
                p.drawText(QtCore.QRectF(X(a) - 120, Y(b) - 18, 116, 14),
                           QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, lab)
        for pt in self.overlay.get("points") or ():
            c5 = QtGui.QColor(pt.get("color") or fg)
            p.setPen(QtGui.QPen(c5, 1.4))
            p.setBrush(c5 if pt.get("filled", True) else QtCore.Qt.NoBrush)
            p.drawEllipse(QtCore.QPointF(X(pt["x"]), Y(pt["y"])), 3.6, 3.6)
        p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(QtGui.QPen(fg, 1))
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawRect(QtCore.QRectF(m_l, m_t, W, H))
        p.end()

    def _nearest(self, pos):
        if not self._xform or not self.series:
            return None
        X, Y = self._xform[0], self._xform[1]
        best, bd = None, 12.0
        for s in self.series:
            pts = [(X(a), Y(b)) for a, b in s["after"]]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                dx, dy = bx - ax, by - ay
                L = dx * dx + dy * dy or 1.0
                t = max(0.0, min(1.0, ((pos.x() - ax) * dx + (pos.y() - ay) * dy) / L))
                d = ((ax + t * dx - pos.x()) ** 2 + (ay + t * dy - pos.y()) ** 2) ** 0.5
                if d < bd:
                    best, bd = s, d
        return best

    def _nearest_point(self, pos):
        if not self._xform:
            return None
        X, Y = self._xform[0], self._xform[1]
        best, bd = None, 7.0
        for pt in self.overlay.get("points") or ():
            d = ((X(pt["x"]) - pos.x()) ** 2 + (Y(pt["y"]) - pos.y()) ** 2) ** 0.5
            if d < bd:
                best, bd = pt, d
        return best

    def mouseMoveEvent(self, ev):
        pt = self._nearest_point(ev.position())
        if pt is not None:
            QtWidgets.QToolTip.showText(ev.globalPosition().toPoint(), pt.get("tip", ""), self)
            return
        s = self._nearest(ev.position())
        key = s["key"] if s else None
        if key != self.highlight:
            self.highlight = key
            self.update()
        if s:
            QtWidgets.QToolTip.showText(ev.globalPosition().toPoint(), s.get("tip", key), self)
        else:
            QtWidgets.QToolTip.hideText()

    def leaveEvent(self, _ev):
        self.highlight = None
        self.update()

    def mousePressEvent(self, ev):
        s = self._nearest(ev.position())
        if s:
            self.picked.emit(s["key"])


# -------------------------------------------------------------------- dialog
class GearboxEditDialog(QtWidgets.QDialog):

    def __init__(self, ws, sset: GE.ScheduleSet, parent=None):
        super().__init__(parent or ws)
        self.ws, self.sset = ws, sset
        self.kind = sset.kind
        #: upshift references to draw / follow / check against (lock-up)
        self.has_refs = any(sc.refs for sc in sset.schedules)
        self.session = ws.primary
        self.setWindowTitle(TITLES.get(self.kind, "Schedule editor"))
        self._fit_to_screen(1440, 860)
        self.steps: List[object] = []          # added operations
        self.edit: Optional[GE.Edit] = None
        self.result = None
        self._timer = QtCore.QTimer(self, singleShot=True, interval=120)
        self._timer.timeout.connect(self._recompute)
        dark = _is_dark()
        self.colors = {s.key: _hue(i, len(sset.schedules), dark)
                       for i, s in enumerate(sset.schedules)}

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self)
        split.addWidget(self._build_navigator())
        split.addWidget(self._build_center())
        split.addWidget(self._build_editor())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([320, 760, 360])
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(split)
        default = next((n for n in sset.presets
                        if n.startswith("Coding 1: Gear") or n.startswith("Gear ")), None) \
            if self.kind == "tcc" else None
        if default:
            self.preset.setCurrentText(default)
        self._apply_preset(self.preset.currentText())
        self._recompute()
        if self.kind != "tcc":
            self._fill_blends()
        self.log_chk.setEnabled(getattr(ws, "project_ref", None) is not None
                                and _log_module(self.session) is not None)
        if not self.log_chk.isEnabled():
            self.log_chk.setToolTip("Logs are read from the project this file was opened from")

    def closeEvent(self, ev):
        # a log load still running in the background stops with the window
        if getattr(self, "replay_box", None) is not None:
            self.replay_box.shutdown()
        super().closeEvent(ev)

    def done(self, r):
        # Esc / Close end the dialog without a close event
        if getattr(self, "replay_box", None) is not None:
            self.replay_box.shutdown()
        super().done(r)

    # ---------------------------------------------------------- navigator
    def _fit_to_screen(self, w: int, h: int) -> None:
        """Open at ``w`` x ``h``, or smaller where the screen is: the
        window always fits (and can be made bigger)."""
        scr = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            w = min(w, int(g.width() * 0.92))
            h = min(h, int(g.height() * 0.9))
        self.resize(w, h)

    def showEvent(self, ev):
        super().showEvent(ev)
        # the content settled: shrink to the screen again, and keep the whole
        # window on it (the minimum is what the controls need)
        if not getattr(self, "_fitted", False):
            self._fitted = True
            scr = self.screen() or QtGui.QGuiApplication.primaryScreen()
            if scr is not None:
                g = scr.availableGeometry()
                w = min(self.width(), max(self.minimumWidth(), int(g.width() * 0.92)))
                h = min(self.height(), max(self.minimumHeight(), int(g.height() * 0.9)))
                self.resize(w, h)
                fr = self.frameGeometry()
                fr.moveCenter(g.center())
                x = max(g.left(), min(fr.left(), g.right() - fr.width() + 1))
                y = max(g.top(), min(fr.top(), g.bottom() - fr.height() + 1))
                self.move(x, y)

    def _build_navigator(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 4, 0)
        v.addWidget(QtWidgets.QLabel("<b>%s</b>" % (
            "Characteristics" if self.kind == "tcc" else "Schedules")))
        self.preset = QtWidgets.QComboBox()
        self.preset.setToolTip("Select a preset group of schedules, or a set you saved.")
        self._fill_presets()
        self.preset.activated.connect(lambda _i: self._apply_preset(self.preset.currentText()))
        v.addWidget(self.preset)
        row = QtWidgets.QHBoxLayout()
        self.range_edit = QtWidgets.QLineEdit()
        self.range_edit.setPlaceholderText("Range, e.g. 10-24, 31, 40.0")
        self.range_edit.setToolTip("Schedule numbers; a bare number means both codings, "
                                   "n.0 / n.1 one coding. Enter selects them.")
        self.range_edit.returnPressed.connect(lambda: self._select_range(False))
        add = QtWidgets.QToolButton(text="+")
        add.setToolTip("Add the range to the current selection")
        add.clicked.connect(lambda: self._select_range(True))
        row.addWidget(self.range_edit, 1)
        row.addWidget(add)
        v.addLayout(row)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self._items: Dict[str, QtWidgets.QTreeWidgetItem] = {}
        groups: Dict[str, QtWidgets.QTreeWidgetItem] = {}
        same: Dict[tuple, List[str]] = {}
        for s in self.sset.schedules:
            same.setdefault((s.bank, tuple(s.values())), []).append(s.key)
        # groups in a reading order: per coding, real groups first, then
        # "Unused" / "Gear not located" (lock-up) at the end
        order = sorted({(s.bank, s.group or "Schedules") for s in self.sset.schedules},
                       key=lambda bg: (bg[0], bg[1] in ("Unused", "Gear not located"),
                                       bg[1] if bg[1].startswith("Gear") else "",
                                       min(i for i, s in enumerate(self.sset.schedules)
                                           if (s.bank, s.group or "Schedules") == bg)))
        for bank, grp in order:
            gname = self._group_name(bank, grp)
            g = groups[gname] = QtWidgets.QTreeWidgetItem(self.tree, [gname])
            g.setFlags(g.flags() | QtCore.Qt.ItemIsAutoTristate | QtCore.Qt.ItemIsUserCheckable)
            g.setCheckState(0, QtCore.Qt.Unchecked)
            g.setExpanded(bank == 0 and grp not in ("Unused", "Gear not located"))
        for i, s in enumerate(self.sset.schedules):
            gname = self._group_name(s.bank, s.group or "Schedules")
            g = groups.get(gname)
            if g is None:
                g = groups[gname] = QtWidgets.QTreeWidgetItem(self.tree, [gname])
                g.setFlags(g.flags() | QtCore.Qt.ItemIsAutoTristate | QtCore.Qt.ItemIsUserCheckable)
                g.setCheckState(0, QtCore.Qt.Unchecked)
                g.setExpanded(s.bank == 0)
            text = s.key + ("  ·  " + s.role if s.role else "")
            it = QtWidgets.QTreeWidgetItem(g, [text])
            it.setData(0, QtCore.Qt.UserRole, i)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(0, QtCore.Qt.Unchecked)
            pm = QtGui.QPixmap(10, 10)
            pm.fill(self.colors[s.key])
            it.setIcon(0, QtGui.QIcon(pm))
            tip = [s.key]
            if s.role:
                tip.append("Selected by: " + s.role)
            if len(s.states) > 1:
                tip.append("Also used by states %s -- an edit reaches all of them"
                           % ", ".join(map(str, s.states)))
            twins = [k for k in same[(s.bank, tuple(s.values()))] if k != s.key]
            if twins:
                tip.append("Identical to " + ", ".join(twins))
                it.setText(0, text + "  ≡")
            it.setToolTip(0, "\n".join(tip))
            self._items[s.key] = it
        self.tree.itemChanged.connect(lambda *_: self._schedule())
        v.addWidget(self.tree, 1)
        row = QtWidgets.QHBoxLayout()
        for text, fn in (("All", lambda: self._check_all(True)),
                         ("None", lambda: self._check_all(False)),
                         ("Save set…", self._save_set)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        self.count_lbl = QtWidgets.QLabel()
        v.addWidget(self.count_lbl)
        if self.sset.notes:
            nl = QtWidgets.QLabel("\n".join(self.sset.notes))
            nl.setWordWrap(True)
            nl.setStyleSheet("color: palette(mid);")
            v.addWidget(nl)
        return w

    def _group_name(self, bank: int, group: str) -> str:
        """Tree heading: the coding only when the image has more than one."""
        if len({sc.bank for sc in self.sset.schedules}) > 1:
            return "Coding %d — %s" % (bank + 1, group)
        return group

    def _saved_sets(self) -> Dict[str, List[str]]:
        bs = self._binary_set()
        if bs is not None:
            return dict(bs.extra.get(_SETS_KEY) or {})
        return dict(getattr(self.session, "_schedule_sets", {}) or {})

    def _binary_set(self):
        ref = getattr(self.ws, "project_ref", None)
        if ref is None:
            return None
        try:
            return ref.doc.project.set(ref.sid)
        except Exception:
            return None

    def _fill_presets(self):
        self.preset.clear()
        for name in self.sset.presets:
            self.preset.addItem(name)
        saved = self._saved_sets()
        if saved:
            self.preset.insertSeparator(self.preset.count())
            for name in sorted(saved):
                self.preset.addItem("Saved: " + name)

    def _set_checked(self, keys, add=False):
        self.tree.blockSignals(True)
        want = set(keys)
        for k, it in self._items.items():
            if k in want:
                it.setCheckState(0, QtCore.Qt.Checked)
            elif not add:
                it.setCheckState(0, QtCore.Qt.Unchecked)
        self.tree.blockSignals(False)
        self._schedule()

    def _apply_preset(self, name):
        if name.startswith("Saved: "):
            keys = self._saved_sets().get(name[7:], [])
        else:
            keys = [self.sset.schedules[i].key for i in self.sset.presets.get(name, [])]
        self._set_checked(keys)

    def _select_range(self, add):
        try:
            idx = GE.parse_range(self.range_edit.text(), self.sset)
        except ValueError as ex:
            QtWidgets.QToolTip.showText(self.range_edit.mapToGlobal(QtCore.QPoint(0, 20)),
                                        str(ex), self.range_edit)
            return
        self._set_checked([self.sset.schedules[i].key for i in idx], add)

    def _check_all(self, on):
        self._set_checked(list(self._items) if on else [])

    def _save_set(self):
        keys = [s.key for s in self._selected_schedules()]
        if not keys:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Save schedule set",
                                                  "Name for these %d schedules:" % len(keys))
        if not ok or not name.strip():
            return
        name = name.strip()
        bs = self._binary_set()
        if bs is not None:
            sets = dict(bs.extra.get(_SETS_KEY) or {})
            sets[name] = keys
            bs.extra[_SETS_KEY] = sets
            try:
                ref = self.ws.project_ref
                ref.doc.project._touch()
                from . import gui_project as _gp
                _gp.autosave_soon(self.ws.win, ref.doc)
            except Exception:
                pass
        else:
            store = dict(getattr(self.session, "_schedule_sets", {}) or {})
            store[name] = keys
            self.session._schedule_sets = store
        self._fill_presets()
        self.preset.setCurrentText("Saved: " + name)

    def _selected(self) -> List[int]:
        return [it.data(0, QtCore.Qt.UserRole) for it in self._items.values()
                if it.checkState(0) == QtCore.Qt.Checked]

    def _selected_schedules(self):
        return [self.sset.schedules[i] for i in self._selected()]

    # ------------------------------------------------------------- centre
    def _build_center(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(4, 0, 4, 0)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Line:"), 0, QtCore.Qt.AlignTop)
        lines_w = QtWidgets.QWidget()
        sp = lines_w.sizePolicy()
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        lines_w.setSizePolicy(sp)
        lines = _Flow(lines_w)
        self.line_group = QtWidgets.QButtonGroup(self)
        self.line_group.setExclusive(True)
        for ln in self.sset.lines:
            b = QtWidgets.QToolButton(text=ln.label)
            b.setCheckable(True)
            if self.kind == "tcc":
                from . import lockup as _lk
                b.setText("%s  %s" % (ln.label, _lk.THRESHOLD_TEXT.get(ln.label, "")))
                b.setToolTip(_lk.THRESHOLD_NOTE.get(ln.label, ""))
            else:
                b.setToolTip("%s shift (%s gear → %s gear)" % (
                    "Upshift" if ln.kind == "up" else "Downshift", ln.gear_left, ln.gear_entered))
            self.line_group.addButton(b, ln.index)
            lines.addWidget(b)
        first = self.sset.lines[0].index
        self.line_group.button(first).setChecked(True)
        self.line_group.idClicked.connect(self._line_changed)
        bar.addWidget(lines_w, 1)
        tcc = self.kind == "tcc"
        # what is drawn with the line, and in which units: a row of its own,
        # so the line buttons have the whole width to wrap in
        opts = QtWidgets.QHBoxLayout()
        self.pair_chk = QtWidgets.QCheckBox("Paired threshold" if tcc else "Opposite shift")
        self.pair_chk.setChecked(True)
        self.pair_chk.setToolTip(
            "Draw RO with OR (the release/apply pair), dashed" if tcc else
            "Draw the opposite shift between the same two gears, dashed, "
            "so the gap between them is visible")
        self.pair_chk.toggled.connect(lambda *_: self._redraw())
        opts.addWidget(self.pair_chk)
        self.refs_chk = QtWidgets.QCheckBox("Upshifts")
        self.refs_chk.setVisible(tcc and self.has_refs)
        self.refs_chk.setChecked(tcc and self.has_refs)
        self.refs_chk.setToolTip("Draw, dotted, the upshift out of the characteristic's gear in "
                                 "each program that uses it: full lock (RC) should complete "
                                 "below these")
        self.refs_chk.toggled.connect(lambda *_: self._redraw())
        opts.addWidget(self.refs_chk)
        # second row: what is drawn over the schedules
        bar2 = QtWidgets.QHBoxLayout()
        self.eff_chk = QtWidgets.QCheckBox("Effective line")
        self.eff_chk.setToolTip(
            "Draw, bold, the line the gearbox drives on in a mode: its corner schedules "
            "blended with the weights in the blend table (Blends\u2026). Before the edit "
            "dashed, after solid. No edit depends on the weights.")
        self.blend_box = QtWidgets.QComboBox()
        self.blend_box.setMinimumContentsLength(14)
        self.blend_btn = QtWidgets.QPushButton("Blends\u2026")
        self.blend_btn.setToolTip("Edit the blend table: corners, weights, programs, "
                                  "source -- or fit the weights from the project's logs")
        self.blend_btn.clicked.connect(self._edit_blends)
        self.eff_chk.toggled.connect(lambda *_: self._redraw())
        self.blend_box.currentIndexChanged.connect(lambda *_: self._redraw())
        self.log_chk = QtWidgets.QCheckBox("Logged lock-ups" if tcc else "Logged shifts")
        self.log_chk.setToolTip(
            "Plot the events in the logs kept with this file's project, on the line "
            "they belong to: " + (
                "where engine and turbine speed came together to within one 32 rpm step "
                "(filled) and parted again (hollow), in the gears of the ticked "
                "characteristics. The converter status is not in these logs, so 'together' "
                "is full lock (RC) or regulation at very low slip (after OR); a release "
                "is RC minus the hysteresis, or RO" if tcc else
                "where each shift started (the last sample still in the old ratio), "
                "filled when the speed crossed the line, hollow when the pedal moved it "
                "(those only bound the line)") +
            ". Each event is read against the version flashed when it was logged.")
        self.log_chk.toggled.connect(self._logs_toggled)
        for wdg in (self.eff_chk, self.blend_box, self.blend_btn):
            wdg.setVisible(not tcc)
            bar2.addWidget(wdg)
        # the logs toggle goes where there is room: beside the opposite-shift
        # box on the shift editor, on the (otherwise empty) row below on the
        # lock-up editor, whose row above holds two boxes already
        logs_row = bar2 if tcc else opts
        if not tcc:
            bar2.addStretch(1)
        logs_row.addWidget(self.log_chk)
        self.log_lbl = QtWidgets.QLabel()
        self.log_lbl.setStyleSheet("color: palette(mid);")
        sp = self.log_lbl.sizePolicy()      # its text never widens the window
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Ignored)
        self.log_lbl.setSizePolicy(sp)
        logs_row.addWidget(self.log_lbl, 1)
        if tcc:
            opts.addStretch(1)
        self._bar2 = bar2
        self.unit_box = QtWidgets.QComboBox()
        for key, text in UNITS:
            self.unit_box.addItem(text, key)
        self.unit_box.currentIndexChanged.connect(lambda *_: (self._unit_changed(), self._redraw()))
        opts.addWidget(self.unit_box)
        v.addLayout(bar)
        v.addLayout(opts)
        v.addLayout(self._bar2)
        vs = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.chart = ScheduleChart()
        self.chart.picked.connect(self._pick)
        vs.addWidget(self.chart)
        self.table = QtWidgets.QTableWidget()
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(22)
        vs.addWidget(self.table)
        vs.setSizes([520, 300])
        v.addWidget(vs, 1)
        return w

    def _line(self) -> int:
        return self.line_group.checkedId()

    def _unit(self) -> str:
        return self.unit_box.currentData()

    def _vehicle(self):
        veh = getattr(getattr(self.ws, "model", None), "vehicle", None)
        try:
            return veh or _sv.vehicle_of(self.session)
        except Exception:
            return None

    def _factor(self, s, line) -> float:
        u = self._unit()
        if u == "output":
            return 1.0
        if u == "engine":
            return GE.ratio_of(self.sset, s, line)
        try:
            return _sv.z_factor(s.entity, s.col_offset + line, "kmh", self._vehicle())
        except Exception:
            return 1.0

    def _line_changed(self, _id):
        # The step's lines follow the chart until the user ticks them by hand.
        if self._lines_follow:
            for ln, chk in self.op_lines.items():
                chk.blockSignals(True)
                chk.setChecked(ln == self._line())
                chk.blockSignals(False)
        self._schedule()

    def _pick(self, key):
        it = self._items.get(key)
        if it is not None:
            self.tree.scrollToItem(it)
            self.tree.setCurrentItem(it)

    # ------------------------------------------------------------- editor
    def _build_editor(self):
        outer = QtWidgets.QScrollArea()
        outer.setWidgetResizable(True)
        outer.setFrameShape(QtWidgets.QFrame.NoFrame)
        w = QtWidgets.QWidget()
        outer.setWidget(w)
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(4, 0, 0, 0)

        box = QtWidgets.QGroupBox("Step")
        f = QtWidgets.QFormLayout(box)
        self.op_box = QtWidgets.QComboBox()
        for key, text in OPS:
            if OPS_ONLY.get(key, self.kind) != self.kind:
                continue
            if key == "track" and not self.has_refs:
                continue
            self.op_box.addItem(text, key)
        self.op_box.currentIndexChanged.connect(self._op_changed)
        f.addRow("Operation", self.op_box)
        self.amount = QtWidgets.QDoubleSpinBox(decimals=1, minimum=-5000, maximum=5000)
        self.amount.setSingleStep(25)
        self.amount_lbl = QtWidgets.QLabel("Amount")
        f.addRow(self.amount_lbl, self.amount)
        self.amount2 = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=20000)
        self.amount2_lbl = QtWidgets.QLabel("To")
        f.addRow(self.amount2_lbl, self.amount2)
        self.blend = QtWidgets.QSpinBox(minimum=1, maximum=100, value=100, suffix=" %")
        self.blend_lbl = QtWidgets.QLabel("How far")
        self.blend.setToolTip("How far toward the target each cell moves")
        f.addRow(self.blend_lbl, self.blend)
        self.source = QtWidgets.QComboBox()
        for s in self.sset.schedules:
            self.source.addItem(s.key)
        self.source_lbl = QtWidgets.QLabel("From")
        f.addRow(self.source_lbl, self.source)
        self.anchor_box = QtWidgets.QComboBox()
        lo_name, hi_name = ("RO", "OR") if self.kind == "tcc" else ("downshift", "upshift")
        for key, text in (("low", "Keep the %s, move the %s" % (lo_name, hi_name)),
                          ("high", "Keep the %s, move the %s" % (hi_name, lo_name)),
                          ("mid", "Move both about the middle")):
            self.anchor_box.addItem(text, key)
        self.anchor_box.setToolTip(
            "Which of the pair stays where it is. The gap is upper minus lower: "
            + ("OR minus RO, the band slip control starts and stops in"
               if self.kind == "tcc" else "the upshift minus the downshift that undoes it"))
        self.anchor_lbl = QtWidgets.QLabel("Which")
        f.addRow(self.anchor_lbl, self.anchor_box)
        self.anchor_box.currentIndexChanged.connect(lambda *_: self._schedule())
        self.ref_box = QtWidgets.QComboBox()
        names = sorted({n for sc in self.sset.schedules for n in sc.refs},
                       key=lambda n: (len(n), n))
        self.ref_box.addItems(names)
        self.ref_box.setToolTip("Whose upshift out of the gear to follow")
        self.ref_lbl = QtWidgets.QLabel("Upshift of")
        f.addRow(self.ref_lbl, self.ref_box)
        wr = QtWidgets.QHBoxLayout()
        self.within_chk = QtWidgets.QCheckBox("only where closer than")
        self.within = QtWidgets.QSpinBox(minimum=0, maximum=3000, value=100, suffix=" rpm out")
        wr.addWidget(self.within_chk)
        wr.addWidget(self.within)
        wr.setContentsMargins(0, 0, 0, 0)
        self.within_w = QtWidgets.QWidget()
        self.within_w.setLayout(wr)
        f.addRow("", self.within_w)
        self.ref_box.currentIndexChanged.connect(lambda *_: self._schedule())
        self.within_chk.toggled.connect(lambda *_: self._schedule())
        self.within.valueChanged.connect(lambda *_: self._schedule())
        lines_w = QtWidgets.QWidget()
        lg = QtWidgets.QGridLayout(lines_w)
        lg.setContentsMargins(0, 0, 0, 0)
        lg.setSpacing(2)
        self.op_lines: Dict[int, QtWidgets.QCheckBox] = {}
        half = (len(self.sset.lines) + 1) // 2
        for n, ln in enumerate(self.sset.lines):
            c = QtWidgets.QCheckBox(ln.label)
            c.toggled.connect(self._lines_toggled)
            lg.addWidget(c, n // half, n % half)
            self.op_lines[ln.index] = c
        c0 = self.op_lines[self.sset.lines[0].index]
        c0.blockSignals(True)
        c0.setChecked(True)
        c0.blockSignals(False)
        self._lines_follow = True
        f.addRow("Lines", lines_w)
        self.with_pair = QtWidgets.QCheckBox("and each one's pair" if self.kind != "tcc"
                                             else "and each one's pair (RO \u2194 OR)")
        self.with_pair.setToolTip("Also apply the step to the paired line of every ticked "
                                  "line, so the two move together")
        self.with_pair.toggled.connect(lambda *_: self._schedule())
        f.addRow("", self.with_pair)
        self.pedal_w = QtWidgets.QWidget()
        pr = QtWidgets.QHBoxLayout(self.pedal_w)
        pr.setContentsMargins(0, 0, 0, 0)
        self.p_from = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=100.4, suffix=" %")
        self.p_to = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=100.4, value=100.4, suffix=" %")
        pr.addWidget(self.p_from)
        pr.addWidget(QtWidgets.QLabel("to"))
        pr.addWidget(self.p_to)
        f.addRow("Pedal", self.pedal_w)
        self.fade_w = QtWidgets.QWidget()
        tr = QtWidgets.QHBoxLayout(self.fade_w)
        tr.setContentsMargins(0, 0, 0, 0)
        self.taper = QtWidgets.QCheckBox("from")
        self.t_from = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=100.4, value=80, suffix=" %")
        self.t_to = QtWidgets.QDoubleSpinBox(decimals=1, minimum=0, maximum=100.4, value=100, suffix=" %")
        tr.addWidget(self.taper)
        tr.addWidget(self.t_from)
        tr.addWidget(QtWidgets.QLabel("to 0 at"))
        tr.addWidget(self.t_to)
        f.addRow("Fade", self.fade_w)
        self.taper.setToolTip("Full amount below the first pedal, fading to nothing at the second")
        self.direction = QtWidgets.QComboBox()
        for key, text in DIRECTIONS:
            self.direction.addItem(text, key)
        f.addRow("Direction", self.direction)
        # the full-load optimum's own controls (shift editor only)
        self.fl_panel = None
        self.pl_panel = None
        self.ax_panel = None
        self._fl_sel = None
        if self.kind != "tcc":
            from .gui_fullload import FullLoadPanel
            from .gui_partload import AxisPanel, PartLoadPanel
            self.fl_panel = FullLoadPanel(self)
            self.fl_panel.changed.connect(self._schedule)
            f.addRow(self.fl_panel)
            self.pl_panel = PartLoadPanel(self)
            self.pl_panel.changed.connect(self._schedule)
            f.addRow(self.pl_panel)
            self.ax_panel = AxisPanel(self)
            self.ax_panel.changed.connect(self._schedule)
            f.addRow(self.ax_panel)
        for wdg in (self.amount, self.amount2, self.p_from, self.p_to, self.t_from,
                    self.t_to, self.blend):
            wdg.valueChanged.connect(lambda *_: self._schedule())
        for wdg in (self.direction, self.source):
            wdg.currentIndexChanged.connect(lambda *_: self._schedule())
        self.taper.toggled.connect(lambda *_: self._schedule())
        rb = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QPushButton("Add step")
        self.add_btn.setToolTip("Keep this step and set up another one; the edit applies all of them")
        self.add_btn.clicked.connect(self._add_step)
        rb.addStretch(1)
        rb.addWidget(self.add_btn)
        f.addRow(rb)
        v.addWidget(box)

        sbox = QtWidgets.QGroupBox("Steps in this edit")
        sv = QtWidgets.QVBoxLayout(sbox)
        self.steps_list = QtWidgets.QListWidget()
        self.steps_list.setMaximumHeight(90)
        sv.addWidget(self.steps_list)
        rm = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Remove")
        b.clicked.connect(self._remove_step)
        rm.addStretch(1)
        rm.addWidget(b)
        sv.addLayout(rm)
        v.addWidget(sbox)

        cbox = QtWidgets.QGroupBox("Checks (advisory)")
        cf = QtWidgets.QGridLayout(cbox)
        self.gap_on = QtWidgets.QCheckBox(
            "OR above RO by ≥" if self.kind == "tcc" else "Gap to the opposite shift ≥")
        self.gap_on.setChecked(True)
        self.gap = QtWidgets.QSpinBox(minimum=0, maximum=2000, value=60, suffix=" rpm out")
        self.gap.setToolTip("Output rpm. Held to the smaller of this and the cell's own gap before the edit")
        self.gap_fix = QtWidgets.QComboBox()
        self.gap_fix.addItems(["Warn only", "Auto-fix: take back the edit",
                               "Auto-fix: move the opposite shift"])
        cf.addWidget(self.gap_on, 0, 0)
        cf.addWidget(self.gap, 0, 1)
        cf.addWidget(self.gap_fix, 1, 0, 1, 2)
        self.mono_on = QtWidgets.QCheckBox("Line never falls as pedal rises")
        self.mono_on.setChecked(self.kind != "tcc")
        self.mono_fix = QtWidgets.QComboBox()
        self.mono_fix.addItems(["Warn only", "Auto-fix: pull back the edited row",
                                "Auto-fix: running maximum"])
        cf.addWidget(self.mono_on, 2, 0, 1, 2)
        cf.addWidget(self.mono_fix, 3, 0, 1, 2)
        self.rpm_on = QtWidgets.QCheckBox("Engine rpm after the shift")
        self.rpm_lo = QtWidgets.QSpinBox(minimum=0, maximum=8000, value=1300, prefix="≥ ")
        self.rpm_hi = QtWidgets.QSpinBox(minimum=0, maximum=8000, value=4500, prefix="≤ ")
        self.lock_on = QtWidgets.QCheckBox("Full lock (RC) below the upshift by ≥")
        self.lock_on.setVisible(self.kind == "tcc" and self.has_refs)
        self.lock_on.setChecked(self.kind == "tcc" and self.has_refs)
        self.lock_m = QtWidgets.QSpinBox(minimum=0, maximum=3000, value=100, suffix=" rpm out")
        self.lock_m.setVisible(self.kind == "tcc" and self.has_refs)
        self.lock_on.setToolTip("Warn where an edited RC cell sits closer than this to the "
                                "upshift out of the gear in a program that uses it")
        cf.addWidget(self.lock_on, 6, 0)
        cf.addWidget(self.lock_m, 6, 1)
        self.same_on = QtWidgets.QCheckBox("Identical %s stay identical" % (
            "characteristics" if self.kind == "tcc" else "schedules"))
        self.same_on.setChecked(True)
        self.same_on.setToolTip(
            "Warn when the edit reaches only some of a group that was byte-identical "
            "(the factory keeps such groups on purpose, e.g. lock-up slots 2/6, 3/7, 4/8)")
        self.same_fix = QtWidgets.QComboBox()
        self.same_fix.addItems(["Warn only", "Auto-fix: extend the edit to the rest"])
        cf.addWidget(self.same_on, 7, 0, 1, 2)
        cf.addWidget(self.same_fix, 8, 0, 1, 2)
        self.step_on = QtWidgets.QCheckBox("No step steeper than 1.5 \u00d7 the factory's")
        self.step_on.setToolTip(
            "Warn where the edit makes a line rise between two pedal rows more steeply "
            "than 1.5 times its own steepest factory rise (the kickdown step aside)")
        self.step_on.setChecked(self.kind != "tcc")
        self.step_on.setVisible(self.kind != "tcc")
        cf.addWidget(self.step_on, 9, 0, 1, 2)
        self.step_on.toggled.connect(lambda *_: self._schedule())
        self.conv_on = QtWidgets.QCheckBox("Upshifts land with the converter locked")
        self.conv_on.setToolTip(
            "Warn where the edit puts an upshift below the lock-up map's OR for the next "
            "gear in that program: the converter is still open after the shift, which costs "
            "what an early (economy) shift was for")
        self.conv_on.setChecked(self.kind != "tcc")
        self.conv_on.setVisible(self.kind != "tcc")
        cf.addWidget(self.conv_on, 10, 0, 1, 2)
        self.conv_on.toggled.connect(lambda *_: self._schedule())
        self.same_on.toggled.connect(lambda *_: self._schedule())
        self.same_fix.currentIndexChanged.connect(lambda *_: self._schedule())
        self.lock_on.toggled.connect(lambda *_: self._schedule())
        self.lock_m.valueChanged.connect(lambda *_: self._schedule())
        cf.addWidget(self.rpm_on, 4, 0, 1, 2)
        rr = QtWidgets.QHBoxLayout()
        rr.addWidget(self.rpm_lo)
        rr.addWidget(self.rpm_hi)
        cf.addLayout(rr, 5, 0, 1, 2)
        for wdg in (self.gap_on, self.mono_on, self.rpm_on):
            wdg.toggled.connect(lambda *_: self._schedule())
        for wdg in (self.gap, self.rpm_lo, self.rpm_hi):
            wdg.valueChanged.connect(lambda *_: self._schedule())
        for wdg in (self.gap_fix, self.mono_fix):
            wdg.currentIndexChanged.connect(lambda *_: self._schedule())
        v.addWidget(cbox)

        self.replay_box = None
        if self.kind != "tcc":
            from .gui_partload import ReplayBox
            self.replay_box = ReplayBox(self)

        fbox = QtWidgets.QGroupBox("Preview")
        fv = QtWidgets.QVBoxLayout(fbox)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        fv.addWidget(self.summary)
        self.findings = QtWidgets.QListWidget()
        self.findings.setWordWrap(True)
        fv.addWidget(self.findings, 1)
        v.addWidget(fbox, 1)
        if self.replay_box is not None:
            v.addWidget(self.replay_box)

        ab = QtWidgets.QHBoxLayout()
        self.version_chk = QtWidgets.QCheckBox("Save as a project version")
        self.version_chk.setChecked(getattr(self.ws, "project_ref", None) is not None)
        self.version_chk.setEnabled(getattr(self.ws, "project_ref", None) is not None)
        ab.addWidget(self.version_chk)
        ab.addStretch(1)
        self.apply_btn = QtWidgets.QPushButton("Apply…")
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self._apply)
        ab.addWidget(self.apply_btn)
        v.addLayout(ab)
        # wide enough for every operation's controls (all still shown here),
        # so nothing is clipped and there is no sideways scrolling
        need = w.minimumSizeHint().width()
        self._op_changed()
        outer.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        outer.setMinimumWidth(need + outer.verticalScrollBar().sizeHint().width() + 6)
        return outer

    def _lines_toggled(self, *_):
        self._lines_follow = False
        self._schedule()

    def _unit_suffix(self) -> str:
        return {"engine": "rpm", "output": "rpm out", "kmh": "km/h"}[self._unit()]

    def _unit_changed(self):
        if self.op_box.currentData() in ("offset", "band", "set", "gap"):
            for sb in (self.amount, self.amount2):
                sb.setSuffix(" " + self._unit_suffix())
        self._schedule()

    def _op_changed(self, *_):
        op = self.op_box.currentData()
        show = {"amount": op in ("offset", "scale", "slide", "band", "set", "smooth", "track",
                                 "gap"),
                "amount2": op == "band", "blend": op in ("band", "set", "copy"),
                "source": op == "copy", "ref": op == "track", "anchor": op == "gap"}
        fl = op == "fullload"
        own = op in ("fullload", "partload", "pedalaxis")     # steps with their own rows
        f = self.pedal_w.parentWidget().layout() if self.pedal_w.parentWidget() else None
        for wdg in (self.pedal_w, self.fade_w, self.with_pair):
            wdg.setVisible(not own)
            lab = f.labelForField(wdg) if isinstance(f, QtWidgets.QFormLayout) else None
            if lab is not None:
                lab.setVisible(not own)
        for panel, key in ((self.pl_panel, "partload"), (self.ax_panel, "pedalaxis")):
            if panel is not None:
                panel.setVisible(op == key)
                if op == key:
                    if key == "partload":
                        for ln, c in self.op_lines.items():
                            c.blockSignals(True)
                            c.setChecked(self.sset.line(ln).kind == "up")
                            c.blockSignals(False)
                        self._lines_follow = False
                    panel.activate()
        if self.fl_panel is not None:
            self.fl_panel.setVisible(fl)
            if fl:
                # the step reaches every upshift unless the user narrows it
                for ln, c in self.op_lines.items():
                    c.blockSignals(True)
                    c.setChecked(self.sset.line(ln).kind == "up")
                    c.blockSignals(False)
                self._lines_follow = False
                self.fl_panel.activate()
        for key, wdgs in (("amount", (self.amount, self.amount_lbl)),
                          ("amount2", (self.amount2, self.amount2_lbl)),
                          ("blend", (self.blend, self.blend_lbl)),
                          ("source", (self.source, self.source_lbl)),
                          ("ref", (self.ref_box, self.ref_lbl, self.within_w)),
                          ("anchor", (self.anchor_box, self.anchor_lbl))):
            for wdg in wdgs:
                wdg.setVisible(show[key])
        unit = self._unit_suffix()
        self.amount.blockSignals(True)
        if op == "offset":
            self.amount_lbl.setText("Add")
            self.amount.setRange(-5000, 5000)
            self.amount.setSuffix(" " + unit)
            self.amount.setValue(0)
        elif op == "scale":
            self.amount_lbl.setText("Change")
            self.amount.setRange(-90, 300)
            self.amount.setSuffix(" %")
            self.amount.setValue(0)
        elif op == "slide":
            self.amount_lbl.setText("Move by")
            self.amount.setRange(-100, 100)
            self.amount.setSuffix(" pedal %")
            self.amount.setValue(0)
            self.amount.setToolTip("Positive: the line's shape moves to LOWER pedal "
                                   "(a downshift then needs less pedal)")
        elif op == "band":
            self.amount_lbl.setText("From")
            self.amount.setRange(0, 20000)
            self.amount.setSuffix(" " + unit)
            self.amount2.setSuffix(" " + unit)
        elif op == "set":
            self.amount_lbl.setText("Value")
            self.amount.setRange(0, 20000)
            self.amount.setSuffix(" " + unit)
        elif op == "track":
            self.amount_lbl.setText("At")
            self.amount.setRange(1, 200)
            self.amount.setSuffix(" % of it")
            self.amount.setValue(85)
        elif op == "gap":
            self.amount_lbl.setText("Gap")
            self.amount.setRange(0, 5000)
            self.amount.setSuffix(" " + unit)
            self.amount.setValue(150 if self.kind == "tcc" else 100)
        elif op == "off":
            # "never" means every threshold of the characteristic, unless the
            # user narrows it
            if self.kind == "tcc":
                for c in self.op_lines.values():
                    c.blockSignals(True)
                    c.setChecked(True)
                    c.blockSignals(False)
        elif op == "smooth":
            self.amount_lbl.setText("Strength")
            self.amount.setRange(0, 100)
            self.amount.setSuffix(" %")
            self.amount.setValue(50)
        self.amount.blockSignals(False)
        self._schedule()

    # ---------------------------------------------------------- the edit
    def _scope(self) -> GE.Scope:
        lines = [ln for ln, c in self.op_lines.items() if c.isChecked()]
        if self.with_pair.isChecked():
            for ln in list(lines):
                pr = self.sset.line(ln).pair
                if pr is not None and pr not in lines:
                    lines.append(pr)
        ts = self.taper.isChecked()
        return GE.Scope(self._selected(), lines, pedal_min=self.p_from.value(),
                        pedal_max=self.p_to.value(),
                        taper_start=self.t_from.value() if ts else None,
                        taper_end=self.t_to.value() if ts else None,
                        direction=self.direction.currentData())

    def _unit_args(self, value):
        """``(value, unit)`` for an op: km/h becomes output rpm."""
        u = self._unit()
        if u == "kmh":
            sel = self._selected_schedules()
            if not sel:
                return value, "output"
            f = self._factor(sel[0], self._line()) or 1.0
            return value / f, "output"
        return value, u

    def _current_op(self):
        op = self.op_box.currentData()
        sc = self._scope()
        if not sc.schedules or not sc.lines:
            return None
        a = self.amount.value()
        if op == "offset":
            if a == 0:
                return None
            v, u = self._unit_args(a)
            return GE.Offset(sc, v, u)
        if op == "scale":
            return GE.Scale(sc, 1 + a / 100.0) if a else None
        if op == "slide":
            return GE.SlidePedal(sc, a) if a else None
        if op == "band":
            lo, u = self._unit_args(a)
            hi, _u = self._unit_args(self.amount2.value())
            return GE.BandRemap(sc, lo, hi, u, blend=self.blend.value() / 100.0) if hi > lo else None
        if op == "set":
            v, u = self._unit_args(a)
            return GE.SetCurve(sc, [(0, v), (100.4, v)], u, blend=self.blend.value() / 100.0)
        if op == "copy":
            src = self.sset.index(self.source.currentText())
            return GE.CopyFrom(sc, source=src, fraction=self.blend.value() / 100.0)
        if op == "smooth":
            return GE.Smooth(sc, a / 100.0) if a else None
        if op == "gap":
            v, u = self._unit_args(a)
            return GE.SetGap(sc, v, u, self.anchor_box.currentData())
        if op == "off":
            return GE.SetOff(sc)
        if op == "fullload" and self.fl_panel is not None:
            return self.fl_panel.make_op(sc)
        if op == "partload" and self.pl_panel is not None:
            return self.pl_panel.make_op(sc)
        if op == "pedalaxis" and self.ax_panel is not None:
            return self.ax_panel.make_op(sc)
        if op == "track":
            ref = self.ref_box.currentText()
            if not ref:
                return None
            return GE.TrackReference(sc, ref, factor=a / 100.0,
                                     within=float(self.within.value())
                                     if self.within_chk.isChecked() else None)
        return None

    def _checks(self):
        out = []
        if self.gap_on.isChecked():
            fx = self.gap_fix.currentIndex()
            out.append(GE.GapCheck(float(self.gap.value()), autofix=fx > 0,
                                   fix="push_partner" if fx == 2 else "take_back",
                                   tolerance=1.0))
        if self.mono_on.isChecked():
            fx = self.mono_fix.currentIndex()
            out.append(GE.MonotonicCheck(autofix=fx > 0, mode="raise" if fx == 2 else "pull_back"))
        if self.rpm_on.isChecked():
            out.append(GE.RpmLimitCheck(floor=self.rpm_lo.value() or None,
                                        ceiling=self.rpm_hi.value() or None))
        if self.kind == "tcc" and self.lock_on.isChecked() and len(self.sset.lines) > 2:
            out.append(GE.ReferenceGapCheck(line=2, margin=float(self.lock_m.value())))
        if self.same_on.isChecked():
            out.append(GE.IdenticalCheck(autofix=self.same_fix.currentIndex() == 1))
        if self.kind != "tcc" and self.step_on.isChecked():
            out.append(GE.StepCheck(1.5))
        if self.kind != "tcc" and self.conv_on.isChecked():
            if getattr(self, "_tcc_set", False) is False:
                try:
                    self._tcc_set = schedule_set_for(self.session, "tcc")
                except Exception:
                    self._tcc_set = None
            if self._tcc_set is not None:
                from .gearbox_edit.partload import ConverterCheck
                out.append(ConverterCheck(self._tcc_set))
        out.append(GE.SharedCheck())
        return out

    def _ops(self):
        cur = self._current_op()
        return list(self.steps) + ([cur] if cur is not None else [])

    def _add_step(self):
        cur = self._current_op()
        if cur is None:
            return
        self.steps.append(cur)
        self.steps_list.addItem(self._describe(cur))
        self.op_box.setCurrentIndex(self.op_box.currentIndex())
        self.amount.setValue(0 if self.op_box.currentData() != "smooth" else 50)
        self._schedule()

    def _describe(self, op) -> str:
        lines = ", ".join(self.sset.line(c).label for c in op.scope.lines)
        return "%s on %s, %d schedules, pedal %.0f–%.0f %%" % (
            op.describe(), lines, len(op.scope.schedules), op.scope.pedal_min, op.scope.pedal_max)

    def _remove_step(self):
        r = self.steps_list.currentRow()
        if 0 <= r < len(self.steps):
            del self.steps[r]
            self.steps_list.takeItem(r)
            self._schedule()

    def _schedule(self):
        self._timer.start()

    def _reread(self):
        """The binary changed since the schedules were read (an undo, a
        redo, an edit elsewhere): read their pedal axes again and drop the
        lock-up sets kept, so the steps pick the rows the binary has."""
        rev = getattr(self.session, "revision", None)
        if rev == getattr(self, "_seen_rev", rev):
            self._seen_rev = rev
            return
        self._seen_rev = rev
        for sc in self.sset.schedules:
            try:
                sc.pedal = tuple(float(v) for v in sc.entity.y_values())
            except Exception:
                pass
        self._tcc_set = False
        for panel in (self.fl_panel, getattr(self, "ax_panel", None)):
            if panel is not None:
                panel._tcc = False

    def _recompute(self):
        self._reread()
        if self.fl_panel is not None and self.op_box.currentData() in ("fullload", "partload"):
            sel = tuple(self._selected())
            if sel != self._fl_sel:
                self._fl_sel = sel
                self.fl_panel.selection_changed()   # the open gears follow the programs
        self.edit = GE.Edit(self.sset, self._ops(), self._checks())
        self.result = self.edit.compute()
        n = len(self._selected())
        self.count_lbl.setText("%d of %d schedules selected" % (n, len(self.sset.schedules)))
        self.summary.setText(self.result.summary(self.sset))
        self.findings.clear()
        fixed: Dict[str, int] = {}
        open_ = [f for f in self.result.findings if not f.fixed]
        for fnd in self.result.findings:
            if fnd.fixed:
                fixed[fnd.code] = fixed.get(fnd.code, 0) + 1
        names = {"gap": "gap to the opposite shift", "monotonic": "line falling with pedal",
                 "rpm": "engine rpm limit", "step": "steep step between pedal rows",
                 "converter": "upshift into an open converter"}
        for code, n in fixed.items():
            it = QtWidgets.QListWidgetItem("✓ %d auto-fixed: %s" % (n, names.get(code, code)))
            it.setForeground(QtGui.QColor("#3a8a3a"))
            self.findings.addItem(it)
        for fnd in open_[:300]:
            it = QtWidgets.QListWidgetItem(("⚠ " if fnd.severity == "warning" else "ℹ ")
                                           + fnd.message)
            if fnd.severity == "warning":
                it.setForeground(QtGui.QColor("#d08a00"))
            self.findings.addItem(it)
        if not self.result.findings and self.result.changes:
            self.findings.addItem("No check findings.")
        self.apply_btn.setEnabled(bool(self.result.changes or self.result.axes))
        self._redraw()
        if getattr(self, "replay_box", None) is not None:
            self.replay_box.edit_changed()

    def _redraw(self):
        if self.result is None:
            return
        line = self._line()
        ln = self.sset.line(line)
        pair = ln.pair if self.pair_chk.isChecked() else None
        series = []
        changed = {(i, c) for (i, r, c) in (ch.key for ch in self.result.changes)}
        for i in self._selected():
            s = self.sset.schedules[i]
            f = self._factor(s, line)
            K = s.cols
            # a stored "off" (never shift / disabled, 10000) is not a speed:
            # it is left out of the line rather than drawn at 10000
            before = [(p, self.edit.origin[i][r * K + line] * f) for r, p in enumerate(s.pedal)
                      if not s.is_off(self.edit.origin[i][r * K + line])]
            ped_after = self.result.pedal_of(self.sset, i)
            after = [(p, self.result.grid[i][r * K + line] * f) for r, p in enumerate(ped_after)
                     if not s.is_off(self.result.grid[i][r * K + line])]
            d = {"key": s.key, "color": self.colors[s.key], "before": before, "after": after,
                 "changed": (i, line) in changed or id(s.entity) in (self.result.axes or {}),
                 "tip": "%s  %s\n%s" % (s.key, s.role, s.group)}
            if pair is not None:
                fp = self._factor(s, pair)
                d["pair_after"] = [(p, self.result.grid[i][r * K + pair] * fp)
                                   for r, p in enumerate(ped_after)
                                   if not s.is_off(self.result.grid[i][r * K + pair])]
            if self.kind == "tcc" and self.refs_chk.isChecked():
                users = ["SKL %d" % n for n in s.states]
                d["refs"] = [list(zip(s.refs[n][0], [v * f for v in s.refs[n][1]]))
                             for n in users if n in s.refs]
            series.append(d)
        unit = dict(UNITS)[self._unit()]
        title = "%s  —  %s" % (ln.label, unit)
        if pair is not None:
            title += "   (dashed: %s)" % self.sset.line(pair).label
        if self.kind == "tcc" and self.refs_chk.isChecked():
            title += "   (dotted: upshift out of the gear)"
        overlay = {}
        eff = self._effective(line)
        if eff:
            overlay["effective"] = [eff]
            title += "   (bold: effective %s)" % eff[0]
        pts = self._event_points(line)
        if pts:
            overlay["points"] = pts
        self.chart.set_series(series, unit, title, overlay)
        self._fill_table(line)

    # ------------------------------------------------- blends and logs
    def _blends(self) -> List[GE.Blend]:
        bs = self._binary_set()
        raw = bs.extra.get(_BLENDS_KEY) if bs is not None else getattr(
            self.session, "_gearbox_blends", None)
        if raw:
            out = []
            for d in raw:
                try:
                    out.append(GE.Blend.from_dict(d))
                except Exception:
                    continue
            return out
        mod = _log_module(self.session)
        if mod is not None and hasattr(mod, "default_blends"):
            try:
                return mod.default_blends(self.session, self.sset)
            except Exception:
                return []
        return []

    def _store_blends(self, blends: List[GE.Blend]):
        data = [b.to_dict() for b in blends]
        bs = self._binary_set()
        if bs is not None:
            bs.extra[_BLENDS_KEY] = data
            try:
                ref = self.ws.project_ref
                ref.doc.project._touch()
                from . import gui_project as _gp
                _gp.autosave_soon(self.ws.win, ref.doc)
            except Exception:
                pass
        else:
            self.session._gearbox_blends = data
        self._fill_blends()

    def _fill_blends(self):
        cur = self.blend_box.currentText()
        self.blend_box.blockSignals(True)
        self.blend_box.clear()
        for b in self._blends():
            if b.indices(self.sset):
                self.blend_box.addItem(b.name)
        if cur and self.blend_box.findText(cur) >= 0:
            self.blend_box.setCurrentText(cur)
        self.blend_box.blockSignals(False)
        has = self.blend_box.count() > 0
        self.eff_chk.setEnabled(has)
        if not has:
            self.eff_chk.setToolTip("No blend has corners yet -- add them with Blends\u2026")
        self._redraw()

    def _current_blend(self) -> Optional[GE.Blend]:
        name = self.blend_box.currentText()
        return next((b for b in self._blends() if b.name == name), None) if name else None

    def _effective(self, line):
        if self.kind == "tcc" or not self.eff_chk.isChecked() or self.result is None:
            return None
        b = self._current_blend()
        corners = b.indices(self.sset) if b else []
        if not corners:
            return None
        f = self._factor(self.sset.schedules[corners[0][0]], line)
        ped = GE.blend_pedal(self.sset, corners)
        before = GE.effective_line(self.sset, self.edit.origin, corners, line, ped)
        after = GE.effective_line(self.sset, self.result.grid, corners, line, ped)
        return (b.name, [(p, v * f) for p, v in zip(ped, before)],
                [(p, v * f) for p, v in zip(ped, after)])

    def _project_logs(self):
        """The project's events for this set, read once (None: no project)."""
        if getattr(self, "_plogs", None) is not None:
            return self._plogs
        ref = getattr(self.ws, "project_ref", None)
        mod = _log_module(self.session)
        if ref is None or mod is None or not hasattr(mod, "ProjectLogs"):
            return None
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self._plogs = mod.ProjectLogs(ref.doc.project, ref.sid, self.session, self.sset)
        except Exception as ex:
            self._plogs = None
            self.log_lbl.setText("Logs could not be read: %s" % ex)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        return self._plogs

    def _logs_toggled(self, on):
        if on and self._project_logs() is None:
            self.log_chk.blockSignals(True)
            self.log_chk.setChecked(False)
            self.log_chk.blockSignals(False)
            if not self.log_lbl.text():
                self.log_lbl.setText("Logs are read from the project this file was opened "
                                     "from -- open it from a project to see them.")
            return
        if not on:
            self.log_lbl.setText("")
        self._redraw()

    def _event_points(self, line):
        if not self.log_chk.isChecked():
            return []
        pl = self._project_logs()
        if pl is None:
            return []
        sel = self._selected_schedules()
        if not sel:
            return []
        dark = _is_dark()
        pts = []
        if self.kind == "tcc":
            gears = {int(s.group.split()[-1]) for s in sel
                     if s.group.startswith("Gear ") and s.group.split()[-1].isdigit()}
            evs = [e for e in pl.locks if e.gear in gears]
            col = QtGui.QColor("#ff922b" if dark else "#c2410c")
            for e in evs:
                ratio = GE.ratio_of(self.sset, next((s for s in sel if s.group == "Gear %d"
                                                     % e.gear), sel[0]), line)
                f = self._factor_for_event(ratio, e)
                pts.append({"x": e.pedal, "y": e.out_rpm * f, "filled": e.kind == "lock",
                            "color": col, "tip": "%s at %.0f rpm out, %.1f %% pedal, gear %d\n%s"
                            % ("Slip gone (locked or tightly regulated)" if e.kind == "lock"
                               else "Slip back (released)", e.out_rpm,
                               e.pedal, e.gear, e.label())})
            what = "lock-ups and releases in gear %s" % ", ".join(map(str, sorted(gears)))
        else:
            evs = [e for e in pl.shifts if e.line == line]
            b = self._current_blend() if self.eff_chk.isChecked() else None
            if b is not None and b.programs:
                evs = [e for e in evs if e.program in b.programs
                       and (b.dsp is None or e.dsp == b.dsp)]
            col = QtGui.QColor("#ff922b" if dark else "#c2410c")
            ratio = self.sset.line(line).ratio_left or 1.0
            for e in evs:
                f = self._factor_for_event(ratio, e)
                pts.append({"x": e.pedal, "y": e.out_rpm * f, "filled": e.steady,
                            "color": col,
                            "tip": "%s %s at %.0f rpm out, %.1f %% pedal (%s)\n%s"
                            % (self.sset.line(line).label, "shift", e.out_rpm, e.pedal,
                               "speed crossed the line" if e.steady else
                               "pedal moved the line: a bound only", e.label())})
            what = "shifts" + (" in %s" % b.name if b is not None and b.programs else "")
        self.log_lbl.setText("%d logged %s%s, from %d logs" % (
            len(pts), what, "" if self.kind == "tcc" else " on this line", len(pl.logs)))
        return pts

    def _factor_for_event(self, ratio, e) -> float:
        u = self._unit()
        if u == "engine":
            return ratio or 1.0
        if u == "kmh":
            sel = self._selected_schedules()
            return self._factor(sel[0], self._line()) if sel else 1.0
        return 1.0

    def _edit_blends(self):
        dlg = BlendDialog(self, self._blends())
        if dlg.exec() == QtWidgets.QDialog.Accepted:
            self._store_blends(dlg.blends)
            if dlg.current and self.blend_box.findText(dlg.current) >= 0:
                self.blend_box.setCurrentText(dlg.current)
                self.eff_chk.setChecked(True)

    def _fill_table(self, line):
        sel = self._selected()
        t = self.table
        t.clear()
        if not sel:
            t.setRowCount(0)
            t.setColumnCount(0)
            return
        rows = max(self.sset.schedules[i].rows for i in sel)
        ref = self.sset.schedules[sel[0]]
        t.setColumnCount(rows)
        t.setRowCount(len(sel))
        axes = {tuple(self.sset.schedules[i].pedal) for i in sel}
        if len(axes) == 1:
            t.setHorizontalHeaderLabels(["%.1f %%" % p for p in ref.pedal]
                                        + [""] * (rows - len(ref.pedal)))
        else:
            # the selection has different pedal axes: a column is a ROW of
            # each schedule, not one pedal value (each cell's tooltip has it)
            t.setHorizontalHeaderLabels(["row %d" % (r + 1) for r in range(rows)])
            for r in range(rows):
                h = t.horizontalHeaderItem(r)
                if h is not None:
                    h.setToolTip("The selected schedules have different pedal axes: "
                                 "hover a cell for its pedal %")
        t.setVerticalHeaderLabels([self.sset.schedules[i].key for i in sel])
        hl = QtGui.QColor("#e8b04a")
        hl.setAlpha(90)
        for row, i in enumerate(sel):
            s = self.sset.schedules[i]
            f = self._factor(s, line)
            for r in range(s.rows):
                old = self.edit.origin[i][r * s.cols + line] * f
                new = self.result.grid[i][r * s.cols + line] * f
                text = "%.0f" % new
                it = QtWidgets.QTableWidgetItem(text)
                it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                tip = "%s at %.1f %% pedal" % (s.key, s.pedal[r])
                if abs(new - old) > 1e-9:
                    it.setBackground(hl)
                    tip += "\nwas %.0f, would be %.0f (%+.0f)" % (old, new, new - old)
                it.setToolTip(tip)
                t.setItem(row, r, it)
        t.resizeColumnsToContents()

    # ------------------------------------------------------------ apply
    def _apply(self):
        res = self.edit.compute()
        if not res.changes and not res.axes:
            return
        warn = [f for f in res.findings if f.severity == "warning" and not f.fixed]
        auto = TITLES.get(self.kind, "Editor") + ": " + "; ".join(
            self._describe(op) for op in self._ops())
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Apply edit")
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.addWidget(QtWidgets.QLabel(
            "Write %s?\nThis is one undoable step (Edit > Undo)." % res.summary(self.sset)))
        if warn:
            lay.addWidget(QtWidgets.QLabel("%d check warning(s) remain -- they do not block this."
                                           % len(warn)))
        lay.addWidget(QtWidgets.QLabel("Version label:"))
        lab = QtWidgets.QLineEdit(auto[:200])
        lay.addWidget(lab)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        label = lab.text().strip() or auto
        try:
            n = self.edit.apply(self.session, label, res)
        except ValueError as ex:
            QtWidgets.QMessageBox.warning(self, TITLES.get(self.kind, "Editor"),
                                          "Nothing was written: %s" % ex)
            return
        if res.axes:
            # the schedules' pedal axes moved: read them again (and the
            # lock-up sets, whose axes may have moved with them)
            self._seen_rev = None
            self._reread()
        try:
            self.ws.model.refresh()
            self.ws._status()
        except Exception:
            pass
        if self.version_chk.isChecked() and getattr(self.ws, "project_ref", None) is not None:
            try:
                from . import gui_project as _gp
                _gp.save(self.ws, tab_only=True, label=label)
            except Exception as ex:
                QtWidgets.QMessageBox.warning(self, TITLES.get(self.kind, "Editor"),
                                              "Applied, but the project could not be saved: %s" % ex)
        self.steps.clear()
        self.steps_list.clear()
        self.amount.setValue(0 if self.op_box.currentData() != "smooth" else 50)
        self._recompute()
        try:
            self.ws.statusBar().showMessage("%s: %d cells written. Undo puts them back."
                                            % (TITLES.get(self.kind, "Editor"), n), 8000)
        except Exception:
            pass


class BlendDialog(QtWidgets.QDialog):
    """The blend table: per mode, the corner schedules and their weights.

    The weights only draw the effective line; nothing written depends on
    them. "Fit from logs" asks the project's logs which mix of a candidate
    set explains the logged shifts, and says honestly when they can't tell.
    """

    def __init__(self, editor: "GearboxEditDialog", blends: List[GE.Blend]):
        super().__init__(editor)
        self.setWindowTitle("Blends -- effective shift lines")
        self.ed, self.sset = editor, editor.sset
        self.blends = [GE.Blend.from_dict(b.to_dict()) for b in blends]
        self.current: Optional[str] = None
        self._row = -1
        lay = QtWidgets.QHBoxLayout(self)
        left = QtWidgets.QVBoxLayout()
        self.list = QtWidgets.QListWidget()
        self.list.setMaximumWidth(160)
        left.addWidget(self.list, 1)
        lb = QtWidgets.QHBoxLayout()
        for text, fn in (("Add", self._add), ("Remove", self._remove)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            lb.addWidget(b)
        left.addLayout(lb)
        lay.addLayout(left)
        right = QtWidgets.QVBoxLayout()
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit()
        self.progs = QtWidgets.QLineEdit()
        self.progs.setPlaceholderText("e.g. 10")
        self.progs.setToolTip("The 'current shift program' value(s) the gearbox logs in "
                              "this mode -- used to pick the logged shifts for the overlay "
                              "and the fit (D 10, S 12, M 27 on the 6HP19)")
        self.dsp = QtWidgets.QLineEdit()
        self.dsp.setPlaceholderText("any")
        self.dsp.setToolTip("Only logged shifts at this dynamic shift program (blank: any)")
        form.addRow("Name", self.name)
        form.addRow("Logged program", self.progs)
        form.addRow("Dynamic program", self.dsp)
        right.addLayout(form)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Corner schedule", "Weight"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        right.addWidget(self.table, 1)
        cb = QtWidgets.QHBoxLayout()
        for text, fn in (("Add corner", self._add_corner), ("Remove corner", self._remove_corner)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            cb.addWidget(b)
        cb.addStretch(1)
        self.total = QtWidgets.QLabel()
        cb.addWidget(self.total)
        right.addLayout(cb)
        form2 = QtWidgets.QFormLayout()
        self.source = QtWidgets.QLineEdit()
        self.note = QtWidgets.QPlainTextEdit()
        self.note.setMaximumHeight(60)
        form2.addRow("Source", self.source)
        form2.addRow("Note", self.note)
        right.addLayout(form2)
        fb = QtWidgets.QHBoxLayout()
        self.cands = QtWidgets.QComboBox()
        self.cands.addItems(list(self.sset.presets))
        self.cands.setToolTip("The schedules the fit may choose corners from")
        pick = next((n for n in self.sset.presets if ": D " in n and "grid" in n), None)
        if pick:
            self.cands.setCurrentText(pick)
        self.fit_btn = QtWidgets.QPushButton("Fit from logs")
        self.fit_btn.clicked.connect(self._fit)
        self.check_btn = QtWidgets.QPushButton("Check against logs")
        self.check_btn.clicked.connect(self._check)
        has_logs = getattr(editor.ws, "project_ref", None) is not None \
            and _log_module(editor.session) is not None
        for b in (self.fit_btn, self.check_btn):
            b.setEnabled(has_logs)
            if not has_logs:
                b.setToolTip("Needs the file opened from a project with logs")
        fb.addWidget(QtWidgets.QLabel("Candidates"))
        fb.addWidget(self.cands, 1)
        fb.addWidget(self.fit_btn)
        fb.addWidget(self.check_btn)
        right.addLayout(fb)
        self.report = QtWidgets.QPlainTextEdit(readOnly=True)
        self.report.setMaximumHeight(130)
        right.addWidget(self.report)
        self.use_btn = QtWidgets.QPushButton("Use the fitted weights")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use_fit)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok
                                        | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        bot = QtWidgets.QHBoxLayout()
        bot.addWidget(self.use_btn)
        bot.addStretch(1)
        bot.addWidget(bb)
        right.addLayout(bot)
        lay.addLayout(right, 1)
        self._fit_result = None
        for b in self.blends:
            self.list.addItem(b.name)
        self.list.currentRowChanged.connect(self._show)
        self.table.itemChanged.connect(lambda *_: self._totals())
        cur = editor.blend_box.currentText()
        names = [b.name for b in self.blends]
        self.list.setCurrentRow(names.index(cur) if cur in names else (0 if names else -1))
        self.resize(720, 560)

    # ---- one blend <-> the form
    def _keys(self) -> List[str]:
        return [s.key for s in self.sset.schedules]

    def _corner_row(self, key: str, w: float):
        r = self.table.rowCount()
        self.table.insertRow(r)
        box = QtWidgets.QComboBox()
        box.addItems(self._keys())
        if key:
            box.setCurrentText(key)
        box.currentIndexChanged.connect(lambda *_: self._totals())
        self.table.setCellWidget(r, 0, box)
        sp = QtWidgets.QDoubleSpinBox(decimals=3, minimum=0.0, maximum=1.5, singleStep=0.01)
        sp.setValue(w)
        sp.valueChanged.connect(lambda *_: self._totals())
        self.table.setCellWidget(r, 1, sp)

    def _show(self, row):
        self._save_form()
        self._row = row
        self.table.setRowCount(0)
        if not 0 <= row < len(self.blends):
            return
        b = self.blends[row]
        self.name.setText(b.name)
        self.progs.setText(", ".join(map(str, b.programs)))
        self.dsp.setText("" if b.dsp is None else str(b.dsp))
        for k, w in b.corners:
            self._corner_row(k, w)
        self.source.setText(b.source)
        self.note.setPlainText(b.note)
        self._totals()

    def _form_blend(self) -> GE.Blend:
        corners = []
        for r in range(self.table.rowCount()):
            k = self.table.cellWidget(r, 0).currentText()
            w = self.table.cellWidget(r, 1).value()
            corners.append((k, w))
        try:
            progs = tuple(int(x) for x in self.progs.text().replace(",", " ").split())
        except ValueError:
            progs = ()
        try:
            dsp = int(self.dsp.text()) if self.dsp.text().strip() else None
        except ValueError:
            dsp = None
        return GE.Blend(self.name.text().strip() or "Blend", corners, progs, dsp,
                        self.source.text().strip(), self.note.toPlainText().strip())

    def _save_form(self):
        if 0 <= self._row < len(self.blends):
            self.blends[self._row] = self._form_blend()
            it = self.list.item(self._row)
            if it is not None:
                it.setText(self.blends[self._row].name)

    def _totals(self):
        t = sum(self.table.cellWidget(r, 1).value() for r in range(self.table.rowCount())
                if self.table.cellWidget(r, 1) is not None)
        self.total.setText("Sum %.3f%s" % (t, "" if abs(t - 1) < 0.05 else
                                            " (drawn normalised to 1)"))

    def _add(self):
        self._save_form()
        self.blends.append(GE.Blend("Blend %d" % (len(self.blends) + 1)))
        self.list.addItem(self.blends[-1].name)
        self.list.setCurrentRow(len(self.blends) - 1)

    def _remove(self):
        r = self.list.currentRow()
        if 0 <= r < len(self.blends):
            self._row = -1
            del self.blends[r]
            self.list.takeItem(r)

    def _add_corner(self):
        self._corner_row("", 0.0)
        self._totals()

    def _remove_corner(self):
        r = self.table.currentRow()
        if r < 0:
            r = self.table.rowCount() - 1
        if r >= 0:
            self.table.removeRow(r)
            self._totals()

    # ---- logs
    def _events(self, b: GE.Blend):
        pl = self.ed._project_logs()
        if pl is None:
            return None, []
        evs = [e for e in pl.shifts if (not b.programs or e.program in b.programs)
               and (b.dsp is None or e.dsp == b.dsp)]
        return pl, evs

    def _fit(self):
        b = self._form_blend()
        pl, evs = self._events(b)
        if pl is None:
            return
        cands = self.sset.presets.get(self.cands.currentText(), [])
        if not evs or not cands:
            self.report.setPlainText("No logged shifts match this blend's program/dsp.")
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            fit = pl.fit(evs, cands)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if fit is None:
            self.report.setPlainText("No fit: the candidates have no usable values at the "
                                     "logged events.")
            return
        S = self.sset.schedules
        lines = ["%s: %s" % ("Fit" if fit.determinable else "Fit (not determinable)",
                             " + ".join("%.2f\u00b7%s" % (w, S[i].key)
                                        for i, w in sorted(fit.weights.items()) if w > 0.005))]
        if fit.errors:
            lines.append("Log-to-log spread: " + ", ".join(
                "%s \u00b1%.2f" % (S[i].key, e) for i, e in sorted(fit.errors.items()) if e > 0.005))
        lines.append("%d steady shifts from %d logs; rms %.0f rpm out after latency %.2f s "
                     "x rate and offsets %s" % (
                         fit.events, fit.logs, fit.rms, fit.latency,
                         ", ".join("%s %+.0f" % kv for kv in fit.lateness.items())))
        if fit.bounds:
            lines.append("%d of %d pedal-driven shifts consistent with it"
                         % (fit.bounds_ok, fit.bounds))
        lines.append(fit.verdict)
        cur = b.indices(self.sset)
        if cur:
            got = pl.evaluate(evs, cur)
            if got:
                lines.append("This blend's own weights: rms %.0f rpm out on the same events"
                             % got[0])
        self.report.setPlainText("\n".join(lines))
        self._fit_result = fit
        self.use_btn.setEnabled(True)

    def _check(self):
        b = self._form_blend()
        pl, evs = self._events(b)
        if pl is None:
            return
        cur = b.indices(self.sset)
        got = pl.evaluate(evs, cur) if cur and evs else None
        if not got:
            self.report.setPlainText("Nothing to check: no corners, or no logged shifts in "
                                     "this blend's program.")
            return
        rms, n, ok, nb = got
        self.report.setPlainText(
            "%s explains %d steady shifts at rms %.0f rpm out (latency and offsets fitted)."
            "%s" % (b.name, n, rms, ("\n%d of %d pedal-driven shifts consistent with it."
                                     % (ok, nb)) if nb else ""))

    def _use_fit(self):
        fit = self._fit_result
        if fit is None:
            return
        self.table.setRowCount(0)
        for i, w in sorted(fit.weights.items()):
            if w > 0.005:
                self._corner_row(self.sset.schedules[i].key, round(w, 3))
        self.source.setText("Fitted from %d logged shifts in %d project logs (%s)" % (
            fit.events, fit.logs, "determinable" if fit.determinable else "NOT determinable"))
        self._totals()

    def _accept(self):
        self._save_form()
        r = self.list.currentRow()
        self.current = self.blends[r].name if 0 <= r < len(self.blends) else None
        self.accept()


def open_editor(ws, kind: str = "shift"):
    """Workspace hook: open (or raise) this tab's shift map or lock-up editor."""
    s = getattr(ws, "primary", None)
    title = TITLES.get(kind, "Editor")
    if getattr(ws, "view_only", None):
        QtWidgets.QMessageBox.information(ws, title, "Not in a read-only tab.")
        return None
    sset = schedule_set_for(s, kind) if s is not None else None
    if sset is None:
        QtWidgets.QMessageBox.information(
            ws, title,
            "Nothing to edit was found in this file, or its gearbox has no %s yet."
            % title.lower())
        return None
    return ws.open_tool("shiftedit" if kind == "shift" else "tccedit",
                        lambda: GearboxEditDialog(ws, sset))

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Injector setup... window for unit-injector (PD) engines
(:mod:`ovtsuite.injectors`).

**This file** says which hardware set the calibration is set up for: the
closest preset, and whether it is the same calibration or only near it.
**New hardware** picks what is going in -- a factory injector set, an
aftermarket nozzle or one of your own presets -- and whether the injector
bodies change too (complete injectors) or only the nozzles, and how the start
of injection follows (by default the centre of injection is kept).
**From logs** instead corrects the duration maps from data logs taken with the
new hardware (:mod:`ovtsuite.injlearn`): the EDC16 lambda probe, or the same
operating points logged before and after the change. The
preview draws fuel per crank degree before and after, lists every table it
will write and what it found; **Apply** / **OK** write it as one undo step.

Injector setup does not change power: the engine is asked for the same fuel
as before and the new hardware delivers it. Raising the limiters to use the
extra capacity is ordinary tuning.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import eoi as _eoi
from . import injectors as I

_SETTINGS_KEY = "injectors/user_presets"


def _session(ws):
    return getattr(ws, "primary", None)


def availability(ws) -> Optional[str]:
    """None when the window can open for tab ``ws``, else why not (kept per
    entity list: the Tools menu asks on every refresh)."""
    s = _session(ws)
    if s is None:
        return "No file open"
    ents = getattr(s, "entities", None)
    hit = getattr(s, "_inj_avail", None)
    if hit is not None and hit[0] is ents:
        return hit[1]
    try:
        why = I.availability(s)
    except Exception as ex:                       # noqa: BLE001 - never breaks the menu
        why = str(ex) or type(ex).__name__
    try:
        s._inj_avail = (ents, why)
    except Exception:
        pass
    return why


def open_for(ws):
    why = availability(ws)
    if why:
        QtWidgets.QMessageBox.information(ws, "Injector setup", why)
        return None
    return ws.open_tool("injectors", lambda: InjectorDialog(ws))


# ------------------------------------------------------------- user presets
def user_presets() -> List[I.Preset]:
    try:
        from .appsettings import settings
        raw = settings().value(_SETTINGS_KEY, "[]")
        return [I.Preset.from_json(d) for d in json.loads(raw or "[]")]
    except Exception:
        return []


def _save_user_presets(presets: List[I.Preset]) -> None:
    from .appsettings import settings
    settings().setValue(_SETTINGS_KEY, json.dumps([p.to_json() for p in presets]))


# ------------------------------------------------------------- window size
#: the preview's share of a window opened for the first time (logical px)
_PREVIEW_H = 260


def _fit_window(dlg, top, split, geom_key: str, split_key: str, width: int) -> bool:
    """Size an Injector setup window (this one and the EDC15 VE pump's): the
    size it was left at, else as tall as the settings above (``top``) need
    plus a preview -- never larger than the screen. ``split`` holds ``top``
    (in its scroll area) and the preview; it is restored with the size. False
    when the splitter is to be shared out on the first show
    (:func:`_default_split`)."""
    from .appsettings import settings
    from .gui_fit import available, fit_to_screen
    try:
        st = settings()
        geo = st.value(geom_key)
    except Exception:                                          # noqa: BLE001
        st, geo = None, None
    if geo is not None and dlg.restoreGeometry(geo):
        g = available(dlg)
        if g is not None and (dlg.width() > g.width() or dlg.height() > g.height()):
            fit_to_screen(dlg, dlg.width(), dlg.height(), 1.0, 0.95)
        spl = st.value(split_key) if st is not None else None
        if spl is not None and split.restoreState(spl):
            return True
    else:
        chrome = 70                     # margins, the button row
        fit_to_screen(dlg, width, top.sizeHint().height() + _PREVIEW_H + chrome)
    return False


def _default_split(top, split, preview: int = _PREVIEW_H) -> None:
    """The settings as tall as they need and the preview the rest; on a short
    window the preview keeps ``preview`` (or half the height, whichever is
    less) and the settings scroll."""
    total = sum(split.sizes())
    if total <= 0:
        return
    h = min(top.sizeHint().height(), max(total - preview, total // 2))
    split.setSizes([h, max(total - h, 1)])


def _scroller(top) -> QtWidgets.QScrollArea:
    """``top`` in a frameless scroll area that scrolls only up and down: the
    settings shrink (and scroll) on a short screen instead of pushing the
    window past it."""
    sa = QtWidgets.QScrollArea()
    sa.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    sa.setWidgetResizable(True)
    sa.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sa.setWidget(top)
    sa.setMinimumWidth(top.minimumSizeHint().width()
                       + sa.verticalScrollBar().sizeHint().width())
    sa.setMinimumHeight(min(200, top.minimumSizeHint().height()))
    return sa


def _keep_size(dlg, split, geom_key: str, split_key: str) -> None:
    """Remember the size (and the splitter) the window was left at."""
    try:
        from .appsettings import settings
        st = settings()
        st.setValue(geom_key, dlg.saveGeometry())
        st.setValue(split_key, split.saveState())
    except Exception:                                          # noqa: BLE001
        pass


def _alive(w) -> bool:
    try:
        from shiboken6 import isValid
        return isValid(w)
    except Exception:                                          # noqa: BLE001
        return True


# ------------------------------------------------------------- the chart
class FlowChart(QtWidgets.QWidget):
    """Fuel per crank degree over engine speed at a few quantities, at 10°
    BTDC: the file as it is (dashed), after the change (solid) and, when
    shown, the comparison file (dotted)."""

    COLORS = ("#2F6FB3", "#C2571A", "#3B8F3B")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.old, self.new, self.cmp = {}, {}, {}
        self.setMinimumSize(300, 140)

    def set_data(self, old, new, cmp=None):
        self.old, self.new = dict(old or {}), dict(new or {})
        self.cmp = dict(cmp or {})
        self.update()

    def paintEvent(self, _e):                                 # noqa: N802
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QtGui.QPalette.ColorRole.Base))
        ink = pal.color(QtGui.QPalette.ColorRole.Text)
        pts = [v for d in (self.old, self.new, self.cmp) for c in d.values() for v in c]
        if not pts:
            p.setPen(ink)
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "No preview")
            return
        fm = p.fontMetrics()
        L, R, T, B = fm.horizontalAdvance("00.00") + 16, 12, 24, fm.height() + 12
        w, h = self.width() - L - R, self.height() - T - B
        xs = [x for x, _y in pts]
        ys = [y for _x, y in pts]
        x0, x1 = min(xs), max(xs)
        top = max(ys) * 1.05 or 1.0
        step = next(st for st in (0.25, 0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 50.0)
                    if top / st <= 6) if top <= 300 else top / 5
        steps = max(1, int(-(-top // step)))
        y0, y1 = 0.0, steps * step
        sx = lambda x: L + (x - x0) / ((x1 - x0) or 1) * w            # noqa: E731
        sy = lambda y: T + h - (y - y0) / ((y1 - y0) or 1) * h        # noqa: E731
        grid = QtGui.QColor(ink)
        grid.setAlpha(40)
        p.setPen(QtGui.QPen(grid, 1))
        for i in range(steps + 1):
            yv = y0 + (y1 - y0) * i / steps
            p.drawLine(QtCore.QPointF(L, sy(yv)), QtCore.QPointF(L + w, sy(yv)))
            p.setPen(ink)
            p.drawText(QtCore.QRectF(0, sy(yv) - 8, L - 6, 16),
                       QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                       "%.2f" % yv)
            p.setPen(QtGui.QPen(grid, 1))
        for xv in range(int(x0), int(x1) + 1, 500):
            p.setPen(ink)
            p.drawText(QtCore.QRectF(sx(xv) - 30, T + h + 4, 60, fm.height()),
                       QtCore.Qt.AlignmentFlag.AlignHCenter, "%d" % xv)
        p.setPen(ink)
        p.drawText(QtCore.QRectF(L, 2, w, T - 4), QtCore.Qt.AlignmentFlag.AlignLeft,
                   "Fuel per crank degree (mg) over engine speed (rpm), at 10° BTDC "
                   "— dashed: now, solid: after" + (", dotted: comparison"
                                                     if self.cmp else ""))
        for k, q in enumerate(sorted(set(self.old) | set(self.new) | set(self.cmp))):
            col = QtGui.QColor(self.COLORS[k % len(self.COLORS)])
            for data, style in ((self.old, QtCore.Qt.PenStyle.DashLine),
                                (self.new, QtCore.Qt.PenStyle.SolidLine),
                                (self.cmp, QtCore.Qt.PenStyle.DotLine)):
                line = data.get(q)
                if not line:
                    continue
                p.setPen(QtGui.QPen(col, 2, style))
                path = QtGui.QPainterPath(QtCore.QPointF(sx(line[0][0]), sy(line[0][1])))
                for x, y in line[1:]:
                    path.lineTo(QtCore.QPointF(sx(x), sy(y)))
                p.drawPath(path)
            p.setPen(col)
            p.drawText(QtCore.QPointF(L + w - 60, T + 14 + 14 * k), "%g mg" % q)
        p.end()


# ------------------------------------------------------------- the window
class InjectorDialog(QtWidgets.QDialog):
    """Fit other unit injectors or nozzles to this file's calibration."""

    def __init__(self, ws, parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.setWindowTitle("Injector setup")
        try:
            from .gui import app_icon
            self.setWindowIcon(app_icon())
        except Exception:                                      # noqa: BLE001
            pass
        self.session = _session(ws)
        #: (undo revision, the choices) as the last Apply left them: OK then
        #: only closes while neither has changed
        self._applied_at = None
        self.platform = I.platform_for(self.session)
        self.models = [m for m in _eoi.models(self.session)
                       if not I._missing_duration(m)]
        self.user = user_presets()
        self.cat = I.catalogue(self.user)
        self.plans: List[I.FitPlan] = []
        self._timer = QtCore.QTimer(self, singleShot=True, interval=150)
        self._timer.timeout.connect(self._replan)
        self._build()
        self._fill_presets()
        self._identify()
        self._show_cmp()
        self._timing_changed()
        self._fit_window()
        self._schedule()

    # ---------------------------------------------------------------- size
    _GEOM_KEY = "injectors/geometry"
    _SPLIT_KEY = "injectors/split"

    def _fit_window(self):
        """The size it was left at, else as tall as the settings need plus
        a preview -- never larger than the screen."""
        self._split_set = _fit_window(self, self._top, self.split, self._GEOM_KEY,
                                      self._SPLIT_KEY, 1000)

    def showEvent(self, ev):                                   # noqa: N802
        super().showEvent(ev)
        if not self._split_set:
            self._split_set = True
            _default_split(self._top, self.split)
        from .gui_fit import keep_on_screen
        QtCore.QTimer.singleShot(0, lambda: _alive(self) and keep_on_screen(self))

    def done(self, r):
        _keep_size(self, self.split, self._GEOM_KEY, self._SPLIT_KEY)
        super().done(r)

    # ---------------------------------------------------------------- layout
    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        # the settings above, the preview below: a splitter, so the preview
        # gives way on a short screen and the window never needs to be tall
        top = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(top)
        lay.setContentsMargins(0, 0, 0, 0)
        self._top = top
        # this file
        gb = QtWidgets.QGroupBox("This file")
        g = QtWidgets.QGridLayout(gb)
        self.ds_boxes = []
        row = 0
        if len(self.models) > 1:
            g.addWidget(QtWidgets.QLabel("Datasets to change:"), row, 0)
            hb = QtWidgets.QHBoxLayout()
            for m in self.models:
                cb = QtWidgets.QCheckBox(m.label if hasattr(m, "label") else (m.group or "Main"))
                cb.setChecked(True)
                cb.toggled.connect(self._schedule)
                hb.addWidget(cb)
                self.ds_boxes.append(cb)
            hb.addStretch(1)
            g.addLayout(hb, row, 1)
            row += 1
        g.addWidget(QtWidgets.QLabel("Calibrated for:"), row, 0,
                    QtCore.Qt.AlignmentFlag.AlignTop)
        self.ident = QtWidgets.QLabel()
        self.ident.setWordWrap(True)
        self.ident.setTextFormat(QtCore.Qt.TextFormat.RichText)
        g.addWidget(self.ident, row, 1)
        row += 1
        self.fitted_lbl = QtWidgets.QLabel("Fitted:")
        g.addWidget(self.fitted_lbl, row, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        self.fitted = QtWidgets.QLabel()
        self.fitted.setWordWrap(True)
        self.fitted.setTextFormat(QtCore.Qt.TextFormat.RichText)
        g.addWidget(self.fitted, row, 1)
        row += 1
        # the comparison file (the tab's): what it is set up for, and its
        # tables beside the ones this writes
        self.cmp_models = self._cmp_models()
        self.cmp_check = QtWidgets.QCheckBox("Show comparison")
        self.cmp_check.setToolTip(
            "Show what the comparison file is set up for, how each table this "
            "writes compares with the comparison's (orange outline: differs) and the "
            "comparison's fuel per degree in the chart (dotted).")
        self.cmp_check.setChecked(True)
        self.cmp_check.toggled.connect(self._show_cmp)
        self.cmp_lbl = QtWidgets.QLabel("Comparison:")
        g.addWidget(self.cmp_lbl, row, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        cv = QtWidgets.QVBoxLayout()
        cv.setSpacing(2)
        cv.addWidget(self.cmp_check)
        self.cmp_ident = QtWidgets.QLabel()
        self.cmp_ident.setWordWrap(True)
        self.cmp_ident.setTextFormat(QtCore.Qt.TextFormat.RichText)
        cv.addWidget(self.cmp_ident)
        g.addLayout(cv, row, 1)
        for w in (self.cmp_lbl, self.cmp_check, self.cmp_ident):
            w.setVisible(bool(self.cmp_models))
        g.setColumnStretch(1, 1)
        lay.addWidget(gb)
        # one entry per Apply that recorded hardware: (the record as applied,
        # the undo command it went with, the record as it was before or None)
        self._hw_written: List[Tuple[dict, object, Optional[dict]]] = []
        self.idents: List[I.DatasetMatch] = []

        # new hardware
        gb = QtWidgets.QGroupBox("New hardware")
        gl = QtWidgets.QVBoxLayout(gb)
        self.mode = QtWidgets.QTabWidget()
        self.mode.setDocumentMode(True)
        pw = QtWidgets.QWidget()
        f = QtWidgets.QGridLayout(pw)
        f.addWidget(QtWidgets.QLabel("Fitting:"), 0, 0)
        self.preset = QtWidgets.QComboBox()
        self.preset.setMinimumContentsLength(34)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(self.preset, 1)
        self.preset_edit = QtWidgets.QToolButton()
        self.preset_edit.setText("Edit...")
        self.preset_edit.setToolTip("Change the preset chosen: its name, parts and notes, "
                                    "or take its calibration from this file again (your "
                                    "own only)")
        self.preset_edit.clicked.connect(self._edit_preset)
        self.preset_rename = QtWidgets.QToolButton()
        self.preset_rename.setText("Rename...")
        self.preset_rename.setToolTip("Rename the preset chosen (your own only)")
        self.preset_rename.clicked.connect(self._rename_preset)
        self.preset_del = QtWidgets.QToolButton()
        self.preset_del.setText("Delete")
        self.preset_del.setToolTip("Delete the preset chosen (your own only)")
        self.preset_del.clicked.connect(self._delete_preset)
        for b in (self.preset_edit, self.preset_rename, self.preset_del):
            prow.addWidget(b)
        f.addLayout(prow, 0, 1)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.info, 1, 1)
        f.addWidget(QtWidgets.QLabel("What changes:"), 2, 0)
        hb = QtWidgets.QHBoxLayout()
        self.r_body = QtWidgets.QRadioButton("Complete injectors (bodies and nozzles)")
        self.r_noz = QtWidgets.QRadioButton("Nozzles only (bodies stay)")
        self.r_body.setToolTip("The injector bodies change too: the solenoid (BIP) "
                               "calibration of the new bodies is written as well.")
        self.r_noz.setToolTip("Only the nozzles change: this file keeps its own "
                              "solenoid (BIP) calibration.")
        self.r_body.setChecked(True)
        # "Nozzles only" set by picking an aftermarket preset (not by hand):
        # going back to a factory set or one of your own goes back to complete
        self._noz_forced = False
        self.r_noz.toggled.connect(self._what_toggled)
        for r in (self.r_body, self.r_noz):
            r.toggled.connect(self._schedule)
            hb.addWidget(r)
        hb.addStretch(1)
        f.addLayout(hb, 2, 1)
        hb = QtWidgets.QHBoxLayout()
        self.sel_cb = QtWidgets.QCheckBox("Use the preset's map selection curve where "
                                          "the layouts match")
        self.sel_cb.setChecked(True)
        self.sel_cb.setToolTip("Exact when this file has as many duration tables and "
                               "selector points as the preset; otherwise each table is "
                               "read from the preset where this file's selector picks it.")
        self.sel_cb.toggled.connect(self._schedule)
        hb.addWidget(self.sel_cb)
        hb.addStretch(1)
        f.addLayout(hb, 3, 1)
        self.record_cb = QtWidgets.QCheckBox("Record in the project as fitted")
        self.record_cb.setChecked(True)
        self.record_cb.setToolTip(
            "Apply also notes in the project that the car has this hardware, and that the "
            "versions saved after this one are calibrated for it: the dyno chart then "
            "does not compare them as though the injectors were the same, and logs are "
            "sorted into before and after the change.")
        self.record_cb.setVisible(self._ref() is not None)
        rb = QtWidgets.QHBoxLayout()
        rb.addWidget(self.record_cb)
        self.fitted_btn = QtWidgets.QPushButton("Record as fitted...")
        self.fitted_btn.setToolTip("Record in the project that the car has this preset's "
                                   "hardware, without changing the file: when it went in "
                                   "before the file was changed, or the file is already set "
                                   "up for it")
        self.fitted_btn.clicked.connect(self._record_fitted)
        self.fitted_btn.setVisible(self._ref() is not None)
        rb.addWidget(self.fitted_btn)
        rb.addStretch(1)
        f.addLayout(rb, 4, 1)
        f.setRowStretch(5, 1)               # the tab is as tall as From logs
        f.setColumnStretch(1, 1)
        self.mode.addTab(pw, "Preset")
        self.mode.addTab(self._build_logs(), "From logs")
        self.mode.setTabToolTip(1, "Correct the duration maps from logs taken with the "
                                   "new hardware: a preset's starting point, or nozzles "
                                   "with no preset")
        self.mode.currentChanged.connect(self._mode_changed)
        gl.addWidget(self.mode)
        f = QtWidgets.QGridLayout()
        gl.addLayout(f)
        self.copies_cb = QtWidgets.QCheckBox("Also the coding-variant copies")
        nvar = 0
        try:
            nvar = len(self.session.variant_entities())
        except Exception:
            nvar = 0
        self.copies_cb.setVisible(nvar > 0)
        self.copies_cb.setChecked(True)
        self.copies_cb.setToolTip("This file keeps a second copy of tables for other "
                                  "codings; the hardware is the same whichever coding runs.")
        f.addWidget(QtWidgets.QLabel("Start of injection:"), 0, 0)
        self.timing = QtWidgets.QComboBox()
        for key in (I.KEEP_CENTRE, I.KEEP_EOI, I.KEEP_SOI):
            self.timing.addItem(I.TIMING_LABELS[key], key)
            self.timing.setItemData(self.timing.count() - 1, I.TIMING_TIPS[key],
                                    QtCore.Qt.ItemDataRole.ToolTipRole)
        self.timing.currentIndexChanged.connect(self._schedule)
        self.timing.currentIndexChanged.connect(self._timing_changed)
        self.start_cb = QtWidgets.QCheckBox("Also the start (cranking) maps")
        self.start_cb.setChecked(True)
        self.start_cb.setToolTip(
            "Starting reads the same duration maps, so the start-of-injection maps used "
            "while the engine cranks (engine speed x coolant) are moved the same way, at "
            "the quantity the ECU starts with. Unticked they stay, and the new duration "
            "moves the end of injection while starting.")
        self.start_cb.setVisible(any(ss.second != "q" for m in self.models
                                     for ss in getattr(m, "soi_sets", ())))
        self.start_cb.toggled.connect(self._schedule)
        hb = QtWidgets.QHBoxLayout()
        hb.addWidget(self.timing)
        hb.addWidget(self.start_cb)
        hb.addWidget(self.copies_cb)
        hb.addStretch(1)
        f.addLayout(hb, 0, 1)
        f.setColumnStretch(1, 1)
        lay.addWidget(gb)

        # preview
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.chart = FlowChart()
        split.addWidget(self.chart)
        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.changes = QtWidgets.QTableWidget(0, 5)
        self.changes.setHorizontalHeaderLabels(["Table", "What", "Cells", "Largest change",
                                                "Comparison"])
        self.changes.horizontalHeaderItem(4).setToolTip(
            "The table as this writes it against the comparison file's (orange outline: differs)")
        self.changes.verticalHeader().setVisible(False)
        self.changes.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.changes.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        # as the table editor: alternating rows make the Windows 11 style draw
        # the selection solid, over shaded cells too; selected text readable
        from .gui_cellmarks import CellDelegate
        self.changes.setAlternatingRowColors(True)
        self.changes.setItemDelegate(CellDelegate(self.changes))
        self.changes.horizontalHeader().setStretchLastSection(True)
        self.changes.cellDoubleClicked.connect(self._goto)
        self.changes.setMinimumHeight(70)
        rl.addWidget(self.changes, 3)
        self.goto_status = QtWidgets.QLabel()
        self.goto_status.setWordWrap(True)
        self.goto_status.setStyleSheet("color: gray")
        rl.addWidget(self.goto_status)
        self.findings = QtWidgets.QListWidget()
        self.findings.setWordWrap(True)
        self.findings.setMinimumHeight(50)
        rl.addWidget(self.findings, 2)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 560])
        self.split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(_scroller(top))
        self.split.addWidget(split)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        outer.addWidget(self.split, 1)

        # the tools on the left; Help, Apply, Cancel, OK on the right, OK last
        # (as the other editing windows)
        bb = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Save as preset...")
        self.save_btn.setToolTip("Keep the hardware this file is set up for as one of "
                                 "your presets, to fit it to other files")
        self.save_btn.clicked.connect(self._save_preset)
        self.eoi_btn = QtWidgets.QPushButton("End of injection...")
        self.eoi_btn.clicked.connect(self._open_eoi)
        bb.addWidget(self.save_btn)
        bb.addWidget(self.eoi_btn)
        bb.addStretch(1)
        self.help_btn = QtWidgets.QPushButton("Help")
        self.help_btn.setToolTip("Help for this window (F1)")
        self.help_btn.clicked.connect(self._help)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.setToolTip("Write the tables listed as one undo step; the window "
                                  "stays open")
        self.apply_btn.clicked.connect(self._apply)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setToolTip("Close without writing anything more (what Apply wrote "
                                   "stays: Edit > Undo takes it back)")
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn = QtWidgets.QPushButton("OK")
        self.ok_btn.setToolTip("Write the tables listed, unless Apply already has, and close")
        self.ok_btn.clicked.connect(self._ok)
        for b in (self.save_btn, self.eoi_btn, self.help_btn, self.apply_btn,
                  self.cancel_btn, self.ok_btn):
            b.setAutoDefault(False)
        for b in (self.help_btn, self.apply_btn, self.cancel_btn, self.ok_btn):
            bb.addWidget(b)
        outer.addLayout(bb)
        if getattr(self.ws, "view_only", None):
            self.apply_btn.setEnabled(False)
            self.ok_btn.setEnabled(False)

    def _build_logs(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        intro = QtWidgets.QLabel(
            "Logs taken with the new hardware show what it really delivers. Fit the "
            "nearest preset first (or keep the file as it is), drive, log, then correct "
            "here; repeat until nothing is left to correct.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.log_hint = QtWidgets.QLabel()
        self.log_hint.setWordWrap(True)
        self.log_hint.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.log_hint.setText(self._recipe_hint())
        v.addWidget(self.log_hint)
        hb = QtWidgets.QHBoxLayout()
        self.after_list = self._log_list(
            hb, "With the new hardware",
            "Logs taken with this file and the new injectors or nozzles fitted")
        self.before_list = self._log_list(
            hb, "With the old hardware (optional)",
            "Logs of the same steady points (idle in neutral, a steady road speed in "
            "the same gear) taken before the change, with the old hardware and its own "
            "calibration")
        v.addLayout(hb, 1)
        self.log_status = QtWidgets.QLabel()
        self.log_status.setWordWrap(True)
        self.log_status.setStyleSheet("color: gray")
        v.addWidget(self.log_status)
        self._runs_cache: Dict[str, object] = {}
        self.learned = None
        return w

    def _log_list(self, parent_layout, title, tip) -> QtWidgets.QListWidget:
        box = QtWidgets.QVBoxLayout()
        lab = QtWidgets.QLabel(title)
        lab.setToolTip(tip)
        box.addWidget(lab)
        lst = QtWidgets.QListWidget()
        lst.setToolTip(tip)
        lst.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        lst.setMinimumHeight(60)
        lst.setMaximumHeight(110)
        box.addWidget(lst, 1)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add...")
        add.clicked.connect(lambda _c=False, ls=lst: self._add_logs(ls))
        rem = QtWidgets.QPushButton("Remove")
        rem.clicked.connect(lambda _c=False, ls=lst: self._remove_logs(ls))
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        box.addLayout(row)
        parent_layout.addLayout(box, 1)
        return lst

    def _recipe_hint(self) -> str:
        """Which Data logging preset carries what the correction needs, planned
        on this file's own measuring blocks."""
        try:
            from .datalog.channels import ChannelNamer
            from .logtune.roles import EDC15_RECIPES, RECIPES, recipe_groups
            key = "e15inj" if getattr(self.session, "platform", "") == "edc15" else "injectors"
            rec = next(r for r in tuple(RECIPES) + tuple(EDC15_RECIPES) if r.key == key)
            groups, missing = recipe_groups(ChannelNamer(self.session), rec, rec.max_groups)
        except Exception:                                      # noqa: BLE001
            return ""
        if missing or not groups:
            return ""
        lam = ("" if key == "e15inj" else
               " Block 40 carries the ECU's own lambda probe: warm, steady mid and full "
               "load is then enough, without logs from before the change.")
        return ("Log with the Data logging preset <i>%s</i> (measuring blocks %s).%s" %
                (_esc(rec.title), ", ".join(str(g) for g in groups), _esc(lam)))

    def _project_ref(self):
        for o in (self.ws, getattr(self.ws, "window", lambda: None)()):
            ref = getattr(o, "project_ref", None) if o is not None else None
            if ref is not None:
                return ref
        return None

    def _add_logs(self, lst):
        from . import recent as _recent
        from .gui_hardware import pick_project_logs
        ref = self._project_ref()
        # the project's own logs first (marked before / after the recorded
        # change), a file from disk after
        got = pick_project_logs(self, ref, I.HW_KIND, before=lst is self.before_list) \
            if ref is not None else "file"
        if got is None:
            return
        if got != "file":
            for path in got:
                self._put_log(lst, path)
            return
        paths, _f = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Data logs", _recent.get_last_dir("log"),
            "Data logs (*.csv);;All files (*)")
        for path in paths:
            self._put_log(lst, path)
        if paths:
            _recent.set_last_dir(os.path.dirname(paths[0]), "log")

    def _put_log(self, lst, path):
        if path not in self._log_paths(lst):          # items show the base name
            it = QtWidgets.QListWidgetItem(os.path.basename(path))
            it.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            it.setToolTip(path)
            lst.addItem(it)
        self._schedule()

    def _remove_logs(self, lst):
        for it in lst.selectedItems():
            lst.takeItem(lst.row(it))
        self._schedule()

    @staticmethod
    def _log_paths(lst) -> List[str]:
        return [lst.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(lst.count())]

    def _logs(self, paths):
        """``(path, the ECU ident it records, its run)`` per log, each read once."""
        from . import injlearn as L
        out = []
        for path in paths:
            key = "%s|%s" % (path, os.path.getmtime(path) if os.path.exists(path) else 0)
            if key not in self._runs_cache:
                self._runs_cache[key] = L.read_logs(self.session, [path])
            out += self._runs_cache[key]
        return out

    def _log_use(self, logs, labels):
        """Which ticked datasets each log is used for (``injlearn.log_datasets``:
        the ECU ident a log records names the dataset it was taken on; a log
        with none, or naming none ticked, is used for every one ticked), and the
        notes the assistant gives too."""
        from . import injlearn as L
        which = L.log_datasets(self.session, labels, [i for _p, i, _r in logs])
        notes = []
        if len(self.models) > 1:
            every = [m.label for m in self.models]
            named = L.log_datasets(self.session, every, [i for _p, i, _r in logs])
            unknown = sorted({os.path.basename(p) for (p, _i, _r), (_d, how)
                              in zip(logs, named) if how == "unknown"})
            if unknown and len(labels) > 1:
                notes.append("%s record%s no ECU ident naming one of this file's datasets, "
                             "so %s used for every dataset ticked. A log is recorded while "
                             "one dataset runs (the coding picks it): tick only that one "
                             "to correct it alone." % (
                                 ", ".join(unknown), "s" if len(unknown) == 1 else "",
                                 "it is" if len(unknown) == 1 else "they are"))
            other = sorted({os.path.basename(p) for (p, _i, _r), (_d, how), (_d2, h2)
                            in zip(logs, which, named) if how == "unknown" and h2 == "ident"})
            if other:
                notes.append("%s record%s the ident of a dataset not ticked: the car was not "
                             "running the datasets ticked when %s logged." % (
                                 ", ".join(other), "s" if len(other) == 1 else "",
                                 "it was" if len(other) == 1 else "they were"))
            for ds in sorted({tuple(d) for d, how in which if how == "ident" and len(d) > 1}):
                notes.append("%s share one ident (they differ by coding only), so a log "
                             "recorded on either is used for both: tick only the one the car "
                             "is coded to to correct it alone." % " and ".join(ds))
        return which, notes

    def _show_log_use(self, lst, logs, which):
        for i in range(lst.count()):
            it = lst.item(i)
            path = it.data(QtCore.Qt.ItemDataRole.UserRole)
            tip = path
            for (p, _i, _r), (ds, how) in zip(logs, which):
                if p == path and len(self.models) > 1:
                    tip += "\nUsed for: %s (%s)" % (
                        ", ".join(ds), "its ECU ident" if how == "ident"
                        else "no ECU ident naming a dataset ticked: every one ticked")
            it.setToolTip(tip)

    def from_logs(self) -> bool:
        return self.mode.currentIndex() == 1

    def _mode_changed(self, *_a):
        self._schedule()

    # ---------------------------------------------------------------- presets
    def _fill_presets(self, select: Optional[str] = None):
        """Fill the Fitting list; ``select`` (else the preset chosen now, if
        it is still there) is chosen afterwards, else the first entry."""
        keep = select or self.preset.currentData()
        self.preset.blockSignals(True)
        self.preset.clear()
        groups = {}
        for p in self.cat:
            groups.setdefault(p.kind, []).append(p)
        model = self.preset.model()
        for kind in (I.OEM, I.AFTERMARKET, I.USER):
            items = groups.get(kind) or []
            if not items:
                continue
            self.preset.addItem(I.KIND_LABELS[kind])
            it = model.item(self.preset.count() - 1)
            it.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            f = it.font()
            f.setBold(True)
            it.setFont(f)
            for p in items:
                self.preset.addItem("    " + p.name, p.id)
        at = self.preset.findData(keep) if keep else -1
        if at < 0:                                   # the first selectable entry
            at = next((i for i in range(self.preset.count()) if self.preset.itemData(i)), -1)
        if at >= 0:
            self.preset.setCurrentIndex(at)
        self.preset.blockSignals(False)
        self._preset_changed()

    def current_preset(self) -> Optional[I.Preset]:
        pid = self.preset.currentData()
        return self.cat.get(pid) if pid else None

    def _preset_changed(self, *_a):
        p = self.current_preset()
        own = p is not None and p.kind == I.USER
        for b in (self.preset_edit, self.preset_rename, self.preset_del):
            b.setEnabled(own)
        if p is None:
            self.info.setText("")
            return
        bits = []
        if p.listed_for:
            bits.append("Listed for: " + ", ".join(p.listed_for))
        parts = p.part_numbers()
        if not parts and p.derived:
            base = self.cat.get(p.base)
            if base is not None and base.part_numbers():
                parts = "%s (%s)" % (base.part_numbers(), base.name)
        if parts:
            bits.append("OEM part numbers: " + parts)
        if p.body or p.nozzle:
            bits.append("; ".join(x for x in (p.body and "Body: " + p.body,
                                              p.nozzle and "Nozzle: " + p.nozzle) if x))
        bits.append("<b>%s</b>" % I.CONFIDENCE_LABELS.get(p.confidence, p.confidence))
        if p.derived:
            base = self.cat.get(p.base)
            if abs(p.flow - 1.0) < 1e-9:
                bits.append("Assumed to deliver what %s does: its maps are written as "
                            "they are, until logs say otherwise"
                            % (base.name if base else p.base))
            else:
                bits.append("Starts from %s with %s %+.0f %% (%s), so it asks for less "
                            "fuel than the nozzles deliver until logs correct it" % (
                                base.name if base else p.base,
                                "hole area" if p.basis == "area" else "flow",
                                (p.flow - 1) * 100, "an upper bound" if p.basis == "area"
                                else "the vendor's figure"))
        if p.source:
            bits.append("From: " + p.source)
        if p.notes:
            bits.append(p.notes)
        self.info.setText("<br>".join(_esc(b) if not b.startswith("<b>") else b
                                      for b in bits))
        if p.kind == I.AFTERMARKET:
            if self.r_body.isChecked():
                self._set_what(body=False)
                self._noz_forced = True
        elif self._noz_forced and self.r_noz.isChecked():
            self._set_what(body=True)
        self._schedule()

    def _set_what(self, body: bool):
        self._setting_what = True
        try:
            (self.r_body if body else self.r_noz).setChecked(True)
        finally:
            self._setting_what = False
        self._noz_forced = False

    def _what_toggled(self, *_a):
        if not getattr(self, "_setting_what", False):
            self._noz_forced = False                    # chosen by hand: it stays

    def _identify(self):
        self._show_fitted()
        self.idents = []
        if not self.models:
            self.ident.setText("No duration set in this file's definition.")
            return
        self.idents = I.identify_datasets(self.models, self.cat, 3)
        self.ident.setText(self._idents_html(self.idents, self.session))
        self._identify_cmp()

    def _idents_html(self, idents, session) -> str:
        """What each dataset is set up for, as rich text (datasets with the
        same answer once, under all their names), and the signs of a tune."""
        groups: List[Tuple[str, List[str]]] = []
        for r in idents:
            txt = self._ident_text(r)
            for g in groups:
                if g[0] == txt:
                    g[1].append(r.label)
                    break
            else:
                groups.append((txt, [r.label]))
        if not groups:
            return ""
        if len(idents) < 2:
            body = groups[0][0]
        elif len(groups) == 1:
            body = "%s: %s" % (_esc(_count_label(len(idents))), groups[0][0])
        else:
            body = "<br>".join("<u>%s</u>: %s" % (_esc(", ".join(names)), txt)
                               for txt, names in groups)
        why = I.datasets_disagree(idents)
        if why:
            body += "<br><span style='color:#B26A00'>%s</span>" % _esc(why)
        signs = I.tuned_signs(session)
        return body + "".join("<br><span style='color:#B26A00'>Not stock: %s</span>"
                              % _esc(t) for t in signs)

    # ---------------------------------------------------------------- comparison
    def _cmp_session(self):
        return getattr(self.ws, "comparison", None)

    def _cmp_models(self) -> list:
        """The comparison file's datasets with a duration set ([] without a
        comparison, or one that is not a unit-injector file)."""
        c = self._cmp_session()
        if c is None:
            return []
        try:
            return [m for m in _eoi.models(c) if not I._missing_duration(m)]
        except Exception:                                      # noqa: BLE001
            return []

    def _cmp_shown(self) -> bool:
        return bool(self.cmp_models) and self.cmp_check.isChecked()

    def _identify_cmp(self):
        if not self.cmp_models:
            self.cmp_ident.setText("")
            return
        name = getattr(getattr(self._cmp_session(), "binfile", None), "name", "") or \
            "the comparison file"
        try:
            body = self._idents_html(I.identify_datasets(self.cmp_models, self.cat, 3),
                                     self._cmp_session())
        except Exception as ex:                                # noqa: BLE001
            body = _esc(str(ex))
        self.cmp_ident.setText("<b>%s</b> (read only): %s" % (_esc(name), body))

    def _show_cmp(self, *_a):
        on = self._cmp_shown()
        self.cmp_ident.setVisible(on)
        self.changes.setColumnHidden(4, not on)
        self._chart()

    def _cmp_model_for(self, model):
        """The comparison's dataset beside ``model``: the same name, else
        the first."""
        ms = self.cmp_models
        if not ms:
            return None
        g = getattr(model, "group", "")
        return next((m for m in ms if getattr(m, "group", "") == g), ms[0])

    def _cmp_curve(self, model) -> dict:
        """The comparison's fuel per crank degree at 10° BTDC, at the
        quantities the chart draws the plan's."""
        cm = self._cmp_model_for(model)
        if cm is None:
            return {}
        try:
            ch = I.characteristic(cm)
        except Exception:                                      # noqa: BLE001
            return {}
        qs = (set(self.plans[0].curve_old) | set(self.plans[0].curve_new)) \
            if self.plans else set()
        out = {}
        for q in sorted(qs or {20.0, 30.0, 45.0}):
            if q > ch.q_max + 1e-6:
                continue
            line = []
            for n_ in range(1000, 4001, 250):
                d = ch.duration(10.0, n_, q)
                if d >= 3.0:
                    line.append((float(n_), q / d))
            out[q] = line
        return out

    def _chart(self):
        if not self.plans:
            self.chart.set_data({}, {})
            return
        pl = self.plans[0]
        self.chart.set_data(pl.curve_old, pl.curve_new,
                            self._cmp_curve(pl.model) if self._cmp_shown() else None)

    def _cmp_entity(self, e):
        """The comparison file's table of ``e``'s name (in its dataset, else
        any), or None."""
        c = self._cmp_session()
        if c is None:
            return None
        g = getattr(e, "group", "") or ""
        hits = [x for x in getattr(c, "entities", []) or [] if x.name == e.name]
        return next((x for x in hits if (getattr(x, "group", "") or "") == g),
                    hits[0] if hits else None)

    def _cmp_text(self, plan, e) -> Tuple[str, bool]:
        """``(what the comparison has, differs)`` for table ``e`` as ``plan``
        writes it."""
        ce = self._cmp_entity(e)
        if ce is None:
            return "not in the comparison", False
        try:
            vals = [list(r) for r in e.values()]
            got = plan.cells.get(id(e))
            for (r, c), v in (got[1].items() if got else ()):
                vals[r][c] = v
            other = ce.values()
            if len(vals) != len(other) or any(len(a) != len(b) for a, b in zip(vals, other)):
                return "another layout", True
            xs = [float(v) for v in e.x_values()] if getattr(e, "x", None) is not None else []
            for pe, ax, i, v in plan.axes:
                if pe is e and ax is getattr(e, "x", None) and i < len(xs):
                    xs[i] = v
            cx = [float(v) for v in ce.x_values()] if getattr(ce, "x", None) is not None else []
            n, worst = 0, 0.0
            for a_row, b_row in zip(vals, other):
                for a, b in zip(a_row, b_row):
                    if I._differs(e, b, a, 1e-6):
                        n += 1
                        worst = max(worst, abs(a - b))
            axes = len(xs) == len(cx) and any(abs(a - b) > 1e-3 for a, b in zip(xs, cx))
        except Exception as ex:                                # noqa: BLE001
            return "could not be compared: %s" % ex, False
        if not n and not axes:
            return "the same", False
        bits = []
        if n:
            bits.append("%d cell%s differ%s (up to %.3g)" % (
                n, "" if n == 1 else "s", "s" if n == 1 else "", worst))
        if axes:
            bits.append("its breakpoints differ")
        return "; ".join(bits), True

    @staticmethod
    def _ident_text(r: "I.DatasetMatch") -> str:
        """One dataset's identification, as rich text."""
        if r.error:
            return _esc(r.error)
        top = r.top
        if top is None:
            return "No preset to compare with."
        if top.exact:
            head = "<b>%s</b> — the same calibration" % _esc(top.preset.name)
        elif top.same:
            head = ("<b>%s</b> — the same hardware; the calibrations differ in places "
                    "(up to %.1f° of duration)" % (_esc(top.preset.name), top.worst))
        else:
            head = ("Closest: <b>%s</b>, %.0f %% apart on average (up to %.1f° of "
                    "duration). Not the same calibration: modified, or hardware "
                    "without a preset." % (_esc(top.preset.name), top.mean * 100, top.worst))
        rest = ", ".join("%s (%.0f %%)" % (_esc(m.preset.name), m.mean * 100)
                         for m in r.matches[1:])
        mpd = r.char.mg_per_degree(10.0, 3000.0, 45.0)
        return (head + "<br>%.2f mg per degree at 10° BTDC, 3000 rpm, 45 mg." % mpd
                + ("<br>Next: " + rest if rest else ""))

    def _calibrated_for(self, p: I.Preset) -> bool:
        """Every dataset to change is set up for ``p``'s hardware."""
        chosen = {id(m) for m in self._chosen_models()}
        rs = [r for r in self.idents if id(r.model) in chosen] or self.idents
        return bool(rs) and all(r.top is not None and r.top.preset.id == p.id and r.top.same
                                for r in rs)

    # ---------------------------------------------------------------- project
    def _ref(self):
        return getattr(self.ws, "project_ref", None)

    def _show_fitted(self):
        ref = self._ref()
        self.fitted_lbl.setVisible(ref is not None)
        self.fitted.setVisible(ref is not None)
        if ref is None:
            return
        try:
            from .project import hardware as H
            proj, sid = ref.doc.project, ref.sid
            cur = H.current(proj, sid, I.HW_KIND)
            dirty = bool(getattr(self.ws, "is_dirty", lambda: False)())
            cal = H.for_checkpoint(proj, sid, ref.branch.head, I.HW_KIND, unsaved=dirty)
        except Exception:                                      # noqa: BLE001
            self.fitted.setText("")
            return
        if not cur:
            self.fitted.setText("<i>No injector change recorded in the project</i> (Apply "
                                "records the hardware you fit).")
            return
        txt = _esc(H.describe(cur[0], proj, sid))
        if not cal:
            txt += ("<br><span style='color:#B26A00'>This version is not set up for it "
                    "yet.</span>")
        elif not cur[0].same_parts(cal[0]):
            txt += ("<br><span style='color:#B26A00'>This version is set up for %s.</span>"
                    % _esc(cal[0].summary))
        self.fitted.setText(txt)

    def _project_changed(self):
        ref = self._ref()
        if ref is None:
            return
        try:
            from . import gui_project as _gp
            win = self.ws.win
            for t in ref.doc.tabs(win):
                win._workspace_changed(t)
            _gp.refresh_panel(win)
            _gp.autosave_soon(win, ref.doc)
        except Exception:                                      # noqa: BLE001
            pass

    def _record_setup(self) -> str:
        """Apply wrote a preset's calibration: record it as fitted, pending on
        the branch head (the maps are in the tab's unsaved edits)."""
        ref = self._ref()
        p = self.current_preset()
        if ref is None or p is None or self.from_logs() or not self.record_cb.isChecked():
            return ""
        from .project import hardware as H
        summary, parts = I.hardware_record(p, self.r_body.isChecked())
        try:
            # record_setup may complete an earlier Record as fitted... record
            # instead of adding one: undo then puts that record back as it was
            before = {r.id: r.to_dict() for r in H.records(ref.doc.project, ref.sid)}
            rec = H.record_setup(ref.doc.project, ref.sid, ref.bid, I.HW_KIND, summary, parts)
        except Exception:                                      # noqa: BLE001
            return ""
        if rec is None:
            return ""
        self._hw_written.append((rec.to_dict(), self.session.undo_stack.top(),
                                 before.get(rec.id)))
        self._project_changed()
        return rec.summary

    def _record_fitted(self):
        """Record as fitted...: the chosen preset, with the day it went in,
        without writing anything to the file."""
        ref = self._ref()
        p = self.current_preset()
        if ref is None or p is None:
            return
        from .gui_hardware import ask_fitted
        from .project import hardware as H
        summary, parts = I.hardware_record(p, self.r_body.isChecked())
        cur = H.current(ref.doc.project, ref.sid, I.HW_KIND)
        if cur and cur[0].parts == parts:
            QtWidgets.QMessageBox.information(
                self, "Injector setup", "The project already records %s as fitted (File > "
                "Project information... to change the date)." % summary)
            return
        cal = self._calibrated_for(p)
        since = ask_fitted(self, "Injector setup", summary, cal)
        if since is None:
            return
        try:
            H.record_fitted(ref.doc.project, ref.sid, ref.bid, I.HW_KIND, summary, parts,
                            calibrated=cal,
                            unsaved=bool(getattr(self.ws, "is_dirty", lambda: False)()),
                            since=since)
        except Exception as ex:                                # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Injector setup", "Not recorded: %s" % ex)
            return
        self._project_changed()
        self._show_fitted()

    def _sync_record(self):
        """Each Apply's record follows its own undo step: undoing it takes
        the record back (or puts back the Record as fitted... record it
        completed, as it was), redoing it puts it back."""
        if not self._hw_written:
            return
        ref = self._ref()
        if ref is None:
            return
        stack = self.session.undo_stack
        done = {id(c) for c in getattr(stack, "_done", [])}
        undone = {id(c) for c in getattr(stack, "_undone", [])}
        # per record id, oldest Apply first: the latest one still done decides;
        # with none done, the earliest undone one puts back what was before it
        want: Dict[str, Tuple[bool, Optional[dict]]] = {}
        known: Dict[str, List[Optional[dict]]] = {}
        for d, cmd, prev in self._hw_written:
            rid = d["id"]
            known.setdefault(rid, []).extend((d, prev))
            if id(cmd) in done:
                want[rid] = (True, d)
            elif id(cmd) in undone and rid not in want:
                want[rid] = (False, prev)
            # neither: dropped from the history, or its redo was discarded by
            # a new edit -- the record stays as it is now
        changed = False
        try:
            from .project import hardware as H
            proj = ref.doc.project
            for rid, (applied, target) in want.items():
                cur = H.get(proj, ref.sid, rid)
                cur = cur.to_dict() if cur is not None else None
                if cur == target:
                    continue
                if applied and cur is not None and cur not in known[rid]:
                    continue                    # changed since by hand: left alone
                self._put_record(proj, ref.sid, rid, target)
                changed = True
        except Exception:                                      # noqa: BLE001
            return
        if changed:
            self._project_changed()

    @staticmethod
    def _put_record(proj, sid: str, rid: str, rec: Optional[dict]) -> None:
        """Record ``rid`` set to ``rec`` in its place (appended when it is not
        there), or removed (``rec`` None)."""
        from .project import hardware as H
        st = proj.set(sid)
        old = list(st.extra.get(H.KEY) or [])
        hit = [i for i, x in enumerate(old) if isinstance(x, dict) and x.get("id") == rid]
        if rec is None:
            new = [x for i, x in enumerate(old) if i not in hit]
        elif hit:
            new = [dict(rec) if i == hit[0] else x for i, x in enumerate(old)
                   if i == hit[0] or i not in hit]
        else:
            new = old + [dict(rec)]
        st.extra[H.KEY] = new
        proj._touch()

    # ---------------------------------------------------------------- planning
    def _schedule(self, *_a):
        self._timer.start()

    def _chosen_models(self):
        if not self.ds_boxes:
            return list(self.models)
        return [m for m, cb in zip(self.models, self.ds_boxes) if cb.isChecked()]

    def _timing_changed(self, *_a):
        # the start maps follow only a timing that moves the start
        self.start_cb.setEnabled(self.timing.currentData() != I.KEEP_SOI)

    def options(self) -> I.FitOptions:
        copies = not self.copies_cb.isHidden() and self.copies_cb.isChecked()
        start = self.start_cb.isChecked()
        if self.from_logs():
            return I.FitOptions(body=False, timing=self.timing.currentData(), selector=True,
                                copies=copies, start=start)
        return I.FitOptions(body=self.r_body.isChecked(),
                            timing=self.timing.currentData(),
                            selector=self.sel_cb.isChecked(),
                            copies=copies, start=start)

    def _choices(self) -> tuple:
        """Everything chosen in the window that the plan depends on."""
        o = self.options()
        return (self.from_logs(), self.preset.currentData(), o.body, o.timing, o.selector,
                o.copies, o.start, tuple(cb.isChecked() for cb in self.ds_boxes),
                tuple(self._log_paths(self.after_list)),
                tuple(self._log_paths(self.before_list)))

    def _unchanged_since_apply(self) -> bool:
        """Nothing changed since the last Apply: not the file (no edit or
        undo since) and not a choice here."""
        if self._applied_at is None:
            return False
        try:
            rev = self.session.undo_stack.revision
        except Exception:                                      # noqa: BLE001
            return False
        return self._applied_at == (rev, self._choices())

    def label(self) -> str:
        pol = {I.KEEP_CENTRE: "centre kept", I.KEEP_EOI: "end kept",
               I.KEEP_SOI: "start kept"}[self.timing.currentData()]
        if self.from_logs():
            return "Injector setup: duration from logs (%s)" % pol
        p = self.current_preset()
        what = "complete injectors" if self.r_body.isChecked() else "nozzles only"
        pol = {I.KEEP_CENTRE: "centre kept", I.KEEP_EOI: "end kept",
               I.KEEP_SOI: "start kept"}[self.timing.currentData()]
        return "Injector setup: %s (%s, %s)" % (p.name if p else "?", what, pol)

    def _replan(self):
        self.plans = []
        self.changes.setRowCount(0)
        self.findings.clear()
        if self.from_logs():
            self._replan_logs()
            self._show_plans()
            return
        p = self.current_preset()
        models = self._chosen_models()
        if p is None or not models:
            self.chart.set_data({}, {})
            self._buttons()
            return
        char = self.cat.char_of(p)
        if char is None:
            self._finding("error", "This preset's base (%s) is not installed." % p.base)
            self._buttons()
            return
        opts = self.options()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            for m in models:
                try:
                    plan = I.plan_fit(self.session, m, char, opts,
                                      self.cat.electrical_of(p), self.cat.extras_of(p),
                                      self.platform, self.label())
                except Exception as ex:                        # noqa: BLE001
                    self._finding("error", "%s: %s" % (m.group or "Main", ex))
                    continue
                self.plans.append(plan)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if p.confidence != I.EXACT:
            self._finding("warning", "Not a factory calibration (%s): log the car "
                          "after fitting and refine the duration maps from the logs."
                          % I.CONFIDENCE_LABELS[p.confidence])
        if p.kind == I.AFTERMARKET and p.flow >= 2.2:
            self._finding("warning", "A very large nozzle lowers the injection pressure: "
                          "ignition may come later. Check the end of injection and the "
                          "exhaust temperature after fitting.")
        if p.platform and p.platform != getattr(self.session, "platform", ""):
            self._finding("note", "This preset comes from an %s file; the duration set "
                          "carries across, and only the solenoid parameters both "
                          "platforms share." % p.platform.upper())
        self._show_plans()

    def _replan_logs(self):
        from . import injlearn as L
        self.learned = None
        after = self._log_paths(self.after_list)
        before = self._log_paths(self.before_list)
        models = self._chosen_models()
        if not after or not models:
            self.log_status.setText("Add at least one log taken with the new hardware.")
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            try:
                la, lb = self._logs(after), self._logs(before)
            except Exception as ex:                            # noqa: BLE001
                self._finding("error", "Could not read the logs: %s" % ex)
                self.log_status.setText("")
                return
            labels = [m.label for m in models]
            wa, notes_a = self._log_use(la, labels)
            wb, notes_b = self._log_use(lb, labels)
            self._show_log_use(self.after_list, la, wa)
            self._show_log_use(self.before_list, lb, wb)
            for n in dict.fromkeys(notes_a + notes_b):
                self._finding("note", n)
            reps = []
            for m in models:
                ra = [r for (_p, _i, r), (ds, _h) in zip(la, wa) if m.label in ds]
                rb = [r for (_p, _i, r), (ds, _h) in zip(lb, wb) if m.label in ds]
                if not ra:
                    self._finding("note", "%s: no log taken with the new hardware was "
                                  "recorded on this dataset (their idents name another); "
                                  "left as it is." % m.label)
                    continue
                try:
                    rep = L.learn(self.session, m, after, before, timing=self.timing.currentData(),
                                  runs_after=ra, runs_before=rb)
                except Exception as ex:                        # noqa: BLE001
                    self._finding("error", "%s: %s" % (m.group or "Main", ex))
                    continue
                reps.append(rep)
                if rep.plan is not None:
                    rep.plan.label = self.label()
                    self.plans.append(rep.plan)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if not reps:
            return
        rep = reps[0]
        self.learned = rep
        self.log_status.setText(
            "%d log row%s measured by the lambda probe, %d point%s matched before and "
            "after." % (rep.used.get("lambda_rows", 0),
                        "" if rep.used.get("lambda_rows", 0) == 1 else "s",
                        rep.used.get("same_work_points", 0),
                        "" if rep.used.get("same_work_points", 0) == 1 else "s"))
        for s in rep.samples:
            if s.source == "same-work":
                self._finding("note", "%s (%.0f rpm, %.1f mg asked): the new hardware "
                              "delivers %+.0f %%." % (s.label[:1].upper() + s.label[1:],
                                                      s.rpm, s.q, (s.k - 1) * 100))
        if rep.points:
            ks = [k for _n, _q, k, _w in rep.points]
            self._finding("note", "Measured at %d breakpoint%s of the duration maps: the "
                          "hardware delivers %+.0f %% on average (%+.0f to %+.0f %%); %d "
                          "breakpoint%s had too little data and stay as they are." % (
                              len(ks), "" if len(ks) == 1 else "s",
                              (sum(ks) / len(ks) - 1) * 100, (min(ks) - 1) * 100,
                              (max(ks) - 1) * 100, rep.unmeasured,
                              "" if rep.unmeasured == 1 else "s"))
        for n in rep.notes:
            self._finding("warning" if "held" in n else "note", n)

    def _show_plans(self):
        for plan in self.plans:
            pre = (plan.model.group + ": ") if len(self.plans) > 1 and plan.model.group else ""
            for ch in plan.changes:
                r = self.changes.rowCount()
                self.changes.insertRow(r)
                it = QtWidgets.QTableWidgetItem(pre + ch.entity.name)
                it.setData(QtCore.Qt.ItemDataRole.UserRole, ch.entity)
                got = plan.cells.get(id(ch.entity))
                if got:
                    it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, sorted(got[1]))
                self.changes.setItem(r, 0, it)
                self.changes.setItem(r, 1, QtWidgets.QTableWidgetItem(ch.label))
                self.changes.setItem(r, 2, QtWidgets.QTableWidgetItem(str(ch.cells)))
                self.changes.setItem(r, 3, QtWidgets.QTableWidgetItem(
                    "%.3g" % ch.max_delta if ch.max_delta else "—"))
                if self.cmp_models:
                    txt, differs = self._cmp_text(plan, ch.entity)
                    ci = QtWidgets.QTableWidgetItem(txt)
                    if differs:
                        from .gui_cellmarks import mark_differs
                        mark_differs(ci)
                    self.changes.setItem(r, 4, ci)
            for fd in plan.findings:
                self._finding(fd.level, pre + fd.text)
        self.changes.resizeColumnsToContents()
        self.changes.setColumnHidden(4, not self._cmp_shown())
        self._chart()
        if self.plans and not any(pl.n_cells for pl in self.plans):
            self._finding("note", "Nothing to change: the logs agree with the maps."
                          if self.from_logs() else
                          "Nothing to change: the file is already set up for "
                          "this hardware.")
        self._buttons()

    def _finding(self, level, text):
        icon = {"error": QtWidgets.QStyle.StandardPixmap.SP_MessageBoxCritical,
                "warning": QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning}.get(
            level, QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation)
        it = QtWidgets.QListWidgetItem(self.style().standardIcon(icon), text)
        it.setData(QtCore.Qt.ItemDataRole.UserRole, level)
        self.findings.addItem(it)

    def _buttons(self):
        ok = bool(self.plans) and any(pl.n_cells for pl in self.plans) and \
            not any(pl.errors for pl in self.plans) and \
            not getattr(self.ws, "view_only", None)
        self.apply_btn.setEnabled(ok)
        # nothing to write ("Nothing to change"): OK only closes
        self.ok_btn.setEnabled(ok or not any(pl.n_cells for pl in self.plans))

    # ---------------------------------------------------------------- actions
    def _apply(self) -> bool:
        if self._timer.isActive():          # a choice just made: plan it first
            self._timer.stop()
            self._replan()
        if not self.plans or not any(pl.n_cells for pl in self.plans):
            return True
        n_tab = sum(len(pl.changes) for pl in self.plans)
        n_val = sum(pl.n_cells for pl in self.plans)
        warn = [f.text for pl in self.plans for f in pl.findings if f.level == "warning"]
        text = ("%s\n\nWrite %d value%s in %d table%s as one undo step?" %
                (self.label(), n_val, "" if n_val == 1 else "s", n_tab,
                 "" if n_tab == 1 else "s"))
        if warn:
            text += "\n\nWarnings:\n" + "\n".join("• " + w for w in warn)
        if QtWidgets.QMessageBox.question(
                self, "Injector setup", text,
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return False
        try:
            n = I.apply_fit(self.session, self.plans, self.label(), self.options().copies)
        except Exception as ex:                                # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Injector setup",
                                          "Nothing was changed: %s" % ex)
            return False
        self._after_apply()
        recorded = self._record_setup()
        try:
            self.ws.window().statusBar().showMessage(
                "%s: %d change%s. Edit > Undo takes them all back.%s"
                % (self.label(), n, "" if n == 1 else "s",
                   " Recorded as fitted in the project." if recorded else ""), 8000)
        except Exception:
            pass
        if self.from_logs():
            # these logs were taken with the maps just corrected: applying them
            # again would correct twice
            self.after_list.clear()
            self.log_status.setText("Applied. Drive and log again with this file, then add "
                                    "the new logs to check what is left.")
        self._identify()
        self._schedule()
        try:
            self._applied_at = (self.session.undo_stack.revision, self._choices())
        except Exception:                                      # noqa: BLE001
            self._applied_at = None
        return True

    def _ok(self):
        """OK: write what is listed and close -- only close when Apply has
        written it and nothing changed since."""
        if self._unchanged_since_apply() or self._apply():
            self.accept()

    def _after_apply(self):
        ws = self.ws
        try:
            ws._changed_cache = None
        except Exception:
            pass
        try:
            ws.model.refresh()
        except Exception:
            pass
        for meth in ("_populate", "_status", "_refresh_tools"):
            try:
                getattr(ws, meth)()
            except Exception:
                pass

    def refresh_from_session(self):
        """An edit made elsewhere (undo, the grid, another tool)."""
        self._sync_record()
        self._identify()
        self._schedule()

    def _goto(self, r, _c):
        """Open the table in its tab, the cells this will write marked, and
        bring the main window forward (as End of injection does)."""
        it = self.changes.item(r, 0)
        e = it.data(QtCore.Qt.ItemDataRole.UserRole) if it else None
        if e is None:
            return
        cells = it.data(QtCore.Qt.ItemDataRole.UserRole + 1) or []
        ws = self.ws
        try:
            win = ws.window()
            if getattr(win, "current", ws) is not ws and hasattr(win, "show_workspace"):
                win.show_workspace(ws)
            ws.show_entity(e)
            model = getattr(ws, "model", None)
            if model is not None and getattr(model, "entity", None) is e and cells:
                model.set_highlight([("z", rr, cc) for rr, cc in cells])
                idx = model.index_of("z", *cells[0])
                if idx.isValid():
                    ws.table.setCurrentIndex(idx)
                    ws.table.scrollTo(
                        idx, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
            win.raise_()
            win.activateWindow()
            self.goto_status.setText("Opened %s in the main window%s." % (
                e.name, ", the cells this changes marked" if cells else ""))
        except Exception as ex:                                # noqa: BLE001 - never silent
            self.goto_status.setText("Could not open %s: %s" % (e.name, ex))

    def _open_eoi(self):
        try:
            from . import gui_eoi
            gui_eoi.open_for(self.ws)
        except Exception as ex:                                # noqa: BLE001
            QtWidgets.QMessageBox.information(self, "Injector setup", str(ex))

    # ---------------------------------------------------------------- your presets
    def _labels(self) -> List[str]:
        return [getattr(m, "label", "") or getattr(m, "group", "") or "Main"
                for m in self.models]

    def _name_taken(self, name: str, but: str = "") -> Optional[I.Preset]:
        """Your preset other than ``but`` that has ``name`` (or its id)."""
        pid = _user_id(name)
        low = name.strip().lower()
        return next((u for u in self.user if u.id != but and
                     (u.id == pid or u.name.strip().lower() == low)), None)

    def _from_file(self, title: str, pid: str, v: dict) -> Optional[I.Preset]:
        """A preset of the hardware dataset ``v["dataset"]`` is set up for."""
        m = self.models[max(0, min(len(self.models) - 1, v.get("dataset", 0)))]
        try:
            return I.preset_from_session(
                self.session, m, self.platform, pid, v["name"],
                source=getattr(self.session.binfile, "name", ""), notes=v["notes"],
                body=v["body"], nozzle=v["nozzle"], cam=v["cam"])
        except Exception as ex:                                # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, title, str(ex))
            return None

    def _store_user(self, presets: List[I.Preset], title: str,
                    select: Optional[str]) -> bool:
        try:
            _save_user_presets(presets)
        except Exception as ex:                                # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, title, str(ex))
            return False
        self.user = list(presets)
        self.cat = I.catalogue(self.user)
        self._fill_presets(select)
        self._identify()
        return True

    def _save_preset(self):
        if not self.models:
            return
        title = "Save as preset"
        dlg = InjectorPresetDialog(self, title, self._labels(),
                           name=getattr(self.session.binfile, "name", "") or "My injectors")
        if not dlg.exec():
            return
        v = dlg.values()
        pid = _user_id(v["name"])
        old = self._name_taken(v["name"])
        if old is not None:
            if QtWidgets.QMessageBox.question(
                    self, title, "You already have a preset named %s. Replace it?" % old.name,
                    QtWidgets.QMessageBox.StandardButton.Yes
                    | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No
            ) != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            pid = old.id                    # it keeps its id: project records name it
        pr = self._from_file(title, pid, v)
        if pr is None:
            return
        self._store_user([u for u in self.user if u.id != pid] + [pr], title, pid)

    def _own_preset(self) -> Optional[I.Preset]:
        p = self.current_preset()
        if p is None or p.kind != I.USER:
            return None
        return next((u for u in self.user if u.id == p.id), None)

    def _edit_preset(self):
        p = self._own_preset()
        if p is None:
            return
        title = "Edit preset"
        dlg = InjectorPresetDialog(self, title, self._labels(), preset=p)
        if not dlg.exec():
            return
        v = dlg.values()
        other = self._name_taken(v["name"], but=p.id)
        if other is not None:
            QtWidgets.QMessageBox.information(
                self, title, "You already have a preset named %s. Choose another name."
                % other.name)
            return
        if v["retake"]:
            pr = self._from_file(title, p.id, v)
            if pr is None:
                return
        else:
            import dataclasses
            pr = dataclasses.replace(p, name=v["name"], notes=v["notes"], body=v["body"],
                                     nozzle=v["nozzle"], cam=v["cam"])
        self._store_user([pr if u.id == p.id else u for u in self.user], title, p.id)

    def _rename_preset(self):
        p = self._own_preset()
        if p is None:
            return
        title = "Rename preset"
        name, ok = QtWidgets.QInputDialog.getText(self, title, "Name:", text=p.name)
        name = (name or "").strip()
        if not ok or not name or name == p.name:
            return
        other = self._name_taken(name, but=p.id)
        if other is not None:
            QtWidgets.QMessageBox.information(
                self, title, "You already have a preset named %s. Choose another name."
                % other.name)
            return
        import dataclasses
        self._store_user([dataclasses.replace(u, name=name) if u.id == p.id else u
                          for u in self.user], title, p.id)

    def _delete_preset(self):
        p = self._own_preset()
        if p is None:
            return
        title = "Delete preset"
        if QtWidgets.QMessageBox.question(
                self, title, "Delete your preset %s? This cannot be undone." % p.name,
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(-1)              # not kept: the first entry after
        self.preset.blockSignals(False)
        self._store_user([u for u in self.user if u.id != p.id], title, None)

    def _help(self):
        try:
            from .gui_help import show_help
            show_help(self, context=self)
        except Exception:
            pass


class InjectorPresetDialog(QtWidgets.QDialog):
    """Save as preset... and Edit... of one of your presets: the name, the
    parts and notes, and (saving, or editing with "take the calibration from
    this file again") the dataset whose calibration it keeps."""

    def __init__(self, parent, title: str, datasets: List[str], name: str = "",
                 preset: Optional[I.Preset] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        f = QtWidgets.QFormLayout(self)
        p = preset
        self.name = QtWidgets.QLineEdit(p.name if p else name)
        f.addRow("Name:", self.name)
        self.retake = None
        if p is not None:
            self.retake = QtWidgets.QCheckBox("Take the calibration from this file again")
            self.retake.setToolTip("Replace the preset's duration maps and solenoid "
                                   "calibration with what this file is set up for now")
            f.addRow("", self.retake)
        self.dataset = QtWidgets.QComboBox()
        self.dataset.addItems(datasets)
        if len(datasets) > 1:
            f.addRow("Dataset:", self.dataset)
            self.dataset.setToolTip("The dataset whose calibration the preset keeps")
        else:
            self.dataset.hide()
        if self.retake is not None:
            self.dataset.setEnabled(False)
            self.retake.toggled.connect(self.dataset.setEnabled)
        self.cam = QtWidgets.QLineEdit(p.cam if p else "")
        self.cam.setPlaceholderText("optional, e.g. the engine code")
        f.addRow("Camshaft:", self.cam)
        self.body = QtWidgets.QLineEdit(p.body if p else "")
        self.body.setPlaceholderText("optional, e.g. the injector part number")
        f.addRow("Injector body:", self.body)
        self.nozzle = QtWidgets.QLineEdit(p.nozzle if p else "")
        self.nozzle.setPlaceholderText("optional, e.g. DSLA150P1043")
        f.addRow("Nozzle:", self.nozzle)
        self.notes = QtWidgets.QPlainTextEdit(p.notes if p else "")
        self.notes.setFixedHeight(70)
        f.addRow("Notes:", self.notes)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.name.textChanged.connect(lambda t: self.ok.setEnabled(bool(t.strip())))
        self.ok.setEnabled(bool(self.name.text().strip()))
        self.resize(420, self.sizeHint().height())

    def values(self) -> dict:
        return {"name": self.name.text().strip(), "cam": self.cam.text().strip(),
                "body": self.body.text().strip(), "nozzle": self.nozzle.text().strip(),
                "notes": self.notes.toPlainText().strip(),
                "dataset": max(0, self.dataset.currentIndex()),
                "retake": self.retake is None or self.retake.isChecked()}


def _user_id(name: str) -> str:
    """The id a preset of yours named ``name`` is saved under."""
    return "user-" + (re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "preset")


def _count_label(n: int) -> str:
    return "Both datasets" if n == 2 else "All %s datasets" % (
        {3: "three", 4: "four", 5: "five", 6: "six"}.get(n, str(n)))


def _esc(t: str) -> str:
    import html
    return html.escape(t)


__all__ = ["InjectorDialog", "InjectorPresetDialog", "FlowChart", "availability", "open_for", "user_presets"]

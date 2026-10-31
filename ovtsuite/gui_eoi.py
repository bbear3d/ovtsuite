# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The End of injection view (:mod:`ovtsuite.eoi`).

**Tools > End of injection...**, or **End of injection...** in the
right-click menu of a start-of-injection or injection-duration table. A tool
window of the tab, so it stays open beside the grid and follows every edit
and undo.

What it shows, over the start-of-injection map's own breakpoints:

* **EOI** -- end of injection in degrees ATDC (negative = before TDC), the
  convention Tune from logs uses;
* **SOI** -- the start the ECU uses there, degrees BTDC;
* **Duration** -- the duration the ECU uses: the duration tables blended by
  the selector, as the ECU does;
* **Table no.** -- which duration table(s) it reads, with the blend.

Typing a value edits what you see: an EOI moves the SOI (the fuelling stays
calibrated), an SOI moves the SOI tables, a duration moves the duration
tables the ECU blends there (that is fuel quantity on a PD engine, so it asks
first). **View** also shows one duration table on its own axes, shaded by how
much the ECU uses each cell at the chosen coolant, for editing that table.

The grid follows the app's table conventions (:mod:`ovtsuite.gui_cellmarks`):
alternating rows and the shared delegate, so a selected cell shows in the
solid accent, and right-click gives Copy, Paste (typed values, as typing
them: one undo step), Set target EOI for the selection and the Go to
buttons. With a comparison loaded, a second read-only grid sits below the
first (:mod:`ovtsuite.gui_eoi_compare`); a cell that differs has the orange
outline in both, over the band or shade it keeps.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import eoi as _eoi
from . import compare_delta as _cd
from .gui_cellmarks import CellDelegate, selection_tsv
from .gui_eoi_edit import EoiEditing, heat_color, remembered
from .gui_eoi_surface import SHOWN_KEY, WIDTH as SURF_W, EoiSurface

MODES = (("eoi", "EOI"), ("soi", "SOI"), ("dur", "Duration"),
         ("idx", "Table no."))

#: EOI bands (deg ATDC): before TDC, then later and later
_EOI_BANDS = ((0.0, "#E1F5EE"), (4.0, "#F1EFE8"), (8.0, "#FAEEDA"),
              (12.0, "#FAC775"), (1e9, "#F09595"))
#: light sequential shades for the SOI and Duration views, low to high. Every
#: view gives its cells a light background, so the dark text reads the same
#: under a light or a dark application theme.
_SEQ = ("#EEF3FA", "#DDE8F6", "#CADCF0", "#B6CEEA", "#A1C0E3")
_IDX = ("#EEEDFE", "#CECBF6", "#AFA9EC", "#E1F5EE", "#9FE1CB", "#5DCAA5",
        "#F1EFE8", "#D3D1C7")
_INK = QtGui.QColor("#2C2C2A")
_UNUSED_BG = QtGui.QColor("#ECECEC")
_UNUSED_INK = QtGui.QColor("#8A8A8A")
_PART_BG = QtGui.QColor("#FAEEDA")
_USED_BG = QtGui.QColor("#FFFFFF")


def _g():
    from . import gui
    return gui


_COND_TIPS = {
    "air": "Intake air temperature: picks the air-temperature correction the "
           "ECU adds to the start of injection (none at 20 °C on most stock files).",
    "baro": "Atmospheric pressure: picks the altitude correction the ECU adds "
            "to the start of injection (none at sea level).",
    "q_start": "The quantity injected while starting, which the duration is "
               "read at (take it from a log of a start).",
    "fuel": "Fuel temperature: the ECU corrects the quantity the duration maps "
            "read for it. Reference = the temperature the maps were measured "
            "at, no correction.",
}


def _num(v: float, nd: int = 2) -> str:
    t = ("%.*f" % (nd, v)).rstrip("0").rstrip(".") if nd else "%d" % round(v)
    return "0" if t in ("-0", "") else t


def cell_value(mode: str, cell: "_eoi.Cell") -> Tuple[float, str]:
    """``(value, text)`` of a grid cell in the shown ``mode``."""
    if mode == "eoi":
        return cell.eoi, _num(cell.eoi, 2)
    if mode == "soi":
        return cell.soi_btdc, _num(cell.soi_btdc, 2)
    if mode == "dur":
        return cell.duration, _num(cell.duration, 2)
    return cell.index, "%.2f" % cell.index


def parse_paste(text: str) -> List[List[Optional[float]]]:
    """Clipboard text as rows of numbers (None where a cell is not one). A
    copy of the whole table (Copy table: a header line and a label column)
    loses both."""
    rows = [[c.strip() for c in ln.split("\t")]
            for ln in text.replace("\r", "").split("\n") if ln.strip()]

    def num(t):
        try:
            return float(t.replace(",", ".").replace("\u00b0", "").strip())
        except ValueError:
            return None
    out = [[num(c) for c in r] for r in rows]
    if out and out[0] and out[0][0] is None and len(out) > 1:
        out = [r[1:] for r in out[1:]]
    return out


def availability(ws) -> Optional[str]:
    """None when the view can open for tab ``ws``, else why not."""
    s = getattr(ws, "primary", None)
    if s is None:
        return "No file open"
    if getattr(s, "support", "full") != "full":
        return "Needs the platform's module and a definition for this file"
    if not _eoi.models(s):
        return ("Only unit-injector (PD) EDC15P and EDC16 software calculates "
                "an injection duration")
    return None


def involved(ws, e) -> bool:
    """Whether ``e`` is one of the tables the view works from."""
    s = getattr(ws, "primary", None)
    if s is None or e is None or getattr(s, "platform", "") not in ("edc15", "edc16"):
        return False
    for m in _eoi.models(s):
        ts = [m.soi, m.duration] + [x.set for x in m.soi_sets] + \
            [c.set for c in m.corrections]
        for t in [x for s_ in ts for x in s_.tables + [s_.selector]]:
            if t is e:
                return True
    return False


def open_for(ws, entity=None):
    """Open (or raise) tab ``ws``'s End of injection window."""
    why = availability(ws)
    if why:
        QtWidgets.QMessageBox.information(ws, "End of injection", why)
        return None
    dlg = ws.open_tool("eoi", lambda: EoiDialog(ws, entity))
    if dlg is not None and entity is not None:
        dlg.focus_entity(entity)
    return dlg


class EoiDialog(QtWidgets.QDialog):
    """Start, duration and end of injection over the SOI map's breakpoints."""

    def __init__(self, ws, entity=None, parent=None):
        super().__init__(parent or ws)
        self.ws = ws
        self.session = ws.primary
        self.models = _eoi.models(self.session)
        self._calcs: Dict[int, _eoi.Calculator] = {}
        self._filling = False
        self._asked_quantity = False
        self._sized_for_comparison = False
        self._prim_vals: Dict[tuple, float] = {}
        self._cmp_vals: Dict[tuple, float] = {}
        self._last_plan = None
        self.edit = EoiEditing(self)
        self.setWindowTitle("End of injection")
        try:
            self.setWindowIcon(_g().app_icon())
        except Exception:
            pass
        v = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        self.ds_lbl = QtWidgets.QLabel("Dataset")
        self.ds = QtWidgets.QComboBox()
        for m in self.models:
            self.ds.addItem(m.label)
        self.ds_lbl.setVisible(len(self.models) > 1)
        self.ds.setVisible(len(self.models) > 1)
        top.addWidget(self.ds_lbl)
        top.addWidget(self.ds)
        top.addWidget(QtWidgets.QLabel("Coolant"))
        self.coolant = QtWidgets.QDoubleSpinBox()
        self.coolant.setRange(-40.0, 130.0)
        self.coolant.setSingleStep(5.0)
        self.coolant.setDecimals(0)
        self.coolant.setSuffix(" °C")
        self.coolant.setValue(self.models[0].coolant_warm if self.models else 90)
        self.coolant.setToolTip("The coolant temperature picks which start-of-"
                                "injection tables the ECU blends.")
        top.addWidget(self.coolant)
        # the SOI table set: the base set, or one the ECU uses instead in
        # some running state (EDC16 dynamic SOI, gear maps)
        self.set_lbl = QtWidgets.QLabel("SOI set")
        self.soi_box = QtWidgets.QComboBox()
        self.soi_box.setToolTip("Which start-of-injection tables to work from: "
                                "the base set, or one the ECU uses instead in "
                                "some running state.")
        top.addWidget(self.set_lbl)
        top.addWidget(self.soi_box)
        # the conditions the model's SOI corrections depend on (air
        # temperature, air pressure); shown for a model that has them
        self.cond: Dict[str, tuple] = {}
        for key, (label, units, dflt, lo, hi) in _eoi.CONDITIONS.items():
            lbl = QtWidgets.QLabel(label)
            sp = QtWidgets.QDoubleSpinBox()
            sp.setDecimals(0)
            sp.setSingleStep(5.0 if units == "°C" else 10.0)
            sp.setSuffix(" " + units)
            if dflt is None:
                # one step below the range: "Reference", the ECU's own
                # reference temperature (no correction)
                sp.setRange(lo - 1.0, hi)
                sp.setSpecialValueText("Reference")
                sp.setValue(lo - 1.0)
            else:
                sp.setRange(lo, hi)
                sp.setValue(dflt)
            sp.setToolTip(_COND_TIPS.get(key, label))
            sp.valueChanged.connect(lambda _v: self._conditions_changed())
            top.addWidget(lbl)
            top.addWidget(sp)
            self.cond[key] = (lbl, sp)
        top.addWidget(self.edit.reset)
        top.addStretch(1)
        v.addLayout(top)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Show"))
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for i, (key, text) in enumerate(MODES):
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setProperty("mode", key)
            self.mode_group.addButton(b, i)
            top.addWidget(b)
        top.addSpacing(12)
        top.addWidget(QtWidgets.QLabel("View"))
        self.view = QtWidgets.QComboBox()
        self.view.setMinimumContentsLength(10)
        self.view.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        top.addWidget(self.view, 1)
        from .gui_eoi_compare import EoiCompare
        self.cmpv = EoiCompare(self)
        top.addSpacing(12)
        top.addWidget(self.cmpv.check)
        from . import gui_compare_delta as _gcd
        self.diff_btn = QtWidgets.QPushButton(_gcd.TITLE)
        self.diff_btn.setToolTip(_gcd.TIP)
        self.diff_btn.setMenu(_gcd.fill_menu(QtWidgets.QMenu(self.diff_btn),
                                             lambda: self.ws))
        v.addLayout(top)
        self.surf_btn = QtWidgets.QPushButton("3D surface")
        self.surf_btn.setCheckable(True)
        self.surf_btn.setToolTip("Show or hide the rotatable, editable 3-D "
                                 "surface beside the grids  Ctrl+3")
        # the table editor's edit bar ends the same way: Heatmap, 3D surface,
        # Difference view
        eb = self.edit.eb
        eb.addStretch()
        eb.addWidget(self.edit.heat)
        eb.addWidget(self.surf_btn)
        eb.addWidget(self.diff_btn)
        v.addWidget(self.edit.bar)
        self.when = QtWidgets.QLabel()
        self.when.setWordWrap(True)
        self.when.setStyleSheet("color: gray")
        self.when.setVisible(False)
        v.addWidget(self.when)

        self.warn = QtWidgets.QLabel()
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("background:#FCEBEB; color:#791F1F; padding:6px;"
                                "border-radius:4px")
        self.warn.setVisible(False)
        v.addWidget(self.warn)

        self.axes_lbl = QtWidgets.QLabel()
        self.axes_lbl.setStyleSheet("color: gray")
        self.surf = EoiSurface(self)
        hdr = QtWidgets.QHBoxLayout()
        hdr.addWidget(self.surf.bar)
        hdr.addWidget(self.axes_lbl, 1, QtCore.Qt.AlignmentFlag.AlignBottom)
        v.addLayout(hdr)
        self.grid = QtWidgets.QTableWidget()
        self.grid.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.prim_head = QtWidgets.QLabel()
        self.prim_head.setStyleSheet("color: gray")
        self.prim_head.setVisible(False)
        pbox = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(pbox)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(2)
        pl.addWidget(self.prim_head)
        pl.addWidget(self.grid, 1)
        # the app's table conventions: alternating rows and the shared
        # delegate (a selected cell shows in the solid accent over the
        # band / shade it keeps), the difference outline
        f = self.grid.font()
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1))
        for g in (self.grid, self.cmpv.grid):
            g.setAlternatingRowColors(True)
            g.setItemDelegate(CellDelegate(g))
            g.horizontalHeader().setSectionResizeMode(
                QtWidgets.QHeaderView.ResizeMode.Stretch)
            g.horizontalHeader().setMinimumSectionSize(34)
            g.verticalHeader().setDefaultSectionSize(22)
            g.setFont(f)
            g.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            g.customContextMenuRequested.connect(
                lambda pos, g=g: self._grid_menu(g, pos))
            sc = QtGui.QShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Copy), g)
            sc.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(lambda g=g: self._copy_selection(g))
        pst = QtGui.QShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Paste),
                              self.grid)
        pst.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        pst.activated.connect(self._paste)
        self.split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.split.setChildrenCollapsible(False)
        self.row1 = QtWidgets.QWidget()
        r1 = QtWidgets.QHBoxLayout(self.row1)
        r1.setContentsMargins(0, 0, 0, 0)
        r1.addWidget(self.surf.pbox)
        r1.addWidget(pbox, 1)
        self.row2 = QtWidgets.QWidget()
        r2 = QtWidgets.QHBoxLayout(self.row2)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.addWidget(self.surf.cbox)
        r2.addWidget(self.cmpv.box, 1)
        self.split.addWidget(self.row1)
        self.split.addWidget(self.row2)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 1)
        v.addWidget(self.split, 1)
        from .gui_cmpscroll import ViewLink
        self.cmpv.attach(ViewLink)
        self.missing = QtWidgets.QLabel()
        self.missing.setWordWrap(True)
        self.missing.setVisible(False)
        v.addWidget(self.missing, 1)

        low = QtWidgets.QHBoxLayout()
        self.info = QtWidgets.QLabel()
        self.info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.info.setWordWrap(True)
        self.info.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.info.setMinimumHeight(80)
        low.addWidget(self.info, 3)
        gocol = QtWidgets.QVBoxLayout()
        self.go_soi = QtWidgets.QPushButton("Go to SOI table")
        self.go_dur = QtWidgets.QToolButton()
        self.go_dur.setText("Go to duration table")
        self.go_dur.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.go_dur.setMenu(QtWidgets.QMenu(self.go_dur))
        gocol.addWidget(self.go_soi)
        gocol.addWidget(self.go_dur)
        gocol.addStretch(1)
        low.addLayout(gocol, 1)
        v.addLayout(low)

        self.status = QtWidgets.QLabel()          # what Go to ... just did
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        self.notes = QtWidgets.QLabel()
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color: gray")
        v.addWidget(self.notes)

        foot = QtWidgets.QHBoxLayout()
        self.copy_btn = QtWidgets.QPushButton("Copy table")
        self.target_btn = QtWidgets.QPushButton("Set target EOI...")
        self.more_btn = QtWidgets.QToolButton()
        self.more_btn.setText("Advanced")
        self.more_btn.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QtWidgets.QMenu(self.more_btn)
        more.addAction("Set step for Alt+Up / Alt+Down...", self.edit.set_step)
        self.collapse_act = more.addAction("Collapse to one duration table...",
                                           self._collapse)
        self.more_btn.setMenu(more)
        foot.addWidget(self.copy_btn)
        foot.addStretch(1)
        foot.addWidget(self.more_btn)
        foot.addWidget(self.target_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        foot.addWidget(close)
        v.addLayout(foot)

        self.ds.currentIndexChanged.connect(self._model_changed)
        self.soi_box.currentIndexChanged.connect(lambda _i: self._set_changed())
        self.coolant.valueChanged.connect(lambda _v: self.rebuild())
        self.mode_group.idClicked.connect(lambda _i: self.rebuild())
        self.view.currentIndexChanged.connect(lambda _i: self.rebuild())
        self.grid.currentCellChanged.connect(
            lambda r, c, _r, _c: self._show_info(r, c))
        self.grid.itemChanged.connect(self._edited)
        self.copy_btn.clicked.connect(self._copy)
        self.target_btn.clicked.connect(self._target)
        self.go_soi.clicked.connect(self._goto_soi)
        self.coolant.valueChanged.connect(lambda _v: self.edit.refresh_reset())
        self.soi_box.currentIndexChanged.connect(
            lambda _i: self.edit.refresh_reset())
        for _l, sp in self.cond.values():
            sp.valueChanged.connect(lambda _v: self.edit.refresh_reset())
        for g in (self.grid, self.cmpv.grid):
            g.selectionModel().selectionChanged.connect(
                lambda *_a: self.surf.repaint())
            g.selectionModel().currentChanged.connect(
                lambda *_a: self.surf.repaint())
        self.edit.attach()
        for w in (self.surf.view, self.surf.cmp_view, self.cmpv.grid):
            self.edit.bind_actions(w)
        self.surf_btn.toggled.connect(self.surf.set_shown)
        sc = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+3"), self)
        sc.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
        sc.activated.connect(self.surf_btn.toggle)
        from . import options as _opt
        _opt.notifier().changed.connect(self._option_changed)

        # open on a dataset the view can calculate, not on one whose tables the
        # definition does not place (unless every one is like that)
        self.ds.setCurrentIndex(next(
            (i for i, m in enumerate(self.models) if not m.missing), 0))
        if entity is not None:
            for i, m in enumerate(self.models):
                if (entity.group or "") == (m.group or ""):
                    self.ds.setCurrentIndex(i)
                    break
        self._model_changed()
        if remembered(SHOWN_KEY):
            self.surf.set_shown(True, remember_it=False)
        self.edit.refresh_reset()
        self._size_window()

    # ------------------------------------------------------------ state
    def say(self, text: str) -> None:
        """A line under the grids and in the tab's status bar."""
        self.status.setText(text)
        try:
            self.ws.statusBar().showMessage(text, 6000)
        except Exception:
            pass

    def diff_state(self) -> Tuple[str, str, bool, bool]:
        """The Difference view: ``(comparison grid's mode, primary grid's
        mode, blank unchanged cells, tint by direction)``."""
        from . import gui_compare_delta as gcd
        from . import options as opt
        return (gcd.mode(), gcd.primary_mode(), bool(opt.get(gcd.BLANK_KEY)),
                bool(opt.get(gcd.TINT_KEY)))

    def shows_difference(self) -> bool:
        """Whether the primary grid shows differences (so is read only)."""
        return bool(self.cmpv.wanted() and self._cmp_vals
                    and self.diff_state()[1] != "abs")

    def _option_changed(self, key: str) -> None:
        from . import gui_compare_delta as gcd
        if key in gcd.KEYS:
            self.rebuild()

    @property
    def model(self) -> _eoi.InjectionModel:
        return self.models[max(0, self.ds.currentIndex())]

    @property
    def calc(self) -> Optional[_eoi.Calculator]:
        m = self.model
        if m.missing:
            return None
        key = (self.ds.currentIndex(), self.soi_key())
        c = self._calcs.get(key)
        if c is None:
            c = self._calcs[key] = _eoi.Calculator(m, self.conditions(), key[1])
        return c

    def soi_key(self) -> str:
        return self.soi_box.currentData() or "base"

    def _set_changed(self) -> None:
        m = self.model
        when = next((x.when for x in m.soi_sets if x.key == self.soi_key()), "")
        self.when.setText(when)
        self.when.setVisible(bool(when))
        self._sync_inputs()
        self.rebuild()

    def _sync_inputs(self) -> None:
        """Show the conditions the current set reads; a start set runs its
        grid over coolant, so the Coolant box has nothing to pick there."""
        m = self.model
        calc = self.calc
        used = set(calc.conditions_used) if calc is not None else \
            {c.cond for c in m.corrections}
        for key, (lbl, sp) in self.cond.items():
            lbl.setVisible(key in used)
            sp.setVisible(key in used)
        start = calc is not None and calc.by_coolant
        self.coolant.setEnabled(not start)
        self.coolant.setToolTip(
            "The start set's grid runs over coolant: every column is its own "
            "coolant temperature." if start else
            "The coolant temperature picks which start-of-injection tables the "
            "ECU blends.")
        self.collapse_act.setEnabled(not start)

    def conditions(self) -> Dict[str, Optional[float]]:
        return {k: (None if sp.specialValueText() and sp.value() <= sp.minimum()
                    else sp.value())
                for k, (_l, sp) in self.cond.items()}

    def _conditions_changed(self) -> None:
        cond = self.conditions()
        for c in self._calcs.values():
            c.conditions.update(cond)
        self.rebuild()

    def mode(self) -> str:
        b = self.mode_group.checkedButton()
        return b.property("mode") if b is not None else "eoi"

    def table_view(self) -> Optional[int]:
        """Duration table number shown on its own axes, or None (the grid)."""
        k = self.view.currentData()
        return None if k is None or k < 0 else int(k)

    def _model_changed(self, *_a):
        m = self.model
        self.view.blockSignals(True)
        self.view.clear()
        self.view.addItem("What the ECU uses (SOI table axes)", -1)
        for k, e in enumerate(m.duration.tables):
            if e is not None:
                self.view.addItem("Duration table %d: %s" % (k, e.name), k)
        self.view.blockSignals(False)
        self.soi_box.blockSignals(True)
        self.soi_box.clear()
        self.soi_box.addItem("Base (normal running)", "base")
        for x in m.soi_sets:
            self.soi_box.addItem(x.label, x.key)
        self.soi_box.blockSignals(False)
        self.set_lbl.setVisible(bool(m.soi_sets))
        self.soi_box.setVisible(bool(m.soi_sets))
        self.when.setVisible(False)
        self._sync_inputs()
        notes = list(m.notes) + [
            "EOI in degrees ATDC: negative = before TDC. SOI in degrees BTDC."]
        self.notes.setText(" ".join(notes))
        self.rebuild()

    def focus_entity(self, e) -> None:
        """Open on ``e``'s view: a duration table shows itself."""
        m = self.model
        for k, t in enumerate(m.duration.tables):
            if t is e:
                i = self.view.findData(k)
                if i >= 0:
                    self.view.setCurrentIndex(i)
                return

    def refresh_from_session(self) -> None:
        """An edit or undo elsewhere: read the tables again."""
        for c in self._calcs.values():
            try:
                c.refresh()
            except Exception:
                pass
        self.rebuild()

    # ------------------------------------------------------------- build
    def rebuild(self) -> None:
        m = self.model
        calc = self.calc
        self.grid.setVisible(calc is not None)
        self.missing.setVisible(calc is None)
        for w in (self.target_btn, self.more_btn, self.copy_btn,
                  self.go_soi, self.go_dur):
            w.setEnabled(calc is not None)
        probs = _eoi.selector_problems(m) if calc is not None else []
        self.warn.setText("<b>The ECU cannot use this selector:</b> "
                          + " ".join(probs) if probs else "")
        self.warn.setVisible(bool(probs))
        if calc is None:
            self.missing.setText(
                "<b>This definition does not place every table the "
                "calculation needs%s:</b><br>" % (
                    " (dataset %s)" % m.label if len(self.models) > 1 else "")
                + "<br>".join(m.missing)
                + "<br><br>Nothing is guessed: the view opens once the "
                "definition has them.")
            self.axes_lbl.setText("")
            self.info.setText("")
            self.cmpv.show(False)
            self.row2.setVisible(False)
            self.surf.sync_rows()
            self.prim_head.setVisible(False)
            return
        k = self.table_view()
        for b in self.mode_group.buttons():
            b.setEnabled(k is None)
        self.target_btn.setEnabled(k is None and not self._read_only())
        self.go_soi.setEnabled(k is None)
        self.more_btn.setEnabled(not self._read_only())
        self._prim_vals, self._cmp_vals = {}, {}
        keep = (self._selected_cells(), (self.grid.currentRow(), self.grid.currentColumn()),
                (self.grid.rowCount(), self.grid.columnCount()))
        self._filling = True
        try:
            if k is None:
                self._fill_grid(calc)
            else:
                self._fill_table(calc, k)
        except ValueError as ex:
            self.grid.setRowCount(0)
            self.info.setText(str(ex))
        finally:
            self._filling = False
        self._restore_selection(*keep)
        self._show_comparison()
        can = self._can_edit()
        for w in self.edit.widgets():
            w.setEnabled(can)
        self._sync_difference_button()
        r, c = max(0, self.grid.currentRow()), max(0, self.grid.currentColumn())
        self._show_info(r, c)
        self.surf.reload()

    def _equal_rows(self) -> None:
        """The primary and comparison grids get the same height (the bar
        between them still moves)."""
        if self.row2.isHidden() or self.split.height() < 40:
            return
        n = self.split.height() - self.split.handleWidth()
        self.split.setSizes([n // 2, n - n // 2])

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if not getattr(self, "_leveled", False):          # the first time only
            self._leveled = True
            QtCore.QTimer.singleShot(0, self._equal_rows)

    def _restore_selection(self, sel, cur, shape) -> None:
        """What was selected is still selected after the grid is filled again
        (an edit, an undo, another condition), when it has the same shape."""
        g = self.grid
        if not sel or (g.rowCount(), g.columnCount()) != shape:
            return
        sm, m = g.selectionModel(), g.model()
        items = QtCore.QItemSelection()
        for r, c in sel:
            items.select(m.index(r, c), m.index(r, c))
        sm.select(items, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if cur[0] >= 0 and cur[1] >= 0:
            sm.setCurrentIndex(m.index(*cur),
                               QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)

    def _sync_difference_button(self) -> None:
        """Enabled while there is a comparison file; says when a difference
        view is on."""
        from . import gui_compare_delta as gcd
        has = self.cmpv.session is not None
        on = has and gcd.active()
        self.diff_btn.setEnabled(has)
        self.diff_btn.setText(gcd.TITLE + (" (on)" if on else ""))

    def _show_comparison(self) -> None:
        """Show the comparison grid under the primary's when there is one to
        show (else say why not); grow the window the first time."""
        cv = self.cmpv
        shown = bool(cv.wanted() and self._cmp_vals)
        from .gui_compare_delta import file_names
        pn, cn = file_names(self.ws)
        cv.show(shown, "Comparison: %s  (read only)" % cn if cn else
                "Comparison (read only)")
        self.prim_head.setText("Primary: %s" % pn if pn else "Primary")
        self.prim_head.setVisible(shown)
        why = "" if shown or not cv.wanted() else cv.reason
        cv.check.setToolTip(why or cv.check.toolTip().split("\n\n")[0])
        if why:
            self.status.setText("Comparison not shown: %s" % why)
        was_hidden = self.row2.isHidden()
        self.row2.setVisible(not cv.box.isHidden())      # no empty row below
        if was_hidden and not self.row2.isHidden():      # shown again: level
            QtCore.QTimer.singleShot(0, self._equal_rows)
        self.surf.sync_rows()
        if shown and not self._sized_for_comparison:
            self._sized_for_comparison = True
            self._size_window(True)

    def _headers(self, t: _eoi.Table) -> None:
        self.grid.setColumnCount(len(t.xs))
        self.grid.setRowCount(len(t.vals))
        self.grid.setHorizontalHeaderLabels([_num(x, 1) for x in t.xs])
        self.grid.setVerticalHeaderLabels(
            [_num(y, 1) for y in (t.ys or [0.0])])
        def _u(ax):
            from .signal_names import english_units
            u = ((ax.units if ax is not None else "") or "").strip("[] ")
            return english_units(u) or "-"
        self.axes_lbl.setText("Columns: %s   ·   Rows: %s" % (
            _u(t.e.x), _u(t.e.y if t.ys else None)))

    # ---------------------------------------------------------- cell look
    @staticmethod
    def _shade(mode: str, val: float, lo: float, hi: float) -> str:
        """The band (EOI), table colour (table number) or sequential shade
        (SOI, duration: ``lo`` to ``hi`` over both grids) of a value."""
        if mode == "eoi":
            return next(col for lim, col in _EOI_BANDS if val <= lim)
        if mode == "idx":
            return _IDX[min(int(val), len(_IDX) - 1)]
        f = 0.0 if hi <= lo else (val - lo) / (hi - lo)
        return _SEQ[min(int(f * len(_SEQ)), len(_SEQ) - 1)]

    def _cell_bg(self, val: float, lo: float, hi: float):
        """The Heatmap colour of a value (the table editor's ramp over
        ``lo``..``hi``), or None: plain cells."""
        if not self.edit.heat.isChecked() or hi <= lo:
            return None
        return heat_color((val - lo) / (hi - lo))

    def _tint(self, mode: str, d: Optional[float]):
        """The Difference view's direction tint of a differing cell."""
        sg = _cd.sign(mode, d)
        if not sg:
            return None
        g = _g()
        return g.DELTA_UP_BG if sg > 0 else g.DELTA_DOWN_BG

    def _new_item(self, txt: str, val: float, bg=None, *, editable=False,
                  italic=False, tip="") -> QtWidgets.QTableWidgetItem:
        it = QtWidgets.QTableWidgetItem(txt)
        it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        if bg is not None:                  # a light fill: dark text, either theme
            it.setForeground(_INK)
            it.setBackground(QtGui.QColor(bg))
        if italic:
            fnt = it.font()
            fnt.setItalic(True)
            it.setFont(fnt)
        if tip:
            it.setToolTip(tip)
        flags = QtCore.Qt.ItemFlag.ItemIsSelectable | QtCore.Qt.ItemFlag.ItemIsEnabled
        if editable:
            flags |= QtCore.Qt.ItemFlag.ItemIsEditable
        it.setFlags(flags)
        it.setData(QtCore.Qt.ItemDataRole.UserRole, val)
        return it

    def _size_comparison(self, t: "_eoi.Table") -> None:
        """The comparison grid has the primary's rows and columns and
        headers."""
        g = self.cmpv.grid
        g.clearContents()
        g.setColumnCount(self.grid.columnCount())
        g.setRowCount(self.grid.rowCount())
        g.setHorizontalHeaderLabels([self.grid.horizontalHeaderItem(c).text()
                                     for c in range(self.grid.columnCount())])
        g.setVerticalHeaderLabels([self.grid.verticalHeaderItem(r).text()
                                   for r in range(self.grid.rowCount())])

    def _fill_grid(self, calc: _eoi.Calculator) -> None:
        cool = self.coolant.value()
        ref = calc.reference(cool)
        mem = calc.soi_members(cool)
        self._ref = ref
        self._cells = {}
        self._headers(ref)
        mode = self.mode()
        vals: Dict[tuple, tuple] = {}
        for r in range(len(ref.vals)):
            for c in range(len(ref.vals[0])):
                rpm, q = ref.point(r, c)
                cell = calc.cell(cool, rpm, q, mem)
                self._cells[(r, c)] = cell
                vals[(r, c)] = cell_value(mode, cell)
        cvals = self.cmpv.values(mode, cool, self._cells) \
            if self.cmpv.wanted() else None
        allv = [v for v, _t in vals.values()] + \
            [v for v, _t in (cvals or {}).values()]
        lo, hi = min(allv), max(allv)
        cmode, pmode, blank, tint = self.diff_state()
        if cvals is None:
            pmode = "abs"
        heat = self.edit.heat.isChecked()
        editable = mode != "idx" and not self._read_only() and pmode == "abs"
        self._size_comparison(ref)
        for (r, c), (val, txt) in vals.items():
            cell = self._cells[(r, c)]
            cval = ctxt = None
            differs = False
            if cvals is not None:
                cval, ctxt = cvals[(r, c)]
                differs = ctxt != txt
            bg = self._cell_bg(val, lo, hi)
            shown, cshown = txt, ctxt
            if differs:
                if pmode != "abs":
                    shown = _cd.text(pmode, _cd.delta(pmode, val, cval), True, 2, blank)
                if cmode != "abs":
                    cshown = _cd.text(cmode, _cd.delta(cmode, val, cval), True, 2, blank)
            elif cvals is not None:
                if pmode != "abs":
                    shown = _cd.text(pmode, None, False, 2, blank)
                if cmode != "abs":
                    cshown = _cd.text(cmode, None, False, 2, blank)
            it = self._new_item(
                shown, val, bg, editable=editable, italic=cell.soi_limited,
                tip=("SOI capped by %s" % calc.model.limiter.name)
                if cell.soi_limited else "")
            if bg is None and tint and differs and not heat and pmode != "abs":
                t = self._tint(pmode, _cd.delta(pmode, val, cval))
                if t is not None:
                    it.setBackground(QtGui.QColor(t))
                    it.setForeground(_INK)
            self.grid.setItem(r, c, it)
            if cvals is not None:
                cbg = self._cell_bg(cval, lo, hi)
                ci = self._new_item(cshown, cval, cbg, tip="Primary: %s" % txt)
                if cbg is None and tint and differs and not heat and cmode != "abs":
                    t = self._tint(cmode, _cd.delta(cmode, val, cval))
                    if t is not None:
                        ci.setBackground(QtGui.QColor(t))
                        ci.setForeground(_INK)
                self.cmpv.grid.setItem(r, c, ci)
                if differs:
                    self.cmpv.mark(it, ci, True)
                    it.setToolTip((it.toolTip() + "\n" if it.toolTip() else "")
                                  + "Comparison: %s" % ctxt)
        self._prim_vals = {rc: v for rc, (v, _t) in vals.items()}
        self._cmp_vals = {rc: v for rc, (v, _t) in cvals.items()} if cvals else {}

    def _table_item(self, val: float, w: float, cool: float,
                    editable: bool) -> QtWidgets.QTableWidgetItem:
        """A duration-table cell: shaded by how much the ECU uses it."""
        if w <= 0:
            it = self._new_item(_num(val, 2), val, _UNUSED_BG, editable=editable,
                                tip="Not used at %.0f °C" % cool)
            it.setForeground(_UNUSED_INK)
        elif w < 0.999:
            it = self._new_item(_num(val, 2), val, _PART_BG, editable=editable,
                                tip="Blended in with weight %.0f %% at %.0f °C"
                                % (100 * w, cool))
        else:
            it = self._new_item(_num(val, 2), val, _USED_BG, editable=editable,
                                tip="Used alone at %.0f °C" % cool)   # light under any theme
        it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, w)
        return it

    def _fill_table(self, calc: _eoi.Calculator, k: int) -> None:
        cool = self.coolant.value()
        t = calc.dur_t[k]
        self._ref = t
        self._cells = {}
        self._headers(t)
        use = calc.usage(k, cool)
        got = self.cmpv.table_values(k, t, cool) if self.cmpv.wanted() else None
        cmode, pmode, blank, tint = self.diff_state()
        if got is None:
            pmode = "abs"
        editable = not self._read_only() and pmode == "abs"
        heat = self.edit.heat.isChecked()
        allv = [x for row in t.vals for x in row] + \
            ([x for x in got[0].values()] if got is not None else [])
        lo, hi = min(allv), max(allv)
        self._size_comparison(t)
        self._prim_vals, self._cmp_vals = {}, {}
        for r in range(len(t.vals)):
            for c in range(len(t.vals[0])):
                val = t.vals[r][c]
                it = self._table_item(val, use[r][c], cool, editable)
                self._prim_vals[(r, c)] = val
                if heat:
                    self._heated(it, val, lo, hi)
                if got is None:
                    self.grid.setItem(r, c, it)
                    continue
                cvals, cuse = got
                cv = cvals[(r, c)]
                ci = self._table_item(
                    cv, cuse[r][c] if cuse is not None else 1.0, cool, False)
                if heat:
                    self._heated(ci, cv, lo, hi)
                if cuse is None:
                    ci.setToolTip("The comparison's table read at this "
                                  "table's breakpoints")
                ctext = _num(cv, 2)
                differs = ctext != it.text()
                ci.setToolTip((ci.toolTip() + "\n" if ci.toolTip() else "")
                              + "Primary: %s" % it.text())
                if cmode != "abs":
                    ci.setText(_cd.text(cmode, _cd.delta(cmode, val, cv)
                                        if differs else None, differs, 2, blank))
                if pmode != "abs":
                    it.setText(_cd.text(pmode, _cd.delta(pmode, val, cv)
                                        if differs else None, differs, 2, blank))
                if tint and differs and not heat:
                    for item, m in ((it, pmode), (ci, cmode)):
                        if m != "abs":
                            tc = self._tint(m, _cd.delta(m, val, cv))
                            if tc is not None:
                                item.setBackground(QtGui.QColor(tc))
                                item.setForeground(_INK)
                self.grid.setItem(r, c, it)
                self.cmpv.grid.setItem(r, c, ci)
                self._cmp_vals[(r, c)] = cv
                if differs:
                    self.cmpv.mark(it, ci, True)
                    it.setToolTip(it.toolTip() + "\nComparison: %s" % ctext)

    def _heated(self, it: QtWidgets.QTableWidgetItem, val: float, lo: float,
                hi: float) -> None:
        """The Heatmap over a duration-table cell (its use stays in the
        tooltip)."""
        bg = self._cell_bg(val, lo, hi)
        if bg is not None:
            it.setBackground(QtGui.QColor(bg))
            it.setForeground(_INK)

    def _read_only(self) -> bool:
        return bool(getattr(self.ws, "view_only", None))

    # ------------------------------------------------------------- info
    def _show_info(self, r: int, c: int) -> None:
        calc = self.calc
        if calc is None or r < 0 or c < 0:
            return
        k = self.table_view()
        menu = self.go_dur.menu()
        menu.clear()
        if k is not None:
            t = calc.dur_t[k]
            if r >= len(t.vals) or c >= len(t.vals[0]):
                return
            rpm, q = t.point(r, c)
            it = self.grid.item(r, c)
            w = it.data(QtCore.Qt.ItemDataRole.UserRole + 1) if it else 0
            self.info.setText(
                "<b>%s</b> at %s rpm, %s mg: %s° &nbsp; used %s at %.0f °C" % (
                    t.e.name, _num(rpm, 0), _num(q, 1), _num(t.vals[r][c], 2),
                    "alone" if w >= 0.999 else ("with weight %.0f %%" % (100 * w)
                                                if w > 0 else "not at all"),
                    self.coolant.value()))
            menu.addAction(t.e.name, lambda e=t.e, rc=(r, c): self._goto(e, [rc]))
            return
        cell = self._cells.get((r, c))
        if cell is None:
            return
        m = calc.model
        soi_names = " + ".join(
            "%s × %.2f" % (calc.soi_t[i].e.name, w) if w < 0.999 else
            calc.soi_t[i].e.name for i, w in cell.soi_members)
        dur_names = " + ".join(
            "%s × %.2f" % (calc.dur_t[i].e.name, w) if w < 0.999 else
            "%s alone" % calc.dur_t[i].e.name for i, w in cell.dur_members)
        if abs(cell.soi_cor) > 0.005:
            soi_names += " %s corrections %s°" % (
                "−" if calc.btdc(cell.soi_cor) < 0 else "+",
                _num(abs(cell.soi_cor), 2))
        lim = (" &nbsp;<i>(capped by %s)</i>" % m.limiter.name) \
            if cell.soi_limited else ""
        if calc.by_coolant:
            where = "%s rpm, %s °C coolant, start quantity %s mg" % (
                _num(cell.rpm, 0), _num(cell.q, 1),
                _num(calc.fuel_q(cell.rpm, cell.q), 1))
        else:
            where = "%s rpm, %s mg" % (_num(cell.rpm, 0), _num(cell.q, 1))
        fq = calc.fuel_q(cell.rpm, cell.q)
        if cell.q_dur is not None and abs(cell.q_dur - fq) > 0.005:
            where += " &nbsp;<span style='color:gray'>(duration read at %s mg: fuel " \
                     "temperature %s %s mg)</span>" % (
                         _num(cell.q_dur, 2), "+" if cell.q_dur > fq else "−",
                         _num(abs(cell.q_dur - fq), 2))
        self.info.setText(
            "<b>%s</b><br>"
            "SOI %s° BTDC &nbsp;<span style='color:gray'>(%s)</span>%s<br>"
            "%s(%s°) = %.2f &nbsp;→&nbsp; duration %s = %s°<br>"
            "EOI = %s − %s = <b>%s</b>" % (
                where,
                _num(cell.soi_btdc, 2), soi_names, lim,
                m.duration.selector.name, _num(cell.soi, 2), cell.index,
                dur_names, _num(cell.duration, 2),
                _num(cell.duration, 2), _num(cell.soi_btdc, 2),
                _eoi.fmt_angle(cell.eoi, 2)))
        for i, _w in cell.dur_members:
            e = calc.dur_t[i].e
            cells = [(rr, cc) for rr, cc, _ in calc.dur_t[i].weights(
                cell.rpm, cell.q_dur if cell.q_dur is not None else cell.q)]
            menu.addAction(e.name, lambda e=e, cl=cells: self._goto(e, cl))

    # --------------------------------------------------------------- go to
    def _goto(self, e, cells) -> None:
        """Open ``e`` in the tab's table view with ``cells`` marked, and bring
        the main window forward (this window stays open beside it)."""
        ws = self.ws
        try:
            win = ws.window()
            if getattr(win, "current", ws) is not ws and hasattr(win, "show_workspace"):
                win.show_workspace(ws)
            ws.show_entity(e)
            model = ws.model
            if model.entity is e and cells:
                model.set_highlight([("z", r, c) for r, c in cells])
                idx = model.index_of("z", *cells[0])
                if idx.isValid():
                    ws.table.setCurrentIndex(idx)
                    ws.table.scrollTo(
                        idx, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
            win.raise_()
            win.activateWindow()
            self.status.setText("Opened %s in the main window%s." % (
                e.name, ", the cells used here marked" if cells else ""))
        except Exception as ex:                       # never silent
            self.status.setText("Could not open %s: %s" % (e.name, ex))

    def _goto_soi(self) -> None:
        """The SOI table the ECU reads most at this coolant temperature, with
        the selected cells marked (no selection: the table on its own)."""
        calc = self.calc
        if calc is None:
            return
        ref = calc.reference(self.coolant.value())
        if self.table_view() is not None:
            self._goto(ref.e, [])
            return
        sel = sorted({(i.row(), i.column()) for i in self.grid.selectedItems()})
        if not sel and self.grid.currentRow() >= 0:
            sel = [(self.grid.currentRow(), self.grid.currentColumn())]
        # the grid is laid out on the reference table's own cells
        self._goto(ref.e, [(r, c) for r, c in sel if (r, c) in self._cells])

    # ---------------------------------------------------------------- edits
    def _apply(self, plan: _eoi.Plan) -> int:
        if not plan.n_cells:
            return 0
        n = _eoi.apply(self.session, plan)
        ws = self.ws
        for fn in ("_refresh_tools",):
            try:
                getattr(ws, fn)()
            except Exception:
                pass
        try:
            ws._changed_cache = None
            ws.model.refresh()
            ws._status()
        except Exception:
            pass
        self.refresh_from_session()
        return n

    def _edited(self, it: QtWidgets.QTableWidgetItem) -> None:
        if self._filling:
            return
        try:
            new = float(it.text().replace(",", ".").replace("°", "").strip())
        except ValueError:
            self.rebuild()
            return
        self.commit({(it.row(), it.column()): new})

    def commit(self, values: Dict[tuple, float]) -> int:
        """Make the shown cells ``{(row, col): value}`` those values, as
        typing them would: in the shown view's terms (an EOI moves the SOI,
        an SOI moves the SOI tables, a duration moves the duration tables,
        a duration table's cells are written), as ONE plan and one undo
        step -- typing one cell, pasting a block, Copy comparison -> primary and
        the Modify selection bar all come here. Returns the cells written."""
        calc = self.calc
        values = {rc: v for rc, v in values.items()
                  if 0 <= rc[0] < self.grid.rowCount()
                  and 0 <= rc[1] < self.grid.columnCount()}
        if calc is None or not values or self._read_only():
            self.rebuild()
            return 0
        cool = self.coolant.value()
        k = self.table_view()
        try:
            if k is not None:
                plan = _eoi.plan_table(calc.dur_t[k].e,
                                       [(r, c, v) for (r, c), v in values.items()])
            else:
                mode = self.mode()
                pts = {(self._cells[rc].rpm, self._cells[rc].q): v
                       for rc, v in values.items()}
                if mode == "eoi":
                    plan = _eoi.plan_target_eoi(calc, cool, lambda n, q: pts.get((n, q)))
                    if plan.unreached:
                        QtWidgets.QMessageBox.information(
                            self, "End of injection",
                            "No start of injection within the SOI table's range "
                            "gives the wanted EOI at %d point%s." % (
                                len(plan.unreached),
                                "" if len(plan.unreached) == 1 else "s"))
                elif mode == "soi":
                    plan = _eoi.plan_soi(calc, cool, [(n, q, v) for (n, q), v in pts.items()])
                elif mode == "dur":
                    if not self._confirm_quantity():
                        self.rebuild()
                        return 0
                    plan = _eoi.plan_duration(calc, cool,
                                              [(n, q, v) for (n, q), v in pts.items()])
                else:
                    self.rebuild()
                    return 0
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "End of injection",
                                          "Nothing was changed: %s" % ex)
            self.rebuild()
            return 0
        self._last_plan = plan
        n = self._apply(plan)
        if not n:
            self.rebuild()
        return n

    def _can_edit(self) -> bool:
        return (self.calc is not None and not self._read_only()
                and not self.shows_difference()
                and (self.table_view() is not None or self.mode() != "idx"))

    # -------------------------------------------------- right-click, paste
    def _selected_cells(self, g=None) -> List[tuple]:
        g = g or self.grid
        return sorted({(i.row(), i.column()) for i in g.selectedIndexes()})

    def _copy_selection(self, g=None) -> str:
        """The selected cells to the clipboard as shown, tab-separated (the
        table editor's Copy); Copy table keeps the whole table."""
        text = selection_tsv(g or self.grid)
        if text:
            QtWidgets.QApplication.clipboard().setText(text)
        return text

    def _paste(self) -> int:
        """Typed values from the clipboard, from the top-left selected cell
        (one value over a selection fills it), written as typing them."""
        if not self._can_edit():
            return 0
        rows = parse_paste(QtWidgets.QApplication.clipboard().text())
        flat = [v for row in rows for v in row if v is not None]
        if not flat:
            return 0
        sel = self._selected_cells()
        if len(flat) == 1 and len(sel) > 1:
            values = {rc: flat[0] for rc in sel}
        else:
            r0, c0 = sel[0] if sel else (max(0, self.grid.currentRow()),
                                         max(0, self.grid.currentColumn()))
            values = {(r0 + i, c0 + j): v for i, row in enumerate(rows)
                      for j, v in enumerate(row) if v is not None}
        values = {rc: v for rc, v in values.items()
                  if rc[0] < self.grid.rowCount() and rc[1] < self.grid.columnCount()}
        self.commit(values)
        return len(values)

    def _grid_menu(self, g, pos) -> None:
        """Right-click on either grid."""
        menu = self._build_menu(g)
        if menu is not None:
            menu.exec(g.viewport().mapToGlobal(pos))

    def _build_menu(self, g) -> Optional[QtWidgets.QMenu]:
        calc = self.calc
        if calc is None:
            return None
        menu = QtWidgets.QMenu(self)
        sel = self._selected_cells(g)
        ke = QtGui.QKeySequence.StandardKey
        copy = menu.addAction("Copy", lambda: self._copy_selection(g))
        copy.setShortcut(QtGui.QKeySequence(ke.Copy))
        copy.setEnabled(bool(sel))
        paste = menu.addAction("Paste", self._paste)
        paste.setShortcut(QtGui.QKeySequence(ke.Paste))
        paste.setEnabled(g is self.grid and self._can_edit()
                         and bool(QtWidgets.QApplication.clipboard().text()))
        menu.addSeparator()
        tgt = menu.addAction("Set target EOI for the selection...", self._target)
        tgt.setEnabled(self.table_view() is None and not self._read_only()
                       and bool(sel) and g is self.grid)
        menu.addAction("Set step...", self.edit.set_step)
        goto = menu.addAction("Go to SOI table (selected cells marked)", self._goto_soi)
        goto.setEnabled(self.go_soi.isEnabled())
        dur = menu.addMenu("Go to duration table")
        for a in self.go_dur.menu().actions():
            dur.addAction(a)
        dur.setEnabled(bool(dur.actions()) and self.go_dur.isEnabled())
        if self.cmpv.wanted() and self._cmp_vals:
            menu.addSeparator()
            self.cmpv.menu_actions(menu, self._cmp_vals, self._prim_vals, sel,
                                   self._can_edit())
        return menu

    def _size_window(self, with_comparison: Optional[bool] = None) -> None:
        """The size the window opens at: the least its layout allows (wide
        enough for its rows and no wider), and never more than the screen has.
        With the comparison grids shown that is the height too; a single grid
        gets a height to read most of it in. Showing the comparison grid, or
        the surface, takes it up to the new minimum; a window you made larger
        stays so."""
        scr = self.screen() or QtWidgets.QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr is not None else QtCore.QRect(0, 0, 1366, 728)
        lay = self.layout()
        hint = lay.totalMinimumSize() if lay is not None else self.minimumSizeHint()
        w, h = hint.width(), hint.height()
        cmp_on = self.cmpv.wanted() if with_comparison is None else with_comparison
        if not cmp_on:
            # one grid has the whole window: tall enough to read most of it
            h = max(h, min(680, avail.height() - 60))
        if self.isVisible() or with_comparison:
            w, h = max(w, self.width()), max(h, self.height())
        w = max(hint.width(), min(w, avail.width() - 40))
        h = max(hint.height(), min(h, avail.height() - 60))
        self.resize(w, h)
        if self.isVisible():
            fg = self.frameGeometry()
            if fg.bottom() > avail.bottom() or fg.right() > avail.right():
                self.move(max(avail.left(), min(self.x(), avail.right() - self.width())),
                          max(avail.top(), min(self.y(), avail.bottom() - self.height())))

    def _confirm_quantity(self) -> bool:
        if self._asked_quantity:
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Edit injection duration",
            "On a unit-injector engine the duration IS the fuel quantity: "
            "changing it injects more or less fuel for the same quantity "
            "request. To move the end of injection without changing the "
            "fuelling, edit EOI or SOI instead.\n\nEdit the duration?")
        self._asked_quantity = r == QtWidgets.QMessageBox.StandardButton.Yes
        return self._asked_quantity

    # ------------------------------------------------------------- actions
    def _copy(self) -> None:
        g = self.grid
        hdr = {"eoi": "EOI deg ATDC (negative = BTDC)", "soi": "SOI deg BTDC",
               "dur": "Duration deg", "idx": "Duration table no."}[self.mode()]
        if self.table_view() is not None:
            hdr = "Duration table %d" % self.table_view()
        lines = ["%s at %.0f C\t%s" % (hdr, self.coolant.value(), "\t".join(
            g.horizontalHeaderItem(c).text() for c in range(g.columnCount())))]
        for r in range(g.rowCount()):
            lines.append("\t".join([g.verticalHeaderItem(r).text()] + [
                _num(g.item(r, c).data(QtCore.Qt.ItemDataRole.UserRole), 3)
                for c in range(g.columnCount())]))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))

    def selected_points(self) -> List[tuple]:
        return sorted({(i.row(), i.column()) for i in self.grid.selectedIndexes()})

    def _target(self) -> None:
        calc = self.calc
        if calc is None:
            return
        d = TargetDialog(self, calc, self.coolant.value(),
                         bool(len(self.selected_points()) > 1))
        if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        target, only_late, sel_only, tables = d.result_values()
        pts = set(self.selected_points()) if sel_only else None

        def want(rpm, q):
            rc = next((k for k, cl in self._cells.items()
                       if (cl.rpm, cl.q) == (rpm, q)), None)
            if pts is not None and rc not in pts:
                return None
            if only_late and rc is not None and self._cells[rc].eoi <= target:
                return None
            return target
        plan = _eoi.plan_target_eoi(calc, self.coolant.value(), want, tables)
        if not plan.n_cells:
            QtWidgets.QMessageBox.information(self, "Set target EOI",
                                              "No cell needed a change.")
            return
        self._apply(plan)
        msg = "Moved SOI in %d cell(s)." % plan.n_cells
        if plan.limited:
            msg += " %d point(s) stopped at the SOI limiter." % len(plan.limited)
        if plan.unreached:
            msg += " %d point(s) can't reach the target." % len(plan.unreached)
        try:
            self.ws.statusBar().showMessage(msg + " Undo puts it back.", 8000)
        except Exception:
            pass

    def _collapse(self) -> None:
        calc = self.calc
        if calc is None:
            return
        d = CollapseDialog(self, calc, self.coolant.value())
        if d.exec() != QtWidgets.QDialog.DialogCode.Accepted or d.plan is None:
            return
        self._apply(d.plan)


class TargetDialog(QtWidgets.QDialog):
    """Set target EOI...: moves SOI so the end of injection meets a target,
    keeping the ECU's duration selection (so the fuelling stays as it is)."""

    def __init__(self, parent, calc: _eoi.Calculator, coolant: float,
                 has_selection: bool):
        super().__init__(parent)
        self.setWindowTitle("Set target EOI")
        self.calc = calc
        f = QtWidgets.QFormLayout(self)
        self.target = QtWidgets.QDoubleSpinBox()
        self.target.setRange(-20.0, 30.0)
        self.target.setSingleStep(0.5)
        self.target.setDecimals(1)
        self.target.setSuffix("° ATDC")
        self.target.setValue(8.0)
        self.target.setToolTip("Degrees after TDC; a negative value is before TDC.")
        f.addRow("End of injection", self.target)
        self.only_late = QtWidgets.QCheckBox("Only where it ends later than this")
        self.only_late.setChecked(True)
        f.addRow("", self.only_late)
        self.sel_only = QtWidgets.QCheckBox("Selected cells only")
        self.sel_only.setChecked(has_selection)
        self.sel_only.setEnabled(has_selection)
        f.addRow("", self.sel_only)
        box = QtWidgets.QGroupBox("SOI tables to change")
        bl = QtWidgets.QVBoxLayout(box)
        mem = dict(calc.soi_members(coolant))
        self.boxes = []
        for i, t in enumerate(calc.soi_t):
            if t is None:
                continue
            cb = QtWidgets.QCheckBox(t.e.name + (
                "  (used at %.0f °C)" % coolant if i in mem else ""))
            cb.setChecked(i in mem)
            cb.setProperty("idx", i)
            self.boxes.append(cb)
            bl.addWidget(cb)
        f.addRow(box)
        note = QtWidgets.QLabel(
            "Each point gets the start of injection that gives the target with "
            "the ECU's own choice of duration table, so the fuelling does not "
            "change. The change goes into the same cell of every ticked table."
            + (" The SOI limiter (%s) is respected." % calc.model.limiter.name
               if calc.model.limiter is not None else ""))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        f.addRow(note)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Apply")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def result_values(self):
        tables = [b.property("idx") for b in self.boxes if b.isChecked()]
        return (self.target.value(), self.only_late.isChecked(),
                self.sel_only.isChecked(), tables or None)


class CollapseDialog(QtWidgets.QDialog):
    """Advanced: put the duration the ECU uses into one table and point the
    selector at it. Optional, never the default -- it gives up the cam-speed
    compensation the other tables provide."""

    def __init__(self, parent, calc: _eoi.Calculator, coolant: float):
        super().__init__(parent)
        self.setWindowTitle("Collapse to one duration table")
        self.calc = calc
        self.plan = None
        f = QtWidgets.QFormLayout(self)
        self.keep = QtWidgets.QComboBox()
        for k, t in enumerate(calc.dur_t):
            if t is not None:
                self.keep.addItem("%s  (%d × %d)" % (
                    t.e.name, len(t.vals[0]), len(t.vals)), k)
        self.keep.setCurrentIndex(min(1, self.keep.count() - 1))
        f.addRow("Keep", self.keep)
        self.cool = QtWidgets.QDoubleSpinBox()
        self.cool.setRange(-40.0, 130.0)
        self.cool.setDecimals(0)
        self.cool.setSuffix(" °C")
        self.cool.setValue(coolant)
        f.addRow("Reference coolant", self.cool)
        self.preview = QtWidgets.QLabel()
        self.preview.setWordWrap(True)
        f.addRow(self.preview)
        warn = QtWidgets.QLabel(
            "The duration tables correct the quantity for the cam's speed at "
            "each start angle. With one table, any SOI away from the "
            "reference (cold, altitude, a later SOI edit) meters a different "
            "quantity for the same request. The other tables are left in the "
            "file, unused, and Undo puts everything back.")
        warn.setWordWrap(True)
        warn.setStyleSheet("background:#FAEEDA; color:#633806; padding:6px;"
                           "border-radius:4px")
        f.addRow(warn)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Collapse")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.keep.currentIndexChanged.connect(self._update)
        self.cool.valueChanged.connect(self._update)
        self._update()

    def _update(self, *_a):
        k = self.keep.currentData()
        try:
            self.plan = _eoi.plan_collapse(self.calc, k, self.cool.value())
        except ValueError as ex:
            self.plan = None
            self.preview.setText(str(ex))
            return
        rows = []
        for t in sorted({20.0, 0.0, 60.0} - {self.cool.value()}):
            err = sorted(_eoi.collapse_error(self.calc, self.plan, k, t))
            if err:
                rows.append("at %.0f °C: max %.2f°, 90 %% of points within %.2f°"
                            % (t, err[-1], err[int(0.9 * (len(err) - 1))]))
        self.preview.setText(
            "Duration today vs after, over the SOI table's points (the "
            "reference itself matches at the kept table's breakpoints):<br>"
            + "<br>".join(rows))


__all__ = ["EoiDialog", "TargetDialog", "CollapseDialog", "availability",
           "involved", "open_for"]

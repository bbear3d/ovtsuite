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
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import eoi as _eoi

MODES = (("eoi", "EOI"), ("soi", "SOI"), ("dur", "Duration"),
         ("idx", "Table no."))

#: EOI bands (deg ATDC): before TDC, then later and later
_EOI_BANDS = ((0.0, "#E1F5EE"), (4.0, "#F1EFE8"), (8.0, "#FAEEDA"),
              (12.0, "#FAC775"), (1e9, "#F09595"))
_IDX = ("#EEEDFE", "#CECBF6", "#AFA9EC", "#E1F5EE", "#9FE1CB", "#5DCAA5",
        "#F1EFE8", "#D3D1C7")
_INK = QtGui.QColor("#2C2C2A")
_UNUSED_BG = QtGui.QColor("#ECECEC")
_UNUSED_INK = QtGui.QColor("#8A8A8A")
_PART_BG = QtGui.QColor("#FAEEDA")


def _g():
    from . import gui
    return gui


def _num(v: float, nd: int = 2) -> str:
    return ("%.*f" % (nd, v)).rstrip("0").rstrip(".") if nd else "%d" % v


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
        for t in (m.soi.tables + m.duration.tables
                  + [m.soi.selector, m.duration.selector]):
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
        self.setWindowTitle("End of injection")
        try:
            self.setWindowIcon(_g().app_icon())
        except Exception:
            pass
        self.resize(940, 680)
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
        top.addSpacing(12)
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
        self.view.setMinimumContentsLength(24)
        top.addWidget(self.view, 1)
        v.addLayout(top)

        self.warn = QtWidgets.QLabel()
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("background:#FCEBEB; color:#791F1F; padding:6px;"
                                "border-radius:4px")
        self.warn.setVisible(False)
        v.addWidget(self.warn)

        self.axes_lbl = QtWidgets.QLabel()
        self.axes_lbl.setStyleSheet("color: gray")
        v.addWidget(self.axes_lbl)
        self.grid = QtWidgets.QTableWidget()
        self.grid.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.grid.horizontalHeader().setMinimumSectionSize(34)
        self.grid.verticalHeader().setDefaultSectionSize(22)
        f = self.grid.font()
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1))
        self.grid.setFont(f)
        v.addWidget(self.grid, 1)
        self.missing = QtWidgets.QLabel()
        self.missing.setWordWrap(True)
        self.missing.setVisible(False)
        v.addWidget(self.missing, 1)

        low = QtWidgets.QHBoxLayout()
        self.info = QtWidgets.QLabel()
        self.info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.info.setWordWrap(True)
        self.info.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.info.setMinimumHeight(96)
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
        self.coolant.valueChanged.connect(lambda _v: self.rebuild())
        self.mode_group.idClicked.connect(lambda _i: self.rebuild())
        self.view.currentIndexChanged.connect(lambda _i: self.rebuild())
        self.grid.currentCellChanged.connect(
            lambda r, c, _r, _c: self._show_info(r, c))
        self.grid.itemChanged.connect(self._edited)
        self.copy_btn.clicked.connect(self._copy)
        self.target_btn.clicked.connect(self._target)
        self.go_soi.clicked.connect(self._goto_soi)

        if entity is not None:
            for i, m in enumerate(self.models):
                if (entity.group or "") == (m.group or ""):
                    self.ds.setCurrentIndex(i)
                    break
        self._model_changed()

    # ------------------------------------------------------------ state
    @property
    def model(self) -> _eoi.InjectionModel:
        return self.models[max(0, self.ds.currentIndex())]

    @property
    def calc(self) -> Optional[_eoi.Calculator]:
        m = self.model
        if m.missing:
            return None
        key = self.ds.currentIndex()
        c = self._calcs.get(key)
        if c is None:
            c = self._calcs[key] = _eoi.Calculator(m)
        return c

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
                "calculation needs:</b><br>" + "<br>".join(m.missing)
                + "<br><br>Nothing is guessed: the view opens once the "
                "definition has them.")
            self.axes_lbl.setText("")
            self.info.setText("")
            return
        k = self.table_view()
        for b in self.mode_group.buttons():
            b.setEnabled(k is None)
        self.target_btn.setEnabled(k is None and not self._read_only())
        self.go_soi.setEnabled(k is None)
        self.more_btn.setEnabled(not self._read_only())
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
        r, c = max(0, self.grid.currentRow()), max(0, self.grid.currentColumn())
        self._show_info(r, c)

    def _headers(self, t: _eoi.Table) -> None:
        self.grid.setColumnCount(len(t.xs))
        self.grid.setRowCount(len(t.vals))
        self.grid.setHorizontalHeaderLabels([_num(x, 1) for x in t.xs])
        self.grid.setVerticalHeaderLabels(
            [_num(y, 1) for y in (t.ys or [0.0])])
        def _u(ax):
            u = ((ax.units if ax is not None else "") or "").strip("[] ")
            return u or "-"
        self.axes_lbl.setText("Columns: %s   ·   Rows: %s" % (
            _u(t.e.x), _u(t.e.y if t.ys else None)))

    def _fill_grid(self, calc: _eoi.Calculator) -> None:
        cool = self.coolant.value()
        ref = calc.reference(cool)
        mem = calc.soi_members(cool)
        self._ref = ref
        self._cells = {}
        self._headers(ref)
        mode = self.mode()
        for r in range(len(ref.vals)):
            for c in range(len(ref.vals[0])):
                rpm, q = ref.point(r, c)
                cell = calc.cell(cool, rpm, q, mem)
                self._cells[(r, c)] = cell
                if mode == "eoi":
                    val, txt = cell.eoi, _num(cell.eoi, 2)
                elif mode == "soi":
                    val, txt = cell.soi_btdc, _num(cell.soi_btdc, 2)
                elif mode == "dur":
                    val, txt = cell.duration, _num(cell.duration, 2)
                else:
                    val, txt = cell.index, "%.2f" % cell.index
                it = QtWidgets.QTableWidgetItem(txt)
                it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                it.setForeground(_INK)
                if mode == "eoi":
                    bg = next(col for lim, col in _EOI_BANDS if val <= lim)
                    it.setBackground(QtGui.QColor(bg))
                elif mode == "idx":
                    it.setBackground(QtGui.QColor(_IDX[min(int(val), len(_IDX) - 1)]))
                if cell.soi_limited:
                    fnt = it.font()
                    fnt.setItalic(True)
                    it.setFont(fnt)
                    it.setToolTip("SOI capped by %s" % calc.model.limiter.name)
                flags = QtCore.Qt.ItemFlag.ItemIsSelectable | \
                    QtCore.Qt.ItemFlag.ItemIsEnabled
                if mode != "idx" and not self._read_only():
                    flags |= QtCore.Qt.ItemFlag.ItemIsEditable
                it.setFlags(flags)
                it.setData(QtCore.Qt.ItemDataRole.UserRole, val)
                self.grid.setItem(r, c, it)

    def _fill_table(self, calc: _eoi.Calculator, k: int) -> None:
        cool = self.coolant.value()
        t = calc.dur_t[k]
        self._ref = t
        self._cells = {}
        self._headers(t)
        use = calc.usage(k, cool)
        for r in range(len(t.vals)):
            for c in range(len(t.vals[0])):
                w = use[r][c]
                it = QtWidgets.QTableWidgetItem(_num(t.vals[r][c], 2))
                it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if w <= 0:
                    it.setBackground(_UNUSED_BG)
                    it.setForeground(_UNUSED_INK)
                    it.setToolTip("Not used at %.0f °C" % cool)
                elif w < 0.999:
                    it.setBackground(_PART_BG)
                    it.setForeground(_INK)
                    it.setToolTip("Blended in with weight %.0f %% at %.0f °C"
                                  % (100 * w, cool))
                else:
                    it.setForeground(_INK)
                    it.setToolTip("Used alone at %.0f °C" % cool)
                flags = QtCore.Qt.ItemFlag.ItemIsSelectable | \
                    QtCore.Qt.ItemFlag.ItemIsEnabled
                if not self._read_only():
                    flags |= QtCore.Qt.ItemFlag.ItemIsEditable
                it.setFlags(flags)
                it.setData(QtCore.Qt.ItemDataRole.UserRole, t.vals[r][c])
                it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, w)
                self.grid.setItem(r, c, it)

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
        lim = (" &nbsp;<i>(capped by %s)</i>" % m.limiter.name) \
            if cell.soi_limited else ""
        self.info.setText(
            "<b>%s rpm, %s mg</b><br>"
            "SOI %s° BTDC &nbsp;<span style='color:gray'>(%s)</span>%s<br>"
            "%s(%s°) = %.2f &nbsp;→&nbsp; duration %s = %s°<br>"
            "EOI = %s − %s = <b>%s</b>" % (
                _num(cell.rpm, 0), _num(cell.q, 1),
                _num(cell.soi_btdc, 2), soi_names, lim,
                m.duration.selector.name, _num(cell.soi, 2), cell.index,
                dur_names, _num(cell.duration, 2),
                _num(cell.duration, 2), _num(cell.soi_btdc, 2),
                _eoi.fmt_angle(cell.eoi, 2)))
        for i, _w in cell.dur_members:
            e = calc.dur_t[i].e
            cells = [(rr, cc) for rr, cc, _ in calc.dur_t[i].weights(cell.rpm, cell.q)]
            menu.addAction(e.name, lambda e=e, cl=cells: self._goto(e, cl))

    # --------------------------------------------------------------- go to
    def _goto(self, e, cells) -> None:
        ws = self.ws
        try:
            ws.show_entity(e)
            model = ws.model
            if model.entity is e and cells:
                model.set_highlight([("z", r, c) for r, c in cells])
                idx = model.index_of("z", *cells[0])
                if idx.isValid():
                    ws.table.setCurrentIndex(idx)
                    ws.table.scrollTo(
                        idx, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
        except Exception:
            pass

    def _goto_soi(self) -> None:
        calc = self.calc
        if calc is None or self.table_view() is not None:
            return
        r, c = self.grid.currentRow(), self.grid.currentColumn()
        cell = self._cells.get((r, c))
        if cell is None:
            return
        i = max(cell.soi_members, key=lambda m: m[1])[0]
        self._goto(calc.soi_t[i].e, [(r, c)])

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
        calc = self.calc
        try:
            new = float(it.text().replace(",", ".").replace("°", "").strip())
        except ValueError:
            self.rebuild()
            return
        r, c = it.row(), it.column()
        cool = self.coolant.value()
        k = self.table_view()
        try:
            if k is not None:
                plan = _eoi.plan_table(calc.dur_t[k].e, [(r, c, new)])
            else:
                cell = self._cells[(r, c)]
                mode = self.mode()
                if mode == "eoi":
                    plan = _eoi.plan_target_eoi(
                        calc, cool, lambda n, q: new if (n, q) == (cell.rpm, cell.q)
                        else None)
                    if plan.unreached:
                        QtWidgets.QMessageBox.information(
                            self, "End of injection",
                            "No start of injection within the SOI table's range "
                            "gives an EOI of %s there." % _eoi.fmt_angle(new))
                elif mode == "soi":
                    plan = _eoi.plan_soi(calc, cool, [(cell.rpm, cell.q, new)])
                else:
                    if not self._confirm_quantity():
                        self.rebuild()
                        return
                    plan = _eoi.plan_duration(calc, cool, [(cell.rpm, cell.q, new)])
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "End of injection",
                                          "Nothing was changed: %s" % ex)
            self.rebuild()
            return
        self._apply(plan)

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

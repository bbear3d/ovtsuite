# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The gearbox fault-code window: any module's
:class:`ovtsuite.tcu_dtc.FaultModel`, shown and switched the same way.

Search by any form of a code (VAG number, P-code, the fault's own number, a
word of the description), switch one row with its tick box or many with the
buttons (one undo step), restore every row from a stock file, compare with
the comparison file. What a row is, and which bytes switch it, is the
module's business; this window only puts the module's edits through the
session's undo stack.
"""

import re

from PySide6 import QtCore, QtGui, QtWidgets

from .session import TunerSession
from .tcu_dtc import model_for

ROLE = QtCore.Qt.ItemDataRole.UserRole
SORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

SHOW_ALL = "All faults"
SHOW_ON = "Enabled"
SHOW_OFF = "Disabled"
SHOW_CODED = "With a fault code"
SHOW_DIFF = "Differs from comparison"
SHOW_ECU = "Stored on the ECU"


def _colors():
    from . import gui as _g
    return _g.DIFF_BG, _g.EDIT_BG, _g.CELL_DARK_FG


class _Item(QtWidgets.QTableWidgetItem):
    """A cell that sorts by its own key rather than its text."""

    def __lt__(self, other):
        a, b = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if a is not None and b is not None:
            try:
                return a < b
            except TypeError:
                pass
        return super().__lt__(other)


def _esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ADDR = re.compile(r"0x[0-9a-f]+")


class TcuDtcDialog(QtWidgets.QDialog):
    """Enable/disable a gearbox's fault codes."""

    def __init__(self, session: TunerSession, parent=None, compare=None, model=None):
        super().__init__(parent)
        self.session = session
        self._mainwin = parent
        self.model = model
        why = ""
        if self.model is None:
            self.model, why = model_for(session)
        self.cmp_model = None
        if compare is not None and self.model is not None:
            cm, _ = model_for(compare)
            if cm is not None and self.model.same_layout(cm):
                self.cmp_model = cm
        self._has_cmp = self.cmp_model is not None
        self._ecu_codes = {}
        m = self.model
        self.setWindowTitle(getattr(m, "title", "Gearbox fault codes"))
        self.resize(940, 640)
        v = QtWidgets.QVBoxLayout(self)

        self.summary = QtWidgets.QLabel()
        f = self.summary.font()
        f.setBold(True)
        self.summary.setFont(f)
        v.addWidget(self.summary)
        self.source = QtWidgets.QLabel(m.source_text() if m else why)
        self.source.setStyleSheet("color: gray")
        self.source.setWordWrap(True)
        v.addWidget(self.source)
        if m is not None and getattr(m, "note", ""):
            n = QtWidgets.QLabel(m.note)
            n.setWordWrap(True)
            n.setStyleSheet("color: #a0522d")
            v.addWidget(n)

        row = QtWidgets.QHBoxLayout()
        self.variant = QtWidgets.QComboBox()
        for label in (getattr(m, "variants", None) or []):
            self.variant.addItem(label)
        self.variant.setVisible(self.variant.count() > 1)
        self.variant.currentIndexChanged.connect(self._populate)
        row.addWidget(self.variant)
        self.filter = QtWidgets.QLineEdit()
        self.filter.setClearButtonEnabled(True)
        self.filter.setPlaceholderText(
            "Search: a code as a scan tool shows it (17094, P0710, U0100), "
            "the fault's number, or a word of its description")
        self.filter.textChanged.connect(self._populate)
        row.addWidget(self.filter, 1)
        row.addWidget(QtWidgets.QLabel("Show:"))
        self.show_box = QtWidgets.QComboBox()
        items = [SHOW_ALL, SHOW_ON, SHOW_OFF, SHOW_CODED]
        if self._has_cmp:
            items.append(SHOW_DIFF)
        self.show_box.addItems(items)
        self.show_box.currentIndexChanged.connect(self._populate)
        row.addWidget(self.show_box)
        v.addLayout(row)

        self.both = None
        if m is not None and getattr(m, "all_variants_text", "") and len(m.variants) > 1:
            self.both = QtWidgets.QCheckBox(m.all_variants_text)
            self.both.setChecked(True)
            self.both.setToolTip(
                "The car runs one of the coding variants and which one is not "
                "in the file, so by default a switch is made in every variant. "
                "Untick to switch only the variant shown.")
            v.addWidget(self.both)

        group = getattr(m, "group_heading", "") if m else ""
        cols = ["On", "Fault"] + ([group] if group else []) + ["VAG code", "OBD-II"]
        if self._has_cmp:
            cols.append("Cmp on")
        cols.append("Description")
        self._cols = cols
        self._group_col = 2 if group else None
        self.table = QtWidgets.QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_check)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.table, 1)

        self.detail = QtWidgets.QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setMinimumHeight(70)
        self.detail.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.detail.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.detail.setContentsMargins(6, 4, 6, 4)
        v.addWidget(self.detail)

        self.ecu_note = QtWidgets.QLabel("")
        self.ecu_note.setStyleSheet("color: gray")
        self.ecu_note.setWordWrap(True)
        v.addWidget(self.ecu_note)

        btns = QtWidgets.QHBoxLayout()
        self.off_btn = QtWidgets.QPushButton("Disable selected")
        self.off_btn.setToolTip("Switch every selected row off (one undo step)")
        self.off_btn.clicked.connect(lambda: self._set_selected(False))
        btns.addWidget(self.off_btn)
        self.on_btn = QtWidgets.QPushButton("Enable selected")
        self.on_btn.setToolTip("Switch every selected row back on (one undo step)")
        self.on_btn.clicked.connect(lambda: self._set_selected(True))
        btns.addWidget(self.on_btn)
        btns.addStretch(1)
        if m is not None and getattr(m, "ecu_read", False):
            read_ecu = QtWidgets.QPushButton("Read codes from ECU...")
            read_ecu.setToolTip("Read the gearbox's stored fault codes and find them here")
            read_ecu.clicked.connect(self._read_ecu_codes)
            btns.addWidget(read_ecu)
        restore = QtWidgets.QPushButton("Restore all from stock...")
        restore.setToolTip("Set every row back to a stock file of the same software "
                           "(one undo step)")
        restore.clicked.connect(self._restore_stock)
        restore.setEnabled(m is not None)
        btns.addWidget(restore)
        if self._has_cmp:
            copyc = QtWidgets.QPushButton("Copy differences from comparison")
            copyc.setToolTip("Set every row whose state differs to the comparison "
                             "file's (one undo step)")
            copyc.clicked.connect(self._copy_all_from_cmp)
            btns.addWidget(copyc)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        v.addLayout(btns)
        for b in (self.off_btn, self.on_btn):
            b.setEnabled(m is not None)

        self._rows = {}
        self._populate()
        self.table.sortItems(1, QtCore.Qt.SortOrder.AscendingOrder)
        from .gui_fit import fit_table_window
        fit_table_window(self, self.table, 940, 680, last_cap=560)

    # -- state -----------------------------------------------------------
    def _vi(self):
        return max(0, self.variant.currentIndex())

    def _all_variants(self):
        return self.both is None or self.both.isChecked()

    def _cmp_rows(self):
        if not self._has_cmp:
            return {}
        return {r.key: r for r in self.cmp_model.rows(self._vi())}

    def _stored(self, r):
        for n in r.vag_numbers:
            if n in self._ecu_codes:
                return self._ecu_codes[n]
        return None

    @staticmethod
    def _matches(r, terms):
        if not terms:
            return True
        # the detail's addresses ("record at 0x1855A") are not what a
        # number typed in the search means
        hay = r.search_text() + " " + _ADDR.sub(" ", " ".join(r.detail).lower())
        for t in terms:
            if t in hay:
                continue
            if t.isdigit() and ("%05d" % int(t)) in hay:
                continue
            return False
        return True

    # -- table -----------------------------------------------------------
    def _populate(self):
        m = self.model
        rows = m.rows(self._vi()) if m is not None else []
        self._rows = {r.key: r for r in rows}
        cmp = self._cmp_rows()
        terms = self.filter.text().lower().split()
        show = self.show_box.currentText()
        diff_bg, edit_bg, dark_fg = _colors()
        shown = []
        for r in rows:
            c = cmp.get(r.key)
            differs = c is not None and c.on != r.on
            stored = self._stored(r)
            if show == SHOW_ON and not r.on:
                continue
            if show == SHOW_OFF and r.on:
                continue
            if show == SHOW_CODED and not (r.vag or r.pcodes):
                continue
            if show == SHOW_DIFF and not differs:
                continue
            if show == SHOW_ECU and stored is None:
                continue
            if not self._matches(r, terms):
                continue
            shown.append((r, c, differs, stored))

        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(shown))
        for i, (r, c, differs, stored) in enumerate(shown):
            chk = _Item()
            flags = (QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
            if r.can_switch:
                flags |= QtCore.Qt.ItemFlag.ItemIsUserCheckable
            chk.setFlags(flags)
            chk.setCheckState(QtCore.Qt.CheckState.Checked if r.on
                              else QtCore.Qt.CheckState.Unchecked)
            chk.setData(ROLE, r.key)
            chk.setData(SORT_ROLE, 0 if r.on else 1)
            chk.setToolTip("Ticked: the fault is detected and stored" if r.can_switch
                           else "Can't be switched: %s" % r.why_not)
            self.table.setItem(i, 0, chk)
            cells = [(r.ident, r.sort)]
            if self._group_col is not None:
                cells.append((r.group, int(r.group) if r.group.isdigit() else r.group))
            cells += [(", ".join(r.vag) or "-", r.vag[0] if r.vag else "~"),
                      (", ".join(r.pcodes) or "-", r.pcodes[0] if r.pcodes else "~")]
            cmp_col = None
            if self._has_cmp:
                cmp_col = len(cells)
                cells.append(("—" if c is None else ("on" if c.on else "off"),
                              2 if c is None else (0 if c.on else 1)))
            cells.append((r.desc, r.desc.lower()))
            tip = "%s  %s" % (r.ident, r.desc)
            if stored is not None:
                tip += "\nSTORED ON ECU: %s (status 0x%02X)" % (stored.text, stored.status)
                chk.setBackground(diff_bg)
            for j, (txt, sk) in enumerate(cells):
                it = _Item(txt)
                it.setData(ROLE, r.key)
                it.setData(SORT_ROLE, sk)
                it.setToolTip(tip)
                if stored is not None:
                    it.setBackground(diff_bg)
                    it.setForeground(dark_fg)
                elif differs and j == cmp_col:
                    it.setBackground(edit_bg)
                    it.setForeground(dark_fg)
                elif not r.on:
                    it.setForeground(QtGui.QColor(150, 150, 150))
                self.table.setItem(i, j + 1, it)
        self.table.blockSignals(False)
        self.table.setSortingEnabled(sorting)
        self.table.resizeColumnsToContents()
        hh = self.table.horizontalHeader()
        for col in range(1, self.table.columnCount() - 1):
            if hh.sectionSize(col) > 240:
                hh.resizeSection(col, 240)
        hh.setStretchLastSection(True)
        self._refresh_summary(len(shown), rows)
        self._show_detail()

    def refresh_from_session(self):
        """Show edits made outside this window (the grid, undo, a patch,
        another tool) -- called by the tab while this window is open."""
        if self.model is not None:
            try:
                self.model.refresh(bytes(self.session.binfile.data))
            except Exception:
                pass                        # keep the last good read
        self._populate()

    def _refresh_summary(self, shown, rows):
        if self.model is None:
            self.summary.setText("No fault-code table")
            return
        on = sum(1 for r in rows if r.on)
        txt = "%d faults - %d enabled, %d disabled" % (len(rows), on, len(rows) - on)
        if shown != len(rows):
            txt += "   (%d shown)" % shown
        if self.session.modified:
            txt += "   (unsaved changes)"
        self.summary.setText(txt)

    def _selected(self):
        out, seen = [], set()
        for ix in self.table.selectionModel().selectedRows():
            it = self.table.item(ix.row(), 0)
            key = it.data(ROLE) if it is not None else None
            if key is not None and key not in seen and key in self._rows:
                seen.add(key)
                out.append(self._rows[key])
        return out

    def _show_detail(self):
        sel = self._selected()
        if not sel:
            self.detail.setText(
                "<span style='color:gray'>Select a row to see what it sets and "
                "where it is switched. Tick or untick the first column to switch "
                "a fault; select several rows to switch them together.</span>")
            return
        if len(sel) > 1:
            on = sum(1 for r in sel if r.on)
            self.detail.setText("%d rows selected - %d enabled, %d disabled."
                                % (len(sel), on, len(sel) - on))
            return
        r = sel[0]
        parts = ["<b>%s</b> &nbsp; %s" % (_esc(r.ident), _esc(r.desc))]
        codes = []
        if r.vag:
            codes.append("VAG " + ", ".join(r.vag))
        if r.pcodes:
            codes.append("OBD-II " + ", ".join(r.pcodes))
        if codes:
            parts.append(_esc(" · ".join(codes)))
        if r.detail:
            parts.append("<span style='color:gray'>%s</span>"
                         % "<br>".join(_esc(x) for x in r.detail))
        if not r.can_switch and r.why_not:
            parts.append("<span style='color:#a0522d'>Can't be switched: %s</span>"
                         % _esc(r.why_not))
        self.detail.setText("<br>".join(parts))

    # -- edits -----------------------------------------------------------
    def _tell(self, text):
        QtWidgets.QMessageBox.information(self, self.windowTitle(), text)

    def _confirm(self, text):
        return QtWidgets.QMessageBox.question(
            self, self.windowTitle(), text + "\n\nGo ahead?"
        ) == QtWidgets.QMessageBox.StandardButton.Yes

    def _apply(self, plan, ask=True):
        if plan.refused and not plan.edits:
            self._tell("\n".join(plan.refused))
            return False
        if not plan.edits:
            return False
        if ask and plan.confirm and not self._confirm(plan.confirm):
            return False
        with self.session.transaction(plan.label or "fault codes"):
            for addr, data in plan.edits:
                self.session.edit_bytes(addr, data)
        if plan.refused:
            self._tell("Not changed:\n" + "\n".join(plan.refused))
        self.model.refresh(bytes(self.session.binfile.data))
        return True

    def _switch(self, keys, on):
        if self.model is None or not keys:
            return
        plan = self.model.plan(keys, on, self._vi(), self._all_variants())
        self._apply(plan)
        self._populate()

    def _on_check(self, item):
        if item.column() != 0:
            return
        r = self._rows.get(item.data(ROLE))
        if r is None or not r.can_switch:
            return
        want = item.checkState() == QtCore.Qt.CheckState.Checked
        if want != r.on:
            QtCore.QTimer.singleShot(0, lambda k=r.key, w=want: self._switch([k], w))

    def _set_selected(self, on):
        # switching every coding variant: a row already in the wanted state
        # in the variant shown may not be in another, so the model decides
        multi = self._all_variants() and len(getattr(self.model, "variants", None) or ()) > 1
        self._switch([r.key for r in self._selected()
                      if r.can_switch and (multi or r.on != on)], on)

    def _menu(self, pos):
        rows = self._selected()
        if not rows:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction("Disable (%d)" % len(rows), lambda: self._set_selected(False))
        menu.addAction("Enable (%d)" % len(rows), lambda: self._set_selected(True))
        if self._has_cmp:
            menu.addSeparator()
            menu.addAction("Copy comparison state to primary (%d row(s))" % len(rows),
                           lambda: self._copy_from_cmp(rows))
        menu.addSeparator()
        menu.addAction("Copy codes to clipboard", lambda: self._copy_text(rows))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_text(self, rows):
        QtWidgets.QApplication.clipboard().setText("\n".join(
            "%s\t%s\t%s\t%s" % (r.ident, ", ".join(r.vag), ", ".join(r.pcodes), r.desc)
            for r in rows))

    def _copy_from_cmp(self, rows):
        cmp = self._cmp_rows()
        ons = [r.key for r in rows if r.key in cmp and cmp[r.key].on and not r.on]
        offs = [r.key for r in rows if r.key in cmp and not cmp[r.key].on and r.on]
        edits, label = [], "copy fault-code states from comparison"
        from .tcu_dtc import Plan
        plan = Plan(label=label)
        for keys, on in ((ons, True), (offs, False)):
            if keys:
                p = self.model.plan(keys, on, self._vi(), self._all_variants())
                plan.edits += [e for e in p.edits if e not in plan.edits]
                plan.refused += p.refused
                if p.confirm:
                    plan.confirm = (plan.confirm + " " + p.confirm).strip()
        self._apply(plan)
        self._populate()

    def _copy_all_from_cmp(self):
        self._copy_from_cmp(list(self._rows.values()))

    # -- ECU -------------------------------------------------------------
    def _read_ecu_codes(self):
        try:
            from .datalog.gui_log import EcuCodesDialog
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, self.windowTitle(), str(ex))
            return
        dlg = EcuCodesDialog(self.parent() or self, self)
        dlg.exec()
        if dlg.codes is None:
            return
        self._ecu_codes = {c.code: c for c in dlg.codes}
        known = set()
        for r in self._rows.values():
            known.update(r.vag_numbers)
        unmatched = [c for c in dlg.codes if c.code not in known]
        if not dlg.codes:
            self.ecu_note.setText("ECU: no fault codes stored.")
        else:
            self.ecu_note.setText(
                "ECU: %d code(s) stored - highlighted below%s." % (
                    len(dlg.codes),
                    ("; not in this file's table: %s" % ", ".join(
                        c.text for c in unmatched)) if unmatched else ""))
            if self.show_box.findText(SHOW_ECU) < 0:
                self.show_box.addItem(SHOW_ECU)
            self.show_box.setCurrentText(SHOW_ECU)
        self._populate()

    # -- stock -----------------------------------------------------------
    def _restore_stock(self):
        stock = pick_stock_session(self, self._mainwin,
                                   "Restore every fault from %s's original (%s, %s)?",
                                   "Open stock binary to restore fault codes from")
        if stock is None:
            return
        sm, why = model_for(stock)
        if sm is None:
            self._tell("The stock file has no fault-code table: %s" % why)
            return
        plan = self.model.restore_plan(sm)
        if not plan.edits:
            self._tell("\n".join(plan.refused) or "Every fault already matches the stock file.")
            return
        self._apply(plan, ask=False)
        self._populate()


def pick_stock_session(dialog, mainwin, question, title):
    """A stock session: the project's original when there is one and the user
    takes it, else a file the user picks. None when cancelled."""
    from .recent import get_last_dir, set_last_dir
    from . import gui_project as _gp
    ref = getattr(mainwin, "project_ref", None)
    root = None
    if ref is not None:
        try:
            root = ref.set.root_of(ref.branch.head)
        except Exception:
            root = None
    if root is not None:
        SB = QtWidgets.QMessageBox.StandardButton
        box = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Icon.Question, "Restore from stock",
            question % (ref.set.name, "as read" if root.kind == "read"
                        else root.label or "imported",
                        (root.ts or "").replace("T", " ")[:16]),
            SB.Cancel, dialog)
        use = box.addButton("Use the original", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        other = box.addButton("Another file...", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        box.setDefaultButton(use)
        box.exec()
        if box.clickedButton() is use:
            try:
                return _gp._session_at(mainwin, root.id, "%s - original" % ref.set.name)
            except Exception as ex:
                QtWidgets.QMessageBox.critical(dialog, "Error", str(ex))
                return None
        if box.clickedButton() is not other:
            return None
    path, _ = QtWidgets.QFileDialog.getOpenFileName(dialog, title, get_last_dir())
    if not path:
        return None
    set_last_dir(path)
    try:
        return TunerSession(path)
    except Exception as ex:
        QtWidgets.QMessageBox.critical(dialog, "Error", str(ex))
        return None


def dialog_class(model):
    """The window class for ``model``: the module's own subclass when it
    gives one (``model.window_class()``, so F1 finds the module's help
    page), else :class:`TcuDtcDialog`."""
    fn = getattr(model, "window_class", None) if model is not None else None
    if callable(fn):
        try:
            cls = fn()
            if isinstance(cls, type) and issubclass(cls, TcuDtcDialog):
                return cls
        except Exception:
            pass
    return TcuDtcDialog


def open_for(session, parent=None, compare=None):
    """The gearbox fault-code window for ``session``."""
    model, _why = model_for(session)
    return dialog_class(model)(session, parent, compare=compare, model=model)

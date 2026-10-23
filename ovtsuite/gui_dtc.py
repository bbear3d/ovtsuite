# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The EDC16 / EDC15 fault-code editor window.

One row per fault path (EDC16) or sub-fault (EDC15). Every code a row can
set is listed and searchable -- an EDC16 path has up to four error types,
each with its own OBD and VAG code -- and the panel under the table
breaks the selected row down per error type. Ticking a row enables it,
unticking disables it; several rows can be switched at once, as one undo
step.
"""

from PySide6 import QtCore, QtGui, QtWidgets

from .dtc import DtcTable
from .session import TunerSession

ROLE = QtCore.Qt.ItemDataRole.UserRole
SORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1
LAMP_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2

SHOW_ALL = "All fault paths"
SHOW_ON = "Enabled"
SHOW_OFF = "Disabled"
SHOW_CODED = "With a fault code"
SHOW_MIL = "Lights the MIL"
SHOW_GLOW = "Lights the glow-plug lamp"

#: lamp key -> (column heading, name in text)
LAMP_COLS = {"mil": ("MIL", "MIL"), "sys": ("Glow", "glow-plug lamp")}
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


class DtcDialog(QtWidgets.QDialog):
    """Enable/disable trouble codes: EDC16 via the DSM fault class, EDC15
    via the sub-fault's defect time."""

    def __init__(self, session: TunerSession, parent=None, compare=None):
        super().__init__(parent)
        self.session = session
        self.dtc = DtcTable(session)
        self._mainwin = parent
        self.cmp_dtc = None
        if compare is not None:
            try:
                self.cmp_dtc = DtcTable(compare)
            except Exception:
                self.cmp_dtc = None
        self._has_cmp = self.cmp_dtc is not None and bool(self.cmp_dtc.paths)
        self._ecu_codes = {}            # code -> Dtc read from the ECU
        self._edc15 = getattr(session, "is_edc15", False)
        self.setWindowTitle("Fault codes (DTC)")
        self.resize(980, 640)
        v = QtWidgets.QVBoxLayout(self)

        self.summary = QtWidgets.QLabel()
        f = self.summary.font()
        f.setBold(True)
        self.summary.setFont(f)
        v.addWidget(self.summary)
        src = getattr(self.dtc, "source_text", None)
        self.source = QtWidgets.QLabel(src() if src else "")
        self.source.setStyleSheet("color: gray")
        self.source.setWordWrap(True)
        if self.source.text():
            v.addWidget(self.source)
        note = getattr(self.dtc, "note", "")
        if note and self.dtc.paths:
            n = QtWidgets.QLabel(note)
            n.setWordWrap(True)
            n.setStyleSheet("color: #a0522d")
            v.addWidget(n)

        row = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QLineEdit()
        self.filter.setClearButtonEnabled(True)
        self.filter.setPlaceholderText(
            "Search: a code as a scan tool shows it (00257, 16485, P0101), "
            "a name (EGR, glow, APP) or several words")
        self.filter.textChanged.connect(self._populate)
        row.addWidget(self.filter, 1)
        row.addWidget(QtWidgets.QLabel("Show:"))
        self.show_box = QtWidgets.QComboBox()
        items = [SHOW_ALL, SHOW_ON, SHOW_OFF, SHOW_CODED, SHOW_MIL, SHOW_GLOW]
        if self._has_cmp:
            items.append(SHOW_DIFF)
        self.show_box.addItems(items)
        self.show_box.currentIndexChanged.connect(self._populate)
        row.addWidget(self.show_box)
        v.addLayout(row)

        cols = ["On", "Fault path", "VAG code", "OBD-II", "Class"]
        if self._has_cmp:
            cols += ["Cmp on", "Cmp class"]
        cols += ["MIL", "Glow", "Description"]
        self._cols = cols
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
        self.on_btn.setToolTip("Switch every selected row back on, to the "
                               "class it had before (one undo step)")
        self.on_btn.clicked.connect(lambda: self._set_selected(True))
        btns.addWidget(self.on_btn)
        self.lamp_btn = QtWidgets.QPushButton("Warning lamps")
        self.lamp_btn.setToolTip(
            "Keep every selected fault but switch its warning lamp -- the MIL "
            "or the glow-plug lamp -- off or on (one undo step). Untick a "
            "row's MIL or Glow box to do the same for one row.")
        lm = QtWidgets.QMenu(self.lamp_btn)
        for lamp, (_col, name) in LAMP_COLS.items():
            cap = name[0].upper() + name[1:]
            lm.addAction("%s off, keep the faults" % cap,
                         lambda lamp=lamp: self._set_lamp_selected(lamp, False))
            lm.addAction("%s on" % cap,
                         lambda lamp=lamp: self._set_lamp_selected(lamp, True))
        self.lamp_btn.setMenu(lm)
        btns.addWidget(self.lamp_btn)
        btns.addStretch(1)
        read_ecu = QtWidgets.QPushButton("Read codes from ECU...")
        read_ecu.setToolTip("Read the stored fault codes and find them here")
        read_ecu.clicked.connect(self._read_ecu_codes)
        btns.addWidget(read_ecu)
        restore = QtWidgets.QPushButton("Restore all from stock...")
        restore.clicked.connect(self._restore_stock)
        btns.addWidget(restore)
        if self._has_cmp:
            copyc = QtWidgets.QPushButton("Copy differences from comparison")
            copyc.setToolTip(
                "For every row whose state differs, set the primary to the "
                "comparison binary's value. One undoable step.")
            copyc.clicked.connect(self._copy_all_from_cmp)
            btns.addWidget(copyc)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        v.addLayout(btns)

        if not self.dtc.paths:
            self.detail.setText(note or "No fault paths were found in this binary.")
        self._populate()
        self.table.sortItems(1, QtCore.Qt.SortOrder.AscendingOrder)
        # wide enough for the codes and a readable description, on screen
        from .gui_fit import fit_table_window
        fit_table_window(self, self.table, 980, 680, last_cap=560)

    # -- helpers -------------------------------------------------------
    def _key(self, fp):
        for k, p in self.dtc.paths.items():
            if p is fp:
                return k
        return fp.name

    def _cmp_of(self, key, fp):
        if self.cmp_dtc is None:
            return None
        # unnamed EDC16 paths pair only between images of one layout
        return self.dtc.counterpart(self.cmp_dtc, key)

    def _stored(self, fp):
        for c in getattr(fp, "stored_codes", ()) or ():
            if c in self._ecu_codes:
                return self._ecu_codes[c]
        return None

    def _matches(self, fp, terms):
        if not terms:
            return True
        hay = fp.search_text() if hasattr(fp, "search_text") else \
            ("%s %s %s %s" % (fp.name, fp.pcode, fp.vag_code or "",
                              fp.code_desc())).lower()
        for t in terms:
            if t in hay:
                continue
            # a number matches its zero-padded scan-tool form and back
            if t.isdigit() and ("%05d" % int(t)) in hay:
                continue
            return False
        return True

    # -- table ---------------------------------------------------------
    def _populate(self):
        terms = self.filter.text().lower().split()
        show = self.show_box.currentText()
        dtc = self.dtc
        diff_bg, edit_bg, dark_fg = _colors()
        rows = []
        for key, fp in dtc.paths.items():
            en = dtc.is_enabled(fp)
            cfp = self._cmp_of(key, fp)
            cmp_diff = False
            if self._has_cmp and cfp is not None:
                cmp_diff = (self.cmp_dtc.is_enabled(cfp) != en
                            or cfp.current_class != fp.current_class)
            stored = self._stored(fp)
            if show == SHOW_ON and not en:
                continue
            if show == SHOW_OFF and en:
                continue
            if show == SHOW_CODED and not (fp.pcodes or fp.vag_codes):
                continue
            if show == SHOW_MIL and not dtc.lights_mil(fp):
                continue
            if show == SHOW_GLOW and not dtc.lights_glow(fp):
                continue
            if show == SHOW_DIFF and not cmp_diff:
                continue
            if show == SHOW_ECU and stored is None:
                continue
            if not self._matches(fp, terms):
                continue
            rows.append((key, fp, en, cfp, cmp_diff, stored))

        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, (key, fp, en, cfp, cmp_diff, stored) in enumerate(rows):
            chk = _Item()
            chk.setFlags(QtCore.Qt.ItemFlag.ItemIsUserCheckable
                         | QtCore.Qt.ItemFlag.ItemIsEnabled
                         | QtCore.Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(QtCore.Qt.CheckState.Checked if en
                              else QtCore.Qt.CheckState.Unchecked)
            chk.setData(ROLE, key)
            chk.setData(SORT_ROLE, 0 if en else 1)
            chk.setToolTip("Ticked: the fault is detected and stored "
                           "(the class decides which lamp, if any, it lights)")
            self.table.setItem(r, 0, chk)
            vag = fp.vag_codes
            cls = fp.current_class
            cells = [
                (fp.dfp if fp.named else fp.dfp, fp.name.lower()
                 if fp.named else "~%05d" % max(fp.index, 0)),
                (fp.vag_str, vag[0] if vag else 1 << 20),
                (fp.pcode_str, fp.pcodes[0] if fp.pcodes else "~"),
                ("" if cls is None else str(cls), -1 if cls is None else cls),
            ]
            cmp_cols = ()
            if self._has_cmp:
                if cfp is None:
                    cells += [("—", 2), ("—", 1 << 20)]
                else:
                    cc = cfp.current_class
                    cells += [("on" if self.cmp_dtc.is_enabled(cfp) else "off",
                               0 if self.cmp_dtc.is_enabled(cfp) else 1),
                              ("—" if cc is None else str(cc),
                               -1 if cc is None else cc)]
                cmp_cols = (4, 5)
            lit = {lamp: self.dtc.lights(fp, lamp) for lamp in LAMP_COLS}
            lamp_cols = {}
            for lamp, (head, _name) in LAMP_COLS.items():
                lamp_cols[len(cells)] = lamp
                cells.append((head if lit[lamp] else "-",
                              0 if lit[lamp] else 1))
            cells.append((fp.code_desc(), fp.code_desc().lower()))
            tip = "%s\n%s" % (fp.dfp, fp.code_desc())
            lines = fp.type_lines() if hasattr(fp, "type_lines") else []
            if lines:
                tip += "\n" + "\n".join(lines)
            if stored is not None:
                tip += "\nSTORED ON ECU: %s (status 0x%02X)" % (
                    stored.text, stored.status)
                chk.setBackground(diff_bg)
            for i, (txt, sk) in enumerate(cells):
                it = _Item(txt)
                it.setData(ROLE, key)
                it.setData(SORT_ROLE, sk)
                it.setToolTip(tip)
                if stored is not None:
                    it.setBackground(diff_bg)
                    it.setForeground(dark_fg)
                elif cmp_diff and i in cmp_cols:
                    it.setBackground(edit_bg)
                    it.setForeground(dark_fg)
                elif not en:
                    it.setForeground(QtGui.QColor(150, 150, 150))
                if i in lamp_cols:
                    self._lamp_item(it, fp, lamp_cols[i], lit[lamp_cols[i]])
                self.table.setItem(r, i + 1, it)
        self.table.blockSignals(False)
        self.table.setSortingEnabled(sorting)
        self.table.resizeColumnsToContents()
        hh = self.table.horizontalHeader()
        for i in (2, 3):                       # long code lists stay readable
            if hh.sectionSize(i) > 260:
                hh.resizeSection(i, 260)
        hh.setStretchLastSection(True)
        self._refresh_summary(len(rows))
        self._show_detail()

    def refresh_from_session(self):
        """Show edits made outside this window (the grid, undo, a patch,
        another tool) -- called by the tab while this window is open."""
        self._populate()

    def _refresh_summary(self, shown=None):
        s = self.dtc.summary()
        txt = "%d %s - %d enabled, %d disabled" % (
            s["total"], "sub-faults" if self._edc15 else "fault paths",
            s["enabled"], s["disabled"])
        if shown is not None and shown != s["total"]:
            txt += "   (%d shown)" % shown
        if self.session.modified:
            txt += "   (unsaved changes)"
        self.summary.setText(txt)

    def _selected(self):
        out, seen = [], set()
        for ix in self.table.selectionModel().selectedRows():
            it = self.table.item(ix.row(), 0)
            key = it.data(ROLE) if it is not None else None
            if key is not None and key not in seen:
                seen.add(key)
                out.append(self.dtc.paths[key])
        return out

    def _show_detail(self):
        sel = self._selected()
        if not sel:
            self.detail.setText(
                "<span style='color:gray'>Select a row to see every code it "
                "can set. Tick or untick the first column to switch the fault; "
                "untick MIL or Glow to keep the fault without that lamp. Select "
                "several rows to switch them together.</span>")
            return
        if len(sel) > 1:
            on = sum(1 for fp in sel if self.dtc.is_enabled(fp))
            self.detail.setText("%d rows selected - %d enabled, %d disabled."
                                % (len(sel), on, len(sel) - on))
            return
        fp = sel[0]
        esc = lambda t: (t.replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;"))
        parts = ["<b>%s</b> &nbsp; %s" % (esc(fp.dfp), esc(fp.code_desc()))]
        lines = fp.type_lines() if hasattr(fp, "type_lines") else []
        if lines:
            parts.append("<br>".join(esc(x) for x in lines))
        elif not self._edc15:
            parts.append("<span style='color:gray'>No OBD or VAG code is "
                         "assigned to this fault path.</span>")
        info = []
        cls = fp.current_class
        if not self._edc15:
            cd = self.dtc.class_of(fp)
            if cls == 0:
                info.append("class 0 (disabled)")
            elif cd is not None:
                info.append("%s, stored" % cd.text)
            elif cls is not None:
                info.append("class %d" % cls)
            for lamp, (_h, name) in LAMP_COLS.items():
                if cd is not None and self.dtc.can_set_lamp(fp, lamp):
                    on = self.dtc.lights(fp, lamp)
                    tgt = self.dtc.classes.get(
                        self.dtc.lamp_target(fp, lamp, not on))
                    if tgt is not None:
                        info.append("%s %s would use %s" % (
                            name, "off" if on else "on", tgt.text))
            if getattr(fp, "own", None) is not None:
                info.append("DSM number %d" % fp.own)
            src = getattr(fp, "name_source", "")
            if src == "catalogue":
                info.append("name from the reference catalogue")
            elif not fp.named:
                info.append("no definition names this path")
            if fp.cla is not None:
                info.append("class byte at 0x%X" % fp.cla.addr)
        else:
            if cls is not None:
                info.append("defect time %s" % ("never (off)" if cls == 0xFFFF
                                                else "%d" % cls))
            if fp.pri is not None:
                info.append("path %s in this dataset: MIL %s, glow-plug lamp %s"
                            % (fp.owner or fp.path,
                               "on" if self.dtc.lights_mil(fp) else "off",
                               "on" if self.dtc.lights_glow(fp) else "off"))
            if fp.cla is not None:
                info.append("record at 0x%X" % fp.cla.addr)
        if info:
            parts.append("<span style='color:gray'>%s</span>"
                         % esc(" · ".join(info)))
        self.detail.setText("<br>".join(parts))

    # -- edits ---------------------------------------------------------
    def _lamp_item(self, it, fp, lamp, on):
        """A lamp cell: a tick box where the lamp can be switched apart
        from the fault itself."""
        dtc = self.dtc
        head, name = LAMP_COLS[lamp]
        if not dtc.can_set_lamp(fp, lamp):
            note = dtc.lamp_note(fp, lamp)
            if note:
                it.setToolTip("The %s can't be switched here: %s" % (name, note))
            return
        it.setFlags(QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    | QtCore.Qt.ItemFlag.ItemIsEnabled
                    | QtCore.Qt.ItemFlag.ItemIsSelectable)
        it.setData(LAMP_ROLE, lamp)
        it.setCheckState(QtCore.Qt.CheckState.Checked if on
                         else QtCore.Qt.CheckState.Unchecked)
        it.setText(head if on else "off")
        it.setToolTip(self._lamp_tip(fp, lamp, on))

    def _lamp_tip(self, fp, lamp, on):
        dtc = self.dtc
        name = LAMP_COLS[lamp][1]
        verb = "Untick" if on else "Tick"
        what = "the %s %s" % (name, "off" if on else "on")
        if self._edc15:
            scope = dtc.lamp_scope(fp)
            return ("%s to switch %s and keep the fault. The lamps are set per "
                    "fault path: this changes path %s in %s (%d sub-fault%s)."
                    % (verb, what, fp.owner or fp.path, fp.group, len(scope),
                       "" if len(scope) == 1 else "s"))
        target = dtc.lamp_target(fp, lamp, not on)
        cd = dtc.classes.get(target)
        return ("%s to switch %s and keep the fault: the path moves to %s "
                "(the class closest to its own that %s the %s)."
                % (verb, what, cd.text if cd else "class %s" % target,
                   "doesn't light" if on else "lights", name))

    def _on_check(self, item):
        key = item.data(ROLE)
        fp = self.dtc.paths.get(key)
        if fp is None:
            return
        want = item.checkState() == QtCore.Qt.CheckState.Checked
        if item.column() == 0:
            if want != self.dtc.is_enabled(fp):
                self.dtc.set_enabled(fp, want)
                QtCore.QTimer.singleShot(0, self._populate)
        else:
            lamp = item.data(LAMP_ROLE)
            if lamp and want != self.dtc.lights(fp, lamp):
                self.dtc.set_lamp(fp, lamp, want)
                QtCore.QTimer.singleShot(0, self._populate)

    def _set_lamp_selected(self, lamp, on):
        fps = [fp for fp in self._selected()
               if self.dtc.lights(fp, lamp) != on
               and self.dtc.can_set_lamp(fp, lamp)]
        if fps:
            self.dtc.set_lamp_many(fps, lamp, on)
        self._populate()

    def _set_mil_selected(self, mil):
        self._set_lamp_selected("mil", mil)

    def _set_class(self, fps, number):
        with self.session.transaction("fault class %d on %d fault paths"
                                      % (number, len(fps))):
            for fp in fps:
                if fp.current_class != number:
                    self.dtc.set_class(fp, number)
        self._populate()

    def _set_selected(self, enabled):
        fps = [fp for fp in self._selected()
               if self.dtc.is_enabled(fp) != enabled]
        if not fps:
            return
        if hasattr(self.dtc, "set_many"):
            self.dtc.set_many(fps, enabled)
        else:
            with self.session.transaction(
                    ("enable" if enabled else "disable")
                    + " %d fault paths" % len(fps)):
                for fp in fps:
                    self.dtc.set_enabled(fp, enabled)
        self._populate()

    def _menu(self, pos):
        fps = self._selected()
        if not fps:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction("Disable (%d)" % len(fps),
                       lambda: self._set_selected(False))
        menu.addAction("Enable (%d)" % len(fps),
                       lambda: self._set_selected(True))
        menu.addSeparator()
        for lamp, (_h, name) in LAMP_COLS.items():
            cap = name[0].upper() + name[1:]
            menu.addAction("%s off, keep the fault" % cap,
                           lambda lamp=lamp: self._set_lamp_selected(lamp, False))
            menu.addAction("%s on" % cap,
                           lambda lamp=lamp: self._set_lamp_selected(lamp, True))
        if not self._edc15 and self.dtc.classes:
            sub = menu.addMenu("Set fault class")
            sub.addAction("0: off", lambda: self._set_class(fps, 0))
            for n in sorted(self.dtc.classes):
                cd = self.dtc.classes[n]
                sub.addAction("%d: %s" % (n, cd.text.split(": ", 1)[1]),
                              lambda n=n: self._set_class(fps, n))
        if self._has_cmp:
            menu.addSeparator()
            menu.addAction("Copy comparison state to primary (%d row(s))"
                           % len(fps), lambda: self._copy_from_cmp(fps))
        menu.addSeparator()
        menu.addAction("Copy codes to clipboard",
                       lambda: self._copy_text(fps))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_text(self, fps):
        out = []
        for fp in fps:
            out.append("%s\t%s\t%s\t%s" % (fp.dfp, fp.vag_str, fp.pcode_str,
                                           fp.code_desc()))
        QtWidgets.QApplication.clipboard().setText("\n".join(out))

    def _copy_from_cmp(self, fps):
        if self.cmp_dtc is None:
            return
        pairs, seen = [], set()
        for fp in fps:
            if fp.cla is None or fp.cla.addr in seen:
                continue
            cfp = self._cmp_of(self._key(fp), fp)
            if cfp is None:
                continue
            want, cur = cfp.current_class, fp.current_class
            if want is None:
                continue
            if cur is None or int(cur) != int(want):
                seen.add(fp.cla.addr)
                pairs.append((fp.cla, fp.cla.eq.decode(want)))
        if pairs:
            self.session.edit_values(pairs, "copy fault-code states from comparison")
            self._populate()

    def _copy_all_from_cmp(self):
        self._copy_from_cmp(list(self.dtc.paths.values()))

    # -- ECU -----------------------------------------------------------
    def _read_ecu_codes(self):
        try:
            from .datalog.gui_log import EcuCodesDialog
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "Fault codes", str(ex))
            return
        dlg = EcuCodesDialog(self.parent() or self, self)
        dlg.exec()
        if dlg.codes is None:
            return
        self._ecu_codes = {c.code: c for c in dlg.codes}
        known = set()
        for fp in self.dtc.paths.values():
            known.update(getattr(fp, "stored_codes", ()) or ())
        unmatched = [c for c in dlg.codes if c.code not in known]
        if not dlg.codes:
            self.ecu_note.setText("ECU: no fault codes stored.")
        else:
            self.ecu_note.setText(
                "ECU: %d code(s) stored - highlighted below%s." % (
                    len(dlg.codes),
                    ("; not in this binary's fault paths: %s" % ", ".join(
                        c.text for c in unmatched)) if unmatched else ""))
            if self.show_box.findText(SHOW_ECU) < 0:
                self.show_box.addItem(SHOW_ECU)
            self.show_box.setCurrentText(SHOW_ECU)
        self._populate()

    # -- stock ---------------------------------------------------------
    def _restore_stock(self):
        from .recent import get_last_dir, set_last_dir
        from . import gui_project as _gp
        stock = None
        ws = self._mainwin
        ref = getattr(ws, "project_ref", None)
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
                "Restore every fault path from %s's original (%s, %s)?"
                % (ref.set.name, "as read" if root.kind == "read"
                   else root.label or "imported",
                   (root.ts or "").replace("T", " ")[:16]),
                SB.Cancel, self)
            use = box.addButton("Use the original",
                                QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            other = box.addButton("Another file...",
                                  QtWidgets.QMessageBox.ButtonRole.ActionRole)
            box.setDefaultButton(use)
            box.exec()
            if box.clickedButton() is use:
                try:
                    stock = _gp._session_at(ws, root.id,
                                            "%s - original" % ref.set.name)
                except Exception as ex:
                    QtWidgets.QMessageBox.critical(self, "Error", str(ex))
                    return
            elif box.clickedButton() is not other:
                return
        if stock is None:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open stock binary to restore fault paths from",
                get_last_dir())
            if not path:
                return
            set_last_dir(path)
            try:
                stock = TunerSession(path)
            except Exception as ex:
                QtWidgets.QMessageBox.critical(self, "Error", str(ex))
                return
        self.dtc.restore_from(DtcTable(stock))
        self._populate()

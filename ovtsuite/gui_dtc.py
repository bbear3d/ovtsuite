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

EDC15 keeps a full set of sub-faults in every dataset (the coding picks
which one the ECU runs): the window shows one dataset at a time, and an
edit can go to the same fault in every dataset. With a comparison binary
open its rows sit in a read-only table of their own under the primary's,
row for row, the cells that differ shaded in both.

An edit made here redraws only the rows it changed; the whole table is
rebuilt only when the filter changes or an edit arrives from elsewhere.

Edits are written to the session as they are made -- the grid and the
comparison shading follow at once -- and the rows changed since the window
opened or since the last **Apply** are shaded as not applied yet. **Apply**
makes them one undo step (:class:`~ovtsuite.undo_mark.UndoMark`); **OK**
does the same and closes; **Cancel** puts every byte they changed back and
closes. The title bar's X and Esc ask first when there are edits not
applied.
"""

from PySide6 import QtCore, QtWidgets

from .dtc import DtcTable
from .gui_cellmarks import (OFF_FG, PENDING_BG, STORED_BG,  # noqa: F401
                            CellDelegate, cmp_fg, mark_differs, mark_pending,
                            copy_selection, follow_current, link_selection,
                            mark_stored)
from .session import TunerSession
from .undo_mark import UndoMark

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

#: the dataset box's entry for every dataset at once
ALL_DATASETS = "All datasets"

#: the app's cell colours (:mod:`ovtsuite.gui_cellmarks`): a row changed
#: since the last Apply (PENDING_BG, bold), a cell that differs from the
#: comparison (orange outline, in both tables), a fault the ECU has stored
#: (STORED_BG); a switched-off fault's text is grey
_GREY = OFF_FG
_CHECKABLE = (QtCore.Qt.ItemFlag.ItemIsUserCheckable
              | QtCore.Qt.ItemFlag.ItemIsEnabled
              | QtCore.Qt.ItemFlag.ItemIsSelectable)
_READ_ONLY = QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable


def _check(on):
    return QtCore.Qt.CheckState.Checked if on else QtCore.Qt.CheckState.Unchecked


def _dtc_text(dtc, encoding: str) -> str:
    """A code the ECU reported, written for this file's code encoding."""
    from .dtc_report import dtc_text
    return dtc_text(dtc, encoding)


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
        self._compare = compare
        self.cmp_dtc = None
        self._edc15 = getattr(session, "is_edc15", False)
        if compare is not None and bool(
                getattr(compare, "is_edc15", False)) == self._edc15:
            try:
                self.cmp_dtc = DtcTable(compare)
            except Exception:
                self.cmp_dtc = None
        self._has_cmp = self.cmp_dtc is not None and bool(self.cmp_dtc.paths)
        self._ecu_codes = {}            # code -> Dtc read from the ECU
        self._rev = None                # session revision the table shows
        # the edits since the window opened / the last Apply: the undo
        # commands they made, and each touched row's state before them
        self._mark = UndoMark(session, "edit fault codes")
        self._base = {}                 # id(fp) -> (fp, state before)
        has_lamps = getattr(self.dtc, "has_lamp_data", lambda: True)()
        self._lamps = tuple(LAMP_COLS) if has_lamps else ()
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
        # EDC15: one dataset (coding block) at a time, the first by default
        self.ds_box = None
        self.all_ds = None
        datasets = getattr(self.dtc, "datasets", lambda: [])()
        # EDC16: the coding-selected copies of the fault-path arrays, one
        # at a time (the table swaps its paths; there is no "all")
        self._swap = hasattr(self.dtc, "use_copy")
        if len(datasets) > 1:
            row.addWidget(QtWidgets.QLabel(
                "Coding copy:" if self._swap else "Dataset:"))
            self.ds_box = QtWidgets.QComboBox()
            self.ds_box.setToolTip(
                "This image's coding states read their own copies of some "
                "fault-path arrays (from the secondary calibration block): "
                "the codings each copy serves are named. Main block: what "
                "the definition places." if self._swap else
                "Every dataset holds its own copy of each fault; the ECU runs "
                "the one its coding selects (the EEPROM's DSV). Shown: one "
                "dataset, or all of them.")
            for lbl, coding in datasets:
                self.ds_box.addItem(
                    lbl if coding is None else "%s (coding %d)" % (lbl, coding),
                    lbl)
            if not self._swap:
                self.ds_box.addItem(ALL_DATASETS, None)
            self.ds_box.currentIndexChanged.connect(self._dataset_changed)
            row.addWidget(self.ds_box)
        row.addWidget(QtWidgets.QLabel("Show:"))
        self.show_box = QtWidgets.QComboBox()
        items = [SHOW_ALL, SHOW_ON, SHOW_OFF, SHOW_CODED]
        if "mil" in self._lamps:
            items.append(SHOW_MIL)
        if "sys" in self._lamps:
            items.append(SHOW_GLOW)
        if self._has_cmp:
            items.append(SHOW_DIFF)
        self.show_box.addItems(items)
        self.show_box.currentIndexChanged.connect(self._populate)
        row.addWidget(self.show_box)
        self.cmp_check = None
        if self._has_cmp:
            self.cmp_check = QtWidgets.QCheckBox("Show comparison")
            self.cmp_check.setToolTip(
                "Show the comparison binary's fault codes in their own table "
                "under this one. Hidden, a cell that differs is still shaded "
                "and its tooltip gives the comparison's value.")
            self.cmp_check.setChecked(True)
            self.cmp_check.toggled.connect(self._show_cmp)
            row.addWidget(self.cmp_check)
        v.addLayout(row)
        if self.ds_box is not None:
            where = "coding copy" if self._swap else "dataset"
            self.all_ds = QtWidgets.QCheckBox(
                "Apply edits to the same fault in every %s" % where)
            self.all_ds.setToolTip(
                "Ticked, switching a fault (or its lamp) on or off does the "
                "same to that fault in every other %s, as one undo step "
                "-- whichever one the car's coding selects then has it."
                % where)
            self.all_ds.setChecked(True)
            v.addWidget(self.all_ds)

        # column ids, in order; the dialog addresses columns by id
        ids = ["on", "path", "vag", "obd", "cls"] + list(self._lamps) + ["desc"]
        heads = {"on": "On", "path": "Fault path", "vag": "VAG code",
                 "obd": "OBD-II",
                 "cls": "Defect time" if self._edc15 else "Class",
                 "desc": "Description"}
        for lamp in self._lamps:
            heads[lamp] = LAMP_COLS[lamp][0]
        self._ids = ids
        self._col = {k: i for i, k in enumerate(ids)}
        cols = [heads[k] for k in ids]
        self._cols = cols
        self.table = QtWidgets.QTableWidget(0, len(cols))
        self._setup_table(self.table, cols)
        self.table.setSortingEnabled(True)
        self.table.itemChanged.connect(self._on_check)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.horizontalHeader().sortIndicatorChanged.connect(
            lambda *_: QtCore.QTimer.singleShot(0, self._fill_cmp_all))

        rows_w = self.table
        self.cmp_table = None
        if self._has_cmp:
            # the comparison: a read-only table of its own under the
            # primary's, the same faults on the same rows, scrolled, sized
            # and selected with it (as the measuring-block window does)
            self.cmp_table = QtWidgets.QTableWidget(0, len(cols))
            self._setup_table(self.cmp_table, cols)
            self.cmp_table.setContextMenuPolicy(
                QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            self.cmp_table.customContextMenuRequested.connect(self._cmp_menu)
            hh, ch = self.table.horizontalHeader(), self.cmp_table.horizontalHeader()
            hh.sectionResized.connect(lambda i, _o, w: ch.resizeSection(i, w))
            ch.sectionClicked.connect(self._sort_from_cmp)
            for a, b in ((self.table, self.cmp_table),
                         (self.cmp_table, self.table)):
                a.verticalScrollBar().valueChanged.connect(
                    b.verticalScrollBar().setValue)
                a.horizontalScrollBar().valueChanged.connect(
                    b.horizontalScrollBar().setValue)
            self.table.currentCellChanged.connect(
                lambda r, c, *_: self._follow(self.cmp_table, r, c))
            self.cmp_table.currentCellChanged.connect(
                lambda r, c, *_: self._follow(self.table, r, c))
            # the whole selection, not just the current cell
            link_selection(self.table, self.cmp_table)
            self.cmp_box = QtWidgets.QWidget()
            cl = QtWidgets.QVBoxLayout(self.cmp_box)
            cl.setContentsMargins(0, 4, 0, 0)
            cl.setSpacing(2)
            cl.addWidget(QtWidgets.QLabel(
                "<b>Comparison:</b> %s (read only)" % getattr(
                    getattr(compare, "binfile", None), "name", "comparison")))
            cl.addWidget(self.cmp_table)
            rows_w = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
            rows_w.setChildrenCollapsible(False)
            rows_w.addWidget(self.table)
            rows_w.addWidget(self.cmp_box)
            rows_w.setSizes([400, 260])
            self.split = rows_w
        v.addWidget(rows_w, 1)

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
        self.lamp_btn = None
        if self._lamps:
            self.lamp_btn = QtWidgets.QPushButton("Warning lamps")
            self.lamp_btn.setToolTip(
                "Keep every selected fault but switch its warning lamp -- the "
                "MIL or the glow-plug lamp -- off or on (one undo step). "
                "Untick a row's MIL or Glow box to do the same for one row.")
            lm = QtWidgets.QMenu(self.lamp_btn)
            for lamp in self._lamps:
                name = LAMP_COLS[lamp][1]
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
                "For every row shown whose state differs, set the primary to "
                "the comparison binary's value. One undoable step.")
            copyc.clicked.connect(self._copy_all_from_cmp)
            btns.addWidget(copyc)
        v.addLayout(btns)

        # Apply, Cancel, OK -- in that order, OK last
        ok_row = QtWidgets.QHBoxLayout()
        self.pending_lbl = QtWidgets.QLabel("")
        self.pending_lbl.setStyleSheet("color: #8a6d00")
        ok_row.addWidget(self.pending_lbl)
        ok_row.addStretch(1)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.setToolTip(
            "Keep the edits made since the window opened or since the last "
            "Apply (rows shaded yellow) as one undo step")
        self.apply_btn.clicked.connect(self.apply)
        self.apply_btn.setDefault(True)
        ok_row.addWidget(self.apply_btn)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setToolTip(
            "Take back every edit made since the last Apply (or since the "
            "window opened) and close the window")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(self.cancel)
        ok_row.addWidget(self.cancel_btn)
        self.ok_btn = QtWidgets.QPushButton("OK")
        self.ok_btn.setToolTip("Keep the edits not applied yet (one undo "
                               "step) and close the window")
        self.ok_btn.setAutoDefault(False)
        self.ok_btn.clicked.connect(self.ok)
        ok_row.addWidget(self.ok_btn)
        v.addLayout(ok_row)

        if not self.dtc.paths:
            self.detail.setText(note or "No fault paths were found in this binary.")
        self.table.sortItems(self._col["path"], QtCore.Qt.SortOrder.AscendingOrder)
        self._populate()
        # wide enough for the codes and a readable description, on screen
        from .gui_fit import fit_table_window
        fit_table_window(self, self.table, 980, 820 if self._has_cmp else 680,
                         last_cap=560)

    @staticmethod
    def _setup_table(t, cols):
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setStretchLastSection(True)
        # as the table editor; it also makes the Windows 11 style draw the
        # selection solid, so it shows over the shaded cells
        t.setAlternatingRowColors(True)
        t.setItemDelegate(CellDelegate(t))     # selected text readable

    # -- helpers -------------------------------------------------------
    def _key(self, fp):
        for k, p in self.dtc.paths.items():
            if p is fp:
                return k
        return fp.name

    def _cmp_of(self, key, fp):
        if self.cmp_dtc is None:
            return None
        counterpart = getattr(self.dtc, "counterpart", None)
        if counterpart is None:
            return self.cmp_dtc.paths.get(key)
        # unnamed EDC16 paths pair only between images of one layout
        return counterpart(self.cmp_dtc, key)

    def _group(self):
        """The EDC15 dataset shown (None = every dataset, or EDC16)."""
        if self.ds_box is None or self._swap:
            return None
        return self.ds_box.currentData()

    def _dataset_changed(self, *_):
        """Another dataset (EDC15: a filter) or coding copy (EDC16: the
        table's paths become that copy's; the comparison follows to its
        copy of the same place in its list, when it has as many)."""
        if self._swap and self.ds_box is not None:
            self.dtc.use_copy(self.ds_box.currentData())
            if self.cmp_dtc is not None and hasattr(self.cmp_dtc, "use_copy"):
                mine = getattr(self.dtc, "copies", [])
                theirs = getattr(self.cmp_dtc, "copies", [])
                i = self.ds_box.currentIndex()
                if len(mine) == len(theirs) and 0 <= i < len(theirs):
                    self.cmp_dtc.use_copy(theirs[i].label)
                elif theirs:
                    self.cmp_dtc.use_copy(theirs[0].label)
        self._populate()

    def _expand(self, fps, lamp=False):
        """``fps`` plus, when edits go to every dataset, the same faults in
        the other datasets. Not a lamp edit on EDC16: a coding copy can
        define its classes differently, so the class a lamp moves a path
        to is decided per copy (switch to the copy to set its lamps)."""
        if self.all_ds is None or not self.all_ds.isChecked() or (
                lamp and self._swap):
            return list(fps)
        out, seen = [], set()
        for fp in fps:
            for o in self.dtc.siblings(fp):
                if id(o) not in seen:
                    seen.add(id(o))
                    out.append(o)
        return out

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
            if t.isdigit() and (("%05d" % int(t)) in hay or ("%06d" % int(t)) in hay):
                continue
            return False
        return True

    def _cells(self, tbl, fp):
        """Column id -> (text, sort key) for ``fp`` as table ``tbl`` (the
        primary's or the comparison's) has it."""
        vag = fp.vag_codes
        cls = fp.current_class
        en = tbl.is_enabled(fp)
        if self._group() is not None and self._edc15:
            label = "%s_%s" % (fp.path, fp.kind)        # the dataset is shown
        else:
            label = fp.dfp
        desc = fp.code_desc()
        if desc == fp.name:
            desc = ""                     # nothing beyond the path's own name
        out = {
            "on": ("", 0 if en else 1),
            "path": (label, fp.name.lower() if fp.named
                     else "~%05d" % max(fp.index, 0)),
            "vag": (fp.vag_str, vag[0] if vag else 1 << 20),
            "obd": (fp.pcode_str, fp.pcodes[0] if fp.pcodes else "~"),
            "cls": ("" if cls is None else str(cls), -1 if cls is None else cls),
            "desc": (desc, desc.lower()),
        }
        for lamp in self._lamps:
            lit = tbl.lights(fp, lamp)
            out[lamp] = (LAMP_COLS[lamp][0] if lit else "-", 0 if lit else 1)
        return out

    def _state(self, tbl, fp):
        """What 'differs from comparison' compares: on/off, class, lamps."""
        return (tbl.is_enabled(fp), fp.current_class,
                tuple(tbl.lights(fp, lamp) for lamp in self._lamps))

    # -- table ---------------------------------------------------------
    def _rows(self):
        """The (key, fp) pairs the filter and the dataset let through."""
        terms = self.filter.text().lower().split()
        show = self.show_box.currentText()
        group = self._group()
        dtc = self.dtc
        out = []
        for key, fp in dtc.paths.items():
            if group is not None and getattr(fp, "group", None) != group:
                continue
            if show == SHOW_ON and not dtc.is_enabled(fp):
                continue
            if show == SHOW_OFF and dtc.is_enabled(fp):
                continue
            if show == SHOW_CODED and not (fp.pcodes or fp.vag_codes):
                continue
            if show == SHOW_MIL and not dtc.lights_mil(fp):
                continue
            if show == SHOW_GLOW and not dtc.lights_glow(fp):
                continue
            if show == SHOW_DIFF:
                cfp = self._cmp_of(key, fp)
                if cfp is None or self._state(dtc, fp) == self._state(
                        self.cmp_dtc, cfp):
                    continue
            if show == SHOW_ECU and self._stored(fp) is None:
                continue
            if not self._matches(fp, terms):
                continue
            out.append((key, fp))
        return out

    def _populate(self):
        rows = self._rows()
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, (key, fp) in enumerate(rows):
            self._fill_row(r, key, fp)
        self.table.blockSignals(False)
        self.table.setSortingEnabled(sorting)
        self.table.resizeColumnsToContents()
        hh = self.table.horizontalHeader()
        for k in ("vag", "obd"):               # long code lists stay readable
            i = self._col[k]
            if hh.sectionSize(i) > 260:
                hh.resizeSection(i, 260)
        hh.setStretchLastSection(True)
        self._fill_cmp_all()
        self._rev = getattr(self.session, "revision", None)
        self._refresh_summary(len(rows))
        self._show_detail()

    def _fill_row(self, r, key, fp):
        """(Re)draw primary row ``r`` for ``fp``. Signals blocked, sorting
        off: the caller's business."""
        dtc = self.dtc
        en = dtc.is_enabled(fp)
        stored = self._stored(fp)
        cells = self._cells(dtc, fp)
        cfp = self._cmp_of(key, fp) if self._has_cmp else None
        ccells = self._cells(self.cmp_dtc, cfp) if cfp is not None else None
        tip = "%s\n%s" % (fp.dfp, fp.code_desc())
        lines = fp.type_lines() if hasattr(fp, "type_lines") else []
        if lines:
            tip += "\n" + "\n".join(lines)
        if stored is not None:
            tip += "\nSTORED ON ECU: %s (status 0x%02X)" % (
                stored.text, stored.status)
        if self._has_cmp and cfp is None:
            tip += "\nNot in the comparison binary."
        pending = self._is_pending(fp)
        if pending:
            tip += ("\nChanged here, not applied yet (Apply keeps it, "
                    "Cancel takes it back).")
        for k in self._ids:
            txt, sk = cells[k]
            it = _Item(txt)
            it.setData(ROLE, key)
            it.setData(SORT_ROLE, sk)
            ctip = tip
            differs = ccells is not None and k not in ("path", "desc") and (
                ccells[k] != cells[k] if k != "on"
                else self.cmp_dtc.is_enabled(cfp) != en)
            if differs:
                ctip += "\nComparison: %s" % (
                    ("on" if self.cmp_dtc.is_enabled(cfp) else "off")
                    if k == "on" else (ccells[k][0] or "-"))
            if k == "on":
                it.setFlags(_CHECKABLE)
                it.setCheckState(_check(en))
                ctip = ("Ticked: the fault is detected and stored (the class "
                        "decides which lamp, if any, it lights)\n") + ctip
            it.setToolTip(ctip)
            # as the shift editor: the fill is not applied (whole row) >
            # stored on the ECU (whole row) > off (grey); a cell that
            # differs from the comparison has its outline over any of them
            if pending:
                mark_pending(it, self.table.font())
            elif stored is not None:
                mark_stored(it)
            elif not en:
                it.setForeground(_GREY)
            if differs:
                mark_differs(it)
            if k in LAMP_COLS:
                self._lamp_item(it, fp, k, dtc.lights(fp, k))
            self.table.setItem(r, self._col[k], it)

    def _fill_cmp_row(self, r, key):
        """Comparison row ``r``: what the comparison has for the fault on
        primary row ``r`` (``key``)."""
        t = self.cmp_table
        fp = self.dtc.paths.get(key)
        cfp = self._cmp_of(key, fp) if fp is not None else None
        if cfp is None:
            for k in self._ids:
                it = QtWidgets.QTableWidgetItem("—" if k != "desc" else
                                                "not in the comparison binary")
                it.setFlags(_READ_ONLY)
                it.setForeground(_GREY)
                it.setData(ROLE, key)
                t.setItem(r, self._col[k], it)
            return
        mine = self._cells(self.dtc, fp)
        cells = self._cells(self.cmp_dtc, cfp)
        cen = self.cmp_dtc.is_enabled(cfp)
        quiet = cmp_fg(t)
        for k in self._ids:
            it = QtWidgets.QTableWidgetItem(cells[k][0])
            it.setFlags(_READ_ONLY)
            it.setData(ROLE, key)
            it.setForeground(quiet)
            if k == "on":
                it.setCheckState(_check(cen))
                differs = cen != self.dtc.is_enabled(fp)
            elif k in LAMP_COLS:
                lit = self.cmp_dtc.lights(cfp, k)
                it.setCheckState(_check(lit))
                differs = cells[k] != mine[k]
            else:
                differs = k not in ("path", "desc") and cells[k] != mine[k]
            if differs:
                mark_differs(it)
            elif not cen:
                it.setForeground(_GREY)
            t.setItem(r, self._col[k], it)

    def _row_keys(self):
        out = []
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            out.append(it.data(ROLE) if it is not None else None)
        return out

    def _fill_cmp_all(self):
        """The comparison table, row for row with the primary's as it is
        sorted now."""
        if self.cmp_table is None:
            return
        keys = self._row_keys()
        self.cmp_table.setUpdatesEnabled(False)
        self.cmp_table.setRowCount(len(keys))
        for r, key in enumerate(keys):
            self._fill_cmp_row(r, key)
        self.cmp_table.setUpdatesEnabled(True)
        hh, ch = self.table.horizontalHeader(), self.cmp_table.horizontalHeader()
        for i in range(len(self._ids)):
            ch.resizeSection(i, hh.sectionSize(i))
        # a sort moved the primary's selected rows: the comparison's follow
        copy_selection(self.table, self.cmp_table)

    def _refresh_rows(self, fps):
        """Redraw just the rows of ``fps`` (an edit made here), not the
        table: a few cells instead of every row's."""
        want = {}
        for fp in fps:
            want[id(fp)] = fp
        keys_before = self._row_keys()
        rows = [(r, k) for r, k in enumerate(keys_before)
                if k is not None and id(self.dtc.paths.get(k)) in want]
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        for r, key in rows:
            self._fill_row(r, key, self.dtc.paths[key])
        self.table.blockSignals(False)
        self.table.setSortingEnabled(sorting)    # may move the edited rows
        if self.cmp_table is not None:
            keys = self._row_keys()
            if keys == keys_before:
                for r, key in rows:
                    self._fill_cmp_row(r, key)
            else:
                self._fill_cmp_all()
        self._rev = getattr(self.session, "revision", None)
        self._refresh_summary(self.table.rowCount())
        self._show_detail()

    def _show_cmp(self, on):
        if self.cmp_table is not None:
            self.cmp_box.setVisible(on)

    def _sort_from_cmp(self, col):
        """A click on the comparison's heading sorts both tables."""
        hh = self.table.horizontalHeader()
        order = QtCore.Qt.SortOrder.AscendingOrder
        if hh.sortIndicatorSection() == col and \
                hh.sortIndicatorOrder() == QtCore.Qt.SortOrder.AscendingOrder:
            order = QtCore.Qt.SortOrder.DescendingOrder
        self.table.sortItems(col, order)

    @staticmethod
    def _follow(other, r, c):
        """The other table's current cell follows this one's (its selection
        is :func:`link_selection`'s: every selected row, not just this)."""
        follow_current(other, r, c)

    def refresh_from_session(self):
        """Show edits made outside this window (the grid, undo, a patch,
        another tool) -- called by the tab while this window is open. An
        edit this window made has been drawn already."""
        rev = getattr(self.session, "revision", None)
        if rev is not None and rev == self._rev:
            return
        self._populate()

    def _refresh_summary(self, shown=None):
        group = self._group()
        s = self.dtc.summary(group) if group is not None else self.dtc.summary()
        txt = "%d %s - %d enabled, %d disabled" % (
            s["total"], "sub-faults" if self._edc15 else "fault paths",
            s["enabled"], s["disabled"])
        if group is not None:
            txt += " in %s" % group
        elif self._swap and self.ds_box is not None:
            txt += " in %s" % self.ds_box.currentText()
        if shown is not None and shown != s["total"]:
            txt += "   (%d shown)" % shown
        if self.session.modified:
            txt += "   (unsaved changes)"
        self.summary.setText(txt)
        self._sync_pending()

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
            lamps = (" Untick MIL or Glow to keep the fault without that lamp."
                     if self._lamps else "")
            self.detail.setText(
                "<span style='color:gray'>Select a row to see every code it "
                "can set. Tick or untick the first column to switch the fault."
                "%s Select several rows to switch them together.</span>" % lamps)
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
            others = len(self.dtc.siblings(fp)) - 1 \
                if hasattr(self.dtc, "siblings") else 0
            if others:
                info.append("also in %d other dataset%s" % (
                    others, "" if others == 1 else "s"))
        if info:
            parts.append("<span style='color:gray'>%s</span>"
                         % esc(" · ".join(info)))
        if self._has_cmp:
            cfp = self._cmp_of(self._key(fp), fp)
            if cfp is None:
                parts.append("<span style='color:gray'>Comparison: not in the "
                             "comparison binary.</span>")
            else:
                cc = cfp.current_class
                what = ("on" if self.cmp_dtc.is_enabled(cfp) else "off")
                if cc is not None:
                    what += ", %s %s" % ("defect time" if self._edc15
                                         else "class", cc)
                for lamp in self._lamps:
                    what += ", %s %s" % (LAMP_COLS[lamp][1], "on" if
                                         self.cmp_dtc.lights(cfp, lamp) else "off")
                same = self._state(self.dtc, fp) == self._state(self.cmp_dtc, cfp)
                parts.append("<span style='color:gray'>Comparison: %s%s</span>"
                             % (esc(what), "" if same else " &mdash; <b>differs</b>"))
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
        it.setFlags(_CHECKABLE)
        it.setData(LAMP_ROLE, lamp)
        it.setCheckState(_check(on))
        it.setText(head if on else "off")
        tip = self._lamp_tip(fp, lamp, on)
        old = it.toolTip()
        if "\nComparison: " in old:
            tip += "\nComparison: " + old.rsplit("\nComparison: ", 1)[1]
        it.setToolTip(tip)

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

    def _touched_by_lamp(self, fps):
        """Every row a lamp edit on ``fps`` redraws: on EDC15 the lamps are
        the whole path's, so its other sub-faults change with it."""
        scope = getattr(self.dtc, "lamp_scope", None)
        if scope is None:
            return list(fps)
        out = []
        for fp in fps:
            out += scope(fp)
        return out

    def _on_check(self, item):
        key = item.data(ROLE)
        fp = self.dtc.paths.get(key)
        if fp is None:
            return
        want = item.checkState() == QtCore.Qt.CheckState.Checked
        if item.column() == 0:
            if want != self.dtc.is_enabled(fp):
                fps = [o for o in self._expand([fp])
                       if self.dtc.is_enabled(o) != want]
                self._switch(fps, want)
                QtCore.QTimer.singleShot(0, lambda: self._refresh_rows(fps))
        else:
            lamp = item.data(LAMP_ROLE)
            if lamp and want != self.dtc.lights(fp, lamp):
                fps = [o for o in self._expand([fp], lamp=True)
                       if self.dtc.lights(o, lamp) != want
                       and self.dtc.can_set_lamp(o, lamp)]
                touched = self._touched_by_lamp(fps)
                with self._edit(touched, "%s %s" % (
                        LAMP_COLS[lamp][1], "on" if want else "off")):
                    if len(fps) == 1:
                        self.dtc.set_lamp(fp, lamp, want)
                    else:
                        self.dtc.set_lamp_many(fps, lamp, want)
                QtCore.QTimer.singleShot(0, lambda: self._refresh_rows(touched))

    def _switch(self, fps, enabled):
        """Switch ``fps`` on or off, as one undo step."""
        if not fps:
            return
        with self._edit(fps, ("enable" if enabled else "disable")
                        + " %d fault path(s)" % len(fps)):
            if len(fps) == 1:
                self.dtc.set_enabled(fps[0], enabled)
            elif hasattr(self.dtc, "set_many"):
                self.dtc.set_many(fps, enabled)
            else:
                for fp in fps:
                    self.dtc.set_enabled(fp, enabled)

    def _set_lamp_selected(self, lamp, on):
        fps = [fp for fp in self._expand(self._selected(), lamp=True)
               if self.dtc.lights(fp, lamp) != on
               and self.dtc.can_set_lamp(fp, lamp)]
        if fps:
            touched = self._touched_by_lamp(fps)
            with self._edit(touched, "%s %s on %d fault path(s)" % (
                    LAMP_COLS[lamp][1], "on" if on else "off", len(fps))):
                self.dtc.set_lamp_many(fps, lamp, on)
            self._refresh_rows(touched)

    def _set_mil_selected(self, mil):
        self._set_lamp_selected("mil", mil)

    def _set_class(self, fps, number):
        with self._edit(fps, "fault class %d on %d fault paths"
                        % (number, len(fps))):
            for fp in fps:
                if fp.current_class != number:
                    self.dtc.set_class(fp, number)
        self._refresh_rows(fps)

    def _set_selected(self, enabled):
        fps = [fp for fp in self._expand(self._selected())
               if self.dtc.is_enabled(fp) != enabled]
        if not fps:
            return
        self._switch(fps, enabled)
        self._refresh_rows(fps)

    def _menu(self, pos):
        fps = self._selected()
        if not fps:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction("Disable (%d)" % len(fps),
                       lambda: self._set_selected(False))
        menu.addAction("Enable (%d)" % len(fps),
                       lambda: self._set_selected(True))
        if self._lamps:
            menu.addSeparator()
        for lamp in self._lamps:
            name = LAMP_COLS[lamp][1]
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

    def _cmp_menu(self, pos):
        """The read-only comparison's menu: its codes to the clipboard, or
        its fault states onto the primary's rows (the Copy differences from
        comparison path, for the selected rows)."""
        fps = self._selected()          # the two tables select the same rows
        if not fps:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction("Copy to primary (%d row(s))" % len(fps),
                       lambda: self._copy_from_cmp(fps))
        menu.addSeparator()
        cfps = self._cmp_paths(fps)
        a = menu.addAction("Copy codes to clipboard",
                           lambda: self._copy_text(cfps))
        a.setEnabled(bool(cfps))
        menu.addAction("Select all", self.cmp_table.selectAll)
        menu.exec(self.cmp_table.viewport().mapToGlobal(pos))

    def _cmp_paths(self, fps):
        """The comparison's fault paths for the primary's ``fps`` (the
        ones it has)."""
        out = []
        for fp in fps:
            cfp = self._cmp_of(self._key(fp), fp)
            if cfp is not None and all(cfp is not x for x in out):
                out.append(cfp)
        return out

    def _copy_text(self, fps):
        out = []
        for fp in fps:
            out.append("%s\t%s\t%s\t%s" % (fp.dfp, fp.vag_str, fp.pcode_str,
                                           fp.code_desc()))
        QtWidgets.QApplication.clipboard().setText("\n".join(out))
        return "\n".join(out)

    def _copy_from_cmp(self, fps):
        if self.cmp_dtc is None:
            return
        pairs, seen, done = [], set(), []
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
                done.append(fp)
        if pairs:
            with self._edit(done, "copy fault-code states from comparison"):
                self.session.edit_values(
                    pairs, "copy fault-code states from comparison")
            self._refresh_rows(done)

    def _copy_all_from_cmp(self):
        """Every row in the dataset shown (or every dataset)."""
        group = self._group()
        self._copy_from_cmp([fp for fp in self.dtc.paths.values()
                             if group is None
                             or getattr(fp, "group", None) == group])

    # -- ECU -----------------------------------------------------------
    def _read_ecu_codes(self):
        try:
            from .datalog.gui_log import EcuCodesDialog
        except Exception as ex:
            QtWidgets.QMessageBox.critical(self, "Fault codes", str(ex))
            return
        dlg = EcuCodesDialog(self.parent() or self, self, report=self._report_context())
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
                        _dtc_text(c, self._code_encoding()) for c in unmatched))
                     if unmatched else ""))
            if getattr(dlg, "show_paths", True):
                self._show_ecu_view()
                return
        # the report stays open (it does not block): its "Show in fault
        # paths" switches the view later (_show_ecu_view); its Close leaves
        # the codes highlighted without it
        self._populate()

    def _show_ecu_view(self):
        """Show just the faults that set a code read from the ECU (the
        report's "Show in fault paths"), and bring this window forward --
        its tab made the current one, restored if minimised."""
        if not self._ecu_codes:
            return
        if self.show_box.findText(SHOW_ECU) < 0:
            self.show_box.addItem(SHOW_ECU)
        self.show_box.blockSignals(True)
        self.show_box.setCurrentText(SHOW_ECU)
        self.show_box.blockSignals(False)
        self._populate()
        try:
            from .gui_dtc_report import bring_to_front
        except Exception:
            return
        bring_to_front(self, self._mainwin)     # its tab in front, not minimised

    def _report_context(self):
        """What the fault code report needs from this window: the fault
        paths that set a code (with whether each is enabled), and the
        same code lookups as the VAG code / OBD-II / Description columns."""
        try:
            from .gui_dtc_report import ReportContext, file_part
            from .dtc_report import PathRef
        except Exception:
            return None

        def paths_for(code):
            out, seen = [], set()
            for fp in self.dtc.paths.values():
                if code in (getattr(fp, "stored_codes", ()) or ()) and fp.dfp not in seen:
                    seen.add(fp.dfp)
                    out.append((fp, PathRef(fp.dfp, self.dtc.is_enabled(fp))))
            self._report_fps[code] = [fp for fp, _r in out]
            return [r for _fp, r in out]

        def describe(code, _paths):
            for fp in self._report_fps.get(code, ()):
                pc = fp.pcode_str if fp.pcode_str != "-" else ""
                return pc, fp.code_desc()
            return "", ""
        self._report_fps = {}
        return ReportContext(ws=self._mainwin, paths_for=paths_for, describe=describe,
                             part=file_part(self.session), engine=True,
                             file=getattr(self.session.binfile, "name", "") or "",
                             encoding=self._code_encoding(),
                             on_show_paths=self._show_ecu_view)

    def _code_encoding(self) -> str:
        """How this file's fault paths store their codes -- the words the
        ECU reports: the EDC16 layout's ``encoding`` ("sae" on P447/P475,
        "vag" otherwise), "vag" for an EDC15, "" when not known."""
        lay = getattr(self.dtc, "layout", None)
        enc = getattr(lay, "encoding", "") if lay is not None else ""
        if not enc and getattr(self.session, "is_edc15", False):
            enc = "vag"
        return enc or ""

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
        with self._edit(list(self.dtc.paths.values()),
                        "restore DTC classes from stock"):
            self.dtc.restore_from(DtcTable(stock))
        self._populate()

    # -- Apply / Cancel / OK ---------------------------------------------
    def _edit(self, fps, label):
        """The context an edit made here runs in: one undo step, kept as
        this window's (see :meth:`cancel`), each row's state noted first."""
        for fp in fps:
            if id(fp) not in self._base:
                self._base[id(fp)] = (fp, self._sig(fp))
        return self._mark.track(label)

    def _sig(self, fp):
        """What a row's 'changed, not applied' compares: the class (or
        defect time) byte, and on EDC15 the path's own lamp bytes. Not
        is_enabled(): on EDC16 that reads the coding copy shown, and a row
        noted in one copy must compare the same in another."""
        if self._edc15:
            return (fp.current_class,
                    tuple(self.dtc.lights(fp, lamp) for lamp in self._lamps))
        return fp.current_class

    def _is_pending(self, fp) -> bool:
        b = self._base.get(id(fp))
        return b is not None and self._sig(fp) != b[1]

    def _pending_fps(self):
        return [fp for fp, sig in self._base.values() if self._sig(fp) != sig]

    def has_pending(self) -> bool:
        """Edits made here since the window opened or the last Apply that
        are still in effect (not undone from the main window)."""
        return self._mark.has_pending()

    def _sync_pending(self):
        if not hasattr(self, "apply_btn"):
            return                         # still building the window
        n = len(self._pending_fps())
        self.apply_btn.setEnabled(self.has_pending())
        self.pending_lbl.setText(
            "%d row%s changed, not applied" % (n, "" if n == 1 else "s")
            if n else "")

    def apply(self) -> int:
        """Keep the edits made since the window opened or the last Apply
        as one undo step. Returns how many rows they changed."""
        rows = self._pending_fps()
        self._mark.apply("edit fault codes")
        self._base = {}
        self._redraw(rows)
        return len(rows)

    def _redraw(self, fps):
        """Redraw the rows of ``fps`` after Apply / Cancel moved the mark
        (their shading goes); the whole table for a large set."""
        if not fps:
            self._rev = getattr(self.session, "revision", None)
            self._refresh_summary(self.table.rowCount())
        elif len(fps) < 200:
            self._refresh_rows(fps)
        else:
            self._populate()

    def cancel(self):
        """Cancel: take back every edit made since the last Apply (or since
        the window opened) and close -- no question: that is what it is
        for."""
        self._discard()
        QtWidgets.QDialog.reject(self)

    def ok(self):
        """OK: keep the edits not applied yet, as Apply does, and close."""
        self.apply()
        QtWidgets.QDialog.accept(self)

    def _discard(self):
        rows = self._pending_fps()
        self._mark.discard()
        self._base = {}
        if self.isVisible():
            self._redraw(rows)

    def _may_close(self) -> bool:
        """Whether the window may go by the title bar's X or Esc: with
        edits not applied, asks to apply or discard them (False when the
        user cancels) -- a stray Esc should not lose a set of edits."""
        if not self.has_pending():
            return True
        SB = QtWidgets.QMessageBox.StandardButton
        n = len(self._pending_fps())
        r = QtWidgets.QMessageBox.question(
            self, "Fault codes",
            "Keep the edits not applied yet (%d row%s changed)?\n\n"
            "Apply keeps them as one undo step; Discard takes them back."
            % (n, "" if n == 1 else "s"),
            SB.Apply | SB.Discard | SB.Cancel, SB.Apply)
        if r == SB.Cancel:
            return False
        if r == SB.Apply:
            self.apply()
        else:
            self._discard()
        return True

    # Esc and the title bar's X end in done() by reject(); QDialog's own
    # closeEvent calls reject(), so reject() must not call close() back
    # (Qt ignores a close() made during one). Cancel and OK call QDialog's
    # own reject() / accept(): they need no question.
    def closeEvent(self, ev):
        if not self._may_close():
            ev.ignore()
            return
        super().closeEvent(ev)

    def reject(self):
        if self._may_close():
            super().reject()

    def accept(self):
        if self._may_close():
            super().accept()

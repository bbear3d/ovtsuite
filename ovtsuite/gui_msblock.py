# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The measuring-block window (Tools > Measuring blocks...): the blocks a
scan tool shows and the signals in their cells, edited and then applied.

Each block is one row of cells; with a comparison binary, the comparison's
blocks are a table of their own under it, scrolled and selected with it. Cell edits, clearing and copying from the comparison are
held -- shown in bold -- until **Apply** (or **OK**, which also closes the
window) writes them as one undo step; **Cancel** forgets them and closes.
The title bar's X and Esc ask first when there are changes not applied.
"""
from PySide6 import QtCore, QtGui, QtWidgets

from .gui_cellmarks import (OFF_FG, PENDING_BG, CellDelegate,  # noqa: F401
                            cmp_fg, mark_differs, mark_pending,
                            follow_current, link_selection,
                            selection_text, selection_tsv)
from .msblock import MeasuringBlocks
from .session import TunerSession
from .signal_names import sentence_case
from .signals import EMPTY_IDS, SignalCatalog, observed_ids

#: the app's cell colours (:mod:`ovtsuite.gui_cellmarks`): a cell edited
#: but not applied yet (PENDING_BG, bold, dark text), a cell that differs
#: from the comparison (orange outline, over the fill, in both tables), an
#: empty slot's grey text


def _tidy(text: str) -> str:
    """A signal name or label as the window shows it: sentence case, the
    first word capitalised ("engine speed" and "Engine Speed" both read
    "Engine speed"), acronyms, units and A2L / xcwGrp names as written."""
    return sentence_case(text) if text else text


def _ds_text(ds) -> str:
    """A dataset's entry in the picker: its label, the coding value that
    selects it and its ident ("Manual - coding 2 - 038906012BD ... SG")."""
    txt = ds.label
    if getattr(ds, "dsv", None) is not None:
        txt += " - coding %d" % ds.dsv
    ident = " ".join((getattr(ds, "ident", "") or "").split())
    if ident:
        txt += " - " + ident
    return txt


class MsBlockDialog(QtWidgets.QDialog):
    """View/edit the measuring-block (signal-block) structure.

    EDC16: groups 1-127 with up to eight cells -- the ECU sends a group's
    own map row as cells 1-4 and the row 127 above it as cells 5-8
    (:mod:`ovtsuite.msblock`). EDC15: blocks per dataset, four cells, plus
    the group descriptors (display type, norm, message) on their own tab."""

    def __init__(self, session: TunerSession, parent=None, compare=None):
        super().__init__(parent)
        self.session = session
        self.mb = MeasuringBlocks(session)
        self._compare = compare
        self._mainwin = parent
        self._edc15 = bool(getattr(session, "is_edc15", False))
        self._logtags = {}              # (group, slot) -> presets it was built for
        self._load_logtags()
        self.cmp_mb = None
        self.cmp_catalog = None
        self._cmp_by_num = {}
        if compare is not None and self.mb.available and (
                bool(getattr(compare, "is_edc15", False)) == self._edc15):
            try:
                _cmb = MeasuringBlocks(compare)
                if _cmb.available:
                    self.cmp_mb = _cmb
                    self._cmp_by_num = {g.number: g for g in _cmb.groups()}
            except Exception:
                self.cmp_mb = None
        self._has_cmp = self.cmp_mb is not None
        self.setWindowTitle("Measuring blocks")
        v = QtWidgets.QVBoxLayout(self)

        if not self.mb.available:
            self.resize(520, 160)
            what = ("xcwK measuring-block constants" if self._edc15
                    else "measuring-block map (UACC_RSigBlkGrp_MAP)")
            v.addWidget(QtWidgets.QLabel(
                "This binary's definition has no %s, so there are no "
                "measuring blocks to show." % what))
            b = QtWidgets.QPushButton("OK")
            b.clicked.connect(self.accept)
            v.addWidget(b)
            return

        self._nslots = self.mb.max_slots
        if self._edc15:
            text = ("Each measuring block (as a scan tool numbers it) shows four "
                    "cells, per calibration dataset. A cell selects a group "
                    "descriptor (xcwGrp), shown as \"id - name\"; 255 = empty. "
                    "Click a cell to pick another descriptor from its list "
                    "(type to filter it). The "
                    "Descriptors tab sets what each descriptor reads and how "
                    "the scan tool scales it.")
        else:
            text = ("Groups 1-127, as a scan tool numbers them. The ECU sends a "
                    "group's own map row as cells 1-4 and the row 127 above it "
                    "as cells 5-8, so a group can show up to eight values. A "
                    "cell selects an ESB entry (signal + display scaling), "
                    "shown as \"id - name\"; 255 = empty. Click a cell to pick "
                    "another signal from its list (type to filter it), or "
                    "select a cell and type an id.")
        info = QtWidgets.QLabel(text)
        info.setWordWrap(True)
        info.setStyleSheet("color: gray;")
        v.addWidget(info)
        self._catalog_note = QtWidgets.QLabel()
        self._catalog_note.setWordWrap(True)
        self._catalog_note.setStyleSheet("color: gray; font-style: italic;")
        v.addWidget(self._catalog_note)

        self.summary = QtWidgets.QLabel()
        v.addWidget(self.summary)

        row = QtWidgets.QHBoxLayout()
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Block number, or part of a signal name...")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._populate)
        row.addWidget(self.filter, 1)
        self.ds_box = None
        self.all_ds = None
        if self._edc15 and len(self.mb.datasets()) > 1:
            # which dataset (coding variant) to show: the car's coding picks
            # one, so one is shown at a time -- the first, as the other
            # windows treat it -- or all of them together
            by_label = {d.label: d for d in getattr(session, "datasets", []) or []}
            row.addWidget(QtWidgets.QLabel("Dataset:"))
            self.ds_box = QtWidgets.QComboBox()
            self.ds_box.setToolTip(
                "The calibration dataset (coding variant) whose blocks and "
                "descriptors are shown. The car's coding selects which one "
                "the ECU uses; \"All datasets\" lists every one together.")
            self.ds_box.addItem("All datasets", None)
            for ds in self.mb.datasets():
                d = by_label.get(ds)
                self.ds_box.addItem(_ds_text(d) if d is not None else ds, ds)
            self.ds_box.setCurrentIndex(1)
            self.ds_box.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            self.ds_box.setMinimumContentsLength(18)
            self.ds_box.currentIndexChanged.connect(self._populate)
            self.ds_box.currentIndexChanged.connect(
                lambda *_: self._populate_descriptors())
            row.addWidget(self.ds_box)
        self.pop_only = QtWidgets.QCheckBox("Populated only")
        self.pop_only.setChecked(True)
        self.pop_only.stateChanged.connect(self._populate)
        row.addWidget(self.pop_only)
        self.cmp_cols = None
        if self._has_cmp:
            self.cmp_cols = QtWidgets.QCheckBox("Show comparison")
            self.cmp_cols.setToolTip(
                "Show the comparison binary's blocks in their own table under "
                "this one. Hidden, a cell that differs is still shaded and its "
                "tooltip names the comparison's signal.")
            self.cmp_cols.setChecked(True)
            self.cmp_cols.toggled.connect(self._show_cmp_cols)
            row.addWidget(self.cmp_cols)
        v.addLayout(row)
        if self._edc15 and len(self.mb.datasets()) > 1:
            self.all_ds = QtWidgets.QCheckBox(
                "Apply edits to every dataset (Manual / Automatic ...)")
            self.all_ds.setToolTip(
                "When ticked, changing a block cell or a descriptor writes the "
                "same value into the same block / descriptor of every dataset, "
                "as one undo step -- whichever one the car's coding selects then "
                "has it. On when the window opens.")
            self.all_ds.setChecked(True)
            v.addWidget(self.all_ds)

        self.catalog = SignalCatalog(session.family, session)
        self.observed = observed_ids(session)
        self._choices = [(sid, _tidy(lbl)) for sid, lbl
                         in self.catalog.choices(list(self.observed))]
        if self._has_cmp:
            try:
                self.cmp_catalog = SignalCatalog(
                    self._compare.family, self._compare)
            except Exception:
                self.cmp_catalog = None
        n = self._nslots
        _mcols = ["Block"] + ["Cell %d" % (i + 1) for i in range(n)]
        self._mb_cols = _mcols
        self._pending = {}                 # (dataset, block, slot) -> id, not applied
        self._pending_desc = {}            # (dataset, id, field) -> (value, every)
        self._groups_by_key = {}
        self.table = QtWidgets.QTableWidget(0, len(_mcols))
        self.table.setHorizontalHeaderLabels(_mcols)
        if not self._edc15 and n > 4:
            for i in range(4, n):
                hi = self.table.horizontalHeaderItem(1 + i)
                hi.setToolTip("Sent from the map row 127 above the group's own")
        self.table.verticalHeader().setVisible(False)
        # as the table editor; it also makes the Windows 11 style draw the
        # selection solid, so it shows over the shaded cells
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self._cell_w = 160
        for _c in range(1, len(_mcols)):
            hdr.setSectionResizeMode(_c, QtWidgets.QHeaderView.Interactive)
            hdr.resizeSection(_c, self._cell_w)
        hdr.setStretchLastSection(True)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectItems)
        self.table.itemSelectionChanged.connect(lambda: self._show_detail(self.table))
        from .gui import SignalDelegate

        class _Delegate(SignalDelegate):
            def initStyleOption(self, opt, index):
                super().initStyleOption(opt, index)
                selection_text(opt)         # selected text readable
        self.table.setItemDelegate(CellDelegate(self.table))
        delegate = _Delegate(self._choices, self.catalog, self.table)
        self._delegate = delegate
        for col in range(1, n + 1):
            self.table.setItemDelegateForColumn(col, delegate)
        # one click opens a cell's list (a double-click still works, and
        # typing on a selected cell or F2 starts an edit)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.DoubleClicked
            | QtWidgets.QAbstractItemView.EditKeyPressed
            | QtWidgets.QAbstractItemView.AnyKeyPressed)
        self.table.clicked.connect(self._click_edit)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._mb_menu)
        self._copy_shortcut(self.table)

        blocks = self.table
        self.cmp_table = None
        if self._has_cmp:
            # the comparison: a read-only table of its own under the
            # primary's, the same blocks on the same rows, scrolled, sized
            # and selected with it
            self.cmp_table = QtWidgets.QTableWidget(0, len(_mcols))
            self.cmp_table.setHorizontalHeaderLabels(_mcols)
            self.cmp_table.verticalHeader().setVisible(False)
            self.cmp_table.setAlternatingRowColors(True)
            self.cmp_table.setItemDelegate(CellDelegate(self.cmp_table))
            self.cmp_table.setEditTriggers(
                QtWidgets.QAbstractItemView.NoEditTriggers)
            self.cmp_table.setContextMenuPolicy(
                QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            self.cmp_table.customContextMenuRequested.connect(self._cmp_menu)
            self._copy_shortcut(self.cmp_table)
            ch = self.cmp_table.horizontalHeader()
            for _c in range(len(_mcols)):
                ch.setSectionResizeMode(_c, QtWidgets.QHeaderView.Interactive)
                ch.resizeSection(_c, hdr.sectionSize(_c))
            ch.setStretchLastSection(True)
            hdr.sectionResized.connect(
                lambda i, _o, w: self.cmp_table.horizontalHeader().resizeSection(i, w))
            for a, b in ((self.table, self.cmp_table), (self.cmp_table, self.table)):
                a.verticalScrollBar().valueChanged.connect(b.verticalScrollBar().setValue)
                a.horizontalScrollBar().valueChanged.connect(
                    b.horizontalScrollBar().setValue)
            self.table.currentCellChanged.connect(
                lambda r, c, *_: self._follow(self.cmp_table, r, c))
            self.cmp_table.currentCellChanged.connect(
                lambda r, c, *_: self._follow(self.table, r, c))
            # the whole selection, not just the current cell
            link_selection(self.table, self.cmp_table)
            self.cmp_table.itemSelectionChanged.connect(
                lambda: self._show_detail(self.cmp_table))
            # a heading, not a frame: the two tables' columns line up
            self.cmp_box = QtWidgets.QWidget()
            cl = QtWidgets.QVBoxLayout(self.cmp_box)
            cl.setContentsMargins(0, 4, 0, 0)
            cl.setSpacing(2)
            head = QtWidgets.QLabel("<b>Comparison:</b> %s (read only)" % getattr(
                getattr(self._compare, "binfile", None), "name", "comparison"))
            cl.addWidget(head)
            cl.addWidget(self.cmp_table)
            blocks = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
            blocks.setChildrenCollapsible(False)
            blocks.addWidget(self.table)
            blocks.addWidget(self.cmp_box)
            blocks.setSizes([400, 260])
            self.blocks_split = blocks
        if self._edc15:
            self.tabs = QtWidgets.QTabWidget()
            self.tabs.addTab(blocks, "Blocks")
            self.tabs.addTab(self._build_descriptor_tab(), "Descriptors")
            v.addWidget(self.tabs, 1)
        else:
            v.addWidget(blocks, 1)

        if self.mb.related:
            row2 = QtWidgets.QHBoxLayout()
            row2.addWidget(QtWidgets.QLabel("Related tables:"))
            self.ref_box = QtWidgets.QComboBox()
            for e in self.mb.related:
                self.ref_box.addItem("%s  (%dx%d)" % (e.name, e.rows, e.cols), e)
            open_ref = QtWidgets.QPushButton("Show in editor")
            open_ref.setToolTip("Select the table in the main window; this "
                                "window stays open.")
            open_ref.clicked.connect(self._open_ref)
            row2.addWidget(self.ref_box, 1)
            row2.addWidget(open_ref)
            v.addLayout(row2)

        self.detail = QtWidgets.QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(QtCore.Qt.RichText)
        self.detail.setStyleSheet(
            "padding:4px; background:palette(midlight); border-radius:4px;")
        self.detail.setText("Select a cell to see its signal details.")
        v.addWidget(self.detail)

        btns = QtWidgets.QHBoxLayout()
        clr = QtWidgets.QPushButton("Clear selected block(s)")
        clr.clicked.connect(self._clear_sel)
        btns.addWidget(clr)
        if self._has_cmp:
            copyb = QtWidgets.QPushButton("Copy block(s) from comparison")
            copyb.setToolTip(
                "Set the selected blocks' cells to what the comparison binary "
                "shows in the same block. Signals are matched by meaning, so "
                "this works between different software versions. With \"Apply "
                "edits to every dataset\" ticked the same block of every "
                "dataset gets them too. Held until Apply, one undoable step.")
            copyb.clicked.connect(lambda: self._copy_mb_sel_from_cmp())
            btns.addWidget(copyb)
        btns.addStretch(1)
        self.pending_lbl = QtWidgets.QLabel("")
        self.pending_lbl.setStyleSheet("color: #8a6d00")
        btns.addWidget(self.pending_lbl)
        # Apply, Cancel, OK -- in that order, OK last
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.setToolTip("Write the changes (shaded yellow) as one "
                                  "undo step and keep the window open")
        self.apply_btn.clicked.connect(self.apply)
        self.apply_btn.setDefault(True)
        btns.addWidget(self.apply_btn)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setToolTip("Forget the changes not applied yet (made "
                                   "since the last Apply) and close the window")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(self.cancel)
        btns.addWidget(self.cancel_btn)
        self.ok_btn = QtWidgets.QPushButton("OK")
        self.ok_btn.setToolTip("Write the changes not applied yet (one undo "
                               "step) and close the window")
        self.ok_btn.setAutoDefault(False)
        self.ok_btn.clicked.connect(self.ok)
        btns.addWidget(self.ok_btn)
        v.addLayout(btns)
        from .gui_fit import fit_to_screen
        want = 140 + self._cell_w * n
        fit_to_screen(self, min(1500, want), 820 if self._has_cmp else 700,
                      frac_w=0.85, frac_h=0.9)

        self._loading = False
        self._populate()
        self._sync_pending()

    # ---------------------------------------------------- helpers
    def _click_edit(self, index):
        """A click on an editable cell opens its list straight away."""
        if not (1 <= index.column() <= self._nslots):
            return
        it = self.table.item(index.row(), index.column())
        if it is None or not (it.flags() & QtCore.Qt.ItemIsEditable):
            return
        if QtWidgets.QApplication.keyboardModifiers() & (
                QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier):
            return                         # extending a selection
        if self.table.state() == QtWidgets.QAbstractItemView.EditingState:
            return
        self._delegate.popup_next = True
        self.table.edit(index)
        self._delegate.popup_next = False

    def _show_cmp_cols(self, on):
        if self.cmp_table is not None:
            self.cmp_box.setVisible(on)

    def _follow(self, other, r, c):
        """The other table's current cell follows this one's (no loop: it is
        already there the second time); the selection, every selected cell,
        is :func:`link_selection`'s."""
        follow_current(other, r, c)

    # ---------------------------------------------------- held changes
    def _key(self, g, slot):
        return (getattr(g, "group", None) if self._edc15 else None, g.number, slot)

    def _stage(self, edits):
        """Hold ``[(group, slot, id)]`` until Apply; an id back to what the
        file has is no change."""
        for g, slot, val in edits:
            k = self._key(g, slot)
            chans = g.channels()
            if slot < len(chans) and chans[slot] == val:
                self._pending.pop(k, None)
            else:
                self._pending[k] = val
        self._populate()
        self._sync_pending()

    def _sync_pending(self):
        n = len(self._pending) + len(self._pending_desc)
        self.apply_btn.setEnabled(bool(n))
        self.pending_lbl.setText("%d change%s not applied" % (n, "" if n == 1 else "s")
                                 if n else "")

    def has_pending(self) -> bool:
        return bool(self._pending or self._pending_desc)

    def apply(self) -> int:
        """Write every held change as one undo step. Returns how many."""
        if not self.has_pending():
            return 0
        groups = {(getattr(g, "group", None) if self._edc15 else None, g.number): g
                  for g in self.mb.groups()}
        edits = [(groups[(ds, num)], slot, val)
                 for (ds, num, slot), val in sorted(self._pending.items(),
                                                    key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2]))
                 if (ds, num) in groups]
        n = 0
        with self.session.transaction("edit measuring blocks"):
            if edits:
                n += self.mb.set_channels(edits) or len(edits)
            for (ds, idx, fld), (val, every) in self._pending_desc.items():
                self.mb.set_descriptor(ds, idx, fld, val, all_datasets=every)
                n += 1
        self._pending.clear()
        self._pending_desc.clear()
        if self._edc15:
            self._populate_descriptors()
        self._populate()
        self._sync_pending()
        self.detail.setText("Applied %d change%s (one undo step)." % (n, "" if n == 1 else "s"))
        return n

    def _discard(self):
        self._pending.clear()
        self._pending_desc.clear()
        if self._edc15:
            self._populate_descriptors()
        self._populate()
        self._sync_pending()

    def cancel(self):
        """Cancel: forget every change made since the last Apply (or since
        the window opened) and close -- no question: that is what it is
        for."""
        if getattr(self, "mb", None) is not None and self.mb.available:
            self._pending.clear()
            self._pending_desc.clear()
        QtWidgets.QDialog.reject(self)

    def ok(self):
        """OK: write the changes not applied yet, as Apply does, and close."""
        if getattr(self, "mb", None) is not None and self.mb.available:
            self.apply()
        QtWidgets.QDialog.accept(self)

    def _may_close(self) -> bool:
        """Whether the window may go by the title bar's X or Esc: with
        changes not applied, asks to apply or discard them (False when the
        user cancels) -- a stray Esc should not lose a set of edits."""
        if getattr(self, "mb", None) is None or not self.mb.available \
                or not self.has_pending():
            return True
        SB = QtWidgets.QMessageBox.StandardButton
        r = QtWidgets.QMessageBox.question(
            self, "Measuring blocks",
            "Apply the %d change(s) not applied yet?"
            % (len(self._pending) + len(self._pending_desc)),
            SB.Apply | SB.Discard | SB.Cancel, SB.Apply)
        if r == SB.Cancel:
            return False
        if r == SB.Apply:
            self.apply()
        else:
            self._pending.clear()
            self._pending_desc.clear()
        return True

    # Esc and the title bar's X end in done() by reject(). QDialog's own
    # closeEvent calls reject(), so reject() must not call close() back:
    # Qt ignores a close() made while one is in progress, and the window
    # stayed open (the old Close button did nothing). Cancel and OK call
    # QDialog's own reject() / accept(): they need no question.
    def closeEvent(self, ev):
        if not self._may_close():
            ev.ignore()
            return
        super().closeEvent(ev)           # -> reject(): nothing pending now

    def reject(self):
        if self._may_close():
            super().reject()

    def accept(self):
        if self._may_close():
            super().accept()

    def _ds_for(self, g):
        return getattr(g, "group", None) if self._edc15 else None

    def _info(self, sid, g=None):
        """SignalInfo for a cell; on EDC15 from the cell's own dataset."""
        if self._edc15 and g is not None:
            d = self.mb.descriptor(g.group, sid)
            if d is not None:
                from .signals import SignalInfo
                note = ""
                if d.message is not None:
                    note = "message 0x%04X" % d.message
                    if d.read_ram:
                        note += ", RAM 0x%04X" % d.read_ram
                    note += ", display type %s, norm %s" % (d.display, d.norm)
                return SignalInfo(sid, "xcwGrp%02d" % sid, "", note, d.name)
        return self.catalog.info(sid)

    def _cell_text(self, sid, g=None) -> str:
        if sid in EMPTY_IDS:
            return "%d - (empty)" % sid
        info = self._info(sid, g)
        if info.known:
            return "%d - %s" % (sid, _tidy(info.human or info.name))
        return _tidy(info.label())

    def _cmp_cell_text(self, sid: int, cg=None) -> str:
        if sid in EMPTY_IDS:
            return "%d - (empty)" % sid
        if self._edc15 and cg is not None:
            d = self.cmp_mb.descriptor(cg.group, sid)
            if d is not None and d.name:
                return "%d - %s" % (sid, _tidy(d.name))
            return str(sid)
        if self.cmp_catalog is None:
            return str(sid)
        info = self.cmp_catalog.info(sid)
        if info.known:
            return "%d - %s" % (sid, _tidy(info.human or info.name))
        return _tidy(info.label())

    def _translated(self, g, cid):
        """The comparison's id ``cid`` expressed in the primary's terms."""
        if self._edc15:
            return self.mb.translate_from(self.cmp_mb, g.group, cid)
        return self.mb.translate_from(self.cmp_mb, cid,
                                      self.catalog, self.cmp_catalog)

    def _cmp_differs(self, g, cid, mine) -> bool:
        """Does the comparison's cell ``cid`` show something other than the
        primary's ``mine``? Equal ids over the same definition are equal
        whether or not the signal has a name (a bare number, a CAN bus
        signal, an ESB entry with no signal number): only a different id, or
        the same id meaning something else in the other binary, differs."""
        if mine is None:
            return True
        if cid == mine:
            if cid in EMPTY_IDS:
                return False
            if self._edc15:
                return self._translated(g, cid) != mine
            if self._cmp_shares_esb or self.mb.same_entry(self.cmp_mb, cid):
                return False
        return self._translated(g, cid) != mine

    def _matches(self, g, needle: str) -> bool:
        if needle.isdigit():
            blk = getattr(g, "block", g.number)
            return int(needle) == int(blk)
        nl = needle.lower()
        for ch in g.channels():
            if ch in EMPTY_IDS:
                continue
            info = self._info(ch, g)
            if nl in (info.name or "").lower() or nl in (info.human or "").lower():
                return True
        return False

    # ---------------------------------------------------- the table
    def _populate(self):
        self._loading = True
        needle = self.filter.text().strip()
        groups = (self.mb.populated_groups() if self.pop_only.isChecked()
                  else self.mb.groups())
        if self.ds_box is not None and self.ds_box.currentData():
            groups = [g for g in groups if g.group == self.ds_box.currentData()]
        if needle:
            groups = [g for g in groups if self._matches(g, needle)]
        n = self._nslots
        self._cmp_shares_esb = bool(self._has_cmp and not self._edc15
                                    and self.mb.shares_esb(self.cmp_mb))
        self.table.setRowCount(len(groups))
        if self.cmp_table is not None:
            self.cmp_table.setRowCount(len(groups))
        quiet = cmp_fg(self.cmp_table) if self.cmp_table is not None else None
        for r, g in enumerate(groups):
            num = QtWidgets.QTableWidgetItem(str(g.number))
            num.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
            num.setData(QtCore.Qt.UserRole, g)
            self.table.setItem(r, 0, num)
            gnum = getattr(g, "block", g.number)
            gtags = sorted({t for (gg, _s), ts in self._logtags.items() if gg == gnum
                            for t in ts})
            if gtags:
                num.setToolTip("Cells built for logging: %s" % ", ".join(gtags))
            chans = g.channels()
            shown = list(chans)
            for c in range(n):
                if c >= len(chans) or (hasattr(g, "editable") and not g.editable(c)):
                    it = QtWidgets.QTableWidgetItem("—")
                    it.setFlags(QtCore.Qt.ItemIsSelectable)
                    it.setToolTip("No map row for this half of the group")
                    self.table.setItem(r, c + 1, it)
                    continue
                ch = chans[c]
                held = self._pending.get(self._key(g, c))
                if held is not None:
                    shown[c] = held
                cur = ch if held is None else held
                it = QtWidgets.QTableWidgetItem(self._cell_text(cur, g))
                it.setData(QtCore.Qt.UserRole, cur)
                if cur not in EMPTY_IDS:
                    info = self._info(cur, g)
                    tip = _tidy(info.label())
                    if info.note and "intnum=" not in info.note:
                        tip += "\n" + info.note
                    it.setToolTip(tip)
                else:
                    it.setForeground(OFF_FG)
                if held is not None:
                    mark_pending(it, self.table.font())
                    it.setToolTip((it.toolTip() + "\n" if it.toolTip() else "")
                                  + "Not applied yet (the file has %s)"
                                  % self._cell_text(ch, g))
                tag = self._logtags.get((gnum, c))
                if tag:
                    it.setToolTip((it.toolTip() + "\n" if it.toolTip() else "")
                                  + "Built for logging: %s. Changing it takes it out of "
                                  "those presets' optimised blocks." % ", ".join(tag))
                    if held is None:
                        f = it.font()
                        f.setItalic(True)
                        it.setFont(f)
                self.table.setItem(r, c + 1, it)
            if not self._has_cmp:
                continue
            cg = self._cmp_by_num.get(g.number)
            cch = cg.channels() if cg is not None else []
            lab = QtWidgets.QTableWidgetItem(str(g.number))
            lab.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
            lab.setData(QtCore.Qt.UserRole, g)
            self.cmp_table.setItem(r, 0, lab)
            for c in range(n):
                cid = cch[c] if c < len(cch) else None
                it = QtWidgets.QTableWidgetItem(
                    "—" if cid is None else self._cmp_cell_text(cid, cg))
                it.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
                it.setForeground(quiet)
                if cid is not None:
                    it.setData(QtCore.Qt.UserRole, cid)
                    mine = shown[c] if c < len(shown) else None
                    if self._cmp_differs(g, cid, mine):
                        # the outline in both, over a held cell's yellow too
                        mark_differs(it)
                        own = self.table.item(r, c + 1)
                        if own is not None and own.data(QtCore.Qt.UserRole) is not None:
                            mark_differs(own)
                        if own is not None:
                            own.setToolTip((own.toolTip() + "\n" if own.toolTip() else "")
                                           + "Comparison: " + it.text())
                        if self._translated(g, cid) is None:
                            it.setToolTip("This signal has no definition in "
                                          "the primary binary")
                self.cmp_table.setItem(r, c + 1, it)
        self._loading = False
        self._refresh_summary()

    def _load_logtags(self):
        """Which cells Tools > Build logging blocks built, for which presets
        (only those the file still holds)."""
        try:
            from . import branding
            from .datalog.blockpack.service import logging_tags
            ref = getattr(self._mainwin, "project_ref", None)
            self._logtags = logging_tags(self.session, ref.set if ref is not None else None,
                                         QtCore.QSettings(*branding.QSETTINGS))
        except Exception:
            self._logtags = {}

    def refresh_from_session(self):
        """Show edits made outside this window (the grid, undo, a patch,
        another tool) -- called by the tab while this window is open."""
        if not self.mb.available:
            return
        self._load_logtags()
        if self._edc15:
            self.mb.refresh_descriptors()
            self._populate_descriptors()
        elif hasattr(self.mb, "forget_cache"):
            self.mb.forget_cache()
        self._populate()

    def _show_detail(self, table=None):
        table = table if table is not None else self.table
        for it in table.selectedItems():
            if it.column() == 0:
                continue
            head = table.item(it.row(), 0)
            g = head.data(QtCore.Qt.UserRole)
            ch = it.data(QtCore.Qt.UserRole)
            cmp_col = table is not self.table
            slot = it.column() - 1
            where = "Block %s, cell %d" % (g.number, slot + 1)
            if not self._edc15 and not cmp_col and slot >= 4 and g.ext_row is not None:
                where += " (map row %d)" % (g.ext_row + 1)
            if cmp_col:
                where += " - comparison"
            if ch is None:
                self.detail.setText(where + ": no map row for this half of the group.")
                return
            if ch in EMPTY_IDS:
                self.detail.setText("%s: empty (id %d)." % (where, ch))
                return
            info = (self._info(ch, g) if not cmp_col
                    else (self.cmp_catalog.info(ch) if self.cmp_catalog is not None
                          and not self._edc15 else self._info(ch, g)))
            txt = "<b>%s</b>: %s" % (where, _tidy(info.label()))
            if self._edc15:
                if info.note:
                    txt += "<br><span style='color:gray'>%s</span>" % info.note
            else:
                mb = self.cmp_mb if cmp_col else self.mb
                ent = mb.esb_entry(ch)
                if ent is not None:
                    txt += ("<br><span style='color:gray'>ESB index %d: internal "
                            "signal %d, display formula %d, norm %d</span>"
                            % (ent.index, ent.signal, ent.formula, ent.norm))
            self.detail.setText(txt)
            return
        self.detail.setText("Select a cell to see its signal details.")

    def _refresh_summary(self):
        s = self.mb.summary()
        unit = "blocks" if self._edc15 else "groups"
        self.summary.setText(
            "%d %s - %d populated, %d empty%s" % (
                s["groups"], unit, s["populated"], s["empty"],
                "   (unsaved changes)" if self.session.modified else ""))
        if self._edc15:
            self._catalog_note.setText(
                "Cells are named from this binary's own group descriptors.")
            return
        nk = self.catalog.named_count
        src = getattr(self.catalog, "source_family", None)
        if nk and src:
            self._catalog_note.setText(
                "Signal names: %d ids named for family %s (from %s, which "
                "uses the same signal-number table)." % (
                    nk, self.session.family, src))
        elif nk:
            self._catalog_note.setText(
                "Signal names: %d ids named for family %s." % (
                    nk, self.session.family))
        else:
            self._catalog_note.setText(
                "Signal names: not available for this software (family "
                "%s), so cells show the ESB id and its internal signal "
                "number. You can still pick or type an id for any cell."
                % self.session.family)

    def _say_error(self, text):
        self.detail.setText("<span style='color:#c0392b'><b>%s</b></span>" % text)

    def _on_item_changed(self, item):
        # only the primary cells (1..n) are editable; the block column and
        # the read-only comparison columns never write.
        n = self._nslots
        if self._loading or not (1 <= item.column() <= n):
            return
        head = self.table.item(item.row(), 0)
        if head is None:
            return
        g = head.data(QtCore.Qt.UserRole)
        slot = item.column() - 1
        old = self._pending.get(self._key(g, slot), g.channels()[slot])
        head = item.text().strip().split(" ", 1)[0]
        try:
            val = int(head, 0)
        except ValueError:
            try:
                val = int(head)
            except ValueError:
                self._populate()
                return
        if val == old:
            self._populate()               # just re-label; nothing to write
            return
        ok = (self.mb.valid_id(val, g.group) if self._edc15
              else self.mb.valid_id(val))
        if not ok:
            mx = self.mb.max_index
            if self._edc15:
                self._say_error("%d is not a descriptor of dataset %s (use one "
                                "of its xcwGrp ids, or 255 for empty)."
                                % (val, g.group))
            else:
                self._say_error("%d is not a defined ESB entry%s - the ECU would "
                                "read past its tables. Use 255 for an empty cell."
                                % (val, (" (0-%d)" % mx) if mx is not None else ""))
            self._populate()
            return
        edits = [(g, slot, val)]
        if self.all_ds is not None and self.all_ds.isChecked():
            edits += [(sg, slot, val) for sg in self.mb.siblings(g)
                      if self.mb.valid_id(val, sg.group)]
        # held until Apply
        self._stage(edits)
        self._show_detail()

    def _selected_groups(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out, seen = [], set()
        for r in rows:
            it = self.table.item(r, 0)
            if it is None:
                continue
            g = it.data(QtCore.Qt.UserRole)
            k = self._key(g, 0)
            if k not in seen:
                seen.add(k)
                out.append(g)
        return out

    def _clear_sel(self):
        groups = self._selected_groups()
        if not groups:
            return
        edits = []
        for g in groups:
            targets = [g]
            if self.all_ds is not None and self.all_ds.isChecked():
                targets += self.mb.siblings(g)
            for t in targets:
                edits += [(t, s, 255) for s in range(t.slots)
                          if not hasattr(t, "editable") or t.editable(s)]
        self._stage(edits)                 # held until Apply

    def _mb_menu(self, pos):
        groups = self._selected_groups()
        if not groups:
            return
        menu = QtWidgets.QMenu(self)
        ix = self.table.indexAt(pos)
        it = self.table.item(ix.row(), ix.column()) if ix.isValid() else None
        if (it is not None and 1 <= ix.column() <= self._nslots
                and it.flags() & QtCore.Qt.ItemIsEditable):  # (a block's own row)
            ch = menu.addAction("Change this cell...")
            ch.triggered.connect(lambda: self._click_edit(ix))
            menu.addSeparator()
        a = menu.addAction("Clear %d block(s)" % len(groups))
        a.triggered.connect(self._clear_sel)
        if self._has_cmp:
            act = menu.addAction(
                "Copy cells from comparison (%d block(s))" % len(groups))
            act.triggered.connect(lambda: self._copy_mb_sel_from_cmp())
        menu.addSeparator()
        self._add_copy_actions(menu, self.table)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _cmp_menu(self, pos):
        """The read-only comparison's menu: copy its cells, or copy them
        into the primary's (held until Apply, as the button's copy)."""
        if self.cmp_table is None or not self.cmp_table.selectedIndexes():
            return
        menu = QtWidgets.QMenu(self)
        n = len(self._cmp_edits(self._cmp_pick())[0])
        act = menu.addAction("Copy to primary (%d cell(s)%s)" % (
            n, ", every dataset" if self._every_ds() else ""))
        act.setToolTip("Set the primary's matching cells to these signals "
                       "(held until Apply)")
        act.setEnabled(bool(n))
        act.triggered.connect(lambda: self._copy_cmp_cells_to_primary())
        menu.addSeparator()
        self._add_copy_actions(menu, self.cmp_table)
        menu.exec(self.cmp_table.viewport().mapToGlobal(pos))

    def _add_copy_actions(self, menu, table):
        cp = menu.addAction("Copy")
        cp.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Copy))
        cp.triggered.connect(lambda: self._copy_cells(table))
        sa = menu.addAction("Select all")
        sa.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.SelectAll))
        sa.triggered.connect(table.selectAll)

    def _copy_shortcut(self, table):
        """Ctrl+C on ``table`` copies its whole selection (Qt's own copies
        the current cell only)."""
        a = QtGui.QAction(table)
        a.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Copy))
        a.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        a.triggered.connect(lambda: self._copy_cells(table))
        table.addAction(a)

    def _copy_cells(self, table) -> str:
        """The selected cells of ``table`` to the clipboard, tab separated
        (the table editor's Copy)."""
        text = selection_tsv(table)
        if text:
            QtWidgets.QApplication.clipboard().setText(text)
        return text

    def _cmp_selected_slots(self):
        """``{row: [slot, ...]}`` of the comparison's selected cells (a
        selected Block cell stands for every cell of its block)."""
        out = {}
        if self.cmp_table is None:
            return out
        for ix in self.cmp_table.selectedIndexes():
            r, c = ix.row(), ix.column()
            head = self.table.item(r, 0)
            if head is None:
                continue
            g = head.data(QtCore.Qt.UserRole)
            if g is None:
                continue
            slots = out.setdefault(r, set())
            if c == 0:
                slots.update(range(g.slots))
            elif 1 <= c <= min(self._nslots, g.slots):
                slots.add(c - 1)
        return {r: sorted(v) for r, v in out.items() if v}

    def _cmp_pick(self):
        """``[(group, slots)]`` of the comparison's selected cells."""
        return [(self.table.item(r, 0).data(QtCore.Qt.UserRole), slots)
                for r, slots in sorted(self._cmp_selected_slots().items())]

    def _copy_cmp_cells_to_primary(self):
        """The comparison's selected cells into the primary's matching
        cells (the Copy block(s) from comparison path, cell by cell)."""
        return self._copy_mb_sel_from_cmp(self._cmp_pick())

    def _every_ds(self) -> bool:
        """Is "Apply edits to every dataset" ticked (EDC15, several)?"""
        return self.all_ds is not None and self.all_ds.isChecked()

    def _same_in(self, g, idx, sg):
        """The primary's descriptor in ``sg``'s dataset that shows what
        ``idx`` shows in ``g``'s: the same message number, preferring the
        same display type and norm (as the comparison is matched); None when
        that dataset has no descriptor for the message."""
        if idx in EMPTY_IDS:
            return idx
        descs = getattr(self.mb, "descriptors", {}) or {}
        mine = descs.get(getattr(g, "group", None), {}).get(idx)
        theirs = descs.get(getattr(sg, "group", None), {})
        if mine is None or not getattr(mine, "message", None):
            return None
        key = (mine.message, mine.display, mine.norm)
        d = theirs.get(idx)
        if d is not None and (d.message, d.display, d.norm) == key:
            return idx
        exact = [i for i, x in sorted(theirs.items())
                 if (x.message, x.display, x.norm) == key]
        same = [i for i, x in sorted(theirs.items()) if x.message == mine.message]
        pick = exact or same
        return pick[0] if pick else None

    def _cmp_edits(self, pick):
        """``(edits, lost)`` for copying the comparison's signals into
        ``pick`` (``[(group, slots or None)]``): ``edits`` the cells that
        change (``[(group, slot, id)]``), ``lost`` the cells left as they are
        because the primary has no definition for that signal. With "Apply
        edits to every dataset" ticked each cell also goes into the same
        block of every other dataset, matched by meaning there (as a cell
        edit does), unless that dataset's block is picked itself."""
        picked = {}
        for g, slots in pick:
            picked[self._key(g, 0)[:2]] = slots
        edits, lost, seen = [], 0, set()

        def add(t, slot, val):
            k = self._key(t, slot)
            if k in seen:
                return
            chans = t.channels()
            cur = self._pending.get(k, chans[slot] if slot < len(chans) else None)
            if val != cur:
                seen.add(k)
                edits.append((t, slot, val))
        for g, slots in pick:
            cg = self._cmp_by_num.get(g.number)
            if cg is None:
                continue
            cch = cg.channels()
            for slot in range(min(len(cch), g.slots)):
                if slots is not None and slot not in slots:
                    continue
                if hasattr(g, "editable") and not g.editable(slot):
                    continue
                want = self._translated(g, cch[slot])
                if want is None:
                    lost += 1
                    continue
                add(g, slot, want)
                if not self._every_ds():
                    continue
                for sg in self.mb.siblings(g):
                    own = picked.get(self._key(sg, 0)[:2], False)
                    if own is None or (own is not False and slot in own):
                        continue        # copied from its own comparison row
                    if slot >= sg.slots:
                        continue
                    val = self._same_in(g, want, sg)
                    if val is not None and self.mb.valid_id(val, sg.group):
                        add(sg, slot, val)
        return edits, lost

    def _copy_mb_sel_from_cmp(self, pick=None):
        """Hold the comparison's signals for ``pick`` (``[(group, slots or
        None)]``; the selected blocks, every cell, by default) until Apply
        (every dataset's block too when that box is ticked)."""
        if not self._has_cmp:
            return 0
        if pick is None:
            pick = [(g, None) for g in self._selected_groups()]
        edits, lost = self._cmp_edits(pick)
        self._stage(edits)                 # held until Apply
        msg = ("Copied %d cell(s) from the comparison%s; Apply writes them."
               % (len(edits), " (every dataset)" if self._every_ds() else ""))
        if lost:
            msg += (" %d cell(s) were left as they are: the primary binary has "
                    "no definition for that signal." % lost)
        self.detail.setText(msg)
        return len(edits)

    def _open_ref(self):
        e = self.ref_box.currentData()
        win = self._mainwin
        if e is not None and win is not None and hasattr(win, "show_entity"):
            win.show_entity(e)

    # ------------------------------------------- EDC15 descriptors
    _DESC_COLS = ["Dataset", "Id", "Reads", "Message", "Display type", "Norm"]
    _DESC_FIELD = {3: "M", 4: "A", 5: "N"}

    def _build_descriptor_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        note = QtWidgets.QLabel(
            "A descriptor is what a block cell points at: the message number "
            "picks the value the ECU reads, the display type and norm tell the "
            "scan tool how to scale it (e.g. a bigger MAP sensor needs the boost "
            "descriptors rescaled). Double-click a number to change it; hex "
            "such as 0x0F10 is accepted.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        lay.addWidget(note)
        self.desc_tbl = QtWidgets.QTableWidget(0, len(self._DESC_COLS))
        self.desc_tbl.setHorizontalHeaderLabels(self._DESC_COLS)
        self.desc_tbl.verticalHeader().setVisible(False)
        self.desc_tbl.setAlternatingRowColors(True)
        self.desc_tbl.setItemDelegate(CellDelegate(self.desc_tbl))
        dh = self.desc_tbl.horizontalHeader()
        dh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        dh.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.desc_tbl.itemChanged.connect(self._on_desc_changed)
        lay.addWidget(self.desc_tbl, 1)
        self._populate_descriptors()
        return w

    def _populate_descriptors(self):
        if not getattr(self, "desc_tbl", None):
            return
        self._loading = True
        want_ds = self.ds_box.currentData() if self.ds_box is not None else None
        rows = []
        for ds, desc in self.mb.descriptors.items():
            if want_ds and ds != want_ds:
                continue
            for i, d in sorted(desc.items()):
                rows.append((ds, i, d))
        self.desc_tbl.setRowCount(len(rows))
        held = self._pending_desc
        for r, (ds, i, d) in enumerate(rows):
            msg = held.get((ds, i, "M"), (d.message or 0,))[0]
            vals = [ds, "%d" % i, _tidy(d.name or ""), "0x%04X" % msg,
                    "%s" % held.get((ds, i, "A"), (d.display,))[0],
                    "%s" % held.get((ds, i, "N"), (d.norm,))[0]]
            for c, t in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(t)
                it.setData(QtCore.Qt.UserRole, (ds, i))
                if c in self._DESC_FIELD and (ds, i, self._DESC_FIELD[c]) in held:
                    mark_pending(it, self.desc_tbl.font())
                    it.setToolTip("Not applied yet")
                if c not in self._DESC_FIELD or d.entities.get(self._DESC_FIELD[c]) is None:
                    it.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
                self.desc_tbl.setItem(r, c, it)
        self._loading = False

    def _on_desc_changed(self, item):
        if self._loading or item.column() not in self._DESC_FIELD:
            return
        ds, idx = item.data(QtCore.Qt.UserRole)
        fld = self._DESC_FIELD[item.column()]
        try:
            val = int(item.text().strip(), 0)
        except ValueError:
            self._populate_descriptors()
            return
        hi = 0xFFFF if fld == "M" else 0xFF
        if not 0 <= val <= hi:
            self._say_error("%s must be 0-%d." % (self._DESC_COLS[item.column()], hi))
            self._populate_descriptors()
            return
        every = self.all_ds is not None and self.all_ds.isChecked()
        self._pending_desc[(ds, idx, fld)] = (val, every)    # held until Apply
        self._populate_descriptors()
        self._sync_pending()

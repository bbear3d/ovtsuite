# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Differences window: what differs between two binaries, and the patch
made of it, in one window.

It was two -- **Differences** (a list to read and jump from) and **Generate
Patch** (a checklist saved as a patch) -- each with its own way of counting
what changed, so a table could show one count in one and another in the
other. Now there is one difference (:func:`ovtsuite.patch.diff_entities`,
counted by :func:`summarize`) shown two ways:

* **List**: every parameter that differs, with its group, how many cells
  changed and the first change; double-click opens it in the editor behind.
* **Make patch**: the same rows with a check box each, the direction (whose
  values the patch carries; **Swap**), **Add tables...** and the whole /
  as-found choice (:mod:`ovtsuite.gui_patchtables`), **Save patch...**.

Raw byte ranges are rows of the same table: undefined calibration data (in a
patch, unchecked by default; address-literal) and, ticked under **Show**, the
program/boot area, the MPC and the EEPROM (shown only: a patch does not carry
them). **Compare** picks the two sides: the tab and its comparison, or two
versions of the tab's own history (what *Generate patch from history* was).
"""
from __future__ import annotations

import html
import logging
import os
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtWidgets

from .gui_cellmarks import CellDelegate

CHECKED = QtCore.Qt.CheckState.Checked
UNCHECKED = QtCore.Qt.CheckState.Unchecked

_log = logging.getLogger(__name__)

LIST, PATCH = "list", "patch"

# columns
C_CHECK, C_NAME, C_CHANGED, C_FIRST, C_GROUP, C_GRADE = range(6)
HEADERS = ["", "Parameter", "Cells changed", "First change", "Group", "Grade"]


def summarize(e, cp) -> Tuple[Optional[int], str]:
    """``(cells changed, first change)`` between ``e`` and its counterpart
    ``cp`` -- the one count both views show. Cells are paired the way the
    editor pairs them (a run stored as a row against one stored as a
    column is the same run), compared as decoded values (two NaNs equal);
    ``None`` cells changed when the two cannot be paired at all."""
    from . import gui as _g
    from .model import values_equal
    if e.kind == "constant" or cp.kind == "constant":
        a, b = e.cell(0, 0), cp.cell(0, 0)
        return (0 if values_equal(a, b, 1e-9) else 1), "%s → %s" % (_g._fmt(a), _g._fmt(b))
    pairs = _g._cell_pairs(e, cp)
    if pairs is None:
        return None, "%s vs %s" % (_g._shape_text(e), _g._shape_text(cp))
    run = _g._is_run(e)
    n, first = 0, ""
    for (r, c), (r2, c2) in pairs:
        a, b = e.cell(r, c), cp.cell(r2, c2)
        if not values_equal(a, b, 1e-9):
            n += 1
            if not first:
                where = "[%d]" % (r * e.cols + c) if run else "[%d,%d]" % (r, c)
                first = "%s %s → %s" % (where, _g._fmt(a), _g._fmt(b))
    axes = _g._axis_changes(e, cp)
    if axes:
        ax, i, a, b = axes[0]
        note = "%s axis[%d] %s → %s" % (ax.upper(), i, _g._fmt(a), _g._fmt(b))
        if len(axes) > 1:
            note += " (+%d more axis point(s))" % (len(axes) - 1)
        first = "%s; %s" % (first, note) if first else note
    return n, first


def _entities(session) -> list:
    from . import patch as _patch
    return list(session.entities) + _patch._variants(session)


class DifferencesWindow(QtWidgets.QDialog):
    """``primary`` -> ``comparison``: a patch made here carries the
    comparison's values (``PatchEntry.values``), so applying it makes a
    binary like ``primary`` into ``comparison``.

    ``mode``: ``"list"`` or ``"patch"``. ``ws``: the tab it was opened from
    (double-click jumps there; **Compare** offers its history). ``solo``: no
    comparison -- a patch of whole tables of ``comparison`` only.
    ``preselect``: parameters of ``comparison`` to put in whole.
    ``on_swap`` is accepted for older callers; Swap now works in place."""

    def __init__(self, primary, comparison, parent=None, on_swap=None,
                 solo=False, preselect=(), mode=PATCH, ws=None, history=None):
        super().__init__(parent)
        from . import gui as _g
        from . import patch as _patch
        self._patch = _patch
        self.ws = ws if ws is not None else (parent if hasattr(parent, "show_entity") else None)
        self.solo = bool(solo)
        self.mode = PATCH if self.solo else mode
        self.primary = primary
        self.comparison = comparison
        self._entries: list = []
        self._gaps: List[Tuple[int, int]] = []
        self._raw: List[Tuple[str, int, int]] = []
        self._rows: List[Tuple[str, int]] = []
        self._gap_on: set = set()
        self._history_target = history
        self._given = (primary, comparison)
        self._valid = True                  # False: the history pair picked is not usable
        self._fit = True                    # fit the columns to the next fill
        self._vcache: dict = {}             # version id -> session (this window's lifetime)
        self._vcache_key: tuple = ()        # the history the cache was built from
        self.setWindowIcon(_g.app_icon())
        self.resize(900, 640)

        v = QtWidgets.QVBoxLayout(self)
        # -- mode and source
        top = QtWidgets.QHBoxLayout()
        self.list_btn = QtWidgets.QPushButton("List")
        self.patch_btn = QtWidgets.QPushButton("Make patch")
        for b, m in ((self.list_btn, LIST), (self.patch_btn, PATCH)):
            b.setCheckable(True)
            b.clicked.connect(lambda _c=False, m=m: self.set_mode(m))
            top.addWidget(b)
        top.addSpacing(12)
        self.source_lbl = QtWidgets.QLabel("Compare:")
        self.source = QtWidgets.QComboBox()
        self.source.currentIndexChanged.connect(self._source_changed)
        top.addWidget(self.source_lbl)
        top.addWidget(self.source, 1)
        v.addLayout(top)
        # -- history versions (Compare: two versions of the history)
        self.hist_row = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(self.hist_row)
        hl.setContentsMargins(0, 0, 0, 0)
        self.from_box = QtWidgets.QComboBox()
        self.to_box = QtWidgets.QComboBox()
        hl.addWidget(QtWidgets.QLabel("From version:"))
        hl.addWidget(self.from_box, 1)
        hl.addWidget(QtWidgets.QLabel("To version:"))
        hl.addWidget(self.to_box, 1)
        v.addWidget(self.hist_row)
        # -- show filters
        show = QtWidgets.QHBoxLayout()
        show.addWidget(QtWidgets.QLabel("Show:"))
        self.blk_cal = QtWidgets.QCheckBox("Calibration")
        self.blk_cal.setChecked(True)
        self.blk_prog = QtWidgets.QCheckBox("Program/boot")
        self.blk_mpc = QtWidgets.QCheckBox("MPC / internal flash")
        self.blk_eeprom = QtWidgets.QCheckBox("EEPROM")
        self.excl_checksum_chk = QtWidgets.QCheckBox("Skip checksum bytes")
        self.excl_checksum_chk.setToolTip(
            "Checksum, digest and signature bytes recompute on their own whenever "
            "anything else in their block changes, so a difference there alone "
            "is not meaningful")
        self.excl_checksum_chk.setChecked(True)
        for chk in (self.blk_cal, self.blk_prog, self.blk_mpc, self.blk_eeprom):
            chk.toggled.connect(self._show_changed)
            show.addWidget(chk)
        self.excl_checksum_chk.toggled.connect(self._checksum_changed)
        show.addStretch(1)
        show.addWidget(self.excl_checksum_chk)
        v.addLayout(show)
        # -- direction (patch)
        way = QtWidgets.QHBoxLayout()
        self.direction = QtWidgets.QLabel()
        self.direction.setWordWrap(True)
        way.addWidget(self.direction, 1)
        self.swap_btn = QtWidgets.QPushButton("Swap direction")
        self.swap_btn.setToolTip("Make the patch the other way round")
        self.swap_btn.clicked.connect(self.swap)
        way.addWidget(self.swap_btn)
        self.add_btn = QtWidgets.QPushButton("Add tables...")
        self.add_btn.clicked.connect(self._add_tables)
        way.addWidget(self.add_btn)
        from . import gui_relpatch as _rel             # Carry as: values / change
        _rel.install_carry_button(self, way)
        v.addLayout(way)
        self.head_lbl = QtWidgets.QLabel()
        self.head_lbl.setWordWrap(True)
        v.addWidget(self.head_lbl)
        # -- the table: the app's look (gui_cellmarks)
        self.tbl = QtWidgets.QTableWidget(0, len(HEADERS))
        self.tbl.setHorizontalHeaderLabels(HEADERS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setItemDelegate(CellDelegate(self.tbl))
        self.tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._rows_menu)
        self.tbl.cellDoubleClicked.connect(self._open)
        self.tbl.horizontalHeader().setSectionResizeMode(
            C_NAME, QtWidgets.QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.tbl, 1)
        # -- buttons
        btns = QtWidgets.QHBoxLayout()
        self.all_btn = QtWidgets.QPushButton("Check all")
        self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.none_btn = QtWidgets.QPushButton("Check none")
        self.none_btn.clicked.connect(lambda: self._set_all(False))
        self.copy_btn = QtWidgets.QPushButton("Copy list")
        self.copy_btn.clicked.connect(self._copy)
        for b in (self.all_btn, self.none_btn, self.copy_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        self.save_btn = QtWidgets.QPushButton("Save patch...")
        self.save_btn.clicked.connect(self._save)
        btns.addWidget(self.save_btn)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        btns.addWidget(close)
        v.addLayout(btns)

        self._fill_sources()
        self._reload()
        if preselect:
            self.add_whole(list(preselect))
        self.set_mode(self.mode)

    # ============================================================ sources
    @classmethod
    def from_history(cls, target, parent=None, mode=PATCH):
        """Compare two versions of ``target``'s own history (the original as
        read, or as first opened, to the live state by default)."""
        from . import history as _history
        from . import patch as _patch
        try:
            older, newer = _patch.sessions_from_history(target, _history.ORIGINAL)
        except ValueError:
            older = newer = target
        return cls(older, newer, parent, mode=mode, history=target)

    def _versions(self):
        from . import history as _history
        target = self._history_target
        return _history.list_versions(target) if target is not None else []

    def _original_label(self, versions):
        ref = getattr(self.ws, "project_ref", None)
        if ref is not None:
            try:
                s = ref.set
                head = s.branch(ref.bid).head
                if len(list(s.lineage(head))) - 1 == len(
                        [v for v in versions if not v.get("pending")]):
                    return "The original as read"
            except Exception:                           # noqa: BLE001
                pass
            return "Before the oldest version listed"
        return "As first opened (before the oldest version)"

    def _fill_sources(self):
        """What **Compare** offers: the pair given (the tab and its
        comparison, when that is what it is) and two versions of the tab's
        history; opened on the history, that is chosen."""
        from . import history as _history
        ws = self.ws
        on_history = self._history_target is not None
        if self._history_target is None and ws is not None:
            self._history_target = getattr(ws, "primary", None)
        self.source.blockSignals(True)
        self.source.clear()
        cmp_ = getattr(ws, "comparison", None) if ws is not None else None
        if self.solo:
            self.source.addItem(os.path.basename(self.comparison.binfile.name), "solo")
        elif not on_history:
            pair = {id(self.primary), id(self.comparison)}
            if cmp_ is not None and pair == {id(cmp_), id(ws.primary)}:
                self.source.addItem("This tab and its comparison (%s)"
                                    % os.path.basename(cmp_.binfile.name), "comparison")
            else:
                self.source.addItem("%s and %s" % (
                    os.path.basename(self.primary.binfile.name),
                    os.path.basename(self.comparison.binfile.name)), "given")
        if self._history_target is not None and not self.solo:
            versions = self._versions()
            self.source.addItem("Two versions of this file's history", "history")
            for box in (self.from_box, self.to_box):
                box.blockSignals(True)
                box.clear()
            self.from_box.addItem(self._original_label(versions), _history.ORIGINAL)
            for ver in reversed(versions):                       # oldest first
                label = "%s -- %s%s" % (ver.get("ts", "?"), _history.display_label(ver),
                                        "  (pending, unsaved)" if ver.get("pending") else "")
                self.from_box.addItem(label, ver["id"])
                self.to_box.addItem(label, ver["id"])
            self.to_box.addItem("Current (live, unsaved edits included)", None)
            self.to_box.setCurrentIndex(self.to_box.count() - 1)
            for box in (self.from_box, self.to_box):
                box.blockSignals(False)
                box.currentIndexChanged.connect(lambda _i: self._history_changed())
            if on_history:
                self.source.setCurrentIndex(self.source.findData("history"))
        self.source.blockSignals(False)
        self._sync_source_ui()

    def _sync_source_ui(self):
        self.hist_row.setVisible(self.source.currentData() == "history")
        many = self.source.count() > 1
        self.source.setEnabled(many)

    def _source_changed(self, _i):
        self._sync_source_ui()
        kind = self.source.currentData()
        ws = self.ws
        if kind == "comparison" and ws is not None and ws.comparison is not None:
            self._load(ws.comparison, ws.primary)
        elif kind == "given":
            self._load(*self._given)
        elif kind == "history":
            self._history_changed()

    def _history_changed(self):
        if self.source.currentData() != "history":
            return
        older_id, newer_id = self.from_box.currentData(), self.to_box.currentData()
        if older_id == newer_id:
            self._invalid("<b>Pick two different versions.</b>")
            return
        try:
            older = self._version_session(older_id)
            newer = self._history_target if newer_id is None \
                else self._version_session(newer_id)
        except ValueError as exc:
            self._invalid(html.escape(str(exc)))
            return
        self._load(older, newer)

    def _invalid(self, text):
        """No usable pair: an empty table (nothing to carry) and Save off
        until a valid pair is chosen."""
        target = self._history_target
        self._load(target, target, valid=False)
        self.head_lbl.setText(text)

    def _version_session(self, vid):
        """The history version ``vid`` as a session, built once per window
        (a full load when its size differs from the file's); dropped when
        the history changes. ``ValueError`` for a version that is gone."""
        from . import history as _history
        key = tuple((v.get("id"), v.get("ts"), bool(v.get("pending")))
                    for v in self._versions())
        if key != self._vcache_key:
            self._vcache, self._vcache_key = {}, key
        hit = self._vcache.get(vid)
        if hit is None:
            target = self._history_target
            hit = self._patch._session_from_bytes(
                _history.bytes_at_version(target, vid), target)
            self._vcache[vid] = hit
        return hit

    # ============================================================== data
    def _load(self, primary, comparison, valid=True):
        self.primary, self.comparison = primary, comparison
        self._valid = valid
        self.save_btn.setEnabled(valid)
        self._reload()

    def _checksum_changed(self, _on=None):
        """The checksum filter changes only the byte-range rows, not the
        parameters: keep the ticks, whole and added tables, and redo those."""
        self._sync_checks()
        if not self.solo and self.primary is not self.comparison:
            old = {self._gaps[j][0] for j in self._gap_on if j < len(self._gaps)}
            excl = self.excl_checksum_chk.isChecked()
            try:
                self._gaps = self._patch.diff_gaps(self.primary, self.comparison,
                                                   exclude_checksums=excl)
            except TypeError:
                self._gaps = self._patch.diff_gaps(self.primary, self.comparison)
            self._gap_on = {j for j, g in enumerate(self._gaps) if g[0] in old}
            self._raw = self._raw_rows()
        self._populate()

    def _reload(self):
        """Diff the two sides again and fill the table."""
        self.__dict__.pop("_as_found", None)
        self.__dict__.pop("_values_of", None)          # gui_relpatch.carry_rows
        self._gap_on = set()
        self._fit = True
        if self.solo or self.primary is self.comparison:
            self._entries = [e for e in self._entries if getattr(e, "whole", False)] \
                if self.solo else []
            self._gaps, self._raw = [], []
        else:
            excl = self.excl_checksum_chk.isChecked()
            self._entries = self._summarized(self._patch.diff_entities(self.primary, self.comparison))
            try:
                self._gaps = self._patch.diff_gaps(self.primary, self.comparison,
                                                   exclude_checksums=excl)
            except TypeError:
                self._gaps = self._patch.diff_gaps(self.primary, self.comparison)
            self._raw = self._raw_rows()
        self._populate()
        self._titles()

    def _summarized(self, entries):
        """The entries counted by :func:`summarize` (one count for both
        views); a pair that only looked different in shape (a run stored
        the other way round, the same values) drops out."""
        by_name = {e.name: e for e in _entities(self.primary)}
        out = []
        for pe in entries:
            e = by_name.get(pe.name)
            cp = self.primary.counterpart(self.comparison, e) if e is not None else None
            if e is not None and cp is not None:
                try:
                    n, first = summarize(e, cp)
                except (ValueError, TypeError, IndexError, KeyError,
                        AttributeError, ArithmeticError) as exc:
                    # keep the diff's own count; both views read this one table
                    _log.warning("Differences: could not count %s (%s: %s); "
                                 "showing the diff's own count", pe.name,
                                 type(exc).__name__, exc)
                    n, first = pe.changed_cells, pe.first_change
                if n == 0 and not first:
                    continue
                pe.changed_cells, pe.first_change = n, first
            out.append(pe)
        return out

    def _raw_rows(self):
        wanted = []
        P = self._patch
        if self.blk_prog.isChecked():
            wanted.append(P.PROGRAM_BLOCK)
        if self.blk_mpc.isChecked():
            wanted.append(P.MPC_BLOCK)
        if self.blk_eeprom.isChecked():
            wanted.append(P.EEPROM_BLOCK)
        if not wanted:
            return []
        try:
            return P.diff_outside_calibration(
                self.primary, self.comparison, blocks=wanted,
                exclude_checksums=self.excl_checksum_chk.isChecked())
        except Exception:                               # noqa: BLE001
            return []

    def _show_changed(self, _on=None):
        self._sync_checks()
        if not self.solo and self.primary is not self.comparison:
            self._raw = self._raw_rows()
        self._populate()

    # ============================================================= table
    def _sync_checks(self, skip_entry=None):
        """The table's check boxes back into the entries and gap set
        (``skip_entry``: an entry just replaced, whose row is stale)."""
        for row, (kind, k) in enumerate(self._rows):
            if kind == "entry" and k == skip_entry:
                continue
            it = self.tbl.item(row, C_CHECK)
            if it is None:
                continue
            on = it.checkState() == CHECKED
            if kind == "entry":
                self._entries[k].selected = on
            elif kind == "gap":
                (self._gap_on.add if on else self._gap_on.discard)(k)

    def _populate(self):
        cal = self.blk_cal.isChecked() or self.mode == PATCH
        self._rows = [("entry", i) for i in range(len(self._entries))]
        if cal:
            self._rows += [("gap", j) for j in range(len(self._gaps))]
        self._rows += [("raw", k) for k in range(len(self._raw))]
        self.tbl.setRowCount(len(self._rows))
        for row, (kind, k) in enumerate(self._rows):
            self._fill(row, kind, k)
            self.tbl.setRowHidden(row, kind == "entry" and not cal)
        if self._fit:
            self._fit = False
            self.tbl.resizeColumnsToContents()
        self._heading()

    def _item(self, row, col, text, align_right=False):
        it = QtWidgets.QTableWidgetItem(text)
        if align_right:
            it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight
                                | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.tbl.setItem(row, col, it)
        return it

    def _check(self, row, on, enabled=True, tip=""):
        it = QtWidgets.QTableWidgetItem()
        flags = QtCore.Qt.ItemFlag.ItemIsEnabled
        if enabled:
            flags |= QtCore.Qt.ItemFlag.ItemIsUserCheckable
            it.setCheckState(CHECKED if on else UNCHECKED)
        it.setFlags(flags)
        if tip:
            it.setToolTip(tip)
        self.tbl.setItem(row, C_CHECK, it)

    def _fill(self, row, kind, k):
        from . import gui as _g
        if kind == "entry":
            e = self._entries[k]
            self._check(row, e.selected)
            if getattr(e, "whole", False):
                ch = ("whole table" if e.changed_cells is None
                      else "%d/%d, whole table" % (e.changed_cells, e.total_cells))
            elif e.changed_cells is None:
                ch = "structure differs"
            else:
                ch = "%d/%d" % (e.changed_cells, e.total_cells)
            if getattr(e, "changed_axes", 0):
                ch += " + %d axis" % e.changed_axes
            from . import gui_relpatch as _rel
            ch = _rel.changed_cell_text(e) or ch
            self._item(row, C_NAME, e.title())
            self._item(row, C_CHANGED, ch, True)
            self._item(row, C_FIRST, e.first_change)
            self._item(row, C_GROUP, _g.tier_name(e.tier) if getattr(e, "tier", None) else "")
            self._item(row, C_GRADE, e.source_grade)
        elif kind == "gap":
            s, end = self._gaps[k]
            self._check(row, k in self._gap_on,
                        tip="Address-literal: only for a target of the same family")
            a = bytes(self.primary.binfile.data[s:s + 1]).hex()
            b = bytes(self.comparison.binfile.data[s:s + 1]).hex()
            self._item(row, C_NAME, "Undefined data @ 0x%X" % s)
            self._item(row, C_CHANGED, "%d bytes" % (end - s), True)
            self._item(row, C_FIRST, "@0x%X: %s → %s" % (s, a, b))
            self._item(row, C_GROUP, "raw bytes")
            self._item(row, C_GRADE, "address")
        else:
            label, addr, length = self._raw[k]
            self._check(row, False, enabled=False,
                        tip="Shown only: a patch does not carry this area")
            a = bytes(self.primary.binfile.data)
            b = bytes(self.comparison.binfile.data)
            first = "@0x%X: %s → %s" % (
                addr, a[addr:addr + 1].hex() if addr < len(a) else "??",
                b[addr:addr + 1].hex() if addr < len(b) else "??")
            self._item(row, C_NAME, "%s @ 0x%X" % (label, addr))
            self._item(row, C_CHANGED, "%d bytes" % length, True)
            self._item(row, C_FIRST, first)
            self._item(row, C_GROUP, "raw bytes")
            self._item(row, C_GRADE, "")

    def _fill_row(self, i, _e=None):
        """Row of entry ``i`` again."""
        if not 0 <= i < len(self._entries):
            return
        self._sync_checks(skip_entry=i)
        for row, (kind, k) in enumerate(self._rows):
            if kind == "entry" and k == i:
                self._fill(row, kind, k)
                return

    def _heading(self):
        found = self.__dict__.get("_as_found", {})
        n_whole = sum(1 for e in self._entries if getattr(e, "whole", False)
                      and e.name not in found)
        n_found = len(self._entries) - n_whole
        if self.solo:
            text = ("<b>%d table(s).</b> Use <b>Add tables...</b> to put more in."
                    % len(self._entries))
        elif self.mode == LIST:
            text = "<b>%d parameter(s) differ%s.</b> Double-click a row to open it in the editor." % (
                n_found, ", %d raw byte range(s)" % (len(self._gaps) + len(self._raw))
                if (self._gaps or self._raw) else "")
        else:
            text = ("<b>%d parameter(s) differ.</b> A patch is applied by parameter "
                    "identity, not by address, so it can target a binary of another "
                    "layout -- check the ones to carry." % n_found)
            if n_whole:
                text += " <b>%d table(s)</b> added whole." % n_whole
            if self._gaps:
                text += (" %d undefined-data range(s) differ too (unchecked: "
                         "address-literal)." % len(self._gaps))
        self.head_lbl.setText(text)

    def _titles(self):
        p, c = self.primary.binfile.name, self.comparison.binfile.name
        if self.solo:
            self.setWindowTitle("Create Patch from Tables — %s" % c)
            self.direction.setText(
                "Whole tables from <b>%s</b>: applying the patch sets them in the "
                "target to this file's values." % html.escape(c))
        else:
            self.setWindowTitle(("Differences — %s vs %s" if self.mode == LIST
                                 else "Generate Patch — %s to %s") % (p, c))
            self.direction.setText(
                "Carries <b>%s</b>'s values over <b>%s</b>: applying the patch "
                "makes a binary like <b>%s</b> into <b>%s</b>."
                % tuple(html.escape(x) for x in (c, p, p, c)))
        self.add_btn.setToolTip(
            "Put tables in the patch whole -- every cell and breakpoint, changed or "
            "not -- with their values from %s." % c)

    # ============================================================== mode
    def set_mode(self, mode):
        self._sync_checks()
        self.mode = PATCH if self.solo else mode
        patch = self.mode == PATCH
        self.list_btn.setChecked(not patch)
        self.patch_btn.setChecked(patch)
        for w in (self.list_btn, self.patch_btn):
            w.setVisible(not self.solo)
        self.source_lbl.setVisible(not self.solo)
        self.source.setVisible(not self.solo)
        self.blk_cal.setEnabled(not patch)
        for w in (self.direction, self.add_btn, self.all_btn, self.none_btn, self.save_btn,
                  self.carry_btn):
            w.setVisible(patch)
        self.swap_btn.setVisible(patch and not self.solo)
        self.tbl.setColumnHidden(C_CHECK, not patch)
        self.tbl.setColumnHidden(C_GRADE, not patch)
        self.tbl.setToolTip("Right-click rows to take them whole (every breakpoint "
                            "too) or back as found" if patch else
                            "Double-click a row to open it in the editor")
        self._populate()
        self._titles()

    def swap(self):
        """The patch the other way round, in this window."""
        self._load(self.comparison, self.primary, valid=self._valid)

    # =========================================================== actions
    def _set_all(self, on):
        for row, (kind, _k) in enumerate(self._rows):
            it = self.tbl.item(row, C_CHECK)
            if kind != "raw" and it is not None and not self.tbl.isRowHidden(row):
                it.setCheckState(CHECKED if on else UNCHECKED)

    # kept for older callers (the window's own Check all / none)
    @staticmethod
    def _set_all_tbl(tbl, checked):
        for r in range(tbl.rowCount()):
            it = tbl.item(r, 0)
            if it is not None and it.flags() & QtCore.Qt.ItemFlag.ItemIsUserCheckable:
                it.setCheckState(CHECKED if checked else UNCHECKED)

    def _open(self, row, _col):
        if self.mode != LIST:           # Make patch: the editor behind is not reachable
            return
        if not (0 <= row < len(self._rows)) or self._rows[row][0] != "entry":
            return
        ws = self.ws
        if ws is None or getattr(ws, "primary", None) is None:
            return
        pe = self._entries[self._rows[row][1]]
        e = self._patch.find_entity(ws.primary, pe.name, getattr(pe, "coding", ""))
        if e is None:
            return
        ws.show_entity(e)
        ws.raise_()
        ws.activateWindow()

    def _copy(self):
        cols = [c for c in range(1, len(HEADERS)) if not self.tbl.isColumnHidden(c)]
        lines = ["\t".join(HEADERS[c] for c in cols)]
        for r in range(self.tbl.rowCount()):
            if self.tbl.isRowHidden(r):
                continue
            lines.append("\t".join((self.tbl.item(r, c).text() if self.tbl.item(r, c) else "")
                                   for c in cols))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))

    # ------------------------------------------------ whole / as found
    def _add_tables(self):
        from . import gui_patchtables as _gpt
        src = self.comparison
        ents = _entities(src)
        self._sync_checks()
        found = {} if self.solo else _gpt.values_names(self.primary, src, self._entries)
        got = _gpt.pick(
            self, ents, title="Add tables to the patch",
            taken={e.name for e in self._entries if e.name and getattr(e, "whole", False)},
            partial={n: _gpt.changed_note(self._entries[k]) for n, k in found.items()},
            intro="Tables are put in whole, with their values from <b>%s</b>. One "
                  "already in the patch as found changed goes in whole, "
                  "breakpoints and all." % html.escape(src.binfile.name))
        if got:
            self.add_whole(got)

    def _set_whole(self, row, whole):
        """Entry ``row`` taken whole (every breakpoint written on apply) or
        back as found changed. False when it cannot be."""
        from . import gui_patchtables as _gpt
        cur = self._entries[row]
        if getattr(cur, "relative", False):
            return False                # carried as a change: Carry as > Values first
        if bool(getattr(cur, "whole", False)) == whole:
            return True
        stash = self.__dict__.setdefault("_as_found", {})
        if whole:
            new = _gpt.whole_from_diff(self.primary, self.comparison, cur)
            if new is None:
                return False
            new.changed_cells = cur.changed_cells if new.changed_cells is None else new.changed_cells
            stash[new.name] = cur
        else:
            new = stash.pop(cur.name, None)
            if new is None:
                return False            # added whole: never found changed
        new.selected = True
        self._entries[row] = new
        self._fill_row(row, new)
        return True

    def _rows_menu(self, pos):
        if self.mode != PATCH:
            return
        hit = self.tbl.rowAt(pos.y())
        sel = {i.row() for i in self.tbl.selectedIndexes()}
        rows = sorted(sel if hit in sel else ({hit} - {-1}))
        rows = [r for r in rows if r < len(self._rows) and self._rows[r][0] == "entry"]
        if not rows:
            return
        stash = self.__dict__.get("_as_found", {})
        m = QtWidgets.QMenu(self)
        w = m.addAction("Take whole (every cell and breakpoint)")
        w.setEnabled(not self.solo and any(
            not getattr(self._entries[r], "whole", False) for r in rows))
        f = m.addAction("As found (breakpoints only where changed)")
        f.setEnabled(any(getattr(self._entries[r], "whole", False)
                         and self._entries[r].name in stash for r in rows))
        got = m.exec(self.tbl.viewport().mapToGlobal(pos))
        if got is None:
            return
        for r in rows:
            self._set_whole(r, got is w)
        self._heading()

    def add_whole(self, ents):
        """Put parameters of the values file in whole; one already in the
        list as found changed is made whole and checked."""
        from . import gui_patchtables as _gpt
        self._sync_checks()
        have = {e.name: k for k, e in enumerate(self._entries)}
        if not self.solo:
            have.update(_gpt.values_names(self.primary, self.comparison, self._entries))
        for e in ents:
            if e.name in have:
                k = have[e.name]
                if not self.solo:
                    self._set_whole(k, True)
                self._entries[k].selected = True
                continue
            patched = None
            if not self.solo:
                try:
                    patched = self.comparison.counterpart(self.primary, e)
                except Exception:                       # noqa: BLE001
                    patched = None
            entry = self._patch.whole_entry(e, patched)
            self._entries.append(entry)
            have[e.name] = len(self._entries) - 1
        self._populate()

    # =============================================================== save
    def _save(self):
        from . import gui_project as _gp
        self._sync_checks()
        blocks = [self._patch.build_block_entry(self.primary, self.comparison, s, e)
                  for j, (s, e) in enumerate(self._gaps) if j in self._gap_on]
        selected = [e for e in self._entries if e.selected]
        if not selected and not blocks:
            QtWidgets.QMessageBox.information(self, "Generate Patch", "Nothing is checked.")
            return
        default = "%s_to_%s.patch" % (
            os.path.splitext(os.path.basename(self.primary.binfile.name))[0],
            os.path.splitext(os.path.basename(self.comparison.binfile.name))[0])
        pt = self._patch.new_patch(self.primary, self.comparison, selected, blocks)
        ws = self.ws
        ref = getattr(ws, "project_ref", None)
        if ref is not None:
            # kept in the project by default; Project panel > Export writes it
            # to a file whenever wanted
            SB = QtWidgets.QMessageBox.StandardButton
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Question, "Generate Patch",
                "Keep the patch (%d parameter(s), %d block(s)) in the project %s?"
                % (len(selected), len(blocks), ref.doc.title), SB.Cancel, self)
            keep = box.addButton("Keep in the project",
                                 QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            tofile = box.addButton("Save as a file...",
                                   QtWidgets.QMessageBox.ButtonRole.ActionRole)
            box.setDefaultButton(keep)
            box.exec()
            if box.clickedButton() is keep:
                _gp.keep_patch(ws, pt, default)
                self.accept()
                return
            if box.clickedButton() is not tofile:
                return
        from .recent import get_last_dir, set_last_dir
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save patch", os.path.join(get_last_dir("patch"), default),
            "Patch files (*.patch)")
        if not path:
            return
        set_last_dir(path, "patch")
        pt.save(path)
        QtWidgets.QMessageBox.information(
            self, "Generate Patch", "Saved %d parameter(s) and %d block(s) to %s"
            % (len(selected), len(blocks), os.path.basename(path)))
        self.accept()

    # ------------------------------------------- older callers' spelling
    def _summarize(self, e, cp):
        """:func:`summarize` (kept as the Differences list's method)."""
        return summarize(e, cp)


def history_window(target, parent=None):
    """Generate patch from history: this window on two versions of
    ``target``'s history."""
    return DifferencesWindow.from_history(target, parent)


__all__ = ["DifferencesWindow", "summarize", "history_window", "LIST", "PATCH"]

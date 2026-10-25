# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison binary in the shift map and lock-up editors.

When the tab has a comparison file, :class:`CompareView` builds the
comparison's schedule set with the same provider as the window's own
(``gui_gearbox_edit.schedule_set_for``), pairs the schedules by key
(:mod:`ovtsuite.gearbox_edit.compare`) and adds to the window:

* **Show comparison** -- one switch for everything below (the window looks
  as it did without a comparison when it is off; copying still works);
* each schedule's comparison line on the chart, faded and dashed in the
  schedule's colour behind its own, the hover tooltip giving both values;
* a read-only copy of the value table for the comparison under the table,
  same rows, columns and units, scrolled, sized and selected with it
  (:class:`gui_cmpscroll.ViewLink`), the cells that differ shaded in both
  with the main editor's "differs from comparison" colours (``gui.DIFF_BG``
  with ``gui.CELL_DARK_FG`` text, in either theme) and the other value in
  the tooltip;
* right-click **Copy selected cells from comparison** / **Copy whole line
  from comparison** (on the table, either copy, or on a line on the chart):
  each adds a step to *Steps in this edit*, applied with Apply like any
  other step (one undo step), and removed with Remove.

The tables show the edit's preview: a cell a step has already brought to
the comparison's value is no longer shaded. A cell the edit would change is
"held, not applied yet", shown as the fault-code and measuring-block windows
show theirs (:data:`PENDING_BG`, bold, dark text); in the primary table that
wins over the comparison shading, as it does there, while the comparison's
copy of the cell still shows whether it differs from the preview. When the comparison has no
matching set (another gearbox, other lines) or a schedule another shape,
the status beside the switch says so and those parts stay off.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import gearbox_edit as GE
# a cell the edit's steps would change, not applied yet: the colour the
# fault-code and measuring-block windows give their held changes, from one
# place, with bold text
from .gui_cellmarks import PENDING_BG

SHOW_TEXT = "Show comparison"

#: item data on a primary cell the edit changes (so the comparison shading
#: leaves it alone)
PENDING_ROLE = int(QtCore.Qt.UserRole) + 41


def _colors():
    from . import gui as _g
    return _g.DIFF_BG, _g.CELL_DARK_FG


def mark_differs(item: QtWidgets.QTableWidgetItem) -> None:
    """``item`` in the main editor's "differs from comparison" colours:
    opaque orange, dark text (the default text is light in the dark theme)."""
    bg, fg = _colors()
    item.setBackground(QtGui.QBrush(bg))
    item.setForeground(QtGui.QBrush(fg))


def mark_pending(item: QtWidgets.QTableWidgetItem, font: QtGui.QFont) -> None:
    """``item`` as a change held until Apply: :data:`PENDING_BG`, bold, dark
    text. Wins over :func:`mark_differs` in the primary table."""
    _bg, fg = _colors()
    item.setBackground(QtGui.QBrush(PENDING_BG))
    item.setForeground(QtGui.QBrush(fg))
    bold = QtGui.QFont(font)
    bold.setBold(True)
    item.setFont(bold)
    item.setData(PENDING_ROLE, True)


def is_pending(item: Optional[QtWidgets.QTableWidgetItem]) -> bool:
    return bool(item is not None and item.data(PENDING_ROLE))


def comparison_of(ws):
    """The tab's comparison session, or None (none, or the file itself)."""
    comp = getattr(ws, "comparison", None)
    if comp is None or comp is getattr(ws, "primary", None):
        return None
    return comp


class CompareView(QtCore.QObject):
    """The comparison parts of one :class:`GearboxEditDialog`."""

    def __init__(self, dlg, layout: QtWidgets.QVBoxLayout, split: QtWidgets.QSplitter):
        super().__init__(dlg)
        self.dlg = dlg
        self.session = comparison_of(dlg.ws)
        self.cset: Optional[GE.ScheduleSet] = None
        self.pairing: Optional[GE.Pairing] = None
        self.chk = self.lbl = self.box = self.cmp_table = self.link = None
        self._vals: Dict[int, List[float]] = {}
        self._rev = None
        self._sel: List[int] = []
        if self.session is None:
            return
        from .gui_gearbox_edit import schedule_set_for
        try:
            self.cset = schedule_set_for(self.session, dlg.kind)
        except Exception:
            self.cset = None
        self.pairing = GE.pair_sets(dlg.sset, self.cset)
        self._index = {s.key: i for i, s in enumerate(dlg.sset.schedules)}
        name = getattr(getattr(self.session, "binfile", None), "name", "comparison")

        row = QtWidgets.QHBoxLayout()
        self.chk = QtWidgets.QCheckBox(SHOW_TEXT)
        self.chk.setToolTip(
            "Draw the comparison file's lines on the chart (faded, dashed, in each "
            "schedule's colour) and its values in a read-only table under this one; "
            "cells that differ are shaded in both. Right-click the table or a line "
            "to copy from the comparison.")
        self.chk.setChecked(self.pairing.ok)
        self.chk.setEnabled(self.pairing.ok)
        self.chk.toggled.connect(lambda *_: dlg._redraw())
        row.addWidget(self.chk)
        self.lbl = QtWidgets.QLabel()
        self.lbl.setStyleSheet("color: palette(mid);")
        sp = self.lbl.sizePolicy()          # its text never widens the window
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Ignored)
        self.lbl.setSizePolicy(sp)
        self.lbl.setText("%s: %s" % (name, self.pairing.summary()))
        self.lbl.setToolTip(self.lbl.text())
        row.addWidget(self.lbl, 1)
        layout.insertLayout(max(0, layout.indexOf(split)), row)
        if not self.pairing.ok:
            return

        self.box = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(self.box)
        cl.setContentsMargins(0, 2, 0, 0)
        cl.setSpacing(2)
        import html
        self.head = QtWidgets.QLabel("<b>Comparison:</b> %s (read only)" % html.escape(name))
        cl.addWidget(self.head)
        self.cmp_table = QtWidgets.QTableWidget()
        self.cmp_table.setObjectName("gearboxCompareTable")
        self.cmp_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        # like the main editor's comparison grid (gui_cmpscroll): rows as
        # the table above shades them, the selection drawn the same way
        self.cmp_table.setAlternatingRowColors(dlg.table.alternatingRowColors())
        from .gui_cellmarks import CellDelegate
        self.cmp_table.setItemDelegate(CellDelegate(self.cmp_table))
        self.cmp_table.verticalHeader().setDefaultSectionSize(
            dlg.table.verticalHeader().defaultSectionSize())
        cl.addWidget(self.cmp_table)
        split.addWidget(self.box)
        split.setSizes([440, 220, 180])
        from .gui_cmpscroll import ViewLink
        same = (lambda i: i)
        self.link = ViewLink(dlg.table, self.cmp_table, same, same, parent=self)
        for t in (dlg.table, self.cmp_table):
            t.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            t.customContextMenuRequested.connect(
                lambda pos, t=t: self._exec(self.table_menu(t, pos),
                                            t.viewport().mapToGlobal(pos)))
        dlg.chart.context.connect(lambda key, gp: self._exec(self.chart_menu(key), gp))

    # ------------------------------------------------------------ state
    def active(self) -> bool:
        """Drawing the comparison: paired, and the switch on."""
        return bool(self.pairing is not None and self.pairing.ok
                    and self.chk is not None and self.chk.isChecked())

    def values(self, j: int) -> List[float]:
        """Comparison schedule ``j``'s stored values (read again when the
        comparison changed -- it may be another tab's live file)."""
        rev = getattr(self.session, "revision", None)
        if rev != self._rev:
            self._rev, self._vals = rev, {}
        got = self._vals.get(j)
        if got is None:
            got = self._vals[j] = self.cset.schedules[j].values()
        return got

    # ------------------------------------------------------------ chart
    def decorate(self, series: List[dict], line: int) -> str:
        """Add each series' comparison line (``cmp``); the title's note."""
        if not self.active():
            return ""
        dlg, pr = self.dlg, self.pairing
        for d in series:
            i = self._index.get(d["key"])
            j = pr.pairs.get(i)
            if j is None:
                d["tip"] = d.get("tip", d["key"]) + "\nNot in the comparison file"
                continue
            t = self.cset.schedules[j]
            f = dlg._factor(dlg.sset.schedules[i], line)
            vals, K = self.values(j), t.cols
            d["cmp"] = [(p, vals[r * K + line] * f) for r, p in enumerate(t.pedal)
                        if not t.is_off(vals[r * K + line])]
            if i in pr.axis_differs:
                d["tip"] = d.get("tip", d["key"]) + "\nThe comparison's pedal axis differs"
        return "   (faded dashes: comparison)"

    # ------------------------------------------------------------ table
    def fill(self, line: int) -> None:
        """The comparison table for ``line``, and the difference shading in
        both tables (the primary's was just filled by the dialog)."""
        if self.box is None:
            return
        dlg = self.dlg
        self._sel = list(dlg._selected())
        on = self.active()
        self.box.setVisible(on)
        ct = self.cmp_table
        ct.clear()
        pt = dlg.table
        if not on or not self._sel or dlg.result is None:
            ct.setRowCount(0)
            ct.setColumnCount(0)
            return
        ct.setColumnCount(pt.columnCount())
        ct.setRowCount(pt.rowCount())
        for c in range(pt.columnCount()):
            h = pt.horizontalHeaderItem(c)
            ct.setHorizontalHeaderItem(c, QtWidgets.QTableWidgetItem(h.text() if h else ""))
        ct.setVerticalHeaderLabels([dlg.sset.schedules[i].key for i in self._sel])
        pr = self.pairing
        for row, i in enumerate(self._sel):
            s = dlg.sset.schedules[i]
            j = pr.pairs.get(i)
            if j is None:
                for r in range(s.rows):
                    it = QtWidgets.QTableWidgetItem("—")
                    it.setTextAlignment(QtCore.Qt.AlignCenter)
                    it.setToolTip("%s is not in the comparison file (or has another shape)"
                                  % s.key)
                    ct.setItem(row, r, it)
                continue
            t = self.cset.schedules[j]
            f = dlg._factor(s, line)
            cv_all, grid = self.values(j), dlg.result.grid[i]
            axis = i in pr.axis_differs
            for r in range(s.rows):
                cv = cv_all[r * t.cols + line]
                pv = grid[r * s.cols + line]
                it = QtWidgets.QTableWidgetItem("%.0f" % (cv * f))
                it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                tip = "Comparison %s at %.1f %% pedal" % (t.key, t.pedal[r])
                if axis:
                    tip += "\nThe pedal axes differ: not compared cell for cell"
                elif cv != pv:
                    tip += "\nThis file: %.0f (%+.0f)" % (pv * f, (pv - cv) * f)
                    mark_differs(it)
                    mine = pt.item(row, r)
                    if mine is not None:
                        if not is_pending(mine):     # held change wins
                            mark_differs(mine)
                        mine.setToolTip(mine.toolTip() + "\nComparison: %.0f (%+.0f)"
                                        % (cv * f, (pv - cv) * f))
                it.setToolTip(tip)
                ct.setItem(row, r, it)
        ct.resizeColumnsToContents()
        ph, ch = pt.horizontalHeader(), ct.horizontalHeader()
        for c in range(pt.columnCount()):
            w = max(ph.sectionSize(c), ch.sectionSize(c))
            ph.resizeSection(c, w)
            ch.resizeSection(c, w)
        self.link.sync_all()

    # ------------------------------------------------------- copying
    def _cmp_values(self, idx: Iterable[int]) -> Dict[int, List[float]]:
        return {self.pairing.pairs[i]: self.values(self.pairing.pairs[i])
                for i in idx if i in self.pairing.pairs}

    def copy_cells_op(self, cells: List[Tuple[int, int, int]]):
        """A step copying the comparison's ``(schedule, row, line)`` cells
        that differ from the preview; None when none does."""
        if self.pairing is None or not self.pairing.ok:
            return None
        cur = self.dlg.result.grid if self.dlg.result is not None else None
        n = len({k for k in cells if self.pairing.comparable(k[0])})
        return GE.copy_op(self.pairing, cells, self._cmp_values({k[0] for k in cells}),
                          "copy %d cell%s from comparison" % (n, "" if n == 1 else "s"),
                          current=cur)

    def copy_line_op(self, schedules: List[int], line: int):
        """A step copying the whole of ``line`` (every pedal row) of each of
        ``schedules`` from the comparison; None when nothing differs."""
        if self.pairing is None or not self.pairing.ok:
            return None
        sset = self.dlg.sset
        cells = [(i, r, line) for i in schedules for r in range(sset.schedules[i].rows)]
        cur = self.dlg.result.grid if self.dlg.result is not None else None
        return GE.copy_op(self.pairing, cells, self._cmp_values(schedules),
                          "copy whole %s from comparison" % sset.line(line).label,
                          current=cur)

    def add_step(self, op) -> None:
        """``op`` into the window's steps, as Add step puts one there."""
        if op is None:
            return
        dlg = self.dlg
        dlg.steps.append(op)
        dlg.steps_list.addItem(dlg._describe(op))
        dlg._schedule()

    def _table_cells(self, table, pos=None) -> List[Tuple[int, int]]:
        cells = sorted({(ix.row(), ix.column()) for ix in table.selectedIndexes()})
        if not cells and pos is not None:
            ix = table.indexAt(pos)
            if ix.isValid():
                cells = [(ix.row(), ix.column())]
        return [(r, c) for r, c in cells if r < len(self._sel)]

    def _why_not(self, schedules: List[int]) -> str:
        pr = self.pairing
        if not any(i in pr.pairs for i in schedules):
            return "not in the comparison file"
        if not any(pr.comparable(i) for i in schedules):
            return "the pedal axes differ"
        return "no difference"

    def table_menu(self, table, pos=None) -> QtWidgets.QMenu:
        """The right-click menu of either table (not shown: see ``_exec``)."""
        menu = QtWidgets.QMenu(table)
        line = self.dlg._line()
        cells = self._table_cells(table, pos)
        keys = [(self._sel[r], c, line) for r, c in cells]
        rows = sorted({self._sel[r] for r, _c in cells})
        op = self.copy_cells_op(keys) if keys else None
        a = menu.addAction("Copy selected cells from comparison")
        a.setEnabled(op is not None)
        if op is None and keys:
            a.setText(a.text() + " (%s)" % self._why_not(rows))
        a.triggered.connect(lambda *_, op=op: self.add_step(op))
        op2 = self.copy_line_op(rows, line) if rows else None
        b = menu.addAction("Copy whole line from comparison")
        b.setToolTip("Every pedal row of the line shown, for the schedules in the selection")
        b.setEnabled(op2 is not None)
        if op2 is None and rows:
            b.setText(b.text() + " (%s)" % self._why_not(rows))
        b.triggered.connect(lambda *_, op=op2: self.add_step(op))
        return menu

    def chart_menu(self, key: str) -> QtWidgets.QMenu:
        """The right-click menu of a schedule's line on the chart."""
        menu = QtWidgets.QMenu(self.dlg.chart)
        i = self._index.get(key)
        line = self.dlg._line()
        op = self.copy_line_op([i], line) if i is not None else None
        a = menu.addAction("Copy whole line from comparison (%s, %s)"
                           % (key, self.dlg.sset.line(line).label))
        a.setEnabled(op is not None)
        if op is None and i is not None:
            a.setText(a.text() + " — " + self._why_not([i]))
        a.triggered.connect(lambda *_, op=op: self.add_step(op))
        return menu

    @staticmethod
    def _exec(menu: QtWidgets.QMenu, global_pos: QtCore.QPoint) -> None:
        menu.exec(global_pos)
        menu.deleteLater()

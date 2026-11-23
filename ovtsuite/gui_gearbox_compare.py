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
  with the app's "differs from comparison" orange outline
  (:func:`gui_cellmarks.mark_differs`) and the other value in the tooltip;
* right-click **Copy selected cells from comparison** / **Copy whole line
  from comparison** (on the table, either copy, or on a line on the chart):
  each adds a step to *Steps in this edit*, applied with Apply like any
  other step (one undo step), and removed with Remove.

The tables show the edit's preview: a cell a step has already brought to
the comparison's value is no longer shaded. A cell the edit would change is
"held, not applied yet", shown as the fault-code and measuring-block windows
show theirs (:data:`PENDING_BG`, bold, dark text); in the primary table the
outline of a difference lies over it, as it does there. When the comparison has no
matching set (another gearbox, or no line in common) the status beside the switch
says so and those parts stay off. Lines are matched by name (a lock-up map of 30
columns has three per gear, one of 40 has four), and a schedule with more or fewer
pedal points than the comparison's has its pedal axis different (see below).
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

from . import gearbox_edit as GE
# a cell the edit's steps would change, not applied yet: the colour the
# fault-code and measuring-block windows give their held changes, from one
# place, with bold text
from .gui_cellmarks import PENDING_BG, dark_fg, mark_differs

SHOW_TEXT = "Show comparison"
ALIGN_TEXT = "Align to this file's pedal axis"

#: item data on a primary cell the edit changes
PENDING_ROLE = int(QtCore.Qt.UserRole) + 41


def mark_pending(item: QtWidgets.QTableWidgetItem, font: QtGui.QFont) -> None:
    """``item`` as a change held until Apply: :data:`PENDING_BG`, bold, dark
    text. The outline of :func:`mark_differs` lies over it."""
    item.setBackground(QtGui.QBrush(PENDING_BG))
    item.setForeground(QtGui.QBrush(dark_fg()))
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
        self._vals_aligned: Dict[tuple, Optional[List[float]]] = {}
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
        # a pair whose pedal axis differs can be read at this file's pedal
        # points (gui_cmpsize "Aligned to the primary's axes")
        self.align_chk = QtWidgets.QCheckBox(ALIGN_TEXT)
        self.align_chk.setToolTip(
            "Read the comparison's values at this file's pedal points where the "
            "two pedal axes differ, so every cell is compared (and can be "
            "copied) at the same pedal. Off: those schedules are drawn on their "
            "own axis and not compared. The chart always draws the comparison's "
            "own points.")
        self.align_chk.setVisible(bool(self.pairing.axis_differs))
        self.align_chk.setEnabled(self.pairing.ok)
        from . import gui_cmpsize as _gcs
        self.align_chk.setChecked(_gcs.default() == "aligned")
        self.align_chk.toggled.connect(self._align_toggled)
        row.addWidget(self.align_chk)
        self.lbl = QtWidgets.QLabel()
        self.lbl.setStyleSheet("color: palette(mid);")
        sp = self.lbl.sizePolicy()          # its text never widens the window
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Ignored)
        self.lbl.setSizePolicy(sp)
        self._name = name
        self._set_aligned(self.align_chk.isChecked())
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

    def _set_aligned(self, on: bool) -> None:
        """Compare the schedules whose pedal axis differs at this file's pedal
        points (``on``), or leave them uncompared; the summary says which."""
        pr = self.pairing
        pr.aligned = set(pr.axis_differs) if on else set()
        self._vals_aligned = {}
        self.lbl.setText("%s: %s" % (self._name, pr.summary()))
        self.lbl.setToolTip(self.lbl.text())

    def _align_toggled(self, on: bool) -> None:
        self._set_aligned(on)
        self.dlg._redraw()

    def cvalues(self, i: int):
        """``(pedal, values)`` of the comparison's schedule paired with primary
        schedule ``i``, in the primary schedule's shape (rows x lines): the
        comparison's own pedal points, or -- aligned, or when they match -- read at
        the primary's. A cell the comparison has no value for (a line it lacks, a
        pedal row beyond its own) is None. Lines are matched by name."""
        pr = self.pairing
        j = pr.pairs[i]
        t, s = self.cset.schedules[j], self.dlg.sset.schedules[i]
        vals = self.values(j)
        cols = [pr.cmp_col(c) for c in range(s.cols)]
        identity = cols == list(range(t.cols))
        same_axis = i not in pr.axis_differs
        if identity and same_axis:
            return tuple(t.pedal), vals
        key = (j, self._rev, i in pr.aligned)
        got = self._vals_aligned.get(key)
        if got is None:
            if i in pr.aligned:
                got = GE.align_values(t.pedal, vals, t.cols, s.pedal, t.is_off, cols)
                if got is None:
                    pr.aligned.discard(i)       # its axis cannot be read along
            if got is None:
                # its own points; the rows past its last are not there
                own = GE.align_values(t.pedal, vals, t.cols, t.pedal, t.is_off, cols)                     or [None] * (t.rows * s.cols)
                n = s.rows * s.cols
                got = (own + [None] * n)[:n]
            self._vals_aligned[key] = got
        if i in pr.aligned or same_axis:
            return tuple(s.pedal), got
        return tuple((list(t.pedal) + [None] * s.rows)[:s.rows]), got

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
            col = pr.cmp_col(line)
            if col is None or col >= t.cols:
                d["tip"] = d.get("tip", d["key"]) + "\nThis line is not in the comparison"
                continue
            vals, K = self.values(j), t.cols
            d["cmp"] = [(p, vals[r * K + col] * f) for r, p in enumerate(t.pedal)
                        if not t.is_off(vals[r * K + col])]
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
                    it.setToolTip("%s is not in the comparison file" % s.key)
                    ct.setItem(row, r, it)
                continue
            t = self.cset.schedules[j]
            f = dlg._factor(s, line)
            cped, cv_all = self.cvalues(i)
            grid = dlg.result.grid[i]
            axis = not pr.comparable(i)
            for r in range(s.rows):
                cv = cv_all[r * s.cols + line]
                pv = grid[r * s.cols + line]
                if cv is None:
                    it = QtWidgets.QTableWidgetItem("—")
                    it.setTextAlignment(QtCore.Qt.AlignCenter)
                    it.setToolTip("%s has no value here in the comparison (%s)" % (
                        s.key, "that line is not in it" if pr.cmp_col(line) is None
                        else "it has fewer pedal points"))
                    ct.setItem(row, r, it)
                    continue
                it = QtWidgets.QTableWidgetItem("%.0f" % (cv * f))
                it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                tip = "Comparison %s at %.1f %% pedal" % (t.key, cped[r])
                if i in pr.aligned:
                    tip += (" (read at this file's pedal points from its own %.1f.."
                            "%.1f %%)" % (t.pedal[0], t.pedal[-1]))
                if axis:
                    tip += "\nThe pedal axes differ: not compared cell for cell"
                elif cv != pv:
                    tip += "\nThis file: %.0f (%+.0f)" % (pv * f, (pv - cv) * f)
                    mark_differs(it)
                    mine = pt.item(row, r)
                    if mine is not None:
                        mark_differs(mine)   # the frame lies over a held change
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
        return {self.pairing.pairs[i]: self.cvalues(i)[1]
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

    def average_cells_op(self, cells: List[Tuple[int, int, int]], weight: float):
        """A step moving the ``(schedule, row, line)`` cells ``weight`` percent of the way
        to the comparison's values (the preview's, as the copy does); None when nothing
        would change."""
        if self.pairing is None or not self.pairing.ok or self.dlg.result is None:
            return None
        n = len({k for k in cells if self.pairing.comparable(k[0])})
        return GE.average_op(
            self.pairing, cells, self._cmp_values({k[0] for k in cells}),
            "average %d cell%s with the comparison (%g %%)" % (n, "" if n == 1 else "s", weight),
            weight, self.dlg.result.grid)

    def average_line_op(self, schedules: List[int], line: int, weight: float):
        """The same for the whole of ``line`` (every pedal row) of each schedule."""
        sset = self.dlg.sset
        cells = [(i, r, line) for i in schedules for r in range(sset.schedules[i].rows)]
        op = self.average_cells_op(cells, weight)
        if op is not None:
            op.label = "average whole %s with the comparison (%g %%)" % (
                sset.line(line).label, weight)
        return op

    def _ask_weight(self) -> Optional[float]:
        from . import gui_average as ga
        w, ok = QtWidgets.QInputDialog.getDouble(
            self.dlg, ga.OP_TEXT, "Percent of the comparison in the result:\n" + ga.WEIGHT_TIP,
            ga.last_weight(), 0.0, 100.0, 1)
        if not ok:
            return None
        QtCore.QSettings(*ga.branding.QSETTINGS).setValue(ga.KEY, w)
        return w

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
            return "the pedal axes differ -- tick Align to this file's pedal axis"
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
        # average with the comparison: 50 %, or the weight asked for
        menu.addSeparator()
        for text, ask in (("Average selected cells with comparison", False),
                          ("Average selected cells with comparison...", True)):
            act = menu.addAction(text)
            ok = bool(keys) and any(self.pairing.comparable(i) for i in rows)
            act.setEnabled(ok)
            if not ok:
                act.setToolTip(self._why_not(rows) if keys else "Select cells first")
            act.triggered.connect(
                lambda *_, keys=keys, ask=ask: self._average_cells(keys, ask))
        return menu

    def _average_cells(self, keys, ask: bool) -> None:
        w = self._ask_weight() if ask else 50.0
        if w is not None:
            self.add_step(self.average_cells_op(keys, w))

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
        avg = menu.addAction("Average whole line with comparison (%s, %s)"
                             % (key, self.dlg.sset.line(line).label))
        avg.setEnabled(i is not None and self.pairing.comparable(i))
        if i is not None and not avg.isEnabled():
            avg.setText(avg.text() + " — " + self._why_not([i]))
        avg.triggered.connect(
            lambda *_, i=i, line=line: i is not None and self.add_step(
                self.average_line_op([i], line, 50.0)))
        return menu

    @staticmethod
    def _exec(menu: QtWidgets.QMenu, global_pos: QtCore.QPoint) -> None:
        menu.exec(global_pos)
        menu.deleteLater()

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison part of the End of injection window
(:mod:`ovtsuite.gui_eoi`).

With a comparison loaded the window gets a **Show comparison** switch and,
below the primary's grid, a read-only grid of the comparison's own EOI / SOI
/ duration / table number. The comparison's injection model
(:func:`ovtsuite.eoi.models`) is calculated at the primary grid's own
(rpm, quantity) points, so the two grids always have the same rows and
columns, whatever the comparison's own SOI breakpoints are, and the grids
scroll and select together (:class:`ovtsuite.gui_cmpscroll.ViewLink`: a
cell, or a range, selected in one is selected in the other).

A cell that differs from the comparison has the app's orange outline in both
grids (:func:`ovtsuite.gui_cellmarks.mark_differs`), over the band and shade
the view gives its cells, which stay as they are. Right-click on either grid:
**Copy comparison → primary** for the selected cells (one undo step, through
the window's usual plans) and **Copy primary − comparison** (the difference,
as text).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtWidgets

from . import eoi as _eoi
from .gui_cellmarks import mark_differs

Values = Dict[Tuple[int, int], Tuple[float, str]]


def comparison_of(ws):
    """The tab's comparison session, or None (none, or the file itself)."""
    comp = getattr(ws, "comparison", None)
    if comp is None or comp is getattr(ws, "primary", None):
        return None
    return comp


class EoiCompare(QtCore.QObject):
    """The comparison grid and switch of one :class:`EoiDialog`."""

    def __init__(self, dlg):
        super().__init__(dlg)
        self.dlg = dlg
        self.session = comparison_of(dlg.ws)
        self.models: List[_eoi.InjectionModel] = []
        if self.session is not None:
            try:
                self.models = list(_eoi.models(self.session))
            except Exception:                     # noqa: BLE001
                self.models = []
        self._calcs: Dict[tuple, Optional[_eoi.Calculator]] = {}
        self.reason = ""                          # why there is no comparison
        self.check = QtWidgets.QCheckBox("Show comparison")
        self.check.setChecked(True)
        self.check.setToolTip(
            "A read-only grid of the comparison file's own values below, "
            "calculated at the same engine speeds and quantities. Cells that "
            "differ have an orange outline in both grids.")
        self.grid = QtWidgets.QTableWidget()
        self.grid.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setObjectName("eoiCompareGrid")
        self.head = QtWidgets.QLabel()
        self.head.setStyleSheet("color: gray")
        self.box = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(self.box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.head)
        lay.addWidget(self.grid, 1)
        self.link = None
        self.box.hide()
        self.check.setVisible(self.session is not None)
        self.check.toggled.connect(lambda _on: dlg.rebuild())

    # --------------------------------------------------------- the model
    def model(self) -> Optional[_eoi.InjectionModel]:
        """The comparison's model for the dataset the window shows: the one
        of the same group, else the same position."""
        if not self.models:
            return None
        mine = self.dlg.model
        for m in self.models:
            if (m.group or "") == (mine.group or ""):
                return m
        i = self.dlg.ds.currentIndex()
        return self.models[i] if 0 <= i < len(self.models) else self.models[0]

    def calc(self) -> Optional[_eoi.Calculator]:
        """The comparison's calculator for the shown SOI set, at the
        window's conditions; None (with :attr:`reason`) when it has none."""
        self.reason = ""
        if self.session is None:
            return None
        m = self.model()
        if m is None:
            self.reason = "The comparison has no injection tables to compare."
            return None
        if m.missing:
            self.reason = ("The comparison's definition does not place every "
                           "table the calculation needs.")
            return None
        key = self.dlg.soi_key()
        if key != "base" and not any(x.key == key for x in m.soi_sets):
            self.reason = "The comparison has no \"%s\" SOI set." % key
            return None
        ck = (id(m), key)
        c = self._calcs.get(ck)
        if c is None:
            try:
                c = _eoi.Calculator(m, self.dlg.conditions(), key)
            except Exception as ex:               # noqa: BLE001
                self.reason = "The comparison could not be calculated: %s" % ex
                return None
            self._calcs[ck] = c
        c.conditions.update(self.dlg.conditions())
        return c

    def wanted(self) -> bool:
        return self.session is not None and self.check.isChecked()

    # ------------------------------------------------------------ values
    def values(self, mode: str, cool: float, cells) -> Optional[Values]:
        """The comparison's ``{(r, c): (value, text)}`` for the primary's
        grid cells (``cells``: ``{(r, c): Cell}``), in the shown ``mode``."""
        calc = self.calc()
        if calc is None:
            return None
        from .gui_eoi import cell_value
        try:
            mem = calc.soi_members(cool)
            return {rc: cell_value(mode, calc.cell(cool, cell.rpm, cell.q, mem))
                    for rc, cell in cells.items()}
        except Exception as ex:                   # noqa: BLE001
            self.reason = "The comparison could not be calculated: %s" % ex
            return None

    def table_values(self, k: int, ref: _eoi.Table, cool: float):
        """``({(r, c): value}, usage)`` of the comparison's duration table
        ``k`` at the primary table's own breakpoints (read at each point, so
        a table with other breakpoints still lines up); ``usage`` is the
        comparison's own use of its cells when the axes are the same, else
        None."""
        calc = self.calc()
        if calc is None or k >= len(calc.dur_t) or calc.dur_t[k] is None:
            self.reason = "The comparison has no duration table %d." % k
            return None
        t = calc.dur_t[k]
        vals = {}
        for r in range(len(ref.vals)):
            for c in range(len(ref.vals[0])):
                rpm, q = ref.point(r, c)
                vals[(r, c)] = t.at(rpm, q)
        same = (t.xs == ref.xs and t.ys == ref.ys)
        return vals, (calc.usage(k, cool) if same else None)

    # ---------------------------------------------------------- showing
    def attach(self, view_link_cls) -> None:
        """Link the two grids (scroll, sizes, current cell, selection)."""
        if self.link is None:
            same = lambda i: i                           # noqa: E731
            self.link = view_link_cls(self.dlg.grid, self.grid, same, same,
                                      parent=self)

    def show(self, on: bool, head: str = "") -> None:
        self.box.setVisible(on)
        self.head.setText(head)

    def mark(self, grid_item, cmp_item, differs: bool) -> None:
        """The outline on a cell that differs, in both grids."""
        if differs:
            mark_differs(grid_item)
            mark_differs(cmp_item)

    # ------------------------------------------------------- the menu
    def menu_actions(self, menu: QtWidgets.QMenu, cmp_by_cell: Dict[Tuple[int, int], float],
                     prim_by_cell: Dict[Tuple[int, int], float],
                     cells: List[Tuple[int, int]], can_edit: bool) -> None:
        """Add the comparison actions for the selected ``cells``."""
        n = len(cells)
        copy_from = menu.addAction(
            "Copy comparison → primary (%d cell%s)" % (n, "" if n == 1 else "s"))
        copy_from.setEnabled(can_edit and n > 0 and bool(cmp_by_cell))
        copy_from.triggered.connect(
            lambda: self.dlg.commit({rc: cmp_by_cell[rc] for rc in cells
                                     if rc in cmp_by_cell}))
        diff = menu.addAction("Copy primary − comparison (the difference)")
        diff.setEnabled(n > 0 and bool(cmp_by_cell))
        diff.triggered.connect(lambda: self.copy_difference(cells, cmp_by_cell,
                                                            prim_by_cell))

    def copy_difference(self, cells, cmp_by_cell, prim_by_cell) -> str:
        """The selected cells' primary - comparison, as tab-separated text
        (the rectangle round them, blank where a cell is not selected)."""
        sel = {rc for rc in cells if rc in cmp_by_cell and rc in prim_by_cell}
        if not sel:
            return ""
        rows = sorted({r for r, _c in sel})
        cols = sorted({c for _r, c in sel})
        from .gui_eoi import _num
        lines = []
        for r in range(rows[0], rows[-1] + 1):
            lines.append("\t".join(
                _num(prim_by_cell[(r, c)] - cmp_by_cell[(r, c)], 3)
                if (r, c) in sel else ""
                for c in range(cols[0], cols[-1] + 1)))
        text = "\n".join(lines)
        QtWidgets.QApplication.clipboard().setText(text)
        return text

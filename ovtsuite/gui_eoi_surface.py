# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The 3-D surface of the End of injection window (Ctrl+3).

A column to the left of the grids, one surface per grid and level with it: the
primary's beside the primary grid, the comparison's (read only, on the same
height and colour scale, orbit and zoom linked) beside the comparison grid. It
is the table editor's surface (:class:`ovtsuite.gui_surface3d.Surface3DView`:
drag empty space to orbit, wheel to zoom, Home resets) over what the grid
shows -- EOI, SOI, duration, table number, or one duration table's cells.

Its selection IS the grid's: a point picked on the surface selects that cell,
a selection made in the grid is marked on the surface. Edits go the way typing
into the grid goes (:meth:`EoiDialog.commit`), so each is one undo step with
the window's usual limiter and fuel-quantity handling:

=======================  ==================================================
click a point            select that cell (in the grid too)
Shift+click / drag       select the block from the current cell to the point
Ctrl+click               add / remove the point
Ctrl+drag up/down        raise / lower every selected point (snapped to the
                         view's step); Esc cancels, release writes it
arrows                   move the current point (Ctrl: to the edge)
drag elsewhere           orbit;  wheel: zoom;  Home: reset
=======================  ==================================================
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from PySide6 import QtCore, QtWidgets

from .gui_surface3d import Surface3DView

#: width of the surface column, in pixels
WIDTH = 330
SHOWN_KEY = "eoi/surface"

_UNITS = {"eoi": "° ATDC", "soi": "° BTDC", "dur": "°",
          "idx": "table", "tab": "°"}


class _EoiEditor:
    """What :class:`Surface3DView` asks its editor, answered by the window's
    primary grid."""

    def __init__(self, surf: "EoiSurface"):
        self.surf = surf
        self.anchor: Optional[Tuple[int, int]] = None

    @property
    def d(self):
        return self.surf.d

    def can_edit(self) -> bool:
        d = self.d
        return d._can_edit() and not d.shows_difference()

    def describe(self, i, j) -> str:
        ref = getattr(self.d, "_ref", None)
        try:
            rpm, q = ref.point(i, j)
            return "%g rpm, %g mg" % (rpm, q)
        except Exception:                               # noqa: BLE001
            return "row %d, col %d" % (i + 1, j + 1)

    # -- selection: the grid's -----------------------------------------
    def _sm(self):
        return self.d.grid.selectionModel()

    def _ix(self, i, j):
        return self.d.grid.model().index(i, j)

    def selected(self) -> Set[Tuple[int, int]]:
        return set(self.d._selected_cells())

    def current(self) -> Optional[Tuple[int, int]]:
        g = self.d.grid
        r, c = g.currentRow(), g.currentColumn()
        return (r, c) if r >= 0 and c >= 0 else None

    def select_one(self, i, j):
        ix = self._ix(i, j)
        if ix.isValid():
            self._sm().setCurrentIndex(
                ix, QtCore.QItemSelectionModel.ClearAndSelect)
            self.anchor = (i, j)
            self.d.grid.scrollTo(ix)

    def toggle(self, i, j):
        ix = self._ix(i, j)
        if ix.isValid():
            self._sm().select(ix, QtCore.QItemSelectionModel.Toggle)
            self._sm().setCurrentIndex(ix, QtCore.QItemSelectionModel.NoUpdate)
            self.anchor = (i, j)

    def extend_to(self, i, j):
        a = self.anchor or self.current() or (i, j)
        lo = self._ix(min(a[0], i), min(a[1], j))
        hi = self._ix(max(a[0], i), max(a[1], j))
        if lo.isValid() and hi.isValid():
            self._sm().select(QtCore.QItemSelection(lo, hi),
                              QtCore.QItemSelectionModel.ClearAndSelect)
            self._sm().setCurrentIndex(self._ix(i, j),
                                       QtCore.QItemSelectionModel.NoUpdate)
            self.anchor = a

    def move(self, di, dj, extend: bool):
        v = self.surf.view
        cur = self.current() or (0, 0)
        i = max(0, min(v.rows - 1, cur[0] + di))
        j = max(0, min(v.cols - 1, cur[1] + dj))
        if extend:
            self.anchor = self.anchor or cur
            self.extend_to(i, j)
        else:
            self.select_one(i, j)

    def jump(self, d, extend: bool):
        self.d.edit.jump(d, extend)
        if not extend:
            self.anchor = self.current()

    # -- Ctrl+drag --------------------------------------------------------
    def _new_values(self, delta: float) -> Dict[Tuple[int, int], float]:
        from .gui_eoi_edit import step_for
        d = self.d
        st = step_for(d.edit.view_key())
        step = round(delta / st) * st if st > 0 else delta
        if step == 0:
            return {}
        cur = d._prim_vals
        return {rc: cur[rc] + step for rc in sorted(self.selected()) if rc in cur}

    def drag_preview(self, delta: float):
        if self.can_edit():
            self.surf.view.preview = self._new_values(delta)
            self.surf.view.update()

    def drag_cancel(self):
        self.surf.view.preview = {}
        self.surf.view.update()
        self.d.say("Height change cancelled.")

    def drag_commit(self, delta: float) -> int:
        self.surf.view.preview = {}
        self.surf.view.update()
        if not self.d.edit._ok():
            return 0
        vals = self._new_values(delta)
        if not vals:
            return 0
        return self.d.edit.run(vals, "Raised" if delta > 0 else "Lowered")


class EoiSurface(QtCore.QObject):
    """The surfaces, their controls and the show / hide of the column."""

    def __init__(self, dlg):
        super().__init__(dlg)
        self.d = dlg
        self.shown = False
        self._zkeep = None
        self._grew = 0
        self.view = Surface3DView([], [], [], "", None)
        self.cmp_view = Surface3DView([], [], [], "", None)
        for v in (self.view, self.cmp_view):
            v.compact = True
            v.dec = 2
            v.setMinimumSize(120, 90)
        self.view.linked.append(self.cmp_view)
        self.cmp_view.linked.append(self.view)
        self.view.editor = _EoiEditor(self)
        self.view.on_reset = self._reset_range
        # the controls, one level row above the surfaces
        self.bar = QtWidgets.QWidget()
        self.bar.setFixedWidth(WIDTH)
        h = QtWidgets.QHBoxLayout(self.bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        reset = QtWidgets.QPushButton("Reset view")
        reset.setToolTip("Back to the starting angle and zoom (Home)")
        reset.clicked.connect(self.view.reset_view)
        self.wire = QtWidgets.QCheckBox("Wireframe")
        self.wire.setToolTip("Draw only the grid lines")
        self.wire.toggled.connect(
            lambda on: [v.set_wire(on) for v in (self.view, self.cmp_view)])
        hbox = QtWidgets.QVBoxLayout()
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.setSpacing(1)
        lbl = QtWidgets.QLabel("Height")
        lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.height = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.height.setRange(10, 200)
        self.height.setValue(round(self.view.zscale * 100))
        self.height.setToolTip("Stretch or flatten the surface (display only)")
        self.height.setFixedWidth(110)
        self.height.valueChanged.connect(
            lambda x: [v.set_zscale(x / 100.0)
                       for v in (self.view, self.cmp_view)])
        hbox.addWidget(lbl)
        hbox.addWidget(self.height)
        h.addWidget(reset, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(self.wire, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        h.addLayout(hbox)
        h.addStretch(1)
        # one box per grid, level with it
        self.pbox = self._box(self.view, "Primary")
        self.cbox = self._box(self.cmp_view, "Comparison (read only)")
        self.bar.setVisible(False)
        self.pbox.setVisible(False)
        self.cbox.setVisible(False)

    def _box(self, view, title: str) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setFixedWidth(WIDTH)
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        cap = QtWidgets.QLabel(title)
        cap.setStyleSheet("color: gray; font-weight: bold;")
        v.addWidget(cap)
        v.addWidget(view, 1)
        w._cap = cap
        return w

    # ----------------------------------------------------------- showing
    def set_shown(self, on: bool, remember_it: bool = True) -> None:
        on = bool(on)
        d = self.d
        if on != self.shown:
            self.shown = on
            self.bar.setVisible(on)
            self.pbox.setVisible(on)
            self.sync_rows()
            if on:
                self.reload()
            if not d.isMaximized() and d.isVisible():
                grow = WIDTH + 8
                if on:
                    self._grew = grow
                    d.resize(d.width() + grow, d.height())
                else:
                    d.resize(max(640, d.width() - self._grew), d.height())
                    self._grew = 0
        if d.surf_btn.isChecked() != on:
            d.surf_btn.blockSignals(True)
            d.surf_btn.setChecked(on)
            d.surf_btn.blockSignals(False)
        if remember_it:
            from .gui_eoi_edit import remember
            remember(SHOWN_KEY, on)

    def sync_rows(self) -> None:
        """The comparison's surface is shown while the comparison grid is."""
        self.cbox.setVisible(self.shown and not self.d.cmpv.box.isHidden())

    def _reset_range(self) -> None:
        self._zkeep = None
        self.reload()

    # ---------------------------------------------------------- the values
    def reload(self) -> None:
        """Read the grids' values again (nothing while hidden)."""
        d = self.d
        if not self.shown:
            return
        self.sync_rows()
        g = d.grid
        rows, cols = g.rowCount(), g.columnCount()
        pv = d._prim_vals
        if rows < 2 or cols < 2 or len(pv) < rows * cols:
            self.view.set_values([])
            self.cmp_view.set_values([])
            return
        key = (d.mode(), d.table_view(), d.ds.currentIndex(), rows, cols)
        unit = _UNITS["tab" if d.table_view() is not None else d.mode()]
        vals = [[pv[(r, c)] for c in range(cols)] for r in range(rows)]
        cv = d._cmp_vals
        cmp_vals = [[cv[(r, c)] for c in range(cols)] for r in range(rows)] \
            if cv and len(cv) >= rows * cols and not d.cmpv.box.isHidden() else None
        flat = [x for row in vals for x in row]
        if cmp_vals:
            flat += [x for row in cmp_vals for x in row]
        lo, hi = min(flat), max(flat)
        if self._zkeep is not None and self._zkeep[0] == key:
            lo, hi = min(lo, self._zkeep[1]), max(hi, self._zkeep[2])
        self._zkeep = (key, lo, hi)               # only widened while the same view
        for v in (self.view, self.cmp_view):
            v.zunits = unit
        self.view.set_values(vals, (lo, hi))
        self.cmp_view.set_values(cmp_vals or [], (lo, hi))
        names = {"eoi": "EOI", "soi": "SOI", "dur": "Duration", "idx": "Table no.",
                 "tab": "duration table"}
        self.pbox._cap.setText(
            "Primary \u00b7 %s" % names["tab" if d.table_view() is not None
                                       else d.mode()] if cmp_vals else "Primary")

    def repaint(self) -> None:
        if self.shown:
            self.view.update()

    def views(self) -> List[Surface3DView]:
        return [self.view, self.cmp_view]

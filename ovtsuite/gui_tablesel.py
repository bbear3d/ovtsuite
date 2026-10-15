# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A table's selection stays on one side of the comparison banner.

The primary table and the comparison below it are one grid (one selection
model), so a drag, Shift-click or Ctrl-click across the banner used to select
cells of both -- and Copy then took a block spanning both, banner included.
:func:`attach` keeps the selection in ONE half: the half the selection was
started in (the cell pressed, or the current cell when the keyboard moves
it). A drag that crosses the banner stops at it; a click in the other half
starts a new selection there."""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore

PRIMARY, COMPARISON = "primary", "comparison"


def half_of(model, row: int) -> Optional[str]:
    """Which half grid ``row`` is in (None without a comparison, or for the
    banner itself)."""
    sep = model.sep_row()
    if sep is None or row == sep:
        return None
    return PRIMARY if row < sep else COMPARISON


def _rows(model, half: str):
    """``[lo, hi]`` grid rows of ``half``."""
    sep = model.sep_row()
    return (0, sep - 1) if half == PRIMARY else (sep + 1, model.rowCount() - 1)


class _Keeper(QtCore.QObject):
    def __init__(self, ws):
        super().__init__(ws.table)
        self.ws = ws
        self.half: Optional[str] = None
        self.busy = False
        sm = ws.table.selectionModel()
        sm.selectionChanged.connect(self._clip)
        sm.currentChanged.connect(self._current)
        ws.model.modelReset.connect(self._reset)
        ws.table.viewport().installEventFilter(self)

    def _reset(self):
        self.half = None

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.MouseButtonPress:
            idx = self.ws.table.indexAt(ev.position().toPoint())
            if idx.isValid():
                h = half_of(self.ws.model, idx.row())
                if h is not None:
                    self.half = h
        return False

    def _current(self, cur, _prev):
        """The keyboard moved the current cell: without Shift (or a mouse
        button) that starts a new selection, in the cell's half."""
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication
        if not cur.isValid():
            return
        if app.mouseButtons() != QtCore.Qt.MouseButton.NoButton:
            return
        if app.keyboardModifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
            return
        h = half_of(self.ws.model, cur.row())
        if h is not None:
            self.half = h

    def _clip(self, *_a):
        if self.busy:
            return
        m = self.ws.model
        if m.sep_row() is None:
            return
        sm = self.ws.table.selectionModel()
        keep = self.half
        if keep is None:
            cur = sm.currentIndex()
            keep = half_of(m, cur.row()) if cur.isValid() else None
            if keep is None:
                return
            self.half = keep
        lo, hi = _rows(m, keep)
        drop = None
        for r in sm.selection():
            if r.top() >= lo and r.bottom() <= hi:
                continue
            if drop is None:
                from PySide6 import QtCore as _qc
                drop = _qc.QItemSelection()
            parts = []
            if r.top() < lo:
                parts.append((r.top(), min(r.bottom(), lo - 1)))
            if r.bottom() > hi:
                parts.append((max(r.top(), hi + 1), r.bottom()))
            for a, b in parts:
                drop.select(m.index(a, r.left()), m.index(b, r.right()))
        if drop is not None and not drop.isEmpty():
            self.busy = True
            try:
                sm.select(drop, QtCore.QItemSelectionModel.SelectionFlag.Deselect)
            finally:
                self.busy = False


def attach(ws) -> None:
    """Keep ``ws``'s table selection in one half (call once, after the
    table has its model)."""
    ws.__dict__["_ovt_tablesel"] = _Keeper(ws)

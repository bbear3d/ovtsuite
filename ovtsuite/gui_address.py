# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The address of the selected cells, at the right of the main window's
status bar: the active tab's table (:func:`watch`) or the hex editor while
a byte is picked in it (:func:`show_hex`). The addresses come from
:mod:`ovtsuite.celladdr`."""

from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtWidgets

from . import celladdr


def label(win) -> Optional[QtWidgets.QLabel]:
    return win.__dict__.get("_ovt_addr_label")


def install(win) -> QtWidgets.QLabel:
    """The label (permanent: a status message does not hide it); it follows
    the tab that is showing."""
    lab = QtWidgets.QLabel()
    lab.setObjectName("address_label")
    lab.setToolTip("Where the selected cells are stored in the binary")
    lab.setTextInteractionFlags(
        QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    win.statusBar().addPermanentWidget(lab)
    win.__dict__["_ovt_addr_label"] = lab
    win.tabs.currentChanged.connect(lambda _i: refresh(win))
    return lab


def _set(win, text: str) -> None:
    lab = label(win)
    if lab is not None:
        lab.setText(text)


def text_for(ws) -> str:
    """What the label says for a tab: the address(es) of its table's
    selected cells."""
    model = getattr(ws, "model", None)
    table = getattr(ws, "table", None)
    if model is None or table is None or model.entity is None:
        return ""
    sm = table.selectionModel()
    if sm is None:
        return ""
    idxs = sm.selectedIndexes()
    if not idxs:
        cur = sm.currentIndex()
        idxs = [cur] if cur.isValid() else []
    ov = getattr(model, "overlay", None)
    mode = ov.mode if ov is not None else None
    addrs = []
    for idx in idxs:
        try:
            kind, a, b = model._kind(idx.row(), idx.column())
            addrs.append(celladdr.cell_addr(model.entity, kind, a, b,
                                            model.compare, mode))
        except Exception:               # a cell the model has no value for
            addrs.append(None)
    return celladdr.describe(addrs)


def refresh(win) -> None:
    """Show the active tab's address(es)."""
    ws = getattr(win, "current", None)
    try:
        _set(win, text_for(ws) if ws is not None else "")
    except RuntimeError:                 # the tab is being torn down
        _set(win, "")


def watch(ws) -> None:
    """Keep the label in step with a tab's table: its selection, and a new
    table (or the grid turned) in the same model."""
    def update(*_a):
        win = getattr(ws, "win", None)
        if win is not None and getattr(win, "current", None) is ws:
            refresh(win)
    sm = ws.table.selectionModel()
    sm.selectionChanged.connect(update)
    sm.currentChanged.connect(update)
    ws.model.modelReset.connect(update)
    ws.model.layoutChanged.connect(update)


def show_hex(dialog, addrs) -> None:
    """The hex editor's picked byte(s), in its main window's label, until
    the dialog goes (the tab's own address then comes back)."""
    win = _main_window(dialog)
    if win is None or label(win) is None:
        return
    _set(win, celladdr.describe(addrs))
    if not dialog.__dict__.get("_ovt_addr_hooked"):
        dialog.__dict__["_ovt_addr_hooked"] = True
        dialog.finished.connect(lambda *_a: refresh(win))


def _main_window(w):
    p = w.parentWidget()
    while p is not None:
        if label(p) is not None:
            return p
        p = p.parentWidget()
    return None

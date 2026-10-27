# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Controls for showing the comparison as a difference
(:mod:`ovtsuite.compare_delta`; model side: ``TableModel.set_delta``).

The mode is the option ``ui/compare_delta``, so every tab and window follows
it and it is remembered. It can be changed in Tools > Options > Editor >
Comparison table, and -- where ``ui/compare_delta_control`` allows -- from

* a drop-down on the comparison banner (the default): on the comparison
  grid's heading in the below / beside layouts, on the banner row of the
  table in the others (:class:`BannerControl`, one per tab);
* a drop-down on a toolbar of its own (:func:`install_toolbar`).

Tint colours and the other two options: ``ui/compare_delta_blank`` (blank the
cells that do not differ) and ``ui/compare_delta_tint`` (blue below, red
above, opaque with dark text so the numbers read in both themes).
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from . import compare_delta as _cd
from . import options as _opt

MODE_KEY = "ui/compare_delta"
BLANK_KEY = "ui/compare_delta_blank"
TINT_KEY = "ui/compare_delta_tint"
CONTROL_KEY = "ui/compare_delta_control"
KEYS = (MODE_KEY, BLANK_KEY, TINT_KEY, CONTROL_KEY)

_TIP = ("Show the comparison as a difference from this file instead of its "
        "own values. Copying always copies the real values.")


def mode() -> str:
    return _cd.valid(_opt.get(MODE_KEY))


def control() -> str:
    c = _opt.get(CONTROL_KEY)
    return c if c in dict(_cd.CONTROLS) else "banner"


def apply(model) -> None:
    """Put the options into effect in one table model."""
    model.set_delta(mode(), _opt.get(BLANK_KEY), _opt.get(TINT_KEY))


def _combo(parent=None) -> QtWidgets.QComboBox:
    cb = QtWidgets.QComboBox(parent)
    for key, label in _cd.MODES:
        cb.addItem(label, key)
    cb.setToolTip(_TIP)
    cb.setObjectName("compareDeltaMode")
    return cb


def _show(cb: QtWidgets.QComboBox, m: str) -> None:
    i = cb.findData(m)
    if i >= 0 and cb.currentIndex() != i:
        cb.blockSignals(True)
        cb.setCurrentIndex(i)
        cb.blockSignals(False)


def _picked(cb: QtWidgets.QComboBox) -> None:
    _opt.set(MODE_KEY, cb.currentData())


class BannerControl(QtCore.QObject):
    """The drop-down on a tab's comparison banner. Lives on the grid's
    heading while the comparison is in its own grid, on the table's banner
    row otherwise."""

    def __init__(self, grid):
        super().__init__(grid)
        self.grid = grid
        self.cb = _combo()
        self.cb.activated.connect(lambda _i: _picked(self.cb))
        t = grid.ws.table
        self._hosts = (t.viewport(), grid.cmp_head)
        for h in self._hosts:
            h.installEventFilter(self)
        for sig in (t.verticalScrollBar().valueChanged,
                    t.horizontalScrollBar().valueChanged,
                    t.verticalHeader().sectionResized):
            sig.connect(self.place)
        grid.ws.model.modelReset.connect(self.place)
        grid.ws.model.layoutChanged.connect(self.place)
        self.cb.hide()

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.Resize:
            QtCore.QTimer.singleShot(0, self.place)
        return False

    def wanted(self) -> bool:
        return (control() in ("banner", "both")
                and self.grid.ws.model.sep_row() is not None)

    def place(self, *_a) -> None:
        try:
            self._place()
        except RuntimeError:            # the tab is going away
            pass

    def _place(self) -> None:
        g, cb = self.grid, self.cb
        head = g.cmp_head
        if not self.wanted():
            cb.hide()
            head.setContentsMargins(0, 0, 0, 0)
            head.setMinimumHeight(0)
            return
        _show(cb, mode())
        cb.setFont(g.ws.table.font())
        w = cb.sizeHint().width()
        if g.active():
            if cb.parent() is not head:
                cb.setParent(head)
            h = cb.sizeHint().height()
            head.setContentsMargins(0, 0, w + 8, 0)
            head.setMinimumHeight(h)
            cb.setGeometry(max(0, head.width() - w - 2), 0, w, h)
            cb.show()
            return
        head.setContentsMargins(0, 0, 0, 0)
        head.setMinimumHeight(0)
        t = g.ws.table
        vp = t.viewport()
        if cb.parent() is not vp:
            cb.setParent(vp)
        m = g.ws.model
        r = t.visualRect(m.index(m.sep_row(), 0))
        if r.height() <= 0 or r.bottom() < 0 or r.top() > vp.height():
            cb.hide()
            return
        h = min(cb.sizeHint().height(), r.height())
        cb.setGeometry(max(0, vp.width() - w - 4), r.top() + (r.height() - h) // 2,
                       w, h)
        cb.show()
        cb.raise_()


def attach(grid) -> BannerControl:
    """Give a tab's comparison grid its banner control and put the options
    into effect in its model."""
    apply(grid.ws.model)
    ctl = BannerControl(grid)
    grid.delta_ctl = ctl
    return ctl


# ------------------------------------------------------------ the toolbar
def install_toolbar(win) -> QtWidgets.QToolBar:
    tb = QtWidgets.QToolBar("Comparison", win)
    tb.setObjectName("compare_toolbar")
    lbl = QtWidgets.QLabel("Comparison shows:")
    lbl.setContentsMargins(6, 0, 4, 0)
    tb.addWidget(lbl)
    cb = _combo()
    cb.activated.connect(lambda _i: _picked(cb))
    tb.addWidget(cb)
    win.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, tb)
    win.__dict__["_ovt_delta_toolbar"] = (tb, cb)
    sync_toolbar(win)
    return tb


def sync_toolbar(win) -> None:
    ent = win.__dict__.get("_ovt_delta_toolbar")
    if ent is None:
        return
    tb, cb = ent
    _show(cb, mode())
    tb.setVisible(control() in ("toolbar", "both"))


def apply_all(win) -> None:
    """A delta option changed: every tab of ``win`` follows now."""
    try:
        tabs = list(win.workspaces())
    except Exception:
        tabs = []
    for ws in tabs:
        try:
            apply(ws.model)
            g = getattr(ws, "cmp_grid", None)
            ctl = getattr(g, "delta_ctl", None)
            if ctl is not None:
                ctl.place()
            if g is not None and g.active():
                QtCore.QTimer.singleShot(0, g._fit)
        except RuntimeError:            # a tab being closed
            pass
    sync_toolbar(win)

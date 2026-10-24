# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where a tab's 3-D surface and dyno panels go around its table.

With both panels open, stacking them above the table leaves the table a
strip. The layout (**View > Table panels**, or Tools > Options > Editor)
chooses:

``stack``  both above the table, one over the other (the panels' own
           splitters, as :mod:`gui_surface3d` and :mod:`gui_dyno` make them)
``side``   side by side above the table
``right``  to the right of the table, one over the other

The panels themselves are not touched: only the splitters around them are
rebuilt, so a panel's state, its toggle and its own remembered sizes stay
(in ``stack``). The other layouts remember their splitters here, per
layout. When both panels are hidden their shared area goes too.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import options as _opt

KEY = "ui/table_panels"
MODES = (("stack", "Stacked above the table"),
         ("side", "Side by side above the table"),
         ("right", "Beside the table, on the right"))
_SIZES = "tablepanels/sizes/%s"


def mode() -> str:
    m = _opt.get(KEY)
    return m if m in dict(MODES) else "stack"


def is_stacked(ws) -> bool:
    """Whether ``ws``'s panels use their own splitters (and sizes)."""
    return getattr(ws, "panel_layout", "stack") == "stack"


def install(ws, root: QtWidgets.QWidget) -> QtWidgets.QWidget:
    """``root`` is what :func:`gui_dyno.attach` returned (the stacked
    layout). Returns the widget to put in its place; the layout is applied
    to it now and whenever it changes."""
    holder = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(root)
    ws._panel_holder = holder
    ws._panel_root = root
    ws.panel_layout = "stack"
    watcher = _Watcher(ws, holder)
    for p in (ws.surface, ws.dyno_panel):
        p.installEventFilter(watcher)
    ws._panel_watcher = watcher
    if mode() != "stack":
        apply(ws, mode())
    return holder


def apply(ws, new: str) -> None:
    """Lay ``ws``'s panels out as ``new``."""
    holder = getattr(ws, "_panel_holder", None)
    if holder is None or new == getattr(ws, "panel_layout", "stack"):
        return
    surf, dyno = ws.surface, ws.dyno_panel
    table = getattr(ws, "table_area", ws.table)     # gui_cmpscroll
    hidden = {p: p.isHidden() for p in (surf, dyno)}
    focus = QtWidgets.QApplication.focusWidget()
    old = ws._panel_root
    holder.setUpdatesEnabled(False)
    try:
        V, H = QtCore.Qt.Orientation.Vertical, QtCore.Qt.Orientation.Horizontal
        if new == "stack":
            from . import gui_dyno as _gdy
            from . import gui_surface3d as _s3d
            inner = _split(V, surf, table)
            root = _split(V, dyno, inner)
            inner.splitterMoved.connect(lambda *_a: _s3d._save_sizes(ws))
            root.splitterMoved.connect(lambda *_a: _gdy._save_sizes(ws))
            ws.surface_split, ws.dyno_split = inner, root
            ws._panel_area = None
        else:
            area = _split(H if new == "side" else V, surf, dyno)
            area.setStretchFactor(1, 1)
            root = (_split(V, area, table) if new == "side"
                    else _split(H, table, area))
            if new == "side":
                root.setStretchFactor(0, 0)
                root.setStretchFactor(1, 1)
            for s in (root, area):
                s.splitterMoved.connect(lambda *_a: _save(ws))
            ws.surface_split = ws.dyno_split = root
            ws._panel_area = area
        compact = getattr(dyno, "set_compact", None)
        if compact is not None:
            compact(new != "stack")
        holder.layout().addWidget(root)
        ws._panel_root = root
        ws.panel_layout = new
        old.setParent(None)
        old.deleteLater()
        for p, h in hidden.items():
            p.setHidden(h)
        _sync_area(ws)
        _restore(ws)
    finally:
        holder.setUpdatesEnabled(True)
    if focus is not None and not focus.isHidden():
        focus.setFocus()


def apply_all(win, new: str) -> None:
    for ws in _workspaces(win):
        try:
            apply(ws, new)
        except RuntimeError:                # a tab being closed
            pass


def _workspaces(win):
    try:
        return list(win.workspaces())
    except Exception:
        return []


def _split(orient, a, b) -> QtWidgets.QSplitter:
    s = QtWidgets.QSplitter(orient)
    s.setChildrenCollapsible(False)
    s.addWidget(a)
    s.addWidget(b)
    return s


def _sync_area(ws) -> None:
    """The shared area of ``side`` / ``right`` shows while a panel does."""
    area = getattr(ws, "_panel_area", None)
    if area is None:
        return
    both_off = ws.surface.isHidden() and ws.dyno_panel.isHidden()
    if area.isHidden() != both_off:
        area.setHidden(both_off)
        if not both_off:
            _restore(ws)


def _save(ws) -> None:
    area = getattr(ws, "_panel_area", None)
    if area is None or area.isHidden():
        return
    root = ws._panel_root
    sizes = root.sizes() + area.sizes()
    if all(x > 0 for x in sizes):
        from .appsettings import settings
        st = settings()
        st.setValue(_SIZES % ws.panel_layout, ",".join(str(int(x)) for x in sizes))


def _restore(ws) -> None:
    area = getattr(ws, "_panel_area", None)
    if area is None:
        return
    from .appsettings import settings
    raw = settings().value(_SIZES % ws.panel_layout, "")
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(x) for x in raw)
    try:
        sizes = [int(x) for x in str(raw or "").split(",") if x.strip()]
    except ValueError:
        sizes = []
    root = ws._panel_root
    if len(sizes) == 4 and all(x > 0 for x in sizes):
        root.setSizes(sizes[:2])
        area.setSizes(sizes[2:])
        return
    total = sum(root.sizes()) or 800
    if ws.panel_layout == "side":
        top = int(total * 0.45)
        root.setSizes([top, max(1, total - top)])
    else:
        tw = int(total * 0.58)
        root.setSizes([tw, max(1, total - tw)])
    t2 = sum(area.sizes()) or 400
    area.setSizes([t2 // 2, t2 - t2 // 2])


class _Watcher(QtCore.QObject):
    """A panel shown or hidden: its shared area follows."""

    def __init__(self, ws, parent):
        super().__init__(parent)
        self.ws = ws

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QtCore.QEvent.Type.ShowToParent, QtCore.QEvent.Type.HideToParent):
            QtCore.QTimer.singleShot(0, self._sync)
        return False

    def _sync(self):
        try:
            _sync_area(self.ws)
        except RuntimeError:
            pass


def add_menu(win, view_menu: QtWidgets.QMenu) -> QtWidgets.QMenu:
    """View > Table panels: the layout, for every tab (an option)."""
    sub = view_menu.addMenu("Table panels")
    sub.setToolTipsVisible(True)
    group = QtGui.QActionGroup(sub)
    group.setExclusive(True)
    acts = {}
    for key, text in MODES:
        a = sub.addAction(text)
        a.setCheckable(True)
        a.setToolTip("Where the 3D surface and the dyno chart go around "
                     "the table")
        group.addAction(a)
        a.triggered.connect(lambda _c=False, key=key: _opt.set(KEY, key))
        acts[key] = a
    sub.aboutToShow.connect(lambda: acts[mode()].setChecked(True))
    return sub

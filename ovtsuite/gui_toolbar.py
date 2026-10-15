# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The main window's toolbar: buttons for menu items, chosen in Options >
Toolbar (:mod:`ovtsuite.gui_options`).

Each button IS the menu item's ``QAction`` (:mod:`ovtsuite.gui_actions`), so
it is enabled, checked and triggered exactly as the menu item is. A
sub-menu on the toolbar (Open Recent, Export ...) is a button that drops the
menu down. Right-click the toolbar for Customize toolbar...; View > Toolbar
shows or hides it.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import gui_actions as _ga
from . import options as _opt

SIZES = {"small": 16, "medium": 22, "large": 32}
STYLES = {"icons": QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly,
          "beside": QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon,
          "under": QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon}


def toolbar(win):
    return win.__dict__.get("_ovt_toolbar")


def install(win) -> QtWidgets.QToolBar:
    """The toolbar, filled from the options; and View > Toolbar."""
    tb = QtWidgets.QToolBar("Toolbar", win)
    tb.setObjectName("main_toolbar")          # so the window can restore it
    tb.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
    tb.customContextMenuRequested.connect(
        lambda pos: _context_menu(win, tb, pos))
    win.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, tb)
    win.__dict__["_ovt_toolbar"] = tb
    view = next((a.menu() for a in win.menuBar().actions()
                 if _ga.slug(a.text()) == "view" and a.menu()), None)
    if view is not None:
        act = tb.toggleViewAction()
        act.setText("Toolbar")
        first = view.actions()[0] if view.actions() else None
        view.insertAction(first, act)
        view.insertSeparator(first)
        # (triggered: by the user only -- not when the window itself hides)
        act.triggered.connect(lambda on: _opt.set("toolbar/visible", on))
    rebuild(win)
    return tb


def rebuild(win) -> None:
    """Fill the toolbar from Options again (buttons, size, style, shown)."""
    tb = toolbar(win)
    if tb is None:
        return
    win.__dict__["_ovt_tb_applying"] = True
    try:
        # (not tb.clear(): that deletes the actions, and these are the
        # menus' own)
        for a in list(tb.actions()):
            tb.removeAction(a)
            if a.isSeparator():
                a.deleteLater()
        ids = _ga.register(win)
        last_sep = True
        for aid in _opt.toolbar():
            if aid == "|":
                if not last_sep:
                    tb.addSeparator()
                    last_sep = True
                continue
            a = ids.get(aid)
            if a is None:
                continue                    # a module's item that is not there
            tb.addAction(a)
            last_sep = False
            if aid in ("edit/undo", "edit/redo"):
                from . import gui_undomenu as _gum   # the history drop-down
                _gum.decorate(win, tb, a)
            if a.menu() is not None:
                btn = tb.widgetForAction(a)
                if isinstance(btn, QtWidgets.QToolButton):
                    btn.setPopupMode(
                        QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        px = SIZES.get(_opt.get("toolbar/icon_size"), 22)
        tb.setIconSize(QtCore.QSize(px, px))
        tb.setToolButtonStyle(STYLES.get(_opt.get("toolbar/style"),
                                         STYLES["icons"]))
        for a in tb.actions():          # text-only buttons keep their style
            if not a.isSeparator() and a.icon().isNull():
                btn = tb.widgetForAction(a)
                if isinstance(btn, QtWidgets.QToolButton):
                    btn.setToolButtonStyle(
                        QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
            elif not a.isSeparator() and a.objectName() not in (
                    "edit/undo", "edit/redo"):     # (worked out when shown)
                _tooltip(win, a)
        tb.setVisible(bool(_opt.get("toolbar/visible")))
    finally:
        win.__dict__["_ovt_tb_applying"] = False


def _tooltip(win, a: QtGui.QAction) -> None:
    """A button's tooltip: its name and shortcut (the action's own longer
    tooltip, where it has one, stays in the menu's status tip)."""
    btn = toolbar(win).widgetForAction(a)
    if btn is None:
        return
    text = _ga.label(a)
    if not a.shortcut().isEmpty():
        text += "  (%s)" % a.shortcut().toString(
            QtGui.QKeySequence.SequenceFormat.NativeText)
    btn.setToolTip(text)


def _context_menu(win, tb, pos) -> None:
    m = QtWidgets.QMenu(tb)
    m.addAction("Customize toolbar...", lambda: open_customize(win))
    m.addAction("Hide toolbar", lambda: _opt.set("toolbar/visible", False))
    m.exec(tb.mapToGlobal(pos))


def open_customize(win) -> None:
    from .gui_options import open_options
    open_options(win, _opt.TOOLBAR)

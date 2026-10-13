# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The start page: what an empty tab shows (the window keeps one when no
project is open) -- the recent projects, one click each, and the ways to
start: New project, Open project, Import, Reopen last closed project.
"""

from __future__ import annotations

import os

from PySide6 import QtCore, QtWidgets


def _project_icon():
    from .gui_project import project_icon
    return project_icon()


class StartPage(QtWidgets.QWidget):
    def __init__(self, ws):
        super().__init__()
        self.ws = ws
        outer = QtWidgets.QVBoxLayout(self)
        outer.addStretch(1)
        mid = QtWidgets.QHBoxLayout()
        outer.addLayout(mid, 4)
        outer.addStretch(1)
        mid.addStretch(1)
        box = QtWidgets.QVBoxLayout()
        mid.addLayout(box, 3)
        mid.addStretch(1)

        title = QtWidgets.QLabel("Start")
        f = title.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        f.setBold(True)
        title.setFont(f)
        box.addWidget(title)

        row = QtWidgets.QHBoxLayout()
        self.buttons = {}
        for key, text, tip in (
                ("new", "New project...", "Ctrl+N"),
                ("open", "Open project...", "Ctrl+O"),
                ("import", "Import...", "A binary, container, WinOLS project "
                                        "or definition (Ctrl+Shift+I)"),
                ("reopen", "Reopen last closed", "Ctrl+Shift+T")):
            b = QtWidgets.QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, k=key: self._do(k))
            row.addWidget(b)
            self.buttons[key] = b
        row.addStretch(1)
        box.addLayout(row)

        box.addSpacing(12)
        box.addWidget(QtWidgets.QLabel("Recent projects"))
        self.recent = QtWidgets.QListWidget()
        self.recent.setAlternatingRowColors(True)
        self.recent.itemActivated.connect(self._open_item)
        self.recent.itemClicked.connect(self._open_item)
        box.addWidget(self.recent, 1)
        self.hint = QtWidgets.QLabel(
            "Or drop a project or a binary on the window.")
        self.hint.setEnabled(False)
        box.addWidget(self.hint)
        self.refresh()

    # -------------------------------------------------------------------
    def refresh(self):
        from .recent import load_recent_projects
        self.recent.clear()
        items = load_recent_projects()
        for p in items:
            it = QtWidgets.QListWidgetItem(
                "%s    %s" % (os.path.splitext(os.path.basename(p))[0],
                              os.path.dirname(p)))
            it.setData(QtCore.Qt.ItemDataRole.UserRole, p)
            it.setToolTip(p)
            it.setIcon(_project_icon())
            self.recent.addItem(it)
        if not items:
            it = QtWidgets.QListWidgetItem("(no recent projects yet)")
            it.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.recent.addItem(it)
        try:
            self.buttons["reopen"].setEnabled(bool(self.ws.win.closed_history()))
        except Exception:
            pass

    def showEvent(self, ev):
        self.refresh()
        super().showEvent(ev)

    def _open_item(self, item):
        path = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if path:
            from . import gui_files as _gf
            _gf.open_any(self.ws.win, path)

    def _do(self, key):
        win = self.ws.win
        from . import gui_files as _gf
        from . import gui_project as _gp
        if key == "new":
            _gp.new_project(win)
        elif key == "open":
            _gf.open_project_dialog(win)
        elif key == "import":
            _gf.import_dialog(win, "new")
        elif key == "reopen":
            win.reopen_last_closed()


def wrap(ws, content):
    """A stack of the start page and the tab's own ``content``."""
    stack = QtWidgets.QStackedWidget()
    stack.addWidget(content)
    ws._start_page = None
    ws._start_stack = stack
    sync(ws)
    return stack


def sync(ws):
    """Show the start page while the tab is empty, its content otherwise."""
    stack = getattr(ws, "_start_stack", None)
    if stack is None:
        return
    if ws.primary is None:
        if ws._start_page is None:
            ws._start_page = StartPage(ws)
            stack.addWidget(ws._start_page)
        stack.setCurrentWidget(ws._start_page)
    else:
        stack.setCurrentIndex(0)


__all__ = ["StartPage", "wrap", "sync"]

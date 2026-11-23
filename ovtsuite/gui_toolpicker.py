# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The tool picker: one small window that starts the app or any standalone tool.

macOS ships ONE app, ``OVT-Suite.app``, and it is the launcher: opening it shows this
window, with the editor (the main app) and each standalone tool (Diag, Flasher,
Logger, Simulator, Sniffer, Converter) as a tile with its own icon. (Windows has a
launcher exe per tool, ``ovtsuite.launcher``.) A click starts that program as a
process of its own -- the same executable with the tool's flag -- and closes the
picker. Files opened with the app (double-click, drop on the icon) go to the editor.
Its Dock icon also lists them (:func:`install_dock_menu`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Tuple

from . import branding

#: (flag, tool id, label): the tools in the order they are shown; the first tile is the app
TOOLS: List[Tuple[Optional[str], str, str]] = [
    (None, "", branding.APP_NAME),
    ("--diag", "diag", branding.TOOL_NAMES["diag"]),
    ("--flasher", "flasher", branding.TOOL_NAMES["flasher"]),
    ("--logger", "logger", branding.TOOL_NAMES["logger"]),
    ("--simulator", "simulator", branding.TOOL_NAMES["simulator"]),
    ("--sniffer", "sniffer", branding.TOOL_NAMES["sniffer"]),
    ("--converter", "converter", branding.TOOL_NAMES["converter"]),
]


def icon_path(tool: str) -> str:
    name = "ovtsuite_%s.ico" % tool if tool else "ovtsuite.ico"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", name)


def start(flag: Optional[str], args: Optional[List[str]] = None) -> None:
    """Start the app (``flag`` None) or a tool as a process of its own."""
    cmd = [sys.executable] + ([flag] if flag else []) + list(args or [])
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def install_dock_menu(app) -> None:
    """macOS: the Dock menu of this process lists the app and every tool. Best effort;
    a no-op elsewhere."""
    if sys.platform != "darwin":
        return
    try:
        from PySide6 import QtGui, QtWidgets
        menu = QtWidgets.QMenu()
        for flag, tool, label in TOOLS:
            a = menu.addAction(QtGui.QIcon(icon_path(tool)), label)
            a.triggered.connect(lambda _c=False, f=flag: start(f))
        menu.setAsDockMenu()
        app._ovt_dock_menu = menu                 # keep it alive
    except Exception:
        pass


def run_picker() -> int:
    from PySide6 import QtCore, QtGui, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName(branding.APP_NAME)
    app.setOrganizationName(branding.ORG)
    app.setWindowIcon(QtGui.QIcon(icon_path("")))
    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle(branding.APP_NAME)

    class _Files(QtCore.QObject):
        """A file opened with the app (Finder double-click, dropped on the icon)."""
        def eventFilter(self, obj, ev):
            if ev.type() == QtCore.QEvent.Type.FileOpen:
                path = ev.file()
                start(None, [path] if path.lower().endswith(".ovtmod") else ["gui", path])
                dlg.accept()
                return True
            return False
    files = _Files(dlg)
    app.installEventFilter(files)
    grid = QtWidgets.QGridLayout(dlg)
    grid.setSpacing(10)
    grid.setContentsMargins(16, 16, 16, 16)
    cols = 4
    for i, (flag, tool, label) in enumerate(TOOLS):
        b = QtWidgets.QToolButton()
        b.setText(label.replace("OVT ", "OVT\n", 1) if label != branding.APP_NAME else label)
        b.setIcon(QtGui.QIcon(icon_path(tool)))
        b.setIconSize(QtCore.QSize(64, 64))
        b.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        b.setFixedSize(112, 110)
        b.setToolTip("Start %s" % label)
        b.clicked.connect(lambda _c=False, f=flag: (start(f), dlg.accept()))
        grid.addWidget(b, i // cols, i % cols)
    dlg.exec()
    return 0

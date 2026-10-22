# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Toolbar and menu icons: Lucide SVGs (``data/icons``, ISC licence, see
Help > Credits & licences) drawn in the palette's text colour.

An :class:`_SvgEngine` draws the outline icon at whatever size is asked for,
in the colour of the palette in use *when it is drawn* -- so the icons follow
a switch to the dark theme without being rebuilt, and a disabled action's
icon is drawn in the disabled text colour.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from PySide6 import QtCore, QtGui, QtSvg

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "icons")

#: action id (ovtsuite.gui_actions) -> icon name
FOR_ACTION = {
    "file/new-project": "file-plus",
    "file/open-project": "folder-open",
    "file/open-recent": "history",
    "file/reopen-last-closed-project": "archive-restore",
    "file/import": "import",
    "file/import-as-comparison": "git-compare",
    "file/compare-with-open-tab": "columns-2",
    "file/compare-with-saved-version": "git-compare-arrows",
    "file/file-information": "info",
    "file/project-information": "car",
    "file/close-tab": "x",
    "file/close-comparison": "circle-x",
    "file/save": "save",
    "file/save-this-branch-only": "git-branch",
    "file/save-as": "save-all",
    "file/close-project": "file-x",
    "file/export": "file-output",
    "file/new-window": "app-window",
    "file/exit": "log-out",
    "edit/undo": "undo-2",
    "edit/redo": "redo-2",
    "edit/cells/revert-to-last-saved-values": "archive-restore",
    "edit/find": "search",
    "edit/find-panel": "search",
    "edit/find-in-definition": "file-search",
    "edit/scale-selection": "percent",
    "edit/copy-cells": "copy",
    "edit/paste-cells": "clipboard-paste",
    "edit/copy-values-from-tab": "files",
    "edit/show-differences": "diff",
    "edit/generate-patch": "file-diff",
    "edit/apply-patch": "bandage",
    "edit/remove-patch": "eraser",
    "edit/version-history": "history",
    "view/swap-x-and-y-axes": "arrow-right-left",
    "view/keep-table-open": "pin",
    "view/close-table": "file-x",
    "view/next-table": "chevron-right",
    "view/previous-table": "chevron-left",
    "view/project-panel": "panel-left",
    "tools/dtc-fault-path-editor": "triangle-alert",
    "tools/measuring-blocks": "gauge",
    "tools/gear-ratios": "cog",
    "tools/vss-pulse-settings": "activity",
    "tools/hex-editor": "binary",
    "tools/verify-checksums": "shield-check",
    "tools/eeprom-editor": "memory-stick",
    "tools/ecu-read-write": "cpu",
    "tools/data-logging": "chart-line",
    "tools/tune-from-logs": "sliders-horizontal",
    "tools/end-of-injection": "syringe",
    "tools/dyno-chart": "chart-spline",
    "tools/bus-sniffer-j2534": "radio",
    "tools/ecu-simulator-bench": "flask-conical",
    "tools/ai/assistant": "sparkles",
    "tools/modules": "puzzle",
    "tools/options": "settings",
    "help/help-contents": "circle-help",
    "help/about": "info",
}


@lru_cache(maxsize=None)
def _svg(name: str) -> Optional[bytes]:
    path = os.path.join(ICON_DIR, name + ".svg")
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


class _SvgEngine(QtGui.QIconEngine):
    def __init__(self, name: str, data: bytes):
        super().__init__()
        self.name = name
        self.data = data

    def _color(self, mode) -> QtGui.QColor:
        pal = QtGui.QGuiApplication.palette()
        M = QtGui.QIcon.Mode
        if mode == M.Disabled:
            return pal.color(QtGui.QPalette.ColorGroup.Disabled,
                             QtGui.QPalette.ColorRole.WindowText)
        # (Selected too: the Windows 11 style draws a selected row's text in
        # the normal colour on a pale highlight -- HighlightedText would be
        # white on it)
        return pal.color(QtGui.QPalette.ColorRole.WindowText)

    def paint(self, painter, rect, mode, state):
        svg = self.data.replace(b"currentColor",
                                self._color(mode).name().encode("ascii"))
        r = QtSvg.QSvgRenderer(QtCore.QByteArray(svg))
        side = min(rect.width(), rect.height())
        target = QtCore.QRectF(rect.x() + (rect.width() - side) / 2,
                               rect.y() + (rect.height() - side) / 2, side, side)
        r.render(painter, target)

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size, mode, state, scale):
        pm = QtGui.QPixmap(QtCore.QSize(max(1, round(size.width() * scale)),
                                        max(1, round(size.height() * scale))))
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.paint(p, QtCore.QRect(QtCore.QPoint(0, 0), pm.size()), mode, state)
        p.end()
        pm.setDevicePixelRatio(scale)
        return pm

    def clone(self):
        return _SvgEngine(self.name, self.data)

    def key(self):
        return "ovt-lucide"

    def iconName(self):
        return self.name


def icon(name: str) -> QtGui.QIcon:
    """The named icon (an empty QIcon when there is no such file)."""
    data = _svg(name)
    if data is None:
        return QtGui.QIcon()
    return QtGui.QIcon(_SvgEngine(name, data))


def for_action(action_id: str) -> QtGui.QIcon:
    name = FOR_ACTION.get(action_id)
    return icon(name) if name else QtGui.QIcon()

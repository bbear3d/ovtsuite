# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Small helpers that keep a dialog the same shape whatever it is showing.

* ``retain_space`` -- a hidden widget keeps the room it takes, so showing or
  hiding it (an option that applies to one control unit only) does not move
  everything under it.
* ``reserve_lines`` -- a word-wrapped label keeps the height of its longest
  text, so swapping the text does not resize the window.
* ``fit_width`` -- a button wide enough for the longest of its labels.
* ``PercentBar`` -- a progress bar drawn by a style sheet in the palette's
  colours, so the percentage shows in every style (the native macOS bar draws
  none) and in both themes.
* ``load_size`` / ``save_size`` -- a dialog's remembered size (size only, never
  the position).
"""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6 import QtCore, QtGui, QtWidgets


def retain_space(*widgets) -> None:
    """Keep the room of every widget in ``widgets`` while it is hidden."""
    for w in widgets:
        sp = w.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        w.setSizePolicy(sp)


def reserve_lines(label: QtWidgets.QLabel, lines: int) -> None:
    """At least ``lines`` lines of height for ``label``."""
    label.setMinimumHeight(label.fontMetrics().lineSpacing() * int(lines) + 2)


def fit_width(button: QtWidgets.QAbstractButton, texts: Iterable[str]) -> None:
    """A minimum width that holds the widest of ``texts`` in ``button``."""
    keep = button.text()
    widest = 0
    for t in list(texts) + [keep]:
        button.setText(t)
        widest = max(widest, button.sizeHint().width())
    button.setText(keep)
    button.setMinimumWidth(widest)


class PercentBar(QtWidgets.QProgressBar):
    """A progress bar with the percentage in it, in any style and either theme."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextVisible(True)
        self.setFormat("%p%")
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._in_sheet = False
        self.sheet()

    def sheet(self) -> None:
        pal = self.palette()
        role = QtGui.QPalette.ColorRole
        self.setStyleSheet(style_sheet(pal.color(role.Base).name(),
                                       pal.color(role.Mid).name(),
                                       pal.color(role.Text).name(),
                                       pal.color(role.Highlight).name()))

    def changeEvent(self, ev):
        if ev.type() == QtCore.QEvent.Type.PaletteChange and not self._in_sheet:
            self._in_sheet = True            # (the sheet changes the palette)
            try:
                self.sheet()
            finally:
                self._in_sheet = False
        super().changeEvent(ev)


def style_sheet(base: str, edge: str, text: str, fill: str) -> str:
    return ("QProgressBar { border: 1px solid %s; border-radius: 4px; "
            "background: %s; color: %s; text-align: center; min-height: 16px; } "
            "QProgressBar::chunk { background: %s; border-radius: 3px; }"
            % (edge, base, text, fill))


def load_size(settings_key: str) -> Optional[QtCore.QSize]:
    """The size remembered under ``settings_key``, or None."""
    try:
        from .. import branding
        s = QtCore.QSettings(*branding.QSETTINGS)
        w = int(s.value(settings_key + "/w", 0) or 0)
        h = int(s.value(settings_key + "/h", 0) or 0)
    except Exception:                        # noqa: BLE001 - a convenience only
        return None
    return QtCore.QSize(w, h) if w > 0 and h > 0 else None


def save_size(settings_key: str, size: QtCore.QSize) -> None:
    try:
        from .. import branding
        s = QtCore.QSettings(*branding.QSETTINGS)
        s.setValue(settings_key + "/w", int(size.width()))
        s.setValue(settings_key + "/h", int(size.height()))
    except Exception:                        # noqa: BLE001
        pass

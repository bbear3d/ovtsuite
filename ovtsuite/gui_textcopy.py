# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Ctrl+C on selected text in a read-only text view.

Edit > Copy cells owns Ctrl+C for the whole main window. A text box you can
type in (and a plain read-only ``QTextEdit``) tells Qt it wants that key
(``QEvent.ShortcutOverride``) and gets it; a ``QTextBrowser`` -- the AI
assistant's conversation, the update notes, a findings list -- and a
selectable ``QLabel`` do not, so the window's shortcut took the key, copied
(nothing of) the table instead and the selected words stayed uncopied.

:func:`install` puts one filter on the application: when the key is Copy (or
Select All) and the focused widget is such a view with text selected (or,
for Select All, one that can select), the view keeps the key. Everything
else -- a table in focus, a view with nothing selected -- is left to the
window's own shortcut as before.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

_COPY = QtGui.QKeySequence.StandardKey.Copy
_SELECT_ALL = QtGui.QKeySequence.StandardKey.SelectAll


def _selectable_text(w) -> bool:
    """``w`` is a read-only view of text the user may select."""
    if isinstance(w, QtWidgets.QTextEdit):          # QTextBrowser is one
        return w.isReadOnly() and bool(
            w.textInteractionFlags()
            & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    if isinstance(w, QtWidgets.QPlainTextEdit):
        return w.isReadOnly()
    if isinstance(w, QtWidgets.QLabel):
        return bool(w.textInteractionFlags()
                    & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    return False


def _has_selection(w) -> bool:
    if isinstance(w, (QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit)):
        return w.textCursor().hasSelection()
    if isinstance(w, QtWidgets.QLabel):
        return w.hasSelectedText()
    return False


def keeps_key(w, ev) -> bool:
    """Whether the key press ``ev`` (a ShortcutOverride) belongs to ``w``
    rather than to a window shortcut."""
    if not _selectable_text(w):
        return False
    if ev.matches(_COPY):
        return _has_selection(w)
    if ev.matches(_SELECT_ALL):
        # a QLabel selects all only through the mouse; leave Ctrl+A alone
        return not isinstance(w, QtWidgets.QLabel)
    return False


class _Filter(QtCore.QObject):
    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.ShortcutOverride \
                and isinstance(obj, QtWidgets.QWidget) and keeps_key(obj, ev):
            ev.accept()
            return True
        return False


_FILTER = None


def install(app=None) -> bool:
    """Put the filter on ``app`` (once). True when it was added."""
    global _FILTER
    app = app or QtWidgets.QApplication.instance()
    if app is None:
        return False
    if _FILTER is not None and _FILTER.parent() is app:
        return False
    _FILTER = _Filter(app)
    app.installEventFilter(_FILTER)
    return True

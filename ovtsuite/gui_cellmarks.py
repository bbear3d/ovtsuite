# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Cell colours the item-based editing windows share -- the fault-code
windows (:mod:`ovtsuite.gui_dtc`, :mod:`ovtsuite.gui_tcu_dtc`), the
measuring blocks (:mod:`ovtsuite.gui_msblock`) and the shift / lock-up
editors (:mod:`ovtsuite.gui_gearbox_compare`) -- so they read like the main
table editor:

* **differs from the comparison**: ``gui.DIFF_BG`` (opaque orange) with
  ``gui.CELL_DARK_FG`` text, in the primary's cell and the comparison's
  copy, the same in either theme (:func:`mark_differs`);
* **changed, not applied yet**: :data:`PENDING_BG` (pale yellow), bold, dark
  text (:func:`mark_pending`). It takes the place of orange in the primary;
  the comparison's copy stays orange where it differs;
* **stored on the ECU** (the fault-code windows, after reading the codes
  from the car): :data:`STORED_BG` (light violet), dark text -- a meaning the
  rest of the app has no colour for, a hue apart from orange and yellow;
  below the other two in a primary cell.

The tables using these turn on alternating row colours: the Windows 11 style
then draws the selection in the solid accent colour, which shows over a
filled cell (its faint default selection does not). Their delegate
(:class:`CellDelegate`, or :func:`selection_text` in a delegate of their own)
writes a selected cell's text in the selected-text colour.

Kept light (Qt only): ``gui`` is imported when a colour is first asked for.
"""
from __future__ import annotations

from PySide6 import QtGui, QtWidgets

#: a cell changed in the window, not applied yet (bold, dark text)
PENDING_BG = QtGui.QColor(255, 236, 170)
#: a fault the car's ECU has stored (read with Read codes from ECU...)
STORED_BG = QtGui.QColor(218, 202, 255)
#: text of a switched-off fault / an empty slot (either theme)
OFF_FG = QtGui.QColor(150, 150, 150)


def diff_colors():
    """``(gui.DIFF_BG, gui.CELL_DARK_FG)``."""
    from . import gui as _g
    return _g.DIFF_BG, _g.CELL_DARK_FG


def dark_fg() -> QtGui.QColor:
    from . import gui as _g
    return _g.CELL_DARK_FG


def mark_differs(item: QtWidgets.QTableWidgetItem) -> None:
    """``item`` as differing from the comparison: orange, dark text."""
    bg, fg = diff_colors()
    item.setBackground(QtGui.QBrush(bg))
    item.setForeground(QtGui.QBrush(fg))


def mark_pending(item: QtWidgets.QTableWidgetItem, font: QtGui.QFont) -> None:
    """``item`` as changed but not applied: :data:`PENDING_BG`, bold, dark
    text (``font`` is the table's; a bold copy is set)."""
    item.setBackground(QtGui.QBrush(PENDING_BG))
    item.setForeground(QtGui.QBrush(dark_fg()))
    bold = QtGui.QFont(font)
    bold.setBold(True)
    item.setFont(bold)


def mark_stored(item: QtWidgets.QTableWidgetItem) -> None:
    """``item`` on a fault the ECU has stored: :data:`STORED_BG`, dark text."""
    item.setBackground(QtGui.QBrush(STORED_BG))
    item.setForeground(QtGui.QBrush(dark_fg()))


def selection_text(opt: QtWidgets.QStyleOptionViewItem) -> None:
    """A selected cell's text in the theme's selected-text colour, whatever
    colour the cell gives it otherwise: the dark text of a shaded cell or the
    comparison's quiet grey would be hard to read on the solid accent."""
    if opt.state & QtWidgets.QStyle.StateFlag.State_Selected:
        hl = opt.palette.color(QtGui.QPalette.ColorRole.HighlightedText)
        opt.palette.setColor(QtGui.QPalette.ColorRole.Text, hl)
        opt.palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, hl)


class CellDelegate(QtWidgets.QStyledItemDelegate):
    """The tables' delegate: :func:`selection_text` on the stock drawing."""

    def initStyleOption(self, opt, index):
        super().initStyleOption(opt, index)
        selection_text(opt)


def cmp_fg(widget: QtWidgets.QWidget) -> QtGui.QColor:
    """The read-only comparison table's text: the theme's text colour a third
    of the way to the background -- quieter than the primary's, louder than
    :data:`OFF_FG`, in either theme."""
    pal = widget.palette()
    t = pal.color(QtGui.QPalette.ColorRole.Text)
    b = pal.color(QtGui.QPalette.ColorRole.Base)
    f = 0.3
    return QtGui.QColor(round(t.red() + (b.red() - t.red()) * f),
                        round(t.green() + (b.green() - t.green()) * f),
                        round(t.blue() + (b.blue() - t.blue()) * f))

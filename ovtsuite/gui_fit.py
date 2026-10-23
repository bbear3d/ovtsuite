# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Window sizes that follow the content and stay on the screen.

A tool window opens wide enough for its table (the last, stretching column
capped so a long description does not ask for the whole screen) and never
larger than the screen it opens on.
"""

from PySide6 import QtGui, QtWidgets


def screen_of(widget):
    """The screen ``widget`` is on (or will open on), or the primary one."""
    scr = None
    try:
        scr = widget.screen()
    except Exception:
        pass
    if scr is None and widget.parentWidget() is not None:
        try:
            scr = widget.parentWidget().screen()
        except Exception:
            scr = None
    return scr or QtGui.QGuiApplication.primaryScreen()


def available(widget):
    """The available geometry of ``widget``'s screen, or None."""
    scr = screen_of(widget)
    return scr.availableGeometry() if scr is not None else None


def table_width(table: QtWidgets.QTableView, last_cap: int = 520,
                last_min: int = 240) -> int:
    """The width ``table`` needs to show every column at its current size,
    with the last column at its contents' width clamped to
    [``last_min``, ``last_cap``]."""
    hh = table.horizontalHeader()
    n = hh.count()
    w = 0
    last = -1
    for i in range(n):
        if not hh.isSectionHidden(i):
            last = i
    for i in range(n):
        if hh.isSectionHidden(i):
            continue
        if i == last:
            want = max(table.sizeHintForColumn(i), hh.sectionSizeFromContents(i).width())
            w += max(last_min, min(last_cap, want))
        else:
            w += hh.sectionSize(i)
    vh = table.verticalHeader()
    if vh.isVisible():
        w += vh.width()
    w += table.verticalScrollBar().sizeHint().width()
    w += 2 * table.frameWidth() + 4
    return w


def fit_to_screen(win: QtWidgets.QWidget, w: int, h: int,
                  frac_w: float = 0.92, frac_h: float = 0.88) -> None:
    """Resize ``win`` to ``w`` x ``h``, or smaller where its screen is."""
    g = available(win)
    if g is not None:
        w = min(w, int(g.width() * frac_w))
        h = min(h, int(g.height() * frac_h))
    win.resize(max(w, 200), max(h, 150))


def keep_on_screen(win: QtWidgets.QWidget) -> None:
    """Move a shown window so all of its frame is on its screen."""
    g = available(win)
    if g is None:
        return
    fr = win.frameGeometry()
    x = max(g.left(), min(fr.left(), g.right() - fr.width() + 1))
    y = max(g.top(), min(fr.top(), g.bottom() - fr.height() + 1))
    if (x, y) != (fr.left(), fr.top()):
        win.move(x, y)


def fit_table_window(win: QtWidgets.QWidget, table: QtWidgets.QTableView,
                     min_w: int, h: int, last_cap: int = 520,
                     extra: int = 0) -> None:
    """Size ``win`` so ``table`` shows its columns (see :func:`table_width`)
    plus ``extra`` pixels of chrome, at least ``min_w`` wide, ``h`` tall,
    and within the screen."""
    lay = win.layout()
    margins = 0
    if lay is not None:
        m = lay.contentsMargins()
        margins = m.left() + m.right()
    w = max(min_w, table_width(table, last_cap) + margins + extra)
    fit_to_screen(win, w, h)

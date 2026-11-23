# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The shared cell drawing (ovtsuite.gui_cellmarks): a selected cell's text
stays legible with and without focus, in a light and a dark palette -- the
white selected-text colour vanished on a light inactive selection."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore, QtGui  # noqa: E402

pytestmark = pytest.mark.slow

P = QtGui.QPalette
S = QtWidgets.QStyle.StateFlag


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _palette(dark):
    """A light / dark palette whose inactive highlight is a light / dark grey
    (as a view without focus draws it) and whose selected-text colour is
    white in every group."""
    if dark:
        from ovtsuite.gui_theme import dark_palette
        pal = dark_palette()
        pal.setColor(P.ColorGroup.Inactive, P.ColorRole.Highlight, QtGui.QColor(70, 70, 74))
    else:
        pal = QtGui.QPalette()
        for role, col in ((P.ColorRole.Base, QtGui.QColor(255, 255, 255)),
                          (P.ColorRole.Text, QtGui.QColor(0, 0, 0)),
                          (P.ColorRole.Highlight, QtGui.QColor(0, 103, 192))):
            pal.setColor(role, col)
        pal.setColor(P.ColorGroup.Inactive, P.ColorRole.Highlight, QtGui.QColor(229, 229, 229))
    pal.setColor(P.ColorRole.HighlightedText, QtGui.QColor(255, 255, 255))
    return pal


def _table(app, pal, alternating=True):
    t = QtWidgets.QTableWidget(1, 1)
    t.setPalette(pal)
    t.setAlternatingRowColors(alternating)
    it = QtWidgets.QTableWidgetItem("12 - Engine speed")
    # a shaded cell's dark text, which the accent would swallow
    it.setForeground(QtGui.QBrush(QtGui.QColor(20, 20, 20)))
    t.setItem(0, 0, it)
    return t


def _drawn_text(t, active):
    """The text colour CellDelegate gives the selected cell, and the palette
    group it is drawn in."""
    from ovtsuite.gui_cellmarks import CellDelegate
    d = CellDelegate(t)
    opt = QtWidgets.QStyleOptionViewItem()
    opt.initFrom(t)
    opt.widget = t
    opt.palette = QtGui.QPalette(t.palette())
    opt.state = S.State_Enabled | S.State_Selected | (S.State_Active if active else S.State_None)
    cg = P.ColorGroup.Active if active else P.ColorGroup.Inactive
    opt.palette.setCurrentColorGroup(cg)
    d.initStyleOption(opt, t.model().index(0, 0))
    return opt.palette.color(cg, P.ColorRole.Text), opt.palette.color(cg, P.ColorRole.Highlight)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("active", [True, False], ids=["focused", "not-focused"])
def test_selected_text_contrasts_with_the_highlight_drawn(app, dark, active):
    from ovtsuite.gui_cellmarks import MIN_CONTRAST, contrast
    t = _table(app, _palette(dark))
    text, hl = _drawn_text(t, active)
    assert contrast(text, hl) >= MIN_CONTRAST, (text.name(), hl.name())
    if active:                       # the solid accent: the selected-text colour
        assert text == QtGui.QColor(255, 255, 255)
    t.deleteLater()


def test_light_inactive_selection_gets_dark_text(app):
    """The reported case: light theme, the table without focus, a light grey
    selection -- the text is dark, not white."""
    t = _table(app, _palette(False))
    text, hl = _drawn_text(t, active=False)
    assert text.lightness() < 100 and hl.lightness() > 200
    t.deleteLater()


def test_contrast_ratio():
    from ovtsuite.gui_cellmarks import contrast
    black, white = QtGui.QColor(0, 0, 0), QtGui.QColor(255, 255, 255)
    assert round(contrast(black, white)) == 21 and contrast(white, white) == 1


# -------------------------------------------------- the difference outline
def _grab(t):
    t.resize(260, 120)
    t.show()
    QtWidgets.QApplication.processEvents()
    return t.viewport().grab().toImage()


def _px(img, rect, dx, dy):
    return QtGui.QColor(img.pixel(rect.x() + dx, rect.y() + dy))


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_differing_cell_is_framed_not_filled(app, dark):
    """mark_differs frames the cell in the orange outline and leaves its own
    fill (a shade, the pending yellow) and text alone; a plain cell has no
    frame; a selected cell keeps the frame over the accent."""
    from ovtsuite.gui_cellmarks import (DIFF_OUTLINE, OUTLINE_ROLE, CellDelegate,
                                        mark_differs)
    t = QtWidgets.QTableWidget(2, 2)
    t.setPalette(_palette(dark))
    t.setItemDelegate(CellDelegate(t))
    t.setAlternatingRowColors(True)
    shade = QtGui.QColor(200, 230, 200)
    for r in range(2):
        for c in range(2):
            it = QtWidgets.QTableWidgetItem("5")
            it.setBackground(QtGui.QBrush(shade))
            t.setItem(r, c, it)
    mark_differs(t.item(0, 0))
    assert t.item(0, 0).data(OUTLINE_ROLE) is True
    assert t.item(0, 0).background().color() == shade          # fill stays
    img = _grab(t)
    r0 = t.visualRect(t.model().index(0, 0))
    r1 = t.visualRect(t.model().index(0, 1))
    assert _px(img, r0, 0, r0.height() // 2) == DIFF_OUTLINE   # left edge
    assert _px(img, r0, r0.width() // 2, 0) == DIFF_OUTLINE     # top edge
    assert _px(img, r1, r1.width() - 8, 6) == shade              # unmarked: fill
    assert _px(img, r1, 0, r1.height() // 2) != DIFF_OUTLINE     # not framed
    t.selectRow(0)
    QtWidgets.QApplication.processEvents()
    img = _grab(t)
    assert _px(img, r0, 0, r0.height() // 2) == DIFF_OUTLINE     # over a selection
    t.deleteLater()


def test_outline_delegate_for_a_model_view(app):
    """A model answering OUTLINE_ROLE gets the frame from OutlineDelegate."""
    from ovtsuite.gui_cellmarks import DIFF_OUTLINE, OUTLINE_ROLE, OutlineDelegate

    class M(QtCore.QAbstractTableModel):
        def rowCount(self, _=QtCore.QModelIndex()):
            return 1

        def columnCount(self, _=QtCore.QModelIndex()):
            return 2

        def data(self, ix, role=QtCore.Qt.DisplayRole):
            if role == OUTLINE_ROLE:
                return True if ix.column() == 1 else None
            return "1" if role == QtCore.Qt.DisplayRole else None

    v = QtWidgets.QTableView()
    v.setModel(M())
    v.setItemDelegate(OutlineDelegate(v))
    img = _grab(v)
    a = v.visualRect(v.model().index(0, 0))
    b = v.visualRect(v.model().index(0, 1))
    assert _px(img, b, 0, b.height() // 2) == DIFF_OUTLINE
    assert _px(img, a, 0, a.height() // 2) != DIFF_OUTLINE
    v.deleteLater()


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("active", [True, False])
def test_outline_delegate_selected_heat_cell_is_legible(app, dark, active):
    """A heat-coloured cell (dark text role, light fill) selected in a view
    drawn with OutlineDelegate -- the main editor's table and the comparison
    grids -- keeps text that contrasts with the selection actually painted,
    focused or not, in both palettes (the inactive highlight of the light one
    is pale, under the white selected-text colour)."""
    from ovtsuite.gui_cellmarks import OutlineDelegate, contrast

    class M(QtCore.QAbstractTableModel):
        def rowCount(self, _=QtCore.QModelIndex()):
            return 2

        def columnCount(self, _=QtCore.QModelIndex()):
            return 2

        def data(self, ix, role=QtCore.Qt.DisplayRole):
            if role == QtCore.Qt.DisplayRole:
                return "88"
            if role == QtCore.Qt.BackgroundRole:
                return QtGui.QBrush(QtGui.QColor(160, 230, 170))
            if role == QtCore.Qt.ForegroundRole:
                return QtGui.QBrush(QtGui.QColor(20, 20, 20))

    pal = _palette(dark)
    if not dark:
        pal.setColor(P.ColorGroup.Inactive, P.ColorRole.Highlight,
                     QtGui.QColor(215, 215, 215))
        pal.setColor(P.ColorGroup.Inactive, P.ColorRole.HighlightedText,
                     QtGui.QColor(255, 255, 255))
    other = QtWidgets.QWidget()
    v = QtWidgets.QTableView()
    v.setModel(M())
    v.setItemDelegate(OutlineDelegate(v))
    v.setPalette(pal)
    v.setAlternatingRowColors(True)
    v.resize(300, 150)
    v.show()
    if not active:
        other.show()
        other.activateWindow()
    else:
        v.activateWindow()
    v.selectRow(0)
    QtWidgets.QApplication.processEvents()
    img = v.viewport().grab().toImage()
    r = v.visualRect(v.model().index(0, 0))
    counts = {}
    for x in range(r.left() + 2, r.right() - 2):
        for y in range(r.top() + 2, r.bottom() - 2):
            k = img.pixelColor(x, y).rgb()
            counts[k] = counts.get(k, 0) + 1
    bg = QtGui.QColor.fromRgb(max(counts, key=counts.get))
    txt = max((QtGui.QColor.fromRgb(k) for k in counts),
              key=lambda c: contrast(c, bg))
    assert contrast(bg, txt) >= 3.0, (bg.getRgb(), txt.getRgb())
    v.deleteLater()
    other.deleteLater()

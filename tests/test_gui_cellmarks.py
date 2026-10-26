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
from PySide6 import QtGui  # noqa: E402

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

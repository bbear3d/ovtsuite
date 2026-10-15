# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The table's selection stays on one side of the comparison banner
(ovtsuite.gui_tablesel), on a stand-in grid: 3 primary rows, the banner
(row 3), 3 comparison rows."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import gui_tablesel as T  # noqa: E402

pytestmark = pytest.mark.slow

SEL = QtCore.QItemSelectionModel.SelectionFlag


class _Grid(QtGui.QStandardItemModel):
    def __init__(self, sep=3):
        super().__init__(7, 4)
        self.sep = sep

    def sep_row(self):
        return self.sep


@pytest.fixture
def ws():
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    m = _Grid()
    t = QtWidgets.QTableView()
    t.setModel(m)
    t.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
    w = SimpleNamespace(table=t, model=m)
    T.attach(w)
    yield w
    t.deleteLater()


def _rows(w):
    return sorted({i.row() for i in w.table.selectionModel().selectedIndexes()})


def _range(w, r0, r1, flags=SEL.Select):
    s = QtCore.QItemSelection(w.model.index(r0, 0), w.model.index(r1, 3))
    w.table.selectionModel().select(s, flags)


def test_a_selection_across_the_banner_keeps_the_half_it_started_in(ws):
    sm = ws.table.selectionModel()
    sm.setCurrentIndex(ws.model.index(1, 1), SEL.ClearAndSelect)
    _range(ws, 1, 5)
    assert _rows(ws) == [1, 2]


def test_a_comparison_selection_stays_in_the_comparison(ws):
    sm = ws.table.selectionModel()
    sm.setCurrentIndex(ws.model.index(5, 1), SEL.ClearAndSelect)
    _range(ws, 0, 6)
    assert _rows(ws) == [4, 5, 6]


def test_a_click_in_the_other_half_starts_there(ws):
    sm = ws.table.selectionModel()
    sm.setCurrentIndex(ws.model.index(1, 1), SEL.ClearAndSelect)
    _range(ws, 0, 2)
    # Ctrl-click a comparison cell: a press there, then its selection
    ws.table.show()
    QtWidgets.QApplication.processEvents()
    rect = ws.table.visualRect(ws.model.index(5, 1))
    from PySide6.QtTest import QTest
    QTest.mouseClick(ws.table.viewport(), QtCore.Qt.LeftButton,
                     QtCore.Qt.ControlModifier, rect.center())
    assert _rows(ws) == [5]


def test_without_a_comparison_nothing_is_dropped(ws):
    ws.model.sep = None
    sm = ws.table.selectionModel()
    sm.setCurrentIndex(ws.model.index(1, 1), SEL.ClearAndSelect)
    _range(ws, 0, 6)
    assert _rows(ws) == list(range(7))

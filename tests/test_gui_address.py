# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The selected cells' address at the right of the status bar
(ovtsuite.gui_address): one cell, a range, an axis point, blank for the
corner and with no table; a status message does not hide it; the hex
editor's picked byte shows there until it closes."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

pytestmark = pytest.mark.slow          # the main window


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever the window remembers goes to a file of the test's own."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app):
    from ovtsuite import gui
    w = gui.MainWindow()
    w.show()
    yield w
    w.close_discarding()


def _map():
    from ovtsuite.binary import BinaryFile
    from ovtsuite.model import Axis, Entity
    bf = BinaryFile(bytes(0x2000), name="synthetic.bin", endian=">")
    e = Entity(name="T", kind="map", tier=1, categories=["Misc"],
               description="", units="", addr=0x1000, rows=3, cols=4,
               bits=16, signed=False, equation="X", source="verified")
    e.binfile = bf
    e.x = Axis(addr=0x800, count=4, bits=16)
    e.y = Axis(addr=0x900, count=3, bits=16)
    return e


def test_the_status_bar_shows_where_the_cells_are(win, app):
    from ovtsuite import gui_address
    lab = gui_address.label(win)
    ws = win.current
    assert lab is not None and lab.text() == ""
    ws.model.set_entity(_map(), None)
    m, t = ws.model, ws.table
    sm = t.selectionModel()
    F = QtCore.QItemSelectionModel.SelectionFlag

    def grid(kind, a, b):
        for r in range(m.rowCount()):
            for c in range(m.columnCount()):
                if m._kind(r, c) == (kind, a, b):
                    return m.index(r, c)
        raise AssertionError((kind, a, b))
    sm.setCurrentIndex(grid("z", 1, 0), F.ClearAndSelect)
    assert lab.text() == "0x001002"
    sm.select(grid("z", 0, 1), F.Select)
    assert lab.text() == "0x001002–0x001006 (2 cells)"
    sm.setCurrentIndex(grid("x", 2, 0), F.ClearAndSelect)
    assert lab.text() == "0x000804"
    if m._top and m._left:
        sm.setCurrentIndex(grid("corner", 0, 0), F.ClearAndSelect)
        assert lab.text() == ""
    sm.setCurrentIndex(grid("z", 1, 0), F.ClearAndSelect)
    win.statusBar().showMessage("busy")
    app.processEvents()
    assert lab.isVisible() and lab.text() == "0x001002"
    # the hex editor's picked byte, until it closes
    dlg = QtWidgets.QDialog(ws)
    gui_address.show_hex(dlg, [0x1234])
    assert lab.text() == "0x001234"
    dlg.reject()
    assert lab.text() == "0x001002"
    # a new table in the model: nothing selected yet
    ws.model.set_entity(_map(), None)
    assert lab.text() == ""

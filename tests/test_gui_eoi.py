# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The End of injection window (ovtsuite/gui_eoi.py), offscreen on 03G906016LQ:
it opens from the tab, shows EOI over the SOI map's breakpoints, typing an
EOI moves the SOI as one undo step, a duration table shows which cells the
ECU uses, and the window follows an undo made elsewhere."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
VP37 = os.path.join(BINARIES, "EDC15", "038906012A.bin")


@pytest.fixture(scope="module")
def G(tmp_path_factory):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope,
                             str(tmp_path_factory.mktemp("settings")))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
        yield gui


@pytest.fixture
def win(G):
    from ovtsuite.session import TunerSession
    w = G.MainWindow(TunerSession(LQ))
    w.resize(1300, 800)
    w.show()
    QtWidgets.QApplication.processEvents()
    yield w
    w.close_discarding()


def _cell(d, rpm, q):
    ref = d._ref
    for (r, c), cell in d._cells.items():
        if (cell.rpm, cell.q) == (rpm, q):
            return r, c, cell
    raise AssertionError("no cell at %s/%s" % (rpm, q))


def test_opens_and_shows_eoi_over_the_soi_axes(win):
    from ovtsuite import gui_eoi
    ws = win.current
    assert gui_eoi.availability(ws) is None
    d = gui_eoi.open_for(ws)
    assert d is ws.tool("eoi")
    assert (d.grid.rowCount(), d.grid.columnCount()) == (14, 16)
    r, c, cell = _cell(d, 2000.0, 20.0)
    assert float(d.grid.item(r, c).text()) == pytest.approx(5.87, abs=0.01)


def test_typing_an_eoi_moves_soi_in_one_undo_step(win):
    from ovtsuite import gui_eoi
    ws = win.current
    d = gui_eoi.open_for(ws)
    r, c, cell = _cell(d, 2000.0, 20.0)
    soi0 = cell.soi_btdc
    d.grid.item(r, c).setText("4")
    QtWidgets.QApplication.processEvents()
    r, c, cell = _cell(d, 2000.0, 20.0)
    assert cell.eoi == pytest.approx(4.0, abs=0.05)
    assert cell.soi_btdc > soi0
    ws.primary.undo()
    d.refresh_from_session()
    assert _cell(d, 2000.0, 20.0)[2].soi_btdc == pytest.approx(soi0)


def test_duration_table_view_marks_unused_cells(win):
    from ovtsuite import gui_eoi
    d = gui_eoi.open_for(win.current)
    d.view.setCurrentIndex(d.view.findData(6))       # MAP6: never used on LQ
    QtWidgets.QApplication.processEvents()
    used = {d.grid.item(r, c).data(QtCore.Qt.ItemDataRole.UserRole + 1)
            for r in range(d.grid.rowCount()) for c in range(d.grid.columnCount())}
    assert used == {0.0}
    assert not d.target_btn.isEnabled()


def test_not_offered_without_injection_tables(G):
    if not os.path.exists(VP37):
        pytest.skip("reference binary missing")
    from ovtsuite import gui_eoi
    from ovtsuite.session import TunerSession
    w = G.MainWindow(TunerSession(VP37))
    try:
        assert gui_eoi.availability(w.current) is not None
    finally:
        w.close_discarding()

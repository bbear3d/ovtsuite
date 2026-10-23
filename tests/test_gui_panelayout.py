# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""View > Table panels: the 3-D surface and dyno panels around the table."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6 import QtWidgets
except ImportError:                                     # pragma: no cover
    pytest.skip("PySide6 not installed", allow_module_level=True)

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


@pytest.fixture
def ws(monkeypatch):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    w = gui.MainWindow(TunerSession(LQ))
    w.resize(1500, 950)
    w.show()
    QtWidgets.QApplication.processEvents()
    yield w.current
    w.close_discarding()


def _pump():
    for _ in range(4):
        QtWidgets.QApplication.processEvents()


def test_layouts_move_the_panels_and_keep_their_state(ws):
    from ovtsuite import gui_dyno, gui_panelayout as P, gui_surface3d, options
    gui_surface3d.set_shown(ws, True, remember=False, focus=False)
    gui_dyno.set_shown(ws, True, remember=False)
    _pump()
    stacked_h = ws.table.height()
    options.set(P.KEY, "side")
    _pump()
    assert ws.panel_layout == "side"
    assert ws.surface.isVisible() and ws.dyno_panel.isVisible()
    assert abs(ws.surface.y() - ws.dyno_panel.y()) < 5          # one row
    assert ws.table.height() > stacked_h
    options.set(P.KEY, "right")
    _pump()
    assert ws.table.mapTo(ws, ws.table.rect().topLeft()).x() < \
        ws.surface.mapTo(ws, ws.surface.rect().topLeft()).x()
    gui_surface3d.set_shown(ws, False, remember=False)
    gui_dyno.set_shown(ws, False, remember=False)
    _pump()
    assert ws._panel_area.isHidden()                 # the table takes it all
    gui_dyno.set_shown(ws, True, remember=False)
    _pump()
    assert not ws._panel_area.isHidden() and ws.dyno_panel.isVisible()
    options.set(P.KEY, "stack")
    _pump()
    assert ws.panel_layout == "stack" and ws.dyno_panel.isVisible()
    assert not ws.surface.isVisible()                # still hidden, as it was

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Zoom of the map table and the hex editor (ovtsuite.gui_zoom): Ctrl+wheel
and Ctrl+= / - / 0, remembered and shared by every view of the kind."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import branding, gui_zoom  # noqa: E402


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture
def app():
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings(*branding.QSETTINGS).remove("zoom/testview")
    yield a
    QtCore.QSettings(*branding.QSETTINGS).remove("zoom/testview")


def test_zoom_is_shared_and_remembered(app):
    a, b = QtWidgets.QTableView(), QtWidgets.QTableView()
    base = a.font().pointSizeF()
    called = []
    za = gui_zoom.install(a, "testview", after=called.append)
    gui_zoom.install(b, "testview")
    za.zoom_in()
    za.zoom_in()
    f = gui_zoom.scale("testview")
    assert f == pytest.approx(1.21, abs=0.01) and called
    assert a.font().pointSizeF() == pytest.approx(base * f, rel=0.02)
    assert b.font().pointSizeF() == pytest.approx(base * f, rel=0.02)
    c = QtWidgets.QTableView()                       # a view made later
    gui_zoom.install(c, "testview")
    assert c.font().pointSizeF() == pytest.approx(base * f, rel=0.02)
    za.reset()
    assert a.font().pointSizeF() == pytest.approx(base, rel=0.02)
    for _ in range(40):
        za.zoom_out()
    assert gui_zoom.scale("testview") == gui_zoom.MIN

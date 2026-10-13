# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The collapsible side panel (ovtsuite.gui_autohide), as the Project panel
uses it: resize, collapse to a strip, fly out over the window on hover and
go again when the mouse leaves, stay when opened from the menu, expand, and
remember collapsed state and width."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

pytestmark = pytest.mark.slow                           # a GUI test


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def win(app, tmp_path):
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    w = QtWidgets.QMainWindow()
    w.resize(1000, 700)
    w.setCentralWidget(QtWidgets.QTextEdit())
    w.show()
    yield w
    w.close()


def _pump(app, ms):
    t = QtCore.QElapsedTimer()
    t.start()
    while t.elapsed() < ms:
        app.processEvents()


def _dock(win):
    from ovtsuite.gui_autohide import AutoHideDock

    class Dock(AutoHideDock):
        settings_key = "test/dock"
    d = Dock("Project", win)
    body = QtWidgets.QTreeWidget()
    d.set_body(body)
    win.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, d)
    d.show()
    d.restore_state()
    return d, body


def test_collapse_fly_out_on_hover_and_back(app, win):
    from ovtsuite.gui_autohide import STRIP_WIDTH, HOVER_DELAY, LEAVE_DELAY
    d, body = _dock(win)
    _pump(app, 50)
    d.collapse()
    _pump(app, 50)
    assert d.is_collapsed() and d.width() == STRIP_WIDTH and not body.isVisible()
    # resting on the strip flies the panel out, over the window
    QtGui.QCursor.setPos(d.strip.mapToGlobal(QtCore.QPoint(5, 200)))
    QtWidgets.QApplication.sendEvent(d.strip, QtGui.QEnterEvent(
        QtCore.QPointF(5, 200), QtCore.QPointF(5, 200), QtCore.QPointF(5, 200)))
    _pump(app, HOVER_DELAY + 150)
    assert d.is_flown_out() and body.isVisible()
    assert d.width() == STRIP_WIDTH                     # nothing moved
    assert d.flyout.x() == d.strip.mapTo(win, QtCore.QPoint(0, 0)).x() + STRIP_WIDTH
    d.flyout.set_width(420)                             # its edge resizes it
    # the mouse leaves: it goes
    QtGui.QCursor.setPos(win.mapToGlobal(QtCore.QPoint(900, 300)))
    _pump(app, LEAVE_DELAY + 400)
    assert not d.is_flown_out()
    # opened from the menu, it stays until the focus moves away
    d.show_and_focus(body)
    _pump(app, LEAVE_DELAY + 400)
    assert d.is_flown_out()
    d.flyout.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,
                                           QtCore.Qt.Key.Key_Escape,
                                           QtCore.Qt.KeyboardModifier.NoModifier))
    assert not d.is_flown_out()
    # expanding docks it again at the width it was given
    d.expand()
    _pump(app, 50)
    assert not d.is_collapsed() and body.parent() is d and d.width() == 420


def test_collapsed_state_and_width_are_remembered(app, win):
    d, _body = _dock(win)
    win.resizeDocks([d], [380], QtCore.Qt.Orientation.Horizontal)
    _pump(app, 50)
    d.collapse()
    d.deleteLater()
    _pump(app, 50)
    d2, _ = _dock(win)
    assert d2.is_collapsed() and d2.saved_width() == 380
    d2.setFloating(True)                                # floating: expanded
    assert not d2.is_collapsed()

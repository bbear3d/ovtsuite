# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The help window and F1 (ovtsuite.gui_help), driven offscreen."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

pytestmark = pytest.mark.slow          # the GUI


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def H(app, monkeypatch):
    from ovtsuite import gui_help, helpdocs
    # every module's pages, whatever is installed where the tests run
    monkeypatch.setattr(helpdocs, "installed_modules", lambda: list(helpdocs.known_modules()))
    monkeypatch.setattr(gui_help, "_window", None)
    yield gui_help
    if gui_help._window is not None:
        gui_help._window.close()


def test_opens_a_page_and_navigates(H):
    w = H.show_help(None, "flashing")
    assert w.current == "flashing"
    assert "ECU read / write" in w.view.toPlainText()
    w._link(QtCore.QUrl("cables.md"))
    assert w.current == "cables"
    w.go_back()
    assert w.current == "flashing"
    w.go_forward()
    assert w.current == "cables"


def test_contents_and_search(H):
    w = H.show_help(None)
    assert w.current == "index"
    tops = [w.tree.topLevelItem(i).text(0) for i in range(w.tree.topLevelItemCount())]
    assert tops[-1] == "Modules"
    w.search.setText("version history")
    assert w.side.currentWidget() is w.hits and w.hits.count() >= 1
    w._open_first_hit()
    assert "version" in w.view.toPlainText().lower()


def test_absent_module_page(H, monkeypatch):
    from ovtsuite import helpdocs
    monkeypatch.setattr(helpdocs, "installed_modules", lambda: [])
    H._window = None
    w = H.show_help(None, "modules")
    w._link(QtCore.QUrl("modules/zf8hp/index.md"))
    assert "not installed" in w.view.toPlainText()


def test_f1_opens_the_page_for_the_window_in_front(H, app, monkeypatch):
    class FlashDialog(QtWidgets.QDialog):           # named like the real one
        pass
    d = FlashDialog()
    monkeypatch.setattr(QtWidgets.QApplication, "activeModalWidget", staticmethod(lambda: d))
    H.install_f1(app)
    ev = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F1,
                         QtCore.Qt.KeyboardModifier.NoModifier)
    assert H._filter.eventFilter(d, ev) is True
    # (a modal dialog's own help window: the shared one would be blocked)
    own = d._ovt_help_window
    assert own.parentWidget() is d and own.current == "flashing"
    other = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_F2,
                            QtCore.Qt.KeyboardModifier.NoModifier)
    assert H._filter.eventFilter(d, other) is False
    d.close()


def test_standalone_tools_get_help_and_about(app):
    from ovtsuite.gui_about import add_tool_buttons
    d = QtWidgets.QDialog()
    lay = QtWidgets.QVBoxLayout(d)
    lay.addWidget(QtWidgets.QLabel("tool"))
    h, a = add_tool_buttons(d, "sniffer")
    assert h.text() == "Help" and a.text().startswith("About")
    row = d._tool_button_row
    assert row.indexOf(h) < row.indexOf(a)
    d.close()


def test_help_from_a_modal_dialog_is_not_blocked_by_it(H, app):
    """The standalone flasher and logger run as a modal dialog, and Qt blocks
    every window outside a modal dialog: help opened from one came up behind
    it and could not be used. Help asked for while a dialog is modal is a
    window of that dialog's own, which it does not block."""
    dlg = QtWidgets.QDialog()
    dlg.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
    dlg.show()
    app.processEvents()
    try:
        assert QtWidgets.QApplication.activeModalWidget() is dlg
        w = H.show_help(dlg, "flashing")
        assert w.parentWidget() is dlg and w.isWindow() and w.isVisible()
        assert w.current == "flashing"
        assert H.show_help(dlg, "logging") is w          # the same one again
    finally:
        dlg.close()
        dlg.deleteLater()
        app.processEvents()
    shared = H.show_help(None, "flashing")
    assert shared.parentWidget() is None
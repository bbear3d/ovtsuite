# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Ctrl+C copies selected text in a read-only view although Edit > Copy
cells owns the shortcut for the window (gui_textcopy)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

from ovtsuite import gui_textcopy  # noqa: E402

CTRL = QtCore.Qt.KeyboardModifier.ControlModifier


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app):
    gui_textcopy.install(app)
    w = QtWidgets.QMainWindow()
    w.hits = []
    act = QtGui.QAction("Copy cells", w)
    act.setShortcut("Ctrl+C")
    act.triggered.connect(lambda: w.hits.append(1))
    w.addAction(act)
    holder = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(holder)
    w.setCentralWidget(holder)
    w.lay = lay
    yield w
    w.close()


def _show(w):
    w.show()
    w.activateWindow()
    QtTest.QTest.qWaitForWindowActive(w)


def _copy(w, widget):
    QtWidgets.QApplication.clipboard().setText("sentinel")
    widget.setFocus()
    QtTest.QTest.keyClick(widget, QtCore.Qt.Key.Key_C, CTRL)
    return QtWidgets.QApplication.clipboard().text()


def test_text_browser_selection_is_copied(window):
    tb = QtWidgets.QTextBrowser()
    tb.setPlainText("hello world")
    window.lay.addWidget(tb)
    _show(window)
    c = tb.textCursor()
    c.select(c.SelectionType.WordUnderCursor)
    tb.setTextCursor(c)
    assert _copy(window, tb) == "hello"
    assert window.hits == []


def test_nothing_selected_leaves_the_key_to_the_window(window):
    tb = QtWidgets.QTextBrowser()
    tb.setPlainText("hello world")
    window.lay.addWidget(tb)
    _show(window)
    assert _copy(window, tb) == "sentinel"
    assert window.hits == [1]


def test_selectable_label_selection_is_copied(window):
    lab = QtWidgets.QLabel("some selectable words")
    lab.setTextInteractionFlags(
        QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard)
    window.lay.addWidget(lab)
    _show(window)
    lab.setSelection(5, 10)
    assert _copy(window, lab) == "selectable"
    assert window.hits == []


def test_other_widgets_still_get_the_window_shortcut(window):
    tbl = QtWidgets.QTableWidget(2, 2)
    window.lay.addWidget(tbl)
    _show(window)
    _copy(window, tbl)
    assert window.hits == [1]


def test_install_is_idempotent(app):
    gui_textcopy.install(app)
    assert gui_textcopy.install(app) is False


def test_ctrl_c_in_the_ai_assistants_conversation_in_the_real_window(
        app, tmp_path, monkeypatch, filtered=True):
    """The reported bug, end to end: with a file open (that enables Edit >
    Copy cells, which has Ctrl+C), select text in the assistant's chat and
    press Ctrl+C."""
    from PySide6 import QtCore as C
    C.QSettings.setPath(C.QSettings.IniFormat, C.QSettings.UserScope, str(tmp_path))
    C.QSettings.setDefaultFormat(C.QSettings.IniFormat)
    monkeypatch.setenv("OVT_NO_INFO_PROMPT", "1")
    from ovtsuite import gui
    from ovtsuite import gui_files
    if filtered:
        gui_textcopy.install(app)
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    # (a binary nothing recognises opens with a note and the hex editor; the
    # test only needs a file open)
    monkeypatch.setattr(gui.Workspace, "_explain_limited_support",
                        lambda self, loaded: None)
    path = tmp_path / "some.bin"
    path.write_bytes(bytes(range(256)) * 4)
    w = gui.MainWindow()
    try:
        w.show()
        w.activateWindow()
        QtTest.QTest.qWaitForWindowActive(w)
        assert gui_files.import_files(w, [str(path)], target="new")
        copy_cells = next(a for a in w.findChildren(QtGui.QAction)
                          if a.text().replace("&", "") == "Copy cells")
        assert copy_cells.isEnabled(), "a file is open: the window owns Ctrl+C"
        dock = next((d for d in w.findChildren(QtWidgets.QDockWidget)
                     if type(d).__name__ == "AssistantDock"), None)
        assert dock is not None
        dock.expand()
        dock.view.setPlainText("The map is the driver's wish torque.")
        c = dock.view.textCursor()
        c.setPosition(4)
        c.setPosition(7, c.MoveMode.KeepAnchor)
        dock.view.setTextCursor(c)
        w.activateWindow()                      # (a dialog may have had it)
        QtTest.QTest.qWait(50)
        dock.view.setFocus()
        assert QtWidgets.QApplication.activeWindow() is w
        assert _copy(w, dock.view) == "map"
    finally:
        w.close_discarding()

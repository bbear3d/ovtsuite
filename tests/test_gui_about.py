# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Help > About (ovtsuite.gui_about), driven offscreen."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
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


def test_about_dialog_shows_versions_and_credits(app):
    from ovtsuite import branding, credits, gui_about
    d = gui_about.AboutDialog(None)
    assert branding.__version__ in d.info_label.text()
    assert d.tabs.count() == 2 and d.tabs.currentIndex() == gui_about.ABOUT
    # every credit is in the tree, under its group
    shown = []
    for i in range(d.tree.topLevelItemCount()):
        top = d.tree.topLevelItem(i)
        shown += [top.child(j).text(0) for j in range(top.childCount())]
    for c in credits.CREDITS:
        assert c.name in shown
    # picking one shows its licence text
    top = d.tree.topLevelItem(0)
    d.tree.setCurrentItem(top.child(0))
    assert "Greg Menounos" in d.detail.text()
    assert "Permission is hereby granted" in d.text.toPlainText()
    d.close()


def test_about_opens_on_credits_and_copies_versions(app):
    from ovtsuite import gui_about
    d = gui_about.AboutDialog(None, gui_about.CREDITS)
    assert d.tabs.currentIndex() == gui_about.CREDITS
    d._copy_info()
    assert gui_about.runtime_info().splitlines()[0] in \
        QtWidgets.QApplication.clipboard().text()
    d.close()


def test_main_window_has_a_help_menu(app, tmp_path):
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    w = gui.MainWindow()
    helpm = [m for m in w.menuBar().findChildren(QtWidgets.QMenu)
             if m.title().replace("&", "") == "Help"]
    assert helpm, "no Help menu"
    texts = [a.text() for a in helpm[0].actions()]
    assert any(t.startswith("About ") and "Qt" not in t for t in texts)
    assert "About Qt" in " ".join(texts)
    w.close()


@pytest.mark.parametrize("shape", ["buttonbox", "close", "nothing"])
def test_standalone_tools_get_an_about_button(app, shape):
    """Wherever a tool keeps its closing buttons, About joins them."""
    from ovtsuite.gui_about import add_about_button
    d = QtWidgets.QDialog()
    lay = QtWidgets.QVBoxLayout(d)
    lay.addWidget(QtWidgets.QLabel("tool"))
    if shape == "buttonbox":
        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        lay.addWidget(box)
    elif shape == "close":
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QPushButton("Go"))
        close = QtWidgets.QPushButton("Close")
        row.addWidget(close)
        lay.addLayout(row)
    btn = add_about_button(d, "flasher")
    assert btn.text().startswith("About")
    if shape == "buttonbox":
        assert btn in box.buttons()
    elif shape == "close":
        assert row.indexOf(btn) == row.indexOf(close) - 1      # just before Close
    else:
        assert btn.parentWidget() is d
    d.close()


def test_tool_about_names_the_tool(app):
    from ovtsuite import branding, gui_about
    d = gui_about.AboutDialog(None, gui_about.ABOUT, "sniffer")
    assert d.windowTitle() == "About %s" % branding.TOOL_NAMES["sniffer"]
    d.close()

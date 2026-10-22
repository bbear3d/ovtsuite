# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tools menu shows only what the active tab can use (ovtsuite.gui_toolsmenu)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES, ZF6HP_REFERENCE  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
ZF = os.path.join(ZF6HP_REFERENCE, "5.0TFSI-Stock.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


@pytest.fixture
def win(monkeypatch):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    w = gui.MainWindow()
    w.show()
    yield w
    w.close_discarding()


def _shown(win):
    """The Tools menu as it opens: {text: visible} (sub-menus by title)."""
    m = win.tools_menu
    m.aboutToShow.emit()
    out = {}
    for a in m.actions():
        if not a.isSeparator():
            out[a.text()] = a.isVisible()
    return out


def test_an_empty_tab_shows_only_the_window_wide_tools(win):
    shown = _shown(win)
    assert shown["ECU read / write..."] and shown["Data logging..."]
    assert shown["Modules..."]
    assert not shown["Hex editor..."] and not shown["DTC / Fault-Path editor..."]
    win.tools_menu.aboutToHide.emit()
    assert win.tools_menu.actions()[0].isVisible()     # back as it closes


def test_another_platforms_tools_are_hidden(win):
    if not os.path.exists(LQ) or not os.path.exists(ZF):
        pytest.skip("reference binaries missing")
    assert win.open_path(LQ)
    shown = _shown(win)
    assert shown["DTC / Fault-Path editor..."] and shown["Hex editor..."]
    zf6 = win.zf6hp_menu.menuAction()
    if zf6.isEnabled():                                # the module is installed
        assert not shown[zf6.text()]
    win.tools_menu.aboutToHide.emit()
    assert win.open_path(ZF, new_tab=True)
    shown = _shown(win)
    assert shown["Hex editor..."]
    if zf6.isEnabled():
        assert shown[zf6.text()]
    win.tools_menu.aboutToHide.emit()

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Windows' "Always show scrollbars" setting: Qt's windows11 style hides
scroll bars until hovered whatever the setting, so the app draws them as
hovered when it is on. Windows 11 is not available to the test, so the
mechanism is checked on Fusion, with the override standing in for the
setting. Offscreen, fast."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import gui_style  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class Recorder(QtWidgets.QProxyStyle):
    def __init__(self):
        super().__init__(QtWidgets.QStyleFactory.create("Fusion"))
        self.states = []

    def drawComplexControl(self, control, option, painter, widget=None):
        if control == QtWidgets.QStyle.ComplexControl.CC_ScrollBar:
            self.states.append(option.state)
        return super().drawComplexControl(control, option, painter, widget)


def test_scroll_bars_are_drawn_as_hovered(app):
    rec = Recorder()
    style = gui_style.VisibleScrollBarStyle(rec)
    bar = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Vertical)
    bar.setStyle(style)
    bar.setRange(0, 100)
    bar.resize(16, 200)
    img = bar.grab()
    assert not img.isNull() and rec.states
    assert all(s & QtWidgets.QStyle.StateFlag.State_MouseOver for s in rec.states)


def test_only_where_the_setting_and_the_style_ask_for_it(app, monkeypatch):
    monkeypatch.delenv(gui_style.ENV, raising=False)
    if sys.platform != "win32":
        assert gui_style.windows_always_shows_scrollbars() is False
    monkeypatch.setenv(gui_style.ENV, "auto")
    assert gui_style.wants_visible_scrollbars() is False
    monkeypatch.setenv(gui_style.ENV, "always")
    assert gui_style.wants_visible_scrollbars() is True

    before = app.style()
    if (before.name() or "").lower() not in gui_style._OVERLAY_STYLES:
        monkeypatch.setenv(gui_style.ENV, "auto")
        assert gui_style.install(app) is False          # Fusion: nothing to fix
        assert app.style() is before

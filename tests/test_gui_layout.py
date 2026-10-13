# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The window must never be wider than its contents allow.

Bear saw the tab bar's + button and the right of the editor missing until the
window was resized: with a ZF shift map on screen the edit bar alone needed
1,209 px (a QHBoxLayout of buttons cannot shrink) and the window's minimum
width reached 1,595 px, so on a smaller screen the right of every tab was
laid out past the window's edge. The edit bar now wraps, the title wraps and
the status line clips.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import ZF6HP_REFERENCE  # noqa: E402

pytestmark = pytest.mark.slow

IMAGE = os.path.join(ZF6HP_REFERENCE, "3.0TFSI-Stock.bin")
#: A window this narrow must still hold everything (a small laptop screen).
NARROW = 1000


@pytest.fixture(scope="module")
def win(tmp_path_factory, isolated_qsettings_module):
    if not os.path.exists(IMAGE):
        pytest.skip("reference binary missing")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope,
                             str(tmp_path_factory.mktemp("settings")))
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    w = gui.MainWindow(TunerSession(IMAGE))
    w.resize(NARROW, 700)
    w.show()
    ws = w.current
    e = next(e for e in ws.primary.entities if gui._view6.z_modes(e))
    ws.show_entity(e)
    ws._zmode_pref = "engine"                   # the widest selector text
    ws._sync_zmode()
    app.processEvents()
    yield w
    w.close_discarding()


def test_the_window_can_be_as_narrow_as_a_small_screen(win):
    assert win.minimumSizeHint().width() <= NARROW, \
        "the contents need %dpx" % win.minimumSizeHint().width()


def test_the_edit_bar_wraps_instead_of_overflowing(win):
    ws = win.current
    right = ws.surf_btn.mapTo(win, QtCore.QPoint(ws.surf_btn.width(), 0)).x()
    assert right <= win.width(), "3D surface drawn past the window's edge"
    assert ws.editbar.height() > ws.surf_btn.height() * 1.5, "not wrapped"


def test_the_new_tab_button_is_inside_the_window(win):
    plus = win.tabs.cornerWidget()
    assert plus.isVisible()
    assert plus.mapTo(win, QtCore.QPoint(plus.width(), 0)).x() <= win.width()


def test_a_long_title_wraps(win):
    ws = win.current
    ws.title.setText("x " * 400)
    QtWidgets.QApplication.processEvents()
    assert win.minimumSizeHint().width() <= NARROW


def _under(w):
    """The widget the user's click on ``w`` actually reaches."""
    win = w.window()
    hit = win.childAt(w.mapTo(win, w.rect().center()))
    while hit is not None and hit is not w:
        hit = hit.parentWidget()
    return hit


def test_every_edit_bar_control_takes_its_click(win):
    """Nothing lies over the edit bar's controls -- on a ZF map, and on an
    EDC16 map where the ZF-only "Show Z as" group is empty. That empty group's
    container used to keep Qt's default 640x480 at the bar's top-left, on top
    of "Modify selection:", and the operation dropdown took no clicks."""
    from conftest import BINARIES
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    lq = os.path.join(BINARIES, "03G906016LQ.bin")
    sessions = [win]
    if os.path.exists(lq):
        w2 = gui.MainWindow(TunerSession(lq))
        w2.resize(NARROW, 700)
        w2.show()
        ws2 = w2.current
        ws2.show_entity(next(e for e in ws2.primary.entities
                             if e.kind == "map" and e.rows > 2))
        QtWidgets.QApplication.processEvents()
        sessions.append(w2)
    try:
        for w in sessions:
            ws = w.current
            bar = ws.editbar
            ctrls = [c for c in bar.findChildren(QtWidgets.QWidget)
                     if isinstance(c, (QtWidgets.QComboBox,
                                       QtWidgets.QAbstractButton,
                                       QtWidgets.QLineEdit))
                     and c.isVisible()]
            assert ws.op_box in ctrls
            blocked = [c for c in ctrls if _under(c) is not c]
            assert not blocked, [type(c).__name__ for c in blocked]
    finally:
        for w in sessions[1:]:
            w.close_discarding()

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Undo / Redo history drop-downs on the toolbar (ovtsuite.gui_undomenu)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import options as opt  # noqa: E402
from ovtsuite.session import CommandStack, Command  # noqa: E402

pytestmark = pytest.mark.slow


def test_the_stack_lists_its_labels_next_first():
    st = CommandStack()
    st.push(Command("a", [])), st.push(Command("b", []))
    assert st.undo_labels() == ["b", "a"] and st.redo_labels() == []


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


@pytest.fixture
def win(tmp_path, monkeypatch):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    path = str(tmp_path / "options.ini")
    monkeypatch.setattr(opt, "_store", lambda: QtCore.QSettings(
        path, QtCore.QSettings.Format.IniFormat))
    for name in ("information", "warning", "question", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: 0))
    # an unknown binary explains itself in a dialog: no modal loops here
    monkeypatch.setattr(QtWidgets.QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda self, *a: 0)
    from ovtsuite import gui
    w = gui.MainWindow()
    w.show()
    f = tmp_path / "x.bin"
    f.write_bytes(bytes(4096))
    assert w.open_path(str(f))
    app.processEvents()
    yield w
    w.close_discarding()


def _button(w, kind):
    from ovtsuite import gui_toolbar
    tb = gui_toolbar.toolbar(w)
    a = w.__dict__["_ovt_%s_action" % kind]
    return a, tb.widgetForAction(a)


def test_buttons_drop_down_the_history_and_undo_to_a_step(win):
    from ovtsuite import gui_undomenu as G
    s = win.current.primary
    a, btn = _button(win, "undo")
    assert btn.popupMode() == QtWidgets.QToolButton.MenuButtonPopup
    win._status()
    assert not a.isEnabled()                    # nothing to undo yet
    for i in range(3):
        s.edit_bytes(0x10 * (i + 1), bytes([i + 1]))
    win._workspace_changed(win.current)
    assert a.isEnabled()
    menu = btn.menu()
    G.fill(win, G.UNDO, menu)
    items = [x.text() for x in menu.actions()]
    assert items[0] == "Edit bytes at 0x30"
    assert items[2].startswith("Edit bytes at 0x10") and "3 steps" in items[2]
    menu.actions()[1].trigger()                 # undo two steps at once
    assert s.undo_stack.undo_labels() == ["edit bytes @0x10"]
    assert s.binfile.data[0x20] == 0 and s.binfile.data[0x10] == 1
    r, rbtn = _button(win, "redo")
    assert r.isEnabled()
    G.fill(win, G.REDO, rbtn.menu())
    assert len(rbtn.menu().actions()) == 2
    assert "Redo: Edit bytes at 0x20" in G.tooltip(win, G.REDO)


def test_describe_names_the_parameter():
    from types import SimpleNamespace
    from ovtsuite import gui_undomenu as G
    from ovtsuite.model import Axis, Entity
    e = Entity("Sym_MAP", "map", 1, [], "", "", 0x100, 2, 2, 16, False, "X",
               "kb", x=Axis(0xF0, 2, 16), label="Boost limit")
    s = SimpleNamespace(entities=[e], platform="x")
    from ovtsuite.gui import _english
    t = _english(e)                             # the tree's own name
    assert G.describe(s, "edit Sym_MAP") == "Edit %s" % t
    assert G.describe(s, "edit bytes @0x102") == "Edit bytes of %s (0x102)" % t
    assert G.describe(s, "edit bytes @0xF2") == "Edit X axis of %s" % t
    assert G.describe(s, "copy x from y") == "Copy x from y"

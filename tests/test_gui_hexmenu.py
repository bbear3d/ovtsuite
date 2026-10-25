# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The hex editor's right-click menu (ovtsuite/gui_hexmenu.py): what it
offers on the primary and comparison views, and that copy from / to the
comparison, paste and revert write exactly the selected bytes as one undo
step."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions + the GUI

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
KN = os.path.join(BINARIES, "03G906016KN.bin")
SEL = QtCore.QItemSelectionModel.SelectionFlag


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _session(path):
    if not os.path.exists(path):
        pytest.skip("reference binary missing: %s" % path)
    from ovtsuite.session import TunerSession
    return TunerSession(path)


@pytest.fixture(scope="module")
def pair(app):
    return _session(LQ), _session(KN)


class Tab(QtWidgets.QWidget):
    """Stands in for the workspace tab that owns the hex editor."""

    def __init__(self, s):
        super().__init__()
        self.primary = s
        self.shown = []

    def show_entity(self, e):
        self.shown.append(e)


@pytest.fixture
def dlg(pair, app):
    a, b = pair
    while a.undo() is not None:          # each test starts from the file
        pass
    tab = Tab(a)
    from ovtsuite import gui
    d = gui.HexDialog(a, b, parent=tab)
    yield d
    d.close()
    while a.undo() is not None:
        pass


@pytest.fixture
def menus(monkeypatch):
    got = []

    class Menu(QtWidgets.QMenu):          # records what it holds, never opens
        def exec(self, *_a):
            got.append({a.text(): a for a in self.actions() if a.text()})
    monkeypatch.setattr(QtWidgets, "QMenu", Menu)
    return got


def _mixed_row(a, b, start=0x1C0000):
    """A 16-byte row with some bytes that differ and some that match."""
    da, db = a.binfile.data, b.binfile.data
    for r in range(start // 16, len(da) // 16):
        diff = [i for i in range(16) if da[r * 16 + i] != db[r * 16 + i]]
        if 2 <= len(diff) <= 12:
            return r, diff
    pytest.skip("no mixed row")


def _same_row(a, b, start=0x1C0000):
    da, db = a.binfile.data, b.binfile.data
    for r in range(start // 16, len(da) // 16):
        if da[r * 16:r * 16 + 16] == db[r * 16:r * 16 + 16]:
            return r
    pytest.skip("no matching row")


def _select(d, r, c0, c1, flag=SEL.ClearAndSelect):
    sel = QtCore.QItemSelection(d.model.index(r, c0), d.model.index(r, c1))
    d.view.selectionModel().setCurrentIndex(d.model.index(r, c0), SEL.NoUpdate)
    d.view.selectionModel().select(sel, flag)


def _menu(d, menus):
    d_rect = d.view.visualRect(d.view.currentIndex())
    from ovtsuite import gui_hexmenu
    gui_hexmenu.show_menu(d, d_rect.center())
    return menus[-1]


def test_menu_on_the_primary_view(dlg, menus, pair):
    a, b = pair
    r, _diff = _mixed_row(a, b)
    _select(dlg, r, 0, 15)
    QtWidgets.QApplication.clipboard().setText("00")
    acts = _menu(dlg, menus)
    for t in ("Copy", "Copy as text", "Paste",
              "Copy selected bytes from comparison",
              "Revert to as-read values", "Revert to last saved values",
              "Select all"):
        assert t in acts, t
    assert "Copy bytes to primary" not in acts
    assert acts["Copy selected bytes from comparison"].isEnabled()
    # nothing edited yet: the reverts have nothing to do
    assert not acts["Revert to as-read values"].isEnabled()


def test_menu_on_the_comparison_view(dlg, menus, pair):
    a, b = pair
    dlg.show_box.setCurrentIndex(1)
    r, _diff = _mixed_row(a, b)
    _select(dlg, r, 0, 15)
    acts = _menu(dlg, menus)
    assert "Copy bytes to primary" in acts and acts["Copy bytes to primary"].isEnabled()
    for t in ("Paste", "Copy selected bytes from comparison",
              "Revert to as-read values"):
        assert t not in acts, t
    assert "Copy" in acts


def test_matching_selection_disables_the_comparison_actions(dlg, menus, pair):
    a, b = pair
    r = _same_row(a, b)
    _select(dlg, r, 0, 15)
    acts = _menu(dlg, menus)
    act = acts["Copy selected bytes from comparison"]
    assert not act.isEnabled() and "already match" in act.toolTip()
    dlg.show_box.setCurrentIndex(1)
    _select(dlg, r, 0, 15)
    acts = _menu(dlg, menus)
    assert not acts["Copy bytes to primary"].isEnabled()


def test_copy_from_comparison_writes_the_selection_in_one_step(dlg, menus, pair):
    a, b = pair
    r, diff = _mixed_row(a, b)
    before = bytes(a.binfile.data)
    c0, c1 = diff[0], diff[-1]
    _select(dlg, r, c0, c1)
    acts = _menu(dlg, menus)
    acts["Copy selected bytes from comparison"].trigger()
    now = a.binfile.data
    for i in range(len(before)):
        if r * 16 + c0 <= i <= r * 16 + c1:
            assert now[i] == b.binfile.data[i]
        elif now[i] != before[i]:
            pytest.fail("byte 0x%X outside the selection changed" % i)
    assert a.undo() is not None
    assert bytes(a.binfile.data) == before, "one undo puts it all back"


def test_copy_to_primary_from_the_ascii_column(dlg, menus, pair):
    a, b = pair
    r, diff = _mixed_row(a, b)
    before = bytes(a.binfile.data)
    dlg.show_box.setCurrentIndex(1)
    _select(dlg, r, 16, 16)                     # the row's text = its 16 bytes
    acts = _menu(dlg, menus)
    acts["Copy bytes to primary"].trigger()
    assert bytes(a.binfile.data[r * 16:r * 16 + 16]) == \
        bytes(b.binfile.data[r * 16:r * 16 + 16])
    changed = [i for i in range(len(before)) if a.binfile.data[i] != before[i]]
    assert changed == [r * 16 + i for i in diff]
    a.undo()
    assert bytes(a.binfile.data) == before


def test_paste_and_revert_to_as_read(dlg, menus, pair):
    a, b = pair
    r = _same_row(a, b)
    at = r * 16 + 2
    old = bytes(a.binfile.data[at:at + 2])
    new = bytes([old[0] ^ 0xFF, old[1] ^ 0xFF])
    QtWidgets.QApplication.clipboard().setText(new.hex(" "))
    _select(dlg, r, 2, 2)
    _menu(dlg, menus)["Paste"].trigger()
    assert bytes(a.binfile.data[at:at + 2]) == new
    _select(dlg, r, 0, 15)
    acts = _menu(dlg, menus)
    assert acts["Revert to as-read values"].isEnabled()
    acts["Revert to as-read values"].trigger()
    assert bytes(a.binfile.data[at:at + 2]) == old


def test_copy_as_text(dlg, menus, pair):
    a, _b = pair
    _select(dlg, 0, 0, 3)
    _menu(dlg, menus)["Copy as text"].trigger()
    want = "".join(chr(c) if 32 <= c < 127 else "." for c in a.binfile.data[:4])
    assert QtWidgets.QApplication.clipboard().text() == want


def test_go_to_table(dlg, menus, pair):
    a, _b = pair
    e = next(e for e in a.entities if e.n_cells * e.step > 0)
    dlg.select_range(e.addr, e.addr + 1)
    acts = _menu(dlg, menus)
    go = [t for t in acts if t.startswith("Go to table:")]
    assert go, acts.keys()
    acts[go[0]].trigger()
    tab = dlg.parent()
    assert tab.shown and tab.shown[-1].addr <= e.addr

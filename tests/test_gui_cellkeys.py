# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Keyboard editing in the map table (gui_cellkeys).

Edit > Cells -- step up/down by the map's own step (Alt+Up / Alt+Down, x10
with Shift), +/-1 % (Alt+PgUp / PgDn), interpolate, smooth, copy from the
comparison (Ctrl+R), back to the as-read values (Ctrl+Shift+R) -- plus the
spreadsheet keys in the table: Ctrl(+Shift)+arrow to the edge of the data,
``=`` for a sum over the selection, and in a cell editor Ctrl+Enter to fill
the selection and Left/Right at the ends of the text to move on.

Offscreen, on the LQ with the KN as its comparison.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtGui, QtTest, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
KN = os.path.join(BINARIES, "03G906016KN.bin")


# -- the sum box's language (no Qt needed) ------------------------------------

@pytest.mark.parametrize("text,x,want", [
    ("+5", 10, 15), ("-5", 10, 5), ("*1.5", 10, 15), ("/4", 10, 2.5),
    ("=120", 10, 120), ("+10%", 10, 11), ("-10%", 10, 9),
    ("x*1.1+2", 10, 13), ("=x/2", 10, 5), ("2*x", 3, 6), ("+2,5", 1, 3.5),
])
def test_sums(text, x, want):
    from ovtsuite.gui_cellkeys import parse_expression
    assert parse_expression(text)(x) == pytest.approx(want)


@pytest.mark.parametrize("text", ["", "abc", "__import__('os')", "x+", "y*2"])
def test_not_sums(text):
    from ovtsuite.gui_cellkeys import parse_expression
    with pytest.raises((ValueError, SyntaxError)):
        parse_expression(text)(1.0)


# -- in the window ------------------------------------------------------------

@pytest.fixture(scope="module")
def G(tmp_path_factory, isolated_qsettings_module):
    for p in (LQ, KN):
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gui.Workspace, "show_file_info",
                   lambda self, *a, **k: None)
        yield gui


@pytest.fixture(scope="module")
def _win(G):
    from ovtsuite.session import TunerSession
    w = G.MainWindow(TunerSession(LQ))
    w.resize(1200, 760)
    w.show()
    QtWidgets.QApplication.processEvents()
    w.current.set_comparison(TunerSession(KN))
    QtWidgets.QApplication.processEvents()
    yield w
    w.close_discarding()


MAP = "AccPed_trqEngLim_MAP"        # 7x6 on both; KN's values differ


@pytest.fixture
def win(_win):
    ws = _win.current
    while ws.primary.modified:
        ws.primary.undo()
    e = next(x for x in ws.primary.entities if x.name == MAP)
    ws.open_table(e)
    QtWidgets.QApplication.processEvents()
    yield _win
    while ws.primary.modified:
        ws.primary.undo()


def _open(win, name):
    ws = win.current
    ws.open_table(next(x for x in ws.primary.entities if x.name == name))
    QtWidgets.QApplication.processEvents()
    return ws


#: mid-range torque values: room to step either way
PLAIN = "AccPed_trqEng0_MAP"


def _act(win, text):
    return next(a for a in win._cell_actions if a.text() == text)


def _select(ws, cells):
    """Select Z cells [(r, c)] and make the first current."""
    m, t = ws.model, ws.table
    sm = t.selectionModel()
    sm.clearSelection()
    for i, (r, c) in enumerate(cells):
        ix = m.index_of("z", r, c)
        sm.select(ix, QtCore.QItemSelectionModel.Select)
        if i == 0:
            sm.setCurrentIndex(ix, QtCore.QItemSelectionModel.NoUpdate)
    t.setFocus()


def test_the_menu_holds_them_with_their_keys(win):
    keys = {a.text(): a.shortcut().toString() for a in win._cell_actions}
    assert keys["Step up"] == "Alt+Up" and keys["Step down x10"] == "Alt+Shift+Down"
    assert keys["Copy from comparison"] == "Ctrl+R"
    assert keys["Revert to as-read values"] == "Ctrl+Shift+R"
    assert keys["Increase 1%"] == "Alt+PgUp"
    for a in win._cell_actions:      # bound to the tables, not the window
        assert a.shortcutContext() == QtCore.Qt.WidgetWithChildrenShortcut
        assert a in win.current.table.actions()
    from ovtsuite import gui_actions
    assert any(k.startswith("edit/cells/") for k in gui_actions.register(win))


def test_step_uses_the_maps_own_step(win):
    from ovtsuite import gui_cellkeys as K
    ws = _open(win, PLAIN)
    e = ws.model.entity
    _select(ws, [(1, 1), (1, 2)])
    before = [e.cell(1, 1), e.cell(1, 2)]
    raw = K.default_step(e)
    _act(win, "Step up").trigger()
    assert [e.cell(1, 1), e.cell(1, 2)] == pytest.approx([v + raw for v in before])
    ws.primary.undo()                                   # one undo step
    assert [e.cell(1, 1), e.cell(1, 2)] == pytest.approx(before)
    K.set_map_step(ws, e, 10 * raw)
    try:
        _act(win, "Step down x10").trigger()
        assert e.cell(1, 1) == pytest.approx(before[0] - 100 * raw, abs=1.5 * raw)
    finally:
        K.set_map_step(ws, e, None)
    assert K.map_step(ws, e) == (raw, False)


def test_percent(win):
    ws = _open(win, PLAIN)
    e = ws.model.entity
    _select(ws, [(2, 2)])
    v = e.cell(2, 2)
    _act(win, "Increase 1%").trigger()
    assert e.cell(2, 2) == pytest.approx(v * 1.01, abs=2 * e.eq.factor + 1e-9)


def test_copy_from_comparison_and_back_to_as_read(win):
    ws = win.current
    m, e = ws.model, ws.model.entity
    cells = [(r, c) for r in range(e.rows) for c in range(e.cols)
             if m._cmp[r][c] is not None and m._cmp[r][c] != e.cell(r, c)]
    assert cells, "LQ and KN should differ in %s" % MAP
    _select(ws, cells[:3])
    stock = [e.cell(r, c) for r, c in cells[:3]]
    _act(win, "Copy from comparison").trigger()                      # Ctrl+R
    assert [e.cell(r, c) for r, c in cells[:3]] == \
        pytest.approx([m._cmp[r][c] for r, c in cells[:3]])
    _act(win, "Revert to as-read values").trigger()                  # Ctrl+Shift+R
    assert [e.cell(r, c) for r, c in cells[:3]] == pytest.approx(stock)


def test_revert_to_last_saved_goes_back_to_the_save_not_the_read(win):
    from ovtsuite import gui_cellkeys as K
    ws = _open(win, PLAIN)
    p, e = ws.primary, ws.model.entity
    assert _act(win, "Revert to last saved values").shortcut().toString() == "F11"
    cells = [(1, 1), (1, 2)]
    _select(ws, cells)
    read = [e.cell(r, c) for r, c in cells]
    kept = p._last_saved_snapshot
    try:
        _act(win, "Step up x10").trigger()
        saved = [e.cell(r, c) for r, c in cells]
        assert saved != read
        p._last_saved_snapshot = bytes(p.binfile.data)   # as a save leaves it
        _act(win, "Step up x10").trigger()
        assert [e.cell(r, c) for r, c in cells] != saved
        _act(win, "Revert to last saved values").trigger()          # F11
        assert [e.cell(r, c) for r, c in cells] == pytest.approx(saved)
        _act(win, "Revert to as-read values").trigger()             # Ctrl+Shift+R
        assert [e.cell(r, c) for r, c in cells] == pytest.approx(read)
    finally:
        p._last_saved_snapshot = kept
    menu = QtWidgets.QMenu()
    K.add_to_menu(win, menu)
    assert [a.text() for a in menu.actions() if a.text()] == list(K.MENU_ACTIONS)


def test_ctrl_arrow_goes_to_the_edge_of_the_map(win):
    ws = win.current
    m, t, e = ws.model, ws.table, ws.model.entity
    _select(ws, [(2, 2)])
    QtTest.QTest.keyClick(t, QtCore.Qt.Key_Right, QtCore.Qt.ControlModifier)
    assert m.z_at(t.currentIndex()) == (2, e.cols - 1) or \
        m.z_at(t.currentIndex()) == (e.cols - 1, 2)       # if shown turned
    QtTest.QTest.keyClick(t, QtCore.Qt.Key_Up,
                          QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier)
    sel = {m.z_at(ix) for ix in t.selectionModel().selectedIndexes()}
    assert len(sel) > 1, "Ctrl+Shift+Up selects up to the edge"
    # never into the X band above the map
    assert all(z is not None for z in sel)


def test_ctrl_arrow_leaves_only_the_cell_it_lands_on_selected(win, monkeypatch):
    """With Ctrl held (as it is, during Ctrl+arrow) a plain setCurrentIndex
    toggles the target into the selection; the jump selects it alone."""
    ws = win.current
    m, t = ws.model, ws.table
    _select(ws, [(2, 2)])
    monkeypatch.setattr(QtGui.QGuiApplication, "keyboardModifiers",
                        staticmethod(lambda: QtCore.Qt.ControlModifier))
    QtTest.QTest.keyClick(t, QtCore.Qt.Key_Right, QtCore.Qt.ControlModifier)
    sel = {m.z_at(ix) for ix in t.selectionModel().selectedIndexes()}
    assert sel == {m.z_at(t.currentIndex())}


def test_editor_arrows_move_on_and_ctrl_enter_fills(win):
    ws = win.current
    m, t, e = ws.model, ws.table, ws.model.entity
    _select(ws, [(3, 2)])
    t.edit(t.currentIndex())
    QtWidgets.QApplication.processEvents()
    ed = QtWidgets.QApplication.focusWidget()
    assert isinstance(ed, QtWidgets.QLineEdit)
    ed.setText("123")
    ed.setCursorPosition(len(ed.text()))
    QtTest.QTest.keyClick(ed, QtCore.Qt.Key_Right)
    QtWidgets.QApplication.processEvents()
    QtWidgets.QApplication.processEvents()
    assert e.cell(3, 2) == pytest.approx(123, abs=e.eq.factor)    # kept
    assert m.z_at(t.currentIndex()) in ((3, 3), (4, 2))         # moved on
    assert t.state() != QtWidgets.QAbstractItemView.EditingState

    _select(ws, [(4, 1), (4, 2), (4, 3)])
    t.edit(t.currentIndex())
    QtWidgets.QApplication.processEvents()
    ed = QtWidgets.QApplication.focusWidget()
    ed.setText("77")
    QtTest.QTest.keyClick(ed, QtCore.Qt.Key_Return, QtCore.Qt.ControlModifier)
    QtWidgets.QApplication.processEvents()
    QtWidgets.QApplication.processEvents()
    assert [e.cell(4, c) for c in (1, 2, 3)] == pytest.approx(
        [77] * 3, abs=e.eq.factor)


def test_sum_over_the_selection(win):
    from ovtsuite import gui_cellkeys as K
    ws = _open(win, PLAIN)
    e = ws.model.entity
    _select(ws, [(5, 1), (5, 2)])
    before = [e.cell(5, 1), e.cell(5, 2)]
    assert K.apply_expression(ws, "*2") == 2
    assert [e.cell(5, 1), e.cell(5, 2)] == pytest.approx(
        [2 * v for v in before], abs=e.eq.factor)

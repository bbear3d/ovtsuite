# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Table tabs: several tables open at once inside one file tab.

Driven offscreen. What is pinned is the behaviour agreed for them: a click
opens a table in the italic preview tab and the next click reuses it; a
double-click, an edit or double-clicking the tab keeps it open; each table
remembers its heatmap, its ZF "Show Z as" mode, and where it was scrolled to
and what was selected; Swap X and Y axes is one switch for every table;
and the tables come back with their file tab (tab history and the startup
restore).
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

from conftest import BINARIES, ZF6HP_REFERENCE  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions + the GUI

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
KN = os.path.join(BINARIES, "03G906016KN.bin")
ZF = os.path.join(ZF6HP_REFERENCE, "3.0TFSI-Stock.bin")


@pytest.fixture(scope="module")
def G(tmp_path_factory, isolated_qsettings_module):
    for p in (LQ, KN):
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope,
                             str(tmp_path_factory.mktemp("settings")))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
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
    yield w
    w.close_discarding()


@pytest.fixture
def win(G, _win):
    """The window with one file tab (LQ), no tables open, axes as stored."""
    while _win.tabs.count() > 1:
        ws = _win.workspaces()[-1]
        while ws.primary is not None and ws.primary.modified:
            ws.primary.undo()
        _win.close_tab(ws)
    G.set_swap_axes(False)
    ws = _win.current
    ws.close_all_tables()
    ws.heat_chk.setChecked(False)
    yield _win
    while ws.primary.modified:
        ws.primary.undo()
    G.set_swap_axes(False)


def _in_view(ws, e):
    """Whether the tree shows ``e``'s row: a hidden row (a supporting map
    behind "Advanced") cannot be clicked, by a user or by these tests."""
    item = ws._item_of.get(id(e))
    while item is not None:
        if item.isHidden():
            return False
        item = item.parent()
    return id(e) in ws._item_of


def _maps(ws, n, min_rows=2, min_cols=2):
    """``n`` distinct named 2-D maps the tree shows, biggest first."""
    out = [e for e in ws.primary.entities
           if e.name and e.rows >= min_rows and e.cols >= min_cols
           and _in_view(ws, e)]
    out.sort(key=lambda e: -(e.rows * e.cols))
    seen, picked = set(), []
    for e in out:
        if e.name not in seen:
            seen.add(e.name)
            picked.append(e)
        if len(picked) == n:
            break
    return picked


def _click(ws, e):
    """A single click on ``e`` in the parameter tree."""
    ws.tree.setCurrentItem(ws._item_of[id(e)])


def _double(ws, e):
    item = ws._item_of[id(e)]
    ws.tree.setCurrentItem(item)
    ws.tree.itemDoubleClicked.emit(item, 0)


def _shown(ws):
    return [t.entity for t in ws.tables()]


# -- opening -----------------------------------------------------------------

def test_a_click_opens_a_preview_that_the_next_click_reuses(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _click(ws, a)
    assert _shown(ws) == [a] and ws.model.entity is a
    assert ws.ttabs.is_preview(0) and ws.ttabs.isVisible()
    _click(ws, b)
    assert _shown(ws) == [b], "the preview tab is reused, not added to"
    assert ws.model.entity is b and ws.ttabs.is_preview(0)


def test_a_double_click_keeps_the_table_open(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _double(ws, a)
    assert not ws.ttabs.is_preview(0)
    _click(ws, b)
    assert _shown(ws) == [a, b]
    assert ws.ttabs.currentIndex() == 1 and ws.ttabs.is_preview(1)


def test_double_clicking_the_preview_tab_keeps_it(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _click(ws, a)
    ws.ttabs.tabBarDoubleClicked.emit(0)
    assert not ws.ttabs.is_preview(0)
    _click(ws, b)
    assert _shown(ws) == [a, b]


def test_an_edit_keeps_the_table_open(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _click(ws, a)
    m = ws.model
    ix = next(m.index(r, c) for r in range(m.rowCount())
              for c in range(m.columnCount()) if m.z_at(m.index(r, c)))
    old = m.data(ix, QtCore.Qt.EditRole)
    assert m.setData(ix, str(float(old) + 1), QtCore.Qt.EditRole)
    assert not ws.ttabs.is_preview(0), "an edited table must not be replaced"
    _click(ws, b)
    assert _shown(ws) == [a, b]


def test_a_table_already_open_is_switched_to_not_opened_again(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _double(ws, a)
    _double(ws, b)
    _click(ws, a)
    assert _shown(ws) == [a, b] and ws.ttabs.currentIndex() == 0
    ws.show_entity(b)                      # how the dialogs jump to a table
    assert _shown(ws) == [a, b] and ws.ttabs.currentIndex() == 1


def test_switching_tabs_moves_the_tree_and_the_grid(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _double(ws, a)
    _double(ws, b)
    ws.ttabs.setCurrentIndex(0)
    assert ws.model.entity is a and ws._current_entity() is a
    assert (a.name in ws.title.text())
    ws.cycle_table(1)
    assert ws.model.entity is b and ws._current_entity() is b


def test_the_tab_says_what_the_table_is_and_the_symbol_is_in_its_tip(G, win):
    ws = win.current
    (a,) = _maps(ws, 1)
    _double(ws, a)
    text = ws.ttabs.tabText(0)
    assert text == G._elide(G._english(a), 32)
    assert a.name in ws.ttabs.tabToolTip(0)


# -- closing -----------------------------------------------------------------

def test_closing_a_table_shows_its_neighbour_and_the_last_leaves_none(win):
    ws = win.current
    a, b, c = _maps(ws, 3)
    for e in (a, b, c):
        _double(ws, e)
    ws.ttabs.setCurrentIndex(1)
    ws.close_table()
    assert _shown(ws) == [a, c] and ws.model.entity in (a, c)
    assert ws._current_entity() is ws.model.entity
    ws.close_all_tables()
    assert ws.tables() == [] and ws.model.entity is None
    assert not ws.ttabs.isVisible()
    assert ws.title.text() == "Select a parameter"
    assert ws.tree.selectedItems() == []
    _click(ws, a)                          # and the tree still opens one
    assert _shown(ws) == [a]


def test_close_other_tables_keeps_the_one_asked_for(win):
    ws = win.current
    a, b, c = _maps(ws, 3)
    for e in (a, b, c):
        _double(ws, e)
    ws.close_other_tables(1)
    assert _shown(ws) == [b] and ws.model.entity is b


def test_the_shortcuts(win):
    from PySide6 import QtGui
    acts = {a.text(): a.shortcut().toString()
            for a in win.findChildren(QtGui.QAction)}
    assert acts.get("Close table") == "Ctrl+F4"
    assert acts.get("Close tab") == "Ctrl+W", "Ctrl+W still closes the file"
    assert acts.get("Next table") == "Ctrl+PgDown"
    assert acts.get("Previous table") == "Ctrl+PgUp"


# -- what each table remembers ---------------------------------------------

def test_each_table_keeps_its_own_heatmap(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _double(ws, a)
    ws.heat_chk.setChecked(True)
    _double(ws, b)
    ws.heat_chk.setChecked(False)
    ws.ttabs.setCurrentIndex(0)
    assert ws.heat_chk.isChecked() and ws.model._heatmap
    ws.ttabs.setCurrentIndex(1)
    assert not ws.heat_chk.isChecked() and not ws.model._heatmap


def test_each_table_keeps_its_scroll_and_selection(win):
    ws = win.current
    big, other = _maps(ws, 2, min_rows=12, min_cols=8)
    ws.table.resize(300, 200)
    ws.table.setMinimumSize(1, 1)
    ws.table.setMaximumSize(300, 200)       # small enough to scroll
    try:
        _double(ws, big)
        m = ws.model
        sm = ws.table.selectionModel()
        sel = QtCore.QItemSelection(m.index(3, 2), m.index(5, 4))
        sm.select(sel, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        sm.setCurrentIndex(m.index(5, 4),
                           QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        vbar, hbar = ws.table.verticalScrollBar(), ws.table.horizontalScrollBar()
        ws.table.doItemsLayout()
        assert vbar.maximum() > 0, "the grid has to scroll for this test"
        vbar.setValue(vbar.maximum())
        hbar.setValue(hbar.maximum())
        where = (vbar.value(), hbar.value())
        picked = sorted((i.row(), i.column()) for i in sm.selectedIndexes())

        _double(ws, other)
        assert ws.model.entity is other
        ws.ttabs.setCurrentIndex(0)
        sm = ws.table.selectionModel()
        assert (vbar.value(), hbar.value()) == where
        assert sorted((i.row(), i.column())
                      for i in sm.selectedIndexes()) == picked
        assert (ws.table.currentIndex().row(),
                ws.table.currentIndex().column()) == (5, 4)
    finally:
        ws.table.setMaximumSize(16777215, 16777215)


# -- Swap X and Y axes: one switch -------------------------------------------

def test_swap_turns_every_table_and_is_remembered(G, win):
    ws = win.current
    a, b = _maps(ws, 2)
    _double(ws, a)
    assert not ws.model.transposed
    win._toggle_transpose()
    assert ws.model.transposed and win.swap_act.isChecked()
    assert G.swap_axes(), "remembered for next time"
    _double(ws, b)
    assert ws.model.transposed, "the next table opened is turned too"
    ws.ttabs.setCurrentIndex(0)
    assert ws.model.transposed
    win._toggle_transpose()
    assert not ws.model.transposed and not G.swap_axes()


def test_swap_reaches_the_other_file_tabs(G, win):
    first = win.current
    (a,) = _maps(first, 1)
    _double(first, a)
    win.open_path(KN, new_tab=True)
    second = win.current
    assert second is not first
    win._toggle_transpose()                 # flipped while in the KN tab
    win.show_workspace(first)
    assert first.model.transposed and win.swap_act.isChecked()
    win._toggle_transpose()
    assert not first.model.transposed


# -- the tables come back with their file tab -----------------------------

def _open_three(ws):
    a, b, c = _maps(ws, 3)
    _double(ws, a)
    _double(ws, b)
    _click(ws, c)                           # c is the preview
    ws.ttabs.setCurrentIndex(1)
    ws.heat_chk.setChecked(True)
    return a, b, c


def test_the_tab_session_lists_the_tables(win):
    ws = win.current
    a, b, c = _open_three(ws)
    ent = win.tab_session()[0]
    assert [t["name"] for t in ent["tables"]] == [a.name, b.name, c.name]
    assert [t["pinned"] for t in ent["tables"]] == [True, True, False]
    assert ent["table_active"] == 1 and ent["tables"][1]["heat"]


def test_reopening_tabs_at_startup_brings_the_tables_back(G, win):
    ws = win.current
    a, b, c = _open_three(ws)
    entries = win.tab_session()
    w2 = G.MainWindow()
    try:
        made = w2.restore_tabs(entries, [0])
        ws2 = made[0]
        assert [t.entity.name for t in ws2.tables()] == [a.name, b.name, c.name]
        assert [ws2.ttabs.is_preview(i) for i in range(3)] == [False, False, True]
        assert ws2.ttabs.currentIndex() == 1
        assert ws2.model.entity.name == b.name and ws2.heat_chk.isChecked()
        assert ws2._current_entity() is ws2.model.entity
    finally:
        w2.close_discarding()


def test_restoring_a_closed_tab_brings_its_tables_back(win):
    first = win.current
    win.open_path(KN, new_tab=True)
    ws = win.current
    a, b, c = _open_three(ws)
    win.close_tab(ws)
    assert win.closed_history()[0]["tables"]
    back = win.reopen_last_closed()
    assert back is not None and back is not first
    assert [t.entity.name for t in back.tables()] == [a.name, b.name, c.name]
    assert back.model.entity.name == b.name


def test_old_history_entries_still_show_their_one_parameter(win):
    first = win.current
    win.open_path(KN, new_tab=True)
    ws = win.current
    (a,) = _maps(ws, 1)
    win.close_tab(ws)
    ent = dict(win.closed_history()[0])
    ent.pop("tables", None)
    ent.pop("table_active", None)
    ent["entity"] = a.name
    back = win.restore_closed(ent)
    assert back is not first and [t.entity.name for t in back.tables()] == [a.name]


def test_opening_another_file_in_the_tab_keeps_the_tables_it_has(win):
    ws = win.current
    names = [e.name for e in _open_three(ws)]
    ws._quiet = True
    try:
        assert ws._load_primary(KN)
        kept = [t.entity.name for t in ws.tables()]
        assert kept and set(kept) <= set(names)
        assert all(t.entity in ws.primary.entities
                   + ws.primary.variant_entities() for t in ws.tables())
        assert ws.model.entity is ws._tt_cur.entity
    finally:
        assert ws._load_primary(LQ)
        ws._quiet = False


def test_closing_the_file_closes_its_tables(win):
    first = win.current
    win.open_path(KN, new_tab=True)
    ws = win.current
    _open_three(ws)
    ws.close_primary()
    assert ws.tables() == [] and not ws.ttabs.isVisible()
    assert first.primary is not None


# -- ZF 6HP: "Show Z as" is per table ---------------------------------------

def test_each_speed_map_keeps_its_own_z_mode(G, win):
    if not os.path.exists(ZF):
        pytest.skip("reference binary missing: %s" % ZF)
    win.open_path(ZF, new_tab=True)
    ws = win.current
    speed = [e for e in ws.primary.entities if G._view6.z_modes(e)][:2]
    assert len(speed) == 2
    a, b = speed
    _double(ws, a)
    ws.zmode_box.setCurrentIndex(ws.zmode_box.findData("engine"))
    _double(ws, b)
    assert ws.model.zmode == "engine", "a new table starts in the last choice"
    ws.zmode_box.setCurrentIndex(ws.zmode_box.findData("output"))
    ws.ttabs.setCurrentIndex(0)
    assert ws.model.zmode == "engine"
    assert ws.zmode_box.currentData() == "engine"
    ws.ttabs.setCurrentIndex(1)
    assert ws.model.zmode == "output"


# -- middle-click in the tree ------------------------------------------------

def _middle(ws, e):
    """A real middle-click (press + release) on ``e``'s row in the tree."""
    from PySide6.QtTest import QTest
    item = ws._item_of[id(e)]
    ws.tree.scrollToItem(item)
    QtWidgets.QApplication.processEvents()
    pos = ws.tree.visualItemRect(item).center()
    QTest.mouseClick(ws.tree.viewport(), QtCore.Qt.MouseButton.MiddleButton,
                     QtCore.Qt.KeyboardModifier.NoModifier, pos)


def test_a_middle_click_opens_a_new_tab_and_leaves_the_preview(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _click(ws, a)                           # a is the preview
    _middle(ws, b)
    assert _shown(ws) == [a, b], "the preview is kept, not replaced"
    assert ws.ttabs.is_preview(0) and not ws.ttabs.is_preview(1)
    assert ws.model.entity is b and ws._current_entity() is b


def test_middle_clicking_an_open_table_switches_to_it(win):
    ws = win.current
    a, b = _maps(ws, 2)
    _middle(ws, a)
    _middle(ws, b)
    _middle(ws, a)
    assert _shown(ws) == [a, b] and ws.model.entity is a

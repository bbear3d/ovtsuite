# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Find panel (ovtsuite/gui_search.py), driven offscreen over real images.

Pinned here: it opens from Edit > Find (Ctrl+F) and keeps everything when it is
collapsed or closed and opened again; a search follows the typing and a newer
one supersedes an older; a result selects its parameter in the tree and shows
it, Ctrl+Enter / double-click pins it in its own table tab; the cells a value,
address or byte search found are highlighted -- whichever way the grid is
turned; scope can widen to every open tab, and picking a hit in another tab
switches to it.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow

A = os.path.join(BINARIES, "038906016Q.Bin")
B = os.path.join(BINARIES, "038906016AB.bin")


@pytest.fixture(scope="module")
def G(tmp_path_factory, isolated_qsettings_module):
    for p in (A, B):
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
    w = G.MainWindow(TunerSession(A))
    w.resize(1300, 800)
    w.show()
    QtWidgets.QApplication.processEvents()
    yield w
    w.close_discarding()


@pytest.fixture
def win(G, _win):
    """One tab (A), the dock closed and reset to its defaults."""
    while _win.tabs.count() > 1:
        _win.close_tab(_win.workspaces()[-1])
    ws = _win.current
    while ws.tables():
        ws.close_table()
    _win.search_dock.hide()
    p = _win.search_dock.panel
    p._quiet = True
    p.history.clear()
    p._queries.clear()
    p.opts = type(p.opts)()
    p.opts.max_results = 2000
    p.mode.setCurrentIndex(0)
    p.scope.setCurrentIndex(0)
    p.edit.setText("")
    p._quiet = False
    p._mode_changed()
    p.set_collapsed(False)
    QtWidgets.QApplication.processEvents()
    return _win


def wait(p, timeout=30.0):
    """Until the panel has nothing pending: no timer, no search running."""
    end = time.time() + timeout
    while time.time() < end:
        QtWidgets.QApplication.processEvents()
        if not p._busy and not p._live_timer.isActive():
            QtWidgets.QApplication.processEvents()
            return
        time.sleep(0.01)
    raise AssertionError("search did not finish")


def search(p, text, mode=None):
    if mode:
        p.mode.setCurrentIndex(p.mode.findData(mode))
    p.edit.setText(text)
    if mode in ("value", "bytes") or not p._live_timer.isActive():
        p.run()
    wait(p)


def hits(p):
    return [p.results.topLevelItem(i).data(0, QtCore.Qt.ItemDataRole.UserRole)
            for i in range(p.results.topLevelItemCount())]


# --------------------------------------------------------------- the dock --
def test_find_opens_from_the_edit_menu_and_focuses_the_query(win):
    d = win.search_dock
    assert not d.isVisible()
    act = next(a for a in win.actions() if a.text() == "Find...")
    assert act.shortcut().toString() == "Ctrl+F"
    act.trigger()
    QtWidgets.QApplication.processEvents()
    assert d.isVisible()
    assert d.panel.edit.hasFocus() or d.panel.edit.isVisible()


def test_close_and_reopen_restores_query_options_and_selection(win):
    d, p = win.search_dock, win.search_dock.panel
    d.open()
    p._set_opt("match", "exact")
    p._set_opt("case_sensitive", True)
    search(p, "boost")
    assert p.results.topLevelItemCount() > 0
    p.results.setCurrentItem(p.results.topLevelItem(1))
    QtWidgets.QApplication.processEvents()
    n, row = p.results.topLevelItemCount(), p.results.currentIndex().row()
    d.close()
    QtWidgets.QApplication.processEvents()
    assert not d.isVisible()
    d.open()
    QtWidgets.QApplication.processEvents()
    assert d.isVisible()
    assert p.edit.text() == "boost"
    assert p.opts.match == "exact" and p.opts.case_sensitive
    assert p.results.topLevelItemCount() == n
    assert p.results.currentIndex().row() == row


def test_collapse_hides_results_but_keeps_them(win):
    d, p = win.search_dock, win.search_dock.panel
    d.open()
    search(p, "boost")
    n = p.results.topLevelItemCount()
    p.toggle_collapsed()
    QtWidgets.QApplication.processEvents()
    assert p.collapsed and not p.body.isVisible()
    assert p.edit.isVisible()               # the query row stays
    p.toggle_collapsed()
    QtWidgets.QApplication.processEvents()
    assert not p.collapsed and p.body.isVisible()
    assert p.results.topLevelItemCount() == n
    # opening from Find while collapsed expands it
    p.set_collapsed(True)
    d.open()
    assert not p.collapsed


def test_options_and_history_survive_a_new_window(G, win):
    from ovtsuite.session import TunerSession
    p = win.search_dock.panel
    win.search_dock.open()
    p._set_opt("match", "regex")
    search(p, r"^Boost\w+")
    p._commit_history()
    w2 = G.MainWindow(TunerSession(A))
    try:
        p2 = w2.search_dock.panel
        assert p2.opts.match == "regex"
        assert r"^Boost\w+" in p2.history.for_mode("text")
    finally:
        w2.close_discarding()


# ------------------------------------------------------------------ text --
def test_text_search_follows_typing_and_selecting_shows_the_parameter(win):
    d, p = win.search_dock, win.search_dock.panel
    d.open()
    search(p, "boost pressure")
    hs = hits(p)
    assert hs
    ws = win.current
    p.results.setCurrentItem(p.results.topLevelItem(0))
    QtWidgets.QApplication.processEvents()
    assert ws.model.entity is hs[0].entity
    assert ws._current_entity() is hs[0].entity       # selected in the tree
    # a click is a preview: nothing was pinned
    assert not any(t.pinned for t in ws.tables())


def test_newer_search_supersedes_older(win):
    p = win.search_dock.panel
    win.search_dock.open()
    p.edit.setText("zzzzzz")
    p.run()
    p.edit.setText("boost pressure")
    p.run()
    wait(p)
    hs = hits(p)
    assert hs and all("zzzzzz" not in (h.entity.name or "").lower()
                      for h in hs)
    assert p.status.text().endswith("results") or "result" in p.status.text()


def test_enter_walks_the_results_and_ctrl_enter_pins(win):
    p = win.search_dock.panel
    win.search_dock.open()
    search(p, "boost pressure")
    ws = win.current
    QtTest = pytest.importorskip("PySide6.QtTest")
    QtTest.QTest.keyClick(p.edit, QtCore.Qt.Key.Key_Return)
    assert p.results.currentIndex().row() == 0
    QtTest.QTest.keyClick(p.edit, QtCore.Qt.Key.Key_Return)
    assert p.results.currentIndex().row() == 1
    QtTest.QTest.keyClick(p.edit, QtCore.Qt.Key.Key_Return,
                          QtCore.Qt.KeyboardModifier.ShiftModifier)
    assert p.results.currentIndex().row() == 0
    QtTest.QTest.keyClick(p.edit, QtCore.Qt.Key.Key_Return,
                          QtCore.Qt.KeyboardModifier.ControlModifier)
    QtWidgets.QApplication.processEvents()
    assert any(t.pinned and t.entity is hits(p)[0].entity
               for t in ws.tables())


def test_double_click_opens_in_a_new_table_tab(win):
    p = win.search_dock.panel
    win.search_dock.open()
    search(p, "boost pressure")
    ws = win.current
    p.open_in_tab(p.results.topLevelItem(2))
    e = hits(p)[2].entity
    assert any(t.pinned and t.entity is e for t in ws.tables())


def test_bad_regex_is_reported_not_raised(win):
    p = win.search_dock.panel
    win.search_dock.open()
    p._set_opt("match", "regex")
    p.edit.setText("([")
    p.run()
    wait(p)
    assert p.results.topLevelItemCount() == 0
    assert "regular expression" in p.status.text().lower()


def test_stop_cancels_a_running_search(win):
    p = win.search_dock.panel
    win.search_dock.open()
    p.mode.setCurrentIndex(p.mode.findData("value"))
    p.edit.setText(">=-1000000")             # matches nearly everything
    p.run()
    assert p._busy
    p._go_or_stop()
    assert not p._busy
    wait(p)
    assert p.results.topLevelItemCount() == 0


# ------------------------------------------------------ highlight / cells --
def _const(ws):
    e = next(x for x in ws.primary.entities
             if x.kind == "constant" and not x.text_kind and x.bits == 16
             and x.tier == 1)
    return e


def test_value_search_highlights_the_cell_and_scrolls_to_it(win):
    p = win.search_dock.panel
    win.search_dock.open()
    ws = win.current
    k = _const(ws)
    search(p, "%.9g" % k.cell(0, 0), "value")
    p._set_opt("tolerance", 1e-6)
    hs = [h for h in hits(p) if h.entity is k]
    assert hs
    row = [h.entity for h in hits(p)].index(k)
    p.results.setCurrentItem(p.results.topLevelItem(row))
    QtWidgets.QApplication.processEvents()
    assert ws.model.entity is k
    assert ("z", 0, 0) in ws.model.hl
    idx = ws.model.index_of("z", 0, 0)
    assert idx.isValid()
    assert ws.model.data(idx, QtCore.Qt.BackgroundRole) == G_HL(win)
    # leaving the table clears it
    other = next(x for x in ws.primary.entities if x is not k and x.kind == "map")
    ws.show_entity(other)
    assert not ws.model.hl


def G_HL(win):
    from ovtsuite import gui
    return gui.HL_BG


def _map(ws):
    return next(x for x in ws.primary.entities
                if x.kind == "map" and x.rows >= 3 and x.cols >= 3
                and x.x is not None and x.y is not None
                and x.x.addr is not None and x.y.addr is not None)


@pytest.mark.parametrize("transposed", [False, True])
def test_address_search_resolves_the_exact_cell_in_either_orientation(
        win, transposed):
    p = win.search_dock.panel
    win.search_dock.open()
    ws = win.current
    m = _map(ws)
    r, c = 1, 2
    a = m.addr + m.index(r, c) * m.step + (m.step - 1)     # last byte of it
    ws.show_entity(m)
    ws.model.transposed = bool(transposed)
    ws.model.layoutChanged.emit()
    search(p, "0x%X" % a, "address")
    hs = [h for h in hits(p) if h.entity is m]
    assert hs and hs[0].cells == (("z", r, c),)
    p.results.setCurrentItem(p.results.topLevelItem(
        [h.entity for h in hits(p)].index(m)))
    QtWidgets.QApplication.processEvents()
    idx = ws.model.index_of("z", r, c)
    assert idx.isValid()
    assert ws.model.z_at(idx) == (r, c)              # the inverse of _kind
    assert ws.model.data(idx, QtCore.Qt.BackgroundRole) == G_HL(win)
    ws.model.transposed = False


def test_address_inside_an_axis_highlights_the_axis_element(win):
    p = win.search_dock.panel
    win.search_dock.open()
    ws = win.current
    m = _map(ws)
    a = m.x.addr + 2 * max(1, m.x.bits // 8)
    search(p, "0x%X" % a, "address")
    h = next(h for h in hits(p) if h.entity is m and h.cells[0][0] == "x")
    assert h.cells == (("x", 2, 0),)
    p.results.setCurrentItem(p.results.topLevelItem(hits(p).index(h)))
    QtWidgets.QApplication.processEvents()
    idx = ws.model.index_of("x", 2, 0)
    assert idx.isValid() and ws.model.data(
        idx, QtCore.Qt.BackgroundRole) == G_HL(win)


def test_address_search_with_no_owner_says_where_the_nearest_is(win):
    p = win.search_dock.panel
    win.search_dock.open()
    ws = win.current
    top = max(e.end for e in ws.primary.entities if e.addr is not None) + 5
    search(p, "0x%X" % (top + 0x100000), "address")
    assert p.results.topLevelItemCount() == 0
    assert "nearest" in p.status.text().lower()


def test_bytes_search_runs_inside_the_open_table_only(win):
    p = win.search_dock.panel
    win.search_dock.open()
    ws = win.current
    m = _map(ws)
    ws.show_entity(m)
    cell = m.raw_cell(1, 1)
    pat = " ".join("%02X" % b for b in int(cell).to_bytes(
        m.step, "big", signed=False))
    p.mode.setCurrentIndex(p.mode.findData("bytes"))
    p._set_opt("include_axes", False)
    p.edit.setText(pat)
    p.run()
    wait(p)
    hs = hits(p)
    assert hs and all(h.entity is m for h in hs)
    assert any(("z", 1, 1) in h.cells for h in hs)


def test_bytes_search_without_a_table_explains_itself(win):
    p = win.search_dock.panel
    win.search_dock.open()
    ws = win.current
    while ws.tables():
        ws.close_table()
    ws.model.set_entity(None, None)
    p.mode.setCurrentIndex(p.mode.findData("bytes"))
    p.edit.setText("DE AD")
    p.run()
    wait(p)
    assert "table" in p.status.text().lower() or "parameter" in \
        p.status.text().lower()


# ------------------------------------------------------------------ scope --
def test_all_open_tabs_scope_and_picking_in_another_tab(G, win):
    from ovtsuite.session import TunerSession
    p = win.search_dock.panel
    ws_a = win.current
    ws_b = win._add_workspace(TunerSession(B))
    win.show_workspace(ws_a)
    win.search_dock.open()
    p.scope.setCurrentIndex(p.scope.findData("all"))
    search(p, "boost pressure")
    owners = {h.source.owner for h in hits(p)}
    assert owners == {ws_a, ws_b}
    assert not p.results.isColumnHidden(4)
    row = next(i for i, h in enumerate(hits(p)) if h.source.owner is ws_b)
    p.results.setCurrentItem(p.results.topLevelItem(row))
    QtWidgets.QApplication.processEvents()
    assert win.current is ws_b
    assert ws_b.model.entity is hits(p)[row].entity
    # back to this-tab scope: only the active tab is searched
    p.scope.setCurrentIndex(p.scope.findData("tab"))
    search(p, "boost pressure")
    assert {h.source.owner for h in hits(p)} == {ws_b}


def test_pair_scope_needs_a_comparison(win):
    p = win.search_dock.panel
    win.search_dock.open()
    p._update_scope_choices()
    assert not p.scope.model().item(1).isEnabled()

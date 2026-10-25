# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison table in a grid of its own, scrolled with the table
(ovtsuite.gui_cmpscroll; Tools > Options > Editor > Comparison table)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from conftest import BINARIES  # noqa: E402

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
KN = os.path.join(BINARIES, "03G906016KN.bin")
TALL = "InjCrv_tT3DiaDevMax_MAP"        # 25x40: taller than the grid
RESAMPLED = "AccPed_dnDOSetP_MAP"      # 6x6 over a 5x6: values resampled
SEL = QtCore.QItemSelectionModel.SelectionFlag


@pytest.fixture(scope="module")
def win(tmp_path_factory, isolated_qsettings_module):
    for p in (LQ, KN):
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope,
                             str(tmp_path_factory.mktemp("settings")))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    w = gui.MainWindow(TunerSession(LQ), TunerSession(KN))
    w.resize(1300, 900)
    w.show()
    app.processEvents()
    yield w
    from ovtsuite import options
    options.set("ui/compare_layout", "off")
    w.close_discarding()


def _pump():
    app = QtWidgets.QApplication.instance()
    for _ in range(3):
        app.processEvents()


def _mode(m):
    from ovtsuite import options
    options.set("ui/compare_layout", m)
    _pump()


def _show(win, name):
    ws = win.current
    e = next(e for e in ws.primary.entities if e.name == name)
    ws.show_entity(e)
    if ws.model.transposed:
        ws._toggle_transpose()
    _pump()
    ws.table.verticalScrollBar().setValue(0)
    _pump()
    return ws


def _cur(view):
    i = view.currentIndex()
    return (i.row(), i.column())


@pytest.mark.parametrize("m", ["below", "beside"])
def test_the_comparison_follows_the_table(win, m):
    _mode(m)
    ws = _show(win, TALL)
    t, v, g = ws.table, ws.cmp_grid.view, ws.cmp_grid
    sep = ws.model.sep_row()
    assert sep is not None and g.active() and v.isVisible()
    assert g.split.orientation() == (QtCore.Qt.Orientation.Horizontal
                                     if m == "beside" else
                                     QtCore.Qt.Orientation.Vertical)
    # each grid shows its own half only
    assert all(t.isRowHidden(r) for r in range(sep, ws.model.rowCount()))
    assert not any(t.isRowHidden(r) for r in range(sep))
    assert all(v.isRowHidden(r) for r in range(sep + 1))
    # scroll the table: the comparison shows the same rows
    t.verticalScrollBar().setValue(7)
    _pump()
    top = t.rowAt(0)
    assert top > 0 and v.rowAt(0) == sep + 1 + top
    # the current cell and the selection follow, onto the same point
    t.selectionModel().setCurrentIndex(ws.model.index(top + 2, 3),
                                       SEL.ClearAndSelect)
    t.selectionModel().select(QtCore.QItemSelection(
        ws.model.index(top + 2, 3), ws.model.index(top + 3, 5)), SEL.Select)
    _pump()
    assert _cur(v) == (sep + 1 + top + 2, 3)
    rows = {i.row() for i in v.selectionModel().selectedIndexes()}
    assert rows == {sep + 1 + top + 2, sep + 1 + top + 3}
    assert v.rowAt(0) == sep + 1 + top          # still level with the table
    # and back: scrolling the comparison moves the table
    v.verticalScrollBar().setValue(v.verticalScrollBar().value() + 3)
    _pump()
    assert v.rowAt(0) == sep + 1 + t.rowAt(0)
    # column widths are shared
    t.setColumnWidth(2, 140)
    _pump()
    assert v.columnWidth(2) == 140
    assert v.rowHeight(sep + 1 + top) == t.rowHeight(top)


def test_a_click_in_the_comparison_moves_the_table(win):
    _mode("below")
    ws = _show(win, TALL)
    t, v = ws.table, ws.cmp_grid.view
    sep = ws.model.sep_row()
    v.selectionModel().setCurrentIndex(ws.model.index(sep + 1 + 4, 6),
                                       SEL.ClearAndSelect)
    _pump()
    assert _cur(t) == (4, 6)
    assert [(i.row(), i.column())
            for i in t.selectionModel().selectedIndexes()] == [(4, 6)]


def test_a_resampled_comparison_lines_up_cell_for_cell(win):
    """Different sizes: build_overlay put the comparison on the primary's
    breakpoints, so the comparison grid has the table's rows exactly."""
    _mode("below")
    ws = _show(win, RESAMPLED)
    m, t, v = ws.model, ws.table, ws.cmp_grid.view
    cp = ws.primary.counterpart(ws.comparison, m.entity)
    assert (cp.rows, cp.cols) != (m.entity.rows, m.entity.cols)
    assert m.overlay is not None and m.overlay.mode != "direct"
    sep = m.sep_row()
    shown_t = [r for r in range(m.rowCount()) if not t.isRowHidden(r)]
    shown_v = [r for r in range(m.rowCount()) if not v.isRowHidden(r)]
    assert len(shown_t) == len(shown_v) == sep
    t.selectionModel().setCurrentIndex(m.index(sep - 1, m.columnCount() - 1),
                                       SEL.ClearAndSelect)
    _pump()
    assert _cur(v) == (m.rowCount() - 1, m.columnCount() - 1)


def test_copy_in_the_comparison_grid(win):
    _mode("below")
    ws = _show(win, TALL)
    m, v = ws.model, ws.cmp_grid.view
    sep = m.sep_row()
    r0 = sep + 1 + m._top
    c0 = m._left
    v.selectionModel().select(QtCore.QItemSelection(
        m.index(r0, c0), m.index(r0 + 1, c0 + 1)), SEL.ClearAndSelect)
    ws.cmp_grid.copy()
    text = QtWidgets.QApplication.clipboard().text()
    lines = text.strip("\n").split("\n")
    assert len(lines) == 2 and len(lines[0].split("\t")) == 2
    assert lines[0].split("\t")[0] == m.data(m.index(r0, c0))


def test_off_keeps_one_grid_and_independent_scrolling(win):
    _mode("below")
    ws = _show(win, TALL)
    _mode("off")
    t, v, g = ws.table, ws.cmp_grid.view, ws.cmp_grid
    sep = ws.model.sep_row()
    assert not g.active() and not g.cmp_box.isVisible()
    # the comparison is back in the table's own grid, under the banner
    assert not any(t.isRowHidden(r) for r in range(ws.model.rowCount()))
    before = v.verticalScrollBar().value()
    t.verticalScrollBar().setValue(t.verticalScrollBar().maximum())
    _pump()
    assert v.verticalScrollBar().value() == before
    t.selectionModel().setCurrentIndex(ws.model.index(3, 3),
                                       SEL.ClearAndSelect)
    _pump()
    assert not v.selectionModel().selectedIndexes()
    assert t.verticalHeader().minimumWidth() == 0
    assert sep is not None


def test_the_setting_applies_to_a_new_table_and_a_turned_one(win):
    _mode("beside")
    ws = _show(win, RESAMPLED)
    ws = _show(win, TALL)
    sep = ws.model.sep_row()
    assert ws.table.isRowHidden(sep)
    ws._toggle_transpose()
    _pump()
    sep = ws.model.sep_row()
    t, v = ws.table, ws.cmp_grid.view
    assert t.isRowHidden(sep) and v.isRowHidden(sep)
    assert not v.isRowHidden(sep + 1)
    t.selectionModel().setCurrentIndex(ws.model.index(1, 2),
                                       SEL.ClearAndSelect)
    _pump()
    assert _cur(v) == (sep + 2, 2)
    ws._toggle_transpose()
    _pump()
    _mode("off")


# -- mark: one grid, the matching cells framed --------------------------
def _sel(view, r0, c0, r1, c1):
    m = view.model()
    sm = view.selectionModel()
    sm.setCurrentIndex(m.index(r0, c0), SEL.ClearAndSelect)
    sm.select(QtCore.QItemSelection(m.index(r0, c0), m.index(r1, c1)),
              SEL.Select)
    _pump()


def test_mark_frames_the_matching_cells_both_ways(win):
    _mode("mark")
    ws = _show(win, TALL)
    t, g = ws.table, ws.cmp_grid
    sep = ws.model.sep_row()
    assert g.marking() and g.marks.isVisible() and not g.cmp_box.isVisible()
    assert not any(t.isRowHidden(r) for r in range(ws.model.rowCount()))
    _sel(t, 3, 2, 4, 5)                      # primary cells ...
    assert g.mark_ranges() == [(sep + 4, 2, sep + 5, 5)]    # ... framed below
    _sel(t, sep + 1 + 6, 3, sep + 1 + 6, 3)  # a comparison cell ...
    assert g.mark_ranges() == [(6, 3, 6, 3)]                # ... framed above
    # drawn where the cell is, when it is on screen
    t.scrollTo(ws.model.index(6, 3))
    _pump()
    assert g.marks.rects() and g.marks.rects()[0] == t.visualRect(
        ws.model.index(6, 3))
    img = g.marks.grab()
    assert img.width() == t.viewport().width()


def test_ctrl_j_jumps_to_the_matching_cells_and_back(win):
    _mode("mark")
    ws = _show(win, TALL)
    t, g = ws.table, ws.cmp_grid
    sep = ws.model.sep_row()
    _sel(t, 2, 4, 3, 6)
    g.jump()
    _pump()
    assert _cur(t) == (sep + 3, 4)
    rows = {i.row() for i in t.selectionModel().selectedIndexes()}
    assert rows == {sep + 3, sep + 4}
    assert t.rowAt(0) <= sep + 3 <= t.rowAt(t.viewport().height() - 1) \
        or t.rowAt(t.viewport().height() - 1) == -1
    assert g.mark_ranges() == [(2, 4, 3, 6)]
    g.jump()                                 # and back
    _pump()
    assert _cur(t) == (2, 4)
    assert {i.row() for i in t.selectionModel().selectedIndexes()} == {2, 3}
    assert t.rowAt(0) <= 2


def test_ctrl_j_switches_grid_below(win):
    _mode("below")
    ws = _show(win, TALL)
    t, v, g = ws.table, ws.cmp_grid.view, ws.cmp_grid
    sep = ws.model.sep_row()
    _sel(t, 5, 3, 5, 3)
    t.setFocus()
    g.jump()
    _pump()
    assert _cur(v) == (sep + 6, 3)
    assert all(sc.isEnabled() for sc in g.jump_sc)


def test_off_marks_nothing_and_has_no_jump(win):
    _mode("mark")
    ws = _show(win, TALL)
    _mode("off")
    t, g = ws.table, ws.cmp_grid
    _sel(t, 3, 2, 4, 5)
    assert not g.marks.isVisible() and g.mark_ranges() == []
    assert not any(sc.isEnabled() for sc in g.jump_sc)
    g.jump()
    _pump()
    assert _cur(t) == (3, 2)


@pytest.mark.parametrize("m", ["below", "beside"])
def test_no_banner_painted_under_the_table_in_its_own_grid(win, m):
    """Qt paints a span in a hidden row: in its own grid the banner row is
    hidden in the table, so it carries no span there (else its text shows
    over the table's last row); back in one grid it spans again."""
    _mode(m)
    ws = _show(win, TALL)
    sep = ws.model.sep_row()
    assert sep is not None and ws.table.isRowHidden(sep)
    assert ws.table.columnSpan(sep, 0) == 1
    ws.show_entity(ws.model.entity)         # a model reset keeps it so
    _pump()
    assert ws.table.columnSpan(ws.model.sep_row(), 0) == 1
    _mode("off")
    sep = ws.model.sep_row()
    assert ws.table.columnSpan(sep, 0) == ws.model.columnCount()


def _differing_cz(ws):
    """A comparison cell (grid row, col) whose value differs from this
    file's, and the primary (r, c) it lines up with."""
    m = ws.model
    for gr in range(m.sep_row() + 1, m.rowCount()):
        for gc in range(m.columnCount()):
            k, a, b = m._kind(gr, gc)
            if k == "cz" and m._at(m._vals, a, b) is not None \
                    and m._at(m._cmp, a, b) is not None \
                    and m._at(m._cmp, a, b) != m._at(m._vals, a, b):
                return (gr, gc), (a, b)
    return None, None


def _differing_map(win):
    """A map on screen whose comparison differs somewhere, with that cell."""
    names = {e.name for e in win.current.primary.entities}
    for name in (TALL, RESAMPLED, "AccPed_trqEng0_MAP", "FlMng_qAFSCDSmoke_MAP"):
        if name in names:
            ws = _show(win, name)
            cell, rc = _differing_cz(ws)
            if cell:
                return ws, cell, rc
    pytest.skip("no map whose comparison differs")


@pytest.mark.parametrize("m", ["off", "below"])
def test_copy_cells_to_primary_from_the_comparison(win, m):
    """Right-click > Copy cells to primary on comparison cells writes their
    values into the same cells of this file, one undoable step -- in the
    table's own grid (rows under the banner) and in the comparison grid."""
    from ovtsuite import gui_cellkeys as gck
    _mode(m)
    ws, cell, rc = _differing_map(win)
    view = ws.cmp_grid.view if m == "below" else ws.table
    ix = ws.model.index(*cell)
    view.selectionModel().setCurrentIndex(ix, SEL.ClearAndSelect)
    assert gck.comparison_cells(ws, view) == [rc]
    want = ws.model._at(ws.model._cmp, *rc)
    assert gck.to_primary(ws, view) == 1
    assert ws.model._at(ws.model._vals, *rc) == want
    ws.primary.undo()
    ws.model.refresh()
    assert ws.model._at(ws.model._vals, *rc) != want
    view.clearSelection()
    _mode("off")


def test_the_menu_offers_copy_to_primary_on_comparison_rows(win, monkeypatch):
    """On comparison rows the table's menu has Copy cells to primary in
    place of Copy selected cells from comparison."""
    from PySide6 import QtWidgets as W
    _mode("off")
    ws, cell, _rc = _differing_map(win)
    texts = {}

    class Menu(W.QMenu):                # records what it holds, never opens
        def exec(self, *_a):
            texts["t"] = [a.text() for a in self.actions() if a.text()]
    monkeypatch.setattr(W, "QMenu", Menu)
    ws.table.selectionModel().setCurrentIndex(ws.model.index(*cell),
                                              SEL.ClearAndSelect)
    ws._table_menu(QtCore.QPoint(1, 1))
    assert "Copy cells to primary" in texts["t"]
    assert "Copy selected cells from comparison" not in texts["t"]
    ws.table.selectionModel().setCurrentIndex(ws.model.index(1, 1),
                                              SEL.ClearAndSelect)
    ws._table_menu(QtCore.QPoint(1, 1))
    assert "Copy cells to primary" not in texts["t"]
    assert "Copy selected cells from comparison" in texts["t"]


# ------------------------------------- views of different sizes (ViewLink)
def _last_shown(view, orient):
    """The last section ``view`` shows (at its trailing edge)."""
    if orient == QtCore.Qt.Vertical:
        h, n = view.verticalHeader(), view.viewport().height()
    else:
        h, n = view.horizontalHeader(), view.viewport().width()
    i = h.logicalIndexAt(n - 1)
    return i if i >= 0 else h.count() - 1


def _first_shown(view, orient):
    h = view.verticalHeader() if orient == QtCore.Qt.Vertical else view.horizontalHeader()
    return h.logicalIndexAt(0)


@pytest.mark.parametrize("per_item", [True, False])
def test_views_of_different_sizes_reach_their_ends_together(per_item):
    """A taller (wider) view scrolls less far than a shorter one: at either
    end both views are at that end and show the same last (first) row;
    in between they keep the same top row where both can; a resize that
    moves an end brings the other view back to it."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite.gui_cmpscroll import ViewLink
    host = QtWidgets.QWidget()
    a, b = QtWidgets.QTableWidget(60, 30, host), QtWidgets.QTableWidget(60, 30, host)
    mode = (QtWidgets.QAbstractItemView.ScrollPerItem if per_item
            else QtWidgets.QAbstractItemView.ScrollPerPixel)
    for t in (a, b):
        t.setVerticalScrollMode(mode)
        t.setHorizontalScrollMode(mode)
    a.setGeometry(0, 0, 500, 400)
    b.setGeometry(0, 400, 260, 150)
    same = (lambda i: i)
    link = ViewLink(a, b, same, same)
    host.show()
    _pump()
    V, H = QtCore.Qt.Vertical, QtCore.Qt.Horizontal
    try:
        for orient in (V, H):
            bar = (lambda t: t.verticalScrollBar() if orient == V
                   else t.horizontalScrollBar())
            assert bar(a).maximum() != bar(b).maximum()
            for src, dst in ((a, b), (b, a)):
                bar(src).setValue(bar(src).maximum())
                _pump()
                assert bar(dst).value() == bar(dst).maximum(), (orient, src is a)
                assert _last_shown(dst, orient) == _last_shown(src, orient) == \
                    (59 if orient == V else 29)
                bar(src).setValue(0)
                _pump()
                assert bar(dst).value() == 0
                # partway, short of both ends: the same top row
                mid = min(bar(a).maximum(), bar(b).maximum()) // 2
                bar(src).setValue(mid)
                _pump()
                assert _first_shown(dst, orient) == _first_shown(src, orient)
        # a at its bottom, b made shorter: b back at its bottom too
        a.verticalScrollBar().setValue(a.verticalScrollBar().maximum())
        _pump()
        b.setGeometry(0, 400, 260, 110)
        _pump()
        assert _last_shown(b, V) == 59
        assert link.enabled
    finally:
        host.close()
        host.deleteLater()
        _pump()

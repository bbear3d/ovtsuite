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

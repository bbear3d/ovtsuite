# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The comparison binary in the shift map and lock-up editors
(ovtsuite.gui_gearbox_compare, ovtsuite.gearbox_edit.compare), offscreen, on
the 6HP19 3.0 TFSI stage 1 image compared with the stock one."""
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets  # noqa: E402

from conftest import ZF6HP_REFERENCE as SIXHP  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def pair(app):
    tuned = os.path.join(SIXHP, "3.0TFSI-Stage-1.bin")
    stock = os.path.join(SIXHP, "3.0TFSI-Stock.bin")
    if not (os.path.exists(tuned) and os.path.exists(stock)):
        pytest.skip("no ZF 6HP reference binaries")
    from ovtsuite.session import TunerSession
    return TunerSession(tuned), TunerSession(stock)


def _dialog(primary, comparison, kind="shift"):
    from ovtsuite import gui_gearbox_edit as G

    class WS(QtWidgets.QWidget):
        pass
    ws = WS()
    ws.primary, ws.model, ws.project_ref = primary, None, None
    ws.comparison = comparison
    d = G.GearboxEditDialog(ws, G.schedule_set_for(primary, kind))
    d._recompute()
    return d


@pytest.fixture
def shift(pair):
    d = _dialog(*pair)
    yield d
    d.close()


@pytest.fixture
def tcc(pair):
    d = _dialog(*pair, kind="tcc")
    yield d
    d.close()


# ------------------------------------------------------------- pairing (core)
def test_pairing_by_key_and_shape(pair):
    from ovtsuite import gearbox_edit as GE
    from ovtsuite import gui_gearbox_edit as G
    a, b = (G.schedule_set_for(s) for s in pair)
    p = GE.pair_sets(a, b)
    assert p.ok and len(p.pairs) == 82 and not p.unmatched and not p.axis_differs
    assert all(a.schedules[i].key == b.schedules[j].key for i, j in p.pairs.items())
    assert "82 of 82 schedules paired" in p.summary()
    # nothing to pair with: said so, and off
    assert not GE.pair_sets(a, None).ok
    assert "no shift schedules" in GE.pair_sets(a, None).summary()
    t = G.schedule_set_for(pair[1], "tcc")
    assert not GE.pair_sets(a, t).ok        # another kind / other lines


def test_copy_op_takes_the_comparisons_cells_exactly(pair):
    from ovtsuite import gearbox_edit as GE
    from ovtsuite import gui_gearbox_edit as G
    a, b = (G.schedule_set_for(s) for s in pair)
    p = GE.pair_sets(a, b)
    i = next(i for i in p.pairs if a.schedules[i].values() != b.schedules[p.pairs[i]].values())
    s, j = a.schedules[i], p.pairs[i]
    bv = b.schedules[j].values()
    diff = [k for k, (x, y) in enumerate(zip(s.values(), bv)) if x != y]
    cells = [(i, k // s.cols, k % s.cols) for k in diff]
    op = GE.copy_op(p, cells, {j: bv}, "copy", current={i: s.values()})
    assert op is not None and len(op.values) == len(diff)
    res = GE.Edit(a, [op]).compute()
    assert res.grid[i] == bv
    # nothing differs any more: no step
    assert GE.copy_op(p, cells, {j: bv}, "copy", current={i: bv}) is None


# ---------------------------------------------------------- the dialog: chart
def test_ghost_lines_in_every_unit(shift):
    cv = shift.cmpv
    assert cv.chk.isVisibleTo(shift) and cv.chk.isChecked() and cv.chk.isEnabled()
    assert "82 of 82" in cv.lbl.text()
    shift._apply_preset("All schedules")
    for u in range(shift.unit_box.count()):
        shift.unit_box.setCurrentIndex(u)
        shift._recompute()
        assert shift.chart.series and all(s.get("cmp") for s in shift.chart.series)
        assert "comparison" in shift.chart.title
    # both values in the hover tooltip
    shift.chart.resize(800, 500)
    shift.chart.grab()                      # paints: sets the chart's transform
    s = shift.chart.series[0]
    tip = shift.chart.tip_at(s, 400)
    assert "this file" in tip and "comparison" in tip


def test_ghost_lines_on_the_lockup_editor(tcc):
    assert tcc.cmpv.active()
    tcc._apply_preset("Both codings: Gear 3")
    tcc._recompute()
    assert tcc.chart.series and all(s.get("cmp") for s in tcc.chart.series)


def test_switch_off_hides_everything(shift):
    cv = shift.cmpv
    cv.chk.setChecked(False)
    shift._recompute()
    assert not any(s.get("cmp") for s in shift.chart.series)
    assert not cv.box.isVisibleTo(shift)
    assert all(shift.table.item(r, c).background().style() == QtCore.Qt.NoBrush
               for r in range(shift.table.rowCount())
               for c in range(shift.table.columnCount()) if shift.table.item(r, c))


# ---------------------------------------------------------- the dialog: table
def _differing(t):
    return [(r, c) for r in range(t.rowCount()) for c in range(t.columnCount())
            if t.item(r, c) is not None
            and t.item(r, c).background().style() != QtCore.Qt.NoBrush]


def test_comparison_table_shades_and_follows(shift):
    from ovtsuite import gui as _g
    cv, pt = shift.cmpv, shift.table
    ct = cv.cmp_table
    shift.range_edit.setText("10-12")
    shift._select_range(False)
    shift._recompute()
    assert cv.box.isVisibleTo(shift) and "read only" in cv.head.text()
    assert (ct.rowCount(), ct.columnCount()) == (pt.rowCount(), pt.columnCount())
    assert [ct.verticalHeaderItem(r).text() for r in range(ct.rowCount())] == \
        [pt.verticalHeaderItem(r).text() for r in range(pt.rowCount())]
    diff = _differing(ct)
    assert diff and _differing(pt) == diff
    r, c = diff[0]
    assert ct.item(r, c).background().color() == _g.DIFF_BG
    assert "This file:" in ct.item(r, c).toolTip()
    assert "Comparison:" in pt.item(r, c).toolTip()
    assert ct.item(r, c).text() != pt.item(r, c).text()
    # same column widths, current cell and scroll as the primary
    assert all(pt.columnWidth(k) == ct.columnWidth(k) for k in range(pt.columnCount()))
    pt.setCurrentCell(1, 3)
    assert (ct.currentRow(), ct.currentColumn()) == (1, 3)
    pt.resize(200, 120)
    ct.resize(200, 120)
    pt.horizontalHeader().resizeSection(0, 300)
    assert ct.columnWidth(0) == 300
    pt.horizontalScrollBar().setValue(pt.horizontalScrollBar().maximum())
    # at the end, both at the end (their viewports may differ in width)
    assert ct.horizontalScrollBar().value() == ct.horizontalScrollBar().maximum()
    assert _edge(ct, QtCore.Qt.Horizontal, True) == _edge(pt, QtCore.Qt.Horizontal, True)
    pt.horizontalScrollBar().setValue(1)
    assert ct.horizontalHeader().logicalIndexAt(0) == pt.horizontalHeader().logicalIndexAt(0)


def _cells(t):
    return [(r, c) for r in range(t.rowCount()) for c in range(t.columnCount())
            if t.item(r, c) is not None]


def test_cell_colours_follow_the_main_editor(shift):
    """Differs from the comparison: the main editor's orange and dark text
    (gui.TableModel._cell_style), in both tables. A cell the edit would
    change: the app's "held, not applied yet" look (the fault-code and
    measuring-block windows: PENDING_BG, bold, dark text), which wins over
    the orange in the primary while the comparison's copy still says
    whether it differs from the preview. No other fill (the old translucent
    amber, or anything else) in either table."""
    from ovtsuite import gui as _g
    from ovtsuite import gui_dtc, gui_msblock
    from ovtsuite import gui_gearbox_compare as GC
    assert GC.PENDING_BG == gui_dtc.PENDING_BG == gui_msblock.PENDING_BG
    pt, ct = shift.table, shift.cmpv.cmp_table
    shift.range_edit.setText("10-12")
    shift._select_range(False)
    shift._recompute()
    # no edit yet: only the comparison colours, the same in both tables
    diff = _differing(ct)
    assert diff and _differing(pt) == diff
    for t in (pt, ct):
        for r, c in diff:
            it = t.item(r, c)
            assert it.background().color() == _g.DIFF_BG
            assert it.foreground().color() == _g.CELL_DARK_FG
            assert not it.font().bold() and not GC.is_pending(it)
    # a pending offset over part of the pedal range
    shift.op_box.setCurrentIndex(shift.op_box.findData("offset"))
    shift.amount.setValue(150)
    shift.p_from.setValue(40.0)
    shift.p_to.setValue(70.0)
    shift._recompute()
    changed = {(row, r) for row, i in enumerate(shift._selected())
               for r in range(shift.sset.schedules[i].rows)
               if shift.result.grid[i][r * shift.sset.schedules[i].cols + shift._line()]
               != shift.edit.origin[i][r * shift.sset.schedules[i].cols + shift._line()]}
    assert changed and len(changed) < len(_cells(pt))
    cdiff = set(_differing(ct))
    assert changed & cdiff                   # the precedence case is there
    for r, c in _cells(pt):
        it, cit = pt.item(r, c), ct.item(r, c)
        bg, fg = it.background(), it.foreground()
        if (r, c) in changed:                # held change: wins
            assert GC.is_pending(it) and it.font().bold()
            assert bg.color() == GC.PENDING_BG and fg.color() == _g.CELL_DARK_FG
            assert "not applied yet" in it.toolTip()
        elif (r, c) in cdiff:                # differs only
            assert bg.color() == _g.DIFF_BG and fg.color() == _g.CELL_DARK_FG
            assert not it.font().bold()
        else:                                # neither: the view's own colours
            assert bg.style() == QtCore.Qt.NoBrush
            assert fg.style() == QtCore.Qt.NoBrush
        # the comparison copy: orange where it differs from the preview,
        # nothing else ever
        if (r, c) in cdiff:
            assert cit.background().color() == _g.DIFF_BG
            assert cit.foreground().color() == _g.CELL_DARK_FG
        else:
            assert cit.background().style() == QtCore.Qt.NoBrush
        assert not cit.font().bold()
    # both tables shade rows like the main editor's grid -- in the Windows
    # 11 style that also draws the selection over the cell fills
    assert pt.alternatingRowColors() and ct.alternatingRowColors()


def test_cell_colours_in_the_lockup_editor(tcc):
    from ovtsuite import gui as _g
    from ovtsuite import gui_gearbox_compare as GC
    tcc._check_all(True)
    tcc._recompute()
    pt, ct = tcc.table, tcc.cmpv.cmp_table
    diff = _differing(ct)
    assert diff and _differing(pt) == diff
    for t in (pt, ct):
        assert t.alternatingRowColors()
        for r, c in diff:
            assert t.item(r, c).background().color() == _g.DIFF_BG
            assert t.item(r, c).foreground().color() == _g.CELL_DARK_FG
    tcc.op_box.setCurrentIndex(tcc.op_box.findData("offset"))
    tcc.amount.setValue(50)
    tcc._recompute()
    pend = [(r, c) for r, c in _cells(pt) if GC.is_pending(pt.item(r, c))]
    assert pend
    assert all(pt.item(r, c).background().color() == GC.PENDING_BG
               and pt.item(r, c).font().bold() for r, c in pend)
    assert all(ct.item(r, c).background().style() in (QtCore.Qt.NoBrush,)
               or ct.item(r, c).background().color() == _g.DIFF_BG
               for r, c in _cells(ct))


def _pump():
    for _ in range(3):
        QtWidgets.QApplication.processEvents()


def _edge(t, orient, last):
    """The first / last row (column) ``t`` shows."""
    if orient == QtCore.Qt.Vertical:
        h, n = t.verticalHeader(), t.viewport().height()
    else:
        h, n = t.horizontalHeader(), t.viewport().width()
    i = h.logicalIndexAt(n - 1 if last else 0)
    return i if i >= 0 else h.count() - 1


@pytest.mark.parametrize("orient", ["v", "h"])
def test_comparison_scrolls_to_the_end_with_the_table_of_another_size(shift, orient):
    """The value table and the comparison under it differ in size, so they
    scroll different distances: scrolled to its end, either one takes the
    other to its end (both show the last row / column); at the start both
    are at the start; in between both keep the same top row; resizing the
    comparison while the table is at its end keeps it at its end."""
    pt, ct = shift.table, shift.cmpv.cmp_table
    o = QtCore.Qt.Vertical if orient == "v" else QtCore.Qt.Horizontal
    bar = (lambda t: t.verticalScrollBar() if o == QtCore.Qt.Vertical
           else t.horizontalScrollBar())
    shift.show()
    _pump()
    if o == QtCore.Qt.Vertical:
        pt.setFixedHeight(320)
        ct.setFixedHeight(130)
    else:
        for k in range(pt.columnCount()):
            pt.horizontalHeader().resizeSection(k, 120)
        pt.setFixedWidth(700)
        ct.setFixedWidth(330)
    _pump()
    n = (pt.rowCount() if o == QtCore.Qt.Vertical else pt.columnCount()) - 1
    assert 0 < bar(pt).maximum() != bar(ct).maximum() > 0
    for src, dst in ((pt, ct), (ct, pt)):
        bar(src).setValue(bar(src).maximum())
        _pump()
        assert bar(dst).value() == bar(dst).maximum()
        assert _edge(dst, o, True) == _edge(src, o, True) == n
        bar(src).setValue(0)
        _pump()
        assert bar(dst).value() == 0 and _edge(dst, o, False) == 0
        mid = min(bar(pt).maximum(), bar(ct).maximum()) // 2
        bar(src).setValue(mid)
        _pump()
        assert _edge(dst, o, False) == _edge(src, o, False)
    bar(pt).setValue(bar(pt).maximum())
    _pump()
    if o == QtCore.Qt.Vertical:
        ct.setFixedHeight(100)
    else:
        ct.setFixedWidth(260)
    _pump()
    assert _edge(ct, o, True) == n


# ------------------------------------------------------- the dialog: copying
def test_copy_selected_cells_is_a_step_and_one_undo(shift):
    cv, pt = shift.cmpv, shift.table
    s = shift.session
    before = bytes(s.binfile.data)
    shift.range_edit.setText("10.0, 11.0")
    shift._select_range(False)
    shift._recompute()
    diff = _differing(pt)
    assert len(diff) >= 2
    pick = diff[:2]
    for r, c in pick:
        pt.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(r, c, r, c), True)
    menu = cv.table_menu(pt)
    acts = {a.text(): a for a in menu.actions()}
    act = acts["Copy selected cells from comparison"]
    assert act.isEnabled()
    act.trigger()
    assert len(shift.steps) == 1 and shift.steps_list.count() == 1
    assert "copy 2 cells from comparison" in shift.steps_list.item(0).text()
    shift._recompute()
    # no longer differing: the comparison copy unshaded, the primary's cell
    # shaded as edited (the preview) with no comparison value in its tooltip
    assert all((r, c) not in _differing(cv.cmp_table) for r, c in pick)
    assert all("Comparison:" not in pt.item(r, c).toolTip() for r, c in pick)
    assert shift.result.cells == 2
    n = shift.edit.apply(s, "test", shift.result)
    assert n == 2
    s.undo()
    assert bytes(s.binfile.data) == before


def _line_differs(d, i, line):
    s, cv = d.sset.schedules[i], d.cmpv
    cmp_vals = cv.values(cv.pairing.pairs[i])
    return any(d.result.grid[i][r * s.cols + line] != cmp_vals[r * s.cols + line]
               for r in range(s.rows))


def test_copy_whole_line_from_table_and_chart(tcc):
    cv = tcc.cmpv
    pr = cv.pairing
    tcc._check_all(True)
    tcc._recompute()
    sel = tcc._selected()
    i = None
    for ln in tcc.sset.lines:               # a characteristic and line that differ
        tcc.line_group.button(ln.index).click()
        tcc._recompute()
        line = tcc._line()
        i = next((i for i in sel if _line_differs(tcc, i, line)), None)
        if i is not None:
            break
    assert i is not None
    key = tcc.sset.schedules[i].key
    menu = cv.chart_menu(key)
    act = menu.actions()[0]
    assert act.isEnabled() and key in act.text()
    act.trigger()
    assert len(tcc.steps) == 1 and "copy whole" in tcc.steps_list.item(0).text()
    tcc._recompute()
    s, j = tcc.sset.schedules[i], pr.pairs[i]
    assert [tcc.result.grid[i][r * s.cols + line] for r in range(s.rows)] == \
        [cv.values(j)[r * s.cols + line] for r in range(s.rows)]
    # the same line again: nothing left to copy, the menu says so
    again = cv.chart_menu(key).actions()[0]
    assert not again.isEnabled() and "no difference" in again.text()
    # and from the table: the whole line of the selected row
    row = tcc._selected().index(i)
    tcc.table.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(row, 0, row, 0), True)
    acts = {a.text().split(" (")[0]: a for a in cv.table_menu(tcc.table).actions()}
    assert not acts["Copy whole line from comparison"].isEnabled()
    tcc.steps_list.setCurrentRow(0)
    tcc._remove_step()
    assert not tcc.steps


# ------------------------------------------------- no / unusable comparison
def test_no_comparison_adds_nothing(pair):
    d = _dialog(pair[0], None)
    try:
        assert d.cmpv.chk is None and d.cmpv.box is None
        assert d.table.contextMenuPolicy() != QtCore.Qt.CustomContextMenu
        assert not any(s.get("cmp") for s in d.chart.series)
    finally:
        d.close()


def test_comparison_without_a_matching_set_says_so(pair):
    other = SimpleNamespace(platform="edc16", binfile=SimpleNamespace(name="other.bin"),
                            revision=0)
    d = _dialog(pair[0], other)
    try:
        cv = d.cmpv
        assert cv.chk is not None and not cv.chk.isEnabled() and not cv.chk.isChecked()
        assert "no shift schedules" in cv.lbl.text() and "other.bin" in cv.lbl.text()
        assert cv.box is None and not any(s.get("cmp") for s in d.chart.series)
    finally:
        d.close()


@pytest.mark.parametrize("kind", ["shift", "tcc"])
def test_selected_shaded_cells_keep_readable_text(pair, kind):
    """A shaded cell's dark text would vanish on the solid selection: both
    tables draw a selected cell's text in the selected-text colour."""
    from ovtsuite.gui_cellmarks import CellDelegate
    d = _dialog(*pair, kind=kind)
    for t in (d.table, d.cmpv.cmp_table):
        assert isinstance(t.itemDelegate(), CellDelegate)
    d.close()

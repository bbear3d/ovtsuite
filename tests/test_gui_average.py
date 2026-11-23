# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Average with the comparison (ovtsuite/gui_average.py): the Modify selection bar's
*Average with comparison %*, the Edit > Cells actions and the right-click entry for
comparison cells. The number is the comparison's weight in percent; it is offered only while
the comparison is on the primary's axes.

Offscreen, on the 03G906016 LQ (primary) and KN (comparison) calibrations."""
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
RESAMPLED = "AccPed_dnDOSetP_MAP"      # 6x6 over a 5x6: values resampled
MISMATCH = "AccPed_trqEngLim_MAP"      # same 7x6, different breakpoints


def test_the_blend_and_the_weight_range():
    from ovtsuite import gui_average as ga
    assert ga.blend(10.0, 20.0, 0) == 10.0 and ga.blend(10.0, 20.0, 100) == 20.0
    assert ga.blend(10.0, 20.0, 50) == 15.0 and ga.blend(10.0, 20.0, 25) == 12.5
    assert ga.valid_weight(0) and ga.valid_weight(100) and ga.valid_weight(33.3)
    assert not ga.valid_weight(-1) and not ga.valid_weight(100.5)


@pytest.fixture(scope="module")
def win(tmp_path_factory, isolated_qsettings_module):
    for p in (LQ, KN):
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope,
                             str(tmp_path_factory.mktemp("settings")))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    w = gui.MainWindow(TunerSession(LQ), TunerSession(KN))
    w.resize(1200, 760)
    w.show()
    app.processEvents()
    yield w
    w.close_discarding()


def _show(win, name, size=None):
    from ovtsuite import gui_cmpsize as cs
    ws = win.current
    cs.set_size(ws, size)
    e = next(e for e in ws.primary.entities if e.name == name)
    ws.show_entity(e)
    QtWidgets.QApplication.processEvents()
    return ws, e


def _op(ws, op, text):
    i = ws.op_box.findData(op)
    ws.op_box.setCurrentIndex(i)
    ws.op_val.setText(text)
    ws._apply_op()


def _avg_item(ws):
    return ws.op_box.model().item(ws.op_box.findData("avg"))


def test_it_is_off_while_the_comparison_is_the_same_size_on_other_axes(win):
    from ovtsuite import gui_average as ga
    ws, _e = _show(win, MISMATCH)
    assert ws.model.overlay.mode == "axis-mismatch"
    ok, why = ga.state(ws)
    assert not ok and "Aligned" in why and "operating point" in why
    assert not _avg_item(ws).isEnabled()
    assert "operating point" in ws.op_box.itemData(ws.op_box.findData("avg"),
                                                    QtCore.Qt.ToolTipRole)
    before = ws.model.entity.values()
    _op(ws, "avg", "50")
    assert ws.model.entity.values() == before          # nothing was written
    assert "operating point" in ws.statusBar().currentMessage()
    # the Edit > Cells actions are greyed too
    ga.refresh_actions(win)
    acts = [a for a in win._cell_actions if a.text().startswith("Average with comparison")]
    assert len(acts) == 2 and not any(a.isEnabled() for a in acts)


def test_aligned_turns_it_on_and_averages_at_the_same_operating_point(win):
    from ovtsuite import gui_average as ga
    ws, e = _show(win, MISMATCH, "aligned")
    try:
        ok, why = ga.state(ws)
        assert ok and why == "" and _avg_item(ws).isEnabled()
        m = ws.model
        old, cmp_ = e.values(), [list(r) for r in m._cmp]
        cells = [(r, c) for r in range(e.rows) for c in range(e.cols)]
        want = [(r, c, ga.blend(old[r][c], cmp_[r][c], 25)) for r, c in cells
                if abs(old[r][c] - cmp_[r][c]) > 1e-6]
        assert want, "the two calibrations are expected to differ here"
        n = ga.run(ws, cells, 25.0)
        assert n == len(want)
        now = e.values()
        step = abs(e.eq.decode(1) - e.eq.decode(0)) if hasattr(e, "eq") else 1.0
        for r, c, v in want:
            assert abs(now[r][c] - v) <= step, (r, c, now[r][c], v)
        ws.undo()
        assert e.values() == old                       # one undo step
    finally:
        _show(win, MISMATCH, None)


def test_the_bar_takes_a_weight_and_keeps_a_number_per_operation(win):
    ws, e = _show(win, RESAMPLED)                     # a different size: on the primary's axes
    assert ws.model.overlay.mode == "resampled" and _avg_item(ws).isEnabled()
    ws.op_box.setCurrentIndex(ws.op_box.findData("mul"))
    ws.op_val.setText("1.2")
    ws.op_box.setCurrentIndex(ws.op_box.findData("avg"))
    assert ws.op_val.text() == "50"                   # the weight begins at 50
    ws.op_val.setText("30")
    ws.op_box.setCurrentIndex(ws.op_box.findData("mul"))
    assert ws.op_val.text() == "1.2"
    ws.op_box.setCurrentIndex(ws.op_box.findData("avg"))
    assert ws.op_val.text() == "30"
    old = e.values()
    _op(ws, "avg", "0")
    assert e.values() == old                          # weight 0 keeps the cells
    _op(ws, "avg", "150")
    assert e.values() == old
    assert "0 to 100" in ws.statusBar().currentMessage()
    _op(ws, "avg", "100")                             # the comparison's own values
    cmp_ = ws.model._cmp
    step = abs(e.eq.decode(1) - e.eq.decode(0))
    for r in range(e.rows):
        for c in range(e.cols):
            if cmp_[r][c] is not None:
                assert abs(e.cell(r, c) - cmp_[r][c]) <= step
    ws.undo()
    assert e.values() == old


def test_it_is_off_at_the_comparisons_original_size(win):
    from ovtsuite import gui_average as ga
    ws, _e = _show(win, RESAMPLED, "original")
    try:
        ok, why = ga.state(ws)
        assert not ok and "original size" in why
        assert not _avg_item(ws).isEnabled()
    finally:
        _show(win, RESAMPLED, None)


def test_comparison_cells_can_be_averaged_into_the_primary(win):
    from ovtsuite import gui_average as ga, gui_cellkeys as K
    ws, e = _show(win, RESAMPLED)
    old = e.values()
    # select the comparison's cells (rows under the banner) and average them in
    m = ws.model
    sep = m.sep_row()
    sm = ws.table.selectionModel()
    sm.select(QtCore.QItemSelection(m.index(sep + 2, 1),
                                    m.index(m.rowCount() - 1, m.columnCount() - 1)),
              QtCore.QItemSelectionModel.ClearAndSelect)
    assert K.comparison_cells(ws, ws.table)
    n = ga.to_primary(ws, ws.table, 50.0)
    assert n > 0 and e.values() != old
    ws.undo()
    assert e.values() == old


def test_hex_output_tables_can_be_averaged_but_text_and_choices_cannot():
    """Hex tables are bytes, averaged byte for byte; text and lists of choices are not numbers."""
    from types import SimpleNamespace as NS
    from ovtsuite import gui_average as ga

    def ws(**ent):
        e = NS(text_kind="", options=None, **ent)
        m = NS(entity=e, _cmp=[[1.0]], compare=object(), outline_all=False,
               overlay=NS(mode="direct"))
        return NS(model=m)
    assert ga.state(ws())[0]
    e = ws()
    e.model.entity.text_kind = "hex"
    assert ga.state(e)[0]
    e.model.entity.text_kind = "ascii"
    ok, why = ga.state(e)
    assert not ok and "text" in why
    e.model.entity.text_kind = ""
    e.model.entity.options = {0: "off", 1: "on"}
    assert not ga.state(e)[0]


def test_a_delta_view_primary_blocks_every_edit_and_says_only_that(win):
    """Scale / set / average on a primary showing differences write nothing and
    leave the blocked message (not 'Scaled N cell(s)' / 'Averaged ...')."""
    from ovtsuite import gui_average as ga, gui_cellkeys as K
    ws, e = _show(win, RESAMPLED)
    m = ws.model
    before = e.values()
    saved = m._delta
    try:
        m.set_delta("abs", primary="d1")
        assert m.primary_delta() == "d1"
        ok, why = ga.state(ws)
        assert not ok and "differences" in why
        assert not _avg_item(ws).isEnabled()
        idxs = [m.index(r, c) for r in range(m.rowCount())
                for c in range(m.columnCount()) if m.z_at(m.index(r, c))]
        assert m.apply_op(idxs, "mul", 2.0) == 0
        assert m._edit_cells([(0, 0, 1.0)]) is False
        ws.statusBar().clearMessage()
        _op(ws, "mul", "2")
        assert "Scaled" not in ws.statusBar().currentMessage()
        assert "applied" not in ws.statusBar().currentMessage()
        cells = [(r, c) for r in range(e.rows) for c in range(e.cols)]
        assert ga.run(ws, cells, 50.0) == 0
        assert K._write(ws, [(0, 0, 1.0)], "Pasted") == 0
        assert "Pasted" not in ws.statusBar().currentMessage()
        assert e.values() == before
    finally:
        m.set_delta("abs", primary="abs")
        m._delta = saved
        _show(win, RESAMPLED)

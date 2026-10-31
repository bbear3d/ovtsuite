# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Editing in the End of injection window like the table editor
(ovtsuite/gui_eoi_edit.py, gui_eoi_surface.py), on the synthetic injection
model of test_gui_eoi_compare: Modify selection, the Edit > Cells actions,
Reset conditions, the Heatmap, the Difference view and the 3-D surface."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from test_gui_eoi_compare import app, dlg  # noqa: E402,F401  (the fixtures)
from ovtsuite import gui_eoi_edit as E  # noqa: E402

pytestmark = pytest.mark.slow


def _select(d, r0, c0, r1, c1):
    sm, m = d.grid.selectionModel(), d.grid.model()
    sm.select(QtCore.QItemSelection(m.index(r0, c0), m.index(r1, c1)),
              QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    sm.setCurrentIndex(m.index(r0, c0), QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)


# ------------------------------------------------------- the arithmetic
def test_the_operations_are_the_table_editors():
    assert E.apply_op("set", 3, 5) == 5
    assert E.apply_op("add", 3, 5) == 8
    assert E.apply_op("sub", 3, 5) == -2
    assert E.apply_op("mul", 3, 5) == 15
    assert E.apply_op("div", 10, 4) == 2.5
    assert E.apply_op("div", 10, 0) == 10             # dividing by 0 leaves it


def test_fill_keeps_the_edges_and_smooth_reads_the_old_values():
    v = {(r, c): float(c * 10) for r in range(2) for c in range(3)}
    v[(0, 1)] = 99.0
    got = E.interpolated(v, list(v), "h")
    assert got == {(0, 1): 10.0, (1, 1): 10.0}
    corners = {(0, 0): 0.0, (0, 2): 4.0, (2, 0): 8.0, (2, 2): 12.0}
    allc = {(r, c): 0.0 for r in range(3) for c in range(3)}
    allc.update(corners)
    blk = E.interpolated(allc, list(allc), "2d")
    assert blk[(1, 1)] == 6.0 and blk[(0, 1)] == 2.0 and blk[(1, 0)] == 4.0
    vals = {(0, 0): 0.0, (0, 1): 6.0, (0, 2): 0.0}
    assert E.smoothed(vals, [(0, 1)]) == {(0, 1): 2.0}


def test_ctrl_arrow_goes_to_the_edge_of_the_data_as_in_excel():
    row = [1, 1, None, 1, 1]
    blank = lambda r, c: row[c] is None                         # noqa: E731
    assert E.edge(blank, 1, 5, 0, 0, 0, 1) == (0, 1)            # end of the run
    assert E.edge(blank, 1, 5, 0, 1, 0, 1) == (0, 3)            # across the gap


# ------------------------------------------------------ Modify selection
def test_modify_selection_is_one_undo_step_on_what_the_grid_shows(dlg):
    prim = dlg.session
    steps = prim.undo_steps
    _select(dlg, 0, 0, 1, 1)
    before = dict(dlg._prim_vals)                      # EOI = duration - SOI
    dlg.edit.op_box.setCurrentIndex(1)                 # Add +
    dlg.edit.op_val.setText("1.5")
    dlg.edit.apply_btn.click()
    assert prim.undo_steps == steps + 1
    for rc in ((0, 0), (0, 1), (1, 0), (1, 1)):
        assert dlg._prim_vals[rc] == pytest.approx(before[rc] + 1.5, abs=2e-3)
    assert dlg._prim_vals[(2, 2)] == pytest.approx(before[(2, 2)], abs=2e-3)
    assert len(dlg._selected_cells()) == 4              # still selected


def test_nothing_selected_applies_to_the_whole_table(dlg):
    dlg.grid.clearSelection()
    dlg.grid.setCurrentCell(-1, -1)
    before = dict(dlg._prim_vals)
    dlg.edit.op_box.setCurrentIndex(1)
    dlg.edit.op_val.setText("1")
    dlg.edit.apply_btn.click()
    assert all(dlg._prim_vals[rc] == pytest.approx(before[rc] + 1, abs=2e-3) for rc in before)


def test_table_no_is_read_only_and_says_so(dlg):
    dlg.mode_group.button(3).click()
    assert not dlg.edit.apply_btn.isEnabled()
    n = dlg.session.undo_steps
    dlg.edit.step(1)
    assert dlg.session.undo_steps == n and "read-only" in dlg.status.text()


def test_the_cell_actions_use_the_shown_views_step(dlg):
    _select(dlg, 1, 1, 1, 1)
    e0 = dlg._prim_vals[(1, 1)]
    assert dlg.edit.cell_action("Step up")
    assert dlg._prim_vals[(1, 1)] == pytest.approx(e0 + E.STEPS["eoi"], abs=2e-3)
    assert dlg.edit.cell_action("Step down x10")
    assert dlg._prim_vals[(1, 1)] == pytest.approx(e0 - 9 * E.STEPS["eoi"], abs=2e-3)
    assert dlg.edit.cell_action("Increase 1%")
    assert not dlg.edit.cell_action("Not an action")


def test_a_duration_edit_asks_first_and_steps_by_a_tenth(dlg):
    dlg.mode_group.button(2).click()                   # Duration
    dlg._asked_quantity = True                         # (answered yes)
    _select(dlg, 0, 0, 0, 0)
    d0 = dlg._prim_vals[(0, 0)]
    dlg.edit.cell_action("Step up")
    assert dlg._prim_vals[(0, 0)] == pytest.approx(d0 + 0.1, abs=2e-3)


def test_revert_says_so_when_there_is_no_image_to_go_back_to(dlg):
    _select(dlg, 0, 0, 0, 0)
    dlg.edit.cell_action("Revert to as-read values")
    assert dlg.status.text()                           # never silent


def test_copy_from_the_comparison_through_the_shortcut_action(dlg):
    _select(dlg, 0, 1, 0, 1)                           # differs: SOI 2 earlier
    assert dlg._prim_vals[(0, 1)] != pytest.approx(dlg._cmp_vals[(0, 1)], abs=2e-3)
    dlg.edit.cell_action("Copy selected cells from comparison")
    assert dlg._prim_vals[(0, 1)] == pytest.approx(dlg._cmp_vals[(0, 1)], abs=2e-3)


# ------------------------------------------------------ Reset conditions
def test_reset_conditions_goes_back_to_the_defaults(dlg):
    assert not dlg.edit.reset.isEnabled()              # nothing to reset
    dlg.coolant.setValue(60)
    assert dlg.edit.reset.isEnabled()
    for key, (_l, sp) in dlg.cond.items():
        sp.setValue(sp.maximum() - 1)
    dlg.edit.reset.click()
    assert dlg.coolant.value() == dlg.model.coolant_warm
    assert dlg.soi_box.currentIndex() == 0
    from ovtsuite import eoi as _eoi
    for key, (_l, sp) in dlg.cond.items():
        label, units, dflt, lo, hi = _eoi.CONDITIONS[key]
        assert sp.value() == (lo - 1.0 if dflt is None else dflt), key
    assert not dlg.edit.reset.isEnabled()
    assert dlg.conditions()["fuel"] is None             # Reference


# ----------------------------------------------------------- the Heatmap
def test_heatmap_off_is_plain_on_is_the_table_editors_ramp(dlg):
    from ovtsuite.gui_eoi_edit import heat_color
    assert dlg.grid.item(0, 0).background().style() == QtCore.Qt.BrushStyle.NoBrush
    dlg.edit.heat.setChecked(True)
    vals = list(dlg._prim_vals.values()) + list(dlg._cmp_vals.values())
    lo, hi = min(vals), max(vals)
    for rc, v in dlg._prim_vals.items():
        want = heat_color((v - lo) / (hi - lo))
        assert dlg.grid.item(*rc).background().color() == want
    dlg.edit.heat.setChecked(False)
    assert dlg.grid.item(0, 0).background().style() == QtCore.Qt.BrushStyle.NoBrush


# ------------------------------------------------------- Difference view
def test_the_comparison_grid_can_show_the_difference(dlg, monkeypatch):
    monkeypatch.setattr(dlg, "diff_state", lambda: ("d1", "abs", False, True))
    dlg.rebuild()
    g = dlg.cmpv.grid
    # this file - comparison: the comparison's SOI at (0, 1) is 2 lower -> its
    # EOI 2 later, so this file's is 2 earlier
    assert g.item(0, 1).text() == "-2.00"
    assert g.item(0, 0).text() == "0"
    monkeypatch.setattr(dlg, "diff_state", lambda: ("d1", "abs", True, False))
    dlg.rebuild()
    assert g.item(0, 0).text() == ""                    # blank unchanged cells


def test_the_primary_grid_showing_differences_is_read_only(dlg, monkeypatch):
    monkeypatch.setattr(dlg, "diff_state", lambda: ("abs", "d1", False, False))
    dlg.rebuild()
    assert dlg.shows_difference() and not dlg._can_edit()
    assert not dlg.edit.apply_btn.isEnabled()
    n = dlg.session.undo_steps
    _select(dlg, 0, 0, 0, 0)
    dlg.edit.step(1)
    assert dlg.session.undo_steps == n and "read only" in dlg.status.text()
    assert not (dlg.grid.item(0, 0).flags() & QtCore.Qt.ItemFlag.ItemIsEditable)


# ------------------------------------------------------- the 3-D surface
def test_the_surface_follows_the_grid_and_edits_through_it(dlg, app):
    assert not dlg.surf.shown and not dlg.surf.pbox.isVisibleTo(dlg)
    dlg.surf_btn.setChecked(True)
    app.processEvents()
    s = dlg.surf
    assert s.shown and s.pbox.isVisibleTo(dlg) and s.bar.isVisibleTo(dlg)
    assert (s.view.rows, s.view.cols) == (3, 3)
    assert s.cbox.isVisibleTo(dlg) and s.cmp_view.rows == 3     # beside the comparison
    # each surface sits in the same row as its grid
    assert dlg.row1.layout().indexOf(s.pbox) == 0 and dlg.row2.layout().indexOf(s.cbox) == 0
    ed = s.view.editor
    ed.select_one(1, 2)                                 # a point picked = a cell selected
    assert dlg._selected_cells() == [(1, 2)]
    e0 = dlg._prim_vals[(1, 2)]
    steps = dlg.session.undo_steps
    assert ed.drag_commit(1.0) == 1                     # Ctrl+drag up, snapped to 0.5
    assert dlg.session.undo_steps == steps + 1
    assert dlg._prim_vals[(1, 2)] == pytest.approx(e0 + 1.0, abs=2e-3)
    assert s.view.vals[1][2] == pytest.approx(e0 + 1.0, abs=2e-3)         # and it redraws
    dlg.surf_btn.setChecked(False)
    assert not s.pbox.isVisibleTo(dlg) and not s.cbox.isVisibleTo(dlg)
    assert s.view.rows == 3                             # (kept; redrawn when shown)


def test_the_surface_cannot_edit_a_read_only_view(dlg, monkeypatch):
    dlg.mode_group.button(3).click()                    # Table no.
    assert not dlg.surf.view.editor.can_edit()


# ---------------------------------------------------------- window size
def test_the_window_opens_at_the_least_its_layout_allows(dlg):
    need = dlg.layout().totalMinimumSize()
    assert dlg.width() <= need.width() + 2 and dlg.width() >= need.width()
    assert dlg.height() >= need.height()


def test_hiding_the_comparison_gives_its_room_to_the_primary_grid(dlg, app):
    assert dlg.row2.isVisibleTo(dlg)
    dlg.cmpv.check.setChecked(False)
    app.processEvents()
    assert not dlg.row2.isVisibleTo(dlg)                # no empty row below
    assert dlg.row1.height() >= dlg.split.height() - 2  # the primary fills it
    dlg.cmpv.check.setChecked(True)
    app.processEvents()
    assert dlg.row2.isVisibleTo(dlg) and dlg.row1.height() < dlg.split.height()

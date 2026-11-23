# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Relative patch entries in the GUI (ovtsuite.gui_relpatch): the Generate
patch window's Carry as (values / change by difference / by percentage) and
the Apply patch dialog's Change and Copies columns, its preview and its
question before a stacking patch goes on a second time.

Slow: each case loads full TunerSessions."""
import os

import pytest

from ovtsuite import patch as P
from ovtsuite import patch_relative as R
from ovtsuite import gui_relpatch as GR

pytestmark = pytest.mark.slow

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
PRIMARY, COMPARISON = "038906016AB.bin", "038997016R_remap.bin"


def _session(name):
    path = os.path.join(BIN, name)
    if not os.path.exists(path):
        pytest.skip("reference binary %s not present" % name)
    from ovtsuite.session import TunerSession
    return TunerSession(path)


@pytest.fixture(scope="module")
def a():
    return _session(PRIMARY)


@pytest.fixture(scope="module")
def b():
    return _session(COMPARISON)


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _found_map(d):
    for i, pe in enumerate(d._entries):
        if pe.kind == "map" and not pe.whole and pe.changed_cells \
                and pe.rows > 1 and pe.cols > 1:
            a = P.find_entity(d.primary, pe.name, pe.coding)
            if a is not None and (a.rows, a.cols) == (pe.rows, pe.cols):
                return i, pe
    pytest.skip("no same-shape map found changed between the two files")


def test_carry_as_change_and_back_to_values(app, a, b):
    from ovtsuite import gui_differences as GD
    d = GD.DifferencesWindow(a, b)
    try:
        i, pe = _found_map(d)
        done, refused = GR.carry_rows(d, [i], GR.CHANGE_ABS)
        rel = d._entries[i]
        assert done == 1 and not refused and rel.relative
        assert GR.carried_as(rel) == GR.CHANGE_ABS
        d._fill_row(i)
        row = [r for r, (k, j) in enumerate(d._rows) if k == "entry" and j == i][0]
        assert "change" in d.tbl.item(row, GD.C_CHANGED).text()
        GR.carry_rows(d, [i], GR.CHANGE_PCT)
        assert GR.carried_as(d._entries[i]) == GR.CHANGE_PCT
        GR.carry_rows(d, [i], GR.VALUES)
        assert d._entries[i] is pe                       # the same value entry
        assert d.carry_btn is not None
    finally:
        d.close()


@pytest.mark.parametrize("how", [GR.CHANGE_ABS, GR.CHANGE_PCT])
def test_a_change_applied_to_the_primary_gives_the_comparisons_table(app, a, b, how):
    from ovtsuite import gui_differences as GD
    d = GD.DifferencesWindow(a, b)
    try:
        i, pe = _found_map(d)
        GR.carry_rows(d, [i], how)
        rel = d._entries[i]
    finally:
        d.close()
    want = P.find_entity(b, pe.name, pe.coding).values()
    target = P.find_entity(a, pe.name, pe.coding)
    res = P.apply_patch(P.Patch(entries=[rel]), a, fix_checksum=False)
    try:
        assert res.applied == [pe.name]
        got = target.values()
        for r in range(target.rows):
            for c in range(target.cols):
                assert got[r][c] == pytest.approx(want[r][c], rel=1e-3, abs=0.02)
    finally:
        a.undo()


def test_the_apply_dialog_shows_the_change_copies_and_a_preview(app, a):
    from ovtsuite import gui
    base = "InjCrv_phiBas8_GMAP"
    rel = R.relative_entry(base, [{"op": "add", "value": 1.0, "y": [55, None]}])
    pt = P.Patch(entries=[rel], comparison_name="t")
    dlg = gui.PatchApplyDialog(a, pt, "timing.patch")
    try:
        assert "add +1" in dlg.tbl.item(0, 5).text()
        copies = len(GR.tables_here(rel, a))
        assert dlg.tbl.item(0, 6).text() == str(copies) and copies >= 1
        dlg._show_preview(0)
        text = dlg.preview.toPlainText()
        assert base in text and "cell(s) would change" in text
    finally:
        dlg.close()


def test_a_stacking_patch_asks_before_going_on_twice(app, a):
    rel = R.relative_entry("InjCrv_phiBas8_GMAP", [{"op": "add", "value": 1.0}])
    cap = R.relative_entry("InjCrv_phiBas8_GMAP", [{"op": "cap", "value": 99}])
    pt = P.Patch(entries=[rel])
    assert GR.reapply_warning(pt, a, "stack.patch") == ""     # never applied
    P.apply_patch(pt, a, source_label="stack.patch", fix_checksum=False)
    try:
        assert "already applied" in GR.reapply_warning(pt, a, "stack.patch")
        assert GR.reapply_warning(P.Patch(entries=[cap]), a, "stack.patch") == ""
    finally:
        a.undo()
        a.change_log[:] = [l for l in a.change_log if "stack.patch" not in l]

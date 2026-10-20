# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The shift map editor window (ovtsuite.gui_gearbox_edit), offscreen, on a
real 6HP19 image: select, set up a step, preview, apply as one undo step."""
import os

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtWidgets  # noqa: E402

from conftest import ZF6HP_REFERENCE as SIXHP  # noqa: E402


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def dlg(app):
    path = os.path.join(SIXHP, "3.0TFSI-Stock.bin")
    if not os.path.exists(path):
        pytest.skip("no ZF 6HP reference binaries")
    from ovtsuite.session import TunerSession
    from ovtsuite import gui_gearbox_edit as G
    s = TunerSession(path)

    class WS(QtWidgets.QWidget):
        pass
    ws = WS()
    ws.primary, ws.model, ws.project_ref = s, None, None
    d = G.GearboxEditDialog(ws, G.schedule_set_for(s))
    d._recompute()
    return d


def test_provider_and_presets(dlg):
    assert len(dlg.sset.schedules) == 82
    assert "All schedules" in dlg.sset.presets
    assert any(n.startswith("Coding 1: D") for n in dlg.sset.presets)


def test_range_selection(dlg):
    dlg.range_edit.setText("10-12")
    dlg._select_range(False)
    keys = [s.key for s in dlg._selected_schedules()]
    assert keys == ["SKL 10.0", "SKL 11.0", "SKL 12.0", "SKL 10.1", "SKL 11.1", "SKL 12.1"] \
        or sorted(keys) == sorted(["SKL 10.0", "SKL 11.0", "SKL 12.0",
                                   "SKL 10.1", "SKL 11.1", "SKL 12.1"])


def test_step_previews_and_applies_as_one_undo(dlg):
    from ovtsuite import gui_gearbox_edit as G
    s = dlg.session
    before = bytes(s.binfile.data)
    dlg.range_edit.setText("10.0, 11.0")
    dlg._select_range(False)
    dlg.line_group.button(8).click()                  # 5>4
    dlg.op_box.setCurrentIndex([k for k, _ in G.OPS].index("offset"))
    dlg.amount.setValue(100)                           # +100 engine rpm
    dlg._recompute()
    res = dlg.result
    assert res.cells == 2 * 12
    ch = res.changes[0]
    assert round((ch.new - ch.old) * dlg.sset.line(8).ratio_left) in (99, 100, 101)
    n = dlg.edit.apply(s, "test", res)
    assert n == res.cells
    s.undo()
    assert bytes(s.binfile.data) == before


def test_saved_set_without_a_project(dlg):
    dlg.range_edit.setText("27")
    dlg._select_range(False)
    QtWidgets.QInputDialog.getText = staticmethod(lambda *a, **k: ("manual", True))
    dlg._save_set()
    assert dlg._saved_sets()["manual"] == ["SKL 27.0", "SKL 27.1"]
    assert dlg.preset.findText("Saved: manual") >= 0


def test_tools_menu_opens_the_editor(app, tmp_path):
    """Tools > ZF 6HP > Shift map editor... on a real main window."""
    path = os.path.join(SIXHP, "3.0TFSI-Stock.bin")
    if not os.path.exists(path):
        pytest.skip("no ZF 6HP reference binaries")
    from ovtsuite import gui as G
    from ovtsuite.session import TunerSession
    w = G.MainWindow(TunerSession(path), None)
    try:
        act = next(a for a in w.zf6hp_menu.actions() if a.text() == "Shift map editor...")
        act.trigger()
        dlg = w.current.tool("shiftedit")
        assert dlg is not None and len(dlg.sset.schedules) == 82
        dlg.close()
    finally:
        w.close_discarding()


# ---------------------------------------------------------------- lock-up mode
TUNE = os.path.join(os.path.dirname(__file__), os.pardir, "Reference", "Tunes",
                    "2010-A6-BHW-6HP-19A.ovt")


@pytest.fixture
def tcc(app):
    if not os.path.exists(TUNE):
        pytest.skip("Bear's 6HP19 project is not in Reference/Tunes")
    from ovtsuite import project as ovt
    from ovtsuite import gui_gearbox_edit as G
    from test_zf6hp_shiftedit import _checkpoint_bytes, _session
    s = _session(_checkpoint_bytes(ovt.load(TUNE), "96bee7a881"))

    class WS(QtWidgets.QWidget):
        pass
    ws = WS()
    ws.primary, ws.model, ws.project_ref = s, None, None
    d = G.GearboxEditDialog(ws, G.schedule_set_for(s, "tcc"))
    d._recompute()
    return d


def test_tcc_mode_controls(tcc):
    from ovtsuite import gui_gearbox_edit as G
    assert tcc.windowTitle() == G.TITLES["tcc"]
    assert tcc.preset.currentText().startswith("Coding 1: Gear")
    assert [b.text() for b in tcc.line_group.buttons()][:2] == [
        "RO  Regulated → Open", "OR  Open → Regulated"]
    assert tcc.op_box.findData("track") >= 0
    assert tcc.ref_box.findText("SKL 12") >= 0
    assert not tcc.mono_on.isChecked() and tcc.lock_on.isChecked()
    # the dotted upshift references reach the chart
    tcc.preset.setCurrentText("Coding 1: Gear 3")
    tcc._apply_preset("Coding 1: Gear 3")
    tcc._recompute()
    assert any(s.get("refs") for s in tcc.chart.series)


def test_tcc_gear3_edit_through_the_window(tcc):
    """The tcc_gear3 pass, set up with the window's own controls: two steps,
    every cell as the historical result (see test_zf6hp_shiftedit)."""
    tcc.preset.setCurrentText("Both codings: Gear 3")
    tcc._apply_preset("Both codings: Gear 3")
    tcc.unit_box.setCurrentIndex(tcc.unit_box.findData("output"))
    tcc.p_from.setValue(15.0)
    tcc.p_to.setValue(65.0)
    tcc.direction.setCurrentIndex(tcc.direction.findData("lower"))
    for ln, c in tcc.op_lines.items():
        c.setChecked(ln == 1)
    tcc.op_box.setCurrentIndex(tcc.op_box.findData("track"))
    tcc.ref_box.setCurrentText("SKL 12")
    tcc.amount.setValue(85)
    tcc.within_chk.setChecked(True)
    tcc.within.setValue(100)
    tcc._add_step()
    for ln, c in tcc.op_lines.items():
        c.setChecked(ln == 0)
    tcc.op_box.setCurrentIndex(tcc.op_box.findData("set"))
    tcc.blend.setValue(100)
    tcc.amount.setValue(1051)
    tcc._recompute()
    assert tcc.result.cells == 200
    from ovtsuite.gearbox_edit import Edit, Scope, SetCurve, TrackReference
    sset = tcc.sset
    gear3 = sset.presets["Both codings: Gear 3"]
    rows = dict(pedal_min=15.0, pedal_max=65.0, direction="lower")
    core = Edit(sset, [TrackReference(Scope(gear3, [1], **rows), "SKL 12", factor=0.85,
                                      within=100),
                       SetCurve(Scope(gear3, [0], **rows), [(0, 1051), (100.4, 1051)],
                                "output")]).compute()
    assert sorted((c.key, c.new) for c in tcc.result.changes) == \
        sorted((c.key, c.new) for c in core.changes)
    s = tcc.session
    before = bytes(s.binfile.data)
    assert tcc.edit.apply(s, "test", tcc.result) == 200
    s.undo()
    assert bytes(s.binfile.data) == before


def test_tools_menu_opens_the_lockup_editor(app, tmp_path):
    """Tools > ZF 6HP > Lock-up (TCC) editor... on a real main window."""
    path = os.path.join(SIXHP, "3.0TFSI-Stock.bin")
    if not os.path.exists(path):
        pytest.skip("no ZF 6HP reference binaries")
    from ovtsuite import gui as G
    from ovtsuite.session import TunerSession
    w = G.MainWindow(TunerSession(path), None)
    try:
        act = next(a for a in w.zf6hp_menu.actions() if a.text() == "Lock-up (TCC) editor...")
        act.trigger()
        dlg = w.current.tool("tccedit")
        assert dlg is not None and dlg.sset.kind == "tcc" and dlg.sset.schedules
        dlg.close()
    finally:
        w.close_discarding()


# ------------------------------------------------- overlays: blend and logs
@pytest.fixture
def shift_in_project(app):
    if not os.path.exists(TUNE):
        pytest.skip("Bear's 6HP19 project is not in Reference/Tunes")
    from types import SimpleNamespace
    from ovtsuite import project as ovt
    from ovtsuite import gui_gearbox_edit as G
    from test_zf6hp_shiftedit import _checkpoint_bytes, _session
    proj = ovt.load(TUNE)
    s = _session(_checkpoint_bytes(proj, "133345e948"))

    class WS(QtWidgets.QWidget):
        pass
    ws = WS()
    ws.primary, ws.model = s, None
    ws.project_ref = SimpleNamespace(doc=SimpleNamespace(project=proj), sid="e054a2030b")
    d = G.GearboxEditDialog(ws, G.schedule_set_for(s))
    d._recompute()
    return d


def test_effective_line_and_logged_shifts_on_the_chart(shift_in_project):
    d = shift_in_project
    assert d.blend_box.currentText() == "D" and d.eff_chk.isEnabled()
    d.preset.setCurrentText("Coding 1: D (economic) — adaptive grid")
    d._apply_preset(d.preset.currentText())
    d.line_group.button(2).click()                       # 3>4
    d.eff_chk.setChecked(True)
    d.log_chk.setChecked(True)
    d._recompute()
    ov = d.chart.overlay
    name, before, after = ov["effective"][0]
    assert name == "D" and before == after and len(after) >= 12
    assert len(ov["points"]) > 10                           # D's 3>4 shifts, program 10
    assert "logged shifts in D" in d.log_lbl.text()
    # an edit on the corners moves the effective line by exactly that much
    d.op_box.setCurrentIndex(d.op_box.findData("offset"))
    d.unit_box.setCurrentIndex(d.unit_box.findData("output"))
    d.amount.setValue(100)
    d._recompute()
    _n, before, after = d.chart.overlay["effective"][0]
    assert all(abs((a - b) - 100) < 1e-6 for (_p, a), (_q, b) in zip(after, before))


def test_blend_table_edits_are_kept_with_the_project(shift_in_project):
    from ovtsuite import gui_gearbox_edit as G
    d = shift_in_project
    dlg = G.BlendDialog(d, d._blends())
    assert dlg.name.text() == "D" and dlg.table.rowCount() == 2
    dlg.table.cellWidget(1, 1).setValue(0.42)
    dlg._fit()
    assert "not determinable" in dlg.report.toPlainText()
    dlg._accept()
    d._store_blends(dlg.blends)
    kept = d._binary_set().extra[G._BLENDS_KEY]
    assert kept[0]["corners"] == [["SKL 11.0", 0.58], ["SKL 16.0", 0.42]]


def test_lockup_gap_off_and_pair_controls(tcc):
    tcc.preset.setCurrentText("Coding 1: Gear 3")
    tcc._apply_preset("Coding 1: Gear 3")
    tcc.unit_box.setCurrentIndex(tcc.unit_box.findData("output"))
    for ln, c in tcc.op_lines.items():
        c.setChecked(ln == 0)
    tcc.op_box.setCurrentIndex(tcc.op_box.findData("gap"))
    tcc.anchor_box.setCurrentIndex(tcc.anchor_box.findData("low"))
    tcc.amount.setValue(150)
    tcc.p_from.setValue(20)
    tcc.p_to.setValue(60)
    tcc._recompute()
    res = tcc.result
    assert res.cells and {tcc.sset.line(c.key[2]).label for c in res.changes} == {"OR"}
    for ch in res.changes:
        i, r, _c = ch.key
        s = tcc.sset.schedules[i]
        assert res.grid[i][r * s.cols + 1] - res.grid[i][r * s.cols + 0] == 150
    # "never": every threshold of the characteristic by default
    tcc.op_box.setCurrentIndex(tcc.op_box.findData("off"))
    assert all(c.isChecked() for c in tcc.op_lines.values())
    tcc._recompute()
    assert all(c.new in (10000, 8160) for c in tcc.result.changes)
    # with its pair: RO ticked moves OR too
    for ln, c in tcc.op_lines.items():
        c.setChecked(ln == 0)
    tcc.with_pair.setChecked(True)
    tcc.op_box.setCurrentIndex(tcc.op_box.findData("offset"))
    tcc.amount.setValue(50)
    tcc._recompute()
    assert {tcc.sset.line(c.key[2]).label for c in tcc.result.changes} == {"RO", "OR"}


# ------------------------------------------------------------------ ZF 8HP
_8HP = os.path.join(os.path.dirname(__file__), os.pardir, "Reference", "ZF8HP", "OEM",
                    "8K0927158AD_1005.frf.bin")


def test_8hp_editors_from_the_tools_menu(app):
    """Tools > ZF 8HP > both editors on a Gen1 image: 14 line buttons, gear
    groups for the WK characteristics, no upshift-reference controls."""
    if not os.path.exists(_8HP):
        pytest.skip("no ZF 8HP reference image")
    from ovtsuite import gui as G
    from ovtsuite.session import TunerSession
    w = G.MainWindow(TunerSession(_8HP), None)
    try:
        acts = {a.text(): a for a in w.zf8hp_menu.actions()}
        acts["Shift map editor..."].trigger()
        dlg = w.current.tool("shiftedit")
        assert dlg is not None and len(dlg.line_group.buttons()) == 14
        assert dlg.sset.line("7>8").ratio_left == 0.839
        dlg.close()
        acts["Lock-up (TCC) editor..."].trigger()
        dlg = w.current.tool("tccedit")
        assert dlg.preset.currentText() == "Gear 1"
        assert not dlg.has_refs and not dlg.lock_on.isChecked()
        assert dlg.op_box.findData("track") < 0 and dlg.op_box.findData("off") >= 0
        dlg.preset.setCurrentText("Gear 3")
        dlg._apply_preset("Gear 3")
        dlg._recompute()
        assert {s.group for s in dlg._selected_schedules()} == {"Gear 3"}
        dlg.close()
    finally:
        w.close_discarding()

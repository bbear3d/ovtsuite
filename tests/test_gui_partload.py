# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The shift editor's part-load strategy, pedal-axis and replay controls
(ovtsuite.gui_partload), offscreen, on Bear's project."""
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6 import QtWidgets  # noqa: E402

pytestmark = pytest.mark.slow

TUNE = os.path.join(os.path.dirname(__file__), os.pardir, "Reference", "Tunes",
                    "2010-A6-BHW-6HP-19A.ovt")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def ed(app):
    if not os.path.exists(TUNE):
        pytest.skip("Bear's 6HP19 project is not in Reference/Tunes")
    from ovtsuite import project as ovt
    from ovtsuite import gui_gearbox_edit as G
    from test_zf6hp_shiftedit import _checkpoint_bytes, _session
    proj = ovt.load(TUNE)
    s = _session(_checkpoint_bytes(proj, "634e8e29e1"))

    class WS(QtWidgets.QWidget):
        pass
    ws = WS()
    ws.primary, ws.model, ws.win = s, None, None
    ws.project_ref = SimpleNamespace(doc=SimpleNamespace(project=proj, tabs=lambda win: []),
                                     sid="e054a2030b")
    d = G.GearboxEditDialog(ws, G.schedule_set_for(s))
    d._recompute()
    return d


def _select(ed, *presets):
    ed.preset.setCurrentText(presets[0])
    ed._apply_preset(presets[0])
    for p in presets[1:]:
        ed._set_checked([ed.sset.schedules[i].key for i in ed.sset.presets[p]], add=True)


def test_part_load_d_economy_s_sport(ed):
    _select(ed, "Coding 1: D (economic) — adaptive grid", "Coding 1: S (sport) — adaptive grid")
    ed.op_box.setCurrentIndex(ed.op_box.findData("partload"))
    p = ed.pl_panel
    assert p.isVisibleTo(ed) and not ed.pedal_w.isVisibleTo(ed)
    assert p.rows["D"].box.currentText() == "Economy"
    assert p.rows["S"].box.currentText() == "Sport"
    assert p.rows[""].box.currentText() == "Leave alone"
    ed._recompute()
    res = ed.result
    assert res.changes
    sset = ed.sset
    i11, i13 = sset.index("SKL 11.0"), sset.index("SKL 13.0")
    s11 = sset.schedules[i11]
    # 4>5 at light pedal: D lower than S
    d = res.grid[i11][0 * s11.cols + 3]
    s_ = res.grid[i13][0 * s11.cols + 3]
    assert d < s_
    # full-load rows untouched; from the full-torque row (87.8 %) the full-load
    # point -- the planned one, but never above the row full load has now
    # (the full-load step not applied), so the line never falls into it
    r100 = next(r for r, q in enumerate(s11.pedal) if abs(q - 100.0) < 0.1)
    assert res.grid[i11][r100 * s11.cols + 3] == ed.edit.origin[i11][r100 * s11.cols + 3]
    plan = ed.fl_panel.plan.targets[1].output                 # 2>3 full-load point
    own = ed.edit.origin[i11][r100 * s11.cols + 1]
    r88 = next(r for r, q in enumerate(s11.pedal) if abs(q - 87.8) < 0.1)
    assert abs(res.grid[i11][r88 * s11.cols + 1] - min(plan, own)) <= 0.6
    assert max(res.grid[i11][r * s11.cols + 1] for r in range(r100)) <= own + 0.6
    # a preset edited by hand becomes Custom
    p.rows["D"].floor.setValue(1300)
    assert p.rows["D"].box.currentText() == "Custom"


def test_full_load_step_can_start_at_the_full_torque_row(ed):
    _select(ed, "Coding 1: D (economic) — adaptive grid")
    ed.op_box.setCurrentIndex(ed.op_box.findData("fullload"))
    fl = ed.fl_panel
    assert fl.ft_chk.isEnabled() and "87.8" in fl.ft_chk.text()
    fl.ft_chk.setChecked(True)
    ed._recompute()
    sset = ed.sset
    i11 = sset.index("SKL 11.0")
    s = sset.schedules[i11]
    r88 = next(r for r, q in enumerate(s.pedal) if abs(q - 87.8) < 0.1)
    assert abs(ed.result.grid[i11][r88 * s.cols + 1] - fl.plan.targets[1].output) <= 0.6


def test_pedal_axis_moves_to_full_torque_and_undoes(ed):
    _select(ed, "Coding 1: D (economic) — adaptive grid")
    ed.op_box.setCurrentIndex(ed.op_box.findData("pedalaxis"))
    ax = ed.ax_panel
    assert 80.0 <= ax.ft.value() <= 90.0                      # 84.5 on Bear's ECU
    ed._recompute()
    res = ed.result
    assert not res.changes and res.axes
    n_shift = len(ed.sset.presets["Coding 1: D (economic) — adaptive grid"])
    assert len(res.axes) > n_shift                            # + the lock-up maps
    i11 = ed.sset.index("SKL 11.0")
    new = res.pedal_of(ed.sset, i11)
    assert abs(new[9] - ax.ft.value()) < 0.5 and new[10] == pytest.approx(100.0, abs=0.05)
    s = ed.session
    before = bytes(s.binfile.data)
    n = ed.edit.apply(s, "test", res)
    assert n > 0
    e = ed.sset.schedules[i11].entity
    assert abs(e.y_values()[9] - ax.ft.value()) < 0.5
    s.undo()
    assert bytes(s.binfile.data) == before


def test_replay_box_before_and_after(ed):
    _select(ed, "Coding 1: D (economic) — adaptive grid", "Coding 1: S (sport) — adaptive grid")
    ed.op_box.setCurrentIndex(ed.op_box.findData("partload"))
    ed._recompute()
    rb = ed.replay_box
    assert rb.run_btn.isEnabled()
    seen = []
    rb.bar.valueChanged.connect(lambda v: seen.append(v))
    rb.run(wait=True)
    assert rb.drives is not None and rb.drives.hours > 1.0, rb.note.text()
    b = rb.table.item(0, 0).text()
    a = rb.table.item(1, 0).text()
    assert float(b) > 3.0 and "%" in a
    assert rb._before.agree > 0.8                      # the replay follows the logged gears
    assert seen and max(seen) >= rb.bar.maximum() - 1  # the bar went to the end
    assert not rb.prog_w.isVisible()


def test_converter_check_and_efficiency_map(ed):
    _select(ed, "Coding 1: D (economic) — adaptive grid")
    ed.op_box.setCurrentIndex(ed.op_box.findData("partload"))
    assert ed.conv_on.isChecked()
    ed._recompute()
    conv = [f for f in ed.result.findings if f.code == "converter"]
    assert conv and "converter open" in conv[0].message
    ed.conv_on.setChecked(False)
    ed._recompute()
    assert not [f for f in ed.result.findings if f.code == "converter"]
    rb = ed.replay_box
    rb.run(wait=True)
    from ovtsuite.gui_effmap import EfficiencyDialog
    shown = []
    EfficiencyDialog.show = lambda self: shown.append(self)
    rb.open_map()
    dlg = shown[0]
    m = dlg.map
    assert m.cells and m.before and m.after
    assert min(m.cells.values()) > 180 and min(m.cells.values()) < 230   # ~204 g/kWh best
    dlg.map.grab()                                                     # paints without error


def test_logs_load_in_the_background_with_a_progress_bar(ed):
    import time
    _select(ed, "Coding 1: D (economic) — adaptive grid")
    rb = ed.replay_box
    rb.run()                                   # returns at once; a thread reads the logs
    assert rb.prog_w.isVisibleTo(ed) and not rb.run_btn.isEnabled()
    t0 = time.time()
    while rb.drives is None and time.time() - t0 < 150:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.05)
    assert rb.drives is not None, rb.note.text()
    assert not rb.prog_w.isVisibleTo(ed) and rb.run_btn.isEnabled()
    assert rb.table.item(0, 0) is not None


def test_cancel_stops_the_load(ed):
    import time
    rb = ed.replay_box
    rb.run()
    rb.cancel()
    t0 = time.time()
    while rb._thread is not None and time.time() - t0 < 60:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.05)
    assert rb.drives is None and "Cancelled" in rb.note.text()
    assert rb.run_btn.isEnabled()

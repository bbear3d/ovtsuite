# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The shift editor's "Full-load optimum" step (ovtsuite.gui_fullload),
offscreen, on Bear's project: the ECU it finds, the targets, the edit."""
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


def test_full_load_step_finds_the_flashed_ecu_and_edits_the_full_load_rows(ed):
    assert ed.op_box.findData("fullload") >= 0
    ed.preset.setCurrentText("Coding 1: D (economic) — adaptive grid")
    ed._apply_preset(ed.preset.currentText())
    ed.op_box.setCurrentIndex(ed.op_box.findData("fullload"))
    p = ed.fl_panel
    assert p.isVisibleTo(ed) and not ed.pedal_w.isVisibleTo(ed)
    assert p.set_box.currentText().startswith("Stock")
    assert p.ver_box.currentText().startswith("As flashed")
    assert p.plan is not None and "kW" in p.curve_lbl.text()
    assert {t.label for t in p.plan.targets.values()} == {"1>2", "2>3", "3>4", "4>5", "5>6"}
    assert p.open_lbl.text() == "open in gear 1"           # no lock-up in 1st for D
    assert p.table.rowCount() == 5
    assert len(p.chart.traces) == 2                         # today and the optimum
    # every upshift ticked, nothing else
    ticked = {ed.sset.line(ln).label for ln, c in ed.op_lines.items() if c.isChecked()}
    assert ticked == {"1>2", "2>3", "3>4", "4>5", "5>6"}
    ed._recompute()
    res = ed.result
    assert res.changes
    # rows from the blend pedal up -- and, where a step is spread, down to 50 %
    assert all(ed.sset.schedules[i].pedal[r] >= 50.0 for (i, r, c) in (ch.key for ch in res.changes))
    i11 = ed.sset.index("SKL 11.0")
    s = ed.sset.schedules[i11]
    r100 = next(r for r, q in enumerate(s.pedal) if abs(q - 100.0) < 0.1)
    want = p.plan.targets[0].output
    assert abs(res.grid[i11][r100 * s.cols + 0] - want) <= 0.5
    # the steps it creates are spread: the step check has nothing to say
    assert not [f for f in res.findings if f.code == "step" and not f.fixed]


def test_min_gain_leaves_close_lines_alone(ed):
    ed.preset.setCurrentText("Coding 1: D (economic) — adaptive grid")
    ed._apply_preset(ed.preset.currentText())
    ed.op_box.setCurrentIndex(ed.op_box.findData("fullload"))
    ed.fl_panel.min_gain.setValue(5.0)
    ed._recompute()
    i11 = ed.sset.index("SKL 11.0")
    i20 = ed.sset.index("SKL 20.0")
    on = lambda i: {ed.sset.line(ch.key[2]).label for ch in ed.result.changes  # noqa: E731
                    if ch.key[0] == i}
    # SKL 11's upper upshifts are within a percent already; SKL 20's sit far lower
    assert "1>2" in on(i11) and "5>6" not in on(i11) and "4>5" not in on(i11)
    assert "5>6" in on(i20)


def test_shift_editor_without_a_project_says_where_the_torque_comes_from(app):
    from conftest import ZF6HP_REFERENCE as SIXHP
    f = os.path.join(SIXHP, "3.0TFSI-Stock.bin")
    if not os.path.exists(f):
        pytest.skip("no ZF 6HP reference binaries")
    from ovtsuite.session import TunerSession
    from ovtsuite import gui_gearbox_edit as G

    class WS(QtWidgets.QWidget):
        pass
    ws = WS()
    ws.primary, ws.model, ws.project_ref = TunerSession(f), None, None
    d = G.GearboxEditDialog(ws, G.schedule_set_for(ws.primary))
    d.op_box.setCurrentIndex(d.op_box.findData("fullload"))
    assert "project" in d.fl_panel.curve_lbl.text()
    d._recompute()
    assert not d.result.changes

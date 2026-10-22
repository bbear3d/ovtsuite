# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The dyno chart's other runs and its extras (ovtsuite.gui_dyno with
dyno_sources, dyno_diff, dyno_log, dyno_speed, gui_dyno_export), on
03G906016LQ: runs from another tab, a pin, a file, a project's branch and a
data log; what changed; the settings a project keeps; presets; road speed;
exports; and long version names that must not widen the windows."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from test_gui_dyno import FakeTab, LQ, _pump  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


@pytest.fixture
def ws():
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite.session import TunerSession
    t = FakeTab(TunerSession(LQ))
    t.resize(1200, 800)
    t.show()
    _pump()
    yield t
    for d in t._tools.values():
        d.close()
    t.close()


def _raise_limit(session, by=30):
    e = next(x for x in session.entities if x.name == "EngPrt_trqLimP_MAP")
    session.edit_cells(e, [(r, c, v + by) for r, row in enumerate(e.values())
                           for c, v in enumerate(row) if v > 0])
    return e


def _run(st, key):
    return next(r for r in st.runs if r.key == key)


def test_long_version_names_keep_the_window_and_the_panel_on_screen(ws, monkeypatch):
    """A version called by a very long name (and chosen as the reference)
    shows in the lists and tooltips; it widens neither the window nor the
    tab's panel."""
    from ovtsuite import gui_dyno, history
    long = "Stage 2 with the EGR off, the DPF delete and the new injectors " * 5
    monkeypatch.setattr(gui_dyno.DynoState, "versions", lambda self: [
        {"id": "v1", "ts": "2026-10-21 10:00", "label": long},
        {"id": "v2", "ts": "2026-10-21 11:00", "label": "x" * 200}])
    monkeypatch.setattr(history, "display_label", lambda v: v.get("label", ""))
    st = gui_dyno.state_for(ws)
    gui_dyno.set_shown(ws, True)
    st.set_reference("version", "v2")
    d = gui_dyno.open_for(ws)
    _pump()
    assert d.ref_box.findData("version:v2") >= 0
    assert d.minimumSizeHint().width() < 1100
    assert ws.dyno_panel.minimumSizeHint().width() < 900
    assert d.ref_box.sizeHint().width() < 400
    scr = d.screen().availableGeometry()
    assert d.width() <= scr.width() and d.height() <= scr.height()


def test_runs_from_a_tab_a_pin_and_a_file(ws):
    """Each run is worked out through the reference, in a colour of its
    own that it keeps when another goes."""
    from ovtsuite import dyno_sources as S, gui_dyno
    from ovtsuite.session import TunerSession
    other = FakeTab(TunerSession(LQ))
    try:
        st = gui_dyno.state_for(ws)
        st.poll()
        stock = st.curve.peak_torque().torque
        tab = st.add_source(S.make_tab(other))
        pin = st.pin()
        f = st.add_source(S.Source(S.FILE, "LQ on disk", path=LQ))
        assert [x.slot for x in (tab, pin, f)] == [2, 3, 4]
        _raise_limit(ws.primary)
        assert st.poll()
        for src in (tab, pin, f):
            r = _run(st, src.key)
            assert r.curve is not None, r.why
            assert r.curve.peak_torque().torque == pytest.approx(stock, abs=0.5)
        assert st.curve.peak_torque().torque > stock + 2
        _raise_limit(other.primary)             # the tab's run is live
        assert st.poll()
        assert _run(st, tab.key).curve.peak_torque().torque > stock + 2
        st.remove_source(tab.key)
        assert [x.slot for x in st.sources] == [3, 4]       # no repainting
        img = ws.dyno_panel.chart.grab() if gui_dyno.is_shown(ws) else None
        assert img is None or img.width() > 0
    finally:
        other.close()


def test_what_changed_names_the_map(ws):
    from ovtsuite import gui_dyno
    st = gui_dyno.state_for(ws)
    pin = st.pin()
    _raise_limit(ws.primary)
    st.poll()
    d = gui_dyno.open_for(ws)
    d._diff_with(pin.key)
    assert d.diff_box.currentData() == pin.key
    assert d.diff_table.rowCount() > 0
    rows = [d.diff_table.item(r, 6).text() for r in range(d.diff_table.rowCount())]
    assert any("cells" in t for t in rows)
    r = next(i for i, t in enumerate(rows) if "cells" in t)
    ws.opened.clear()
    d._diff_clicked(r, 0)
    assert ws.opened == ["EngPrt_trqLimP_MAP"]
    assert "Nm" in d.diff_note.text()


def test_a_projects_branch_as_a_run_and_as_the_reference(ws, tmp_path):
    from ovtsuite import dyno_sources as S, gui_dyno, project as ovt
    proj, s = ovt.import_file(LQ)
    path = str(tmp_path / "car.ovt")
    proj.save(path)
    st = gui_dyno.state_for(ws)
    src = st.add_source(S.Source(S.PROJECT, "car ▸ ECU", path=path, sid=s.id,
                                 bid=s.active.id))
    r = _run(st, src.key)
    assert r.curve is not None, r.why
    assert r.curve.peak_power().power == pytest.approx(st.curve.peak_power().power,
                                                       abs=0.1)
    st.set_reference("source", src.key)
    assert st.ref_curve is not None and st.ref_label == "car ▸ ECU"
    assert all(x.key != src.key for x in st.runs)       # drawn once, as the reference


def test_a_data_log_as_a_measured_run(ws, tmp_path):
    """A simulated full-load pull, logged on the file's own measuring blocks:
    the measured run lands on the calculated curve where the torque limiter
    governs (the car fuels to it)."""
    import logtune_sim as sim
    from ovtsuite import dyno_sources as S, gui_dyno
    from ovtsuite.datalog.channels import ChannelNamer
    s = ws.primary
    rows = sim.wot_pull(sim.Car(s))
    path = sim.write_log(str(tmp_path / "pull.csv"), s, ChannelNamer(s), [1, 8, 9, 10], rows)
    st = gui_dyno.state_for(ws)
    src = st.add_source(S.Source(S.LOG, "Log pull", path=path))
    r = _run(st, src.key)
    assert r.dots and r.curve is not None, r.why
    assert len(r.curve.points) > 10
    calc = {p.rpm: p for p in st.curve.points}
    near = [(p, calc.get(round(p.rpm / 50) * 50)) for p in r.curve.points
            if 2600 <= p.rpm <= 4000]
    near = [(a, b) for a, b in near if b is not None and b.limiter == "trq_p"]
    assert near
    for a, b in near:
        assert a.torque == pytest.approx(b.torque, rel=0.1)


def test_a_project_tab_keeps_its_dyno_settings(ws):
    """In a project tab the conditions and the runs are the set's, kept in
    the project (a change to it); a plain file's stay the app's."""
    from ovtsuite import dyno_sources as S, gui_dyno
    from ovtsuite.appsettings import settings
    touched = []
    the_set = SimpleNamespace(extra={})
    ws.project_ref = SimpleNamespace(
        set=the_set, doc=SimpleNamespace(project=SimpleNamespace(
            _touch=lambda: touched.append(1)), path=None))
    st = gui_dyno.state_for(ws)
    st.poll()
    st.set_condition(baro=899.0, displacement=2.0)
    st.add_source(S.Source(S.FILE, "LQ on disk", path=LQ))
    st.set_xaxis(mode="speed", gear=5)
    kept = the_set.extra["dyno"]
    assert kept["baro"] == 899.0 and kept["displacement"] == 2.0 and touched
    assert kept["sources"][0]["path"] == LQ and kept["xaxis"]["gear"] == 5
    assert settings().value(gui_dyno.COND_KEY % "baro", None) in (None, "")
    # another tab of the set reads them back
    from ovtsuite.session import TunerSession
    t2 = FakeTab(TunerSession(LQ))
    try:
        t2.project_ref = SimpleNamespace(set=the_set, doc=ws.project_ref.doc)
        st2 = gui_dyno.state_for(t2)
        st2.poll()
        assert st2.cond.baro == 899.0 and st2.cond.displacement == 2.0
        assert [x.label for x in st2.sources] == ["LQ on disk"]
        assert st2.xaxis["mode"] == "speed" and st2.speed_factor()
    finally:
        t2.close()


def test_presets_road_speed_and_exports(ws, tmp_path, monkeypatch):
    from ovtsuite import dyno_speed, gui_dyno, gui_dyno_export
    d = gui_dyno.open_for(ws)
    st = d.state
    i = d.preset_box.findText("Altitude 1000 m (899 hPa)")
    d.preset_box.setCurrentIndex(i)
    d._preset_chosen(i)
    assert st.cond.baro == 899.0
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("Mine", True)))
    d._save_preset()
    assert [n for n, _f in dyno_speed.user_presets()] == ["Mine"]
    assert d.preset_box.currentText() == "Mine" and d.preset_del.isEnabled()
    # road speed in 4th: the file's ratio and rolling circumference
    d.x_box.setCurrentIndex(d.x_box.findData("speed"))
    assert st.xaxis["mode"] == "speed" and st.xaxis["gear"] == 4
    k4 = dyno_speed.file_gears(ws.primary)[0][4]
    assert st.speed_factor() == pytest.approx(k4 / 1000.0)
    d.f_k.setValue(30.0)                        # the user's own figure
    assert st.speed_factor() == pytest.approx(0.03)
    text = gui_dyno_export.csv_text(st)
    head = [l for l in text.splitlines() if not l.startswith("#")][0]
    assert head.startswith("rpm,km/h,") and "gear 4: 30.0 km/h" in text
    img = gui_dyno_export.image(st, 1000, 560)
    assert img.width() >= 1000 and img.height() > 560 + 60
    grab = d.chart.grab()
    assert grab.width() > 300


def test_the_add_menu_lists_the_other_tabs_in_the_window(monkeypatch):
    """In the real window: another open tab is offered, and added it is a
    live run worked out through this tab's reference."""
    from test_gui_dyno import STOCK_Q
    if not (os.path.exists(LQ) and os.path.exists(STOCK_Q)):
        pytest.skip("reference binaries missing")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import dyno_sources as S, gui, gui_dyno
    from ovtsuite.session import TunerSession
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    w = gui.MainWindow(TunerSession(LQ))
    try:
        w.show()
        first = w.current
        assert w.open_path(STOCK_Q, new_tab=True)
        _pump()
        d = gui_dyno.open_for(first)
        d._fill_add_menu()
        tabs = next(a.menu() for a in d.add_menu.actions() if a.text() == "Open tab")
        acts = [a for a in tabs.actions() if "038906016Q" in a.text()]
        assert len(acts) == 1
        acts[0].trigger()
        st = d.state
        src = st.sources[-1]
        assert src.kind == S.TAB
        r = next(x for x in st.runs if x.key == src.key)
        assert r.curve is not None and r.curve.points, r.why
        assert d.runs_list.count() == 1
        d.close()
    finally:
        w.close_discarding()


def test_road_speed_from_a_gearbox_and_a_tyre(ws, monkeypatch):
    """The catalogue's gearbox on the tyre given; your own gearbox (two
    final drives); a tyre size or a circumference."""
    from ovtsuite import dyno_speed as sp, gui_dyno
    assert sp.tyre_circ("225/45 R17") == pytest.approx(1.933, abs=0.002)
    assert sp.tyre_circ("1.95") == 1.95 and sp.tyre_circ("nonsense") is None
    d = gui_dyno.open_for(ws)
    st = d.state
    d.x_box.setCurrentIndex(d.x_box.findData("speed"))
    i = d.gearbox_box.findData("02Q-GRF")
    assert i > 0
    d.gearbox_box.setCurrentIndex(i)
    d.tyre_edit.setText("205/55 R16")
    d.tyre_edit.editingFinished.emit()
    st.set_xaxis(gear=6)
    circ = sp.tyre_circ("205/55 R16")
    assert st.speed_factor() == pytest.approx(60 * circ / (0.814 * 2.760) / 1000)
    assert "02Q" in d.speed_note.text() or "02Q" in d.speed_note.toolTip()
    # your own gearbox: gears 1-4 on the first final drive, 5-6 on the second
    monkeypatch.setattr(gui_dyno.GearboxDialog, "exec", lambda self: (
        self.name.setText("Mine"), self.gears.setText("3.5 2.0 1.3 0.9 0.9 0.75"),
        self.finals.setText("4.0 3.0"), self.final_of.setText("1 1 1 1 2 2"),
        self._save(), QtWidgets.QDialog.Accepted)[-1])
    d._edit_gearbox(new=True)
    gid = st.xaxis["gearbox"]
    assert sp.gearbox(gid)["name"] == "Mine"
    assert st.speed_factor() == pytest.approx(60 * circ / (0.75 * 3.0) / 1000)
    # the ECU's own model stays a choice on EDC16, said to be the ECU's
    st.set_xaxis(gearbox="")
    assert "ECU" in st.gear_speeds()[1]


def test_edc15_road_speed_needs_the_gearbox():
    """EDC15 keeps no gear ratios: no estimate, the gearbox is chosen."""
    from ovtsuite import dyno_speed as sp
    from ovtsuite.session import TunerSession
    from conftest import BINARIES
    p = os.path.join(BINARIES, "EDC15", "038906019GQ.bin")
    if not os.path.exists(p):
        pytest.skip("reference binary missing")
    gears, why = sp.ecu_gears(TunerSession(p))
    assert gears == {} and "EDC15" in why

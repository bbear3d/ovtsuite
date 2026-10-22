# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The dyno chart panel and window (ovtsuite/gui_dyno.py), offscreen on
03G906016LQ."""
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
STOCK_Q = os.path.join(BINARIES, "038906016Q.Bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


class FakeTab(QtWidgets.QWidget):
    """Just what gui_dyno uses of a tab (gui.Workspace)."""

    def __init__(self, primary, comparison=None):
        super().__init__()
        self.primary, self.comparison, self.project_ref = primary, comparison, None
        self._tools = {}
        self.opened = []
        self.dyno_btn = QtWidgets.QPushButton("Dyno")
        self.table = QtWidgets.QTableView()
        from ovtsuite import gui_dyno
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.dyno_btn)
        lay.addWidget(gui_dyno.attach(self, self.table), 1)

    def open_tool(self, kind, factory):
        d = self._tools.get(kind)
        if d is None:
            d = self._tools[kind] = factory()
        d.show()
        return d

    def tool(self, kind):
        return self._tools.get(kind)

    def show_entity(self, e):
        self.opened.append(e.name)


@pytest.fixture
def ws():
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite.session import TunerSession
    t = FakeTab(TunerSession(LQ))
    t.resize(1200, 800)
    t.show()
    QtWidgets.QApplication.processEvents()
    yield t
    for d in t._tools.values():
        d.close()
    t.close()


@pytest.fixture
def win(monkeypatch):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import gui
    from ovtsuite.session import TunerSession
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    w = gui.MainWindow(TunerSession(LQ))
    w.resize(1300, 850)
    w.show()
    QtWidgets.QApplication.processEvents()
    yield w
    w.close_discarding()


def _pump():
    for _ in range(3):
        QtWidgets.QApplication.processEvents()


def test_toggle_shows_the_panel_with_the_curve(ws):
    from ovtsuite import gui_dyno
    assert gui_dyno.availability(ws) is None
    assert not gui_dyno.is_shown(ws)
    ws.dyno_btn.click()
    _pump()
    assert gui_dyno.is_shown(ws) and ws.dyno_btn.isChecked()
    st = gui_dyno.state_for(ws)
    assert st.curve is not None and st.curve.points
    assert st.ref_kind == "original" and "same as current" in st.ref_label
    assert "kW @ 4000" in ws.dyno_panel.summary.text()
    img = ws.dyno_panel.chart.grab()
    assert img.width() > 300 and img.height() > 150
    ws.dyno_btn.click()
    assert not gui_dyno.is_shown(ws)


def test_units_change_the_summary_not_the_numbers(ws):
    from ovtsuite import gui_dyno
    gui_dyno.set_shown(ws, True)
    _pump()
    st = gui_dyno.state_for(ws)
    kw = st.curve.peak_power().power
    p = ws.dyno_panel
    p.power_box.setCurrentIndex(p.power_box.findData("PS"))
    p.torque_box.setCurrentIndex(p.torque_box.findData("lbft"))
    assert st.power_unit == "PS" and st.torque_unit == "lbft"
    assert st.curve.peak_power().power == kw
    assert "PS @" in p.summary.text() and "lb·ft" in p.summary.text()


def test_an_edit_recalculates(ws):
    from ovtsuite import gui_dyno
    gui_dyno.set_shown(ws, True)
    _pump()
    st = gui_dyno.state_for(ws)
    before = st.curve.peak_torque().torque
    e = next(x for x in ws.primary.entities if x.name == "EngPrt_trqLimP_MAP")
    rows = [(r, c, v + 30) for r, row in enumerate(e.values()) for c, v in enumerate(row)
            if v > 0]
    ws.primary.edit_cells(e, rows)
    assert st.poll()
    # 30 Nm more limit: the smoke limiter takes over at ~57 mg, so a little more
    at = {p.rpm: p for p in st.curve.points}
    assert at[2000].limiter.startswith("smk_")
    assert before + 2 < st.curve.peak_torque().torque < before + 30
    # the original stays the reference: its curve is the stock one
    assert st.ref_curve.peak_torque().torque == pytest.approx(before, abs=0.5)
    assert "same as current" not in st.ref_label


def test_window_reference_and_conditions(ws):
    from ovtsuite import gui_dyno
    d = gui_dyno.open_for(ws)
    assert d is ws.tool("dyno")
    st = d.state
    assert d.table.rowCount() == len(st.curve.points)
    assert d.ref_box.currentData() == "original"
    d.f_cap.setValue(1800)
    assert st.cond.boost_cap == 1800
    capped = st.curve.peak_power().power
    d.f_cap.setValue(0)
    assert st.cond.boost_cap is None and st.curve.peak_power().power > capped
    d.ref_box.setCurrentIndex(d.ref_box.findData("none"))
    assert st.ref_kind == "none" and st.ref_curve is None
    i = d.ref_box.findData("comparison")
    d.ref_box.setCurrentIndex(i)
    assert st.ref_curve is None and "no comparison" in st.ref_label


def test_strip_click_opens_the_map(ws):
    from ovtsuite import gui_dyno
    gui_dyno.set_shown(ws, True)
    _pump()
    ws.dyno_panel.chart.limiterClicked.emit("trq_p", 2500.0)
    assert ws.opened == ["EngPrt_trqLimP_MAP"]


def test_window_exports(ws, tmp_path, monkeypatch):
    from ovtsuite import gui_dyno
    d = gui_dyno.open_for(ws)
    out = tmp_path / "lq.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(out), "")))
    d._export_csv()
    text = out.read_text(encoding="utf-8")
    head = [l for l in text.splitlines() if not l.startswith("#")][0]
    assert text.startswith("# ") and head.startswith("rpm,")
    assert "charge 50 °C" in text and "Reference" in text   # the conditions
    png = tmp_path / "lq.png"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(png), "")))
    d._save_png()
    assert png.stat().st_size > 1000


def test_a_tune_against_the_comparison(ws):
    """The comparison as reference: Q stage 2 read through stock Q."""
    from ovtsuite import gui_dyno
    from ovtsuite.session import TunerSession
    if not os.path.exists(STOCK_Q):
        pytest.skip("reference binary missing")
    stg2 = os.path.join(BINARIES, "038906016Q-Stg2-MPPS Read.Bin")
    t = FakeTab(TunerSession(stg2), TunerSession(STOCK_Q))
    try:
        st = gui_dyno.state_for(t)
        st.set_reference("comparison")
        assert st.ref_label.startswith("Comparison")
        assert st.curve.peak_torque().torque > st.ref_curve.peak_torque().torque + 100
        d = gui_dyno.open_for(t)
        assert any(it is not None and it.text().endswith("*")
                   for it in (d.table.item(r, 4) for r in range(d.table.rowCount())))
        d.close()
    finally:
        t.close()


def test_main_window_toggle(win):
    """In the real tab: the edit-bar button shows and hides the panel."""
    from ovtsuite import gui_dyno
    ws = win.current
    ws.dyno_btn.click()
    _pump()
    assert gui_dyno.is_shown(ws) and gui_dyno.state_for(ws).curve.points
    ws.dyno_btn.click()
    assert not gui_dyno.is_shown(ws)


def test_tools_menu_entry(win):
    acts = [a for a in win.tools_menu.actions() if a.text() == "Dyno chart..."]
    assert len(acts) == 1
    win._refresh_enabled()
    assert acts[0].isEnabled()


def test_overboost_box_only_where_calibrated(ws):
    """LQ has only the 2x2 overboost dummy: the box is off and disabled. The
    R50 calibrates one: ticking it applies it in full."""
    from ovtsuite import gui_dyno
    d = gui_dyno.open_for(ws)
    assert not d.f_ob.isEnabled()
    r50 = os.path.join(os.path.dirname(LQ), "md_070906016DP_R50.bin")
    if not os.path.exists(r50):
        pytest.skip("R50 binary missing")
    from ovtsuite.session import TunerSession
    t = FakeTab(TunerSession(r50))
    try:
        d2 = gui_dyno.open_for(t)
        st = d2.state
        assert d2.f_ob.isEnabled() and not d2.f_ob.isChecked()
        steady = st.curve.peak_power().power
        d2.f_ob.setChecked(True)
        assert st.cond.overboost and st.curve.peak_power().power > steady
        d2.close()
    finally:
        t.close()


def test_a_unit_change_in_the_panel_keeps_the_windows_choices(ws):
    """The window refilling its controls (the units changed in the panel)
    does not feed them back as choices: the wish map picked stays."""
    from ovtsuite import gui_dyno
    gui_dyno.set_shown(ws, True)
    dlg = gui_dyno.open_for(ws)
    dlg.wish_box.setCurrentIndex(dlg.wish_box.count() - 1)
    picked = dlg.state.cond.wish
    assert picked
    ws.dyno_panel.power_box.setCurrentIndex(1)
    assert dlg.state.cond.wish == picked


def test_another_file_in_the_tab_starts_afresh(ws):
    """What was picked for one file (the engine, the wish map) does not carry
    over to the next file opened in the tab."""
    from ovtsuite import gui_dyno
    from ovtsuite.session import TunerSession
    st = gui_dyno.state_for(ws)
    st.poll()
    st.set_condition(displacement=2.5, wish="3")
    ws.primary = TunerSession(LQ)
    assert st.poll()
    assert st.cond.displacement is None and st.cond.wish == ""

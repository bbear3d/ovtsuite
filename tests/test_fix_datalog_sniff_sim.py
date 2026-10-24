# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Regressions for the logger / sniffer / simulator bugs found while writing
the manual (bugs 11, 23, 24, 30, 31, 32, 33).

The decode and log-reader checks run anywhere; the dialog checks build the
real windows offscreen and are marked slow like the other GUI tests.
"""
import os

import pytest

from ovtsuite.datalog import logfile as L
from ovtsuite.flash.diag import decode_cell

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_6HP19 = os.path.join(HERE, os.pardir, "Reference", "Logs",
                         "6hp19-tdi20-HALF-6-8_20261008_162724-discover.csv")


# ------------------------------------------------- bug 31: 6HP19 engaged gear
@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def test_formula_17_gear_is_a_number_with_plain_text():
    """The GS19 sends its engaged gear as two ASCII characters, '3 '."""
    assert decode_cell(17, ord("3"), ord(" ")) == (3.0, "", "3")
    assert decode_cell(17, ord("0"), ord(" ")) == (0.0, "", "0")
    # a letter stays text: no value is invented for reverse
    assert decode_cell(17, ord("R"), ord(" ")) == (None, "", "R")


def test_reader_accepts_gear_quoted_by_older_versions():
    assert L.split_value_unit('"3 "') == (3.0, "")
    assert L.split_value_unit('"R "') == (None, "")
    assert L.split_value_unit("3") == (3.0, "")
    assert L.split_value_unit("812 rpm") == (812.0, "rpm")


def test_real_6hp19_log_charts_the_gear_and_tune_from_logs_sees_it():
    if not os.path.exists(LOG_6HP19):
        pytest.skip("no 6HP19 reference log")
    log = L.load(LOG_6HP19)
    i = next(k for k, c in enumerate(log.channels) if "Tcu_stGear" in c.label)
    col = log.column(i)
    assert set(v for v in col if v is not None) == {0.0, 1.0, 2.0, 3.0, 4.0, 5.0}
    assert col.count(None) == 1                     # the one "R " sample
    from ovtsuite.logtune.samples import run_from_log
    assert run_from_log(log).has("tcu_gear")


# ------------------------------------------------------------- GUI fixtures
def _qt():
    from ovtsuite import gui as _g
    if not _g.HAVE_QT:
        pytest.skip("needs a working PySide6 QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets
    return QtCore, QtWidgets


@pytest.fixture
def app(tmp_path, monkeypatch, isolated_qsettings):
    # (isolated_qsettings: the dialogs' remembered selections -- transport,
    # cable, controller, Dual logging -- go to a file of this test's own, not
    # the real settings; setPath() never reached those on Windows)
    QtCore, QtWidgets = _qt()
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import recent
    monkeypatch.setattr(recent, "get_last_dir", lambda *a, **k: str(tmp_path))
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: None))
    return a


class _Bin:
    def __init__(self, name):
        self.name = name


class _Sess:
    def __init__(self, platform, name):
        self.platform = platform
        self.binfile = _Bin(name)


class _Ws:
    def __init__(self, sess):
        self.primary = sess


def _window(*sessions):
    from PySide6 import QtWidgets

    class _Win(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self._ws = [_Ws(s) for s in sessions]

        def workspaces(self):
            return list(self._ws)

        @property
        def current(self):
            return self._ws[0] if self._ws else None

        @property
        def primary(self):
            return self._ws[0].primary if self._ws else None

    return _Win()


def _logger(*sessions):
    from ovtsuite.datalog import gui_log as GL
    win = _window(*sessions)
    dlg = GL.DataLogDialog(win)
    dlg._test_win = win
    return dlg


# ------------------------------------------------------------ bug 11: sniffer
@pytest.mark.slow
def test_serial_tap_cannot_send(app):
    from ovtsuite.sniff import gui_sniff as S
    d = S.SnifferDialog()
    try:
        d.src_box.setCurrentIndex(1)                # serial K-line tap
        assert not d.send_btn.isEnabled()
        assert not d.tp_tpl_btn.isEnabled()
        d.src_box.setCurrentIndex(0)
        assert d.send_btn.isEnabled()

        class _Tap:                                 # a running serial tap: no send()
            pass
        d._worker = _Tap()
        d._send()                                   # used to raise AttributeError
    finally:
        d._worker = None
        d.close()


# -------------------------------------------- bug 33: cells shown on open
@pytest.mark.slow
def test_cell_table_is_filled_on_open(app):
    d = _logger(_Sess("zf6hp", "6hp19.6hp"))
    try:
        p = d.panes[0]
        assert p.conn.ecu() == "ZF6HP"
        g = p._current_group()
        assert p.browse.rowCount() == p._namer.slots(g) > 0
        assert p.browse.item(0, 1).text() == p._namer.channel(g, 0).title
        assert p.group_spin.value() == g
    finally:
        d.close()


# --------------------------------------- bug 23: presets per controller
@pytest.mark.slow
@pytest.mark.parametrize("ecu", ["EDC15", "Aisin TRx", "ZF6HP-UDS"])
def test_controllers_without_presets_say_so(app, ecu, monkeypatch):
    # EDC15 has presets only when something names its cells: no file here,
    # and the module's catalogue (whose generic layout would name them) is
    # taken away, so nothing does
    from ovtsuite.datalog import gui_log as GL
    monkeypatch.setattr(GL._msbcat, "namer_for", lambda *a, **k: None)
    d = _logger(_Sess("edc16", "lq.edc16"))
    try:
        p = d.panes[0]
        if ecu != "EDC15":
            p.conn.transport_box.setCurrentIndex(p.conn.transport_box.findData("can"))
        p.conn.ecu_box.setCurrentText(ecu)
        assert p.conn.ecu() == ecu
        assert p.preset_box.count() == 1             # only "(choose cells by hand)"
        assert not p.preset_box.isEnabled()
        assert p.preset_hint.text().startswith("No presets for %s" % ecu)
        p.conn.ecu_box.setCurrentText("EDC16")
        assert p.preset_box.count() > 1 and p.preset_box.isEnabled()
        assert p.preset_hint.text() == ""
    finally:
        d.close()


@pytest.mark.slow
def test_missing_preset_values_are_named_for_people(app):
    from ovtsuite.logtune.roles import RECIPES

    class _Namer:                                    # carries engine speed only
        available = True

        def populated_groups(self):
            return [1]

        def slots(self, g):
            return 4

        def resolve(self, g, f):
            if f == 0:
                return ("Engine speed (Eng_nAvrg)", "Eng_nAvrg", "rpm")
            return ("Group %d / Cell %d" % (g, f + 1), "", "")

        def channel(self, g, f):
            lbl, sig, unit = self.resolve(g, f)
            return L.Channel(g, f, lbl, unit, sig)

    d = _logger(_Sess("edc16", "lq.edc16"))
    try:
        p = d.panes[0]
        p.conn.ecu_box.setCurrentText("EDC16")
        p._namer = _Namer()
        p.preset_box.setCurrentIndex(p.preset_box.findData(RECIPES[0].key))   # boost
        p._apply_preset()
        hint = p.preset_hint.text()
        assert hint.startswith("Not in any block this ECU serves:")
        assert "Injected quantity" in hint and "Boost (specified)" in hint
        assert "Boost actuator duty" in hint
        for internal in ("iq,", "boost_spec", "n75"):
            assert internal not in hint
    finally:
        d.close()


# ------------------------------------------- bug 32: default log names
@pytest.mark.slow
def test_a_pane_without_its_own_file_is_named_after_its_controller(app):
    d = _logger(_Sess("edc16", "ecu-lq-stg1.edc16"))       # no TCU file open
    try:
        d.dual_chk.setChecked(True)
        tcu = next(p for p in d.panes if p.conn.ecu() == "ZF6HP")
        ecu = next(p for p in d.panes if p.conn.ecu() == "EDC16")
        tcu._set_default_path()
        name = os.path.basename(tcu.path_edit.text())
        assert name.startswith("zf6hp_") and "ecu-lq-stg1" not in name
        assert os.path.basename(ecu.path_edit.text()).startswith("ecu-lq-stg1_")
    finally:
        d.close()


@pytest.mark.slow
def test_two_panes_on_one_controller_never_share_a_default_name(app):
    d = _logger(_Sess("edc16", "lq.edc16"))
    try:
        d.dual_chk.setChecked(True)
        a, b = d.panes
        b.conn.set_ecu_quietly(a.conn.ecu())
        a._set_default_path()
        b._set_default_path()
        assert a.path_edit.text() != b.path_edit.text()
        # and an existing file is never offered again
        open(a.path_edit.text(), "w").close()
        b.path_edit.setText("")
        a._set_default_path()
        assert not os.path.exists(a.path_edit.text())
    finally:
        d.close()


# ------------------------------------- bug 30: fault codes honour CAN
@pytest.mark.slow
def test_fault_codes_window_connects_over_can_when_can_is_chosen(app, monkeypatch):
    from ovtsuite.datalog import gui_log as GL
    made = {}

    class _Task:
        def __init__(self, *a, **k):
            made["args"], made["kw"] = a, k
            self.status = self.done = self.failed = _Sig()

        def start(self):
            made["started"] = True

    class _Sig:
        def connect(self, *_):
            pass

    monkeypatch.setattr(GL, "EcuTask", _Task)
    monkeypatch.setattr(GL, "retire_thread", lambda *_: None)
    win = _window(_Sess("edc16", "lq.edc16"))
    d = GL.EcuCodesDialog(win)
    try:
        d.conn.transport_box.setCurrentIndex(d.conn.transport_box.findData("can"))
        d.conn.ecu_box.setCurrentText("EDC16")
        monkeypatch.setattr(d.conn, "port", lambda: "dev")
        d._run("read")
        assert made.get("started")
        assert made["kw"]["transport"] == "can"
        assert made["kw"]["can_dest"] == d.conn.can_dest()
        made.clear()
        d.conn.transport_box.setCurrentIndex(d.conn.transport_box.findData("kline"))
        d._run("read")
        assert made["kw"]["transport"] == "kline"
    finally:
        d.close()


def test_ecu_task_opens_tp20_for_can(monkeypatch):
    """The CAN branch builds a CanKLine, not a J2534 K-line."""
    _qt()
    from ovtsuite.datalog import gui_log as GL
    from ovtsuite.flash import tp20_transport as TP
    seen = {}

    class _Ch:
        def pass_all(self):
            pass

        def clear_rx(self):
            pass

    class _Dev:
        def connect(self, proto, rate):
            seen["rate"] = rate
            return _Ch()

        def close(self):
            pass

    class _Link:
        def __init__(self, ch=None, tp=None, dest=None, log=None):
            seen["dest"] = dest

        def close(self):
            pass

    class _Diag:
        def __init__(self, link, ecu, log):
            seen["link"] = link

        def connect(self, speed, cap):
            seen["speed"] = speed

        def close(self):
            pass

    monkeypatch.setattr(GL, "open_j2534", lambda port: _Dev())
    monkeypatch.setattr(TP, "CanKLine", _Link)
    monkeypatch.setattr(GL._diag, "DiagSession", _Diag)
    t = GL.EcuTask("dev", "EDC16", "Low", False, lambda s: [], transport="can",
                   can_rate=500000, can_dest=0x01)
    out = []
    t.done.connect(out.append)
    t.run()
    assert isinstance(seen["link"], _Link) and seen["dest"] == 0x01
    assert seen["rate"] == 500000 and seen["speed"] == "Low"
    assert out == [[]]


# ----------------------------------------- bug 24: simulator tells the truth
@pytest.mark.slow
def test_simulator_greys_out_what_has_no_effect(app):
    from ovtsuite.flash import gui_sim as G
    d = G.SimulatorDialog()
    try:
        assert "6HP19" not in d.transport_box.toolTip()
        d.profile_box.setCurrentIndex(d.profile_box.findData("aisin09d"))
        d.transport_box.setCurrentIndex(d.transport_box.findData("tp20"))
        for w in (d.strict_chk, d.erase_spin, d.pending_chk, d.timing_box):
            assert w.isEnabled()
        d.transport_box.setCurrentIndex(d.transport_box.findData("kline"))
        for w in (d.strict_chk, d.erase_spin, d.pending_chk, d.timing_box):
            assert not w.isEnabled()
        assert d.faithful_chk.isEnabled()
        # running on CAN: the timing boxes are read at Start, so they lock
        d.transport_box.setCurrentIndex(d.transport_box.findData("tp20"))
        d._set_running(True)
        assert not d.timing_box.isEnabled()
        d._set_running(False)
        assert d.timing_box.isEnabled()
    finally:
        d.close()


# ------------------- presets and cell lists resolved from a real binary
def _real(name):
    from ovtsuite.session import TunerSession
    p = os.path.join(HERE, os.pardir, "Reference", "Binaries", *name.split("/"))
    if not os.path.exists(p):
        pytest.skip("needs " + name)
    return TunerSession(p)


@pytest.mark.slow
def test_edc16_cell_list_and_presets_leave_out_empty_cells(app):
    """KN group 2 fills four of its eight cells: the list shows four, and a
    preset logs no pad cell (255) of any block it ticks."""
    s = _real("03G906016KN.bin")
    d = _logger(s)
    try:
        p = d.panes[0]
        p.conn.ecu_box.setCurrentText("EDC16")
        p._sync_namer()
        p.group_spin.setValue(2)
        assert p.browse.rowCount() == 4
        assert p.browse.item(0, 1).text() == "Engine speed (Eng_nAvrg)"
        p.group_spin.setValue(7)                       # cells 1, 3, 4, 5, 6
        assert [p.browse.item(r, 0).data(QtCore().Qt.UserRole)[1]
                for r in range(p.browse.rowCount())] == [0, 2, 3, 4, 5]
        p.preset_box.setCurrentIndex(p.preset_box.findData("boost"))
        p._apply_preset()
        assert p._channels
        for ch in p._channels:
            if ch.is_derived:
                continue
            assert p._namer._groups[ch.group].channels()[ch.field] != 255
    finally:
        d.close()


def QtCore():
    return _qt()[0]


@pytest.mark.slow
def test_edc15_fd_offers_the_edc15_presets(app):
    s = _real("EDC15/038906012FD.bin")
    d = _logger(s)
    try:
        p = d.panes[0]
        p.conn.ecu_box.setCurrentText("EDC15")
        p._sync_namer()
        keys = [p.preset_box.itemData(i) for i in range(1, p.preset_box.count())]
        assert len(keys) >= 10 and "e15limits" in keys and "boost" in keys
        assert p.preset_box.isEnabled() and p.preset_hint.text() == ""
        p.preset_box.setCurrentIndex(p.preset_box.findData("e15limits"))
        p._apply_preset()
        assert p.preset_hint.text().startswith("Blocks ")
        assert any(ch.label.startswith("Torque limitation") for ch in p._channels)
    finally:
        d.close()

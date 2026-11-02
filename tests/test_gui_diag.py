# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag's window (offscreen): construction, the table standard, the
close / transport guards while sessions are open, the vehicle live-data
list, the Live data tab's reads on a live session, and a live session
released from a waiting procedure prompter. The module rail has its own
tests (``test_gui_diag_rail.py``).
``python -m pytest tests/test_gui_diag.py -q --slow``"""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    """Settings in a file of the test's own, the garage (vehicles, scans,
    journals, traces) under tmp_path, one fake serial interface."""
    from ovtsuite.diag import garage as G
    from ovtsuite.diag import gui_diag as GD
    from ovtsuite.diag import link as L
    ini = str(tmp_path / "settings.ini")

    def settings(self):
        s = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
        s.beginGroup(GD.SETTINGS_GROUP)
        return s
    monkeypatch.setattr(GD.DiagWindow, "_settings", settings)
    monkeypatch.setattr(G, "root", lambda: str(tmp_path / "garage"))
    # a J2534-kind entry stays listed for every transport (CAN-capable, K-line-capable)
    monkeypatch.setattr(L, "interfaces", lambda: [L.Interface(data="mock", label="Mock device", kind="j2534")])
    # a connection's K-line baud goes to the user's local pack: recorded here instead
    from ovtsuite.diag import learned as LN
    BAUDS.clear()
    monkeypatch.setattr(LN, "record_kline_baud",
                        lambda addr, baud, generation="", name="": (BAUDS.append((addr, baud)), ("", True))[1])


BAUDS = []


def _pump(app, cond, limit=10.0):
    t = time.time()
    while not cond() and time.time() - t < limit:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return cond()


def _window(app):
    from ovtsuite.diag.gui_diag import DiagWindow
    w = DiagWindow()
    w.show()
    app.processEvents()
    return w


class _FakeLive:
    """Stands in for a LiveSession in the window's registry: the guards
    only ask whether any is open."""
    addr = "01"
    prompter = None
    sess = None
    protocol = "kw1281"
    since = 0.0

    def stop(self):
        pass

    def wait(self, *_):
        return True


# ------------------------------------------------------------ construction
def test_window_builds_with_packs_and_rail_rows(app):
    w = _window(app)
    try:
        assert w._packs, "no definition pack loaded"
        # before a scan the rail is an empty state; the address table sits behind "All known addresses"
        assert w.rail.is_empty_state() and w.rail.known_count() > 10
        assert w.rail.summary_text() == "No scan yet"
        assert w.module_stack.currentIndex() == 0              # no module selected yet
        assert "definition pack" in w.trace.toPlainText()
        assert w.transport.isEnabled() and w.iface.isEnabled()
        assert w.windowState() & QtCore.Qt.WindowState.WindowMaximized      # opens maximized
        # the function tabs wait for a session; the state line says so
        assert w.session_state.text() == "Not connected"
        assert not w.adapt_read_btn.isEnabled()
        assert w._connect_hints and not any(h.isHidden() for h in w._connect_hints)
        # the protocol claims nothing before a module has answered; the
        # table's addresses are the tooltip
        w.rail.select("01")
        card = w.rail.card("01")
        assert card is not None and card.kind == "known"
        assert card.chip_text() == "" and "Addresses known" in card.toolTip()
        # the Procedures page is told which module it would run on, and that it has no session
        assert w._proc_ctx_sig is not None and w._proc_ctx_sig[0] == "01" and w._proc_ctx_sig[3] is False
        assert w.module_stack.currentIndex() == 1
        assert "Addresses known" in w.mod_sub.fullText()
        # the workspace: tabs in the plan's order, the tools behind the gear
        names = [w.tabs.tabText(i) for i in range(w.tabs.count())]
        assert names == ["Faults", "Live data", "Adaptation", "Coding", "Actuator tests", "Basic settings",
                         "Identification", "Procedures"]
        assert [w.tools_tabs.tabText(i) for i in range(w.tools_tabs.count())] == [
            "Gateway list", "Vehicle live data", "OBD-II", "Journal", "Garage"]
        assert w.stack.currentIndex() == 0 and w.stack.count() == 3
    finally:
        w.close()


def test_every_table_follows_the_cell_standard(app):
    from ovtsuite.gui_cellmarks import CellDelegate
    from ovtsuite.diag.gui_procedure import ProcedureDialog
    w = _window(app)
    try:
        tables = w.findChildren(QtWidgets.QTableWidget)
        assert len(tables) >= 15
        for t in tables:
            assert t.alternatingRowColors(), t.objectName() or t
            assert isinstance(t.itemDelegate(), CellDelegate)
        dlg = ProcedureDialog({"id": "p", "name": "P", "steps": [{"show": "hi"}]}, "01 Engine", w)
        assert dlg.table.alternatingRowColors()
        assert isinstance(dlg.table.itemDelegate(), CellDelegate)
        dlg.close()
    finally:
        w.close()


# ----------------------------------------------------------------- guards
def test_escape_asks_while_a_session_is_open_and_no_keeps_the_window(app, monkeypatch):
    w = _window(app)
    asked = []

    def question(parent, title, text, *a, **k):
        asked.append(text)
        return QtWidgets.QMessageBox.StandardButton.No
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(question))
    try:
        w._lives["01"] = _FakeLive()
        w.reject()
        app.processEvents()
        assert asked and "Sessions are open" in asked[0]
        assert w.isVisible()
        # nothing busy: no question, the window closes
        w._lives.clear()
        w.reject()
        app.processEvents()
        assert len(asked) == 1
        assert not w.isVisible()
    finally:
        w._lives.clear()
        w.close()


def test_transport_change_refused_while_sessions_are_open(app):
    w = _window(app)
    try:
        assert w.transport.currentData() == "kline"
        w._lives["01"] = _FakeLive()
        w.transport.setCurrentIndex(w.transport.findData("can"))
        app.processEvents()
        assert w.transport.currentData() == "kline"
        assert "Disconnect the live sessions" in w.status.text()
        w._lives.clear()
        w.transport.setCurrentIndex(w.transport.findData("can"))
        assert w.transport.currentData() == "can"
    finally:
        w._lives.clear()
        w.close()


def test_one_shot_refused_while_a_uds_session_is_open(app):
    w = _window(app)
    try:
        w.transport.setCurrentIndex(w.transport.findData("uds"))
        w._lives["01"] = _FakeLive()
        started = []
        w._start(lambda ctx: started.append(1), lambda r: None, "x")
        assert not started and "one session at a time on UDS" in w.status.text()
        assert w._obd_job("read") is None
    finally:
        w._lives.clear()
        w.close()


def test_start_says_busy_instead_of_returning_silently(app):
    w = _window(app)
    try:
        class Running:
            def isRunning(self):
                return True
        w._worker = Running()
        w._busy_what = "Reading generic OBD-II..."
        w._start(lambda ctx: None, lambda r: None, "Clearing...")
        assert w.status.text().startswith("Busy: Reading generic OBD-II")
        w._worker = None
    finally:
        w.close()


# ----------------------------------------------------- vehicle live data
def test_multi_remove_from_a_later_row_of_the_group(app):
    w = _window(app)
    try:
        row = {"addr": "01", "name": "Engine"}
        w._multi_items = [("01", 1, row), ("01", 2, row)]
        w._multi_values = {("01", 1): [("rpm", "800", "01 02 03"), ("load", "12 %", "04 05 06")]}
        w._multi_rebuild_table()
        assert w.multi_table.rowCount() == 3
        assert w.multi_table.item(1, 0).text() == ""          # the label is on the first row only
        w.multi_table.selectRow(1)
        w.multi_remove()
        assert [(a, g) for a, g, _r in w._multi_items] == [("01", 2)]
        assert w.multi_table.rowCount() == 1
    finally:
        w.close()


# ------------------------------------------------------------ keep-alive
def test_keepalive_idle_by_transport_and_protocol():
    from ovtsuite.diag.gui_diag import keepalive_idle

    class S:
        def __init__(self, transport, protocol):
            self.link = type("Lk", (), {"transport": transport})()
            self.protocol = protocol
    assert keepalive_idle(S("can", "kwp2000")) == 0.6
    assert keepalive_idle(S("kline", "kw1281")) == 0.5
    assert keepalive_idle(S("kline", "kwp2000")) == 2.0
    assert keepalive_idle(S("uds", "uds")) == 1.0


# ------------------------------------------------------------- procedures
def test_show_step_stops_the_run_when_the_prompter_was_stopped():
    from ovtsuite.diag import procedures as P
    from ovtsuite.diag.gui_procedure import GuiPrompter
    assert P.Prompter().show("x") is True
    p = GuiPrompter()
    p.stop()
    assert p.show("x") is False

    class Stopped(P.Prompter):
        def show(self, text, rows=None):
            return False
    r = P.ProcedureRun({"id": "p", "name": "P", "steps": [{"show": "hi"}, {"show": "never"}]}, None, None,
                       Stopped()).run()
    assert r.status == "cancelled"


def test_read_faults_connects_once_and_keeps_the_session(app, monkeypatch):
    """Connect-once: Read faults on an unconnected module opens the session
    and runs on it; Read once uses the same session; it stays open."""
    from mock_kw1281 import Link as MockLink, MockKw1281Module
    from ovtsuite.diag import link as L
    ecu = MockKw1281Module(address=0x17, baud=10400)
    opened = []

    def open_link(iface, transport, row, log=None, **kw):
        opened.append(row["addr"])
        return L.Link(MockLink(ecu), [], "kline")
    monkeypatch.setattr(L, "open_link", open_link)
    w = _window(app)
    try:
        w.rail.select("17")
        assert w._current_addr() == "17" and not w._lives
        w.module_op("faults")
        assert "17" in w._lives
        live = w._lives["17"]
        assert _pump(app, lambda: w.fault_table.rowCount() > 0), "no faults arrived"
        assert w.fault_table.item(0, 0).text()                 # the mock's stored code
        m = w._module_info("17")
        assert m is not None and m.reached and m.protocol == "kw1281"
        assert w._lives.get("17") is live and live.isRunning() and live.sess is not None
        assert w.session_state.text().startswith("Session open") and "KW1281" in w.session_state.text()
        assert w.adapt_read_btn.isEnabled() and w._proc_ctx_sig[3] is True
        assert all(h.isHidden() for h in w._connect_hints)
        card = w.rail.card("17")            # answered: the card moved to the module list
        assert card.kind == "module" and card.chip_text().startswith("KW1281") and "live" in card.sub()
        assert w.faults_badge.isVisibleTo(w.tabs) and w.faults_badge._n == w.fault_table.rowCount()
        if m.extra.get("baud"):                                # the session recorded the rate
            assert BAUDS == [("17", int(m.extra["baud"]))]
            assert card.chip_text().startswith("KW1281 @ %d" % int(m.extra["baud"]))
            assert "recorded in your local pack" in w.trace.toPlainText()
        # a second function -- a Live data group ticked -- runs on the same
        # session, no new link: the readings go to the Live data view
        got = []
        w.live_view.set_readings = lambda g, cells: got.append((g, list(cells)))
        w.tabs.setCurrentWidget(w.live_view)
        w.live_view.selectionChanged.emit([1])
        assert _pump(app, lambda: bool(got)), "no group arrived"
        assert got[0][0] == 1 and got[0][1] and got[0][1][0].name
        assert w._live_running and w._live_groups == [1]
        assert opened == ["17"] and len(w._lives) == 1 and w._lives["17"] is live
        assert live.isRunning() and live.sess is not None
        # the transport picker is locked while the session is open
        assert not w.transport.isEnabled()
        # leaving the tab pauses the reads; untick everything ends them
        w.tabs.setCurrentWidget(w.fault_table.parentWidget())
        assert not w._live_running
        w.tabs.setCurrentWidget(w.live_view)
        assert _pump(app, lambda: w._live_running)
        w.live_view.selectionChanged.emit([])
        assert not w._live_running
    finally:
        w.close()
        _pump(app, lambda: False, limit=0.2)


def test_close_releases_a_session_waiting_in_a_prompter(app, monkeypatch):
    """A live session over the KW1281 mock, its thread blocked in a
    GuiPrompter as a procedure would be: closing the window must stop the
    prompter, and the thread with it."""
    from mock_kw1281 import Link as MockLink, MockKw1281Module
    from ovtsuite.diag import link as L
    from ovtsuite.diag.gui_procedure import GuiPrompter
    ecu = MockKw1281Module(address=0x17, baud=10400)
    monkeypatch.setattr(L, "open_link", lambda iface, transport, row, log=None, **kw:
                        L.Link(MockLink(ecu), [], "kline"))
    w = _window(app)
    live = None
    try:
        row = w._res.module_address("17", None)
        live = w.connect_module(row)
        assert live is not None
        assert _pump(app, lambda: live.sess is not None and live.since > 0), "no session"
        assert not w.transport.isEnabled() and not w.iface.isEnabled()
        prompter = GuiPrompter()
        live.prompter = prompter
        answered = []
        live.post("hold", lambda s: answered.append(prompter.confirm("go on?")))
        _pump(app, lambda: False, limit=0.4)        # the thread is now blocked in the prompter
        assert not answered and live.isRunning()
        t = time.time()
        w.close()
        assert time.time() - t < 4.0
        assert not live.isRunning()
        assert answered == [False] and prompter.stopped
        assert not w._lives
        _pump(app, lambda: False, limit=0.2)        # the queued disconnected() and deleteLater
        live = None                                 # the thread object is gone with it
    finally:
        if live is not None:
            try:
                if live.isRunning():
                    live.stop()
                    live.wait(3000)
            except RuntimeError:                    # already deleted
                pass
        w.close()


# ------------------------------------------------- the Live data tab's rotation
class _RotLive:
    """A live session whose posted reads the test runs by hand, one at a time."""
    addr = "01"
    prompter = None
    logged_in = False
    protocol = "kwp2000"
    since = 0.0

    def __init__(self, sess):
        self.sess = sess
        self.queue = []

    def post(self, op, fn):
        self.queue.append((op, fn))

    def stop(self):
        pass

    def wait(self, *_):
        return True


class _RotSession:
    """Group 1: rpm 812 and a coolant 87 degC; group 3: not served."""
    def read_group_full(self, g):
        from ovtsuite.diag.kw1281 import GroupReading
        from ovtsuite.flash.diag import Cell
        if g == 3:
            return GroupReading(group=3, cells=None, available=False)
        return GroupReading(group=g, cells=[Cell(0x01, 20, 203), Cell(0x05, 10, 187)])

    def group_names(self, g):
        return ["Engine speed", None]


def _take(live, w):
    """Run the next posted read and hand its result to the window."""
    op, fn = live.queue.pop(0)
    w._live_result(live, op, fn(live.sess))
    return op


def test_live_rotation_reads_the_ticked_groups_in_turn_and_fills_the_tiles(app):
    w = _window(app)
    try:
        got, rates = [], []
        w.live_view.set_readings = lambda g, cells: got.append((g, list(cells)))
        w.live_view.set_rate = rates.append
        live = _RotLive(_RotSession())
        w._lives["01"] = live
        w.rail.select("01")
        w.tabs.setCurrentWidget(w.live_view)
        assert not w._live_running                              # nothing ticked
        w._live_manual_group(1)
        w._live_manual_group(2)
        assert w._live_groups == [1, 2] and w._live_running
        assert len(live.queue) == 1                             # one read in flight, not one per group
        assert _take(live, w) == "group_live"
        assert got[0][0] == 1
        r = got[0][1]
        assert (r[0].name, r[0].text, r[0].unit, r[0].value) == ("Engine speed", "812", "rpm", 812.0)
        assert r[0].frac == pytest.approx(812 / 6000)                    # the bar: where it sits in 0..6000 rpm
        assert r[1].name == "Cell 2" and r[1].value == pytest.approx(87.0) and r[1].text == "87.0"
        assert r[1].unit and r[1].frac is not None                       # a temperature has a range too
        # the next group is posted when the result arrives; a round is two reads
        assert len(live.queue) == 1
        _take(live, w)
        assert [g for g, _c in got] == [1, 2] and len(rates) == 1 and rates[0] > 0
        _take(live, w)
        assert [g for g, _c in got][-1] == 1                             # round two starts over
        # a result of an older run is dropped and does not post again
        stale = live.queue.pop(0)
        w._live_stop()
        w._live_result(live, stale[0], stale[1](live.sess))
        assert not live.queue and not w._live_running
    finally:
        w._lives.clear()
        w._live_stop()
        w.close()


def test_live_group_not_served_is_unticked_and_marked(app):
    w = _window(app)
    try:
        live = _RotLive(_RotSession())
        w._lives["01"] = live
        w.rail.select("01")
        w.tabs.setCurrentWidget(w.live_view)
        w._live_manual_group(3)
        assert w._live_running
        _take(live, w)
        assert w._live_groups == [] and not w._live_running
        assert w.live_view.checked_groups() == []
        assert not next(e for e in w._live_entries if e.group == 3).served
        assert "not available" in w.status.text()
    finally:
        w._lives.clear()
        w._live_stop()
        w.close()


def test_live_reads_pause_when_the_tab_is_left_and_when_the_window_is_minimized(app):
    w = _window(app)
    try:
        live = _RotLive(_RotSession())
        w._lives["01"] = live
        w.rail.select("01")
        w.tabs.setCurrentWidget(w.live_view)
        w._live_manual_group(1)
        assert w._live_running
        w.tabs.setCurrentWidget(w.fault_table.parentWidget())
        assert not w._live_running
        w.tabs.setCurrentWidget(w.live_view)
        assert w._live_running
        w.stack.setCurrentIndex(2)                               # the vehicle tools cover the workspace
        assert not w._live_running
        w.stack.setCurrentIndex(0)
        assert w._live_running
        w.rail.select("02")                                       # another module: its own groups
        assert not w._live_running
    finally:
        w._lives.clear()
        w._live_stop()
        w.close()


def test_unit_ranges_give_the_tile_bar_and_unknown_units_none():
    from ovtsuite.diag.gui_diag import _strip_unit, _unit_frac
    assert _unit_frac("%", 50) == 0.5 and _unit_frac("%", 150) == 1.0 and _unit_frac("%", -3) == 0.0
    assert _unit_frac("°C", 87.5) == pytest.approx((87.5 + 40) / 255)
    assert _unit_frac("rpm", 3000) == 0.5 and _unit_frac("mbar", 1500) == 0.5
    assert _unit_frac("furlongs", 4) is None and _unit_frac("", 4) is None and _unit_frac("%", None) is None
    assert _strip_unit("812 rpm", "rpm") == "812" and _strip_unit("(37 3 100)", "") == "(37 3 100)"


def test_protocol_text_is_only_what_the_module_answered():
    from ovtsuite.diag.gui_diag import _addr_tags, _protocol_text
    from ovtsuite.diag.model import ModuleInfo
    assert _protocol_text(None) == "" and _protocol_text(ModuleInfo(addr="01")) == ""
    assert _protocol_text(ModuleInfo(addr="01", protocol="kw1281", transport="kline", reached=True,
                                     extra={"baud": 9600})) == "KW1281 @ 9600"
    assert _protocol_text(ModuleInfo(addr="01", protocol="kwp2000", transport="kline", reached=True,
                                     extra={"baud": 10400})) == "KWP2000 @ 10400"
    assert _protocol_text(ModuleInfo(addr="01", protocol="kwp2000", transport="can", reached=True)) == "KWP2000 (TP 2.0)"
    assert _protocol_text(ModuleInfo(addr="01", protocol="uds", transport="uds", reached=True)) == "UDS"
    assert _addr_tags({"kline": {"address": 23}, "tp20": {"dest": 7}}) == ["K-line 0x17", "TP 2.0 0x07"]

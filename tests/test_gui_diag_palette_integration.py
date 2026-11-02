# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag's window with the command palette (Ctrl+K) and the Procedures
page wired in (offscreen): the shortcut and the top field open the overlay,
each kind of choice navigates to its tab and row, Shift+Enter adds a live
group without leaving, an action runs; the Procedures page starts the engine
on a live session over the KW1281 mock, receives the result, and a close with
a question pending returns.
``python -m pytest tests/test_gui_diag_palette_integration.py -q --slow``"""
import inspect
import os
import sys
import time

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore, QtTest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ovtsuite.diag import defs as D  # noqa: E402
from ovtsuite.diag.model import Fault, ModuleInfo  # noqa: E402


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    from ovtsuite.diag import garage as G
    from ovtsuite.diag import gui_diag as GD
    from ovtsuite.diag import learned as LN
    from ovtsuite.diag import link as L
    ini = str(tmp_path / "settings.ini")

    def settings(self):
        s = QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat)
        s.beginGroup(GD.SETTINGS_GROUP)
        return s
    monkeypatch.setattr(GD.DiagWindow, "_settings", settings)
    monkeypatch.setattr(G, "root", lambda: str(tmp_path / "garage"))
    monkeypatch.setattr(L, "interfaces", lambda: [L.Interface(data="mock", label="Mock device", kind="j2534")])
    monkeypatch.setattr(LN, "record_kline_baud", lambda addr, baud, generation="", name="": ("", True))
    # the message boxes the window may raise must never block a test
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: None))


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


def _pack():
    def doc(kind, addr, body):
        return D.Doc(kind, D.Match.from_doc({"addr": addr}), dict(body, kind=kind))
    return D.Pack(id="palette-test", docs=[
        doc("measuring", "01", {"groups": {11: {"title": "Boost", "cells": [
            {"name": "Engine speed", "unit": "rpm"}, {"name": "Boost setpoint"}, {"name": "Boost actual"},
            {"name": "Boost duty"}]}}}),
        doc("coding", "19", {"style": "long", "bytes": [
            {"index": 2, "bits": [{"id": "coding.b2.bit3", "bit": 3, "name": "Rain sensor fitted",
                                   "type": "bool"}]}]}),
        doc("adaptation", "17", {"channels": [
            {"id": "adaptation.ch02", "channel": 2, "name": "Distance since service", "unit": "km"},
            {"id": "adaptation.ch40", "channel": 40, "name": "Max distance", "unit": "km"}]}),
        doc("actuator_tests", "01", {"tests": [{"id": "actuator.egr", "tester_id": 7, "name": "EGR valve"},
                                               {"id": "actuator.boost", "tester_id": 9, "name": "Boost valve"}]}),
    ])


def _state(w):
    """Hand-built state: the bundled packs plus one of the test's own, three
    reached modules, the engine with two faults."""
    w._res = D.Resolver(list(w._packs) + [_pack()])
    eng = ModuleInfo("01", "Engine", protocol="kwp2000", transport="can", part="03G906016LQ",
                     identity_text="03G906016LQ", reached=True)
    eng.faults = [Fault(code=0x0414, status=0x2F, vag=1044, sae="P0299", text="Charge pressure too low",
                        status_text="stored"),
                  Fault(code=0x2299, vag=8857, text="Boost valve", intermittent=True)]
    gw = ModuleInfo("19", "Gateway", protocol="uds", transport="uds", part="1K0907530", reached=True,
                    coding_bytes="000000")
    cl = ModuleInfo("17", "Instruments", protocol="kwp2000", transport="can", part="4F0910930", reached=True)
    w._modules = [eng, cl, gw]
    w._rebuild_rail()
    return eng, cl, gw


def _item(w, kind, **match):
    for i in w._palette_items():
        if i.kind == kind and all(i.payload.get(k, getattr(i, k, None)) == v for k, v in match.items()):
            return i
    raise AssertionError("no %s item %r in %s" % (kind, match, [(i.kind, i.title) for i in w._palette_items()][:40]))


def _real_page():
    from ovtsuite.diag.gui_procedures_page import ProceduresPage
    return "NotImplementedError" not in inspect.getsource(ProceduresPage.begin_run)


# ------------------------------------------------------------------ opening
def test_ctrl_k_and_the_top_field_open_the_palette_with_the_prefill(app):
    w = _window(app)
    try:
        _state(w)
        calls = []
        orig = w.palette.popup
        w.palette.popup = lambda items, prefill="": (calls.append((list(items), prefill)), orig(items, prefill))[1]
        assert not w.palette.is_open()
        QtTest.QTest.keyClick(w, QtCore.Qt.Key.Key_K, QtCore.Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert w.palette.is_open() and calls[-1][1] == ""
        kinds = {i.kind for i in calls[-1][0]}
        assert {"module", "live", "fault", "coding", "adaptation", "actuator", "procedure", "action"} <= kinds
        QtTest.QTest.keyClick(w, QtCore.Qt.Key.Key_K, QtCore.Qt.KeyboardModifier.ControlModifier)     # toggles
        app.processEvents()
        assert not w.palette.is_open()
        # text typed in the top field carries over and the field empties
        w.search.setText("boost")
        assert w.palette.is_open() and calls[-1][1] == "boost" and w.search.text() == ""
        w.palette.close_palette()
        # a click on the field opens it
        QtTest.QTest.mouseClick(w.search, QtCore.Qt.MouseButton.LeftButton)
        app.processEvents()
        assert w.palette.is_open() and len(calls) == 3
        # the items are built fresh on every open
        w.palette.close_palette()
        w._modules = [m for m in w._modules if m.addr != "17"]
        w._open_palette()
        assert not [i for i in calls[-1][0] if i.kind == "module" and i.addr == "17"]
    finally:
        w.close()


def test_escape_closes_the_palette_not_the_window(app):
    w = _window(app)
    try:
        w._open_palette()
        assert w.palette.is_open()
        QtTest.QTest.keyClick(w, QtCore.Qt.Key.Key_Escape)
        app.processEvents()
        assert not w.palette.is_open() and w.isVisible()
        assert w.palette.parent() is w                       # an overlay of the window, not a window of its own
    finally:
        w.close()


# ----------------------------------------------- through the real palette widget
def test_typing_a_query_and_pressing_enter_routes_the_choice(app):
    w = _window(app)
    try:
        _state(w)
        w.connect_module = lambda row: None
        w._open_palette()
        field = w.palette.search_field()
        field.setText("P0299")                                           # an SAE code finds the fault
        top = w.palette.hit_items()
        assert top and top[0].kind == "fault" and top[0].title == "Charge pressure too low"
        QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return)
        app.processEvents()
        assert not w.palette.is_open()
        assert w.tabs.currentWidget() is w._pages["Faults"] and w.fault_table.currentRow() == 0
        assert not w.search.hasFocus()                                   # the field would open it again
        # the VAG number in either padding, and a measuring value by name
        w._open_palette("008857")
        assert [i.title for i in w.palette.hit_items() if i.kind == "fault"] == ["Boost valve"]
        field = w.palette.search_field()
        field.setText("01: boost actual")
        assert w.palette.hit_items()[0].kind == "live"
        QtTest.QTest.keyClick(field, QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ShiftModifier)
        app.processEvents()
        assert w.tabs.currentWidget() is w._pages["Faults"]              # Shift+Enter: nowhere new
        assert w.live_view.checked_groups() == [11] and w.palette.is_open()
        assert w.palette.query() == "01: boost actual"                   # still there, to add another
        QtTest.QTest.keyClick(w.palette.search_field(), QtCore.Qt.Key.Key_Escape)
        app.processEvents()
        assert not w.palette.is_open() and w.isVisible()
    finally:
        w.close()


# ------------------------------------------------------------------ routing
def test_module_choice_selects_it_and_shows_the_workspace_without_connecting(app, monkeypatch):
    w = _window(app)
    try:
        _state(w)
        w.connect_module = lambda row: pytest.fail("a module choice must not connect")
        w.stack.setCurrentWidget(w.report_page)
        w._palette_activated(_item(w, "module", addr="17"), False)
        assert w.stack.currentIndex() == 0 and w._current_addr() == "17" and not w._lives
        assert w.module_stack.currentIndex() == 1 and w.mod_title.text().startswith("17")
    finally:
        w.close()


def test_live_choice_opens_the_live_tab_with_the_group_ticked_and_the_tile_flashed(app):
    w = _window(app)
    try:
        _state(w)
        opened = []
        w.connect_module = lambda row: opened.append(row["addr"])      # the usual tick connects; not under test here
        it = _item(w, "live", addr="01", group=11, cell=3)
        assert it.title == "Boost actual" and it.subtitle == "Engine · group 011 cell 3"
        w._palette_activated(it, False)
        assert w._current_addr() == "01" and w.tabs.currentWidget() is w.live_view
        assert w.live_view.checked_groups() == [11] and w._live_checked["01"] == [11]
        idx = w.live_view.tile_groups().index((11, 2))
        assert w.live_view.tiles()[idx]._selected                        # the cell's tile is flashed
        assert opened == ["01"]                                         # ticking a group connects, as on the tab
    finally:
        w.close()


def test_shift_enter_adds_the_group_without_navigating_or_connecting(app):
    w = _window(app)
    try:
        _state(w)
        w.connect_module = lambda row: pytest.fail("adding a group must not connect")
        w.rail.select("17")
        w.tabs.setCurrentWidget(w._pages["Coding"])
        it = _item(w, "live", addr="01", group=11, cell=1)
        w._palette_activated(it, True)                                   # another module: remembered for it
        assert w._current_addr() == "17" and w.tabs.currentWidget() is w._pages["Coding"]
        assert w.palette.is_open()                                       # it stays: more values can be added
        assert w._live_checked["01"] == [11] and not w._lives
        w.rail.select("01")                                              # shown now: its group is already ticked
        assert w.live_view.checked_groups() == [11]
        w.live_view.row(11).checkbox().setChecked(False)
        w.tabs.setCurrentWidget(w._pages["Faults"])
        w._palette_activated(it, True)                                   # the shown module, no session
        assert w.live_view.checked_groups() == [11] and w.tabs.currentWidget() is w._pages["Faults"]
        assert not w._lives
    finally:
        w.close()


def test_fault_choice_selects_the_row_in_the_faults_tab(app):
    w = _window(app)
    try:
        _state(w)
        it = _item(w, "fault", code=0x2299)
        assert it.chip == "history"
        w._palette_activated(it, False)
        assert w._current_addr() == "01" and w.tabs.currentWidget() is w._pages["Faults"]
        assert w.fault_table.rowCount() == 2 and [i.row() for i in w.fault_table.selectedItems()] == [1] * 4 \
            or w.fault_table.currentRow() == 1
        assert w.fault_table.currentRow() == 1
    finally:
        w.close()


def test_coding_adaptation_and_actuator_choices_open_their_tabs_and_rows(app):
    w = _window(app)
    try:
        _state(w)
        w._palette_activated(_item(w, "coding", byte=2, bit=3), False)
        assert w._current_addr() == "19" and w.tabs.currentWidget() is w._pages["Coding"]
        assert w.long_coding_bits.rowCount() >= 1 and w.long_coding_bits.currentRow() >= 0
        assert w.long_coding_bits.item(w.long_coding_bits.currentRow(), 1).text() == "Rain sensor fitted"
        w._palette_activated(_item(w, "adaptation", channel=40), False)
        assert w._current_addr() == "17" and w.tabs.currentWidget() is w._pages["Adaptation"]
        assert w.adapt_ch.value() == 40 and w.adapt_defs.item(w.adapt_defs.currentRow(), 0).text() == "040"
        it = _item(w, "actuator", id="actuator.boost")
        assert it.title == "Actuator test: Boost valve"
        w._palette_activated(it, False)
        assert w._current_addr() == "01" and w.tabs.currentWidget() is w._pages["Actuator tests"]
        assert w.tabs.tabText(w.tabs.currentIndex()) == "Actuator tests"
        assert w.act_defs.item(w.act_defs.currentRow(), 1).text() == "Boost valve"
    finally:
        w.close()


def test_procedure_choice_opens_the_procedures_tab_and_selects_it(app):
    w = _window(app)
    try:
        _state(w)
        picked = []
        w.proc_page.select = picked.append
        it = _item(w, "procedure", id="procedure.engine.boost-check")
        assert it.title.startswith("Procedure: ")
        w._palette_activated(it, False)
        assert w._current_addr() == "01" and w.tabs.currentWidget() is w.proc_page
        assert picked == ["procedure.engine.boost-check"]
    finally:
        w.close()


def test_actions_run_the_window_methods(app, monkeypatch):
    w = _window(app)
    try:
        _state(w)
        w._palette_activated(_item(w, "action", action="open_garage"), False)
        assert w.stack.currentIndex() == 2 and w.tools_tabs.currentWidget() is w._tool_pages["Garage"]
        w._palette_activated(_item(w, "action", action="open_journal"), False)
        assert w.tools_tabs.currentWidget() is w._tool_pages["Journal"]
        w._palette_activated(_item(w, "action", action="trace_toggle"), False)
        assert w.record_chk.isChecked() and w.stack.currentIndex() == 2     # an action leaves the page alone
        assert _item(w, "action", action="trace_toggle").title == "Stop trace recording"
        w._palette_activated(_item(w, "action", action="trace_toggle"), False)
        assert not w.record_chk.isChecked()
        ran = []
        monkeypatch.setattr(type(w), "scan_all", lambda self: ran.append("scan"))
        monkeypatch.setattr(type(w), "reload_definitions", lambda self, quiet=False: ran.append("reload"))
        w._palette_activated(_item(w, "action", action="scan_all"), False)
        w._palette_activated(_item(w, "action", action="reload_definitions"), False)
        assert ran == ["scan", "reload"]
        w.rail.select("17")
        toggled = []
        w.toggle_connect = lambda: toggled.append(w._current_addr())
        w._palette_activated(_item(w, "action", action="connect"), False)
        assert toggled == ["17"]
    finally:
        w.close()


def test_a_choice_that_fails_is_reported_and_does_not_break_the_window(app):
    from ovtsuite.diag.palette_index import PaletteItem
    w = _window(app)
    try:
        _state(w)
        w._palette_activated(PaletteItem("fault", "x", addr="01", payload={"code": "not a number", "index": "bad"}), False)
        w._palette_activated(PaletteItem("module", "nobody", addr="ZZ"), False)
        w._palette_activated(PaletteItem("action", "x", payload={"action": "no_such_action"}), False)
        assert w.isVisible()
    finally:
        w.close()


# --------------------------------------------------------------- live values
def test_the_live_readings_reach_the_items_and_an_open_palette(app):
    from ovtsuite.diag.gui_live_view import Reading

    class Cell:
        decoded = (1012.0, "mbar", "1012 mbar")
        fid = a = b = 0

    class R:
        available = True
        cells = [Cell(), Cell()]
        raw = text = None
    w = _window(app)
    try:
        _state(w)
        w.rail.select("01")
        seen = []
        w.palette.set_values = seen.append
        w._open_palette()
        w.live_view.set_groups([__import__("ovtsuite.diag.gui_live_view", fromlist=["GroupEntry"]).GroupEntry(
            11, "Boost", ["Engine speed", "Boost setpoint"])], [11])
        w._live_deliver(11, R(), ["Engine speed", "Boost setpoint"])
        assert w._palette_readings[("01", 11, 1)] == ("1012", "mbar")
        assert seen and seen[-1][("01", "live", "11.2")] == "1012 mbar"
        w.palette.close_palette()
        assert _item(w, "live", addr="01", group=11, cell=1).value == "1012 mbar"
        # unticking the group, or the session ending, forgets the readings
        w._live_selection_changed([])
        assert not w._palette_readings
        assert Reading                                              # the tile type the feed shares
    finally:
        w.close()


# ------------------------------------------------------ the Procedures page
def test_the_page_is_told_the_procedures_and_the_module(app):
    w = _window(app)
    try:
        got = []
        orig = w.proc_page.set_procedures
        w.proc_page.set_procedures = lambda docs, origins, applicable=None: (got.append((docs, origins, applicable)),
                                                                              orig(docs, origins, applicable))[1]
        ctx = []
        w.proc_page.set_context = lambda *a, **k: ctx.append((a, k))
        w.rail.select("01")
        docs, origins, applicable = got[-1]
        ids = {d["id"] for d in docs}
        assert "procedure.engine.boost-check" in ids and "procedure.engine.boost-check" in applicable
        assert set(origins) >= ids and set(origins.values()) == {"bundled"}
        assert applicable <= ids
        assert ctx[-1] == (("01", w._row_for("01")["name"], "", False, False), {"part": ""})
        w.rail.select("17")
        assert "procedure.engine.boost-check" not in got[-1][2]          # an engine procedure is not for the cluster
        assert ctx[-1][0][0] == "17"
        w.reload_definitions(quiet=True)                                 # refreshed on a definitions reload
        assert len(got) >= 3
    finally:
        w.close()


def test_new_procedure_opens_the_definitions_folder_and_names_the_help(app, monkeypatch):
    w = _window(app)
    try:
        opened, said = [], []
        monkeypatch.setattr(type(w), "open_defs_folder", lambda self: opened.append(1))
        monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: said.append(a[-1])))
        w.proc_page.newProcedureRequested.emit()
        assert opened == [1] and said and "Procedures" in said[0] and "procedure" in said[0]
    finally:
        w.close()


def test_show_trace_expands_the_bottom_pane_and_records(app):
    w = _window(app)
    try:
        w.vsplit.setSizes([w.vsplit.height(), 0])
        assert w.vsplit.sizes()[1] == 0 and not w.record_chk.isChecked()
        w.proc_page.traceRequested.emit()
        assert w.vsplit.sizes()[1] >= 80 and w.record_chk.isChecked()
        w.record_chk.setChecked(False)
    finally:
        w.close()


class _Answering:
    """The page's stand-in prompter wiring: answers a ``show`` at once."""


def _kw1281_session(app, monkeypatch, w):
    from mock_kw1281 import Link as MockLink, MockKw1281Module
    from ovtsuite.diag import link as L
    ecu = MockKw1281Module(address=0x17, baud=10400)
    monkeypatch.setattr(L, "open_link", lambda iface, transport, row, log=None, **kw:
                        L.Link(MockLink(ecu), [], "kline"))
    row = w._res.module_address("17", None)
    w.rail.select("17")
    live = w.connect_module(row)
    assert live is not None
    assert _pump(app, lambda: live.sess is not None and live.since > 0), "no session"
    return live


def _stub_page(w, answer):
    """The window's wiring of the page, with the page's own run methods
    replaced by recorders: ``begin_run`` hands out a real GuiPrompter, which
    ``answer`` (if given) answers."""
    from ovtsuite.diag.gui_procedure import GuiPrompter
    log = {"begin": [], "finish": [], "abort": 0, "prompter": None}

    def begin(doc):
        p = GuiPrompter()
        if answer:
            p.ask.connect(lambda kind, payload: p.answer(True), QtCore.Qt.ConnectionType.DirectConnection)
        log["begin"].append(doc)
        log["prompter"] = p
        return p
    w.proc_page.begin_run = begin
    w.proc_page.finish_run = lambda r: log["finish"].append(r)
    orig_abort = w.proc_page.abort

    def abort():
        log["abort"] += 1
        if log["prompter"] is not None:
            log["prompter"].stop()
        orig_abort()
    w.proc_page.abort = abort
    return log


def test_run_requested_starts_the_engine_on_the_session_and_the_result_reaches_the_page(app, monkeypatch):
    w = _window(app)
    live = None
    try:
        live = _kw1281_session(app, monkeypatch, w)
        log = _stub_page(w, answer=True)
        doc = {"id": "t.simple", "name": "Simple", "safety": "read-only", "steps": [{"show": "hello"}]}
        w.proc_page.runRequested.emit(doc)
        assert log["begin"] == [doc] and live.prompter is log["prompter"]      # the session can release it on a stop
        assert _pump(app, lambda: bool(log["finish"])), "the result never reached the page"
        res = log["finish"][0]
        assert res.procedure_id == "t.simple" and res.status == "done"
        assert live.prompter is None
        assert "Procedure Simple: done" in w.status.text()
        # one at a time: a run in progress refuses another
        live.prompter = object()
        w.proc_page.runRequested.emit(doc)
        assert len(log["begin"]) == 1 and "already running" in w.status.text()
        live.prompter = None
    finally:
        w.close()
        _pump(app, lambda: False, limit=0.2)


def test_run_requested_without_a_session_connects_first_and_runs_nothing(app, monkeypatch):
    w = _window(app)
    try:
        w.rail.select("17")
        w.connect_module = lambda row: object()                    # opening a session is not under test
        log = _stub_page(w, answer=False)
        w.proc_page.runRequested.emit({"id": "t", "name": "T", "steps": []})
        assert not log["begin"] and "Connecting" in w.status.text()
    finally:
        w.close()


def test_close_with_a_question_pending_returns_and_leaves_no_thread(app, monkeypatch):
    w = _window(app)
    live = None
    try:
        live = _kw1281_session(app, monkeypatch, w)
        log = _stub_page(w, answer=False)                          # nobody answers: the engine waits in show()
        w.proc_page.runRequested.emit({"id": "t.wait", "name": "Wait", "safety": "read-only",
                                       "steps": [{"show": "stop here"}]})
        _pump(app, lambda: False, limit=0.4)
        assert live.isRunning() and live.prompter is log["prompter"] and not log["finish"]
        t = time.time()
        w.close()
        assert time.time() - t < 4.0
        assert log["abort"] >= 1 and log["prompter"].stopped
        assert not live.isRunning() and not w._lives
        _pump(app, lambda: False, limit=0.2)
        live = None
    finally:
        if live is not None:
            try:
                if live.isRunning():
                    live.stop()
                    live.wait(3000)
            except RuntimeError:
                pass
        w.close()


def test_disconnect_with_a_question_pending_aborts_the_page_first(app, monkeypatch):
    w = _window(app)
    live = None
    try:
        live = _kw1281_session(app, monkeypatch, w)
        log = _stub_page(w, answer=False)
        w.proc_page.runRequested.emit({"id": "t.wait", "name": "Wait", "safety": "read-only",
                                       "steps": [{"show": "stop here"}]})
        _pump(app, lambda: False, limit=0.4)
        w.disconnect_all()
        assert log["abort"] >= 1
        assert _pump(app, lambda: not w._lives), "the session did not end"
    finally:
        w.close()
        _pump(app, lambda: False, limit=0.2)


@pytest.mark.skipif(not _real_page(), reason="ProceduresPage is still the placeholder (begin_run not implemented)")
def test_the_real_page_runs_a_procedure_over_the_mock(app, monkeypatch):
    w = _window(app)
    try:
        live = _kw1281_session(app, monkeypatch, w)
        done = []
        w.proc_page.finish_run = lambda r: (done.append(r), type(w.proc_page).finish_run(w.proc_page, r))[1]
        doc = {"id": "t.simple", "name": "Simple", "safety": "read-only", "steps": [{"wait": {"seconds": 0.05}}]}
        w.proc_page.runRequested.emit(doc)
        assert w.proc_page.is_running() or done
        assert _pump(app, lambda: bool(done)), "no result"
        assert done[0].status == "done" and not w.proc_page.is_running() and live.prompter is None
    finally:
        w.close()
        _pump(app, lambda: False, limit=0.2)

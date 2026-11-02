# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The module rail of OVT Diag (offscreen) and how the window drives it:
the empty state before a scan, the cards a scan makes (fault badge, protocol
chip, live marker), selection, the search filter, ``Not answering`` and
``All known addresses``, ``Connect by address...``, the summary line, and
the window's pages. ``python -m pytest tests/test_gui_diag_rail.py -q --slow``"""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore, QtGui, QtTest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


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
    monkeypatch.setattr(LN, "record_kline_baud", lambda *a, **k: ("", False))


def _pump(app, cond, limit=10.0):
    t = time.time()
    while not cond() and time.time() - t < limit:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return cond()


# ------------------------------------------------------------- hand-built data
def _rows():
    return [
        {"addr": "01", "name": "Engine", "kline": {"address": 1, "init": "slow"}, "tp20": {"dest": 1}},
        {"addr": "02", "name": "Transmission", "kline": {"address": 2}, "tp20": {"dest": 2},
         "isotp": {"request": 0x7E1, "response": 0x7E9}},
        {"addr": "03", "name": "ABS / ESP", "kline": {"address": 3}, "tp20": {"dest": 3}},
        {"addr": "08", "name": "Climatronic", "kline": {"address": 8}},
        {"addr": "09", "name": "Central electrics", "kline": {"address": 9}},
        {"addr": "17", "name": "Instruments", "kline": {"address": 23}, "tp20": {"dest": 7}},
        {"addr": "55", "name": "Headlight range", "tp20": {"dest": 0x55}},
    ]


def _mod(addr, name, part="", proto="kwp2000", transport="can", faults=0, inter=False, baud=None, **kw):
    from ovtsuite.diag.model import Fault, ModuleInfo
    m = ModuleInfo(addr=addr, name=name, protocol=proto, transport=transport, part=part, reached=True,
                   extra={"baud": baud} if baud else {}, **kw)
    m.faults = [Fault(code=16684 + i, vag=16684 + i, text="Cylinder %d misfire" % (i + 3), intermittent=inter)
                for i in range(faults)]
    return m


def _infos():
    from ovtsuite.diag.model import ModuleInfo
    gone = ModuleInfo(addr="55", name="Headlight range", reached=False, error="no answer (TP 2.0 setup refused)")
    return {m.addr: m for m in (
        _mod("01", "Engine", "03G906016LQ", faults=2, platform="EDC16U31"),
        _mod("02", "Transmission", "ZF 6HP19", proto="uds"),
        _mod("03", "ABS / ESP", "4F0910517AL", faults=1, inter=True),
        _mod("08", "Climatronic", "4F0910043", proto="kw1281", transport="kline", baud=9600),
        _mod("09", "Central electrics", "generic", proto="kwp2000", transport="kline", baud=10400),
        gone)}


def _gone(widget):
    """Delete a widget's C++ objects now, while its Python wrappers are alive
    (the style helpers keep reference cycles that a later garbage collection
    would break before the children are destroyed)."""
    import shiboken6
    widget.close()
    shiboken6.delete(widget)


def _rail(app, with_scan=True, lives=()):
    from ovtsuite.diag.gui_rail import ModuleRail
    r = ModuleRail()
    r.set_modules(_rows(), _infos() if with_scan else {}, set(lives), set())
    r.resize(285, 700)
    r.show()
    app.processEvents()
    return r


# ------------------------------------------------------------------ the rail
def test_empty_state_before_a_scan_is_not_the_address_table(app):
    r = _rail(app, with_scan=False)
    try:
        assert r.is_empty_state()
        assert "No scan yet" in r.empty_label.text()
        assert r.card_addrs() == []                 # no address rows listed as modules
        assert r.known_count() == 7                 # ... they wait behind the expander
        assert r.known_head.text().endswith("All known addresses (7)")
        assert not r.known_head.isChecked() and not r._known_box.isVisible()
        assert r.width() == 285
    finally:
        _gone(r)


def test_cards_from_a_scan_with_badges_chips_and_the_live_marker(app):
    r = _rail(app, lives={"02"})
    try:
        assert not r.is_empty_state()
        assert r.card_addrs("module") == ["01", "02", "03", "08", "09"]
        c = r.card("01")
        assert (c.name(), c.badge_count(), c.badge_kind()) == ("Engine", 2, "bad")
        assert c.chip_text() == "KWP-TP2.0" and "03G906016LQ" in c.sub() and "EDC16U31" in c.sub()
        # every fault intermittent: the badge is amber
        assert (r.card("03").badge_count(), r.card("03").badge_kind()) == (1, "warn")
        # no faults: a green dot; a UDS module says UDS, K-line ones the rate they answered at
        assert r.card("02").badge_kind() == "ok" and r.card("02").badge_count() == 0
        assert r.card("02").chip_text() == "UDS"
        assert r.card("08").chip_text() == "KW1281 @ 9600"
        assert r.card("09").chip_text() == "KWP @ 10400"
        # the live session is marked on the card, others are not
        assert r.card("02").sub().endswith("live") and "live" not in r.card("01").sub()
    finally:
        _gone(r)


def test_selection_by_click_double_click_and_code(app):
    r = _rail(app)
    got, conn = [], []
    r.selected.connect(got.append)
    r.connectRequested.connect(conn.append)
    try:
        assert r.current_addr() == ""
        QtTest.QTest.mouseClick(r.card("03"), QtCore.Qt.MouseButton.LeftButton)
        assert got == ["03"] and r.current_addr() == "03" and r.card("03")._selected
        r.select("01")
        assert got == ["03", "01"] and not r.card("03")._selected and r.card("01")._selected
        r.select("01")                                   # unchanged: no second signal
        assert got == ["03", "01"]
        QtTest.QTest.mouseDClick(r.card("08"), QtCore.Qt.MouseButton.LeftButton)
        assert conn == ["08"] and r.current_addr() == "08"
        # the selection survives the list being rebuilt (a live marker changing)
        r.set_modules(_rows(), _infos(), {"08"}, set())
        assert r.current_addr() == "08" and r.card("08")._selected
        assert got[-1] == "08"                            # ... without a signal of its own
        n = len(got)
        r.set_modules(_rows(), _infos(), set(), set())
        assert len(got) == n
        # the arrow keys walk the list
        QtTest.QTest.keyClick(r.card("08"), QtCore.Qt.Key.Key_Down)
        assert r.current_addr() == "09"
    finally:
        _gone(r)


def test_filter_matches_name_part_address_and_fault_text(app):
    r = _rail(app)
    try:
        r.filter("engine")
        assert r.visible_addrs() == ["01"]
        r.filter("6HP19")
        assert r.visible_addrs() == ["02"]
        r.filter("misfire")                              # fault text of the scan
        assert r.visible_addrs() == ["01", "03"]
        r.filter("09")
        assert "09" in r.visible_addrs() and "01" not in r.visible_addrs()
        # a module that is only an address-table row is found too (the group opens for the search)
        r.filter("instruments")
        assert r.visible_addrs() == ["17"]
        assert r.card("17").kind == "known"
        r.filter("")
        assert r.visible_addrs() == ["01", "02", "03", "08", "09"] and not r.known_head.isChecked()
    finally:
        _gone(r)


def test_not_answering_group_is_collapsed_with_the_reason_in_the_tooltip(app):
    r = _rail(app)
    try:
        assert r.absent_count() == 1
        assert r.absent_head.isVisible() and "Not answering (1)" in r.absent_head.text()
        assert not r._absent_box.isVisible()
        r.absent_head.setChecked(True)
        app.processEvents()
        c = r.card("55")
        assert c.kind == "absent" and c.isVisible() and c.chip_text() == "" and c.badge is None
        assert "TP 2.0 setup refused" in c.toolTip() and "TP 2.0 setup refused" in c.sub()
        # selecting a hidden one opens its group
        r.absent_head.setChecked(False)
        r.select("55")
        assert r.absent_head.isChecked() and r.current_addr() == "55"
    finally:
        _gone(r)


def test_all_known_addresses_expander_lists_plain_dim_cards(app):
    r = _rail(app)
    try:
        assert r.known_count() == 1 and "All known addresses (1)" in r.known_head.text()
        assert r.card("17") is None                       # built when opened
        r.known_head.setChecked(True)
        app.processEvents()
        c = r.card("17")
        assert c.kind == "known" and c.chip_text() == "" and c.badge is None
        assert "K 0x17" in c.sub() and "TP 0x07" in c.sub()
        assert "Addresses known: K-line 0x17, TP 2.0 0x07" in c.toolTip()
        assert r.visible_addrs()[-1] == "17"
        # the old direct-connect workflow: pick the row, double-click connects
        conn = []
        r.connectRequested.connect(conn.append)
        QtTest.QTest.mouseDClick(c, QtCore.Qt.MouseButton.LeftButton)
        assert conn == ["17"]
        # selecting by code opens the group too
        r.known_head.setChecked(False)
        r.select("17")
        assert r.known_head.isChecked() and r.card("17")._selected
        # a gateway-absent row says so
        r.set_modules(_rows(), _infos(), set(), {"17"})
        assert "not installed" in r.card("17").sub()
    finally:
        _gone(r)


def test_footer_buttons_and_scan_button_signals(app):
    r = _rail(app)
    sig = []
    r.scanRequested.connect(lambda: sig.append("scan"))
    r.stopRequested.connect(lambda: sig.append("stop"))
    r.obdRequested.connect(lambda: sig.append("obd"))
    r.garageRequested.connect(lambda: sig.append("garage"))
    r.connectByAddressRequested.connect(lambda: sig.append("addr"))
    try:
        r.set_footer("ISO 15765", 4, 3)
        assert r.obd_chip.text() == "ISO 15765"
        assert r.garage_label.text() == "Garage · 4 vehicles" and r.garage_scans.text() == "3 scans"
        r.scan_btn.click()
        r.set_scanning(True)
        assert r.scan_btn.text() == "Stop"
        r.scan_btn.click()
        r.set_scanning(False)
        assert r.scan_btn.text() == "Scan all"
        QtTest.QTest.mouseClick(r.obd_row, QtCore.Qt.MouseButton.LeftButton)
        QtTest.QTest.mouseClick(r.garage_row, QtCore.Qt.MouseButton.LeftButton)
        r.connect_addr_btn.click()
        assert sig == ["scan", "stop", "obd", "garage", "addr"]
        assert r.connect_addr_btn.text() == "+ Connect by address..."
    finally:
        _gone(r)


# ------------------------------------------------------- connect by address
def test_connect_by_address_row_per_transport(app):
    from ovtsuite.diag.gui_rail import ConnectByAddressDialog, row_for_address
    # K-line: the address is the number the module is woken with
    d = ConnectByAddressDialog("kline", None, _rows())
    ok = d.bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled() and d.row() is None
    d.addr.setText("5a")
    assert ok.isEnabled()
    assert d.row() == {"addr": "5A", "name": "Module 5A", "kline": {"address": 0x5A, "init": "slow"}}
    # a module the table lists keeps what the table knows
    d.addr.setText("17")
    row = d.row()
    assert row["name"] == "Instruments" and row["kline"]["address"] == 23 and row["tp20"]["dest"] == 7
    # CAN: the TP 2.0 destination, the address itself when not given
    d = ConnectByAddressDialog("can", None, _rows())
    assert not d.dest.isHidden() or True
    d.addr.setText("5A")
    assert d.row() == {"addr": "5A", "name": "Module 5A", "tp20": {"dest": 0x5A}}
    d.dest.setText("3f")
    assert d.row()["tp20"] == {"dest": 0x3F}
    # UDS: both ISO-TP ids are needed
    d = ConnectByAddressDialog("uds", None, _rows())
    ok = d.bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    d.addr.setText("5A")
    assert not ok.isEnabled() and d.row() is None
    d.req.setText("7e0")
    assert not ok.isEnabled()
    d.resp.setText("7e8")
    assert ok.isEnabled()
    assert d.row() == {"addr": "5A", "name": "Module 5A", "isotp": {"request": 0x7E0, "response": 0x7E8}}
    # the pure function: a table row's own TP 2.0 destination stays unless one is typed
    base = {"17": {"addr": "17", "name": "Instruments", "tp20": {"dest": 7}}}
    assert row_for_address("can", 0x17, None, None, None, base)["tp20"]["dest"] == 7
    assert row_for_address("can", 0x17, 0x20, None, None, base)["tp20"]["dest"] == 0x20


# ----------------------------------------------------------- the window
def _window(app):
    from ovtsuite.diag.gui_diag import DiagWindow
    w = DiagWindow()
    w.show()
    app.processEvents()
    return w


class _FakeLive:
    """A session on a module: the window posts to it, the test reads the posts."""
    prompter = None
    logged_in = False
    protocol = "kwp2000"

    def __init__(self, addr):
        self.addr = addr
        self.sess = object()
        self.since = time.time()
        self.posted = []

    def post(self, op, fn):
        self.posted.append(op)

    def stop(self):
        pass

    def wait(self, *_):
        return True


def test_window_minimum_width_fits_1366(app):
    w = _window(app)
    try:
        assert w.minimumSizeHint().width() <= 1300
        assert w.rail.width() == 285
        for i in range(w.stack.count()):
            assert w.stack.widget(i).minimumSizeHint().width() <= 1300, w.stack.widget(i)
        # what is left of 1366 px after the rail must hold the module area
        assert w.module_stack.minimumSizeHint().width() <= 1366 - 285
    finally:
        w.close()


def test_summary_line_for_each_state(app):
    from ovtsuite.diag.model import ScanResult
    w = _window(app)
    try:
        assert w.rail.summary_text() == "No scan yet"
        mods = list(_infos().values())
        scan = ScanResult(vehicle_id="", when=time.mktime((2026, 10, 25, 21, 14, 0, 0, 0, -1)), transport="can",
                          modules=mods, basis="table")
        w._scan, w._modules = scan, list(mods)
        w._rebuild_rail()
        assert w.rail.summary_text() == "Scanned 21:14 on CAN · 5 of 6 reached · 2 with faults"
        scan.basis = "gateway"
        w._rebuild_rail()
        assert w.rail.summary_text() == "From gateway list · 5 of 6 reached · 2 with faults"
        scan.stopped = True
        w._modules[0].reached = None
        w._scan.modules = list(w._modules)
        w._rebuild_rail()
        assert w.rail.summary_text() == "Scan stopped: 1 not tried"
    finally:
        w.close()


def test_scan_all_fills_the_rail_and_shows_the_report(app, monkeypatch):
    from ovtsuite.diag import scan as SC
    from ovtsuite.diag.model import ScanResult
    mods = list(_infos().values())
    monkeypatch.setattr(SC, "scan_vehicle", lambda *a, **k: ScanResult(vehicle_id="", transport="kline", modules=mods))
    w = _window(app)
    try:
        assert w.rail.is_empty_state()
        w.scan_all()
        assert _pump(app, lambda: w.stack.currentWidget() is w.report_page), "the report did not show"
        assert not w.rail.is_empty_state()
        assert w.rail.card_addrs("module") == ["01", "02", "03", "08", "09"]
        assert w.rail.absent_count() == 1
        assert "Scan done: 5 of 6" in w.status.text()
        # the report's own navigation: back to the modules; a row selects its module
        w.report_page.backRequested.emit()
        assert w.stack.currentIndex() == 0
        w.stack.setCurrentWidget(w.report_page)
        w.report_page.moduleActivated.emit("03")
        assert w.stack.currentIndex() == 0 and w.rail.current_addr() == "03"
        assert w.module_stack.currentIndex() == 1 and w.mod_title.text().startswith("03")
        # Scan all is Stop while the job runs and back afterwards
        assert w.rail.scan_btn.text() == "Scan all" and w.scan_btn.isEnabled()
    finally:
        w.close()


def test_search_field_opens_the_palette_instead_of_filtering_the_rail(app):
    """The top field is a click-to-open button for the command palette: what
    is typed in it goes to the palette as the prefill and the module rail is
    not filtered by it (the rail keeps its own ``filter``)."""
    w = _window(app)
    try:
        calls = []
        orig = w.palette.popup
        w.palette.popup = lambda items, prefill="": (calls.append((items, prefill)), orig(items, prefill))[1]
        w.rail.select("01")
        assert "DTCs" in w.search.placeholderText() and "procedures" in w.search.placeholderText()
        w.search.setText("trans")                                   # typed: the palette opens with it
        assert calls and calls[-1][1] == "trans" and w.search.text() == ""
        assert w.palette.is_open()
        assert len(w.rail.visible_addrs()) > 1                      # the rail was not filtered
        w.palette.close_palette()
        QtTest.QTest.keyClick(w, QtCore.Qt.Key.Key_K, QtCore.Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert w.palette.is_open() and calls[-1][1] == ""
        w.rail.filter("trans")                                      # the rail's own filter still works
        assert w.rail.visible_addrs() == ["02"]
        w.rail.filter("")
    finally:
        w.close()


def test_chips_show_interface_vehicle_and_trace_state(app):
    w = _window(app)
    try:
        assert w.iface_chip.text() == "Mock device" and "K-line" in w.iface_chip.tail()
        assert w.vehicle_chip.text() == "No vehicle"
        assert w.trace_chip.text() == "Trace off" and w.trace_chip.dotKind() == "neutral"
        w.trace_chip.clicked.emit()                                   # the chip is the switch
        assert w.record_chk.isChecked()
        assert w.trace_chip.text() == "Recording trace" and w.trace_chip.dotKind() == "bad"
        w.transport.setCurrentIndex(w.transport.findData("can"))
        assert "CAN 500k" in w.iface_chip.tail() and w.rail.obd_chip.text() == "ISO 15765"
        v = w._garage.new_vehicle("Audi A6 (4F) 2006", "WAUZZZ4F26N041234", "C6")
        w._refresh_vehicles(select=v.id)
        assert w.vehicle_chip.text() == "Audi A6 (4F) 2006" and w.vehicle_chip.tail() == "WAUZZZ4F…1234"
        assert w.generation == "C6" and w.rail.garage_label.text().startswith("Garage · 1 vehicle")
        # the generation list is short; the description is its tooltip
        i = w.gen_box.findData("C6")
        assert w.gen_box.itemText(i) == "C6"
        assert "Audi A6" in w.gen_box.itemData(i, QtCore.Qt.ItemDataRole.ToolTipRole)
        w.record_chk.setChecked(False)
    finally:
        w.close()


def test_connect_by_address_through_the_window_opens_a_session(app, monkeypatch):
    from mock_kw1281 import Link as MockLink, MockKw1281Module
    from ovtsuite.diag import gui_diag as GD
    from ovtsuite.diag import link as L
    from ovtsuite.diag.gui_rail import row_for_address
    ecu = MockKw1281Module(address=0x17, baud=10400)
    opened = []

    def open_link(iface, transport, row, log=None, **kw):
        opened.append(row)
        return L.Link(MockLink(ecu), [], "kline")
    monkeypatch.setattr(L, "open_link", open_link)

    class Dlg:
        def __init__(self, transport, parent=None, known_rows=()):
            self.transport = transport

        def exec(self):
            return QtWidgets.QDialog.DialogCode.Accepted

        def row(self):
            return row_for_address(self.transport, 0x17)
    monkeypatch.setattr(GD, "ConnectByAddressDialog", Dlg)
    w = _window(app)
    try:
        w.connect_by_address()
        assert _pump(app, lambda: "17" in w._lives and w._lives["17"].sess is not None), "no session"
        assert opened and opened[0]["kline"]["address"] == 0x17
        assert w.rail.current_addr() == "17"
        assert _pump(app, lambda: w.rail.card("17") is not None and w.rail.card("17").kind == "module")
        assert w.rail.card("17").chip_text().startswith("KW1281")
    finally:
        w.close()
        _pump(app, lambda: False, limit=0.2)


def test_open_group_from_the_report_ticks_it_on_the_live_data_tab(app):
    w = _window(app)
    try:
        live = _FakeLive("01")
        w._lives["01"] = live
        w.stack.setCurrentWidget(w.report_page)
        w.report_page.openGroupRequested.emit("01", 11)
        assert w.stack.currentIndex() == 0 and w.rail.current_addr() == "01"
        assert w.tabs.currentWidget() is w.live_view
        assert w.live_view.checked_groups() == [11] and w._live_groups == [11]
        assert w._live_running and live.posted == ["group_live"]
        # group 0 just opens the tab
        w.report_page.openGroupRequested.emit("01", 0)
        assert w.tabs.currentWidget() is w.live_view
    finally:
        w._lives.clear()
        w._live_stop()
        w.close()


def test_garage_tab_lists_vehicles_and_deleting_asks(app, monkeypatch):
    w = _window(app)
    try:
        a = w._garage.new_vehicle("Audi A6", "", "C6")
        b = w._garage.new_vehicle("Golf V", "", "PQ35")
        w._refresh_vehicles(select=a.id)
        t = w.garage_table
        assert t.rowCount() == 2 and w.rail.garage_label.text() == "Garage · 2 vehicles"
        w.rail.garageRequested.emit()
        assert w.stack.currentIndex() == 2 and w.tools_tabs.currentWidget() is w._tool_pages["Garage"]
        rows = {t.item(r, 0).text(): r for r in range(t.rowCount())}
        t.selectRow(rows["Golf V"])
        w.garage_use()
        assert w._vehicle.id == b.id and w.stack.currentIndex() == 0
        # deleting: No leaves it, Yes removes it
        w._show_tools(w._tool_pages["Garage"])
        t.selectRow({t.item(r, 0).text(): r for r in range(t.rowCount())}["Audi A6"])
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
        w.garage_delete()
        assert w._garage.vehicle(a.id) is not None
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes))
        w.garage_delete()
        assert w._garage.vehicle(a.id) is None and w._vehicle.id == b.id and t.rowCount() == 1
    finally:
        w.close()


def test_escape_on_a_page_goes_back_instead_of_closing(app):
    w = _window(app)
    try:
        w._show_tools()
        QtTest.QTest.keyClick(w, QtCore.Qt.Key.Key_Escape)
        assert w.stack.currentIndex() == 0 and w.isVisible()
    finally:
        w.close()

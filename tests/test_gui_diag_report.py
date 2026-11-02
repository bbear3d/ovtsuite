# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The OVT Diag scan report page (offscreen), built from hand-made scans:
the four cards, the module table (rows, fault cards, the change column), the
stopped-scan banner, the empty state, every signal, keyboard, the suite's
table standard, the plain-text report, both palettes.
``python -m pytest tests/test_gui_diag_report.py -q --slow``"""
import os
import time

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore, QtGui                                    # noqa: E402
from PySide6.QtTest import QTest                                     # noqa: E402

Qt = QtCore.Qt


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))


def _mod(addr, name, part="", sw="", coding=None, faults=(), reached=True, **kw):
    from ovtsuite.diag.model import ModuleInfo
    return ModuleInfo(addr, name, protocol="kwp2000", part=part, sw=sw, coding=coding,
                      faults=list(faults), reached=reached, **kw)


def _fault(code, sae, text, status="present, stored", intermittent=False, freeze=None, vag=None):
    from ovtsuite.diag.model import Fault
    return Fault(code=code, sae=sae, text=text, status_text=status, intermittent=intermittent,
                 freeze=dict(freeze or {}), vag=vag)


def _scans(stopped=False, transport="can", trace=""):
    """``(vehicle, now, before, journal)``: 8 modules, 2 with faults (3 in all);
    against the earlier scan: one new fault (+1), two cleared (-2), a coding and
    a lost module."""
    from ovtsuite.diag import journal as J
    from ovtsuite.diag.model import ScanResult, Vehicle
    p0299 = _fault(0x2BB, "P0299", "Boost pressure regulation: control range not reached", vag=8857,
                   freeze={"rpm": "3 210 rpm", "spec": "1 980 mbar"})
    p0401 = _fault(0x191, "P0401", "EGR system: insufficient flow", status="stored", intermittent=True,
                   vag=16785)
    u0100 = _fault(1314, "U0100", "Engine control module: no communication", status="not present now")
    now = ScanResult("a6", when=_ts("2026-10-25 21:14"), interface="J2534", transport=transport, duration_s=48,
                     basis="gateway", trace_path=trace, stopped=stopped)
    now.modules = [
        _mod("01", "Engine", "03G906016LQ", "EDC16U31 8.4", 71, [p0299, p0401]),
        _mod("02", "Transmission", "ZF 6HP19", "0036", 1001),
        _mod("03", "ABS / ESP", "4F0910517AL", "ESP8", None, [u0100]),
        _mod("08", "Climatronic", "4F0910043", "F2MC"),
        _mod("09", "Central electrics", "", "generic KWP", 72),
        _mod("25", "Immobiliser", reached=False, error="no answer"),
        _mod("36", "Seat driver", reached=None if stopped else True),
        _mod("46", "Comfort", reached=None if stopped else True),
    ]
    old = ScanResult("a6", when=_ts("2026-09-14 10:02"), interface="J2534", transport=transport, duration_s=51,
                     basis="gateway")
    old.modules = [
        _mod("01", "Engine", "03G906016LQ", "EDC16U31 8.4", 71,
             [p0401, _fault(0x2C0, "P0100", "Mass air flow sensor")]),
        _mod("02", "Transmission", "ZF 6HP19", "0036", 1001, [_fault(0x2E5, "P0741", "TCC performance")]),
        _mod("03", "ABS / ESP", "4F0910517AL", "ESP8", None, [u0100]),
        _mod("08", "Climatronic", "4F0910043", "F2MC"),
        _mod("09", "Central electrics", "", "generic KWP", 70),
        _mod("25", "Immobiliser"),
        _mod("36", "Seat driver"),
        _mod("46", "Comfort"),
    ]
    veh = Vehicle("a6", name="Audi A6 (4F)", vin="WAUZZZ4F26N123456")
    jr = [J.Entry(when=_ts("2026-10-02 18:40"), addr="09", module="Central electrics", function="coding",
                  before=70, after=72),
          J.Entry(when=_ts("2026-10-16 09:12"), addr="02", module="Transmission", function="clear_faults",
                  before=[1, 2])]
    return veh, now, old, jr


def _page(app, dark=False, scans=True):
    from ovtsuite import gui_theme
    from ovtsuite.diag.gui_report_page import ReportPage
    app.setPalette(gui_theme.dark_palette() if dark else QtGui.QPalette())
    w = ReportPage()
    w.resize(1400, 900)
    if scans:
        w.set_scans([("2026-10-25 21:14", "k2"), ("2026-09-14 (before retune)", "k1")], "k2", "k1")
    return w


def _show(app, w, *args):
    w.show_report(*args)
    w.show()
    for _ in range(4):
        app.processEvents()


def _card(w, i):
    c = w.cards[i]
    return c._title, c._value, c._suffix, c._value_kind, c._lines


def _row_of(w, addr):
    return next(r for r, a in w._rows.items() if a == addr)


def _change_item(w, addr):
    from ovtsuite.diag.gui_report_page import C_CHANGE
    return w.table.item(_row_of(w, addr), C_CHANGE)


# ----------------------------------------------------------------- the cards
def test_cards_with_comparison(app):
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    t, v, s, k, lines = _card(w, 0)
    assert (t, v, s) == ("Modules reached", "7", "/ 8 in gateway list")        # one answered not
    t, v, s, k, lines = _card(w, 1)
    assert (t, v, s, k) == ("Faults now", "3", "in 2 modules", "bad")
    t, v, s, k, lines = _card(w, 2)
    assert t == "Since 2026-09-14"
    assert ("+1 new fault", "bad") in lines
    assert ("-2 cleared", "ok") in lines
    assert ("1 coding changed", "warn") in lines
    assert ("1 no longer answers", "bad") in lines
    t, v, s, k, lines = _card(w, 3)
    assert (t, v) == ("Scan took", "48 s")
    assert lines == [("modules queried in parallel on CAN", "muted")]


def test_cards_without_comparison_and_zero_faults(app):
    veh, now, old, jr = _scans(transport="kline")
    for m in now.modules:
        m.faults = []
    now.basis = "table"
    w = _page(app)
    _show(app, w, veh, now, None, [])
    assert _card(w, 0)[:3] == ("Modules reached", "7", "/ 8 tried")
    t, v, s, k, _ = _card(w, 1)
    assert (v, k) == ("0", "ok")
    t, v, s, k, lines = _card(w, 2)
    assert t == "No comparison" and lines == [("Pick a scan in Compare with", "muted")]
    assert _card(w, 3)[4] == [("one module at a time on K-line", "muted")]
    assert w.journal_box.isHidden()


# ----------------------------------------------------------------- the table
def test_rows_fault_cards_and_changes(app):
    from ovtsuite.diag.gui_report_page import C_CHANGE, C_CODING, C_MODULE, C_PART, C_STATUS, KIND_ROLE
    from ovtsuite.gui_cellmarks import diff_colors
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    tb = w.table
    assert tb.rowCount() == 8 + 2                       # a module row each + a fault row under 01 and 03
    assert len(w._fault_rows) == 2
    assert [len(fc.cards) for fc in w._fault_rows.values()] == [2, 1]
    assert [w._rows[r] for r in sorted(w._rows)] == ["01", "02", "03", "08", "09", "25", "36", "46"]
    r01 = _row_of(w, "01")
    assert tb.item(r01, C_MODULE).text() == "Engine"
    assert tb.item(r01, C_PART).text() == "03G906016LQ \u00b7 EDC16U31 8.4"
    assert tb.item(r01, C_CODING).text() == "0000071"
    assert tb.item(r01, C_STATUS).text() == "2 faults"
    assert tb.item(r01, C_STATUS).data(KIND_ROLE) == "bad"
    assert not tb.item(r01, C_STATUS).icon().isNull()
    assert tb.item(_row_of(w, "02"), C_STATUS).text() == "no faults"
    assert tb.item(_row_of(w, "02"), C_STATUS).data(KIND_ROLE) == "ok"
    assert tb.item(_row_of(w, "25"), C_STATUS).text() == "no answer"
    # the change column
    assert _change_item(w, "01").text().splitlines() == [
        "+ P0299 Boost pressure regulation: control range not reached (new)",
        "- P0100 Mass air flow sensor (cleared)"]
    assert _change_item(w, "02").text() == "- P0741 TCC performance (cleared)"
    assert _change_item(w, "09").text() == "coding 0000070 -> 0000072"
    assert _change_item(w, "25").text() == "no longer answers"
    assert _change_item(w, "08").text() == "\u2014"
    bg, fg = diff_colors()
    for a in ("01", "02", "09", "25"):                   # changed: DIFF_BG, dark text
        it = _change_item(w, a)
        assert it.background().color() == bg
        assert it.foreground().color() == fg
    for a in ("03", "08", "36"):                         # unchanged: not orange
        assert _change_item(w, a).background().color() != bg
    # the header names the compared scan
    assert tb.horizontalHeaderItem(C_CHANGE).text() == "CHANGE SINCE 2026-09-14"
    # the journal since the compared scan
    assert not w.journal_box.isHidden()
    txt = w.journal_label.text()
    assert "09 Central electrics: coding 70 -&gt; 72" in txt and "02 Transmission: cleared 2 faults" in txt
    assert "Written to the car since 2026-09-14" in w.journal_title.text()


def test_fault_card_contents(app):
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    fc = w._fault_rows[_row_of(w, "01") + 1]
    c0, c1 = fc.cards
    find = lambda c, name: c.findChild(QtWidgets.QWidget, name)      # noqa: E731
    assert find(c0, "faultCode").text() == "08857 \u00b7 P0299"
    assert find(c0, "faultText").text().startswith("Boost pressure regulation")
    chip = find(c0, "faultChip")
    assert chip.text() == "present \u00b7 stored" and chip._kind == "bad"
    assert chip.toolTip() == "present, stored"
    assert find(c0, "faultFreeze").text() == "Freeze frame:  rpm = 3 210 rpm     spec = 1 980 mbar"
    assert find(c0, "faultAlso") is None                          # P0299 is new
    chip = find(c1, "faultChip")
    assert chip.text() == "intermittent" and chip._kind == "warn"
    assert find(c1, "faultFreeze") is None
    assert find(c1, "faultAlso").text() == "also in 2026-09-14 scan"
    # a fault that is not present now and not intermittent: neutral
    c_u = w._fault_rows[_row_of(w, "03") + 1].cards[0]
    assert find(c_u, "faultChip")._kind == "neutral"


def test_not_tried_rows_and_stopped_banner(app):
    from ovtsuite.diag.gui_report_page import C_CHANGE, C_STATUS, KIND_ROLE
    veh, now, old, jr = _scans(stopped=True)
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    for a in ("36", "46"):
        it = w.table.item(_row_of(w, a), C_STATUS)
        assert it.text() == "not tried" and it.data(KIND_ROLE) == "neutral"
        assert w.table.item(_row_of(w, a), C_CHANGE).text() == ""      # not "no longer answers"
    assert not w.banner.isHidden()
    assert "Scan stopped:" in w.banner_label.text() and "2 modules not tried" in w.banner_label.text()
    assert ("Scan stopped: 2 modules not tried", "warn") in _card(w, 0)[4]
    # the two modules not visited are not counted as lost
    assert ("1 no longer answers", "bad") in _card(w, 2)[4]
    assert _card(w, 0)[:3] == ("Modules reached", "5", "/ 8 in gateway list")
    # a complete scan: no banner
    veh, now, old, jr = _scans()
    _show(app, w, veh, now, old, jr)
    assert w.banner.isHidden()


def test_table_basis_counts_the_modules_tried(app):
    veh, now, old, jr = _scans(stopped=True)
    now.basis = "table"
    w = _page(app)
    _show(app, w, veh, now, None, [])
    assert _card(w, 0)[:3] == ("Modules reached", "5", "/ 6 tried")


def test_empty_state(app):
    w = _page(app)
    w.show()
    app.processEvents()
    assert w.empty_label.text() == "No scan yet. Press Scan all."
    assert not w.empty_label.isHidden() and w._content.isHidden()
    assert w.text() == ""
    veh, now, old, jr = _scans()
    _show(app, w, veh, now, None, [])
    assert w.empty_label.isHidden() and not w._content.isHidden()
    _show(app, w, veh, None, None, [])                     # back to nothing
    assert not w.empty_label.isHidden() and w._content.isHidden()
    assert w.table.rowCount() == 0


def test_text_is_the_text_report(app):
    from ovtsuite.diag import report as R
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    assert w.text() == R.text_report(veh, now, old, jr)
    _show(app, w, veh, now, None, [])
    assert w.text() == R.text_report(veh, now, None, [])


def test_breadcrumb(app):
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    assert w.vehicle_button.text() == "Audi A6 (4F) \u00b7 WAUZZZ4F\u20263456"
    assert w.scan_button.text() == "Scan \u00b7 2026-10-25 21:14"


# ------------------------------------------------------------------ signals
def test_signals(app):
    veh, now, old, jr = _scans(trace=r"C:\logs\2026-10-25_A6_scan.ovtlog")
    w = _page(app)
    got = {k: [] for k in ("back", "scan", "cmp", "exp", "grp", "mod", "trace")}
    w.backRequested.connect(lambda: got["back"].append(1))
    w.scanChanged.connect(lambda: got["scan"].append(1))
    w.compareChanged.connect(lambda: got["cmp"].append(1))
    w.exportRequested.connect(got["exp"].append)
    w.openGroupRequested.connect(lambda a, g: got["grp"].append((a, g)))
    w.moduleActivated.connect(got["mod"].append)
    w.traceRequested.connect(got["trace"].append)
    _show(app, w, veh, now, old, jr)
    assert got == {k: [] for k in got}                    # nothing fired by filling

    w.vehicle_button.click()
    assert got["back"] == [1]
    w.copy_button.click()
    w.act_html.trigger()
    w.act_pdf.trigger()
    assert got["exp"] == ["text", "html", "pdf"]
    assert [a.text() for a in w.export_menu.actions()] == ["HTML", "PDF"]

    # the compare combo: none + the scans other than the shown one
    cb = w.compare_combo
    assert [cb.itemText(i) for i in range(cb.count())] == ["No comparison", "2026-09-14 (before retune)"]
    assert w.compare_key() == "k1" and w.selected_scan_key() == "k2"
    cb.setCurrentIndex(0)
    assert got["cmp"] == [1] and w.compare_key() is None
    cb.setCurrentIndex(1)
    assert got["cmp"] == [1, 1] and w.compare_key() == "k1"

    # the scan crumb's menu: the saved scans, the shown one checked
    acts = w.scan_menu.actions()
    assert [a.text() for a in acts] == ["2026-10-25 21:14", "2026-09-14 (before retune)"]
    assert [a.isChecked() for a in acts] == [True, False]
    acts[0].trigger()                                      # the shown one: nothing
    assert got["scan"] == []
    acts[1].trigger()
    assert got["scan"] == [1] and w.selected_scan_key() == "k1"
    assert w.scan_button.text() == "Scan \u00b7 2026-09-14 (before retune)"
    cb = w.compare_combo
    assert [cb.itemText(i) for i in range(cb.count())] == ["No comparison", "2026-10-25 21:14"]

    # a fault card's link: the module's Live data tab, group 0
    fc = w._fault_rows[_row_of(w, "03") + 1]
    fc.cards[0].findChild(QtWidgets.QLabel, "faultLink").linkActivated.emit("live")
    assert got["grp"] == [("03", 0)]

    # the trace link
    assert not w.trace_label.isHidden()
    assert "2026-10-25_A6_scan.ovtlog" in w.trace_label.text()
    w.trace_label.linkActivated.emit("trace")
    assert got["trace"] == [r"C:\logs\2026-10-25_A6_scan.ovtlog"]


def test_set_scans_is_silent_and_keeps_keys(app):
    w = _page(app, scans=False)
    seen = []
    w.scanChanged.connect(lambda: seen.append("s"))
    w.compareChanged.connect(lambda: seen.append("c"))
    w.set_scans([("a", "ka"), ("b", "kb"), ("c", "kc")], "kb", "kc")
    assert seen == []
    assert (w.selected_scan_key(), w.compare_key()) == ("kb", "kc")
    w.set_scans([("a", "ka")], "ka", None)
    assert (w.selected_scan_key(), w.compare_key()) == ("ka", None)
    w.set_scans([], None, None)
    assert (w.selected_scan_key(), w.compare_key()) == (None, None) and seen == []


def test_no_trace_link_without_a_trace(app):
    veh, now, old, jr = _scans(trace="")
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    assert w.trace_label.isHidden()
    assert w.footer.text().startswith("Every scan is saved with the vehicle.")


# ----------------------------------------------------------------- keyboard
def test_keyboard_navigation_skips_fault_rows(app):
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    tb = w.table
    got = []
    w.moduleActivated.connect(got.append)
    tb.setFocus()
    tb.selectRow(_row_of(w, "01"))
    QTest.keyClick(tb, Qt.Key.Key_Down)
    assert w._rows[tb.currentRow()] == "02"                # the fault row under 01 is skipped
    QTest.keyClick(tb, Qt.Key.Key_Down)
    assert w._rows[tb.currentRow()] == "03"
    QTest.keyClick(tb, Qt.Key.Key_Down)
    assert w._rows[tb.currentRow()] == "08"
    QTest.keyClick(tb, Qt.Key.Key_Up)
    QTest.keyClick(tb, Qt.Key.Key_Up)
    QTest.keyClick(tb, Qt.Key.Key_Up)
    assert w._rows[tb.currentRow()] == "01"
    assert [i.row() for i in tb.selectionModel().selectedRows()] == [_row_of(w, "01")]
    QTest.keyClick(tb, Qt.Key.Key_Return)
    assert got == ["01"]
    QTest.keyClick(tb, Qt.Key.Key_Down)
    QTest.keyClick(tb, Qt.Key.Key_Enter)
    assert got == ["01", "02"]


def test_double_click_activates_a_module(app):
    from ovtsuite.diag.gui_report_page import C_MODULE
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    got = []
    w.moduleActivated.connect(got.append)
    tb = w.table
    pos = tb.visualItemRect(tb.item(_row_of(w, "08"), C_MODULE)).center()
    vp = tb.viewport()
    gp = vp.mapToGlobal(pos)
    for t in (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.MouseButtonRelease,
              QtCore.QEvent.Type.MouseButtonDblClick, QtCore.QEvent.Type.MouseButtonRelease):
        app.sendEvent(vp, QtGui.QMouseEvent(t, QtCore.QPointF(pos), QtCore.QPointF(gp), Qt.MouseButton.LeftButton,
                                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    assert got == ["08"]


def test_selection_survives_a_refresh(app):
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    w.table.selectRow(_row_of(w, "09"))
    _show(app, w, veh, now, None, [])                      # the comparison dropped: rows move
    assert w._rows[w.table.currentRow()] == "09"
    assert [i.row() for i in w.table.selectionModel().selectedRows()] == [_row_of(w, "09")]


# ----------------------------------------------------------- the table standard
def test_table_follows_the_suite_standard(app):
    from ovtsuite.gui_cellmarks import CellDelegate
    w = _page(app)
    tb = w.table
    assert tb.alternatingRowColors()
    assert isinstance(tb.itemDelegate(), CellDelegate)
    assert tb.selectionBehavior() == QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
    veh, now, old, jr = _scans()
    _show(app, w, veh, now, old, jr)
    # the fault row cannot be selected, the module rows can
    for r in range(tb.rowCount()):
        fl = tb.item(r, 0).flags()
        assert bool(fl & Qt.ItemFlag.ItemIsSelectable) == (r in w._rows)


def test_changed_cells_stay_legible_selected(app):
    """A selected CHANGE cell: the selection's text colour, not the dark text
    of the orange fill (``gui_cellmarks.selection_text`` via ``CellDelegate``)."""
    from ovtsuite.diag.gui_report_page import C_CHANGE
    from ovtsuite.gui_cellmarks import contrast, selected_text_color
    veh, now, old, jr = _scans()
    w = _page(app)
    _show(app, w, veh, now, old, jr)
    tb = w.table
    tb.setFocus()
    tb.selectRow(_row_of(w, "09"))
    opt = QtWidgets.QStyleOptionViewItem()
    tb.itemDelegate().initStyleOption(opt, tb.model().index(_row_of(w, "09"), C_CHANGE))
    opt.widget = tb
    col = opt.palette.color(QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorRole.Text)
    assert col.isValid()
    assert selected_text_color(opt) is None or contrast(selected_text_color(opt),
                                                         opt.palette.color(QtGui.QPalette.ColorRole.Highlight)) >= 3


@pytest.mark.parametrize("dark", [False, True])
def test_builds_in_both_palettes(app, dark):
    veh, now, old, jr = _scans(stopped=True, trace="t.ovtlog")
    w = _page(app, dark=dark)
    _show(app, w, veh, now, old, jr)
    pm = w.grab()
    assert not pm.isNull() and pm.width() > 100
    _show(app, w, veh, None, None, [])
    assert not w.grab().isNull()
    from ovtsuite import gui_theme
    app.setPalette(QtGui.QPalette() if dark else gui_theme.dark_palette())   # a theme switch while shown
    _show(app, w, veh, now, old, jr)
    for _ in range(3):
        app.processEvents()
    assert w.table.rowCount() == 10
    app.setPalette(QtGui.QPalette())

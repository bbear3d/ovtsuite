# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The fault code report window (ovtsuite.gui_dtc_report): its table, the
empty read, history with new / gone, Copy / Save, web search (the URL, no
browser), Ask AI (the question, no assistant), and reads kept in the project
from the Fault codes from ECU window."""

import os
import urllib.parse

import pytest

pytestmark = pytest.mark.slow


@pytest.fixture
def app(isolated_qsettings):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from ovtsuite import gui as _g
    if not _g.HAVE_QT:
        pytest.skip("needs a working PySide6 QtWidgets")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _paths(code):
    return {16485: [_R().PathRef("Dfp_AirFlwSens", True)],
            17964: [_R().PathRef("Dfp_BPACD", False)]}.get(code, [])


def _R():
    from ovtsuite import dtc_report
    return dtc_report


def _read(codes, ts="2026-10-25T10:00:00", controller="EDC16",
          ecu_ident="038906016AB EDC16", **kw):
    """A read of ``codes`` ((code, status[, proto]) tuples; the proto is
    flash.diag.Dtc's default, KW1281, unless given)."""
    from ovtsuite.flash.diag import Dtc
    return _R().build_read([Dtc(*c) for c in codes], paths_for=_paths, ts=ts,
                           controller=controller, ecu_ident=ecu_ident,
                           cable="OpenPort", transport="K-line", **kw)


class _Doc:
    def __init__(self, project):
        self.project = project
        self.title = "bench"

    def tabs(self, win):
        return [w for w in getattr(win, "_tabs", [])]


class _Ref:
    def __init__(self, doc, sid):
        self.doc, self.sid = doc, sid


class _Ws:
    def __init__(self, ref=None, win=None):
        self.project_ref = ref
        self.win = win


def _proj():
    from ovtsuite import project as ovt
    p = ovt.Project("bench")
    s = p.add_set("ECU", platform="unknown")
    return p, s


def _cell(dlg, r, c):
    return dlg.table.item(r, c).text()


def test_table_shows_each_code_decoded(app):
    from PySide6 import QtCore
    from ovtsuite import gui_dtc_report as G
    from ovtsuite.gui_cellmarks import CellDelegate
    r = _read([(16485, 0x87), (12345, 0x1B)], controller="EDC15",
              ecu_ident="038906019GQ 1,9l R4 EDC")
    d = G.FaultCodeReport([(None, r)], G.ReportContext())
    assert d.table.rowCount() == 2 and not d.empty.isVisibleTo(d)
    assert d.table.alternatingRowColors()
    assert isinstance(d.table.itemDelegate(), CellDelegate)
    assert [_cell(d, 0, c) for c in (0, 1, 3, 4)] == ["16485", "P0101", "07-10",
                                                      "07 - signal too low"]
    assert d.table.item(0, 5).data(QtCore.Qt.ItemDataRole.CheckStateRole) == \
        QtCore.Qt.CheckState.Checked.value or \
        d.table.item(0, 5).checkState() == QtCore.Qt.CheckState.Checked
    assert _cell(d, 0, 6) == "Dfp_AirFlwSens (on)"
    assert "healed" in d.table.item(0, 3).toolTip()         # EDC15: re-occurred
    assert d.table.item(0, 5).toolTip().startswith("Sporadic")
    assert _cell(d, 1, 4) == "27 - implausible signal"
    assert _cell(d, 1, 6) == "none in this file"           # flagged
    assert "Not kept" in d.kept.text()
    assert d.show_btn.isDefault() and d.show_btn.isVisibleTo(d)
    d.close()


def test_kwp2000_status_is_a_bit_field(app):
    """An EDC16 answers 18 with a KWP2000 statusOfDTC: 0x40 is "present", not
    fault type 64; 0x20 is stored but not present -- the Intermittent tick."""
    from PySide6 import QtCore
    from ovtsuite import gui_dtc_report as G
    from ovtsuite.flash.diag import DTC_KWP2000
    r = _read([(16610, 0x40, DTC_KWP2000), (16485, 0x21, DTC_KWP2000)])
    d = G.FaultCodeReport([(None, r)], G.ReportContext())
    assert [_cell(d, 0, c) for c in (3, 4)] == ["0x40", "00 - none given"]
    assert _cell(d, 1, 4) == "01 - upper limit exceeded (Max)"
    checked = [d.table.item(i, 5).checkState() == QtCore.Qt.CheckState.Checked
               for i in range(2)]
    assert checked == [False, True]
    assert "bit 6" in d.table.item(1, 5).toolTip()
    assert "present" in d.table.item(0, 3).toolTip()
    d.close()


def test_empty_read_says_no_codes(app):
    from ovtsuite import gui_dtc_report as G
    d = G.FaultCodeReport([(None, _read([]))])
    assert d.empty.text() == "No fault codes stored" and d.empty.isVisibleTo(d)
    assert not d.table.isVisibleTo(d)
    assert "No fault codes stored." in d.text()
    d.close()
    d = G.FaultCodeReport([(None, _read([], action="clear", before=[]))])
    assert "cleared" in d.empty.text()
    d.close()


def test_history_marks_new_and_gone(app):
    from ovtsuite import gui_dtc_report as G
    old = _read([(16485, 7), (18010, 2)], ts="2026-10-24T09:00:00")
    cur = _read([(16485, 7), (17964, 8)])
    d = G.FaultCodeReport([(None, cur), (None, old)])
    assert d.history.count() == 2 and d.history.itemText(0).startswith("This read")
    rows = {_cell(d, r, 0): _cell(d, r, 7) for r in range(d.table.rowCount())}
    assert rows == {"16485": "", "17964": "new", "18010": "gone"}
    d.history.setCurrentIndex(1)                     # the older read: nothing before
    assert d.read is old and d.table.rowCount() == 2
    assert {_cell(d, r, 7) for r in range(2)} == {""}
    d.close()


def test_copy_and_save(app, tmp_path):
    from PySide6 import QtWidgets
    from ovtsuite import gui_dtc_report as G
    d = G.FaultCodeReport([(None, _read([(16485, 7)]))])
    d._copy()
    assert "16485" in QtWidgets.QApplication.clipboard().text()
    t = d.save_to(str(tmp_path / "r.txt"))
    assert "Fault code report" in open(t, encoding="utf-8").read()
    c = d.save_to(str(tmp_path / "r"), csv=True)
    assert c.endswith(".csv") and open(c, encoding="utf-8").read().startswith("time,")
    assert (d.print_btn is not None) == G.HAVE_PRINT
    d.close()


def test_search_opens_the_chosen_site_and_remembers_it(app, monkeypatch):
    from PySide6 import QtGui
    from ovtsuite import gui_dtc_report as G
    opened = []
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl",
                        staticmethod(lambda u: opened.append(u.toString()) or True))
    d = G.FaultCodeReport([(None, _read([(16485, 7), (17964, 8)]))])
    assert d.search_btn.text() == "Search Google"
    assert "web browser" in d.search_btn.toolTip()
    d.table.selectRow(1)
    url = d._search()
    assert opened == [url] and url.startswith("https://www.google.com/search?q=")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
    assert q.startswith('"17964"') and "16485" not in q and q.endswith("038906016AB")
    d.site_actions["rosstech"].trigger()
    assert opened[-1] == "https://wiki.ross-tech.com/wiki/index.php?search=17964"
    assert G.search_choice().id == "rosstech"
    assert d.search_btn.text().startswith("Search Ross-Tech")
    d.close()
    d = G.FaultCodeReport([(None, _read([(16485, 7)]))])
    d._search()                                         # remembered
    assert opened[-1].startswith("https://wiki.ross-tech.com/")
    d.close()


def test_ask_ai_hands_the_assistant_a_question(app, monkeypatch):
    from ovtsuite import gui_ai
    from ovtsuite import gui_dtc_report as G
    asked = []

    class _Lw:
        def tab_for(self, ws):
            return "T7"

    class _Dock:
        def _workspace(self):
            return _Lw()
    monkeypatch.setattr(G, "ai_available", lambda: (True, ""))
    monkeypatch.setattr(gui_ai, "assistant_dock", lambda win: _Dock(), raising=False)
    monkeypatch.setattr(gui_ai, "show_assistant",
                        lambda win, q: asked.append((win, q)), raising=False)
    win = object()
    ctx = G.ReportContext(ws=_Ws(win=win))
    old = _read([(17964, 8)], ts="2026-10-24T09:00:00")
    d = G.FaultCodeReport([(None, _read([(16485, 7), (17964, 0x88)])), (None, old)], ctx)
    d.table.selectRow(1)
    q = d._ask()
    assert asked[-1] == (win, q)
    assert "tab T7" in q and "17964" in q and "16485" not in q
    assert "Dfp_BPACD (disabled)" in q and "fault_codes" in q and "intermittent" in q
    assert not d.show_paths                             # stays open beside it
    d = G.FaultCodeReport([(None, _read([(16485, 7), (17964, 8)])), (None, old)], ctx)
    q = d._ask_report()
    assert asked[-1][1] == q and "oldest first" in q and "17964 (2 reads)" in q
    assert "new 16485" in q
    d.close()


def test_ask_ai_says_when_the_assistant_is_off(app, monkeypatch):
    from PySide6 import QtWidgets
    from ovtsuite import gui_dtc_report as G
    said = []
    monkeypatch.setattr(G, "ai_available", lambda: (False, "The AI assistant is off."))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: said.append(a[2]))
    d = G.FaultCodeReport([(None, _read([(16485, 7)]))], G.ReportContext(ws=_Ws()))
    assert d.ai_btn.toolTip() == "The AI assistant is off."
    assert d._ask() is None and d._ask_report() is None
    assert said == ["The AI assistant is off."] * 2 and not d.show_paths
    d.close()


def test_a_read_is_kept_in_the_project_with_its_history(app, monkeypatch):
    from ovtsuite import dtc_report as R
    from ovtsuite import gui_dtc_report as G
    from ovtsuite.datalog import gui_log as GL
    from ovtsuite.flash.diag import Dtc
    shown = []
    monkeypatch.setattr(G.FaultCodeReport, "show", lambda self: shown.append(self))
    monkeypatch.setattr(GL, "retire_thread", lambda *_: None)
    p, s = _proj()
    R.keep(p, s.id, _read([(16485, 7)], ts="2026-10-24T09:00:00"))
    ctx = G.ReportContext(ws=_Ws(_Ref(_Doc(p), s.id)), paths_for=_paths, file="car.bin")
    d = GL.EcuCodesDialog(None, report=ctx)
    d._ident = "038906016AB EDC16"
    d._done([Dtc(17964, 8)])
    rep = shown[-1]
    assert d.record.ecu_ident == "038906016AB EDC16" and d.record.set_name == "ECU"
    assert d.record.codes[0].paths[0].name == "Dfp_BPACD"
    assert "Kept in project bench" in rep.kept.text()
    assert rep.history.count() == 2 and rep.items[0][1] is d.record
    assert [r.code_numbers for _a, r in R.history(p, s.id)] == [[17964], [16485]]
    # a clear: what was there before, what came back
    d = GL.EcuCodesDialog(None, report=ctx)
    d._what, d._before = "clear", [Dtc(17964, 8), Dtc(1, 0)]
    d._done([Dtc(17964, 8)])
    assert [c.code for c in d.record.still_present] == [17964]
    assert "came straight back): 17964" in shown[-1].header.text()
    assert len(R.history(p, s.id)) == 3


def test_project_panel_opens_a_kept_read(app):
    from ovtsuite import dtc_report as R
    from ovtsuite import gui_dtc_report as G
    from PySide6 import QtWidgets
    p, s = _proj()
    a1 = R.keep(p, s.id, _read([(16485, 7)], ts="2026-10-24T09:00:00"))
    R.keep(p, s.id, _read([(17964, 8)]))
    win = QtWidgets.QMainWindow()
    assert G.open_kept(win, _Doc(p), a1.id)
    d = next(w for w in QtWidgets.QApplication.topLevelWidgets()
             if isinstance(w, G.FaultCodeReport) and w.isVisible())
    assert d.read.code_numbers == [16485] and d.history.currentIndex() == 1
    assert not d.show_btn.isVisibleTo(d) and d.close_btn.isDefault()
    d.close()
    other = p.add_attachment("notes.txt", b"x", set_id=s.id)
    assert not G.open_kept(win, _Doc(p), other.id)


def test_sae_word_codes_show_as_vcds_writes_them(app):
    """A P447 / P475 EDC16 reports the SAE word: the report writes it with six
    digits and its P-code, also when a kept read is opened later."""
    from ovtsuite import gui_dtc_report as G
    from ovtsuite.flash.diag import DTC_KWP2000
    R = _R()
    r = _read([(0x0103, 0x60, DTC_KWP2000)], encoding=R.ENC_SAE)
    kept = R.Read.from_json(r.to_json())
    for rec in (r, kept):
        d = G.FaultCodeReport([(None, rec)], G.ReportContext())
        assert [_cell(d, 0, c) for c in (0, 1)] == ["000259", "P0103"]
        d.close()


def _closed(w):
    """``w`` is closed (hidden, or already deleted by WA_DeleteOnClose)."""
    import shiboken6
    return not shiboken6.isValid(w) or not w.isVisible()


def test_report_does_not_block_and_hands_back_show_in_fault_paths(app):
    """The report after a read is not modal (the assistant panel works beside
    it), stays referenced while open, replaces the report still open for the
    same tab, and its Show in fault paths calls the editor back later."""
    from PySide6 import QtCore
    from ovtsuite import gui_dtc_report as G
    called = []
    ws = _Ws()
    ctx = G.ReportContext(ws=ws, on_show_paths=lambda: called.append(1))
    key = G.report_key(ctx)
    first = G.show_after_read(None, _read([(16485, 7)]), ctx)
    assert first.isVisible() and not first.isModal() and not first.show_paths
    assert first.windowModality() == QtCore.Qt.WindowModality.NonModal
    assert G.open_report(key) is first
    second = G.show_after_read(None, _read([(17964, 8)]), ctx)
    app.processEvents()
    assert _closed(first) and G.open_report(key) is second
    assert called == []                                 # a replaced one hands nothing back
    # another tab's report is a window of its own
    other = G.show_after_read(None, _read([(16485, 7)]), G.ReportContext(ws=_Ws()))
    assert other.isVisible() and second.isVisible()
    other.close()
    second._show_in_paths()
    assert second.show_paths
    app.processEvents()
    assert called == [1] and _closed(second)
    assert G.open_report(key) is None


def test_ask_ai_keeps_the_report_open(app, monkeypatch):
    from ovtsuite import gui_dtc_report as G
    asked = []
    monkeypatch.setattr(G.FaultCodeReport, "ai_action",
                        staticmethod(lambda *a: asked.append(a) or "q"))
    monkeypatch.setattr(G.FaultCodeReport, "ai_report_action",
                        staticmethod(lambda *a: asked.append(a) or "q2"))
    called = []
    ctx = G.ReportContext(ws=_Ws(), on_show_paths=lambda: called.append(1))
    d = G.show_after_read(None, _read([(16485, 7)]), ctx)
    assert d._ask() == "q" and d._ask_report() == "q2"
    app.processEvents()
    assert d.isVisible() and not d.show_paths and called == []
    assert "stays open" in d.ai_btn.toolTip() or not G.ai_available()[0]
    d.close()


def test_kept_read_from_the_project_panel_replaces_the_open_report(app):
    """A kept read opened from the Project panel, for the set whose report is
    open, takes that report's place (one per set)."""
    from PySide6 import QtWidgets
    from ovtsuite import dtc_report as R
    from ovtsuite import gui_dtc_report as G
    p, s = _proj()
    doc = _Doc(p)
    a1 = R.keep(p, s.id, _read([(16485, 7)], ts="2026-10-24T09:00:00"))
    ctx = G.ReportContext(ws=_Ws(_Ref(doc, s.id)))
    after = G.show_after_read(None, _read([(17964, 8)]), ctx)
    key = G.report_key(ctx)
    assert key == G.report_key(None, doc, s.id)
    win = QtWidgets.QMainWindow()
    assert G.open_kept(win, doc, a1.id)
    app.processEvents()
    kept = G.open_report(key)
    assert kept is not None and kept is not after and _closed(after)
    assert not kept.after_read and not kept.isModal()
    kept.close()


def test_ask_ai_after_the_tab_closed_says_so(app, monkeypatch):
    """The report outlives its tab now (it does not block): Ask AI then says
    the tab is gone instead of asking about "tab None"."""
    from PySide6 import QtWidgets
    from ovtsuite import gui_ai
    from ovtsuite import gui_dtc_report as G
    asked, said = [], []

    class _Lw:
        def tab_for(self, ws):
            return None                                 # no open tab for ws

    class _Dock:
        def _workspace(self):
            return _Lw()
    monkeypatch.setattr(G, "ai_available", lambda: (True, ""))
    monkeypatch.setattr(gui_ai, "assistant_dock", lambda win: _Dock(), raising=False)
    monkeypatch.setattr(gui_ai, "show_assistant",
                        lambda win, q: asked.append(q), raising=False)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: said.append(a[2])))
    d = G.FaultCodeReport([(None, _read([(16485, 7)]))], G.ReportContext(ws=_Ws(win=object())))
    assert d._ask() is None and d._ask_report() is None
    assert asked == [] and len(said) == 2 and "closed" in said[0]
    d.close()


def test_report_comes_forward_after_the_read_window_closes(app):
    """Shown from the read window, which closes on the result: the report is
    brought forward again once that has happened (the editor the read window
    belonged to would be activated over it)."""
    from PySide6 import QtCore, QtWidgets
    from ovtsuite import gui_dtc_report as G
    fronted = []
    real = G._front
    G._front = lambda d: fronted.append(d) or real(d)
    try:
        d = G.show_after_read(None, _read([(16485, 7)]), G.ReportContext(ws=_Ws()))
        n = len(fronted)
        assert n >= 1
        QtCore.QCoreApplication.processEvents()
        QtCore.QCoreApplication.sendPostedEvents()
        app.processEvents()
        QtCore.QThread.msleep(5)
        app.processEvents()
        assert len(fronted) > n and fronted[-1] is d     # once more, deferred
    finally:
        G._front = real
    d.close()
    app.processEvents()
    G._front(d)                                         # deleted: no error
    assert isinstance(d, QtWidgets.QDialog)


def test_show_in_fault_paths_brings_the_editor_forward(app):
    """The report's Show in fault paths shows the editor to the user even
    when its tab is not the current one (the tab is switched to) or it is
    minimised (restored), and is quiet about one already deleted."""
    import shiboken6
    from PySide6 import QtCore, QtWidgets
    from ovtsuite import gui_dtc_report as G
    win = QtWidgets.QMainWindow()
    win.tabs = QtWidgets.QTabWidget()
    win.setCentralWidget(win.tabs)
    ws1, ws2 = QtWidgets.QWidget(), QtWidgets.QWidget()
    for w in (ws1, ws2):
        w.win = win
        win.tabs.addTab(w, "t")
    win.show()
    win.tabs.setCurrentWidget(ws2)
    editor = QtWidgets.QDialog(ws1)
    editor.hide()                           # suspended with its tab
    G.bring_to_front(editor, ws1)
    assert win.tabs.currentWidget() is ws1 and editor.isVisible()
    editor.showMinimized()
    app.processEvents()
    G.bring_to_front(editor, ws1)
    app.processEvents()
    assert not editor.windowState() & QtCore.Qt.WindowState.WindowMinimized
    editor.deleteLater()
    app.processEvents()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    if not shiboken6.isValid(editor):
        G.bring_to_front(editor, ws1)       # deleted: no error
    win.close()

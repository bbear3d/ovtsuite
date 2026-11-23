# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Differences window (ovtsuite.gui_differences): List vs Make patch,
the checksum filter keeping the patch choices, the history pair and Save,
and right-click on a row outside the selection.

Slow: loads full TunerSessions."""
import os

import pytest

pytestmark = pytest.mark.slow

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
PRIMARY, COMPARISON = "038906016AB.bin", "038997016R_remap.bin"


def _session(name):
    path = os.path.join(BIN, name)
    if not os.path.exists(path):
        pytest.skip("reference binary %s not present" % name)
    from ovtsuite.session import TunerSession
    return TunerSession(path)


@pytest.fixture(scope="module")
def a():
    return _session(PRIMARY)


@pytest.fixture(scope="module")
def b():
    return _session(COMPARISON)


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Ws:
    """The bits of a tab the window uses: show_entity is recorded."""
    comparison = None
    project_ref = None

    def __init__(self, primary):
        self.primary = primary
        self.opened = []

    def show_entity(self, e):
        self.opened.append(e)

    def raise_(self):
        pass

    def activateWindow(self):
        pass


def _entry_rows(d):
    return [r for r, (k, _i) in enumerate(d._rows) if k == "entry"]


def test_double_click_opens_only_in_list_mode(app, a, b):
    from ovtsuite import gui_differences as GD
    ws = _Ws(a)
    d = GD.DifferencesWindow(b, a, ws=ws, mode=GD.LIST)
    try:
        row = _entry_rows(d)[0]
        d.tbl.cellDoubleClicked.emit(row, GD.C_NAME)
        assert len(ws.opened) == 1
        d.set_mode(GD.PATCH)
        d.tbl.cellDoubleClicked.emit(row, GD.C_NAME)
        assert len(ws.opened) == 1                      # nothing behind the dialog
    finally:
        d.close()


def test_checksum_toggle_keeps_ticks_whole_and_added(app, a, b):
    from ovtsuite import gui_differences as GD
    d = GD.DifferencesWindow(b, a, mode=GD.PATCH)
    try:
        rows = _entry_rows(d)
        assert len(rows) >= 3
        for r in rows:
            d.tbl.item(r, GD.C_CHECK).setCheckState(GD.UNCHECKED)
        d.tbl.item(rows[0], GD.C_CHECK).setCheckState(GD.CHECKED)
        names = [e.name for e in d._entries]
        # the second parameter taken whole
        assert d._set_whole(1, True)
        d.tbl.item(rows[1], GD.C_CHECK).setCheckState(GD.UNCHECKED)
        d._sync_checks()
        d._entries[1].selected = False
        before = [(e.name, e.selected, bool(getattr(e, "whole", False)))
                  for e in d._entries]
        d.excl_checksum_chk.setChecked(not d.excl_checksum_chk.isChecked())
        after = [(e.name, e.selected, bool(getattr(e, "whole", False)))
                 for e in d._entries]
        assert after == before
        assert [e.name for e in d._entries] == names
        d.excl_checksum_chk.setChecked(not d.excl_checksum_chk.isChecked())
        assert [(e.name, e.selected, bool(getattr(e, "whole", False)))
                for e in d._entries] == before
    finally:
        d.close()


def _history_window(a, b, monkeypatch):
    """A window on a's 'history' of two fake versions (v1 = a, v2 = b's
    bytes when they are the same length, else a's with one byte changed)."""
    from ovtsuite import gui_differences as GD
    from ovtsuite import history as H
    base = bytes(a.binfile.data)
    other = bytearray(base)
    for i, m in enumerate(a.entities):
        if m.kind != "constant":
            break
    data2 = bytes(b.binfile.data) if len(b.binfile.data) == len(base) else bytes(other)
    blobs = {"v1": base, "v2": data2}
    monkeypatch.setattr(H, "list_versions", lambda s: [
        {"id": "v2", "ts": "2"}, {"id": "v1", "ts": "1"}])
    monkeypatch.setattr(H, "bytes_at_version", lambda s, vid: blobs[vid])
    monkeypatch.setattr(H, "display_label", lambda v: v["id"])
    calls = []
    orig = None

    d = GD.DifferencesWindow(a, a, history=a, mode=GD.PATCH)
    d.source.setCurrentIndex(d.source.findData("history"))
    return d, calls


def test_history_pair_valid_equal_and_error(app, a, b, monkeypatch):
    from ovtsuite import gui_differences as GD
    d, _ = _history_window(a, b, monkeypatch)
    try:
        # valid pair: v1 -> v2
        d.from_box.setCurrentIndex(d.from_box.findData("v1"))
        d.to_box.setCurrentIndex(d.to_box.findData("v2"))
        assert d.save_btn.isEnabled() and d._valid
        assert d.primary is not d.comparison
        # the same version both sides: empty table, Save off
        d.to_box.setCurrentIndex(d.to_box.findData("v1"))
        assert not d.save_btn.isEnabled()
        assert d.primary is d.comparison and d.tbl.rowCount() == 0
        assert "different versions" in d.head_lbl.text()
        # swap does not re-enable Save
        d.swap()
        assert not d.save_btn.isEnabled()
        # a good pair again
        d.to_box.setCurrentIndex(d.to_box.findData("v2"))
        assert d.save_btn.isEnabled()
        # an error (a version that is gone)
        from ovtsuite import history as H

        def gone(s, vid):
            raise ValueError("no version %r" % vid)
        monkeypatch.setattr(H, "bytes_at_version", gone)
        d._vcache.clear()
        d.from_box.setCurrentIndex(d.from_box.findData(H.ORIGINAL))
        assert not d.save_btn.isEnabled()
        assert d.primary is d.comparison and d.tbl.rowCount() == 0
        assert "no version" in d.head_lbl.text()
    finally:
        d.close()


def test_history_sessions_are_built_once(app, a, b, monkeypatch):
    from ovtsuite import patch as P
    d, _ = _history_window(a, b, monkeypatch)
    try:
        n = []
        real = P._session_from_bytes
        monkeypatch.setattr(P, "_session_from_bytes",
                            lambda *x, **k: (n.append(1), real(*x, **k))[1])
        d.from_box.setCurrentIndex(d.from_box.findData("v1"))
        d.to_box.setCurrentIndex(d.to_box.findData("v2"))
        built = len(n)
        d.to_box.setCurrentIndex(d.to_box.findData("v1"))
        d.to_box.setCurrentIndex(d.to_box.findData("v2"))
        d.from_box.setCurrentIndex(d.from_box.findData("v1"))
        assert len(n) == built
    finally:
        d.close()


def test_right_click_on_an_unselected_row_acts_on_that_row(app, a, b, monkeypatch):
    from PySide6 import QtCore
    from ovtsuite import gui_differences as GD
    d = GD.DifferencesWindow(b, a, mode=GD.PATCH)
    try:
        d.show()
        rows = _entry_rows(d)
        assert len(rows) >= 3
        d.tbl.selectRow(rows[0])

        class FakeMenu:
            """Picks the first action (Take whole) without a popup."""
            def __init__(self, *_a):
                self.acts = []

            def addAction(self, text):
                act = type("A", (), {"enabled": True,
                                     "setEnabled": lambda s, on: None})()
                self.acts.append(act)
                return act

            def exec(self, *_a):
                return self.acts[0]
        monkeypatch.setattr(GD.QtWidgets, "QMenu", FakeMenu)
        taken = []
        monkeypatch.setattr(d, "_set_whole", lambda r, w: taken.append(r) or True)
        y = d.tbl.rowViewportPosition(rows[2]) + 2
        d._rows_menu(QtCore.QPoint(5, y))
        assert taken == [rows[2]]
        taken.clear()
        y = d.tbl.rowViewportPosition(rows[0]) + 2
        d._rows_menu(QtCore.QPoint(5, y))
        assert taken == [rows[0]]
    finally:
        d.close()


def test_table_keeps_the_app_look(app, a, b):
    from ovtsuite import gui_differences as GD
    from ovtsuite.gui_cellmarks import CellDelegate
    d = GD.DifferencesWindow(b, a)
    try:
        assert d.tbl.alternatingRowColors()
        assert isinstance(d.tbl.itemDelegate(), CellDelegate)
    finally:
        d.close()

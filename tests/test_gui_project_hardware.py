# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where a project's fitted hardware shows (ovtsuite.gui_project): the
Project panel's "Fitted: ..." line and version marks, the tab's tooltip,
Version history, and the warning before writing a version set up for other
hardware. Also Record as fitted's wording (gui_hardware.ask_fitted).
"""
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
from ovtsuite import project as ovt  # noqa: E402
from ovtsuite.project import autosave as _as  # noqa: E402
from ovtsuite.project import hardware as H  # noqa: E402

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
PD150 = {"preset": "pd150"}


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    pass


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_record_as_fitted_says_what_it_records(app, monkeypatch):
    """True / False / None (may be set up already) each get their own words."""
    from ovtsuite import gui_hardware
    seen = []

    def exec_(dlg):
        seen.append(dlg.findChild(QtWidgets.QLabel, "calibrated_note").text())
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(QtWidgets.QDialog, "exec", exec_)
    for cal in (True, False, None):
        assert gui_hardware.ask_fitted(None, "Injector setup", "PD150", cal) is None
    assert "is set up for it" in seen[0]
    assert seen[1].startswith("This file is not set up for it yet")
    assert "may already be set up" in seen[2] and "not set up for it yet" not in seen[2]


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info", lambda self, *a, **k: None)
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: 0))
    w = gui.MainWindow()
    w.clear_closed()
    w.show()
    yield w
    w.close_discarding()


def _open(win, tmp_path):
    proj, _s = ovt.import_file(LQ)
    proj.name = "Golf"
    path = str(tmp_path / "golf.ovt")
    proj.save(path)
    assert win.open_path(path)
    return next(ws for ws in win.workspaces() if ws.project_ref is not None)


def _panel_texts(win):
    from ovtsuite import gui_project as gp
    p = gp.panel(win)
    p.refresh()
    out = {}

    def walk(it):
        for i in range(it.childCount()):
            ch = it.child(i)
            out.setdefault("all", []).append((ch.text(0), ch.toolTip(0), ch))
            walk(ch)
    for i in range(p.tree.topLevelItemCount()):
        walk(p.tree.topLevelItem(i))
    return out.get("all", [])


def _poke(ws, off=0x100):
    b = ws.primary.binfile.data[off]
    ws.primary.edit_bytes(off, bytes([b ^ 0xFF]))


@pytest.mark.slow
def test_the_hardware_shows_in_the_panel_the_tab_and_the_history(win, tmp_path, monkeypatch):
    from ovtsuite import gui, gui_project as gp
    ws = _open(win, tmp_path)
    ref = ws.project_ref
    proj, sid = ref.doc.project, ref.sid
    assert not [t for t, _tip, _i in _panel_texts(win) if "Fitted:" in t]
    assert gp.version_note(ws) is None                  # nothing recorded
    # fitted, the file not set up for it yet
    H.record_fitted(proj, sid, ref.bid, H.PD_INJECTORS, "PD150 injectors", PD150,
                    calibrated=False, since="2026-10-01")
    line = [x for x in _panel_texts(win) if "Fitted:" in x[0]]
    assert len(line) == 1
    text, tip, item = line[0]
    assert "PD150 injectors — this version is not set up for it" in text
    assert "fitted 2026-10-01" in tip
    assert gp.tab_tooltip(ws, "x").splitlines()[-1].startswith("Fitted: PD150 injectors —")
    # Injector setup's Apply: the edits set it up; saved, that version has it
    _poke(ws)
    H.record_setup(proj, sid, ref.bid, H.PD_INJECTORS, "PD150 injectors", PD150)
    assert gp.hardware_text(proj, sid, ref.branch.head, True)[0] == "Fitted: PD150 injectors"
    assert ws.save(tab_only=True)
    head = ref.branch.head
    texts = _panel_texts(win)
    assert [t for t, _tip, _i in texts if t.startswith("\U0001f527 Fitted:")] == \
        ["\U0001f527 Fitted: PD150 injectors"]
    marked = [t for t, _tip, _i in texts if "set up for PD150" in t]
    assert len(marked) == 1                              # the saved version only
    # Version history: the mark in Status, the setup in every tooltip
    dlg = gui.VersionHistoryDialog(ws.primary, ws, version_note=gp.version_note(ws))
    try:
        rows = {dlg.tbl.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole): r
                for r in range(dlg.tbl.rowCount())}
        r = rows[head]
        assert "\U0001f527 set up for PD150 injectors" in dlg.tbl.item(r, 3).text()
        assert "Set up for: PD150 injectors" in dlg.tbl.item(r, 0).toolTip()
        others = [x for x in rows.values() if x != r]
        for x in others:
            assert "set up for" not in dlg.tbl.item(x, 3).text()
            assert "Set up for: what the car came with" in dlg.tbl.item(x, 0).toolTip()
    finally:
        dlg.close()
    # a plain dialog (no hook) is unchanged
    dlg = gui.VersionHistoryDialog(ws.primary, ws)
    try:
        assert all("set up for" not in dlg.tbl.item(x, 3).text()
                   for x in range(dlg.tbl.rowCount()))
    finally:
        dlg.close()

    # writing: no warning while the version is set up for what is fitted
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", lambda box: asked.append(box.text()))
    assert gp.hardware_mismatch(ws) == ""
    assert gp.before_flash_write(win, ws) is True and asked == []
    # other injectors fitted since: the write asks, Cancel stops it
    H.add(proj, sid, H.PD_INJECTORS, "PD130 injectors", {"preset": "pd130"},
          since="2026-10-20")
    text = [t for t, _tip, _i in _panel_texts(win) if "Fitted:" in t][0]
    assert "PD130 injectors — this version is set up for: PD150 injectors" in text
    assert gp.before_flash_write(win, ws) is False
    assert "fitted with PD130 injectors" in asked[-1] and "set up for PD150" in asked[-1]
    monkeypatch.setattr(QtWidgets.QMessageBox, "clickedButton", lambda box: next(
        b for b in box.buttons() if b.text() == "Write anyway"))
    assert gp.before_flash_write(win, ws) is True


@pytest.mark.slow
def test_the_ai_is_told_what_each_branch_is_set_up_for(tmp_path, monkeypatch):
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    from ovtsuite.ai import proposals as P
    from ovtsuite.ai.tools import Context, default_registry
    from ovtsuite.ai.workspace import FileWorkspace
    monkeypatch.setenv(P.ENV_DIR, str(tmp_path / "ai"))
    proj, s = ovt.import_file(LQ)
    b = proj.set(s.id).active
    H.record_fitted(proj, s.id, b.id, H.PD_INJECTORS, "PD150 injectors", PD150,
                    calibrated=True)
    path = proj.save(str(tmp_path / "car.ovt"))
    reg, ctx = default_registry(), Context(FileWorkspace(), client="pytest")
    ok, r = reg.call("open_file", ctx, {"path": path})
    assert ok, r
    ok, info = reg.call("project_info", ctx, {"handle": r["opened"][0]["handle"]})
    assert ok, info
    st = info["sets"][0]
    assert st["fitted_hardware"][0]["what"] == "PD150 injectors"
    assert st["branches"][0]["hardware_set_up_for"] == ["PD150 injectors"]

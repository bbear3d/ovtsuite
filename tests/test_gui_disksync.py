# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An open project's .ovt changed by another program (ovtsuite.gui_disksync).

Driven offscreen with a real ECU + TCU project: a project with nothing unsaved
reloads by itself; Save asks first and Reload & merge keeps both sides' work
(unsaved tab edits re-applied on top of the other program's new version, or
kept on a "(mine)" branch where they overlap); Overwrite and Cancel do what
they say; and an exit without saving never writes over the other program's
changes.
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

from conftest import BINARIES, ZF6HP_REFERENCE  # noqa: E402
from ovtsuite import project as ovt  # noqa: E402
from ovtsuite.project import autosave as _as  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions + the GUI

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
ZF = os.path.join(ZF6HP_REFERENCE, "5.0TFSI-Stock.bin")


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


def _need(*paths):
    for p in paths:
        if not os.path.exists(p):
            pytest.skip("reference binary missing: %s" % p)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    QtCore.QSettings.setPath(QtCore.QSettings.IniFormat,
                             QtCore.QSettings.UserScope, str(tmp_path))
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    from ovtsuite import gui
    monkeypatch.setattr(gui.Workspace, "show_file_info",
                        lambda self, *a, **k: None)
    for name in ("information", "warning"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: 0))
    return gui


@pytest.fixture
def win(G):
    w = G.MainWindow()
    w.clear_closed()
    w.show()
    yield w
    w.close_discarding()


@pytest.fixture
def car(tmp_path):
    _need(LQ, ZF)
    proj, _ecu = ovt.import_file(LQ)
    ovt.import_file(ZF, proj)
    path = str(tmp_path / "car.ovt")
    proj.name = "A6"
    proj.save(path)
    return path


@pytest.fixture
def answer(monkeypatch):
    """What the "changed on disk" question answers (and how often it came)."""
    from ovtsuite import gui_disksync as dsg
    got = {"answer": dsg.MERGE, "asked": 0}

    def ask(cls, win, doc, deleted):
        got["asked"] += 1
        return got["answer"]
    monkeypatch.setattr(dsg.DiskChangedDialog, "ask", classmethod(ask))
    return got


def _tab(w, name):
    return next(ws for ws in w.workspaces()
                if ws.project_ref is not None and ws.project_ref.set.name == name)


def _poke(ws, off, val):
    ws.primary.edit_bytes(off, bytes([val]))


def _other_program(path, fn):
    q = ovt.Project.load(path)
    fn(q)
    q.save(path)


def _commit_byte(q, set_name, branch, off, val):
    s = q.set(set_name)
    data = bytearray(q.member_bytes(s.id, "flash", branch=branch))
    data[off] = val
    q.commit(s.id, branch, {"flash": bytes(data)}, label="other program")


def _flash(path, set_name, branch=None):
    p = ovt.Project.load(path)
    s = p.set(set_name)
    return p.member_bytes(s.id, "flash", branch=branch)


def test_a_project_with_nothing_unsaved_reloads_by_itself(win, car, answer):
    from ovtsuite import gui_disksync as dsg
    from ovtsuite import gui_project as gp
    win.open_path(car)
    doc = gp.docs(win)[0]
    _other_program(car, lambda q: q.add_log("run.csv", b"t,rpm\n"))
    dsg.poll(win)                       # noticed; lets the write settle
    assert not doc.project.logs
    dsg.poll(win)
    assert [a.filename for a in doc.project.logs] == ["run.csv"]
    assert not doc.is_dirty(win) and answer["asked"] == 0
    assert doc.project.changed_on_disk() is None


def test_save_merges_unsaved_edits_on_top_of_the_other_programs(win, car, answer):
    win.open_path(car)
    ecu = _tab(win, "ECU")
    branch = ecu.project_ref.branch.name
    _poke(ecu, 0x100, 0x5A)                                    # unsaved, here
    _other_program(car, lambda q: (
        _commit_byte(q, "ECU", branch, 0x300, 0xA5),           # same branch
        _commit_byte(q, "TCU", None, 0x200, 0x77),
        q.set_info({"vin": "WAUZZZ4F06N000001"})))
    assert ecu.save()
    assert answer["asked"] == 1
    data = _flash(car, "ECU", branch)
    assert data[0x100] == 0x5A and data[0x300] == 0xA5         # both
    assert _flash(car, "TCU")[0x200] == 0x77
    assert ovt.Project.load(car).info["vin"] == "WAUZZZ4F06N000001"
    assert not ecu.is_dirty()


def test_overlapping_edits_go_on_a_mine_branch(win, car, answer):
    win.open_path(car)
    ecu = _tab(win, "ECU")
    branch = ecu.project_ref.branch.name
    _poke(ecu, 0x100, 0x5A)
    _other_program(car, lambda q: _commit_byte(q, "ECU", branch, 0x100, 0xA5))
    assert ecu.save()
    assert ecu.project_ref.branch.name == branch + " (mine)"
    assert _flash(car, "ECU", branch)[0x100] == 0xA5           # theirs
    assert _flash(car, "ECU", branch + " (mine)")[0x100] == 0x5A   # ours


def test_overwrite_and_cancel(win, car, answer):
    win.open_path(car)
    ecu = _tab(win, "ECU")
    _poke(ecu, 0x100, 0x5A)
    _other_program(car, lambda q: q.add_log("theirs.csv", b"1"))
    answer["answer"] = "cancel"
    assert not ecu.save()
    assert ecu.is_dirty()
    assert [a.filename for a in ovt.Project.load(car).logs] == ["theirs.csv"]
    answer["answer"] = "overwrite"
    assert ecu.save()
    p = ovt.Project.load(car)
    assert p.logs == [] and _flash(car, "ECU")[0x100] == 0x5A


def test_an_exit_without_saving_leaves_the_other_programs_changes(G, car, answer):
    w = G.MainWindow()
    w.open_path(car)
    _poke(_tab(w, "ECU"), 0x100, 0x5A)
    _other_program(car, lambda q: q.add_log("theirs.csv", b"1"))
    w.close_discarding()                # exit, not saving (writes definitions)
    assert [a.filename for a in ovt.Project.load(car).logs] == ["theirs.csv"]
    assert answer["asked"] == 0

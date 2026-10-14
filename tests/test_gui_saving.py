# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Saving a project without freezing the window, and a quick exit
(ovtsuite.gui_saving, ovtsuite.gui_project).

Driven offscreen with a synthetic project (an unrecognised image: no
reference binaries needed): the .ovt is written on a worker thread while the
window keeps painting and takes no input, a slow write shows "Saving...",
and errors and the "changed on disk" question come through as before.
"""
import os
import random
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:                              # no Qt, or no GL libs
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import project as ovt  # noqa: E402
from ovtsuite.project import autosave as _as  # noqa: E402

pytestmark = pytest.mark.slow          # the GUI


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Whatever this module's windows and dialogs remember goes to a file
    of the test's own, not the real settings (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def G(app, tmp_path, monkeypatch):
    monkeypatch.setenv(_as.ENV_DIR, str(tmp_path / "recovery"))
    from ovtsuite import gui
    for name in ("show_file_info", "_explain_limited_support"):
        monkeypatch.setattr(gui.Workspace, name, lambda self, *a, **k: None)
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
    """A one-set project of an image no module recognises."""
    p = ovt.Project("Synth")
    s = p.add_set("ECU", platform="unknown")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    p.add_root(s.id, {"flash": random.Random(3).randbytes(0x20000)},
               kind="read", label="Read")
    p.add_log("drive.csv", b"t,rpm\n" * 5000)
    path = str(tmp_path / "car.ovt")
    p.save(path)
    return path


def _tab(w):
    return next(ws for ws in w.workspaces() if ws.project_ref is not None)


def _poke(ws, off=0x100):
    b = ws.primary.binfile.data[off]
    ws.primary.edit_bytes(off, bytes([b ^ 0xFF]))
    return b ^ 0xFF


def test_run_hands_back_the_result_or_the_error(app):
    from ovtsuite import gui_saving as sv
    w = QtWidgets.QWidget()
    seen = []
    assert sv.run(w, lambda: (seen.append(threading.current_thread()), 7)[1]) == 7
    assert seen[0] is not threading.main_thread()
    with pytest.raises(KeyError):
        sv.run(w, lambda: {}["x"])
    assert not sv.saving(w)


def test_the_window_keeps_painting_and_shows_saving(app, monkeypatch):
    from ovtsuite import gui_saving as sv
    monkeypatch.setattr(sv, "SHOW_AFTER_MS", 20)
    w = QtWidgets.QWidget()
    w.show()
    ticks, shown, flags = [], [], []
    t = QtCore.QTimer()
    t.setInterval(10)

    def tick():
        ticks.append(1)
        shown.append(any(isinstance(x, sv.SavingDialog) and x.isVisible()
                         for x in QtWidgets.QApplication.topLevelWidgets()))
        flags.append(sv.saving(w))
    t.timeout.connect(tick)
    t.start()
    sv.run(w, lambda: time.sleep(0.4))
    t.stop()
    assert len(ticks) >= 5                    # the event loop kept running
    assert any(shown) and all(flags)
    QtWidgets.QApplication.processEvents()
    assert not any(isinstance(x, sv.SavingDialog) and x.isVisible()
                   for x in QtWidgets.QApplication.topLevelWidgets())


def test_a_project_save_is_written_on_a_worker_thread(win, car, monkeypatch):
    threads = []
    real = ovt.project.SaveJob.run

    def run(self):
        threads.append(threading.current_thread())
        return real(self)
    monkeypatch.setattr(ovt.project.SaveJob, "run", run)
    assert win.open_path(car)
    ws = _tab(win)
    v = _poke(ws)
    assert ws.save()
    assert threads and threads[0] is not threading.main_thread()
    assert not ws.is_dirty()
    p = ovt.Project.load(car)
    assert p.member_bytes(p.set("ECU").id, "flash")[0x100] == v
    assert len(p.set("ECU").checkpoints) == 2
    assert ws.project_ref.doc.project.changed_on_disk() is None


def test_a_failed_write_is_reported(win, car, monkeypatch):
    assert win.open_path(car)
    ws = _tab(win)
    _poke(ws)
    failed = []
    monkeypatch.setattr(type(ws), "_save_failed",
                        lambda self, path, ex: failed.append(ex))

    def boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(ovt.project._ar, "atomic_write", boom)
    assert not ws.save()
    assert failed and "disk full" in str(failed[0])


def test_changed_on_disk_still_asks(win, car, monkeypatch):
    from ovtsuite import gui_disksync as dsg
    from ovtsuite import gui_project as gp
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    asked = []

    def ask(cls, w, d, deleted):
        asked.append(deleted)
        return dsg.CANCEL if len(asked) == 1 else dsg.OVERWRITE
    monkeypatch.setattr(dsg.DiskChangedDialog, "ask", classmethod(ask))
    q = ovt.Project.load(car)
    q.name = "theirs"
    q._touch()
    q.save(car)
    doc.project.name = "mine"
    doc.project._touch()
    assert not dsg.write(win, doc)                 # Cancel: nothing written
    assert ovt.Project.load(car).name == "theirs"
    assert dsg.write(win, doc)                     # Overwrite
    assert ovt.Project.load(car).name == "mine" and asked == [False, False]

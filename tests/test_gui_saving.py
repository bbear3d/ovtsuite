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
errors and the "changed on disk" question come through as before, the
crash-recovery timer stops while the window asks whether to close, and an
exit does not rewrite a big project just for its definition cache.
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


def test_autosave_pauses_while_the_window_asks_to_close(G, win, car,
                                                        monkeypatch):
    assert win.open_path(car)
    _poke(_tab(win))
    timer = win.__dict__["_ovt_autosave_timer"]
    assert timer.isActive()
    during = []

    def exec_(self):
        during.append(timer.isActive())
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(G.UnsavedDialog, "exec", exec_)
    assert not win.close()                         # the user cancelled
    assert during == [False]
    assert timer.isActive()                        # running again


def test_an_exit_does_not_rewrite_a_big_project_for_its_cache(win, car,
                                                              monkeypatch):
    from ovtsuite import gui_project as gp
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    doc.__dict__["_cache_dirty"] = True
    st = os.stat(car)
    monkeypatch.setattr(gp, "EXIT_DEFN_MAX", 1)
    built = []
    monkeypatch.setattr(gp, "keep_definition", lambda ws: built.append(ws))
    assert not gp.write_definitions(win, doc, at_exit=True)
    assert built == []                             # nothing built at exit
    assert os.stat(car).st_mtime_ns == st.st_mtime_ns
    monkeypatch.setattr(gp, "EXIT_DEFN_MAX", 1 << 30)
    assert gp.write_definitions(win, doc, at_exit=True)   # small: written
    assert not doc.__dict__["_cache_dirty"]


# -- a rebuilt definition snapshot goes into the file by itself -------------
def _rebuilds(monkeypatch):
    """``keep_definition`` stores a snapshot the project lacks, as one built
    by the matcher would be (an unrecognised image has none to build)."""
    from ovtsuite import gui_project as gp

    def keep(ws):
        ref = ws.project_ref
        if ref is None or ws.primary is None or ws.is_dirty():
            return False
        proj = ref.doc.project
        if proj.cache(ref.sid, ref.bid) == b"DEFN":
            return False
        proj.set_cache(ref.sid, ref.bid, b"DEFN")
        ref.doc.__dict__["_cache_dirty"] = True
        return True
    monkeypatch.setattr(gp, "keep_definition", keep)
    return gp


def _until(cond, secs=30):
    end = time.monotonic() + secs
    while not cond():
        assert time.monotonic() < end, "timed out"
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)


def _disk_cache(path):
    p = ovt.Project.load(path)
    s = p.sets[0]
    return p.cache(s.id, s.active.id), p.info


def test_a_rebuilt_definition_is_written_alone_soon_after_opening(
        G, win, car, monkeypatch):
    from ovtsuite import gui_defncache as dc
    from ovtsuite import gui_disksync as dsg
    gp = _rebuilds(monkeypatch)
    monkeypatch.setattr(gp, "DEFN_IDLE_MS", 0)
    box = gp._sv._dialog
    monkeypatch.setattr(gp._sv, "_dialog",
                        lambda *a: pytest.fail("a Saving box for the cache"))
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    assert doc.project.set_info({"make": "VW"})     # the project's own edit
    _until(lambda: _disk_cache(car)[0] == b"DEFN" and not dc.running(doc)
           and "_defn_timer" not in doc.__dict__)
    assert "make" not in _disk_cache(car)[1]        # not saved with it
    assert doc.project.modified_since_save          # still unsaved ...
    assert [e.doc for e in gp.unsaved_entries(win)] == [doc]   # ... and asked
    assert not doc.__dict__.get("_cache_dirty")
    assert doc.project.changed_on_disk() is None
    monkeypatch.setattr(gp._sv, "_dialog", box)     # (the user's save may)
    monkeypatch.setattr(dsg, "resolve",
                        lambda *a: pytest.fail("taken as changed on disk"))
    assert gp.save_selection(win, doc, [])
    assert _disk_cache(car) == (b"DEFN", {"make": "VW"})
    assert not doc.project.modified_since_save


def test_a_rebuilt_definition_leaves_a_file_changed_on_disk_alone(
        G, win, car, monkeypatch):
    from ovtsuite import gui_defncache as dc
    gp = _rebuilds(monkeypatch)
    monkeypatch.setattr(gp, "DEFN_IDLE_MS", 1 << 30)
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    other = ovt.Project.load(car)
    other.set_info({"vin": "X"})
    other.save()
    st = os.stat(car)
    gp.leaving(_tab(win))
    dc.finish(doc)
    assert os.stat(car).st_mtime_ns == st.st_mtime_ns  # skipped, silently
    assert doc.project.changed_on_disk() == "changed"  # the save still asks
    assert doc.__dict__.get("_cache_dirty")             # for that save


def test_closing_the_binary_keeps_its_rebuilt_definition(G, win, car,
                                                         monkeypatch):
    from ovtsuite import gui_defncache as dc
    gp = _rebuilds(monkeypatch)
    monkeypatch.setattr(gp, "DEFN_IDLE_MS", 1 << 30)
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    doc.project.set_info({"make": "VW"})
    _tab(win).close_primary()
    dc.finish(doc)
    assert _disk_cache(car) == (b"DEFN", {})


def test_a_tab_taken_away_keeps_its_rebuilt_definition(G, win, car,
                                                       monkeypatch):
    from ovtsuite import gui_defncache as dc
    gp = _rebuilds(monkeypatch)
    monkeypatch.setattr(gp, "DEFN_IDLE_MS", 1 << 30)
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    doc.project.set_info({"make": "VW"})
    win._remove_workspace(_tab(win), remember=False)
    assert doc not in gp.docs(win)
    dc.finish(doc)
    assert _disk_cache(car) == (b"DEFN", {})


def test_dont_save_still_writes_a_rebuilt_definition(G, win, car,
                                                     monkeypatch):
    from ovtsuite import gui_defncache as dc
    gp = _rebuilds(monkeypatch)
    monkeypatch.setattr(gp, "DEFN_IDLE_MS", 1 << 30)
    assert win.open_path(car)
    doc = gp.docs(win)[0]
    doc.project.set_info({"make": "VW"})
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Discard))
    assert gp.close_project(win, doc)
    dc.finish(doc)
    assert _disk_cache(car) == (b"DEFN", {})


def test_quitting_waits_for_searches_once_not_one_by_one(app, monkeypatch):
    from ovtsuite import gui_search as gs
    from ovtsuite import search as S
    monkeypatch.setattr(gs, "JOIN_MS", 300)
    stop = threading.Event()

    def job(cancel, progress):
        stop.wait(5)                    # a search that is slow to notice
        return []
    threads = [gs._SearchThread(i, job, S.Cancel()) for i in range(3)]
    for t in threads:
        gs._LIVE.add(t)
        t.start()
    try:
        t0 = time.monotonic()
        gs._join_all()
        assert time.monotonic() - t0 < 0.8           # one deadline, not 3 x
        assert all(t.cancel.is_set for t in threads)  # all cancelled first
    finally:
        stop.set()
        for t in threads:
            t.wait(5000)
            gs._LIVE.discard(t)
    gs._hook_quit()
    gs._hook_quit()
    assert app.property("_ovt_search_join")


def test_definition_write_retakes_the_recovery_snapshot(monkeypatch):
    """A recovery entry taken before a cache-only write names the file's old
    hash; once the write lands, the next autosave takes a fresh one."""
    from ovtsuite import gui_defncache as dc, gui_project as gp

    class Saver:
        _token = ("old",)

    class Doc:
        saver = Saver()
    calls = []
    monkeypatch.setattr(gp, "autosave_soon", lambda win, doc: calls.append(doc))
    doc = Doc()
    dc._resnapshot("win", doc)
    assert calls == [doc] and doc.saver._token is None
    dc._resnapshot("win", doc)          # nothing snapshotted since: no-op
    assert calls == [doc]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The File menu in the project model (ovtsuite.gui_files): import into a
new or this project (in a new tab or this one), a project version as the
comparison, recent projects, closed projects in the history, drops, project
locks and handing a file to the running window."""
import os
import shutil
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

pytestmark = pytest.mark.slow

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
KN = os.path.join(BINARIES, "03G906016KN.bin")
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
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))
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


def _ptabs(w):
    return [ws for ws in w.workspaces() if ws.project_ref is not None]


def test_import_into_a_new_project_then_into_this_one(win, tmp_path):
    from ovtsuite import gui_files as gf
    _need(LQ, ZF)
    assert gf.import_files(win, [LQ], target="new")
    (t,) = _ptabs(win)
    doc = t.project_ref.doc
    assert doc.path is None and len(doc.project.sets) == 1   # untitled, unsaved
    assert gf.import_files(win, [ZF], target="current")
    tabs = _ptabs(win)
    assert len(tabs) == 2 and {x.project_ref.doc for x in tabs} == {doc}
    assert len(doc.project.sets) == 2
    # into this tab: the tab in front is replaced by the import
    n = win.tabs.count()
    idx = win.tabs.indexOf(win.current)
    assert gf.import_files(win, [LQ], target="current", in_tab=True)
    assert win.tabs.count() == n and win.tabs.indexOf(win.current) == idx
    assert len(doc.project.sets) == 3


def test_a_project_version_as_the_comparison(win, car, monkeypatch):
    from ovtsuite import gui_files as gf
    from ovtsuite.project import Project
    _need(KN)
    assert gf.import_files(win, [KN], target="new")
    ws = win.current
    proj = Project.load(car)
    ecu = next(s for s in proj.sets if s.platform == "edc16")
    # one EDC16 binary with one version in the project: nothing to ask
    assert gf.VersionPicker.only_choice(proj, "edc16") is not None
    assert gf.compare_with_file(win, car)
    assert ws.comparison is not None
    assert ws.comparison.version_of["project"] == os.path.abspath(car)
    from ovtsuite.recent import load_recent_comparisons
    (ref,) = load_recent_comparisons()
    assert ref.startswith(os.path.abspath(car) + "#ovt:" + ecu.id)
    ws.close_comparison()
    assert gf.compare_with_file(win, ref) and ws.comparison is not None


def test_the_picker_lists_sets_branches_and_versions(app, car):
    from ovtsuite import gui_files as gf
    from ovtsuite.project import Project
    proj = Project.load(car)
    dlg = gf.VersionPicker(proj, "Compare", platform="zf6hp")
    sid, bid, cid = dlg.choice
    s = proj.set(sid)
    assert s.platform == "zf6hp" and s.branch(bid).head == cid
    ecu = dlg.tree.topLevelItem(0)
    assert ecu.isDisabled() == (proj.sets[0].platform != "zf6hp")


def test_the_picker_offers_the_latest_version_first(app, car):
    """Newest first, the branch's latest picked: the OVT Flasher writes what
    is picked, and that must not be the original by default."""
    from ovtsuite import gui_files as gf
    from ovtsuite.project import Project
    proj = Project.load(car)
    s = next(x for x in proj.sets if x.platform == "zf6hp")
    st = proj.head_state(s.id)
    main = next(iter(st))
    for i in range(2):
        b = bytearray(st[main])
        b[0x100 + i] ^= 0xFF
        st = dict(st, **{main: bytes(b)})
        proj.commit(s.id, None, st, label="edit %d" % i)
    dlg = gf.VersionPicker(proj, "Write", platform="zf6hp")
    sid, bid, cid = dlg.choice
    assert cid == s.branch(bid).head
    branch = dlg.tree.currentItem().parent()
    assert branch.child(0).text(0) == "edit 1  (latest)"
    assert branch.child(branch.childCount() - 1).data(
        0, QtCore.Qt.ItemDataRole.UserRole)[2] == s.lineage(cid)[-1].id


def test_recent_lists_projects_only(win, car, tmp_path):
    from ovtsuite import gui_files as gf
    from ovtsuite.recent import add_recent
    add_recent(LQ)
    add_recent(car)
    menu = QtWidgets.QMenu()
    gf.fill_recent_menu(win, menu)
    texts = [a.text() for a in menu.actions() if a.text()]
    assert "car" in texts and not any("03G906016LQ" in t for t in texts)


def test_a_closed_project_comes_back_whole(win, car):
    from ovtsuite import gui_project as gp
    assert gp.open_project(win, car)
    doc = _ptabs(win)[0].project_ref.doc
    assert gp.close_project(win, doc)
    (ent,) = win.closed_history()
    assert ent["kind"] == "project" and len(ent["tabs"]) == 2
    menu = QtWidgets.QMenu()
    win.fill_history_menu(menu)
    assert "A6" in menu.actions()[0].text()
    back = win.reopen_last_closed()
    assert back is not None
    assert sorted(t.project_ref.set.name for t in _ptabs(win)) == ["ECU", "TCU"]
    assert win.closed_history() == []


def test_dropping_a_project_opens_it(win, car):
    from ovtsuite import gui_files as gf
    assert gf.handle_drop(win, [car])
    assert len(_ptabs(win)) == 2


def test_a_project_held_by_another_process_is_refused(win, car, monkeypatch):
    import hashlib
    from ovtsuite import gui_files as gf
    from ovtsuite import gui_project as gp
    key = os.path.normcase(os.path.abspath(car))
    other = QtCore.QLockFile(os.path.join(
        gf._lock_dir(), hashlib.sha1(key.encode("utf-8")).hexdigest() + ".lock"))
    assert other.tryLock(0)
    try:
        # this process holds it through another object: like another process
        monkeypatch.setattr(gf, "_HELD", {})
        assert not gp.open_project(win, car)
        assert not _ptabs(win)
    finally:
        other.unlock()
    assert gp.open_project(win, car)


def test_a_second_start_hands_its_file_to_the_first(app, monkeypatch, tmp_path):
    """The second start is another process: it sends the path and waits for
    the first window's "ok" before exiting."""
    import sys
    from ovtsuite import gui_instance as gi
    name = "ovt-test-%d" % os.getpid()
    monkeypatch.setattr(gi, "server_name", lambda: name)
    w = QtWidgets.QWidget()
    got = []
    assert gi.listen(w, got.append) is not None
    f = str(tmp_path / "x.ovt")
    code = ("import sys; from PySide6 import QtCore; "
            "app = QtCore.QCoreApplication([]); "
            "from ovtsuite import gui_instance as gi; "
            "gi.server_name = lambda: %r; "
            "sys.exit(0 if gi.hand_over(%r) else 3)" % (name, f))
    proc = QtCore.QProcess()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc.setWorkingDirectory(root)
    proc.start(sys.executable, ["-c", code])
    t = QtCore.QElapsedTimer()
    t.start()
    while (proc.state() != QtCore.QProcess.ProcessState.NotRunning
           and t.elapsed() < 20000):
        app.processEvents()
        proc.waitForFinished(20)
    while not got and t.elapsed() < 22000:
        app.processEvents()
    assert proc.exitCode() == 0, bytes(proc.readAllStandardError())
    assert got == [os.path.abspath(f)]
    w.__dict__["_ovt_instance_server"].close()


def test_a_window_that_does_not_answer_is_not_trusted(app, monkeypatch):
    """A socket whose window hung (it never answers) must not make a new
    start exit silently -- the app would just not open."""
    from PySide6 import QtNetwork
    from ovtsuite import gui_instance as gi
    name = "ovt-mute-%d" % os.getpid()
    monkeypatch.setattr(gi, "server_name", lambda: name)
    mute = QtNetwork.QLocalServer()
    QtNetwork.QLocalServer.removeServer(name)
    assert mute.listen(name)          # listens, never reads or answers
    try:
        assert gi.hand_over("x.ovt", ack_ms=300) is False
    finally:
        mute.close()


def test_file_information_shows_the_project_and_can_stay_quiet(win, car):
    """File information on a project tab adds the project's side; the
    "Show this when a file is opened" box is remembered and silences the
    popup on opening (Ctrl+I still shows it)."""
    from ovtsuite import branding
    from ovtsuite import gui_fileinfo as fi
    from ovtsuite import gui_project as gp
    st = QtCore.QSettings(*branding.QSETTINGS)
    st.remove(fi.SETTING)                       # whatever an earlier run left
    st.sync()
    assert gp.open_project(win, car)
    ecu = next(t for t in _ptabs(win) if t.project_ref.set.name == "ECU")
    rows = dict(fi.project_rows(ecu))
    assert rows["Project"] == "A6" and rows["Project file"] == car
    assert rows["Branch"] == "Stock" and rows["Unsaved changes"] == "no"
    assert "flash" in rows["Parts"]
    assert fi.project_rows(ecu, ecu.comparison) == []    # not for the comparison
    assert fi.show_on_open()                              # on by default
    dlg = fi.show(ecu, loaded=True)
    assert dlg is not None and dlg.on_open.isChecked()
    dlg.on_open.setChecked(False)
    dlg.close()
    assert not fi.show_on_open()
    assert fi.show(ecu, loaded=True) is None              # opening: quiet
    d2 = fi.show(ecu)                                     # Ctrl+I: shown
    assert d2 is not None
    d2.close()
    st.remove(fi.SETTING)
    st.sync()


def test_an_empty_tab_shows_the_start_page_with_recent_projects(win, car):
    from ovtsuite import gui_project as gp
    from ovtsuite import gui_start
    ws = win.current
    assert ws.is_empty()
    assert ws._start_stack.currentWidget() is ws._start_page
    assert gp.open_project(win, car)
    gp.close_project(win, gp.docs(win)[0])
    ws = win.current
    assert ws.is_empty() and ws._start_stack.currentWidget() is ws._start_page
    page = ws._start_page
    page.refresh()
    paths = [page.recent.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
             for i in range(page.recent.count())]
    assert os.path.normcase(os.path.abspath(car)) in [
        os.path.normcase(os.path.abspath(p)) for p in paths if p]
    item = next(page.recent.item(i) for i in range(page.recent.count())
                if page.recent.item(i).data(QtCore.Qt.ItemDataRole.UserRole))
    page._open_item(item)
    assert _ptabs(win)
    t = _ptabs(win)[0]
    assert t._start_stack.currentIndex() == 0          # the tab's own view
    assert isinstance(page, gui_start.StartPage)

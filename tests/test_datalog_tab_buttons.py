# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The buttons inside a controller tab of the logger, clicked for real.

Regression (2026-10-25): a log started and stopped with the tab's own Start
logging / Stop was never kept -- not in the project, not handed to the
viewer. ``stop_btn.clicked`` was wired straight to ``_stop_logging(finish=
True)``, and ``clicked`` sends ``checked`` (False) to a slot that takes an
argument, so the click ran ``_stop_logging(finish=False)``: the master Stop's
"return (path, rows) for me to finish", with nobody there to finish it. The
earlier tests called ``_stop_logging()`` directly and never saw it.

Also: the ends a recording can meet other than Stop (Disconnect, the session
ending, closing the window, dual logging turned off) keep it the same way;
and the cell browser's "Log every cell of this group" box.
"""
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import project as ovt  # noqa: E402
from ovtsuite.datalog import derived as D  # noqa: E402
from ovtsuite.datalog import gui_log as GL  # noqa: E402
from ovtsuite.datalog import logfile as L  # noqa: E402

pytestmark = pytest.mark.slow          # the logger window


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(until, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not until():
        QtWidgets.QApplication.processEvents()
        time.sleep(0.005)
    return until()


class _Win(QtWidgets.QMainWindow):
    """A main window with one tab, optionally in a project. ``keep_log`` is
    the hook ConnPane._keep_log calls; it puts the log in the project the way
    gui_project.keep_log does."""

    primary = None

    def __init__(self, ws=None):
        super().__init__()
        self.current = ws
        self.kept = []

    def workspaces(self):
        return [self.current] if self.current is not None else []

    def keep_log(self, ws, path, _pane):
        ref = ws.project_ref
        with open(path, "rb") as fh:
            ref.doc.project.add_log(os.path.basename(path), fh.read(), set_id=ref.sid)
        self.kept.append(path)
        return True


def _project_ws(tmp_path):
    proj = ovt.Project("car")
    s = proj.add_set("ECU", platform="unknown")
    work = tmp_path / "proj-tmp"
    work.mkdir()
    doc = SimpleNamespace(project=proj, tmpdir=str(work), title="car")
    return SimpleNamespace(project_ref=SimpleNamespace(doc=doc, sid=s.id, set=s),
                           primary=None)


class _FakeWorker:
    """A connected LogWorker without a car: Start opens the file, rows are
    written by :func:`_drive`, Stop closes it."""

    def __init__(self):
        self.calls = []
        self._stop = False
        self.running = True
        self._fh = self._writer = None
        self._gen = 0

    def start_logging(self, channels, path):
        self.calls.append("start_logging")
        self._fh = open(path, "w", encoding="utf-8", newline="")
        self._writer = L.KwhackWriter(self._fh, list(channels))
        self._gen += 1
        return self._gen

    def row(self, stamp, texts):
        self._writer.row(stamp, texts)

    def stop_logging(self):
        self.calls.append("stop_logging")
        self._close()

    def _close(self):
        if self._fh is not None:
            self._fh.close()
        self._fh = self._writer = None

    def pause(self, on):
        self.calls.append(("pause", on))

    def stop(self):
        self.calls.append("stop")
        self._stop = True
        self._close()               # the teardown closes the file first
        self.running = False

    def abort_transport(self):
        return None

    def set_browse_group(self, g):
        pass

    def isRunning(self):
        return self.running

    def wait(self, ms=0):
        return not self.running


def _dialog(app, tmp_path, monkeypatch, ws=None):
    monkeypatch.setattr(GL, "_prefs", lambda: QtCore.QSettings(
        str(tmp_path / "prefs.ini"), QtCore.QSettings.Format.IniFormat))
    monkeypatch.setattr(GL, "retire_thread", lambda t: None)
    from ovtsuite import recent as _recent
    monkeypatch.setattr(_recent, "get_last_dir", lambda *_a: str(tmp_path))
    monkeypatch.setattr(_recent, "set_last_dir", lambda *_a: None)
    win = _Win(ws)
    d = GL.DataLogDialog(win)
    return win, d


@pytest.fixture
def plain(app, tmp_path, monkeypatch):
    """The logger with no project: logs go to a file."""
    win, d = _dialog(app, tmp_path, monkeypatch)
    yield win, d
    for p in d.panes:
        p._worker = None
    d.close()


@pytest.fixture
def in_project(app, tmp_path, monkeypatch):
    """The logger with this controller's binary in a project tab."""
    win, d = _dialog(app, tmp_path, monkeypatch, _project_ws(tmp_path))
    yield win, d
    for p in d.panes:
        p._worker = None
    d.close()


def _connect(pane):
    w = _FakeWorker()
    pane._worker = w
    pane._channels = [L.Channel(1, 0, "Engine Speed", "rpm")]
    pane._set_logging_ui(False)
    return w


def _drive(pane, w, rows=3):
    t0 = datetime(2026, 10, 25, 9, 0, 0)
    for i in range(rows):
        stamp = t0 + timedelta(seconds=i * 0.2)
        w.row(stamp, ["%d rpm" % (800 + i)])
        pane._on_logged(pane._rec_gen, stamp, ["%d rpm" % (800 + i)], [800.0 + i])


# ------------------------------------------------------------ the bug
def test_the_stop_button_slot_takes_no_argument_from_clicked(app):
    """The root cause, in isolation: ``clicked`` hands ``checked`` to a slot
    that accepts an argument."""
    seen = []

    class _O:
        def f(self, finish=True):
            seen.append(finish)
    b = QtWidgets.QPushButton()
    o = _O()
    b.clicked.connect(o.f)
    b.click()
    assert seen == [False], "PySide passes checked=False -- why the wiring changed"


def test_tab_start_and_stop_keep_the_log_in_the_project(in_project):
    win, d = in_project
    pane = d.pane
    assert not pane.keep_in_project.isHidden() and pane._into_project()
    w = _connect(pane)
    finished = []
    pane.log_finished.connect(finished.append)
    pane.start_btn.click()
    assert pane.is_logging() and w.calls == ["start_logging"]
    path = pane._log_path
    assert path.startswith(win.current.project_ref.doc.tmpdir)
    _drive(pane, w, rows=4)
    pane.stop_btn.click()
    assert not pane.is_logging() and pane.is_connected()
    assert w.calls == ["start_logging", "stop_logging"]
    assert _pump(lambda: finished == [path])
    assert win.kept == [path]
    proj = win.current.project_ref.doc.project
    assert len(proj.logs) == 1
    assert "kept in the project" in d.status.text()


def test_tab_start_and_stop_write_the_file_and_view_it(plain, tmp_path, monkeypatch):
    win, d = plain
    pane = d.pane
    assert pane.keep_in_project.isHidden()
    w = _connect(pane)
    finished = []
    pane.log_finished.connect(finished.append)
    viewed = []
    monkeypatch.setattr(d, "_load_view", viewed.append)
    pane.log_finished.disconnect()
    pane.log_finished.connect(finished.append)
    pane.log_finished.connect(d._load_view)
    want = pane.path_edit.text()
    pane.start_btn.click()
    assert pane._log_path == want
    _drive(pane, w, rows=3)
    pane.stop_btn.click()
    assert _pump(lambda: finished == [want])
    assert viewed == [want]
    assert os.path.exists(want)
    lines = open(want, encoding="utf-8").read().splitlines()
    assert any("801 rpm" in ln for ln in lines)
    assert "3 rows written to %s" % want in d.status.text()
    assert win.kept == []
    # the box has a fresh name for the next log
    assert pane.path_edit.text() != want


def test_tab_stop_and_master_stop_keep_the_same_way(in_project):
    win, d = in_project
    pane = d.pane
    w = _connect(pane)
    pane.start_btn.click()
    first = pane._log_path
    _drive(pane, w)
    pane.stop_btn.click()
    assert _pump(lambda: win.kept == [first])
    d._refresh_master()
    d.m_start.click()
    second = pane._log_path
    assert second and second != first
    _drive(pane, w)
    d.m_stop.click()
    assert _pump(lambda: win.kept == [first, second])
    assert len(win.current.project_ref.doc.project.logs) == 2


def test_tab_pause_reaches_the_worker_and_stop_still_keeps(in_project):
    win, d = in_project
    pane = d.pane
    w = _connect(pane)
    pane.start_btn.click()
    path = pane._log_path
    pane.pause_btn.click()
    assert ("pause", True) in w.calls and pane.pause_btn.isChecked()
    pane.pause_btn.click()
    assert ("pause", False) in w.calls
    pane.pause_btn.click()                          # paused again, then Stop
    pane.stop_btn.click()
    assert not pane.pause_btn.isChecked()
    assert _pump(lambda: win.kept == [path])


def test_disconnect_while_logging_keeps_the_log(in_project):
    win, d = in_project
    pane = d.pane
    w = _connect(pane)
    pane.start_btn.click()
    path = pane._log_path
    _drive(pane, w)
    pane.connect_btn.click()                        # Disconnect
    assert "stop" in w.calls
    pane._on_ended()                                # the worker's ``ended``
    assert win.kept == [path]
    assert "Session ended" in d.status.text()
    pane._on_ended()                                # never twice
    assert win.kept == [path]


def test_closing_the_window_while_logging_keeps_the_log(in_project):
    win, d = in_project
    pane = d.pane
    w = _connect(pane)
    pane.start_btn.click()
    path = pane._log_path
    _drive(pane, w)
    d.close()
    # kept on the close itself: ``ended`` is queued, and may never come
    assert win.kept == [path]
    assert pane._worker is None
    pane._on_ended()                                # it comes after all
    assert win.kept == [path]


def test_turning_dual_off_while_the_second_logs_keeps_its_log(plain, tmp_path):
    win, d = plain
    d.dual_chk.setChecked(True)
    second = d.panes[1]
    w = _connect(second)
    finished = []
    second.log_finished.connect(finished.append)
    second.start_btn.click()
    path = second._log_path
    _drive(second, w)
    d.dual_chk.setChecked(False)
    assert finished == [path] and os.path.exists(path)


def test_a_worker_still_running_at_close_hands_on_from_ended(in_project, monkeypatch):
    monkeypatch.setattr(GL, "CLOSE_WAIT_S", 0.05)
    monkeypatch.setattr(GL, "ABORT_WAIT_S", 0.05)
    win, d = in_project
    pane = d.pane
    w = _connect(pane)
    pane.start_btn.click()
    path = pane._log_path
    _drive(pane, w)
    w.stop = lambda: (w.calls.append("stop"), setattr(w, "_stop", True))
    d.close()
    assert win.kept == [], "its file may still be open"
    w._close()
    pane._on_ended()
    assert win.kept == [path]


# ------------------------------------------------------------ select all
def _browse_rows(pane, n=4):
    """List ``n`` cells of group 1, as the ECU's answer would."""
    pane._fill_browse(pane._current_group(),
                      [(f, "", None) for f in range(n)])
    return pane._browse_items()


def test_select_all_ticks_every_cell_and_unticks_them_again(plain):
    _win, d = plain
    pane = d.pane
    pane._channels = []
    items = _browse_rows(pane)
    g = pane._current_group()
    box = pane.browse_all
    assert box.isEnabled() and box.checkState() == QtCore.Qt.Unchecked
    box.click()
    assert all(it.checkState() == QtCore.Qt.Checked for it in items)
    assert {c.key for c in pane._channels} >= {(g, f) for f in range(4)}
    assert box.checkState() == QtCore.Qt.Checked
    assert pane.sel.rowCount() == len(pane._channels)
    assert "4 cells ticked" in d.status.text()
    box.click()
    assert all(it.checkState() == QtCore.Qt.Unchecked for it in items)
    assert pane._channels == [] and pane.sel.rowCount() == 0
    assert box.checkState() == QtCore.Qt.Unchecked


def test_select_all_shows_partial_and_follows_single_ticks(plain):
    _win, d = plain
    pane = d.pane
    pane._channels = []
    items = _browse_rows(pane)
    box = pane.browse_all
    items[1].setCheckState(QtCore.Qt.Checked)          # one by hand
    assert box.checkState() == QtCore.Qt.PartiallyChecked
    assert len(pane._channels) == 1
    box.click()                                         # partial -> all
    assert box.checkState() == QtCore.Qt.Checked
    assert len(pane._channels) == 4
    items[0].setCheckState(QtCore.Qt.Unchecked)
    assert box.checkState() == QtCore.Qt.PartiallyChecked
    items[0].setCheckState(QtCore.Qt.Checked)
    assert box.checkState() == QtCore.Qt.Checked


def test_select_all_leaves_other_groups_cells_alone(plain):
    _win, d = plain
    pane = d.pane
    other = pane._namer.channel(200, 0)
    pane._channels = [other]
    _browse_rows(pane)
    pane.browse_all.click()
    pane.browse_all.click()
    assert [c.key for c in pane._channels] == [other.key]


def test_select_all_follows_a_change_of_group(plain):
    _win, d = plain
    pane = d.pane
    pane._channels = []
    _browse_rows(pane)
    pane.browse_all.click()
    assert pane.browse_all.checkState() == QtCore.Qt.Checked
    g = pane._current_group()
    pane.group_spin.setValue(g + 1 if g < 255 else g - 1)   # another group
    assert pane.browse_all.checkState() == QtCore.Qt.Unchecked
    pane.group_spin.setValue(g)
    _browse_rows(pane)
    assert pane.browse_all.checkState() == QtCore.Qt.Checked
    pane._clear_selected()
    assert pane.browse_all.checkState() == QtCore.Qt.Unchecked


def test_select_all_adds_end_of_injection_as_ticking_by_hand(plain, monkeypatch):
    """Each cell goes through the same rule as a single tick: start and
    duration in one group add the calculated end of injection, once."""
    _win, d = plain
    pane = d.pane
    pane._channels = []
    roles = {0: "soi", 1: "dur"}
    monkeypatch.setattr(D, "_role", lambda c: None if c.is_derived
                        else roles.get(c.field))
    _browse_rows(pane)
    pane.browse_all.click()
    assert sum(1 for c in pane._channels if c.derived == D.EOI) == 1
    pane.browse_all.click()
    assert pane._channels == []


def test_select_all_is_off_for_a_group_with_no_cells(plain):
    _win, d = plain
    pane = d.pane
    pane.browse.setRowCount(0)
    pane._sync_browse_all()
    assert not pane.browse_all.isEnabled()
    assert pane.browse_all.checkState() == QtCore.Qt.Unchecked

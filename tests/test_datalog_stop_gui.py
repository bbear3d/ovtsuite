# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Stop and Disconnect in the logger window.

Stop ends the recording and nothing else: the connection stays up and goes
back to browsing, and it must return at once -- keeping the log, reading it
into the viewer and (in OVT Logger) rewriting the .ovt all happen after the
buttons have repainted, off the click. Disconnect tears the link down, in
bounded time: past a grace period the transport is cut under a worker that
has not let go.
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.datalog import gui_log as GL  # noqa: E402
from ovtsuite.datalog import logfile as L  # noqa: E402
from ovtsuite.datalog import logview as LV  # noqa: E402
from ovtsuite.flash import j2534 as J  # noqa: E402
from ovtsuite.flash.transport import KLine, KLineError  # noqa: E402


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
    def workspaces(self):
        return []

    current = None
    primary = None


@pytest.fixture
def dlg(app, tmp_path, monkeypatch):
    monkeypatch.setattr(GL, "_prefs", lambda: QtCore.QSettings(
        str(tmp_path / "prefs.ini"), QtCore.QSettings.Format.IniFormat))
    win = _Win()
    d = GL.DataLogDialog(win)
    yield d
    for p in d.panes:
        p._worker = None
    d.close()


class _FakeWorker:
    """Records what the pane asks of its worker."""

    def __init__(self):
        self.calls = []
        self._stop = False
        self.running = True

    def stop_logging(self):
        self.calls.append("stop_logging")

    def stop(self):
        self.calls.append("stop")
        self._stop = True

    def abort_transport(self):
        self.calls.append("abort")
        return "bridge"

    def isRunning(self):
        return self.running

    def wait(self, ms=0):
        return not self.running


def _recording(pane, tmp_path, rows=3, gen=7):
    """Put ``pane`` in the middle of a recording of ``rows`` rows."""
    path = str(tmp_path / ("%s.csv" % pane.title()))
    chans = [L.Channel(1, 0, "Engine Speed", "rpm")]
    log = L.DataLog(channels=chans)
    t0 = datetime(2026, 10, 14, 9, 0, 0)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = L.KwhackWriter(fh, chans)
        for i in range(rows):
            w.row(t0 + timedelta(seconds=i * 0.2), ["%d rpm" % (800 + i)])
    w = _FakeWorker()
    pane._worker = w
    pane._channels = chans
    pane._live = L.DataLog(channels=list(chans))
    pane.chart.set_data([], [[]], ["Engine Speed"], ["rpm"])
    pane._log_path = path
    pane._rec_gen = gen
    pane._logging = True
    pane._t0 = None
    for i in range(rows):
        pane._on_logged(gen, t0 + timedelta(seconds=i * 0.2), ["%d rpm" % (800 + i)], [800.0 + i])
    return w, path


def test_stop_only_stops_recording_and_defers_the_rest(dlg, tmp_path):
    pane = dlg.pane
    w, path = _recording(pane, tmp_path)
    finished = []
    pane.log_finished.connect(finished.append)
    pane._stop_logging()
    # the click itself: file closed, buttons flipped -- the link untouched
    assert w.calls == ["stop_logging"]
    assert not pane.is_logging() and pane.is_connected()
    assert finished == [], "keeping and viewing the log follow the repaint"
    assert _pump(lambda: finished == [path])


def test_a_row_in_flight_at_stop_is_dropped(dlg, tmp_path):
    pane = dlg.pane
    _w, _path = _recording(pane, tmp_path, rows=3, gen=7)
    assert pane._live.rows == 3
    pane._on_logged(6, datetime.now(), ["1 rpm"], [1.0])      # an older recording
    assert pane._live.rows == 3
    pane._stop_logging()
    pane._on_logged(7, datetime.now(), ["2 rpm"], [2.0])      # queued before Stop
    assert pane._live.rows == 3 and len(pane.chart.times) == 3


def test_disconnect_after_stop_does_not_hand_the_log_on_again(dlg, tmp_path, monkeypatch):
    monkeypatch.setattr(GL, "retire_thread", lambda t: None)
    pane = dlg.pane
    _recording(pane, tmp_path)
    finished = []
    pane.log_finished.connect(finished.append)
    pane._stop_logging()
    assert _pump(lambda: len(finished) == 1)
    pane._on_ended()                                # the session ends later
    QtWidgets.QApplication.processEvents()
    assert len(finished) == 1
    assert "Log saved to" not in dlg.status.text()


def test_a_session_that_ends_mid_recording_still_keeps_the_log(dlg, tmp_path, monkeypatch):
    monkeypatch.setattr(GL, "retire_thread", lambda t: None)
    pane = dlg.pane
    _w, path = _recording(pane, tmp_path)
    finished = []
    pane.log_finished.connect(finished.append)
    pane._on_ended()
    assert finished == [path]
    assert "Session ended" in dlg.status.text()


def test_master_stop_stops_every_pane_before_keeping_any(dlg, tmp_path):
    dlg.dual_chk.setChecked(True)
    order = []
    for p in dlg.panes:
        _recording(p, tmp_path)
        p.log_finished.connect(lambda path, _p=p: order.append(("kept", _p.title())))
        p._worker.stop_logging = (lambda _p=p: order.append(("stopped", _p.title())))
    dlg._master_stop()
    assert [o[0] for o in order] == ["stopped", "stopped"]
    assert _pump(lambda: len(order) == 4)
    assert [o[0] for o in order] == ["stopped", "stopped", "kept", "kept"]


def test_disconnect_arms_a_watchdog_and_stop_never_does(dlg, tmp_path, monkeypatch):
    monkeypatch.setattr(GL, "DISCONNECT_GRACE_MS", 20)
    pane = dlg.pane
    w, _path = _recording(pane, tmp_path)
    pane._stop_logging()
    QtWidgets.QApplication.processEvents()
    time.sleep(0.05)
    QtWidgets.QApplication.processEvents()
    assert "abort" not in w.calls and "stop" not in w.calls
    pane.disconnect_now()
    assert w.calls[-1] == "stop"
    assert _pump(lambda: "abort" in w.calls, 2.0)
    # a worker that wound down in time is left alone
    w2 = _FakeWorker()
    w2.running = False
    pane._worker = w2
    pane.disconnect_now()
    time.sleep(0.05)
    QtWidgets.QApplication.processEvents()
    assert "abort" not in w2.calls


def test_closing_waits_once_for_everyone(dlg, monkeypatch):
    """Two stuck workers: one shared deadline, then both transports cut."""
    monkeypatch.setattr(GL, "CLOSE_WAIT_S", 0.2)
    monkeypatch.setattr(GL, "ABORT_WAIT_S", 0.1)
    held = []
    monkeypatch.setattr(GL, "retire_thread", held.append)
    dlg.dual_chk.setChecked(True)
    waits = []

    class _Stuck(_FakeWorker):
        def wait(self, ms=0):
            waits.append(ms)
            time.sleep(ms / 1000.0)
            return False
    workers = []
    for p in dlg.panes:
        p._worker = _Stuck()
        workers.append(p._worker)
    t0 = time.monotonic()
    GL.ConnPane.wind_down(dlg.panes)
    took = time.monotonic() - t0
    assert took < 0.6, "the waits share one deadline"
    assert all("abort" in w.calls for w in workers)
    assert held == workers and all(p._worker is None for p in dlg.panes)


# ------------------------------------------------------------ the worker
def test_worker_generations_mark_each_recording(tmp_path):
    w = GL.LogWorker("x", "EDC16", "Low", False)
    g1 = w.start_logging([L.Channel(1, 0, "a", "")], str(tmp_path / "a.csv"))
    w.stop_logging()
    g2 = w.start_logging([L.Channel(1, 0, "a", "")], str(tmp_path / "b.csv"))
    w.stop_logging()
    assert g2 > g1 and w._gen > g2


def test_abort_transport_only_after_stop_and_only_what_can_be_cut(monkeypatch):
    w = GL.LogWorker("x", "EDC16", "Low", False)
    killed = []

    class _Bridge(J.BridgePassThru):
        def __init__(self):
            pass

        def kill(self):
            killed.append(1)
    w._can = _Bridge()
    assert w.abort_transport() is None and not killed, "never without Disconnect"
    w.stop()
    assert w.abort_transport() == "bridge" and killed == [1]

    class _Serial(KLine):
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True
    w2 = GL.LogWorker("x", "EDC16", "Low", False)
    w2._link = _Serial()
    w2.stop()
    assert w2.abort_transport() == "serial" and w2._link.cancelled
    w3 = GL.LogWorker("x", "EDC16", "Low", False)
    w3._can = object()                      # an in-process DLL: left to the reaper
    w3.stop()
    assert w3.abort_transport() is None


class _Link:
    read_timeout = 8.0

    def __init__(self, *a, **k):
        self.closed = False

    def close(self):
        self.closed = True


def _run_worker(monkeypatch, session_cls):
    monkeypatch.setattr(GL, "KLine", _Link)
    monkeypatch.setattr(GL, "is_j2534", lambda port: False)
    monkeypatch.setattr(GL._diag, "DiagSession", session_cls)
    w = GL.LogWorker("COM9", "EDC16", "Low", False)
    failed, connected = [], []
    w.failed.connect(failed.append)
    w.connected.connect(lambda *a: connected.append(a))
    session_cls.worker = w
    monkeypatch.setattr(GL.time, "sleep", lambda s: None)
    w._run_session()                        # on this thread: signals are direct
    return w, failed, connected


def test_disconnect_during_reconnect_raises_no_error_box(monkeypatch):
    class _Sess:
        identity, baud = "ECU", 10400

        def __init__(self, link, ecu, log):
            self.k = link
            self.closed = False

        def connect(self, *a, **k):
            pass

        def read_group(self, g):
            raise KLineError("line dropped")

        def reconnect(self, is_stop=None):
            _Sess.worker.stop()             # the user pressed Disconnect
            raise KLineError("no answer")

        def close(self):
            self.closed = True
    w, failed, connected = _run_worker(monkeypatch, _Sess)
    assert connected and failed == []


def test_a_real_reconnect_failure_is_still_reported(monkeypatch):
    class _Sess:
        identity, baud = "ECU", 10400

        def __init__(self, link, ecu, log):
            self.k = link

        def connect(self, *a, **k):
            pass

        def read_group(self, g):
            raise KLineError("line dropped")

        def reconnect(self, is_stop=None):
            raise KLineError("no answer")

        def close(self):
            pass
    ticks = iter(range(0, 10000, 3))
    monkeypatch.setattr(GL.time, "time", lambda: next(ticks))
    w, failed, _c = _run_worker(monkeypatch, _Sess)
    assert failed == ["Could not reconnect within 10 seconds."]


def test_disconnect_while_connecting_raises_no_error_box(monkeypatch):
    class _Sess:
        def __init__(self, link, ecu, log):
            self.k = link

        def connect(self, *a, is_stop=None, **k):
            _Sess.worker.stop()
            raise KLineError("stop requested")

        def close(self):
            pass
    _w, failed, connected = _run_worker(monkeypatch, _Sess)
    assert failed == [] and connected == []


def test_live_reads_use_the_short_timeout(monkeypatch):
    class _Sess:
        identity, baud = "ECU", 10400

        def __init__(self, link, ecu, log):
            self.k = link

        def connect(self, *a, **k):
            pass

        def read_group(self, g):
            _Sess.seen = self.k.read_timeout
            _Sess.worker.stop()
            return []

        def close(self):
            pass
    _run_worker(monkeypatch, _Sess)
    assert _Sess.seen == GL.LIVE_READ_TIMEOUT


# ------------------------------------------------------------- the viewer
def _write_log(path, rows):
    log = L.DataLog(channels=[L.Channel(1, 0, "Engine Speed", "rpm"),
                              L.Channel(3, 1, "Boost", "mbar")])
    t0 = datetime(2026, 10, 14, 9, 0, 0)
    for i in range(rows):
        log.append(t0 + timedelta(milliseconds=100 * i),
                   ["%d rpm" % (800 + i), "%d mbar" % (1000 + i)])
    L.write_kwhack(path, log)


def test_the_view_model_serves_every_row(app, tmp_path):
    path = str(tmp_path / "long.csv")
    _write_log(path, 6000)                  # over the old 4000-row cap
    got = LV.parse(path)
    m = LV.LogTableModel()
    m.set_log(got.log, got.secs)
    assert m.rowCount() == 6000 and m.columnCount() == 3
    assert m.headerData(0, QtCore.Qt.Horizontal) == "Time (s)"
    assert m.data(m.index(5999, 0)) == "599.900"
    assert m.data(m.index(5999, 2)) == "6999 mbar"
    assert not (m.flags(m.index(0, 1)) & QtCore.Qt.ItemIsEditable)


def test_the_viewer_reads_off_the_gui_thread(dlg, tmp_path, monkeypatch):
    path = str(tmp_path / "drive.csv")
    _write_log(path, 50)
    threads = []
    real = LV.parse

    def spy(p):
        threads.append(threading.current_thread())
        return real(p)
    monkeypatch.setattr(LV, "parse", spy)
    dlg.view_file(path)
    assert _pump(lambda: dlg._viewed is not None)
    assert threads and threads[0] is not threading.main_thread()
    assert dlg.view_model.rowCount() == 50
    # chart cursor <-> table selection still follow each other
    dlg._cursor_row(10)
    assert dlg.view_chart.cursor == 10
    dlg.view_table.selectRow(20)
    assert dlg.view_chart.cursor == 20


def test_a_newer_load_supersedes_an_older_one(app, tmp_path):
    a, b = str(tmp_path / "a.csv"), str(tmp_path / "b.csv")
    _write_log(a, 5)
    _write_log(b, 9)
    ld = LV.LogLoader()
    got = []
    ld.loaded.connect(lambda r: got.append(r.log.rows))
    ld.load(a)
    ld.load(b)
    assert _pump(lambda: got)
    time.sleep(0.1)
    QtWidgets.QApplication.processEvents()
    assert got == [9]


def test_chart_paints_a_long_log(app):
    c = GL.StripChart()
    c.resize(400, 200)
    n = 50000
    c.set_data([i * 0.01 for i in range(n)], [[float(i % 100) for i in range(n)]], ["x"])
    c.window = 30.0
    from PySide6 import QtGui
    img = QtGui.QImage(400, 200, QtGui.QImage.Format_ARGB32)
    c.render(img)                           # the bisect path; must not raise


# ----------------------------------------------- OVT Logger: keep off-thread
def test_the_standalone_keep_runs_off_the_gui_thread_one_at_a_time(app, tmp_path, monkeypatch):
    from ovtsuite import gui_ovt_file as OF
    from ovtsuite.datalog import standalone as SA
    host = SA.StandaloneLogHost()
    seen, active, peak = [], [0], [0]

    def quiet(path, change):
        seen.append(threading.current_thread())
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return True, ""
    monkeypatch.setattr(OF, "update_project_quiet", quiet)
    ref = type("R", (), {"doc": type("D", (), {"path": str(tmp_path / "car.ovt")})(),
                         "sid": "s1"})()
    ws = type("W", (), {"project_ref": ref})()
    done = []
    for name in ("engine.csv", "gearbox.csv"):
        p = tmp_path / name
        p.write_text("t\n0\n")
        assert host.keep_log_async(ws, str(p), None, done.append) is None
    assert _pump(lambda: len(done) == 2)
    assert done == [True, True]
    assert all(t is not threading.main_thread() for t in seen)
    assert peak[0] == 1, "two rewrites of the same .ovt must not overlap"

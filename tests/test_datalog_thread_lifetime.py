# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The logger must never destroy a QThread that is still running.

Crash, 2026-10-02: disconnecting the CAN logger closed the whole app. The
dialog dropped its only reference to the worker when the worker said "ended"
-- a signal sent from inside the thread, which then still had to unwind --
and PySide deleted the running QThread, a fatal abort in Qt.
"""
import gc
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.datalog import gui_log as GL  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(until, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not until():
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    return until()


class _SlowExit(QtCore.QThread):
    said_ended = QtCore.Signal()

    def run(self):
        self.said_ended.emit()
        time.sleep(0.3)             # still running well after "ended"


def test_a_retired_thread_outlives_its_last_reference(app):
    t = _SlowExit()
    got = []
    t.said_ended.connect(lambda: got.append(1))
    t.start()
    assert _pump(lambda: got)
    assert t.isRunning()
    GL.retire_thread(t)
    del t
    gc.collect()                    # used to abort the process right here
    assert _pump(lambda: not GL._reaper._held)


def test_the_worker_says_ended_last_even_when_the_session_raises(app, monkeypatch):
    order = []

    def boom(self):
        order.append("session")
        raise RuntimeError("link dropped")
    monkeypatch.setattr(GL.LogWorker, "_run_session", boom)
    w = GL.LogWorker("x", "EDC16", "Low", False, transport="can")
    w.ended.connect(lambda: order.append("ended"))
    w.start()
    assert w.wait(5000)
    assert _pump(lambda: "ended" in order)
    assert order == ["session", "ended"]


def test_the_connection_retires_its_worker_on_ended(app, monkeypatch):
    """The retire moved from the dialog to ConnPane when dual logging split
    the per-connection state out; the invariant did not. A QThread destroyed
    while it is still running takes the process down, so whoever owns the
    worker must retire it, and must do so before touching any widget -- if a
    half-built pane raises on the way, the thread is already safe."""
    held = []
    monkeypatch.setattr(GL, "retire_thread", lambda t: held.append(t))
    pane = GL.ConnPane.__new__(GL.ConnPane)
    sentinel = object()
    pane._worker = sentinel
    try:
        GL.ConnPane._on_ended(pane)
    except AttributeError:
        pass                        # the half-built pane has no widgets; the retire comes first
    assert held == [sentinel]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""keepawake.prevent_sleep: the hold is taken for the block and always
released -- on every platform, including when the block raises."""
import sys

import pytest

from ovtsuite.flash import keepawake as K


class _FakeProc:
    live = []

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.terminated = False
        _FakeProc.live.append(self)

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize("plat,exe", [("darwin", "caffeinate"), ("linux", "systemd-inhibit")])
def test_helper_process_held_for_the_block_and_released_on_error(monkeypatch, plat, exe):
    monkeypatch.setattr(K.sys, "platform", plat)
    monkeypatch.setattr(K.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(K.subprocess, "Popen", _FakeProc)
    _FakeProc.live.clear()
    with pytest.raises(RuntimeError):
        with K.prevent_sleep("logging"):
            (p,) = _FakeProc.live
            assert p.cmd[0].endswith(exe) and not p.terminated
            raise RuntimeError("link dropped")
    assert p.terminated
    if plat == "darwin":            # dies with us even if we crash
        assert "-w" in p.cmd
    else:
        assert "--what=idle:sleep" in p.cmd and "--why=logging" in p.cmd


def test_no_helper_available_is_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(K.sys, "platform", "linux")
    monkeypatch.setattr(K.shutil, "which", lambda n: None)
    with K.prevent_sleep():
        pass


class _FakeSession:
    """Stands in for realtime.realtime_session: records entry and exit."""

    def __init__(self):
        self.events = []

    def __call__(self, why="", **kw):
        import contextlib
        events = self.events

        @contextlib.contextmanager
        def cm():
            events.append(("enter", why, kw))
            try:
                yield
            finally:
                events.append(("exit", why, kw))
        return cm()


def _qt():
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_logger_worker_really_runs_inside_a_realtime_session(monkeypatch):
    _qt()
    from ovtsuite.datalog import gui_log
    fake = _FakeSession()
    monkeypatch.setattr(gui_log, "_realtime_session", fake)
    w = gui_log.LogWorker("COM-none", "EDC16", "low", False)
    ran = []
    monkeypatch.setattr(w, "_run_session", lambda: ran.append(list(fake.events)))
    w.run()                                    # synchronously, on this thread
    assert ran and ran[0][0][0] == "enter"     # the body ran INSIDE the session
    assert [e[0] for e in fake.events] == ["enter", "exit"]
    assert fake.events[0][2].get("keep_awake") is True


def test_logger_worker_releases_the_session_when_the_body_raises(monkeypatch):
    _qt()
    from ovtsuite.datalog import gui_log
    fake = _FakeSession()
    monkeypatch.setattr(gui_log, "_realtime_session", fake)
    w = gui_log.LogWorker("COM-none", "EDC16", "low", False)

    def boom():
        raise RuntimeError("link dropped")
    monkeypatch.setattr(w, "_run_session", boom)
    with pytest.raises(RuntimeError):
        w.run()
    assert [e[0] for e in fake.events] == ["enter", "exit"]


def test_flasher_worker_really_runs_inside_a_realtime_session(monkeypatch):
    _qt()
    from ovtsuite import realtime
    from ovtsuite.flash import gui_flash
    fake = _FakeSession()
    monkeypatch.setattr(realtime, "realtime_session", fake)   # _session_guard looks it up here
    w = gui_flash._Worker("read", "EDC15", "COM-none", "low")
    inside = []

    def trace():                               # first thing run() does inside the guard
        inside.append([e[0] for e in fake.events])
        raise RuntimeError("stop here")
    monkeypatch.setattr(w, "_begin_trace", trace)
    failures = []
    w.failed.connect(failures.append)
    w.run()
    assert inside == [["enter"]]               # the work started with the session open
    assert [e[0] for e in fake.events] == ["enter", "exit"]
    assert "flash" in fake.events[0][1].lower() and "read" in fake.events[0][1]

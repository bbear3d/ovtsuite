# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ovtsuite.realtime: refcounting, thread begin/end and failure tolerance."""
import sys
import threading

import pytest

from ovtsuite import realtime


class Rec(realtime.Backend):
    def __init__(self, fail=False):
        self.calls, self.fail, self.opts = [], fail, []

    def _c(self, n):
        self.calls.append(n)
        if self.fail:
            raise RuntimeError("boom")

    def process_begin(self, why, display, system=True):
        self._c("pb")
        self.opts.append((display, system))
        return "tok"

    def process_end(self, token):
        self._c("pe")

    def thread_begin(self):
        self._c("tb")
        return 1

    def thread_end(self, state):
        self._c("te")


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(realtime, "_backend", r)
    monkeypatch.setattr(realtime, "_enabled", True)
    monkeypatch.setattr(realtime, "_sessions", 0)
    monkeypatch.setattr(realtime, "_token", None)
    monkeypatch.setattr(realtime, "_awake", 0)
    monkeypatch.setattr(realtime, "_token_awake", False)
    monkeypatch.setattr("ovtsuite.flash.keepawake._hold_command", lambda why: None)
    return r


def test_single_session(rec):
    with realtime.realtime_session(keep_awake=False):
        assert realtime.describe()["sessions"] == 1 and realtime.describe()["active"]
    assert rec.calls == ["pb", "tb", "te", "pe"]
    assert realtime.describe()["sessions"] == 0


def test_nested_sessions_share_one_activity(rec):
    with realtime.realtime_session(keep_awake=False):
        with realtime.realtime_session(keep_awake=False):
            assert realtime.describe()["sessions"] == 2
        assert "pe" not in rec.calls
    assert rec.calls.count("pb") == 1 and rec.calls.count("pe") == 1
    assert rec.calls.count("tb") == 1 and rec.calls.count("te") == 1


def test_overlapping_threads(rec):
    inside, go = threading.Event(), threading.Event()

    def other():
        with realtime.realtime_session(keep_awake=False):
            inside.set()
            go.wait(5)
    t = threading.Thread(target=other)
    t.start()
    inside.wait(5)
    with realtime.realtime_session(keep_awake=False):
        pass
    assert rec.calls.count("pe") == 0
    go.set()
    t.join()
    assert rec.calls.count("pb") == 1 and rec.calls.count("pe") == 1


def test_not_time_critical(rec):
    with realtime.realtime_session(keep_awake=False, time_critical=False):
        pass
    assert rec.calls == []


def test_exception_propagates_and_releases(rec):
    with pytest.raises(ValueError):
        with realtime.realtime_session(keep_awake=False):
            raise ValueError("x")
    assert realtime.describe()["sessions"] == 0 and "pe" in rec.calls


def test_backend_failures_never_raise(monkeypatch, rec):
    rec.fail = True
    with realtime.realtime_session(keep_awake=False):
        realtime.begin_thread()
        realtime.end_thread()
    assert realtime.describe()["sessions"] == 0


def test_begin_end_thread_nesting(rec):
    realtime.begin_thread()
    realtime.begin_thread()
    realtime.end_thread()
    assert "te" not in rec.calls
    realtime.end_thread()
    realtime.end_thread()                          # unbalanced: ignored
    assert rec.calls == ["tb", "te"]


def test_keep_awake_composes(monkeypatch, rec):
    seen = []
    import contextlib

    @contextlib.contextmanager
    def fake(why):
        seen.append(why)
        yield
    monkeypatch.setattr("ovtsuite.flash.keepawake.prevent_sleep", fake)
    with realtime.realtime_session("because"):
        pass
    assert seen == ["because"]


def test_real_backend_smoke():
    """The real platform calls do not raise (no mock)."""
    real = realtime._make_backend()
    realtime_backend = realtime._backend
    try:
        realtime._backend = real
        with realtime.realtime_session("OVT-Suite test", keep_awake=False):
            if sys.platform == "darwin":
                assert realtime.describe()["active"]
            realtime.begin_thread()
            realtime.end_thread()
        assert not realtime.describe()["active"]
    finally:
        realtime._backend = realtime_backend


def test_timing_only_session_does_not_hold_sleep_off(rec):
    with realtime.realtime_session(keep_awake=False):
        pass
    assert rec.opts == [(False, False)]


def test_stronger_session_rebuilds_the_activity_and_weaker_drops_back(rec):
    with realtime.realtime_session(keep_awake=False):
        assert rec.opts == [(False, False)]
        with realtime.realtime_session(keep_awake=True):
            assert rec.opts == [(False, False), (True, True)]
            assert rec.calls.count("pe") == 1           # the weak one was ended
        # the awake session left: back to a timing-only activity
        assert rec.opts[-1] == (False, False) and len(rec.opts) == 3
    assert rec.calls.count("pb") == rec.calls.count("pe")
    assert realtime.describe()["sessions"] == 0 and not realtime.describe()["active"]


def test_second_awake_session_does_not_rebuild(rec):
    with realtime.realtime_session():
        with realtime.realtime_session():
            pass
        assert len(rec.opts) == 1
    assert rec.opts == [(True, True)]


def test_mac_activity_options_follow_the_session(monkeypatch):
    if sys.platform != "darwin":
        pytest.skip("macOS only")
    assert not (realtime.ACTIVITY_USER_INITIATED_ALLOWING_SLEEP
                & realtime.ACTIVITY_IDLE_SYSTEM_SLEEP_DISABLED)


def test_linux_rr_is_reset_on_fork(monkeypatch):
    import os
    calls = []
    monkeypatch.setattr(os, "sched_setscheduler",
                        lambda pid, pol, par: calls.append((pid, pol)), raising=False)
    monkeypatch.setattr(os, "sched_param", lambda p: p, raising=False)
    monkeypatch.setattr(os, "SCHED_RR", 2, raising=False)
    monkeypatch.setattr(os, "SCHED_RESET_ON_FORK", 0x40000000, raising=False)
    st = realtime._LinuxBackend().thread_begin()
    assert st == ("rr", None)
    assert calls[0] == (0, 2 | 0x40000000)             # this thread only, children reset


def test_windows_backend_reverts_ecoqos_on_last_end():
    """process_end puts the process back under the system's control."""
    calls = []

    class K:
        def __getattr__(self, n):
            return lambda *a, **k: calls.append(n)
    b = realtime._WinBackend.__new__(realtime._WinBackend)
    import ctypes
    b.c = ctypes
    b.winmm = K()
    b.k32 = K()
    seen = []
    b._power_throttling = lambda control, state: seen.append((control, state))
    b.process_begin("x", True)
    b.process_end(True)
    assert seen == [(1, 0), (0, 0)] and calls.count("timeBeginPeriod") == 1 \
        and calls.count("timeEndPeriod") == 1

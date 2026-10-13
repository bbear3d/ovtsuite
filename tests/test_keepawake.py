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


def test_logger_worker_takes_the_hold_on_its_own_thread():
    import inspect
    pytest.importorskip("PySide6")
    from ovtsuite.datalog import gui_log
    src = inspect.getsource(gui_log.LogWorker.run)
    assert "prevent_sleep" in src

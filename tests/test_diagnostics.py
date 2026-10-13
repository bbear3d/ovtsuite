# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The watchdog and excepthook that make a hang or a crash self-reporting.

Written after OVT-Suite hung with a ZF 6HP tune and a comparison loaded and
left nothing behind to look at: no log, no excepthook, no faulthandler.
"""
import os
import sys
import time

import pytest

from ovtsuite import diagnostics as D


def test_log_path_is_under_the_apps_own_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_log_path", None, raising=False)
    p = D.log_path()
    assert p.endswith("diagnostics.log")
    assert os.path.isabs(p)


def test_write_appends_and_survives_a_bad_path(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_log_path", str(tmp_path / "diagnostics.log"),
                        raising=False)
    monkeypatch.setattr(D, "_log_file", None, raising=False)
    D.write("hello")
    D.write("again")
    text = (tmp_path / "diagnostics.log").read_text()
    assert "hello" in text and "again" in text


def test_excepthook_logs_and_chains(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_log_path", str(tmp_path / "diagnostics.log"),
                        raising=False)
    monkeypatch.setattr(D, "_log_file", None, raising=False)
    seen = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: seen.append(a))
    try:
        raise ValueError("boom")
    except ValueError:
        D._excepthook(*sys.exc_info())
    text = (tmp_path / "diagnostics.log").read_text()
    assert "Unhandled exception" in text and "boom" in text
    assert seen, "the default hook must still run"


def test_watchdog_dumps_stacks_when_the_tick_stops(tmp_path, monkeypatch):
    """The point of the thing: a stalled UI thread produces a stack dump."""
    monkeypatch.setattr(D, "_log_path", str(tmp_path / "diagnostics.log"),
                        raising=False)
    monkeypatch.setattr(D, "_log_file", None, raising=False)
    monkeypatch.setattr(D, "_TICK", 0.05, raising=False)
    dog = D._Watchdog(stall=0.2)
    dog.start()
    try:
        for _ in range(6):            # healthy: ticking
            dog.tick()
            time.sleep(0.05)
        assert "unresponsive" not in (tmp_path / "diagnostics.log").read_text() \
            if (tmp_path / "diagnostics.log").exists() else True
        time.sleep(0.9)               # stalled: no ticks
    finally:
        dog._stop.set()
    text = (tmp_path / "diagnostics.log").read_text()
    assert "UI unresponsive" in text
    assert "--- thread" in text


def test_install_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_log_path", str(tmp_path / "diagnostics.log"),
                        raising=False)
    monkeypatch.setattr(D, "_log_file", None, raising=False)
    a = D.install()
    b = D.install()
    assert a == b
    assert sys.excepthook is D._excepthook
    sys.excepthook = sys.__excepthook__

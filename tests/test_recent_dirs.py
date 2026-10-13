# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where file pickers open.

Bear: "the file picker only ever goes to an irrelevant folder". There was one
remembered folder for every picker, and starting a data log, saving a CAN log
or a capture, or exporting an XDF/MPC/EEPROM all moved it -- so File > Open
landed in the logs folder. Each kind of file now remembers its own.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite import recent  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A private config directory, and folders to pick files from."""
    monkeypatch.setattr(recent, "_config_dir", lambda: str(tmp_path / "cfg"))
    monkeypatch.setattr(recent, "_legacy_dirs", lambda: [])
    dirs = {}
    for name in ("bins", "logs", "exports", "other"):
        d = tmp_path / name
        d.mkdir()
        (d / "f.bin").write_bytes(b"x")
        dirs[name] = str(d)
    return dirs


def test_opening_a_binary_is_remembered(cfg):
    recent.set_last_dir(os.path.join(cfg["bins"], "f.bin"))
    assert recent.get_last_dir() == cfg["bins"]


def test_a_log_or_an_export_does_not_move_the_binary_folder(cfg):
    recent.set_last_dir(os.path.join(cfg["bins"], "f.bin"))
    recent.set_last_dir(os.path.join(cfg["logs"], "run.csv"), "log")
    recent.set_last_dir(os.path.join(cfg["exports"], "x.xdf"), "export")
    assert recent.get_last_dir() == cfg["bins"]
    assert recent.get_last_dir("log") == cfg["logs"]
    assert recent.get_last_dir("export") == cfg["exports"]


def test_a_kind_not_yet_used_starts_at_the_binary_folder(cfg):
    recent.set_last_dir(os.path.join(cfg["bins"], "f.bin"))
    assert recent.get_last_dir("patch") == cfg["bins"]
    assert recent.get_last_dir("eeprom") == cfg["bins"]


def test_before_any_pick_the_newest_recent_file_decides(cfg):
    recent.add_recent(os.path.join(cfg["other"], "f.bin"))
    recent.add_recent(os.path.join(cfg["bins"], "f.bin"))
    assert recent.get_last_dir() == cfg["bins"]


def test_the_old_single_folder_is_the_last_resort(cfg):
    recent.set_setting("last_dir", cfg["other"])
    assert recent.get_last_dir() == cfg["other"]
    recent.set_last_dir(cfg["bins"])                  # a folder is fine too
    assert recent.get_last_dir() == cfg["bins"]


def test_a_folder_that_is_gone_is_not_offered(cfg, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    recent.set_last_dir(str(gone), "log")
    gone.rmdir()
    recent.set_last_dir(os.path.join(cfg["bins"], "f.bin"))
    assert recent.get_last_dir("log") == cfg["bins"]


def test_an_unknown_kind_is_a_binary(cfg):
    recent.set_last_dir(os.path.join(cfg["bins"], "f.bin"), "nonsense")
    assert recent.get_last_dir() == cfg["bins"]


def test_a_fresh_install_starts_in_home_not_the_working_directory(cfg, monkeypatch, tmp_path):
    """Nothing remembered yet: every kind starts in the home folder. An empty
    answer used to make the data-log / capture panes pre-fill a bare file name,
    written relative to the working directory -- an installed app's read-only
    install folder when started from a menu or shortcut."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))             # expanduser on Windows
    for kind in ("binary", "log", "export", "patch", "eeprom"):
        assert recent.get_last_dir(kind) == str(home), kind


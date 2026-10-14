# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The app's options (ovtsuite.options): defaults, typed values, the toolbar
list, changed shortcuts, reset, export / import, and the change signal."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from PySide6 import QtCore
except ImportError as exc:                              # no Qt
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite import options as opt  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    path = str(tmp_path / "options.ini")
    monkeypatch.setattr(opt, "_store", lambda: QtCore.QSettings(
        path, QtCore.QSettings.Format.IniFormat))
    return path


def test_defaults_and_typed_values():
    assert opt.get("projects/autosave_s") == 60
    assert opt.get("ui/theme") == "system"
    opt.set("projects/autosave_s", "300")
    assert opt.get("projects/autosave_s") == 300
    opt.set("projects/autosave_s", 99999)                # clamped
    assert opt.get("projects/autosave_s") == 3600
    opt.set("ui/theme", "purple")                        # not a choice
    assert opt.get("ui/theme") == "system"
    opt.set("projects/watch_disk", False)
    assert opt.get("projects/watch_disk") is False
    opt.set("projects/watch_disk", True)                 # default: key removed
    assert not opt._store().contains("projects/watch_disk")


def test_every_option_is_well_formed():
    for o in opt.OPTIONS:
        assert opt.get(o.key) == o.default, o.key
        if o.kind == "choice":
            assert o.default in {c for c, _l in o.choices}
        if o.kind == "int":
            assert o.minimum <= o.default <= o.maximum
    assert len({o.key for o in opt.OPTIONS}) == len(opt.OPTIONS)


def test_changes_are_announced_once():
    got = []
    opt.notifier().changed.connect(got.append)
    try:
        opt.set("ui/theme", "dark")
        opt.set("ui/theme", "dark")                      # unchanged: quiet
        opt.set_toolbar(["file/save"])
        opt.set_shortcuts({"file/save": "Ctrl+Shift+W"})
    finally:
        opt.notifier().changed.disconnect(got.append)
    assert got == ["ui/theme", opt.TOOLBAR_KEY, opt.SHORTCUT_PREFIX]


def test_toolbar_and_shortcuts_round_trip_and_reset(tmp_path):
    assert opt.toolbar() == opt.DEFAULT_TOOLBAR
    opt.set_toolbar(["file/save", "|", "edit/undo"])
    opt.set_shortcuts({"file/save": "F2", "edit/undo": ""})
    opt.set("recent/max", 5)
    assert opt.toolbar() == ["file/save", "|", "edit/undo"]
    assert opt.shortcuts() == {"file/save": "F2", "edit/undo": ""}
    out = str(tmp_path / "mine.json")
    opt.export_to(out)
    opt.reset()
    assert opt.toolbar() == opt.DEFAULT_TOOLBAR and opt.shortcuts() == {}
    assert opt.get("recent/max") == 12
    opt.import_from(out)
    assert opt.toolbar() == ["file/save", "|", "edit/undo"]
    assert opt.shortcuts() == {"file/save": "F2", "edit/undo": ""}
    assert opt.get("recent/max") == 5
    opt.set_shortcuts({"file/save": None})               # back to its default
    assert opt.shortcuts() == {"edit/undo": ""}


def test_a_saved_toolbar_gets_the_find_panel_button_once():
    s = opt._store()
    s.setValue(opt.TOOLBAR_KEY, '["file/save", "edit/find"]')
    s.sync()
    assert opt.toolbar() == ["file/save", "edit/find-panel"]
    assert opt.toolbar() == ["file/save", "edit/find-panel"]
    opt.set_toolbar(["edit/find", "edit/find-panel"])    # chosen: kept
    assert opt.toolbar() == ["edit/find", "edit/find-panel"]
    assert "edit/find-panel" in opt.DEFAULT_TOOLBAR


def test_a_toolbar_chosen_now_is_not_changed():
    opt.set_toolbar(["edit/find"])
    assert opt.toolbar() == ["edit/find"]

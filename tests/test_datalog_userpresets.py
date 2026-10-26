# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Data-logger presets of your own: saved from the cells ticked, kept per
controller across sessions, renamed, edited and deleted; built-ins stay
read-only; a cell is found again by its signal on another calibration."""
import os

import pytest

from ovtsuite.datalog import userpresets as UP
from ovtsuite.datalog.logfile import Channel


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Saved presets go to a file of the test's own (conftest)."""


class _Prefs:
    """Enough of QSettings for the Qt-free half."""

    def __init__(self):
        self.d = {}

    def value(self, k, default=None):
        return self.d.get(k, default)

    def setValue(self, k, v):                                  # noqa: N802
        self.d[k] = v


class _Namer:
    """Group 1: speed, quantity; group 3: boost; group 140 (unserved): gear.
    ``where`` maps signal -> (group, field), so a test can move one."""
    available = True

    def __init__(self, where=None):
        self.where = where or {"nEng": (1, 0), "qInj": (1, 1), "pBoost": (3, 0),
                               "gear": (140, 0)}

    def _at(self, g, f):
        return next((s for s, k in self.where.items() if k == (g, f)), "")

    def resolve(self, g, f):
        s = self._at(g, f)
        return (("%s (G%d/%d)" % (s, g, f + 1)) if s else "", s, "")

    def channel(self, g, f):
        label, s, unit = self.resolve(g, f)
        return Channel(g, f, label, unit, s)

    def find_signal(self, name):
        return self.where.get(name)

    def slots(self, g):
        return 4

    def cell_fields(self, g):
        return list(range(4))

    def populated_groups(self):
        return sorted({g for g, _f in self.where.values()})

    def group_summary(self, g):
        return ""


# ------------------------------------------------------------ Qt-free
def test_saved_per_controller_and_read_back():
    pr = _Prefs()
    a = UP.from_channels("  Pulls ", [Channel(1, 0, "n", "", "nEng"),
                                      Channel(1, 1, "q", "", "qInj")])
    assert a.name == "Pulls" and [(c.group, c.field) for c in a.cells] == [(1, 0), (1, 1)]
    UP.save(pr, "EDC16", [a])
    UP.save(pr, "ZF6HP", [UP.UserPreset("Shifts", [UP.Cell(2, 0)])])
    assert [p.name for p in UP.load(pr, "EDC16")] == ["Pulls"]
    assert [p.name for p in UP.load(pr, "ZF6HP")] == ["Shifts"]
    assert UP.load(pr, "EDC15") == []
    back = UP.load(pr, "EDC16")[0]
    assert back.cells[0].signal == "nEng" and back.cells[1].title == "q"
    UP.save(pr, "EDC16", [])                       # the other controller's are kept
    assert UP.load(pr, "EDC16") == [] and UP.load(pr, "ZF6HP")


def test_damaged_settings_are_not_fatal():
    pr = _Prefs()
    pr.setValue(UP.KEY, "{not json")
    assert UP.load(pr, "EDC16") == []
    pr.setValue(UP.KEY, '{"EDC16": [{"cells": []}, {"name": "ok", "cells": []}]}')
    assert [p.name for p in UP.load(pr, "EDC16")] == ["ok"]


def test_names_are_unique_ignoring_case():
    ps = [UP.UserPreset("Pulls"), UP.UserPreset("Cruise")]
    assert UP.find(ps, " pulls ").name == "Pulls"
    assert UP.find(ps, "PULLS", but="Pulls") is None        # renaming itself
    assert UP.find(ps, "cruise", but="Pulls").name == "Cruise"


def test_end_of_injection_is_a_flag_not_a_cell():
    from ovtsuite.datalog import derived as D
    p = UP.from_channels("t", [Channel(1, 0), D.eoi_channel()])
    assert p.eoi and len(p.cells) == 1


def test_a_cell_is_found_again_by_its_signal():
    p = UP.from_channels("x", [Channel(1, 0, "n", "", "nEng"), Channel(3, 0, "b", "", "pBoost")])
    same = UP.resolve(p, _Namer())
    assert same.cells == [(1, 0), (3, 0)] and not same.missing and same.moved == 0
    moved = UP.resolve(p, _Namer({"nEng": (1, 0), "pBoost": (5, 2)}))
    assert moved.cells == [(1, 0), (5, 2)] and moved.moved == 1


def test_a_cell_no_served_block_carries_is_missing():
    p = UP.from_channels("x", [Channel(1, 0, "n", "", "nEng"),
                               Channel(3, 0, "Boost (G3/1)", "", "pBoost"),
                               Channel(140, 0, "Gear", "", "gear")])
    r = UP.resolve(p, _Namer({"nEng": (1, 0), "gear": (140, 0)}), avoid=[140])
    assert r.cells == [(1, 0)] and r.missing == ["Boost (G3/1)", "Gear"]


def test_unnamed_cells_are_kept_by_block_and_position():
    class _Blind:
        available = False
    p = UP.UserPreset("x", [UP.Cell(7, 2, "sig", "t")])
    assert UP.resolve(p, _Blind()).cells == [(7, 2)]


# ------------------------------------------------------------ the logger
def _qt():
    from ovtsuite import gui as _g
    if not _g.HAVE_QT:
        pytest.skip("needs a working PySide6 QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets
    return QtCore, QtWidgets


class _Bin:
    def __init__(self, name):
        self.name = name


class _Sess:
    def __init__(self, platform, name):
        self.platform = platform
        self.binfile = _Bin(name)


class _Ws:
    def __init__(self, sess):
        self.primary = sess


@pytest.fixture
def logger(tmp_path, monkeypatch):
    QtCore, QtWidgets = _qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from ovtsuite import recent
    from ovtsuite.datalog import gui_log as GL
    from ovtsuite.datalog import gui_presets as GP
    monkeypatch.setattr(recent, "get_last_dir", lambda *a, **k: str(tmp_path))
    msgs = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, **k: msgs.append(a[2] if len(a) > 2 else "")))
    answers = []                                   # what each question is answered
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(
        lambda *a, **k: answers.pop(0) if answers else QtWidgets.QMessageBox.StandardButton.No))
    names = []                                     # what each dialog is filled in with

    def _exec(dlg):
        if not names:
            return False
        name, retake = names.pop(0)
        dlg.name.setText(name)
        if dlg.retake is not None:
            dlg.retake.setChecked(retake)
        return dlg.ok.isEnabled()
    monkeypatch.setattr(GP.UserPresetDialog, "exec", _exec)
    renames = []
    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", staticmethod(
        lambda *a, **k: (renames.pop(0), True) if renames else ("", False)))

    class _Win(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self._ws = [_Ws(_Sess("edc16", "lq.edc16"))]

        def workspaces(self):
            return list(self._ws)

        @property
        def current(self):
            return self._ws[0]

        @property
        def primary(self):
            return self._ws[0].primary

    win = _Win()
    made = []

    def make():
        d = GL.DataLogDialog(win)
        made.append(d)
        p = d.panes[0]
        p.conn.ecu_box.setCurrentText("EDC16")
        p._namer = _Namer()
        return p

    class _Ctx:
        pass
    c = _Ctx()
    c.app, c.make, c.msgs, c.answers, c.names, c.renames = app, make, msgs, answers, names, renames
    c.Yes = QtWidgets.QMessageBox.StandardButton.Yes
    yield c
    for d in made:
        d.close()


def _tick(p, *cells):
    p._channels = [p._namer.channel(g, f) for g, f in cells]
    p._refresh_sel()


def _choose(p, data):
    i = p.preset_box.findData(data)
    assert i >= 0, data
    p.preset_box.setCurrentIndex(i)
    p._apply_preset()


def _mine(p):
    from ovtsuite.datalog.userpresets import is_user_data
    return [p.preset_box.itemText(i).strip() for i in range(p.preset_box.count())
            if is_user_data(p.preset_box.itemData(i))]


@pytest.mark.slow
def test_save_as_lists_it_under_your_presets_and_it_persists(logger):
    p = logger.make()
    assert not p.preset_save.isEnabled()                 # nothing ticked
    _tick(p, (1, 0), (3, 0))
    assert p.preset_save.isEnabled()
    logger.names.append(("Boost pulls", False))
    p._save_user_preset()
    assert _mine(p) == ["Boost pulls"]
    assert p.preset_box.currentData() == "user:Boost pulls"
    head = p.preset_box.findText("Your presets")
    assert head >= 0 and not (p.preset_box.model().item(head).flags()
                              & p.preset_box.model().item(head).flags().ItemIsEnabled)
    assert p.preset_edit.isEnabled() and p.preset_del.isEnabled()
    again = logger.make()                                # a new session reads it back
    assert _mine(again) == ["Boost pulls"]


@pytest.mark.slow
def test_applying_ticks_its_cells_and_names_the_log(logger):
    p = logger.make()
    _tick(p, (1, 0), (3, 0))
    logger.names.append(("Boost pulls", False))
    p._save_user_preset()
    p._clear_selected()
    p.path_edit.setText(os.path.join("x", "lq_1.csv"))
    _choose(p, "user:Boost pulls")
    assert [c.key for c in p._channels] == [(1, 0), (3, 0)]
    assert p.preset_hint.text().startswith("Your preset: 2 cells, blocks 1, 3.")
    assert p.path_edit.text().endswith("lq_1-boost-pulls.csv")
    _choose(p, "boost")                                  # a built-in swaps the tag
    assert p.path_edit.text().endswith("lq_1-boost.csv")


@pytest.mark.slow
def test_a_cell_no_longer_served_is_named_in_the_hint(logger):
    p = logger.make()
    _tick(p, (1, 0), (3, 0))
    logger.names.append(("Mine", False))
    p._save_user_preset()
    p._namer = _Namer({"nEng": (1, 0)})                  # boost is in no block now
    _choose(p, "user:Mine")
    assert [c.key for c in p._channels] == [(1, 0)]
    assert p.preset_hint.text().startswith("Not in any block this ECU serves: pBoost (G3/1).")


@pytest.mark.slow
def test_rename_rejects_empty_and_taken_names(logger):
    p = logger.make()
    _tick(p, (1, 0))
    logger.names += [("A", False)]
    p._save_user_preset()
    _tick(p, (3, 0))
    logger.names += [("B", False)]
    p._save_user_preset()
    logger.renames += ["   "]
    p._rename_user_preset()
    assert _mine(p) == ["A", "B"] and "needs a name" in logger.msgs[-1]
    logger.renames += ["a"]                              # B -> a: taken by A
    p._rename_user_preset()
    assert _mine(p) == ["A", "B"] and "already have" in logger.msgs[-1]
    logger.renames += ["Boost only"]
    p._rename_user_preset()
    assert _mine(p) == ["A", "Boost only"]
    assert p.preset_box.currentData() == "user:Boost only"
    _choose(p, "user:Boost only")
    assert [c.key for c in p._channels] == [(3, 0)]      # its cells went with it


@pytest.mark.slow
def test_edit_takes_the_cells_selected_now_and_or_a_new_name(logger):
    p = logger.make()
    _tick(p, (1, 0))
    logger.names.append(("Mine", False))
    p._save_user_preset()
    _tick(p, (1, 0), (1, 1), (3, 0))
    logger.names.append(("Mine", False))                 # name only, cells kept
    p._edit_user_preset()
    _choose(p, "user:Mine")
    assert [c.key for c in p._channels] == [(1, 0)]
    _tick(p, (1, 1), (3, 0))
    logger.names.append(("Mine v2", True))               # new name and new cells
    p._edit_user_preset()
    assert _mine(p) == ["Mine v2"]
    p._clear_selected()
    _choose(p, "user:Mine v2")
    assert [c.key for c in p._channels] == [(1, 1), (3, 0)]


@pytest.mark.slow
def test_save_as_an_existing_name_asks_before_replacing(logger):
    p = logger.make()
    _tick(p, (1, 0))
    logger.names.append(("Mine", False))
    p._save_user_preset()
    _tick(p, (3, 0))
    logger.names.append(("mine", False))                 # answered No
    p._save_user_preset()
    _choose(p, "user:Mine")
    assert [c.key for c in p._channels] == [(1, 0)]
    _tick(p, (3, 0))
    logger.names.append(("mine", False))
    logger.answers.append(logger.Yes)
    p._save_user_preset()
    assert _mine(p) == ["mine"]
    _choose(p, "user:mine")
    assert [c.key for c in p._channels] == [(3, 0)]


@pytest.mark.slow
def test_delete_asks_first(logger):
    p = logger.make()
    _tick(p, (1, 0))
    logger.names.append(("Mine", False))
    p._save_user_preset()
    p._delete_user_preset()                              # answered No
    assert _mine(p) == ["Mine"]
    logger.answers.append(logger.Yes)
    p._delete_user_preset()
    assert _mine(p) == [] and p.preset_box.findText("Your presets") < 0
    assert p.preset_box.currentIndex() == 0
    assert _mine(logger.make()) == []


@pytest.mark.slow
def test_built_ins_are_read_only_but_can_be_saved_as_a_copy(logger):
    p = logger.make()
    _choose(p, "boost")
    assert not p.preset_edit.isEnabled() and not p.preset_rename.isEnabled()
    assert not p.preset_del.isEnabled() and "Save as" in p.preset_edit.toolTip()
    _tick(p, (1, 0), (3, 0))
    logger.names.append(("Boost copy", False))
    p._save_user_preset()
    assert _mine(p) == ["Boost copy"]
    assert p.preset_box.findData("boost") > 0            # the built-in is still there


@pytest.mark.slow
def test_presets_are_kept_per_controller(logger):
    p = logger.make()
    _tick(p, (1, 0))
    logger.names.append(("Engine only", False))
    p._save_user_preset()
    p.conn.transport_box.setCurrentIndex(p.conn.transport_box.findData("can"))
    p.conn.ecu_box.setCurrentText("ZF6HP-UDS")           # no built-ins
    assert _mine(p) == []
    assert p.preset_hint.text().startswith("No presets for ZF6HP-UDS")
    p._channels = [p._namer.channel(0x1000, 0)]
    p._refresh_sel()
    logger.names.append(("Gearbox", False))
    p._save_user_preset()
    assert _mine(p) == ["Gearbox"] and p.preset_box.isEnabled()
    p.conn.ecu_box.setCurrentText("EDC16")
    assert _mine(p) == ["Engine only"]

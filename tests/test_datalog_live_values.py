# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A ticked cell shows its live value at once, not when logging starts
(datalog/gui_log.py: LogWorker.set_watch / ``watched``, the pane's
Selected cells table)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6 import QtCore, QtWidgets
except ImportError as exc:
    pytest.skip("PySide6 is not usable here: %s" % exc, allow_module_level=True)

from ovtsuite.datalog import gui_log as GL  # noqa: E402
from ovtsuite.datalog import logfile as L  # noqa: E402


@pytest.fixture(autouse=True)
def _own_settings(isolated_qsettings):
    """Settings go to a file of the test's own (conftest)."""


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Win(QtWidgets.QMainWindow):
    def workspaces(self):
        return []

    current = None
    primary = None


class _Worker:
    def __init__(self):
        self.watch = None

    def set_watch(self, channels):
        self.watch = list(channels)

    def set_browse_group(self, g):
        pass


@pytest.fixture
def pane(app, tmp_path, monkeypatch):
    monkeypatch.setattr(GL, "_prefs", lambda: QtCore.QSettings(
        str(tmp_path / "prefs.ini"), QtCore.QSettings.Format.IniFormat))
    d = GL.DataLogDialog(_Win())
    p = d.panes[0]
    yield p
    p._worker = None
    d.close()


def _ch(group, field):
    return L.Channel(group, field, "G%d/%d" % (group, field + 1), "", "")


def test_a_ticked_cell_is_given_to_the_connection_to_read(pane):
    pane._worker = _Worker()
    pane._channels = [_ch(3, 0), _ch(11, 2)]
    pane._refresh_sel()
    assert [(c.group, c.field) for c in pane._worker.watch] == [(3, 0), (11, 2)]
    pane._remove_selected()
    pane._clear_selected()
    assert pane._worker.watch == []


def test_its_value_appears_in_the_selected_cells_table(pane):
    pane._worker = _Worker()
    pane._channels = [_ch(3, 0), _ch(11, 2)]
    pane._refresh_sel()
    pane._on_watched([((11, 2), "812.5"), ((3, 0), "20.1")])
    assert pane.sel.item(0, 2).text() == "20.1"
    assert pane.sel.item(1, 2).text() == "812.5"
    pane._on_watched([((99, 9), "x")])                  # a cell no longer ticked
    pane._logging = True                                # while logging, the rows fill it
    pane._on_watched([((3, 0), "never")])
    assert pane.sel.item(0, 2).text() == "20.1"


def test_a_worker_reads_what_is_ticked_but_not_memory_cells(app):
    w = GL.LogWorker("p", "EDC16", "Medium", False)
    mem = L.Channel(0, 0, "ast", "", "", addr=0x1234)
    w.set_watch([_ch(3, 0), mem])
    assert [c.group for c in w._watch] == [3]
    assert w._watch[0].key == (3, 0)


def test_the_values_clear_when_the_connection_ends(pane):
    pane._channels = [_ch(3, 0)]
    pane._refresh_sel()
    pane._on_watched([((3, 0), "20.1")])
    pane._clear_sel_values()
    assert pane.sel.item(0, 2).text() == ""


class _Cell:
    def __init__(self, text, value):
        self.text, self.value = text, value


class _Sess:
    def __init__(self):
        self.asked = []

    def read_group(self, g):
        self.asked.append(("one", g))
        return [_Cell("b%d" % g, g)]

    def read_groups(self, groups, is_stop=None):
        self.asked.append(("many", list(groups)))
        return {g: [_Cell("g%d/0" % g, g), _Cell("g%d/1" % g, g + 0.5)] for g in groups}


def test_the_idle_read_takes_the_ticked_groups_with_the_browsed_one(app):
    w = GL.LogWorker("p", "EDC16", "Medium", False)
    got, seen = [], []
    w.browsed.connect(lambda g, cells: got.append((g, cells)))
    w.watched.connect(lambda pairs: seen.append(pairs))
    s = _Sess()
    w._browse_read(s, 5)                                # nothing ticked: as before
    assert s.asked == [("one", 5)] and not seen
    w.set_watch([_ch(3, 1), _ch(5, 0)])
    w._browse_read(s, 5)
    assert s.asked[-1] == ("many", [3, 5])              # one read of both groups
    assert got[-1][0] == 5 and [t for _f, t, _v in got[-1][1]] == ["g5/0", "g5/1"]
    assert seen[-1] == [((3, 1), "g3/1"), ((5, 0), "g5/0")]
    w.set_watch([_ch(9, 7)])                            # a cell the group lacks
    w._browse_read(s, 5)
    assert seen[-1] == [((9, 7), "N/A")]

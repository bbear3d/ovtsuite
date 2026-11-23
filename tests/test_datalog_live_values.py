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

    def isRunning(self):
        return False


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


def test_a_worker_monitors_what_is_ticked_memory_cells_included(app):
    w = GL.LogWorker("p", "EDC16", "Medium", False)
    mem = L.Channel(0, 0, "ast", "", "", addr=0x1234)
    g1 = w.set_watch([_ch(3, 0), mem])
    assert [c.group for c in w._watch] == [3, 0]
    assert w._watch[0].key == (3, 0)
    assert w.set_watch([]) == g1 + 1                    # each change is a new generation


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


def test_the_monitor_reads_several_groups_and_memory_in_one_pass(app):
    w = GL.LogWorker("p", "EDC16", "Medium", False)
    mem = L.Channel(0, 0, "ast", "", "", addr=0x1234)
    mon, seen = [], []
    w.monitored.connect(lambda g, t, v: mon.append((g, t, v)))
    w.watched.connect(lambda pairs: seen.append(pairs))
    w._read_mem = lambda sess, c: ("77", 77.0)          # the probe has run
    s = _Sess()
    gen = w.set_watch([_ch(3, 1), _ch(7, 0), mem, _ch(5, 0)])
    w._browse_read(s, 5)
    assert s.asked == [("many", [3, 5, 7])]             # one pass for all groups
    g, texts, values = mon[-1]
    assert g == gen
    assert texts == ["g3/1", "g7/0", "77", "g5/0"]
    assert values == [3.5, 7, 77.0, 5]
    assert [k for k, _t in seen[-1]] == [(3, 1), (7, 0), mem.key, (5, 0)]


def test_a_memory_only_monitor_still_reads_the_browsed_group(app):
    w = GL.LogWorker("p", "EDC16", "Medium", False)
    mem = L.Channel(0, 0, "ast", "", "", addr=0x1234)
    mon = []
    w.monitored.connect(lambda g, t, v: mon.append(t))
    w._read_mem = lambda sess, c: ("N/A", None)
    s = _Sess()
    w.set_watch([mem])
    w._browse_read(s, 5)
    assert s.asked == [("one", 5)] and mon == [["N/A"]]


def test_derived_cells_are_worked_out_from_the_monitored_sources(app):
    from ovtsuite.datalog import derived as D
    w = GL.LogWorker("p", "EDC16", "Medium", False)
    soi = L.Channel(3, 0, "G3/1", "deg", "SOI")
    dur = L.Channel(3, 1, "G3/2", "deg", "DUR")
    mon = []
    w.monitored.connect(lambda g, t, v: mon.append((t, v)))
    w.set_watch([soi, dur, D.eoi_channel()])
    import ovtsuite.datalog.gui_log as G
    seen = []
    orig = G._derived.fill
    G._derived.fill = lambda ch, t, v, plan=None: seen.append(list(ch))
    try:
        w._browse_read(_Sess(), 5)
    finally:
        G._derived.fill = orig
    assert len(seen[0]) == 3 and seen[0][2].is_derived  # sees the sources and the calc


def test_a_pass_read_for_an_old_cell_set_is_not_emitted_while_logging(app):
    w = GL.LogWorker("p", "EDC16", "Medium", False)
    mon = []
    w.monitored.connect(lambda g, t, v: mon.append(g))
    w.set_watch([_ch(3, 0)])
    w._logging = True
    w._browse_read(_Sess(), 5)
    assert mon == []


def test_the_chart_runs_before_start_and_start_keeps_it(pane):
    class W(_Worker):
        n = 0

        def set_watch(self, channels):
            super().set_watch(channels)
            W.n += 1
            return W.n

        def start_logging(self, channels, path):
            return 1

        def stop_logging(self):
            pass
    pane._worker = W()
    pane._channels = [_ch(3, 0), _ch(11, 2)]
    pane._refresh_sel()
    gen = pane._mon_gen
    pane._on_monitored(gen, ["1", "2"], [1.0, 2.0])
    pane._on_monitored(gen, ["1", "3"], [1.0, 3.0])
    assert len(pane.chart.times) == 2 and pane.chart.series[1] == [2.0, 3.0]
    pane._on_monitored(gen - 1, ["x", "x"], [9.0, 9.0])  # stale
    pane._on_monitored(gen, ["x"], [9.0])                # wrong width
    assert len(pane.chart.times) == 2
    pane._logging = True                                 # recording from the monitor
    pane._rec_gen = 1
    pane._on_monitored(gen, ["1", "9"], [1.0, 9.0])      # the rows being logged fill it
    assert len(pane.chart.times) == 2
    pane._on_logged(1, __import__("datetime").datetime.now(), ["5", "6"], [5.0, 6.0])
    assert len(pane.chart.times) == 3                    # same chart, not reset
    pane._logging = False
    pane._push_watch()                                   # Stop: back to monitoring
    assert len(pane.chart.times) == 3
    pane._channels = [_ch(3, 0)]                         # the cell set changed
    pane._refresh_sel()
    assert pane.chart.times == [] and len(pane.chart.series) == 1


def test_many_groups_are_said_in_the_rate(pane):
    pane._channels = [_ch(g, 0) for g in (1, 2, 3, 4, 5)]
    pane._refresh_sel()
    pane._show_rate(2.0)
    assert "5 groups" in pane.rate.text()
    assert "slow" in pane.rate.toolTip()


def test_a_new_connection_with_the_same_cells_starts_a_clean_chart(pane):
    pane._worker = _Worker()
    pane._channels = [_ch(3, 0)]
    pane._refresh_sel()
    gen = pane._mon_gen
    pane._on_monitored(gen, ["1"], [1.0])
    pane._on_monitored(gen, ["2"], [2.0])
    assert len(pane.chart.times) == 2
    pane._on_ended()
    assert pane.chart.times == [] and pane._chart_sig is None
    pane._worker = _Worker()
    pane._on_connected("ECU", 10400)                     # same cells, new session
    assert pane.chart.times == []


def test_the_logged_chart_uses_the_rows_own_stamp(pane):
    import datetime as dt
    pane._channels = [_ch(3, 0)]
    pane._refresh_sel()
    pane._logging = True
    pane._rec_gen = 1
    pane._rec_base = None
    t = dt.datetime(2026, 1, 1, 12, 0, 0)
    pane._on_logged(1, t, ["1"], [1.0])
    pane._on_logged(1, t + dt.timedelta(seconds=2), ["2"], [2.0])   # GUI got both at once
    assert abs((pane.chart.times[1] - pane.chart.times[0]) - 2.0) < 1e-6


def test_the_group_count_includes_the_browsed_group_and_clears_on_disconnect(pane):
    pane._worker = _Worker()
    pane._channels = [_ch(3, 0), _ch(4, 0)]
    pane._refresh_sel()
    pane.group_spin.setValue(9)
    assert pane._monitor_groups() == 3
    pane._show_rate(2.0)
    assert "3 groups" in pane.rate.text()
    pane._on_ended()
    assert pane.rate.toolTip() == "" and pane._mon_groups_note == 0


def test_the_memory_probe_survives_any_error_and_is_retried(app):
    class Boom(Exception):
        pass                                             # like J2534Error: a plain Exception

    w = GL.LogWorker("p", "EDC16", "Medium", False)
    mem = L.Channel(0, 0, "ast", "", "", addr=0x1234)
    w.set_watch([mem])
    calls, failed, st = [], [], []
    w.mem_probed.connect(lambda ok, why: failed.append(ok))
    w.status.connect(st.append)

    def once(sess):
        calls.append(1)
        raise Boom("link down")
    w._probe_memory_once = once
    w._maybe_probe_memory(object())                      # must not raise
    assert not w._mem_probed_once and st and failed == []
    w._maybe_probe_memory(object())
    w._maybe_probe_memory(object())                      # third failure gives up, said once
    assert failed == [False] and w._mem_probed_once and len(calls) == 3
    w2 = GL.LogWorker("p", "EDC16", "Medium", False)
    w2.set_watch([mem])
    w2._probe_memory_once = lambda sess: None
    w2._maybe_probe_memory(object())
    assert w2._mem_probed_once

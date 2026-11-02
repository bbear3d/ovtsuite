# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag's Live data view (offscreen): the group list and its filter, the
tile grid updated in place, the graph, the "also live" strip, the empty
states, CSV logging, both palettes.
``QT_QPA_PLATFORM=offscreen python -m pytest tests/test_gui_diag_live.py -q --slow``"""
import csv
import os

import pytest

pytestmark = pytest.mark.slow
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore, QtGui, QtTest  # noqa: E402

Qt = QtCore.Qt


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def LV(app, monkeypatch):
    from ovtsuite.diag import gui_live_view as mod
    clock = [100.0]
    monkeypatch.setattr(mod, "_now", lambda: clock[0])
    mod.clock = clock                       # tests move time with LV.clock[0] += ...
    yield mod
    del mod.clock


def _groups(LV):
    G = LV.GroupEntry
    return [
        G(1, "Engine basics", ["Engine speed", "Injection qty", "Coolant", "Pedal"]),
        G(11, "Boost control", ["Boost spec", "Boost actual", "N75 duty", None]),
        G(3, "Air mass / EGR", ["MAF spec", "MAF actual", "EGR duty", "Air temp"]),
        G(4, "Injection timing", ["Start of injection spec", "actual", "Fuel temp"]),
        G(133, "Not served by this ECU", [], served=False, note="Known from the firmware"),
    ]


def _readings(LV, n, base=100.0, named=True):
    return [LV.Reading("Cell %d" % i if named else "", "%.1f" % (base + i), "u", base + i, 0.5)
            for i in range(n)]


@pytest.fixture
def view(app, LV):
    v = LV.LiveDataView()
    v.resize(1100, 700)
    v.move(-4000, -4000)
    v.show()
    v.set_groups(_groups(LV), checked=[1, 11])
    app.processEvents()
    yield v
    v.stop_log()
    v.close()
    v.deleteLater()


def _titles(v):
    return [v.row(g).entry.title for g in v.visible_groups()]


# ------------------------------------------------------------------ the list
def test_filter_matches_titles_and_cell_names(view):
    assert view.visible_groups() == [1, 11, 3, 4, 133]
    view._filter.setText("BOOST")                       # a title, case-insensitive
    assert view.visible_groups() == [11]
    view._filter.setText("egr")                          # a title and a cell name
    assert view.visible_groups() == [3]
    view._filter.setText("fuel temp")                    # a cell name only
    assert view.visible_groups() == [4]
    view._filter.setText("zzz")
    assert view.visible_groups() == []
    assert "zzz" in view._list_empty.text() and not view._list_empty.isHidden()
    view._filter.setText("")
    assert view.visible_groups() == [1, 11, 3, 4, 133]
    assert view._list_empty.isHidden()


def test_set_groups_pre_ticks_without_a_signal(view, LV):
    got = []
    view.selectionChanged.connect(got.append)
    view.set_groups(_groups(LV), checked=[3, 1, 999, 133])   # 999 unknown, 133 not served
    assert view.checked_groups() == [3, 1]
    assert got == []
    assert view.row(3).isChecked() and view.row(1).isChecked() and not view.row(11).isChecked()


def test_ticking_emits_selection_in_the_order_checked(view):
    got = []
    view.selectionChanged.connect(got.append)
    view.row(4).checkbox().setChecked(True)
    view.row(1).checkbox().setChecked(False)
    view.row(3).checkbox().setChecked(True)
    assert got == [[1, 11, 4], [11, 4], [11, 4, 3]]
    assert view.checked_groups() == [11, 4, 3]


def test_clicking_a_row_toggles_it(view, app):
    got = []
    view.selectionChanged.connect(got.append)
    row = view.row(3)
    QtTest.QTest.mouseClick(row, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                            QtCore.QPoint(row.width() - 30, row.height() // 2))
    assert got and got[-1] == [1, 11, 3]


def test_not_served_row_is_disabled_with_its_note(view):
    row = view.row(133)
    assert not row.isEnabled() and not row.checkbox().isEnabled()
    assert "Known from the firmware" in row._sub
    got = []
    view.selectionChanged.connect(got.append)
    QtTest.QTest.mouseClick(row, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                            QtCore.QPoint(row.width() - 30, row.height() // 2))
    assert got == [] and 133 not in view.checked_groups()


def test_source_note_and_manual_group(view):
    assert view._note.isHidden()
    view.set_source_note("Named from the loaded calibration")
    assert not view._note.isHidden()
    assert view._note.text() == "NAMED FROM THE LOADED CALIBRATION"
    got = []
    view.manualGroupRequested.connect(got.append)
    view._spin.setValue(77)
    view._add.click()
    assert got == [77]
    view._spin.setValue(3)                               # already listed: ticked, no request
    view._add.click()
    assert got == [77] and 3 in view.checked_groups()


# --------------------------------------------------------------------- tiles
def test_tiles_follow_the_selection(view, LV):
    assert view.tile_groups() == [(1, i) for i in range(4)] + [(11, i) for i in range(4)]
    assert len(view.tiles()) == 8
    assert view.header_text() == "2 groups · 8 values"
    view.row(3).checkbox().setChecked(True)
    assert len(view.tiles()) == 12 and view.tile_groups()[-1] == (3, 3)
    view.row(1).checkbox().setChecked(False)
    assert [g for g, _ in view.tile_groups()] == [11] * 4 + [3] * 4
    assert view.header_text() == "2 groups · 8 values"


def test_tiles_update_in_place(view, LV, app):
    view.set_readings(1, _readings(LV, 4))
    view.set_readings(11, _readings(LV, 4))
    before = view.tiles()
    for k in range(5):
        view.set_readings(1, _readings(LV, 4, base=200.0 + k))
        view.set_readings(11, _readings(LV, 4, base=300.0 + k))
    after = view.tiles()
    assert len(before) == 8 and all(a is b for a, b in zip(before, after))
    assert after[0]._name == "Cell 0" and after[0]._value == "204.0" and after[0]._unit == "u"
    assert after[4]._value == "304.0"
    # a group that is unchecked loses its tiles, the others are the same objects
    view.row(11).checkbox().setChecked(False)
    assert view.tiles() == before[:4]
    # a reading of a group that is not checked is ignored
    view.set_readings(11, _readings(LV, 4))
    assert len(view.tiles()) == 4


def test_tile_name_and_state(view, LV):
    view.set_readings(1, [LV.Reading("", "5", "mg", 5.0, None, ""),
                          LV.Reading("Injection", "6", "mg", 6.0, 0.4, "warn"),
                          LV.Reading("Coolant", "7", "C", 7.0, 0.4, "bad"),
                          LV.Reading("Pedal", "0", "%", 0.0, 0.0, "")])
    t = view.tiles()
    assert t[0]._name == "Group 001 cell 1"
    assert t[1]._state == "warn" and t[1]._edge == "warn"
    assert t[2]._state == "bad" and t[2]._edge == "bad"
    assert t[3]._edge == ""
    # before any reading a tile carries the entry's cell name, or the unnamed label
    assert view.tiles()[7]._name == "Group 011 cell 4"
    assert view.tiles()[4]._name == "Boost spec"


def test_cell_count_follows_the_readings(view, LV):
    view.set_readings(1, _readings(LV, 6))
    assert len(view.tiles()) == 10 and view.header_text() == "2 groups · 10 values"
    view.set_readings(1, _readings(LV, 2))
    assert len(view.tiles()) == 6


def test_reflow_columns_follow_the_width(view, app):
    view.resize(1100, 700)
    app.processEvents()
    wide = view.column_count()
    view.resize(1700, 700)
    app.processEvents()
    wider = view.column_count()
    view.resize(700, 700)
    app.processEvents()
    narrow = view.column_count()
    assert wider > wide > narrow >= 1
    grid = view._grid
    # every tile inside the grid, none overlapping, at least the minimum width
    rects = [t.geometry() for t in view.tiles()]
    assert all(grid.rect().contains(r) for r in rects)
    assert all(r.width() >= 190 for r in rects)
    assert all(not a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1:])
    assert len({r.left() for r in rects}) == narrow


# ------------------------------------------------------------ header / states
def test_header_rate_and_stopped(view, LV):
    view.set_rate(14.2)
    assert view.header_text() == "2 groups · 14.2 frames/s · 8 values"
    view.set_running(False)
    assert "reads stopped" in view.header_text()
    assert "frames/s" not in view.header_text()
    assert not view._stopped.isHidden()
    assert view._grid.graphicsEffect() is not None       # dimmed, still visible
    assert len(view.tiles()) == 8
    view.set_running(True)
    assert view._stopped.isHidden() and view._grid.graphicsEffect() is None
    assert "14.2 frames/s" in view.header_text()


def test_empty_states(app, LV):
    v = LV.LiveDataView()
    v.resize(1000, 600)
    v.move(-4000, -4000)
    v.show()
    app.processEvents()
    assert v.message_text() == "No groups to read. Type a group number on the left."
    v.set_groups(_groups(LV))
    assert v.message_text() == "Tick one or more groups on the left."
    v.row(1).checkbox().setChecked(True)
    assert v.message_text() == ""
    v.row(1).checkbox().setChecked(False)
    assert v.message_text() == "Tick one or more groups on the left."
    v.set_groups([])
    assert v.message_text().startswith("No groups to read")
    v.close()


# --------------------------------------------------------------------- graph
def test_graph_toggle_and_defaults(view, LV, app):
    for k in range(3):
        LV.clock[0] += 1
        view.set_readings(1, _readings(LV, 4, base=10.0 * k))
        view.set_readings(11, _readings(LV, 4, base=10.0 * k))
    assert not view.graph_active() and view._chart.isHidden() and not view._grid.isHidden()
    view._graph_btn.click()
    assert view.graph_active() and not view._chart.isHidden() and view._grid.isHidden()
    ch = view.chart()
    assert len(ch.keys()) == 8
    assert ch.visible_keys() == ch.keys()[:4]            # the first four by default
    assert view.message_text() == ""
    view.grab()                                          # paints without error
    view._graph_btn.click()
    assert not view.graph_active() and view._chart.isHidden() and not view._grid.isHidden()
    assert len(ch.history((1, 0))) == 3                  # kept while the tiles show


def test_graph_legend_click_hides_and_shows(view, LV, app):
    view.set_readings(1, _readings(LV, 4))
    view.set_graph(True)
    app.processEvents()
    ch = view.chart()
    k0 = ch.keys()[0]
    leg = ch.legend
    pos = QtCore.QPoint(40, 4 + leg.ROW * 0 + leg.ROW // 2)
    assert leg.index_at(pos) == 0
    QtTest.QTest.mouseClick(leg, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    assert k0 not in ch.visible_keys()
    QtTest.QTest.mouseClick(leg, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    assert k0 in ch.visible_keys()


def test_history_is_bounded_and_numeric_only(view, LV):
    n = LV.HISTORY_MAX + 150
    for k in range(n):
        LV.clock[0] += 0.1
        view.set_readings(1, [LV.Reading("A", "x", "", float(k), None, ""),
                              LV.Reading("B", "text", "", None, None, ""),
                              LV.Reading("C", "1", "", 1.0, None, ""),
                              LV.Reading("D", "2", "", 2.0, None, "")])
    ch = view.chart()
    assert len(ch.history((1, 0))) == LV.HISTORY_MAX
    assert ch.history((1, 0))[-1][1] == float(n - 1)
    assert (1, 1) not in ch.keys()                        # a text cell has no series
    assert ch.history((1, 1)) == []


def test_unchecking_a_group_drops_its_history(view, LV):
    view.set_readings(1, _readings(LV, 4))
    view.set_readings(11, _readings(LV, 4))
    assert {k[0] for k in view.chart().keys()} == {1, 11}
    view.row(11).checkbox().setChecked(False)
    assert {k[0] for k in view.chart().keys()} == {1}


# ------------------------------------------------------------- other modules
def test_others_strip_and_signals(view, app):
    assert view._strip.isHidden()
    view.set_others([("02", "Transmission", [("Gear", "N"), ("TCC slip", "0 rpm")])])
    assert not view._strip.isHidden()
    rows = view.other_rows()
    assert len(rows) == 1 and rows[0].addr == "02" and rows[0].link is not None
    activated, split = [], []
    view.otherModuleActivated.connect(activated.append)
    view.splitViewRequested.connect(lambda: split.append(1))
    app.processEvents()
    QtTest.QTest.mouseClick(rows[0], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                            QtCore.QPoint(30, rows[0].height() // 2))
    assert activated == ["02"] and split == []
    rows[0].link.click()
    assert split == [1] and activated == ["02"]          # the link is not the row
    view.set_others([])
    assert view._strip.isHidden() and view.other_rows() == []


# ------------------------------------------------------------------ logging
def _read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.reader(f))


def _round(LV, view, k):
    LV.clock[0] += 0.1
    view.set_readings(1, [LV.Reading("Engine speed", "%d" % (800 + k), "rpm", 800.0 + k),
                          LV.Reading("Injection qty", "5.2", "mg", 5.2),
                          LV.Reading("Coolant", "87", "C", 87.0),
                          LV.Reading("Mode", "idle", "", None)])
    view.set_readings(11, [LV.Reading("Boost spec", "1005", "mbar", 1005.0),
                           LV.Reading("Boost actual", "1012", "mbar", 1012.0),
                           LV.Reading("N75 duty", "12.4", "%", 12.4),
                           LV.Reading("", "7", "", 7.0)])


def test_csv_log(view, LV, tmp_path):
    path = str(tmp_path / "live.csv")
    states = []
    view.loggingChanged.connect(states.append)
    view.start_log(path)
    assert view.logging_active() and states == [True]
    assert view._log_btn.text() == "Stop logging" and not view._log_btn.dot.isHidden()
    for k in range(4):
        _round(LV, view, k)
    view.stop_log()
    assert not view.logging_active() and states == [True, False]
    assert view._log_btn.text() == "Log to file" and view._log_btn.dot.isHidden()
    rows = _read_csv(path)
    assert rows[0] == ["time_s",
                       "1:1:Engine speed", "1:2:Injection qty", "1:3:Coolant", "1:4:Pedal",
                       "11:1:Boost spec", "11:2:Boost actual", "11:3:N75 duty", "11:4:Group 011 cell 4"]
    body = rows[1:]
    assert len(body) == 4                                # one row per round, the last flushed on stop
    assert [r[1] for r in body] == ["800.0", "801.0", "802.0", "803.0"]
    assert body[0][5] == "1005.0" and body[0][8] == "7.0"
    assert body[0][4] == "idle"                          # not numeric: the text
    times = [float(r[0]) for r in body]
    assert times == sorted(times) and times[0] > 0
    # stop twice is harmless
    view.stop_log()
    assert states == [True, False]


def test_csv_rows_at_least_50ms_apart(view, LV, tmp_path):
    path = str(tmp_path / "fast.csv")
    view.start_log(path)
    for k in range(10):                                  # a round every 10 ms
        LV.clock[0] += 0.01
        view.set_readings(1, _readings(LV, 4, base=float(k)))
        view.set_readings(11, _readings(LV, 4))
    view.stop_log()
    times = [float(r[0]) for r in _read_csv(path)[1:]]
    assert 1 <= len(times) <= 4
    assert all(b - a >= 0.05 - 1e-6 for a, b in zip(times[:-1], times[1:-1]))


def test_csv_header_waits_for_the_first_group(app, LV, tmp_path):
    v = LV.LiveDataView()
    v.show()
    v.move(-4000, -4000)
    v.set_groups(_groups(LV))                            # nothing ticked yet
    path = str(tmp_path / "late.csv")
    v.start_log(path)
    assert v.logging_active()
    v.row(4).checkbox().setChecked(True)
    LV.clock[0] += 1
    v.set_readings(4, _readings(LV, 3))
    LV.clock[0] += 1
    v.set_readings(4, _readings(LV, 3, base=5.0))
    v.stop_log()
    rows = _read_csv(path)
    assert rows[0][0] == "time_s" and len(rows[0]) == 4 and rows[0][1] == "4:1:Cell 0"
    assert len(rows) == 3
    v.close()


def test_start_log_error_does_not_raise(view, tmp_path):
    bad = str(tmp_path / "no" / "such" / "dir" / "x.csv")
    view.start_log(bad)
    assert not view.logging_active()
    assert "Cannot log to x.csv" in view.notice_text()
    assert view._log_btn.text() == "Log to file"


def test_write_error_stops_logging_and_says_so(view, LV, tmp_path):
    path = str(tmp_path / "w.csv")
    view.start_log(path)
    _round(LV, view, 0)

    class Boom:
        def writerow(self, row):
            raise OSError(28, "No space left on device")
    view._log_writer = Boom()
    states = []
    view.loggingChanged.connect(states.append)
    _round(LV, view, 1)                                  # the round boundary tries a write
    _round(LV, view, 2)
    assert not view.logging_active() and states == [False]
    assert "Logging stopped" in view.notice_text() and "No space" in view.notice_text()
    _round(LV, view, 3)                                  # nothing raises afterwards either


def test_log_button_runs_the_save_dialog(view, LV, tmp_path, monkeypatch):
    chosen = str(tmp_path / "chosen")                    # no extension: ".csv" is added
    seen = {}

    def fake(parent, title, start, filt):
        seen["start"] = start
        return chosen, "CSV files (*.csv)"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(fake))
    view.set_module_addr("01")
    view._log_btn.click()
    name = os.path.basename(seen["start"])
    assert name.startswith("live-01-") and name.endswith(".csv") and len(name) == len("live-01-20261027-101500.csv")
    assert view.logging_active()
    _round(LV, view, 0)
    _round(LV, view, 1)
    view._log_btn.click()                                # "Stop logging"
    assert not view.logging_active()
    assert len(_read_csv(chosen + ".csv")) >= 2
    # cancelling the dialog starts nothing
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a: ("", "")))
    view._log_btn.click()
    assert not view.logging_active()


def test_clear_forgets_readings_and_ends_the_log(view, LV, tmp_path):
    view.start_log(str(tmp_path / "c.csv"))
    _round(LV, view, 0)
    view.set_rate(12.0)
    view.clear()
    assert not view.logging_active()
    assert view.chart().keys() == []
    assert view.tiles()[0]._value == "–" and view.tiles()[0]._name == "Engine speed"
    assert "frames/s" not in view.header_text()
    assert view.checked_groups() == [1, 11]              # the selection stays


# ---------------------------------------------------------------- the themes
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_both_palettes_build_and_paint(app, LV, theme):
    from ovtsuite import gui_theme
    old = QtWidgets.QApplication.palette()
    try:
        app.setPalette(gui_theme.dark_palette() if theme == "dark" else QtGui.QPalette())
        v = LV.LiveDataView()
        v.resize(1100, 700)
        v.move(-4000, -4000)
        v.set_groups(_groups(LV), checked=[1, 11, 3])
        v.set_source_note("Named from the loaded calibration")
        for g in (1, 11, 3):
            v.set_readings(g, _readings(LV, 4))
        v.set_others([("02", "Transmission", [("Gear", "N")])])
        v.show()
        app.processEvents()
        assert not v.grab().isNull()
        v.set_graph(True)
        v.set_running(False)
        app.processEvents()
        assert not v.grab().isNull()
        v.close()
    finally:
        app.setPalette(old)

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Export As...: the formats, the classic VCDS layout (three groups to a log, plain
numbers), a log split over files or logs, a switch's bytes as text, and the window.

The classic layout is checked against a model of how a simple viewer reads it (a Delphi
log viewer, read from its disassembly): the rows holding the word ``Marker`` in their first
cell count the logs; a log's header is the six rows before its ``Marker`` row; the row two
below the date holds ``Group`` in its second cell and a block number from the cell after it."""
import csv
import io
import os
import re
import sys
import time
from datetime import datetime, timedelta

import pytest

from ovtsuite.datalog import export as EX
from ovtsuite.datalog import logfile as LF

T0 = datetime(2026, 11, 3, 21, 43, 3)


def _log(groups=((1, 4), (4, 4), (10, 4), (11, 4)), rows=4, extra=True):
    """A log of ``groups`` (number, cells): engine speed first in each, a switch cell and
    an angle that goes after TDC among them."""
    chans = []
    for g, n in groups:
        for f in range(n):
            chans.append(LF.Channel(g, f, "G%d cell %d" % (g, f + 1),
                                    {0: "rpm", 1: "mg/stroke", 2: "°BTDC", 3: "°C"}.get(f, "")))
    log = LF.DataLog(channels=chans, identity="4Z7907401B 2.5l/4VTEDC G410SG D09" if extra else "")
    for r in range(rows):
        texts = []
        for c in chans:
            if c.group == 4 and c.field == 3:
                texts.append("01001001 00001000")
            elif c.unit == "rpm":
                texts.append("%.1f rpm" % (765.6 + r))
            elif c.unit == "°BTDC":
                texts.append("0.2 °ATDC" if r == 1 else "1.5 °BTDC")
            elif c.unit == "°C":
                texts.append("N/A" if r == 2 else "77.4 °C")
            elif c.unit == "mg/stroke":
                texts.append("4.0 mg/stroke")
            else:
                texts.append("%d.0" % (r + 10))
        log.append(T0 + timedelta(seconds=0.26 * r), texts)
    return log


def _rows(path):
    return list(csv.reader(io.StringIO(open(path, "rb").read().decode("cp1252"), newline="")))


def _viewer_reads(path):
    """What the viewer model makes of ``path``: a list with one entry per log -- the block
    numbers found and the data rows -- or the error it would stop with."""
    rows = _rows(path)
    marks = [i for i, r in enumerate(rows) if r and "MARKER" in r[0].upper()]
    if not marks:
        return "no records"
    out = []
    for n, m in enumerate(marks):
        start = max(0, m - 6)
        cur = start + 2
        while cur < len(rows) - 1 and not any(w in (rows[cur][1] if len(rows[cur]) > 1 else "").upper()
                                              for w in ("GROUP",)):
            cur += 1
        blocks = []
        for j in range(3):
            cell = rows[cur][5 * j + 2] if len(rows[cur]) > 5 * j + 2 else ""
            t = cell[1:]
            blocks.append(int(t) if t.strip().isdigit() else None)
        if all(b is None for b in blocks):
            return "no records"
        end = marks[n + 1] - 6 if n + 1 < len(marks) else len(rows)
        data = [r for r in rows[m + 1:end]]
        assert all(r[0] == "" for r in data), "a data row starts with a value"
        out.append((blocks, data))
    return out


# --------------------------------------------------------------------- bits and blocks
def test_a_switch_is_its_bytes_in_binary_and_never_a_number():
    for t in ("00101011", "01001001 00001000", " 11110000 "):
        assert LF.split_value_unit(t) == (None, "")
        assert LF._classic_cell(None, t) == t.strip()          # written as it is
    assert LF.split_value_unit("1100 mbar") == (1100.0, "mbar")
    assert LF.split_value_unit("10101") == (10101.0, "")        # fewer than eight digits: a number
    assert LF._classic_cell(None, "12.5") == "12.5" and LF._classic_cell(None, "N/A") == "N/A"
    assert LF._classic_cell(None, "idle") == "N/A" and LF._classic_cell(None, "") == ""
    assert not hasattr(LF, "bits_number") and not hasattr(LF, "BITS_AS")      # no other way to write it


def test_the_blocks_are_a_groups_cells_four_at_a_time():
    log = _log(((1, 4), (2, 6), (3, 2)))
    assert [(g, len(i)) for g, i in LF.classic_blocks(log)] == [(1, 4), (2, 4), (2, 2), (3, 2)]
    assert [len(c) for c in LF.classic_chunks(log)] == [3, 1]
    assert LF.classic_chunks(LF.DataLog()) == [[]]
    assert EX.classic_plan(_log()) == [[1, 4, 10], [11]]


# --------------------------------------------------------------------- the classic file
def test_a_classic_log_has_the_shape_a_simple_viewer_needs(tmp_path):
    log = _log(((1, 4), (4, 4), (10, 4)))
    p = str(tmp_path / "c.csv")
    LF.write_vcds_classic(p, log)
    raw = open(p, "rb").read()
    assert raw.count(b"\r\n") == raw.count(b"\n") and b"\xb0BTDC" in raw       # CRLF, one ANSI byte
    rows = _rows(p)
    assert rows[0] == ["Tuesday", "03", "November", "2026", "21:43:03"]
    assert rows[1][:3] == ["4Z7907401B", "", "2.5l/4VTEDC G410SG D09"]
    assert rows[3][:3] == ["", "Group A:", "'001"] and rows[3][6:8] == ["Group B:", "'004"] \
        and rows[3][11:13] == ["Group C:", "'010"] and len(rows[3]) == 13
    assert rows[5][:6] == ["", "TIME", "", "", "", ""] and rows[6][0] == "Marker"
    assert rows[6][1:6] == ["STAMP", " /min", " mg/stroke", "\xb0BTDC", "\xb0C"]
    assert len(rows[3]) == 13 and all(len(r) == 16 for r in rows[4:])
    assert [b for b, _d in _viewer_reads(p)] == [[1, 4, 10]]


def test_the_cells_are_numbers_na_or_empty(tmp_path):
    p = str(tmp_path / "c.csv")
    LF.write_vcds_classic(p, _log(((1, 4), (4, 4))))
    data = _rows(p)[7:]
    assert [r[2] for r in data] == ["765.6", "766.6", "767.6", "768.6"]       # engine speed
    assert [r[4] for r in data] == ["1.5", "-0.2", "1.5", "1.5"]              # after TDC: negative
    assert [r[5] for r in data] == ["77.4", "77.4", "N/A", "77.4"]            # N/A as it is
    assert {r[5 + 5] for r in data} == {"01001001 00001000"}                  # the switch, as written
    assert all(r[0] == "" and r[1] == r[6] for r in data)                     # a time per block


def test_there_is_no_option_for_the_switch(tmp_path):
    import dataclasses
    import inspect
    assert "bits" not in inspect.signature(LF.write_vcds_classic).parameters
    assert [f.name for f in dataclasses.fields(EX.Options)] == ["groups"]
    with pytest.raises(TypeError):
        EX.Options("logs", "number")


def test_a_block_with_fewer_than_four_cells_and_missing_blocks_are_padded(tmp_path):
    p = str(tmp_path / "c.csv")
    LF.write_vcds_classic(p, _log(((11, 3),)))
    rows = _rows(p)
    assert rows[3][1:3] == ["Group A:", "'011"] and rows[3][6:8] == ["Group B:", " Not Running"]
    assert rows[7][:6] == ["", "0.00", "765.6", "4.0", "1.5", ""]
    assert len(rows[7]) == 16 and _viewer_reads(p)[0][0] == [11, None, None]


# --------------------------------------------------------------------- more than three groups
def test_more_than_three_groups_one_log_after_another(tmp_path):
    p = str(tmp_path / "multi.csv")
    log = _log()
    assert EX.export(p, log, "vcds_classic", EX.Options("logs")) == [p]
    got = _viewer_reads(p)
    assert [b for b, _d in got] == [[1, 4, 10], [11, None, None]]            # two logs, found by Marker
    assert all(len(d) == 4 for _b, d in got)
    back = LF.load(p)                                                        # our own reader: side by side again
    assert (back.layout, len(back.channels), back.rows) == ("vcds", 16, 4)
    assert [c.group for c in back.channels] == [1] * 4 + [4] * 4 + [10] * 4 + [11] * 4


def test_more_than_three_groups_one_file_for_each_three(tmp_path):
    p = str(tmp_path / "split.csv")
    out = EX.export(p, _log(), "vcds_classic", EX.Options("files"))
    assert [os.path.basename(x) for x in out] == ["split_part1_groups_1-4-10.csv",
                                                  "split_part2_groups_11.csv"]
    assert not os.path.exists(p)
    assert [_viewer_reads(x)[0][0] for x in out] == [[1, 4, 10], [11, None, None]]
    one = EX.export(str(tmp_path / "small.csv"), _log(((1, 4), (2, 4))), "vcds_classic")
    assert [os.path.basename(x) for x in one] == ["small.csv"]               # fits one log: no parts


def test_a_classic_file_reads_back_with_its_values(tmp_path):
    p = str(tmp_path / "c.csv")
    LF.write_vcds_classic(p, _log(((1, 4), (4, 4), (10, 4))))
    back = LF.load(p)
    assert back.identity == "4Z7907401B 2.5l/4VTEDC G410SG D09" and back.rows == 4
    assert [(c.group, c.field) for c in back.channels[:5]] == [(1, 0), (1, 1), (1, 2), (1, 3), (4, 0)]
    assert back.channels[0].unit == "/min" and back.texts[0][0] == "765.6 /min"
    assert back.values[1][2] == -0.2 and back.texts[2][3] == "N/A"
    assert back.stamps[0] == T0 and back.stamps[1] == T0 + timedelta(seconds=0.26)


CLASSIC_REAL = (
    "Friday,03,November,2006,22:50:12\r\n"
    "8D0 907 551 M,,2.7l V6/5VT     G01 0002,\r\n\r\n"
    ",Group A:,'115,,,,Group B:,'118,,,,Group C:, Not Running\r\n"
    ",,RPM,Load,Absolute Pres.,Absolute Pres.,,RPM,Temperature,Load,Absolute Pres.,,,,,\r\n"
    ",TIME,,,,,TIME,,,,,TIME,,,,\r\n"
    "Marker,STAMP, /min, %, mbar, mbar,STAMP, /min,\xb0C, %, mbar,STAMP,,,,\r\n"
    ",0.25,840,19.5,1000.0,1040.0,0.01,840,54.0,0.0,1040.0,,,,,\r\n"
    ",0.85,1120,23.3,1000.0,1050.0,0.55,1000,54.0,0.0,1040.0,,,,,\r\n")


def test_a_classic_log_written_by_vcds_itself_opens(tmp_path):
    """(An ANSI file: the degree sign is one byte. The reader used to need UTF-8.)"""
    p = str(tmp_path / "vcds-115-118.csv")
    open(p, "wb").write(CLASSIC_REAL.encode("cp1252"))
    log = LF.load(p)
    assert (log.layout, log.rows, len(log.channels)) == ("vcds", 2, 8)
    assert [(c.group, c.field, c.label, c.unit) for c in log.channels[4:6]] == \
        [(118, 0, "RPM", "/min"), (118, 1, "Temperature", "\xb0C")]
    assert log.identity == "8D0 907 551 M 2.7l V6/5VT     G01 0002"
    assert log.texts[1][:3] == ["1120 /min", "23.3 %", "1000.0 mbar"]
    assert _viewer_reads(p)[0][0] == [115, 118, None]


# --------------------------------------------------------------------- the other formats
def test_export_writes_each_format_and_the_native_one_reads_back(tmp_path):
    log = _log()
    p = str(tmp_path / "n.csv")
    assert EX.export(p, log, "ovt") == [p]
    assert LF.load(p).texts == log.texts
    q = str(tmp_path / "a.csv")
    EX.export(q, log, "vcds_advmb")
    LF.write_vcds(str(tmp_path / "ref.csv"), log)
    assert open(q, "rb").read() == open(str(tmp_path / "ref.csv"), "rb").read()   # left alone
    assert [f.key for f in EX.FORMATS] == ["ovt", "vcds_advmb", "vcds_classic"]
    assert EX.by_key("vcds_classic").suffix == "_vcds_classic"
    with pytest.raises(KeyError):
        EX.export(p, log, "nope")


def test_the_advmb_export_still_writes_a_switch_as_it_reads(tmp_path):
    q = str(tmp_path / "a.csv")
    EX.export(q, _log(((1, 4), (4, 4))), "vcds_advmb")
    assert "01001001 00001000" in open(q, encoding="utf-8").read()


# --------------------------------------------------------------------- the window
@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def prefs(tmp_path, monkeypatch):
    from ovtsuite import recent
    store = {}
    monkeypatch.setattr(recent, "get_setting", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(recent, "set_setting", lambda k, v: store.__setitem__(k, v))
    return store


def test_the_dialog_lists_the_formats_and_their_options(app, prefs):
    from ovtsuite.datalog import gui_export as GE
    d = GE.ExportDialog(_log())
    assert [d.formats.item(i).text() for i in range(d.formats.count())] == \
        [f.title for f in EX.FORMATS]
    assert d.formats.item(0).text() == "OVT-Suite log (CSV)"
    assert d._key() == "ovt" and not d.classic_box.isEnabled()
    d.formats.setCurrentRow(2)
    assert d.classic_box.isEnabled() and d.groups_files.isEnabled()
    assert "4 groups of cells, so it becomes 2 logs: groups 1, 4, 10; groups 11" in d.plan.text()
    d.groups_logs.setChecked(True)
    assert not hasattr(d, "bits_text") and not hasattr(d, "bits_number")
    assert d.choice() == ("vcds_classic", EX.Options("logs"))
    small = GE.ExportDialog(_log(((1, 4),)))
    small.formats.setCurrentRow(2)
    assert small.classic_box.isEnabled() and not small.groups_files.isEnabled()
    assert "fits one classic log" in small.plan.text()


def test_export_as_asks_then_writes_and_remembers(app, prefs, tmp_path, monkeypatch):
    from PySide6 import QtWidgets
    from ovtsuite.datalog import gui_export as GE
    out = str(tmp_path / "x.csv")
    monkeypatch.setattr(GE.ExportDialog, "exec", lambda self: (
        self.formats.setCurrentRow(2), self.groups_files.setChecked(True),
        QtWidgets.QDialog.DialogCode.Accepted)[-1])
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (out, "")))
    got = GE.export_log_as(None, _log(), "mylog")
    assert [os.path.basename(x) for x in got] == ["x_part1_groups_1-4-10.csv", "x_part2_groups_11.csv"]
    assert prefs["export/format"] == "vcds_classic" and "export/bits" not in prefs
    # the next window opens on what was chosen
    assert GE.ExportDialog(_log())._key() == "vcds_classic"
    # backing out of either step writes nothing
    monkeypatch.setattr(GE.ExportDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Rejected)
    assert GE.export_log_as(None, _log(), "mylog") is None


def test_the_native_layout_of_a_stored_log_is_a_plain_copy(app, prefs, tmp_path, monkeypatch):
    from PySide6 import QtWidgets
    from ovtsuite.datalog import gui_export as GE
    src = str(tmp_path / "src.csv")
    LF.write_kwhack(src, _log(((1, 4),)))
    raw = open(src, "rb").read() + b"# kept exactly\n"
    out = str(tmp_path / "copy.csv")
    monkeypatch.setattr(GE.ExportDialog, "exec", lambda self: QtWidgets.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (out, "")))
    assert GE.export_stored_log(None, "src.csv", raw) == out
    assert open(out, "rb").read() == raw


@pytest.mark.slow
def test_the_viewer_has_one_export_button(app, tmp_path, monkeypatch):
    from PySide6 import QtCore, QtWidgets
    from ovtsuite.datalog import gui_log as GL

    class Win(QtWidgets.QMainWindow):
        def workspaces(self):
            return []
        current = None
        primary = None
    monkeypatch.setattr(GL, "_prefs", lambda: QtCore.QSettings(
        str(tmp_path / "prefs.ini"), QtCore.QSettings.Format.IniFormat))
    d = GL.DataLogDialog(Win())
    try:
        assert d.export_btn.text() == "Export As..."
        assert not hasattr(d, "exp_vcds") and not hasattr(d, "exp_kw")
        texts = [b.text() for b in d.findChildren(QtWidgets.QPushButton)]
        assert not any(t.startswith("Export as") for t in texts)
        assert "Export As..." in texts
    finally:
        for p in d.panes:
            p._worker = None
        d.close()


# --------------------------------------------------------------------- the defaults
def test_the_defaults_are_text_for_a_switch_and_one_file_of_several_logs(app, prefs):
    from ovtsuite.datalog import gui_export as GE
    assert EX.Options() == EX.Options("logs")
    d = GE.ExportDialog(_log())
    d.formats.setCurrentRow(2)
    assert d.choice() == ("vcds_classic", EX.Options("logs"))
    assert d.groups_logs.isChecked()


def test_the_default_classic_export_is_one_file_with_the_switch_as_written(tmp_path):
    p = str(tmp_path / "d.csv")
    assert EX.export(p, _log(), "vcds_classic") == [p]
    assert len(_viewer_reads(p)) == 2 and "01001001 00001000" in open(p, "rb").read().decode("cp1252")


# --------------------------------------------------------------------- a switch written by VCDS (EDC16)
CRUISE = "\r\n".join([
    "Friday,05,June,2026,20:34:10:00009-VCID:224B5F8FFECFF883F5-5178,VCDS Version: Release 23.3.0,Data version: 20230228 DS346.1",
    "038 906 016 Q,ADVMB,R4 1.9L EDC G000SG  6110,",
    ",,G005,F0,G006,F0,G006,F1,G006,F2,G006,F3,G000,F0,G000,F0,G000,F0,G000,F0,G000,F0,G000,F0,G000,F0,",
    "",
    "Marker,TIME,Group 5 - Field 0,TIME,Group 6 - Field 0,TIME,Group 6 - Field 1,TIME,Group 6 - Field 2,TIME,Group 6 - Field 3,,,,,,,,,,,,,,,",
    ",STAMP,Engine Speed - (G28),STAMP,Vehicle Speed - (actual),STAMP,Cruise Control - Pedal Monitoring,STAMP,Accel. Pedal Pos. - Sensor 1 (G79),STAMP,Cruise Control - Switch Monitoring,,,,,,,,,,,,,,,",
    ",, /min,, km/h,,,, %,,,,,,,,,,,,,,,,,",
    ",0.27,903,0.11,0.0,0.11,00101011,0.11,0.0,0.11,00000011,,,,,,,,,,,,,,,",
    ",0.58,903,0.42,0.0,0.42,00101011,0.42,0.0,0.42,00000011,,,,,,,,,,,,,,,",
    ",3.70,1071,3.55,0.0,3.55,00101100,3.55,6.7,3.55,00000111,,,,,,,,,,,,,,,",
    ""])


def test_switch_bytes_a_vcds_log_writes_as_text_are_text_here(tmp_path):
    """An EDC16 log from VCDS (single bytes, ``00101011``): the cells are text, not the
    number 101011 -- and an advanced-measuring-blocks export gives them back as written."""
    src = str(tmp_path / "Cruise.csv")
    open(src, "wb").write(CRUISE.encode("cp1252"))
    log = LF.load(src)
    assert [t for t in log.texts[0]] == ["903 /min", "0.0 km/h", "00101011", "0.0 %", "00000011"]
    assert log.values[0] == [903.0, 0.0, None, 0.0, None] and log.channels[2].unit == ""
    out = str(tmp_path / "out.csv")
    EX.export(out, log, "vcds_advmb")
    rows = list(csv.reader(open(out, encoding="utf-8", newline="")))
    assert [r[2:11:2] for r in rows[7:]] == [["903", "0.0", "00101011", "0.0", "00000011"],
                                             ["903", "0.0", "00101011", "0.0", "00000011"],
                                             ["1071", "0.0", "00101100", "6.7", "00000111"]]
    again = LF.load(out)
    assert again.texts == log.texts and again.values == log.values
    cls = str(tmp_path / "classic.csv")
    EX.export(cls, log, "vcds_classic")                         # one log of two blocks: as written
    data = _rows(cls)[7:]
    assert data[0][2] == "903" and data[0][7:11] == ["0.0", "00101011", "0.0", "00000011"]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The VCDS CSV layout written by datalog.logfile: what a cell, a name and a line end look
like, checked against what VCDS itself writes (Reference/Tunes/6hponlydrive.CSV)."""
import csv
from datetime import datetime, timedelta

from ovtsuite.datalog import logfile as LF

#: rows of the file before the data: the date, the part number, the Gxxx/Fy markers, a blank
#: row, "Marker/TIME/Group n - Field m", "STAMP/names" and the units
_HEAD = 7


def _log():
    chans = [LF.Channel(1, 0, "Engine speed (G1/1)", "rpm"),
             LF.Channel(1, 1, "Accelerator pedal sensor", "%"),
             LF.Channel(1, 2, "Switch (A/C, closed throttle, raised idle) (G1/3)"),
             LF.Channel(1, 3, "Boost", "mbar")]
    log = LF.DataLog(channels=chans)
    t0 = datetime(2026, 11, 1, 21, 7, 38)
    rows = [["903 rpm", "0.0 %", "01001001 00001000", "1020 mbar"],
            ["881.5 rpm", "12.5 %", "01001001 00001000", "N/A"],
            ["0 rpm", "100.0 %", "00000000 00000000", "999.6 mbar"]]
    for i, texts in enumerate(rows):
        log.append(t0 + timedelta(seconds=0.26 * i), texts)
    return log


def _read(path):
    return list(csv.reader(open(path, encoding="utf-8", newline="")))


def test_a_bit_pattern_cell_is_not_a_number_and_not_a_unit():
    log = _log()
    assert log.values[0][2] is None
    assert log.channels[2].unit == ""            # it used to become "00001000"
    assert LF.split_value_unit("01001001 00001000") == (None, "")
    assert LF.split_value_unit("812 rpm") == (812.0, "rpm")
    assert LF.split_value_unit("1 1/min") == (1.0, "1/min")          # a unit may start with a digit


def test_the_vcds_cells_keep_their_text_and_decimals(tmp_path):
    p = str(tmp_path / "x_vcds.csv")
    LF.write_vcds(p, _log())
    rows = _read(p)
    assert [r[2::2][:4] for r in rows[_HEAD:]] == [
        ["903", "0.0", "01001001 00001000", "1020"],
        ["881.5", "12.5", "01001001 00001000", "N/A"],
        ["0", "100.0", "00000000 00000000", "999.6"]]
    assert rows[_HEAD - 1][2::2][:4] == ["rpm", "%", "", "mbar"]


def test_the_vcds_file_has_crlf_line_ends_and_plain_names(tmp_path):
    p = str(tmp_path / "x_vcds.csv")
    LF.write_vcds(p, _log())
    raw = open(p, "rb").read()
    assert raw.count(b"\n") == _HEAD + 3 and raw.count(b"\r\n") == raw.count(b"\n")
    rows = _read(p)
    assert rows[_HEAD - 3][2::2][:4] == ["Group 1 - Field 0", "Group 1 - Field 1",
                                         "Group 1 - Field 2", "Group 1 - Field 3"]
    assert rows[_HEAD - 2][2::2][:4] == ["Engine speed", "Accelerator pedal sensor",
                                         "Switch (A/C, closed throttle, raised idle)", "Boost"]


def test_a_vcds_file_reads_back_to_the_same_cells(tmp_path):
    p = str(tmp_path / "x_vcds.csv")
    LF.write_vcds(p, _log())
    back = LF.load(p)
    assert back.layout == "vcds"
    assert [c.unit for c in back.channels] == ["rpm", "%", "", "mbar"]
    assert back.texts[0] == ["903 rpm", "0.0 %", "01001001 00001000", "1020 mbar"]
    assert back.texts[1][3] == "N/A"             # no unit made up for a cell with no number
    assert back.values[1][1] == 12.5 and back.values[0][2] is None


def test_the_text_of_one_vcds_cell():
    assert LF._vcds_cell(None, "") == ""
    assert LF._vcds_cell(4.0, "") == "4"
    assert LF._vcds_cell(None, "N/A") == "N/A"
    assert LF._vcds_cell(812.0, "812 rpm") == "812"
    assert LF._vcds_cell(0.0, "0.0 %") == "0.0"


def test_an_angle_after_tdc_is_negative_before_tdc(tmp_path):
    """"1.5 °BTDC" and "0.2 °ATDC" are two spellings of one column: written as plain
    numbers the second came back as 0.2 BTDC."""
    log = LF.DataLog(channels=[LF.Channel(4, 1, "Start of injection setpoint", "")])
    t0 = datetime(2026, 11, 1, 21, 7, 38)
    for i, t in enumerate(["1.5 °BTDC", "0.2 °ATDC", "0.0 °ATDC", "-2.0 °BTDC"]):
        log.append(t0 + timedelta(seconds=i), [t])
    assert log.channels[0].unit == "°BTDC"
    p = str(tmp_path / "soi_vcds.csv")
    LF.write_vcds(p, log)
    rows = _read(p)
    assert [r[2] for r in rows[_HEAD:]] == ["1.5", "-0.2", "0.0", "-2.0"]
    assert rows[_HEAD - 1][2] == "°BTDC"
    back = LF.load(p)
    assert [round(v, 1) for v in back.column(0)] == [1.5, -0.2, 0.0, -2.0]
    # a column that starts after TDC says BTDC in the unit row all the same
    late = LF.DataLog(channels=[LF.Channel(4, 1, "SOI", "")])
    for i, t in enumerate(["0.2 °ATDC", "1.5 °BTDC"]):
        late.append(t0 + timedelta(seconds=i), [t])
    LF.write_vcds(p, late)
    rows = _read(p)
    assert rows[_HEAD - 1][2] == "°BTDC" and [r[2] for r in rows[_HEAD:]] == ["-0.2", "1.5"]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Reading logs VCDS itself wrote: what the first line says about when it was taken (any
language, with the extra fields recent VCDS puts after the seconds), a field name that
runs over two header lines (German), and how a kept log is told to be a VCDS one.

The files are cut-down copies of real ones (ADVMB in German with the ``-VCID`` stamp,
the classic layout in German, the classic layout in English), in Windows ANSI as VCDS
writes them."""
from datetime import datetime

from ovtsuite.datalog import logfile as LF

ADVMB_DE = (
    "Sonntag,30,November,2025,10:53:42:16365-VCID:372B2BC12FCE7A627E0-8062,"
    "VCDS Version: DRV 25.3.1,Datenstand: 20250429 DS365.0\n"
    "4G0 907 589 F,ADVMB,3.0BTD EDC17  H23 0010,\n"
    ",,G004,F0,G026,F0,G000,F0,\n"
    "\n"
    "Markierung,Zeit,Loc. IDE00021,Zeit,Loc. IDE00190,,,\n"
    ",MARKE,Motordrehzahl,MARKE,Ladedruck: Sollwert,,,\n"
    ",, /min,, hPa,,,\n"
    ",0.05,1498,0.05,1063,,,\n"
    ",0.18,1483,0.18,1062,,,\n")

CLASSIC_DE = (
    "Mittwoch,13,Mai,2020,20:12:42:14802,VCDS Version: DRV 12.12.2,Datenstand: 20140410\r\n"
    "06A 906 018 R,,1.8L R4/5VT MOTR HS D03,\r\n"
    "\r\n"
    ",Block A:,'002,,,,Block B:,'007,,,,Block C:, L\xe4uft nicht\r\n"
    ",,Motordrehzahl,Motorlast,Einspritzzeit,angesaugte,,Lambda,Lambdasonden-,"
    "Tastverh\xe4ltnis,Lambda,,,,,\r\n"
    ",Zeit,,,(korrigiert),Luftmasse,Zeit,Regler,spannung,Magnetventil 1,"
    "Korrekturfaktor,Zeit,,,,\r\n"
    "Markierung,MARKE, /min, ms, ms, g/s,MARKE, %, V, %,,MARKE,,,,\r\n"
    ",0.25,2080,3.80,8.13,25.28,0.01,1.6,0.215,0.0,1.00,,,,,\r\n"
    ",0.75,2160,4.40,9.54,30.34,0.50,0.0,0.845,99.6,1.00,,,,,\r\n")

CLASSIC_EN = (
    "Friday,03,November,2006,21:59:29\r\n"
    "8D0 907 551 M,,2.7l V6/5VT     G   0002,\r\n"
    "\r\n"
    ",Group A:,'002,,,,Group B:,'031,,,,Group C:, Not Running\r\n"
    ",,RPM,Load,Inj. On Time,Mass Flow,,Voltage,Voltage,Voltage,Voltage,,,,,\r\n"
    ",TIME,,,,,TIME,,,,,TIME,,,,\r\n"
    "Marker,STAMP, /min, %, ms, g/s,STAMP, V, V, V, V,STAMP,,,,\r\n"
    ",0.58,2080,11.3,0.00,7.14,0.28,0.035,0.730,0.030,0.070,,,,,\r\n")


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_bytes(text.encode("cp1252"))
    return str(p)


def test_the_date_line_is_read_in_any_language_and_with_extra_fields():
    assert LF.vcds_started("Sonntag,30,November,2025,10:53:42:16365-VCID:37-8062".split(",")) \
        == datetime(2025, 11, 30, 10, 53, 42)
    assert LF.vcds_started(["Mittwoch", "13", "Mai", "2020", "20:12:42:14802"]) \
        == datetime(2020, 5, 13, 20, 12, 42)
    assert LF.vcds_started(["Samstag", "07", "M\xe4rz", "2020", "01:02:03"]) \
        == datetime(2020, 3, 7, 1, 2, 3)
    assert LF.vcds_started(["Friday", "03", "November", "2006", "21:59:29"]) \
        == datetime(2006, 11, 3, 21, 59, 29)
    assert LF.vcds_started(["Samedi", "03", "juin", "2006", "21:59:29"]).month == 6
    assert LF.vcds_started(["4G0 907 589 F", "ADVMB", "3.0 TDI", ""]) is None
    assert LF.vcds_started(["Friday", "03", "Nonsense", "2006", "21:59:29"]) is None


def test_an_advmb_log_in_german_gets_its_start_time(tmp_path):
    log = LF.load(_write(tmp_path, "ide.csv", ADVMB_DE))
    assert log.layout == "vcds" and log.rows == 2
    assert log.stamps[0].replace(microsecond=0) == datetime(2025, 11, 30, 10, 53, 42)
    assert [c.label for c in log.channels] == ["Motordrehzahl", "Ladedruck: Sollwert"]
    assert [(c.group, c.field) for c in log.channels] == [(4, 0), (26, 0)]
    assert log.texts[0] == ["1498 /min", "1063 hPa"]


def test_a_german_classic_log_joins_a_name_that_runs_over_two_lines(tmp_path):
    log = LF.load(_write(tmp_path, "g.csv", CLASSIC_DE))
    assert [c.label for c in log.channels] == [
        "Motordrehzahl", "Motorlast", "Einspritzzeit (korrigiert)", "angesaugte Luftmasse",
        "Lambda Regler", "Lambdasonden-spannung", "Tastverh\xe4ltnis Magnetventil 1",
        "Lambda Korrekturfaktor"]
    assert [(c.group, c.field) for c in log.channels][:5] == [
        (2, 0), (2, 1), (2, 2), (2, 3), (7, 0)]
    assert log.stamps[0].replace(microsecond=0) == datetime(2020, 5, 13, 20, 12, 42)
    assert log.texts[1][1] == "4.40 ms"


def test_an_english_classic_log_is_unchanged(tmp_path):
    log = LF.load(_write(tmp_path, "e.csv", CLASSIC_EN))
    assert [c.label for c in log.channels][:4] == ["RPM", "Load", "Inj. On Time", "Mass Flow"]
    assert log.stamps[0].replace(microsecond=0) == datetime(2006, 11, 3, 21, 59, 29)
    assert log.texts[0][0] == "2080 /min"


def test_a_kept_log_is_told_to_be_vcds_by_its_bytes():
    assert LF.sniff(ADVMB_DE.encode("cp1252")) == "vcds"
    assert LF.sniff(CLASSIC_DE.encode("cp1252")) == "vcds"
    assert LF.sniff(CLASSIC_EN.encode("cp1252")) == "vcds"
    assert LF.sniff(b"# Cells: 1.1,1.2\nTimestamp,A,B\n") == "kwhack"
    assert LF.sniff(b"just,some,table\n1,2,3\n") == ""


def test_a_log_added_to_a_project_carries_its_layout():
    from ovtsuite.project import Project
    proj = Project("t")
    a = proj.add_log("ide.csv", ADVMB_DE.encode("cp1252"))
    b = proj.add_log("table.csv", b"x,y\n1,2\n")
    assert a.extra.get("layout") == "vcds"
    assert "layout" not in b.extra

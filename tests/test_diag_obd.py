# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag P4: generic OBD-II (SAE J1979) over ISO 15765-4 CAN and ISO
9141-2 K-line, against scripted emissions modules."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_obd import MockObdCan, MockObdKLine, ObdModule  # noqa: E402

from ovtsuite.diag import obd as O


def _engine():
    return ObdModule({0x04: b"\x40", 0x05: b"\x5A", 0x0C: b"\x0B\xB8", 0x0D: b"\x32", 0x10: b"\x03\xE8",
                      0x11: b"\x33", 0x1C: b"\x06", 0x21: b"\x00\x7B", 0x42: b"\x35\x6A", 0x5C: b"\x6E",
                      0xA6: b"\x00\x1E\x84\x80", 0x02: b""},
                     dtcs=[O.encode_dtc("P0299"), O.encode_dtc("P0401")], pending=[O.encode_dtc("P0101")],
                     vin="WAUZZZ4F26N123456", cal_ids=["03G906016LQ 0971"], ecu_name="ECM", mil=True,
                     compression=True)


def _gearbox():
    return ObdModule({0x05: b"\x50", 0x1C: b"\x06"}, dtcs=[O.encode_dtc("P0700")], ecu_name="TCM")


def test_pid_table_and_dtc_forms():
    assert O.decode_dtc(b"\x02\x99") == "P0299" and O.decode_dtc(b"\x47\x00") == "C0700"
    assert O.decode_dtc(b"\x81\x23") == "B0123" and O.decode_dtc(b"\xC1\x00") == "U0100"
    assert O.encode_dtc("P0299") == b"\x02\x99" and O.encode_dtc("U0100") == b"\xC1\x00"
    assert O.PIDS[0x0C].value(b"\x0B\xB8") == 750.0 and O.PIDS[0x05].value(b"\x5A") == 50
    assert O.PIDS[0x06].value(b"\x80") == 0.0 and O.PIDS[0x0E].value(b"\x80") == 0.0
    assert O.PIDS[0x42].value(b"\x35\x6A") == 13.674 and O.PIDS[0xA6].value(b"\x00\x1E\x84\x80") == 200000.0
    assert O.supported_pids(1, {0x00: bytes.fromhex("BE1FA813")}) == [1, 3, 4, 5, 6, 7, 12, 13, 14, 15, 16, 17,
                                                                        19, 21, 28, 31, 32]
    st = O.decode_monitor_status(bytes.fromhex("83 1F 6B 24"))
    assert st["mil"] and st["dtc_count"] == 3 and st["ignition"] == "compression"
    names = {m["name"]: m["complete"] for m in st["monitors"]}
    assert names["misfire"] is False and names["fuel system"] and names["comprehensive components"]
    assert names["NMHC catalyst"] and names["NOx / SCR aftertreatment"] and names["PM filter"] is True
    assert names["exhaust gas sensor"] is False and "-" not in names and "catalyst" not in names
    assert O.format_value(0x0C, 750.0) == "750 rpm" and O.format_value(0x42, 13.674) == "13.67 V" and O.format_value(0x1C, "EOBD") == "EOBD"


def test_can_two_modules_answer_the_functional_request():
    eng, gbx = _engine(), _gearbox()
    ch = MockObdCan({0x7E8: eng, 0x7E9: gbx})
    sess = O.ObdSession(O.ObdCan(ch))
    assert sess.connect().startswith("ISO 15765-4")
    sup = sess.supported(0x01)
    assert sorted(sup) == [0x7E8, 0x7E9]
    assert 0x0C in sup[0x7E8] and 0xA6 in sup[0x7E8] and sup[0x7E9] == [0x05, 0x1C]
    vals = sess.read_pids([0x0C, 0x05, 0x1C])
    assert vals[0x7E8][0x0C] == 750.0 and vals[0x7E9][0x05] == 40 and vals[0x7E8][0x1C] == "EOBD"
    st = sess.status()
    assert st[0x7E8]["mil"] and st[0x7E8]["dtc_count"] == 2 and not st[0x7E9]["mil"]
    dtcs = sess.dtcs()
    assert [(d.code, d.module, d.kind) for d in dtcs] == [("P0299", 0x7E8, "stored"), ("P0401", 0x7E8, "stored"),
                                                          ("P0700", 0x7E9, "stored"), ("P0101", 0x7E8, "pending")]
    # mode 09: the 17-byte VIN is a multi-frame answer, flow control to the engine's own request id
    assert sess.vin() == "WAUZZZ4F26N123456"
    assert any(cid == 0x7E0 and p[0] >> 4 == 3 for cid, p in ch.log)
    info = sess.info()
    assert info[0x7E8]["Calibration ID"] == ["03G906016LQ 0971"] and info[0x7E9]["ECU name"] == "TCM"
    ff = sess.freeze_frame(0)
    assert ff[0x7E8][0x02] == "P0299" and ff[0x7E8][0x0C] == 750.0
    snap = sess.snapshot()
    assert snap.vin == "WAUZZZ4F26N123456" and snap.modules == [0x7E8, 0x7E9]
    assert snap.pids[0x7E8][0x0D] == 50 and 0x00 not in snap.pids[0x7E8] and 0x01 not in snap.pids[0x7E8]
    d = snap.to_dict()
    assert d["pids"]["2024"]["0C"] == 750.0 and d["dtcs"][0]["code"] == "P0299"
    assert sess.clear() == [0x7E8, 0x7E9] and eng.cleared == 1 and sess.dtcs() == []


def test_kline_iso9141_single_module():
    eng = _engine()
    k = MockObdKLine(eng, src=0x10)
    t = O.ObdKLine(k)
    sess = O.ObdSession(t)
    assert sess.connect().startswith("ISO 9141-2") and k.woken == [0x33] and t.iso9141
    assert k.frames == [] and not k.rx                            # the stray ~address was cleared
    vals = sess.read_pids([0x0C, 0x05])
    assert vals == {0x10: {0x0C: 750.0, 0x05: 50}}
    assert k.frames[-1] == bytes.fromhex("68 6A F1 01 05") + bytes([sum(bytes.fromhex("68 6A F1 01 05")) & 0xFF])
    dtcs = sess.dtcs(kinds=("stored",))
    assert [d.code for d in dtcs] == ["P0299", "P0401"]            # no count byte, zero pairs skipped
    st = sess.status()
    assert st[0x10]["dtc_count"] == 2
    sup = sess.supported(0x01)
    assert 0x0C in sup[0x10]
    # mode 09 over K-line: the VIN arrives as five 4-byte messages with a
    # sequence byte each (the first padded), joined in order
    assert sess.vin() == "WAUZZZ4F26N123456"
    assert sess.info()[0x10]["VIN"] == "WAUZZZ4F26N123456"
    assert sess.clear() == [0x10]

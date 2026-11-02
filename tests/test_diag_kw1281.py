# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag P1: the generic KW1281 command set through ModuleSession, over
the scripted KW1281 module (tests/mock_kw1281.py)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_kw1281 import Link, MockKw1281Module  # noqa: E402

from ovtsuite.diag import defs as D
from ovtsuite.diag import link as L
from ovtsuite.diag.kw1281 import ACTUATOR_END, Refused
from ovtsuite.diag.session import ModuleSession, NotSupported


def _resolver():
    return D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))


def _session(ecu, addr="17"):
    res = _resolver()
    row = res.module_address(addr, None)
    link = L.Link(Link(ecu), [], "kline")
    sess = ModuleSession(link, row, res, generation="C6")
    sess.connect()
    return sess


def test_connect_kw1281_cluster_identifies():
    ecu = MockKw1281Module(address=0x17, baud=10400, part="4F0920930N", coding=7231, wsc=12345)
    sess = _session(ecu)
    assert sess.protocol == "kw1281" and sess.baud == 10400
    assert sess.info.part == "4F0920930N"
    assert sess.info.coding == 7231 and sess.info.wsc == 12345
    assert sess.moddef == {}                 # no KW1281 module document for 17 yet, and that is fine
    faults = sess.read_faults()
    assert [f.code for f in faults] == [1314] and faults[0].proto == "kw1281"
    sess.close()
    assert ecu.mode == "idle"


def test_connect_falls_back_to_9600_for_an_edc15():
    ecu = MockKw1281Module(address=0x01, baud=9600, part="038906012GN", desc="1.9l R4 EDC 00SG 2060",
                           coding=1, wsc=0)
    sess = _session(ecu, "01")
    assert sess.baud == 9600 and sess.protocol == "kw1281"
    assert sess.info.extra["baud"] == 9600                    # the rate it answered at, for the tree and the local pack
    assert sess.kline_bauds() == [10400, 9600, 4800]
    assert sess.info.part == "038906012GN"
    assert sess.moddef["identification"]["record"] == "kw1281_blocks"
    sess.close()


def test_adaptation_read_test_save_needs_login():
    ecu = MockKw1281Module()
    sess = _session(ecu)
    a = sess.adaptation_read(2)
    assert a.channel == 2 and a.value == 150
    assert len(a.cells) == 1 and a.cells[0].fid == 0x01     # the appended measuring value
    with pytest.raises(Refused):
        sess.adaptation_read(99)
    t = sess.adaptation_test(2, 160)
    assert t.value == 160 and ecu.tested == [(2, 160)]
    with pytest.raises(Refused):                            # not logged in
        sess.adaptation_save(2, 160)
    assert sess.login(99999) is False
    assert sess.login(12345) is True
    s = sess.adaptation_save(2, 160)
    assert s.value == 160 and ecu.saved == [(2, 160, 12345)]   # WSC defaulted to the module's own
    sess.adaptation_save(40, 301, wsc=70000)
    assert ecu.saved[-1] == (40, 301, 70000)
    assert sess.adaptation_read(2).value == 160
    sess.keepalive()
    sess.close()


def test_coding_write_reports_new_identity():
    ecu = MockKw1281Module(coding=7231, wsc=12345)
    sess = _session(ecu)
    ok, coding, wsc = sess.set_coding(7233)
    assert ok and coding == 7233 and wsc == 12345
    assert ecu.coding == 7233 and sess.info.coding == 7233
    ok, coding, wsc = sess.set_coding(7233, wsc=70001)        # WSC above 65535 rides on bit 0
    assert ok and wsc == 70001 and ecu.wsc == 70001
    sess.close()


def test_actuator_sequence_and_groups_and_basic_settings():
    ecu = MockKw1281Module()
    sess = _session(ecu)
    steps = []
    while True:
        st = sess.actuator_next()
        steps.append(st)
        if st.ended:
            break
    assert [s.id for s in steps] == [0x0296, 0x0295, 0x04AB]
    assert steps[-1].raw[:2] == ACTUATOR_END and not steps[0].ended
    g = sess.read_group_full(1)
    assert g.available and len(g.cells) == 4 and g.cells[0].a == 200
    assert not sess.read_group_full(5).available
    raw = sess.read_group_full(0)
    assert raw.raw == bytes([0x10, 0x20, 0x30, 0x40])
    b = sess.basic_setting_read(60)
    assert b.available and b.cells[0].b == 30
    assert ecu.log[-1][0] == 0x28
    sess.close()


def test_memory_reads_and_eeprom_write():
    ecu = MockKw1281Module()
    sess = _session(ecu)
    rom = bytes(range(256)) * 8
    assert sess.read_memory_kw("rom", 0x0100, 20) == rom[0x100:0x114]
    assert sess.read_memory_kw("eeprom", 0, 8) is None       # refused before login
    sess.login(12345)
    assert sess.read_memory_kw("eeprom", 0, 40) == rom[:40]  # three 16-byte reads, the last short
    assert sess.kw1281.write_eeprom(0x10, b"\xAA\xBB") is True
    assert ecu.wrote == [(0x10, b"\xAA\xBB")]
    sess.close()


def test_kwp2000_module_refuses_kw1281_only_functions():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mock_tp20 import MockTp20Ecu
    from ovtsuite.flash import tp20_transport as TT
    res = _resolver()
    ecu = MockTp20Ecu(lambda req: b"\x50\x89" if req == b"\x10\x89" else b"\x7f" + bytes([req[0], 0x11]))
    link = L.Link(TT.CanKLine(ecu, dest=0x01), [], "can")
    sess = ModuleSession(link, res.module_address("01", "C6"), res, generation="C6")
    sess.connect()
    from ovtsuite.diag.kwp2000 import Refused
    with pytest.raises(Refused):                 # KWP2000 adaptation exists now; this mock refuses the routine
        sess.adaptation_read(1)
    with pytest.raises(NotSupported):
        sess.kw1281
    with pytest.raises(NotSupported):
        sess.read_memory_kw("eeprom", 0, 8)
    sess.close()

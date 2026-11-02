# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag P2: the VAG KWP2000 function set (routine family B8/B9/BA/BB,
the 0x9A coding/login record, security access, freeze frames) through
ModuleSession, over a scripted KWP2000 module behind the TP 2.0 mock. The
wire forms are the ones KWHack's captures established."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_tp20 import MockTp20Ecu  # noqa: E402

from ovtsuite.flash import tp20_transport as TT
from ovtsuite.diag import defs as D
from ovtsuite.diag import link as L
from ovtsuite.diag.kwp2000 import Wsc
from ovtsuite.diag.session import ModuleSession, NotSupported

WSC6 = bytes.fromhex("78 90 32 B2 B2 6E")       # WSC 45678, importer 345, equipment 987654
SW4 = b"8449"


class Edc16Like:
    """The EDC16's KWP2000 behaviour as captured: session 0x89, 1A 9B record,
    18/14 faults with 12 00 04 freeze frames, the B8/B9/BA/BB routine family
    behind security access, the 0x9A coding/login record."""

    def __init__(self):
        self.part = b"03G906016LQ "
        self.coding = 71
        self.wsc6 = WSC6
        # two faults as VAG words (16785 = P0401, 16683 = P0299): the word
        # encoding is a per-calibration fact the module document states, so
        # the generic engine document reads KWP2000 words as VAG numbers
        self.faults = [(16785, 0x60), (16683, 0x61)]
        self.unlocked = False
        self.logged_in = False
        self.code = 12233
        self.seed = 0xAC7B56D1
        self.adapt = {2: 150, 9: 8192}
        self.channel = None
        self.pending_value = None
        self.saved = []
        self.mode = None            # "adapt" | "act" | ("basic", g)
        self.act_seq = [0x04F1, 0x04F2, 0x04AB]
        self.act_idx = 0
        self.log = []

    def record(self):
        return (b"\x9B" + self.part + SW4 + b"\x03" + self.coding.to_bytes(3, "big") + self.wsc6
                + b"R4 1,9L EDC G000SG")

    def __call__(self, req):
        self.log.append(bytes(req))
        svc, body = req[0], bytes(req[1:])
        nak = lambda nrc: bytes([0x7F, svc, nrc])     # noqa: E731
        if svc == 0x10:
            return b"\x50" + body
        if svc == 0x3E:
            return b"\x7E"
        if svc == 0x1A and body == b"\x9B":
            return b"\x5A" + self.record()
        if svc == 0x18:
            out = bytearray([len(self.faults)])
            for c, st in self.faults:
                out += bytes([c >> 8, c & 0xFF, st])
            return b"\x58" + bytes(out)
        if svc == 0x14:
            self.faults = []
            return b"\x54" + body
        if svc == 0x12:                     # freeze frame: 00 04 <dtc>
            dtc = (body[2] << 8) | body[3]
            if dtc not in [c for c, _ in self.faults]:
                return nak(0x31)
            return (b"\x52\x00" + bytes.fromhex("01 69 00 34 64 B9 07 64 00 21 FF 00")
                    + bytes([0x04, dtc >> 8, dtc & 0xFF]))
        if svc == 0x27:
            if body[0] == 0x03:
                return b"\x67\x03" + self.seed.to_bytes(4, "big")
            if body[0] == 0x04:
                key = int.from_bytes(body[1:5], "big")
                if key == (self.seed + self.code) & 0xFFFFFFFF:
                    self.unlocked = True
                    return b"\x67\x04\x34"
                return nak(0x35)
        if svc == 0x3B and body[0] == 0x9A:
            rec = body[1:]
            if rec[:6] != self.wsc6 or rec[6:10] != SW4 or rec[10] != 0x03:
                return nak(0x31)
            value = int.from_bytes(rec[11:14], "big")
            if value & 0x800000:
                self.logged_in = (value & 0x7FFFFF) == 11463
                return b"\x7B\x9A" if self.logged_in else nak(0x31)
            self.coding = value
            return b"\x7B\x9A"
        if svc == 0x31:
            rid, sel = body[0], bytes(body[1:3])
            if rid == 0xB8:
                if sel == b"\x00\x00":
                    return b"\x71\xB8\x01\x01\x01\x02\x01\x07\x01\x03\x01\x06\x01\x04\x01\x05\x01\x08\xFF\xF0"
                if sel == b"\x01\x03":
                    if not self.unlocked:
                        return nak(0x33)
                    self.mode = "adapt"
                    return b"\x71\xB8\x01\x03"
                if sel == b"\x01\x02":
                    self.mode, self.act_idx = "act", 0
                    return b"\x71\xB8\x01\x02"
                if sel[0] == 0x00:
                    g = sel[1]
                    if g != 60:
                        return nak(0x31)
                    self.mode = ("basic", g)
                    return b"\x71\xB8\x00" + bytes([g])
            if rid == 0xB9:
                if sel == b"\x01\x03" and self.mode == "adapt":
                    if len(body) == 4:                       # channel select
                        if body[3] not in self.adapt:
                            return nak(0x31)
                        self.channel = body[3]
                        return b"\x71\xB9\x01\x03"
                    if len(body) == 5:                       # test value
                        self.pending_value = (body[3] << 8) | body[4]
                        return b"\x71\xB9\x01\x03"
                if sel == b"\x01\x02" and self.mode == "act":
                    self.act_idx += 1
                    return b"\x71\xB9\x01\x02"
            if rid == 0xBA:
                if sel == b"\x01\x03" and self.mode == "adapt" and self.channel is not None:
                    v = self.pending_value if self.pending_value is not None else self.adapt[self.channel]
                    return (b"\x71\xBA\x01\x03\x00\x03" + v.to_bytes(2, "big") + b"\x04"
                            + bytes.fromhex("01 69 00 34 00 00 34 FF FF 34 64 B9"))
                if sel == b"\x01\x02" and self.mode == "act":
                    aid = self.act_seq[min(self.act_idx, len(self.act_seq) - 1)]
                    return b"\x71\xBA\x01\x02\x03\x03" + aid.to_bytes(2, "big") + b"\xFF"
                if sel[0] == 0x00 and self.mode == ("basic", sel[1]):
                    return b"\x71\xBA\x00" + bytes([sel[1], 0x04]) + bytes.fromhex("01 69 00 27 46 A6 31 C8 00 17 65 FF")
            if rid == 0xBB and sel == b"\x01\x03" and self.mode == "adapt":
                v = (body[3] << 8) | body[4]
                if bytes(body[5:11]) != self.wsc6:
                    return nak(0x31)
                self.adapt[self.channel] = v
                self.saved.append((self.channel, v))
                self.pending_value = None
                return b"\x71\xBB\x01\x03"
            return nak(0x31)
        if svc == 0x32 and body[0] == 0xB8:
            self.mode = None
            self.channel = None
            self.pending_value = None
            return b"\x72" + body
        return nak(0x11)


def _session(ecu):
    res = D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))
    row = res.module_address("01", "C6")
    link = L.Link(TT.CanKLine(MockTp20Ecu(ecu, dest=0x01), dest=0x01), [], "can")
    sess = ModuleSession(link, row, res, generation="C6")
    sess.connect()
    return sess


def test_faults_with_status_words_and_freeze_frames():
    ecu = Edc16Like()
    sess = _session(ecu)
    faults = sess.read_faults()
    assert [f.vag for f in faults] == [16785, 16683]
    from ovtsuite import dtc_report as _rep
    # the status words are OVT-Suite's own decoding (dtc_report), not a bare code
    for f, st in zip(faults, (0x60, 0x61)):
        si = _rep.decode_status(st, sess.controller, "kwp2000")   # per family once the part is known
        assert f.status_text == si.text and f.intermittent == si.intermittent
    assert faults[0].status_text.startswith("0x60") and "stored" in faults[0].status_text
    assert faults[0].freeze and faults[0].freeze_raw == ["01 69 00", "34 64 B9", "07 64 00", "21 FF 00"]
    assert any(b"\x12\x00\x04\x41\x91" == r for r in ecu.log)    # 12 00 04 <dtc> per fault
    sess.close()


def test_security_access_gates_the_routine_family():
    ecu = Edc16Like()
    sess = _session(ecu)
    from ovtsuite.diag.kwp2000 import Refused
    with pytest.raises(Refused):
        sess.adaptation_read(2)                                  # 7F 31 33 securityAccessDenied
    assert sess.security_access(12234) is False                  # wrong code -> 7F 27 35
    assert sess.security_access(12233) is True
    a = sess.adaptation_read(2)
    assert a.value == 150 and a.status == 0x00 and len(a.cells) == 4
    assert ecu.log[-1][:2] == b"\x32\xB8"                        # left the routine afterwards
    t = sess.adaptation_test(2, 160)
    assert t.value == 160 and ecu.adapt[2] == 150                # applied, not saved
    s = sess.adaptation_save(2, 160)                             # WSC: the module's own 6 bytes echoed
    assert s.value == 160 and ecu.saved == [(2, 160)]
    with pytest.raises(Refused):
        sess.adaptation_read(99)                                 # requestOutOfRange on select
    sess.close()


def test_coding_and_login_through_the_9a_record():
    ecu = Edc16Like()
    sess = _session(ecu)
    assert sess.info.coding == 71 and sess.info.wsc == 45678
    assert sess.info.extra["importer"] == 345 and sess.info.extra["equipment"] == 987654
    import ovtsuite.diag.session as S
    S.CODING_SETTLE_S = 0.0
    ok, coding, wsc = sess.set_coding(150034)
    assert ok and coding == 150034 and ecu.coding == 150034 and wsc == 45678
    written = [r for r in ecu.log if r[:2] == b"\x3B\x9A"][-1]
    assert written[2:8] == WSC6 and written[8:12] == SW4 and written[12] == 0x03
    assert sess.login(11463) is True and ecu.logged_in
    assert sess.login(1) is False
    assert Wsc.decode(WSC6) == Wsc(45678, 345, 987654) and Wsc(45678, 345, 987654).encode() == WSC6
    sess.close()


def test_actuator_sequence_and_basic_setting():
    ecu = Edc16Like()
    sess = _session(ecu)
    steps = []
    while True:
        st = sess.actuator_next()
        steps.append(st)
        if st.ended:
            break
    assert [s.id for s in steps] == [0x04F1, 0x04F2, 0x04AB]
    assert ecu.mode is None                                      # 32 B8 01 02 sent at the end
    b = sess.basic_setting_read(60)
    assert b.available and len(b.cells) == 4 and b.cells[1].fid == 0x27
    assert ecu.mode == ("basic", 60)
    b2 = sess.basic_setting_read(60)
    assert b2.available and ecu.log.count(b"\x31\xB8\x00\x3C") == 1    # entered once, read twice
    assert not sess.basic_setting_read(61).available
    sess.close()
    assert ecu.mode is None


def test_kw1281_only_functions_on_kwp2000():
    sess = _session(Edc16Like())
    with pytest.raises(NotSupported):
        sess.read_memory_kw("eeprom", 0, 8)
    with pytest.raises(NotSupported):
        sess.kw1281
    sess.close()


def test_platform_from_factory_catalogue_and_catalogue_cell_names():
    """A served part number that is in the EDC16 factory catalogue gives the
    module its platform (so dtc_report decodes the symptom per family) and
    names measuring cells from that calibration when the definitions have
    none."""
    from ovtsuite.datalog import msbcatalogue as _cat
    cat = _cat.load("edc16")
    if cat is None or not getattr(cat, "_by_part", {}):
        pytest.skip("no EDC16 catalogue in this checkout")
    part = sorted(cat._by_part)[0]
    ecu = Edc16Like()
    ecu.part = part.encode().ljust(12)
    sess = _session(ecu)
    assert sess.info.platform == "edc16"
    assert sess.controller == "EDC16"
    namer = sess.catalogue_namer
    assert namer is not None and namer.available
    g = namer.populated_groups()[0]
    names = sess.group_names(g)
    assert names and any(names)
    sess.close()
